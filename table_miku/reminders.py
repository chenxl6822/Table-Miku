from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .assistant_data import load_timetable
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
        today_courses = self._today_course_lines()
        if today_courses:
            next_tip += "\n今日课程：" + "；".join(today_courses[:3])
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

        course_message = self._course_message(settings, now)
        if course_message:
            save_settings(settings)
            self.reminder.emit(course_message)
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
    def _course_message(settings: dict[str, Any], now: datetime) -> str:
        course_settings = settings.get("course_reminders") or {}
        if not course_settings.get("enabled", True):
            return ""

        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        lead_minutes = int(course_settings.get("lead_minutes", 10))
        current_time = now.strftime("%H:%M")
        today_key = now.date().isoformat()
        fired = settings.setdefault("fired_course_reminders", {})
        today_fired = set(fired.get(today_key, []))
        slots = ReminderManager._course_slot_map(settings)

        for entry in load_timetable():
            if str(entry.get("weekday", "")) != weekday:
                continue
            start = str(entry.get("start") or "").strip()
            end = str(entry.get("end") or "").strip()
            section = str(entry.get("section") or "").strip()
            if not start and section:
                slot = slots.get(section)
                if slot:
                    start = slot.get("start", "")
                    end = slot.get("end", "")
            if not start:
                continue
            try:
                class_start = datetime.combine(now.date(), time.fromisoformat(start))
            except ValueError:
                continue
            remind_at = (class_start - timedelta(minutes=lead_minutes)).strftime("%H:%M")
            marker = f"{weekday}|{section}|{start}|{entry.get('course')}"
            if remind_at == current_time and marker not in today_fired:
                today_fired.add(marker)
                fired[today_key] = sorted(today_fired)
                for old_key in list(fired.keys()):
                    if old_key != today_key:
                        fired.pop(old_key, None)
                when = f"{start}-{end}" if end else start
                return f"课程提醒：{lead_minutes} 分钟后上课。\n{weekday} {when} {entry.get('course')}"
        return ""

    @staticmethod
    def _course_slot_map(settings: dict[str, Any]) -> dict[str, dict[str, str]]:
        season = str(settings.get("course_time_season", "default"))
        slots: dict[str, dict[str, str]] = {}
        fallback: dict[str, dict[str, str]] = {}
        for item in settings.get("course_time_slots") or []:
            if not isinstance(item, dict):
                continue
            section = str(item.get("section", "")).strip()
            if not section:
                continue
            normalized = {"start": str(item.get("start", "")), "end": str(item.get("end", ""))}
            if item.get("season", "default") == season:
                slots[section] = normalized
            elif item.get("season", "default") == "default":
                fallback[section] = normalized
        fallback.update(slots)
        return fallback

    @staticmethod
    def _today_course_lines() -> list[str]:
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.now().weekday()]
        lines: list[str] = []
        for entry in load_timetable():
            if entry.get("weekday") != weekday:
                continue
            when = str(entry.get("section") or f"{entry.get('start')}-{entry.get('end')}").strip("-")
            lines.append(f"{when} {entry.get('course')}")
        return lines

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
