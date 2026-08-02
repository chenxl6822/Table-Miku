from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import Future

from table_miku.agent_models import CoachResponse
from table_miku.agent_runtime import (
    AgentRuntime,
    AgentRuntimeCore,
    BackendOutcome,
    DeepSeekConfig,
    DeepSeekModelProvider,
    friendly_agent_error,
)
from table_miku.agent_store import AgentStore
from table_miku.agent_tools import AgentRunContext, create_read_tools


class FakeBackend:
    def __init__(self, text: str = "fake answer", delay: float = 0) -> None:
        self.text = text
        self.delay = delay
        self.closed = False
        self.prompts: list[str] = []

    async def run(self, *, prompt, context, history):
        del context, history
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        return BackendOutcome(CoachResponse(body=f"{self.text}: {prompt}"), [], {"fake": True})

    async def close(self):
        self.closed = True


def test_agent_store_redacts_trims_and_restores_sessions(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    session_id = store.create_session("Spring")
    store.add_message(session_id, "user", "api_key=ds-abcdefghijklmnop")
    for index in range(105):
        store.add_message(session_id, "assistant", f"message-{index}")

    messages = store.list_messages(session_id)
    assert len(messages) == 100
    assert "ds-abcdefghijklmnop" not in json.dumps(messages)
    assert messages[-1]["content"] == "message-104"

    restored = AgentStore(tmp_path / "agent.db")
    assert restored.list_sessions()[0]["id"] == session_id
    assert restored.delete_session(session_id)


def test_interrupted_runs_are_cancelled_on_restart(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    session_id = store.create_session()
    run_id = store.start_run(session_id)

    AgentStore(tmp_path / "agent.db")

    with sqlite3.connect(tmp_path / "agent.db") as conn:
        status = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert status == "cancelled"


def test_runtime_uses_fake_backend_without_network(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    session_id = store.create_session()
    backend = FakeBackend()
    core = AgentRuntimeCore(store=store, backend=backend)

    response = asyncio.run(core.submit(session_id, "解释 Spring IoC"))

    assert response.body == "fake answer: 解释 Spring IoC"
    assert [item["role"] for item in store.list_messages(session_id)] == ["user", "assistant"]
    asyncio.run(core.close())
    assert backend.closed


def test_ungranted_learning_goals_are_blocked_before_model_call(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    session_id = store.create_session()
    backend = FakeBackend()
    core = AgentRuntimeCore(store=store, backend=backend)

    response = asyncio.run(core.submit(session_id, "根据我的学习目标制定今天的复习计划"))

    assert response.intent == "permission_required"
    assert "学习目标" in response.body
    assert "未授权" in response.body
    assert backend.prompts == []
    assert [item["role"] for item in store.list_messages(session_id)] == ["user", "assistant"]


def test_granted_learning_goals_can_reach_model(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    store.set_resource_grant("goals", True)
    session_id = store.create_session()
    backend = FakeBackend()
    core = AgentRuntimeCore(store=store, backend=backend)

    response = asyncio.run(core.submit(session_id, "根据我的学习目标制定今天的复习计划"))

    assert response.body.startswith("fake answer")
    assert backend.prompts == ["根据我的学习目标制定今天的复习计划"]


def test_runtime_timeout_keeps_user_message(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    session_id = store.create_session()
    core = AgentRuntimeCore(store=store, backend=FakeBackend(delay=0.05), timeout_seconds=0.01)

    try:
        asyncio.run(core.submit(session_id, "timeout"))
    except RuntimeError as exc:
        assert "90 秒" in str(exc)
    else:
        raise AssertionError("timeout should fail")
    assert [item["role"] for item in store.list_messages(session_id)] == ["user"]


def test_deepseek_provider_does_not_require_openai_key():
    config = DeepSeekConfig(api_key="deepseek-test", base_url="https://api.deepseek.test", model="chat-test")
    provider = DeepSeekModelProvider(config)
    model = provider.model()

    assert model.model == "chat-test"
    assert str(model._client.base_url).rstrip("/") == "https://api.deepseek.test"
    assert model._client.max_retries == 0
    asyncio.run(provider.close())


def test_deepseek_capability_check_validates_synthetic_tool_arguments():
    class FakeCompletions:
        def __init__(self) -> None:
            self.request = {}

        async def create(self, **kwargs):
            self.request = kwargs
            function = SimpleNamespace(name="synthetic_search", arguments='{"query":"spring"}')
            message = SimpleNamespace(tool_calls=[SimpleNamespace(function=function)])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = DeepSeekModelProvider(
        DeepSeekConfig(api_key="deepseek-test", base_url="https://api.deepseek.test", model="chat-test")
    )
    provider._client = client
    provider._model = object()

    result = asyncio.run(provider.test_capabilities())

    assert result == {
        "chat": True,
        "chat_completion": True,
        "function_tool": True,
        "tool_name": "synthetic_search",
        "json_arguments": True,
        "argument_validation": True,
        "multi_agent_enabled": True,
        "synthetic": True,
        "request_count": 1,
    }
    assert completions.request["model"] == "chat-test"
    assert completions.request["tool_choice"]["function"]["name"] == "synthetic_search"


def test_capability_failure_uses_dedicated_signal(tmp_path: Path):
    runtime = AgentRuntime(store=AgentStore(tmp_path / "agent.db"), backend=FakeBackend())
    messages: list[str] = []
    runtime.capability_failed.connect(messages.append)
    failed: Future[object] = Future()
    failed.set_exception(RuntimeError("HTTP 401 authentication"))
    try:
        runtime._capability_complete(failed)
        assert messages == ["DeepSeek API 认证失败，请检查 DEEPSEEK_API_KEY；本次不会自动重试。"]
    finally:
        runtime.shutdown()


def test_non_strict_tool_validation_allows_only_one_repair(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    session_id = store.create_session()
    context = AgentRunContext(store=store, session_id=session_id)
    tool = next(item for item in create_read_tools() if item.name == "search_local_knowledge")

    first = asyncio.run(tool.on_invoke_tool(SimpleNamespace(context=context), "{}"))
    second = asyncio.run(tool.on_invoke_tool(SimpleNamespace(context=context), "{}"))

    assert tool.strict_json_schema is False
    assert json.loads(first)["repair_allowed"] is True
    assert json.loads(second)["repair_allowed"] is False


def test_friendly_agent_errors_are_specific():
    assert "认证失败" in friendly_agent_error(RuntimeError("HTTP 401 authentication"))
    assert "余额" in friendly_agent_error(RuntimeError("HTTP 429 insufficient balance"))
    assert "超时" in friendly_agent_error(RuntimeError("request timeout"))
