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
    deepseek_key = _env_value("DEEPSEEK_API_KEY")
    openai_key = _api_key()
    if deepseek_key:
        return "DeepSeek API ready"
    if openai_key:
        try:
            import agents  # noqa: F401
        except ImportError:
            return "OpenAI API 可用；openai-agents 未安装，将使用 Responses API。"
        return "OpenAI Agents SDK ready"
    return "缺少 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。请在 .env.local 或环境变量中配置。"


def run_personal_agent(
    context: str,
    request: str,
    model: str = "deepseek-v4-flash",
    provider: str = "deepseek",
    base_url: str = "",
) -> AgentResult:
    provider = provider.lower().strip()
    if provider == "deepseek":
        return _run_chat_completions_api(
            context,
            request,
            model or "deepseek-v4-flash",
            base_url or "https://api.deepseek.com",
            "DEEPSEEK_API_KEY",
            "deepseek",
        )

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


def _run_chat_completions_api(
    context: str,
    request: str,
    model: str,
    base_url: str,
    api_key_env: str,
    provider_name: str,
) -> AgentResult:
    api_key = _env_value(api_key_env)
    if not api_key:
        return AgentResult(False, f"AI 助理还没启用：需要配置 {api_key_env}。", {"provider": provider_name, "model": model})

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Table Miku 的个人助理内核。根据目标、课程表、投递记录、面试复盘、电脑状态和最近事件，"
                    "给出下一步提醒。语气像桌面 Miku：简短、具体、温和但不拖泥带水。"
                    "输出不超过 120 个中文字符，不编造没有出现在上下文里的事实。"
                ),
            },
            {"role": "user", "content": f"上下文：\n{context}\n\n请求：{request}"},
        ],
        "temperature": 0.4,
        "max_tokens": 260,
    }
    endpoint = base_url.rstrip("/") + "/chat/completions"
    request_obj = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Table-Miku/0.5",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return AgentResult(False, _api_failure_message(provider_name, exc.code, detail), {"provider": provider_name, "model": model})
    except OSError as exc:
        return AgentResult(False, f"{provider_name} API 暂时连不上：请检查联网权限、代理或防火墙。{exc}", {"provider": provider_name, "model": model})

    text = ""
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        if isinstance(message, dict):
            text = str(message.get("content", "")).strip()
    metadata = {
        "provider": provider_name,
        "model": model,
        "response_id": data.get("id"),
        "usage": data.get("usage"),
    }
    return AgentResult(True, text or "AI API 已返回，但没有可展示文本。", metadata)


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
        return AgentResult(False, _api_failure_message("OpenAI", exc.code, detail), {"provider": "responses-api", "model": model})
    except OSError as exc:
        return AgentResult(False, f"OpenAI API 暂时连不上：请检查联网权限、代理或防火墙。{exc}", {"provider": "responses-api", "model": model})

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
    return _env_value("OPENAI_API_KEY")


def _env_value(name: str) -> str:
    key = os.environ.get(name, "").strip()
    if key:
        return key
    for filename in (".env.local", ".env"):
        path = PROJECT_ROOT / filename
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                cleaned = line.strip().lstrip("\ufeff")
                if cleaned.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _short_error(text: str, limit: int = 96) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _api_failure_message(provider_name: str, status_code: int, detail: str) -> str:
    compact = _short_error(detail, 140)
    lowered = compact.lower()
    if status_code == 401 or "invalid_api_key" in lowered or "unauthorized" in lowered:
        return f"{provider_name} API 认证失败：请检查 API key 是否正确、是否已启用。HTTP {status_code}。{compact}"
    if status_code == 429:
        if "quota" in lowered or "credit" in lowered or "balance" in lowered:
            return f"{provider_name} API 额度不足：请检查余额、额度或消费上限。HTTP {status_code}。{compact}"
        return f"{provider_name} API 触发速率限制：请降低请求频率或稍后重试。HTTP {status_code}。{compact}"
    if status_code == 400 and ("model" in lowered or "not found" in lowered):
        return f"{provider_name} API 模型不可用：请检查模型名和账号权限。HTTP {status_code}。{compact}"
    if status_code == 403:
        return f"{provider_name} API 权限不足：请检查账号、项目或模型权限。HTTP {status_code}。{compact}"
    return f"{provider_name} API 调用失败：HTTP {status_code}。{compact}"
