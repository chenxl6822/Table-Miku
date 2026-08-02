from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import CancelledError as FutureCancelledError, Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from table_miku.agent_evaluation import (
    TOPOLOGY_EVAL_CASES,
    build_topology_evaluation,
    capability_supports_specialists,
    specialists_enabled,
)
from table_miku.agent_models import CoachResponse
from table_miku.agent_runtime import (
    AgentRuntime,
    AgentRuntimeCore,
    AgentsSDKBackend,
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

    async def evaluate_topologies(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        samples = []
        for case in TOPOLOGY_EVAL_CASES:
            complete = " ".join(group[0] for group in case.required_groups)
            samples.append(
                {
                    "name": case.name,
                    "single_output": complete.rsplit(" ", 1)[0],
                    "multi_output": complete,
                    "multi_tools": [case.specialist_tool],
                }
            )
        return build_topology_evaluation(samples)


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


@pytest.mark.parametrize(
    ("resource", "prompt", "label"),
    (
        ("knowledge", "请从本地知识库检索 Spring IoC", "知识库"),
        ("review", "打开我的错题本", "复习与错题"),
        ("goals", "根据我的学习目标制定今天的复习计划", "学习目标"),
        ("timetable", "查看我的课程表", "课程表"),
        ("interviews", "根据我的投递记录安排下一步", "投递/面试记录"),
    ),
)
def test_ungranted_resources_are_blocked_before_model_call(
    tmp_path: Path, resource: str, prompt: str, label: str
):
    store = AgentStore(tmp_path / "agent.db")
    store.set_resource_grant(resource, False)
    session_id = store.create_session()
    backend = FakeBackend()
    core = AgentRuntimeCore(store=store, backend=backend)

    response = asyncio.run(core.submit(session_id, prompt))

    assert response.intent == "permission_required"
    assert label in response.body
    assert "未授权" in response.body
    assert backend.prompts == []
    assert [item["role"] for item in store.list_messages(session_id)] == ["user", "assistant"]


@pytest.mark.parametrize(
    ("resource", "prompt"),
    (
        ("knowledge", "请从本地知识库检索 Spring IoC"),
        ("review", "打开我的错题本"),
        ("goals", "根据我的学习目标制定今天的复习计划"),
        ("timetable", "查看我的课程表"),
        ("interviews", "根据我的投递记录安排下一步"),
    ),
)
def test_granted_resources_can_reach_model(tmp_path: Path, resource: str, prompt: str):
    store = AgentStore(tmp_path / "agent.db")
    store.set_resource_grant(resource, True)
    session_id = store.create_session()
    backend = FakeBackend()
    core = AgentRuntimeCore(store=store, backend=backend)

    response = asyncio.run(core.submit(session_id, prompt))

    assert response.body.startswith("fake answer")
    assert backend.prompts == [prompt]


def test_resource_revocation_takes_effect_on_next_request(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    store.set_resource_grant("timetable", True)
    session_id = store.create_session()
    backend = FakeBackend()
    core = AgentRuntimeCore(store=store, backend=backend)

    first = asyncio.run(core.submit(session_id, "查看我的课程表"))
    store.set_resource_grant("timetable", False)
    second = asyncio.run(core.submit(session_id, "查看我的课程表"))

    assert first.body.startswith("fake answer")
    assert second.intent == "permission_required"
    assert backend.prompts == ["查看我的课程表"]


@pytest.mark.parametrize(
    "prompt",
    (
        "解释 Spring IoC",
        "制定一份不使用个人数据的通用复习计划",
        "如何改进我的复习方法",
        "我的面试应该怎么准备",
    ),
)
def test_generic_requests_do_not_require_private_resource_grants(tmp_path: Path, prompt: str):
    store = AgentStore(tmp_path / "agent.db")
    for resource in store.resource_grants():
        store.set_resource_grant(resource, False)
    session_id = store.create_session()
    backend = FakeBackend()
    core = AgentRuntimeCore(store=store, backend=backend)

    response = asyncio.run(core.submit(session_id, prompt))

    assert response.body.startswith("fake answer")
    assert backend.prompts == [prompt]


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


def test_agent_instructions_include_read_grant_boundaries():
    provider = DeepSeekModelProvider(
        DeepSeekConfig(api_key="deepseek-test", base_url="https://api.deepseek.test", model="chat-test")
    )
    provider._model = "chat-test"
    backend = AgentsSDKBackend(provider)

    agent = backend._agent(False, {"knowledge": True, "goals": False})

    assert "知识库=允许只读" in agent.instructions
    assert "学习目标=未授权" in agent.instructions
    assert "禁止读取、推断或声称使用未授权资源" in agent.instructions

    multi = backend._agent(True, {"knowledge": True})
    specialist_tools = [tool for tool in multi.tools if tool.name.startswith("consult_")]
    assert len(specialist_tools) == 3
    assert all(tool.strict_json_schema is False for tool in specialist_tools)


def test_synthetic_topology_agents_never_expose_production_tools():
    provider = DeepSeekModelProvider(
        DeepSeekConfig(api_key="deepseek-test", base_url="https://api.deepseek.test", model="chat-test")
    )
    provider._model = "chat-test"
    backend = AgentsSDKBackend(provider)

    for case in TOPOLOGY_EVAL_CASES:
        single = backend._evaluation_agent(case, False)
        multi = backend._evaluation_agent(case, True)

        assert single.tools == []
        assert [tool.name for tool in multi.tools] == [case.specialist_tool]
        assert multi.tools[0].strict_json_schema is False
        assert multi.model_settings.tool_choice == case.specialist_tool
        assert multi.model_settings.parallel_tool_calls is False
        assert "不得调用或声称读取本地知识库" in multi.instructions


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
        "multi_agent_capable": True,
        "synthetic": True,
        "request_count": 1,
    }
    assert completions.request["model"] == "chat-test"
    assert completions.request["tool_choice"]["function"]["name"] == "synthetic_search"


def test_topology_scoring_requires_quality_and_correct_specialist_routing():
    passing_samples = []
    wrong_route_samples = []
    for case in TOPOLOGY_EVAL_CASES:
        complete = " ".join(group[0] for group in case.required_groups)
        single = " ".join(group[0] for group in case.required_groups[:-1])
        passing_samples.append(
            {
                "name": case.name,
                "single_output": single,
                "multi_output": complete,
                "multi_tools": [case.specialist_tool],
            }
        )
        wrong_route_samples.append(
            {
                "name": case.name,
                "single_output": single,
                "multi_output": complete,
                "multi_tools": ["consult_wrong_specialist"],
            }
        )

    passing = build_topology_evaluation(passing_samples)
    wrong_route = build_topology_evaluation(wrong_route_samples)

    assert passing["passed"] is True
    assert passing["multi_score"] > passing["single_score"]
    assert passing["routing_passed"] is True
    assert wrong_route["passed"] is False
    assert wrong_route["routing_passed"] is False


def test_specialists_require_capability_and_passing_quality_evaluation():
    capability = {"multi_agent_capable": True}

    assert capability_supports_specialists(capability)
    assert capability_supports_specialists({"multi_agent_enabled": True})
    assert not specialists_enabled(capability, None)
    assert not specialists_enabled(capability, {"passed": False})
    assert specialists_enabled(capability, {"passed": True})


def test_topology_evaluation_persists_separately_from_capability(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    base_url = "https://api.deepseek.test"
    model = "chat-test"
    store.save_capability(base_url, model, {"multi_agent_capable": True})
    store.save_topology_evaluation(base_url, model, {"passed": True, "single_score": 80, "multi_score": 90})

    assert store.load_capability(base_url, model)["multi_agent_capable"] is True
    evaluation = store.load_topology_evaluation(base_url, model)
    assert evaluation["passed"] is True
    assert evaluation["single_score"] == 80
    assert evaluation["multi_score"] == 90
    assert evaluation["evaluated_at"]


def test_runtime_topology_evaluation_uses_fake_backend_and_persists(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    runtime = AgentRuntime(store=store, backend=FakeBackend())
    config = runtime.core.provider.config
    store.save_capability(config.base_url, config.model, {"multi_agent_capable": True})
    try:
        assert runtime.evaluate_topologies()
        result = runtime._future.result(timeout=3)

        assert result["passed"] is True
        assert result["real_user_data_used"] is False
        saved = store.load_topology_evaluation(config.base_url, config.model)
        assert saved["passed"] is True
    finally:
        runtime.shutdown()


def test_runtime_topology_evaluation_requires_capability_and_times_out_without_retry(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    runtime = AgentRuntime(store=store, backend=FakeBackend(delay=0.05))
    config = runtime.core.provider.config
    failures: list[str] = []
    runtime.topology_evaluation_failed.connect(failures.append)
    try:
        assert not runtime.evaluate_topologies()
        assert failures == ["请先通过当前接口与模型的 DeepSeek Agent 能力测试。"]

        store.save_capability(config.base_url, config.model, {"multi_agent_capable": True})
        runtime.core.timeout_seconds = 0.01
        assert runtime.evaluate_topologies()
        with pytest.raises(RuntimeError, match="超过 0.01 秒"):
            runtime._future.result(timeout=3)
        assert store.load_topology_evaluation(config.base_url, config.model) is None
    finally:
        runtime.shutdown()


def test_runtime_topology_evaluation_can_be_cancelled_without_saving_result(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db")
    runtime = AgentRuntime(store=store, backend=FakeBackend(delay=0.5))
    config = runtime.core.provider.config
    store.save_capability(config.base_url, config.model, {"multi_agent_capable": True})
    try:
        assert runtime.evaluate_topologies()
        assert runtime.cancel()
        with pytest.raises(FutureCancelledError):
            runtime._future.result(timeout=3)
        assert store.load_topology_evaluation(config.base_url, config.model) is None
    finally:
        runtime.shutdown()


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
