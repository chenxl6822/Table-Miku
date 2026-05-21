from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .assistant_data import assistant_context
from .agent_adapter import agents_sdk_status, run_personal_agent
from .assistant_log import append_event, recent_events
from .command_runner import WatchedCommand, parse_command_spec
from .planner import today_tasks
from .storage import load_goals, load_settings, save_settings
from .system_monitor import SystemSnapshot
from .weather import get_weather


class PersonalAssistant(QObject):
    notice = Signal(str, str)

    def __init__(self, system_snapshot_provider, default_cwd: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._system_snapshot_provider = system_snapshot_provider
        self._default_cwd = default_cwd
        self._commands: list[WatchedCommand] = []

        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._timer.start()
        settings = load_settings()
        assistant = settings.get("assistant") or {}
        if assistant.get("enabled", True) and assistant.get("startup_brief", True):
            QTimer.singleShot(7_000, self.brief_now)

    def brief_now(self) -> None:
        self._run_thread(self._brief_worker)

    def weather_now(self) -> None:
        self._run_thread(self._weather_worker)

    def run_watched_command(self, raw: str) -> bool:
        settings = load_settings()
        spec = parse_command_spec(raw, self._default_cwd)
        if spec is None:
            self.notice.emit("surprised", "没有识别到命令。第一行可写 cwd=路径，后面写要运行的命令。")
            return False
        max_output = int((settings.get("assistant") or {}).get("command_max_output_chars", 420))
        command = WatchedCommand(spec, max_output, self)
        command.finished_notice.connect(self._command_finished)
        self._commands.append(command)
        command.start()
        append_event("command", "开始监视命令", spec.command, {"cwd": str(spec.cwd)})
        self.notice.emit("focus", f"我开始盯着这个命令：{_short(spec.command)}。跑完会叫你。")
        return True

    def ai_plan_now(self) -> None:
        settings = load_settings()
        assistant = settings.get("assistant") or {}
        if not assistant.get("ai_agent_enabled", False):
            status = agents_sdk_status()
            self.notice.emit("focus", f"AI Agent 预留好了，但还没启用：{status}")
            return
        self._run_thread(self._agent_worker)

    def _tick(self) -> None:
        settings = load_settings()
        assistant = settings.get("assistant") or {}
        if not assistant.get("enabled", True):
            return

        now_key = datetime.now().strftime("%H:%M")
        today_key = datetime.now().date().isoformat()
        fired = settings.setdefault("assistant_fired", {})
        today_fired = set(fired.get(today_key, []))

        if now_key == str(assistant.get("weather_report_time", "08:10")) and "weather" not in today_fired:
            today_fired.add("weather")
            fired[today_key] = sorted(today_fired)
            self._trim_fired_days(fired, today_key)
            save_settings(settings)
            self.weather_now()

        if now_key == str(assistant.get("daily_brief_time", "08:20")) and "brief" not in today_fired:
            today_fired.add("brief")
            fired[today_key] = sorted(today_fired)
            self._trim_fired_days(fired, today_key)
            save_settings(settings)
            self.brief_now()

    def _brief_worker(self) -> None:
        settings = load_settings()
        snapshot = self._system_snapshot_provider()
        tasks = today_tasks(load_goals())
        parts = ["今日助手简报："]
        if tasks:
            first_task = " ".join(tasks[0].splitlines())
            parts.append(first_task)
        system_line = format_snapshot(snapshot)
        if system_line:
            parts.append(system_line)
        recent = recent_events(2)
        if recent:
            parts.append("最近：" + "；".join(str(item.get("title", "")) for item in recent if item.get("title")))
        message = "\n".join(part for part in parts if part)
        append_event("brief", "生成今日简报", message)
        self.notice.emit("smile", message)

        if (settings.get("assistant") or {}).get("ai_agent_enabled", False):
            self._agent_worker()

    def _weather_worker(self) -> None:
        settings = load_settings()
        city = settings.get("city", "auto")
        try:
            message = get_weather(city)
            append_event("weather", "天气汇报", message)
            self.notice.emit("smile", message)
        except Exception:
            self.notice.emit("surprised", "天气汇报失败了。可能是网络、VPN 或天气服务暂时不可用。")

    def _agent_worker(self) -> None:
        settings = load_settings()
        assistant = settings.get("assistant") or {}
        context = self._agent_context()
        provider = str(assistant.get("ai_provider", "openai"))
        model_key = "deepseek_model" if provider == "deepseek" else "ai_model"
        result = run_personal_agent(
            context,
            "请给我下一步工作提醒和异常摘要。",
            str(assistant.get(model_key, "deepseek-v4-flash" if provider == "deepseek" else "gpt-5-nano")),
            provider,
            str(assistant.get("deepseek_base_url", "https://api.deepseek.com")),
        )
        append_event("ai_agent", "AI Agent 汇报" if result.ok else "AI Agent 未启用", result.text, result.metadata)
        self.notice.emit("smile" if result.ok else "focus", result.text)

    def _agent_context(self) -> str:
        tasks = today_tasks(load_goals())
        snapshot = format_snapshot(self._system_snapshot_provider())
        events = recent_events(6)
        return "\n".join(
            [
                "今日任务：" + (" | ".join(" ".join(task.splitlines()) for task in tasks[:2]) or "暂无"),
                "系统状态：" + (snapshot or "暂无"),
                "最近事件：" + (" | ".join(str(event.get("title", "")) for event in events) or "暂无"),
                assistant_context(),
            ]
        )

    def _command_finished(self, expression: str, message: str) -> None:
        append_event("command", "命令完成", message)
        self.notice.emit(expression, message)
        self._commands = [command for command in self._commands if command.process.state() != QProcess.ProcessState.NotRunning]

    @staticmethod
    def _trim_fired_days(fired: dict[str, Any], today_key: str) -> None:
        for key in list(fired.keys()):
            if key != today_key:
                fired.pop(key, None)

    @staticmethod
    def _run_thread(target) -> None:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()


def format_snapshot(snapshot: SystemSnapshot | None) -> str:
    if snapshot is None:
        return ""
    parts: list[str] = []
    if snapshot.cpu_percent is not None:
        parts.append(f"CPU {snapshot.cpu_percent:.0f}%")
    if snapshot.memory_percent is not None:
        memory = f"内存 {snapshot.memory_percent:.0f}%"
        if snapshot.memory_available_mb is not None:
            memory += f"，可用 {snapshot.memory_available_mb} MB"
        parts.append(memory)
    if snapshot.network:
        ok = [item.name for item in snapshot.network if item.ok]
        failed = [item.name for item in snapshot.network if not item.ok]
        if failed:
            parts.append("网络异常：" + "、".join(failed))
        elif ok:
            parts.append("网络正常：" + "、".join(ok))
    return "电脑状态：" + "；".join(parts) if parts else ""


def _short(text: str, limit: int = 30) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."
