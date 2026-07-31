from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT, runtime_path


DEFAULT_KNOWLEDGE_TOPICS = [
    "计算机网络",
    "计算机组成原理",
    "数据结构",
    "操作系统",
    "编译原理",
    "数据库原理",
    "软件工程",
    "算法设计与分析",
    "计算机安全",
    "分布式系统",
    "Java 后端基础",
    "Go 后端基础",
    "工程实践与架构",
]


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
        "memory_available_warning_mb": 1024,
        "memory_warning_checks": 2,
        "network_enabled": True,
        "network_check_interval_minutes": 2,
        "network_timeout_seconds": 4,
        "network_warning_checks": 2,
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
        "command_timeout_seconds": 600,
        "ai_agent_enabled": False,
        "ai_use_direct_api": True,
        "ai_provider": "deepseek",
        "ai_model": "deepseek-v4-flash",
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
    "weather_alerts": {
        "enabled": True,
        "interval_minutes": 20,
        "cooldown_minutes": 60,
        "lead_minutes": 30,
    },
    "startup": {
        "enabled": False,
    },
    "knowledge": {
        "enabled": True,
        "topics": DEFAULT_KNOWLEDGE_TOPICS,
        "trusted_sources": {
            "enabled": True,
            "obsidian_vault": "",
        },
    },
    "knowledge_review": {
        "enabled": True,
    },
    "fired_review_reminders": {},
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
    return runtime_path(filename)


def read_json(filename: str, default: Any) -> Any:
    path = _path(filename)
    # 确保父目录存在
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        write_json(filename, default)
        return deepcopy(default)

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as ex:
        # 损坏文件备份
        backup = path.with_suffix(path.suffix + ".broken")
        try:
            if backup.exists():
                backup = path.with_suffix(f".broken.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            path.replace(backup)
        except OSError:
            pass
        print(f"[storage] 读取 {filename} 失败 ({ex})，已重置为默认值。损坏文件：{backup.name}")
        write_json(filename, default)
        return deepcopy(default)


def write_json(filename: str, payload: Any) -> None:
    path = _path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再 rename，防止写入中断导致文件损坏
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as ex:
        print(f"[storage] 写入 {filename} 失败: {ex}")
        # 尝试直接写原路径（降级）
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 不再重试，至少应用还能运行


def load_settings() -> dict[str, Any]:
    settings = read_json("settings.json", DEFAULT_SETTINGS)
    merged = deepcopy(DEFAULT_SETTINGS)
    result = _deep_merge(merged, settings)
    _normalize_numeric_settings(result)
    _normalize_assistant_provider(result)
    _normalize_knowledge_settings(result)
    return result


def _deep_merge(default: dict[str, Any], override: Any) -> dict[str, Any]:
    if not isinstance(override, dict):
        return default
    for key, value in override.items():
        if isinstance(default.get(key), dict) and isinstance(value, dict):
            default[key] = _deep_merge(deepcopy(default[key]), value)
        else:
            default[key] = value
    return default


def _normalize_assistant_provider(settings: dict[str, Any]) -> None:
    """If user config still has openai but no OpenAI key exists and DeepSeek key does,
    auto-switch to DeepSeek at runtime without persisting the change."""
    assistant = settings.get("assistant")
    if not isinstance(assistant, dict):
        return
    if assistant.get("ai_provider") != "openai":
        return
    if _env_value("OPENAI_API_KEY"):
        return
    if not _env_value("DEEPSEEK_API_KEY"):
        return
    assistant["ai_provider"] = "deepseek"
    assistant.setdefault("deepseek_model", "deepseek-v4-flash")
    assistant.setdefault("deepseek_base_url", "https://api.deepseek.com")


def _normalize_numeric_settings(settings: dict[str, Any]) -> None:
    """Clamp user-editable numeric settings to safe runtime ranges."""
    settings["reminder_interval_minutes"] = _coerce_int(
        settings.get("reminder_interval_minutes"), 60, 1, 24 * 60
    )
    settings["bubble_seconds"] = _coerce_int(settings.get("bubble_seconds"), 7, 1, 60)

    numeric_sections: dict[str, dict[str, tuple[int, int, int]]] = {
        "quiet_hours": {
            "start": (23, 0, 23),
            "end": (7, 0, 23),
        },
        "system_monitor": {
            "check_interval_seconds": (30, 5, 3600),
            "cpu_warning_percent": (85, 1, 100),
            "cpu_warning_checks": (3, 1, 100),
            "memory_warning_percent": (88, 1, 100),
            "memory_available_warning_mb": (1024, 1, 1024 * 1024),
            "memory_warning_checks": (2, 1, 100),
            "network_check_interval_minutes": (2, 1, 24 * 60),
            "network_timeout_seconds": (4, 1, 120),
            "network_warning_checks": (2, 1, 100),
            "network_healthy_report_minutes": (30, 1, 7 * 24 * 60),
        },
        "assistant": {
            "command_max_output_chars": (420, 120, 10_000),
            "command_timeout_seconds": (600, 5, 24 * 60 * 60),
        },
        "course_reminders": {
            "lead_minutes": (10, 0, 24 * 60),
        },
        "pomodoro": {
            "work_minutes": (25, 1, 24 * 60),
            "break_minutes": (5, 1, 24 * 60),
            "cycles_completed": (0, 0, 1_000_000),
        },
        "weather_alerts": {
            "interval_minutes": (20, 1, 24 * 60),
            "cooldown_minutes": (60, 1, 7 * 24 * 60),
            "lead_minutes": (30, 0, 24 * 60),
        },
    }
    for section_name, fields in numeric_sections.items():
        section = settings.get(section_name)
        if not isinstance(section, dict):
            section = deepcopy(DEFAULT_SETTINGS[section_name])
            settings[section_name] = section
        for field, (default, minimum, maximum) in fields.items():
            section[field] = _coerce_int(section.get(field), default, minimum, maximum)


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(parsed, minimum), maximum)


def _normalize_knowledge_settings(settings: dict[str, Any]) -> None:
    knowledge = settings.setdefault("knowledge", {})
    if not isinstance(knowledge, dict):
        settings["knowledge"] = {"enabled": True, "topics": list(DEFAULT_KNOWLEDGE_TOPICS)}
        return
    existing = knowledge.get("topics")
    ordered: list[str] = []
    for topic in DEFAULT_KNOWLEDGE_TOPICS + (existing if isinstance(existing, list) else []):
        cleaned = str(topic).strip()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    knowledge["topics"] = ordered


def _env_value(name: str) -> str:
    key = os.environ.get(name, "").strip()
    if key:
        return key
    for filename in (".env.local", ".env"):
        path = PROJECT_ROOT / filename
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                cleaned = line.strip().lstrip("﻿")
                if cleaned.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def save_settings(settings: dict[str, Any]) -> None:
    write_json("settings.json", settings)


def load_goals() -> list[dict[str, Any]]:
    goals = read_json("goals.json", DEFAULT_GOALS)
    if not isinstance(goals, list):
        return deepcopy(DEFAULT_GOALS)
    return goals


def save_goals(goals: list[dict[str, Any]]) -> None:
    write_json("goals.json", goals)
