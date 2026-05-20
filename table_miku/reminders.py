from __future__ import annotations

from datetime import datetime, time
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .planner import today_tasks
from .storage import load_goals, load_settings, save_settings


class ReminderManager(QObject):
    reminder = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self._tick)

    def start(self) -> None:
        self.timer.start()
        QTimer.singleShot(2_000, self._first_tip)

    def _first_tip(self) -> None:
        tasks = today_tasks(load_goals())
        if tasks:
            self.reminder.emit("今天的学习雷达启动：\n" + "\n".join(tasks[:2]))

    def _tick(self) -> None:
        settings = load_settings()
        if not settings.get("reminders_enabled", True):
            return
        if self._in_quiet_hours(settings):
            return

        interval = int(settings.get("reminder_interval_minutes", 60))
        last_raw = settings.get("last_reminder_at")
        now = datetime.now()
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
