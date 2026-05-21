from __future__ import annotations

from datetime import datetime
from typing import Any


def start_pomodoro(settings: dict[str, Any]) -> dict[str, Any]:
    pomodoro = settings.setdefault("pomodoro", {})
    pomodoro["enabled"] = True
    pomodoro["running"] = True
    pomodoro["mode"] = "work"
    pomodoro["started_at"] = datetime.now().isoformat(timespec="seconds")
    pomodoro.setdefault("cycles_completed", 0)
    return pomodoro


def stop_pomodoro(settings: dict[str, Any]) -> dict[str, Any]:
    pomodoro = settings.setdefault("pomodoro", {})
    pomodoro["running"] = False
    pomodoro["started_at"] = None
    return pomodoro


def pomodoro_tick(settings: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now()
    pomodoro = settings.setdefault("pomodoro", {})
    if not pomodoro.get("enabled", True) or not pomodoro.get("running", False):
        return ""

    started_raw = pomodoro.get("started_at")
    try:
        started_at = datetime.fromisoformat(str(started_raw))
    except (TypeError, ValueError):
        pomodoro["started_at"] = now.isoformat(timespec="seconds")
        return ""

    mode = str(pomodoro.get("mode", "work"))
    work_minutes = int(pomodoro.get("work_minutes", 25))
    break_minutes = int(pomodoro.get("break_minutes", 5))
    target_minutes = work_minutes if mode == "work" else break_minutes
    if (now - started_at).total_seconds() < target_minutes * 60:
        return ""

    if mode == "work":
        pomodoro["mode"] = "break"
        pomodoro["started_at"] = now.isoformat(timespec="seconds")
        pomodoro["cycles_completed"] = int(pomodoro.get("cycles_completed", 0)) + 1
        return f"番茄钟完成：专注 {work_minutes} 分钟到点啦。站起来喝水，休息 {break_minutes} 分钟。"

    pomodoro["mode"] = "work"
    pomodoro["started_at"] = now.isoformat(timespec="seconds")
    return f"休息结束。下一轮 {work_minutes} 分钟专注开始，我陪你盯住最小任务。"


def pomodoro_status(settings: dict[str, Any]) -> str:
    pomodoro = settings.get("pomodoro") or {}
    if not pomodoro.get("running", False):
        return "番茄钟未运行。"
    mode = "专注" if pomodoro.get("mode", "work") == "work" else "休息"
    cycles = int(pomodoro.get("cycles_completed", 0))
    return f"番茄钟运行中：{mode}，已完成 {cycles} 轮。"
