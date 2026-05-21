from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import user_data_dir


DEFAULT_SETTINGS: dict[str, Any] = {
    "city": "雨湖区,湘潭,湖南",
    "reminders_enabled": True,
    "reminder_interval_minutes": 60,
    "scheduled_reminders": [
        {"time": "08:30", "task": "语言基础 30 分钟：复习一个语法点，写 3 个小例子。"},
        {"time": "10:30", "task": "算法 45 分钟：刷 1 道题，记录思路、复杂度和错因。"},
        {"time": "14:30", "task": "项目 60 分钟：推进一个功能或修一个 bug，提交一次 commit。"},
        {"time": "17:30", "task": "简历/面试 20 分钟：整理一个项目亮点或复盘一道面试题。"},
        {"time": "20:30", "task": "复盘 15 分钟：写下今天完成了什么、明天先做什么。"},
    ],
    "fired_reminders": {},
    "quiet_hours": {"start": 23, "end": 7},
    "last_reminder_at": None,
    "bubble_seconds": 7,
    "system_monitor": {
        "enabled": True,
        "check_interval_seconds": 30,
        "cpu_enabled": True,
        "cpu_warning_percent": 85,
        "cpu_warning_checks": 3,
        "memory_enabled": True,
        "memory_warning_percent": 88,
        "memory_warning_checks": 2,
        "network_enabled": True,
        "network_check_interval_minutes": 2,
        "network_timeout_seconds": 4,
        "network_healthy_report_minutes": 30,
        "network_targets": [
            {"name": "百度", "url": "https://www.baidu.com/"},
            {"name": "Google", "url": "https://www.google.com/generate_204"},
        ],
    },
    "assistant": {
        "enabled": True,
        "daily_brief_time": "08:20",
        "weather_report_time": "08:10",
        "startup_brief": True,
        "command_max_output_chars": 420,
        "ai_agent_enabled": False,
        "ai_use_direct_api": True,
        "ai_provider": "openai",
        "ai_model": "gpt-5-nano",
        "deepseek_model": "deepseek-v4-flash",
        "deepseek_base_url": "https://api.deepseek.com",
    },
    "course_reminders": {
        "enabled": True,
        "lead_minutes": 10,
    },
    "course_time_season": "default",
    "course_time_slots": [
        {"season": "default", "section": "1-2节", "start": "08:00", "end": "09:40"},
        {"season": "default", "section": "3-4节", "start": "10:00", "end": "11:40"},
        {"season": "default", "section": "5-6节", "start": "14:00", "end": "15:40"},
        {"season": "default", "section": "7-8节", "start": "16:00", "end": "17:40"},
        {"season": "default", "section": "9-10节", "start": "19:00", "end": "20:40"},
        {"season": "default", "section": "9-11节", "start": "19:00", "end": "21:25"},
        {"season": "default", "section": "1-4节", "start": "08:00", "end": "11:40"},
    ],
    "pomodoro": {
        "enabled": True,
        "running": False,
        "mode": "work",
        "work_minutes": 25,
        "break_minutes": 5,
        "cycles_completed": 0,
        "started_at": None,
    },
    "startup": {
        "enabled": False,
    },
    "knowledge": {
        "enabled": True,
        "topics": ["计算机网络", "计算机组成原理", "数据结构", "操作系统", "编译原理", "数据库原理"],
    },
}

DEFAULT_GOALS: list[dict[str, Any]] = [
    {
        "title": "大二学生准备进入公司实习",
        "description": "系统学习编程基础、算法、项目实践、数据库、Git、简历和面试，为软件开发实习做准备。",
        "daily_minutes": 120,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target_date": (datetime.now() + timedelta(days=120)).date().isoformat(),
        "plan": [],
    }
]


def _path(filename: str) -> Path:
    return user_data_dir() / filename


def read_json(filename: str, default: Any) -> Any:
    path = _path(filename)
    if not path.exists():
        write_json(filename, default)
        return deepcopy(default)

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(path.suffix + ".broken")
        try:
            path.replace(backup)
        except OSError:
            pass
        write_json(filename, default)
        return deepcopy(default)


def write_json(filename: str, payload: Any) -> None:
    path = _path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_settings() -> dict[str, Any]:
    settings = read_json("settings.json", DEFAULT_SETTINGS)
    merged = deepcopy(DEFAULT_SETTINGS)
    return _deep_merge(merged, settings)


def _deep_merge(default: dict[str, Any], override: Any) -> dict[str, Any]:
    if not isinstance(override, dict):
        return default
    for key, value in override.items():
        if isinstance(default.get(key), dict) and isinstance(value, dict):
            default[key] = _deep_merge(deepcopy(default[key]), value)
        else:
            default[key] = value
    return default


def save_settings(settings: dict[str, Any]) -> None:
    write_json("settings.json", settings)


def load_goals() -> list[dict[str, Any]]:
    goals = read_json("goals.json", DEFAULT_GOALS)
    if not isinstance(goals, list):
        return deepcopy(DEFAULT_GOALS)
    return goals


def save_goals(goals: list[dict[str, Any]]) -> None:
    write_json("goals.json", goals)
