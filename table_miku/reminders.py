from __future__ import annotations

from datetime import datetime, time
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .pomodoro import pomodoro_tick
from .planner import today_tasks
from .storage import load_goals, load_settings, save_settings


class ReminderManager(QObject):
    reminder = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.setInterval(15_000)
        self.timer.timeout.connect(self._tick)

    def start(self) -> None:
        self.timer.start()
        QTimer.singleShot(2_000, self._first_tip)

    def _first_tip(self) -> None:
        tasks = today_tasks(load_goals())
        settings = load_settings()
        scheduled = settings.get("scheduled_reminders") or []
        next_tip = ""
        if scheduled:
            first = scheduled[0]
            next_tip = f"\n下一次定时提醒：{first.get('time')} {first.get('task')}"
        if tasks:
            self.reminder.emit("今天的学习雷达启动：\n" + "\n".join(tasks[:2]) + next_tip)

    def _tick(self) -> None:
        settings = load_settings()
        if not settings.get("reminders_enabled", True):
            return
        if self._in_quiet_hours(settings):
            return

        now = datetime.now()
        pomodoro_message = pomodoro_tick(settings, now)
        if pomodoro_message:
            save_settings(settings)
            self.reminder.emit(pomodoro_message)
            return

        scheduled_message = self._scheduled_message(settings, now)
        if scheduled_message:
            save_settings(settings)
            self.reminder.emit(scheduled_message)
            return

        interval = int(settings.get("reminder_interval_minutes", 60))
        last_raw = settings.get("last_reminder_at")
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
                if (now - last).total_seconds() < interval * 60:
                    return
            except ValueError:
                pass

        tasks = today_tasks(load_goals())
        if not tasks:
            return

        settings["last_reminder_at"] = now.isoformat(timespec="seconds")
        save_settings(settings)
        self.reminder.emit("该学习啦：\n" + tasks[0])

    @staticmethod
    def _scheduled_message(settings: dict[str, Any], now: datetime) -> str:
        current_time = now.strftime("%H:%M")
        today_key = now.date().isoformat()
        fired = settings.setdefault("fired_reminders", {})
        today_fired = set(fired.get(today_key, []))

        for item in settings.get("scheduled_reminders") or []:
            reminder_time = str(item.get("time", "")).strip()
            task = str(item.get("task", "")).strip()
            marker = f"{reminder_time}|{task}"
            if reminder_time == current_time and task and marker not in today_fired:
                today_fired.add(marker)
                fired[today_key] = sorted(today_fired)
                for old_key in list(fired.keys()):
                    if old_key != today_key:
                        fired.pop(old_key, None)
                return f"{reminder_time} 到啦：\n{task}"
        return ""

    @staticmethod
    def _in_quiet_hours(settings: dict[str, Any]) -> bool:
        quiet = settings.get("quiet_hours") or {}
        start_hour = int(quiet.get("start", 23))
        end_hour = int(quiet.get("end", 7))
        current = datetime.now().time()
        start = time(hour=start_hour)
        end = time(hour=end_hour)
        if start_hour < end_hour:
            return start <= current < end
        return current >= start or current < end
