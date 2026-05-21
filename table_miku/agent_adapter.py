from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .paths import PROJECT_ROOT


@dataclass(frozen=True)
class AgentResult:
    ok: bool
    text: str
    metadata: dict[str, Any] | None = None


def agents_sdk_status() -> str:
    if not _api_key():
        return "缺少 OPENAI_API_KEY。"
    try:
        import agents  # noqa: F401
    except ImportError:
        return "OpenAI API 可用；openai-agents 未安装，将使用 Responses API。"
    return "Agents SDK ready"


def run_personal_agent(context: str, request: str, model: str = "gpt-5-nano") -> AgentResult:
    if not _api_key():
        return AgentResult(False, "AI 助理还没启用：需要配置 OPENAI_API_KEY。")

    try:
        from agents import Agent, Runner
    except ImportError:
        return _run_responses_api(context, request, model)

    instructions = (
        "你是 Table Miku 的个人助理内核。"
        "你需要根据用户目标、电脑状态、最近事件、课程表、投递记录和面试复盘，给出简洁、可执行、温和但直接的提醒。"
        "输出控制在 120 个中文字符以内，不要编造不存在的系统状态。"
    )
    agent = Agent(name="Table Miku Assistant", instructions=instructions, model=model)
    result = asyncio.run(Runner.run(agent, f"上下文：\n{context}\n\n用户请求：{request}"))
    text = str(getattr(result, "final_output", "") or "").strip()
    return AgentResult(True, text or "AI 助理已运行，但没有生成可展示内容。", {"provider": "agents-sdk", "model": model})


def _run_responses_api(context: str, request: str, model: str) -> AgentResult:
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "你是 Table Miku 的个人助理内核。根据目标、课程表、投递记录、面试复盘、电脑状态和最近事件，"
                            "给出下一步提醒。语气像桌面 Miku：简短、具体、温和但不拖泥带水。"
                            "输出不超过 120 个中文字符，不编造没有出现在上下文里的事实。"
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": f"上下文：\n{context}\n\n请求：{request}"}],
            },
        ],
        "max_output_tokens": 260,
    }
    request_obj = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "User-Agent": "Table-Miku/0.4",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return AgentResult(False, f"AI API 调用失败：HTTP {exc.code}。{_short_error(detail)}", {"provider": "responses-api", "model": model})
    except OSError as exc:
        return AgentResult(False, f"AI API 暂时连不上：{exc}", {"provider": "responses-api", "model": model})

    text = _extract_response_text(data)
    metadata = {
        "provider": "responses-api",
        "model": model,
        "response_id": data.get("id"),
        "usage": data.get("usage"),
    }
    return AgentResult(True, text or "AI API 已返回，但没有可展示文本。", metadata)


def _extract_response_text(data: dict[str, Any]) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n".join(parts).strip()


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    for filename in (".env.local", ".env"):
        path = PROJECT_ROOT / filename
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("OPENAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _short_error(text: str, limit: int = 96) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."
