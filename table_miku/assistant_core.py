from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .assistant_data import (
    assistant_context,
    format_application_summary,
    format_interview_summary,
    load_application_records,
    load_interview_reviews,
    load_timetable,
)
from .agent_adapter import agents_sdk_status, run_personal_agent
from .assistant_log import append_event, recent_events
from .command_runner import WatchedCommand, parse_command_spec
from .knowledge_base import knowledge_context
from .pomodoro import pomodoro_status
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
            provider = assistant.get("ai_provider", "deepseek")
            if provider == "deepseek" and "DeepSeek API ready" in status:
                self.notice.emit("focus", "AI 助理未开启：DeepSeek API 已就绪，右键开启 AI 助理即可使用。")
            elif provider != "deepseek" and "OpenAI" in status:
                self.notice.emit("focus", f"AI 助理未开启：{status}，右键开启 AI 助理即可使用。")
            else:
                self.notice.emit("focus", f"AI 助理未开启：{status}")
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
        courses = _today_courses()
        apps = format_application_summary(load_application_records(), 3)
        interviews = format_interview_summary(load_interview_reviews(), 3)
        knowledge = knowledge_context(2)

        full_parts: list[str] = []
        if tasks:
            full_parts.append("今日任务：\n" + "\n".join(" ".join(t.splitlines()) for t in tasks[:3]))
        if courses:
            full_parts.append("今日课程：" + "；".join(courses))
        full_parts.append(pomodoro_status(settings))
        if apps:
            full_parts.append(apps)
        if interviews:
            full_parts.append(interviews)
        system_line = format_snapshot(snapshot)
        if system_line:
            full_parts.append(system_line)
        if knowledge:
            full_parts.append(knowledge)
        full_report = "\n\n".join(full_parts)
        append_event("brief", "生成今日简报", full_report)

        bubble_lines: list[str] = []
        if tasks:
            first = " ".join(tasks[0].splitlines())
            bubble_lines.append(_short(first, 72))
        if courses:
            bubble_lines.append("课程：" + "、".join(c.split()[-1] if c.split() else c for c in courses[:2]))
        pomo = pomodoro_status(settings)
        if pomo and "未启动" not in pomo:
            bubble_lines.append(_short(pomo, 48))
        if apps:
            app_line = _short(apps.split("\n")[-1] if "\n" in apps else apps, 48)
            if app_line:
                bubble_lines.append(app_line)
        if not bubble_lines:
            bubble_lines.append("今天还没有学习目标，右键导入一个吧。")
        bubble = "今日简报：\n" + "\n".join(bubble_lines[:5])
        self.notice.emit("smile", bubble)

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
        provider = str(assistant.get("ai_provider", "deepseek"))
        model_key = "deepseek_model" if provider == "deepseek" else "ai_model"
        default_model = "deepseek-v4-flash" if provider == "deepseek" else "gpt-5-nano"
        result = run_personal_agent(
            context,
            "请给我下一步工作提醒和异常摘要。",
            str(assistant.get(model_key, default_model)),
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
                knowledge_context(4),
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


def _today_courses() -> list[str]:
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.now().weekday()]
    lines: list[str] = []
    for entry in load_timetable():
        if entry.get("weekday") != weekday:
            continue
        when = str(entry.get("section") or f"{entry.get('start')}-{entry.get('end')}").strip("-")
        lines.append(f"{when} {entry.get('course')}")
    return lines
