from __future__ import annotations

import asyncio
import re
import threading
from datetime import datetime
from concurrent.futures import CancelledError as FutureCancelledError, Future
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import QObject, Signal

from .agent_adapter import _env_value
from .agent_models import CoachResponse
from .agent_store import AgentStore
from .agent_tools import AgentRunContext, approval_preview, create_read_tools, create_write_tools
from .storage import load_settings


_PERSONAL_GOAL_PATTERNS = (
    re.compile(r"我的学习目标"),
    re.compile(r"(?:根据|基于|按照|结合|参考).{0,8}学习目标"),
    re.compile(r"学习目标.{0,8}(?:制定|安排|规划|生成)"),
    re.compile(r"\bmy\s+(?:learning\s+)?goals?\b", re.IGNORECASE),
)


def _blocked_resource_response(message: str, grants: dict[str, bool]) -> CoachResponse | None:
    """Fail closed when a prompt explicitly requires an ungranted private resource."""
    if grants.get("goals", False):
        return None
    compact = re.sub(r"\s+", "", message)
    if not any(pattern.search(compact) for pattern in _PERSONAL_GOAL_PATTERNS):
        return None
    return CoachResponse(
        body=(
            "“学习目标”目前未授权，因此我不能读取或声称依据你的个人目标制定计划。\n\n"
            "请在 Agent 中心右侧勾选“学习目标”后重新发送；你也可以保持关闭，并让我制定一份不使用个人数据的通用复习计划。"
        ),
        intent="permission_required",
    )


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    base_url: str
    model: str

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None = None) -> DeepSeekConfig:
        assistant = ((settings or load_settings()).get("assistant") or {})
        return cls(
            api_key=_env_value("DEEPSEEK_API_KEY"),
            base_url=str(assistant.get("deepseek_base_url") or "https://api.deepseek.com").rstrip("/"),
            model=str(assistant.get("deepseek_model") or "deepseek-v4-flash"),
        )


class DeepSeekModelProvider:
    """Build a per-runtime DeepSeek client without touching OpenAI globals."""

    def __init__(self, config: DeepSeekConfig) -> None:
        self.config = config
        self._client: Any = None
        self._model: Any = None

    def availability_error(self) -> str:
        if not self.config.api_key:
            return "缺少 DEEPSEEK_API_KEY；本地知识库与复习功能仍可正常使用。"
        try:
            import agents  # noqa: F401
            import openai  # noqa: F401
        except ImportError:
            return "缺少 openai-agents 依赖；请重新安装 requirements.txt。"
        return ""

    def model(self) -> Any:
        error = self.availability_error()
        if error:
            raise RuntimeError(error)
        if self._model is not None:
            return self._model
        from agents import OpenAIChatCompletionsModel, set_tracing_disabled
        from openai import AsyncOpenAI

        set_tracing_disabled(True)
        self._client = AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            max_retries=0,
            timeout=85.0,
        )
        self._model = OpenAIChatCompletionsModel(model=self.config.model, openai_client=self._client)
        return self._model

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def test_capabilities(self) -> dict[str, Any]:
        self.model()
        response = await self._client.chat.completions.create(
            model=self.config.model,
            messages=[{"role": "user", "content": "合成测试：调用工具查询 spring，并使用合法 JSON 参数。"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "synthetic_search",
                    "description": "Synthetic capability test only",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }],
            tool_choice={"type": "function", "function": {"name": "synthetic_search"}},
            temperature=0,
        )
        message = response.choices[0].message if response.choices else None
        calls = list(message.tool_calls or []) if message else []
        arguments = calls[0].function.arguments if calls else ""
        try:
            parsed = __import__("json").loads(arguments)
        except (TypeError, ValueError):
            parsed = {}
        tool_ok = bool(calls and isinstance(parsed.get("query"), str))
        return {
            "chat": message is not None,
            "function_tool": bool(calls),
            "json_arguments": bool(parsed),
            "argument_validation": tool_ok,
            "multi_agent_enabled": tool_ok,
            "synthetic": True,
            "request_count": 1,
        }


@dataclass
class BackendOutcome:
    response: CoachResponse
    source_ids: list[str]
    metadata: dict[str, Any]
    pending: PendingBackendApproval | None = None


@dataclass
class PendingBackendApproval:
    state: Any
    interruption: Any
    agent: Any
    context: AgentRunContext


class AgentBackend(Protocol):
    async def run(self, *, prompt: str, context: AgentRunContext, history: list[dict[str, Any]]) -> BackendOutcome: ...

    async def close(self) -> None: ...

    async def resume(self, pending: PendingBackendApproval, authorized_at: str) -> BackendOutcome: ...


class AgentsSDKBackend:
    def __init__(self, provider: DeepSeekModelProvider) -> None:
        self.provider = provider

    def _agent(self, use_specialists: bool = False) -> Any:
        from agents import Agent

        model = self.provider.model()
        specialist_tools = []
        if use_specialists:
            knowledge = Agent(name="Knowledge Tutor", instructions="检索本地知识并解释来源；不得写入。", model=model, tools=create_read_tools())
            practice = Agent(name="Practice Analyst", instructions="分析答案命中点、遗漏点和追问；不得替用户自评。", model=model, tools=create_read_tools())
            planner = Agent(name="Review Planner", instructions="基于复习、错题及授权目标提出计划；不得写入。", model=model, tools=create_read_tools())
            specialist_tools = [
                knowledge.as_tool("consult_knowledge_tutor", "Ask the knowledge specialist"),
                practice.as_tool("consult_practice_analyst", "Ask the practice specialist"),
                planner.as_tool("consult_review_planner", "Ask the review specialist"),
            ]
        return Agent(
            name="Interview Coach",
            instructions=(
                "你是 Table Miku 的 Java 后端面试学习教练，也是唯一对话出口。"
                "知识检索、复习状态和写入必须通过提供的本地工具完成；禁止声称访问原始 Vault、文件系统、Shell 或网络搜索。"
                "练习时先让用户独立作答，提交前不得展示参考答案；反馈不能替代用户的掌握度自评。"
                "引用资料时保留工具返回的 source_id。若资源未授权，说明需要用户在 Agent 中心开启对应开关。"
            ),
            model=model,
            tools=create_read_tools() + create_write_tools() + specialist_tools,
        )

    async def run(self, *, prompt: str, context: AgentRunContext, history: list[dict[str, Any]]) -> BackendOutcome:
        from agents import RunConfig, Runner

        capability = context.store.load_capability(self.provider.config.base_url, self.provider.config.model) or {}
        use_specialists = bool(capability.get("multi_agent_enabled"))
        agent = self._agent(use_specialists)
        history_text = "\n".join(
            f"{item.get('role', 'user')}: {item.get('content', '')}" for item in history[-20:]
        )
        model_input = f"最近会话：\n{history_text}\n\n当前用户消息：\n{prompt}" if history_text else prompt
        result = await Runner.run(
            agent,
            model_input,
            context=context,
            max_turns=8,
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        )
        if result.interruptions:
            interruption = result.interruptions[0]
            preview = approval_preview(str(interruption.tool_name or ""), str(interruption.arguments or "{}"))
            return BackendOutcome(
                response=CoachResponse(body="Agent 请求执行一项本地写操作，请检查下方预览。", approval_request=preview),
                source_ids=list(context.sources),
                metadata={"provider": "deepseek", "model": self.provider.config.model, "mode": "awaiting-approval"},
                pending=PendingBackendApproval(result.to_state(), interruption, agent, context),
            )
        response = CoachResponse(
            body=str(result.final_output or "").strip() or "DeepSeek 已返回，但没有可展示的文本。",
            sources=list(context.sources.values()),
        )
        return BackendOutcome(
            response=response,
            source_ids=list(context.sources),
            metadata={"provider": "deepseek", "model": self.provider.config.model, "mode": "multi-agent" if use_specialists else "single-agent"},
        )

    async def resume(self, pending: PendingBackendApproval, authorized_at: str) -> BackendOutcome:
        from agents import RunConfig, Runner

        preview = approval_preview(
            str(pending.interruption.tool_name or ""),
            str(pending.interruption.arguments or "{}"),
        )
        pending.context.authorized_at[preview.operation_id] = authorized_at
        pending.state.approve(pending.interruption)
        result = await Runner.run(
            pending.agent,
            pending.state,
            max_turns=8,
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        )
        if result.interruptions:
            raise RuntimeError("一次审批仅允许一项写操作；后续写请求已终止。")
        response = CoachResponse(
            body=str(result.final_output or "").strip() or "操作已执行。",
            sources=list(pending.context.sources.values()),
        )
        return BackendOutcome(
            response=response,
            source_ids=list(pending.context.sources),
            metadata={"provider": "deepseek", "model": self.provider.config.model, "mode": "approved-write"},
        )

    async def close(self) -> None:
        await self.provider.close()


class AgentRuntimeCore:
    def __init__(
        self,
        store: AgentStore | None = None,
        backend: AgentBackend | None = None,
        *,
        timeout_seconds: float = 90.0,
    ) -> None:
        self.store = store or AgentStore()
        self.provider = DeepSeekModelProvider(DeepSeekConfig.from_settings())
        self.backend = backend or AgentsSDKBackend(self.provider)
        self.timeout_seconds = min(max(float(timeout_seconds), 0.01), 90.0)
        self._active_task: asyncio.Task[BackendOutcome] | None = None
        self._pending: tuple[str, str, PendingBackendApproval] | None = None

    async def submit(self, session_id: str, text: str) -> CoachResponse:
        message = text.strip()
        if not message:
            raise ValueError("消息不能为空。")
        if self._active_task is not None and not self._active_task.done():
            raise RuntimeError("已有一个 Agent 运行中，请先停止或等待完成。")
        if not any(item["id"] == session_id for item in self.store.list_sessions()):
            raise ValueError("会话不存在。")

        self.store.add_message(session_id, "user", message)
        history = self.store.list_messages(session_id, limit=100)[:-1]
        run_id = self.store.start_run(session_id)
        blocked = _blocked_resource_response(message, self.store.resource_grants())
        if blocked is not None:
            self.store.add_message(session_id, "assistant", blocked.body, run_id=run_id)
            self.store.finish_run(
                run_id,
                "completed",
                metadata={"mode": "permission-blocked", "resource": "goals"},
            )
            return blocked
        context = AgentRunContext(store=self.store, session_id=session_id)
        self._active_task = asyncio.create_task(self.backend.run(prompt=message, context=context, history=history))
        try:
            outcome = await asyncio.wait_for(self._active_task, timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            self.store.finish_run(run_id, "timeout", error="运行超过 90 秒，已取消。")
            raise RuntimeError("DeepSeek Agent 运行超过 90 秒，已停止；不会自动重试。") from exc
        except asyncio.CancelledError:
            self.store.finish_run(run_id, "cancelled", error="用户取消")
            raise
        except Exception as exc:
            message_for_user = friendly_agent_error(exc)
            self.store.finish_run(run_id, "failed", error=message_for_user)
            raise RuntimeError(message_for_user) from exc
        finally:
            self._active_task = None

        if outcome.pending is not None and outcome.response.approval_request is not None:
            operation_id = outcome.response.approval_request.operation_id
            self._pending = (session_id, run_id, outcome.pending)
            self.store.set_run_status(run_id, "awaiting_approval")
            return outcome.response

        self.store.add_message(
            session_id,
            "assistant",
            outcome.response.body,
            run_id=run_id,
            source_ids=outcome.source_ids,
        )
        self.store.finish_run(run_id, "completed", metadata=outcome.metadata)
        return outcome.response

    async def approve(self, operation_id: str) -> CoachResponse:
        if self._pending is None:
            raise RuntimeError("当前没有待审批操作。")
        session_id, run_id, pending = self._pending
        preview = approval_preview(str(pending.interruption.tool_name or ""), str(pending.interruption.arguments or "{}"))
        if preview.operation_id != operation_id:
            raise RuntimeError("审批 operation_id 与待执行操作不一致。")
        authorized_at = datetime.now().isoformat(timespec="seconds")
        self._active_task = asyncio.create_task(self.backend.resume(pending, authorized_at))
        try:
            outcome = await asyncio.wait_for(self._active_task, timeout=self.timeout_seconds)
        except Exception as exc:
            message = friendly_agent_error(exc)
            self.store.finish_run(run_id, "failed", error=message)
            raise RuntimeError(message) from exc
        finally:
            self._active_task = None
            self._pending = None
        self.store.add_message(session_id, "assistant", outcome.response.body, run_id=run_id, source_ids=outcome.source_ids)
        self.store.finish_run(run_id, "completed", metadata=outcome.metadata)
        return outcome.response

    async def reject(self, operation_id: str) -> CoachResponse:
        if self._pending is None:
            raise RuntimeError("当前没有待审批操作。")
        session_id, run_id, pending = self._pending
        preview = approval_preview(str(pending.interruption.tool_name or ""), str(pending.interruption.arguments or "{}"))
        if preview.operation_id != operation_id:
            raise RuntimeError("拒绝 operation_id 与待执行操作不一致。")
        pending.state.reject(pending.interruption, rejection_message="用户拒绝了本次写入。")
        message = f"已拒绝“{preview.title}”，本地数据未发生变化。"
        self.store.add_message(session_id, "assistant", message, run_id=run_id)
        self.store.finish_run(run_id, "rejected", metadata={"operation_id": operation_id})
        self._pending = None
        return CoachResponse(body=message)

    def cancel(self) -> bool:
        if self._active_task is None or self._active_task.done():
            return False
        self._active_task.cancel()
        return True

    async def close(self) -> None:
        self.cancel()
        await self.backend.close()


def friendly_agent_error(exc: Exception) -> str:
    text = str(exc).strip()
    status = getattr(exc, "status_code", None)
    lowered = text.lower()
    if status == 401 or "401" in lowered or "authentication" in lowered:
        return "DeepSeek API 认证失败，请检查 DEEPSEEK_API_KEY；本次不会自动重试。"
    if status == 429 or "429" in lowered or "rate limit" in lowered:
        if "balance" in lowered or "credit" in lowered or "quota" in lowered:
            return "DeepSeek API 余额或额度不足；当前会话已保留，本次不会自动重试。"
        return "DeepSeek API 触发速率限制；当前会话已保留，本次不会自动重试。"
    if "timeout" in lowered or "timed out" in lowered:
        return "DeepSeek API 连接超时；当前会话已保留，本次不会自动重试。"
    if "tool" in lowered and ("unsupported" in lowered or "not support" in lowered):
        return "当前 DeepSeek 模型不支持所需工具调用；请先运行能力测试。"
    return text or "DeepSeek Agent 运行失败；当前会话已保留，本次不会自动重试。"


class _AsyncLoopThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="table-miku-agent-runtime", daemon=True)
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()


class AgentRuntime(QObject):
    progress = Signal(str, str)
    response_ready = Signal(str, object)
    failed = Signal(str, str)
    sessions_changed = Signal()
    capability_ready = Signal(object)

    def __init__(self, store: AgentStore | None = None, backend: AgentBackend | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.core = AgentRuntimeCore(store=store, backend=backend)
        self.store = self.core.store
        self._thread = _AsyncLoopThread()
        self._thread.start()
        self._thread.ready.wait(timeout=3)
        self._future: Future[Any] | None = None

    def new_session(self, title: str = "新会话") -> str:
        session_id = self.store.create_session(title)
        self.sessions_changed.emit()
        return session_id

    def delete_session(self, session_id: str) -> bool:
        deleted = self.store.delete_session(session_id)
        if deleted:
            self.sessions_changed.emit()
        return deleted

    def submit(self, session_id: str, text: str) -> bool:
        if self._future is not None and not self._future.done():
            self.failed.emit(session_id, "已有一个 Agent 运行中。")
            return False
        self.progress.emit(session_id, "正在调用 DeepSeek 面试教练…")
        self._future = asyncio.run_coroutine_threadsafe(self.core.submit(session_id, text), self._thread.loop)
        self._future.add_done_callback(lambda future: self._complete(session_id, future))
        return True

    def approve(self, operation_id: str) -> bool:
        if self._future is not None and not self._future.done():
            return False
        session_id = self.core._pending[0] if self.core._pending else ""
        self.progress.emit(session_id, "正在执行已批准的本地操作…")
        self._future = asyncio.run_coroutine_threadsafe(self.core.approve(operation_id), self._thread.loop)
        self._future.add_done_callback(lambda future: self._complete(session_id, future))
        return True

    def reject(self, operation_id: str) -> bool:
        if self._future is not None and not self._future.done():
            return False
        session_id = self.core._pending[0] if self.core._pending else ""
        self._future = asyncio.run_coroutine_threadsafe(self.core.reject(operation_id), self._thread.loop)
        self._future.add_done_callback(lambda future: self._complete(session_id, future))
        return True

    def cancel(self) -> bool:
        if self._future is None or self._future.done():
            return False
        self._thread.loop.call_soon_threadsafe(self.core.cancel)
        self._future.cancel()
        return True

    def test_capabilities(self) -> bool:
        if self._future is not None and not self._future.done():
            return False

        async def run_test() -> dict[str, Any]:
            result = await self.core.provider.test_capabilities()
            config = self.core.provider.config
            self.store.save_capability(config.base_url, config.model, result)
            return result

        self._future = asyncio.run_coroutine_threadsafe(run_test(), self._thread.loop)
        self._future.add_done_callback(self._capability_complete)
        return True

    def shutdown(self) -> None:
        if not self._thread.is_alive():
            return
        future = asyncio.run_coroutine_threadsafe(self.core.close(), self._thread.loop)
        try:
            future.result(timeout=3)
        except Exception:
            pass
        self._thread.loop.call_soon_threadsafe(self._thread.loop.stop)
        self._thread.join(timeout=3)

    def _complete(self, session_id: str, future: Future[Any]) -> None:
        try:
            response = future.result()
        except (asyncio.CancelledError, FutureCancelledError):
            self.failed.emit(session_id, "运行已取消。")
        except Exception as exc:
            self.failed.emit(session_id, str(exc))
        else:
            self.response_ready.emit(session_id, response)

    def _capability_complete(self, future: Future[Any]) -> None:
        try:
            result = future.result()
        except Exception as exc:
            self.failed.emit("", friendly_agent_error(exc))
        else:
            self.capability_ready.emit(result)
