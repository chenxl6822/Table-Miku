"""Tests for goal_parser — zero external dependencies, pure string parsing."""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku.goal_parser import parse_goal_input, TIME_RE


class TestTimeRegex:
    def test_standard_time(self):
        m = TIME_RE.match("08:30 复习基础")
        assert m is not None
        assert m.group("hour") == "08"
        assert m.group("minute") == "30"
        assert "复习" in m.group("task")

    def test_chinese_colon_time(self):
        m = TIME_RE.match("14：30 刷算法题")
        assert m is not None
        assert m.group("hour") == "14"
        assert m.group("minute") == "30"

    def test_list_prefix_time(self):
        m = TIME_RE.match("- 20:30 复盘")
        assert m is not None
        assert m.group("task").strip() == "复盘"

    def test_numbered_prefix_time(self):
        from table_miku.goal_parser import _clean_line
        cleaned = _clean_line("1. 09:00 写代码")
        m = TIME_RE.match(cleaned)
        assert m is not None
        assert "写代码" in m.group("task")

    def test_invalid_time(self):
        assert TIME_RE.match("25:00 学习") is None
        assert TIME_RE.match("08:60 学习") is None


class TestParseGoalInput:
    def test_empty_input(self):
        result = parse_goal_input("")
        assert len(result.goals) == 0
        assert len(result.reminders) == 0

    def test_only_reminders(self):
        text = "08:30 复习\n14:30 刷题"
        result = parse_goal_input(text)
        assert len(result.reminders) == 2
        assert result.reminders[0]["time"] == "08:30"
        assert result.reminders[0]["task"] == "复习"

    def test_goal_and_reminders(self):
        text = """目标：准备实习
每天 90 分钟
08:30 复习基础
14:30 刷算法题"""
        result = parse_goal_input(text)
        assert len(result.goals) == 1
        assert result.goals[0]["title"] == "准备实习"
        assert result.goals[0]["daily_minutes"] == 90
        assert len(result.reminders) == 2

    def test_json_input(self):
        text = '{"goals": [{"title": "学Python", "daily_minutes": 60}], "schedule": [{"time": "08:30", "task": "学习"}]}'
        result = parse_goal_input(text)
        assert len(result.goals) == 1
        assert result.goals[0]["title"] == "学Python"
        assert len(result.reminders) == 1

    def test_json_array_input(self):
        text = '[{"title": "目标1", "daily_minutes": 30}, {"time": "09:00", "task": "任务A"}]'
        result = parse_goal_input(text)
        assert len(result.goals) == 1
        assert len(result.reminders) == 1

    def test_daily_minutes_detection(self):
        text = "目标：学习\n每天120分钟\n08:00 学习"
        result = parse_goal_input(text)
        assert result.goals[0]["daily_minutes"] == 120
