from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import user_data_dir


DEFAULT_SETTINGS: dict[str, Any] = {
    "city": "Shanghai",
    "reminders_enabled": True,
    "reminder_interval_minutes": 60,
    "quiet_hours": {"start": 23, "end": 7},
    "last_reminder_at": None,
    "bubble_seconds": 7,
}

DEFAULT_GOALS: list[dict[str, Any]] = [
    {
        "title": "大二学生准备进入公司实习",
        "description": "系统学习编程基础、项目实践、简历和面试，为软件开发实习做准备。",
        "daily_minutes": 90,
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
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_settings() -> dict[str, Any]:
    settings = read_json("settings.json", DEFAULT_SETTINGS)
    merged = deepcopy(DEFAULT_SETTINGS)
    merged.update(settings)
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    write_json("settings.json", settings)


def load_goals() -> list[dict[str, Any]]:
    goals = read_json("goals.json", DEFAULT_GOALS)
    if not isinstance(goals, list):
        return deepcopy(DEFAULT_GOALS)
    return goals


def save_goals(goals: list[dict[str, Any]]) -> None:
    write_json("goals.json", goals)
