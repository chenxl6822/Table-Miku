from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError as FutureCancelledError, Future
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import QObject, Signal

from .agent_adapter import _env_value
from .agent_models import CoachResponse
from .agent_store import AgentStore
from .agent_tools import AgentRunContext, create_read_tools
from .storage import load_settings


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


@dataclass
class BackendOutcome:
    response: CoachResponse
    source_ids: list[str]
    metadata: dict[str, Any]


class AgentBackend(Protocol):
    async def run(self, *, prompt: str, context: AgentRunContext, history: list[dict[str, Any]]) -> BackendOutcome: ...

    async def close(self) -> None: ...


class AgentsSDKBackend:
    def __init__(self, provider: DeepSeekModelProvider) -> None:
        self.provider = provider

    async def run(self, *, prompt: str, context: AgentRunContext, history: list[dict[str, Any]]) -> BackendOutcome:
        from agents import Agent, RunConfig, Runner

        agent = Agent(
            name="Interview Coach",
            instructions=(
                "你是 Table Miku 的 Java 后端面试学习教练，也是唯一对话出口。"
                "知识检索、复习状态和写入必须通过提供的本地工具完成；禁止声称访问原始 Vault、文件系统、Shell 或网络搜索。"
                "练习时先让用户独立作答，提交前不得展示参考答案；反馈不能替代用户的掌握度自评。"
                "引用资料时保留工具返回的 source_id。若资源未授权，说明需要用户在 Agent 中心开启对应开关。"
            ),
            model=self.provider.model(),
            tools=create_read_tools(),
        )
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
            raise RuntimeError("只读阶段出现了意外审批请求，本次运行已停止。")
        response = CoachResponse(
            body=str(result.final_output or "").strip() or "DeepSeek 已返回，但没有可展示的文本。",
            sources=list(context.sources.values()),
        )
        return BackendOutcome(
            response=response,
            source_ids=list(context.sources),
            metadata={"provider": "deepseek", "model": self.provider.config.model, "mode": "single-agent"},
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

        self.store.add_message(
            session_id,
            "assistant",
            outcome.response.body,
            run_id=run_id,
            source_ids=outcome.source_ids,
        )
        self.store.finish_run(run_id, "completed", metadata=outcome.metadata)
        return outcome.response

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
        del operation_id
        return False

    def reject(self, operation_id: str) -> bool:
        del operation_id
        return False

    def cancel(self) -> bool:
        if self._future is None or self._future.done():
            return False
        self._thread.loop.call_soon_threadsafe(self.core.cancel)
        self._future.cancel()
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
