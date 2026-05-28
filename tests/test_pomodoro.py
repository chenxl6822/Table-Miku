"""Tests for pomodoro — pure logic, no Qt dependency."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku.pomodoro import start_pomodoro, stop_pomodoro, pomodoro_tick, pomodoro_status


def make_settings(**overrides) -> dict:
    from table_miku.storage import deepcopy, DEFAULT_SETTINGS
    s = deepcopy(DEFAULT_SETTINGS)
    s.update(overrides)
    return s


class TestPomodoro:
    def test_start(self):
        settings = make_settings()
        start_pomodoro(settings)
        pomo = settings["pomodoro"]
        assert pomo["running"] is True
        assert pomo["mode"] == "work"
        assert pomo["started_at"] is not None

    def test_stop(self):
        settings = make_settings()
        start_pomodoro(settings)
        stop_pomodoro(settings)
        pomo = settings["pomodoro"]
        assert pomo["running"] is False

    def test_tick_noop_when_not_running(self):
        settings = make_settings()
        assert pomodoro_tick(settings) == ""

    def test_tick_work_complete_triggers_break(self):
        settings = make_settings()
        start_pomodoro(settings)
        settings["pomodoro"]["work_minutes"] = 0
        result = pomodoro_tick(settings, datetime.now())
        assert "休息" in result
        assert settings["pomodoro"]["mode"] == "break"

    def test_tick_break_complete_triggers_work(self):
        settings = make_settings()
        start_pomodoro(settings)
        settings["pomodoro"]["work_minutes"] = 0
        pomodoro_tick(settings, datetime.now())
        settings["pomodoro"]["break_minutes"] = 0
        result = pomodoro_tick(settings, datetime.now())
        assert "专注" in result
        assert settings["pomodoro"]["mode"] == "work"

    def test_cycles_counted(self):
        settings = make_settings()
        start_pomodoro(settings)
        settings["pomodoro"]["work_minutes"] = 0
        pomodoro_tick(settings, datetime.now())
        assert settings["pomodoro"]["cycles_completed"] == 1

    def test_status_not_running(self):
        settings = make_settings()
        status = pomodoro_status(settings)
        assert "未运行" in status

    def test_status_running(self):
        settings = make_settings()
        start_pomodoro(settings)
        status = pomodoro_status(settings)
        assert "专注" in status or "运行中" in status
