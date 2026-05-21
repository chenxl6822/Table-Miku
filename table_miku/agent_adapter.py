from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentResult:
    ok: bool
    text: str


def agents_sdk_status() -> str:
    if not os.environ.get("OPENAI_API_KEY"):
        return "缺少 OPENAI_API_KEY。"
    try:
        import agents  # noqa: F401
    except ImportError:
        return "缺少 openai-agents 依赖。"
    return "ready"


def run_personal_agent(context: str, request: str, model: str = "gpt-5-nano") -> AgentResult:
    if not os.environ.get("OPENAI_API_KEY"):
        return AgentResult(False, "AI Agent 还没启用：需要配置 OPENAI_API_KEY。")

    try:
        from agents import Agent, Runner
    except ImportError:
        return AgentResult(False, "AI Agent 还没启用：需要安装 openai-agents。")

    instructions = (
        "你是 Table Miku 的个人助手内核。"
        "你需要根据用户目标、电脑状态、最近事件和天气，给出简洁、可执行、温和但直接的提醒。"
        "输出控制在 120 个中文字符以内，不要编造不存在的系统状态。"
    )
    agent = Agent(name="Table Miku Assistant", instructions=instructions, model=model)
    result = asyncio.run(Runner.run(agent, f"上下文：\n{context}\n\n用户请求：{request}"))
    text = str(getattr(result, "final_output", "") or "").strip()
    return AgentResult(True, text or "AI Agent 已运行，但没有生成可展示内容。")
