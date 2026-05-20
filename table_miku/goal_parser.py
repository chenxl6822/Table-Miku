from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


TIME_RE = re.compile(
    r"^\s*(?:[-*]\s*)?"
    r"(?P<hour>[01]?\d|2[0-3])[:：](?P<minute>[0-5]\d)"
    r"(?:\s*(?:-|~|至|到)\s*(?:[01]?\d|2[0-3])[:：][0-5]\d)?"
    r"[\s,，:：-]*"
    r"(?P<task>.+?)\s*$"
)


@dataclass
class ParsedGoalInput:
    goals: list[dict[str, Any]] = field(default_factory=list)
    reminders: list[dict[str, str]] = field(default_factory=list)


def parse_goal_input(raw: str) -> ParsedGoalInput:
    text = raw.strip()
    if not text:
        return ParsedGoalInput()

    parsed_json = _parse_json(text)
    if parsed_json is not None:
        return parsed_json

    reminders: list[dict[str, str]] = []
    goal_lines: list[str] = []
    daily_minutes = 60

    for line in text.splitlines():
        cleaned = _clean_line(line)
        if not cleaned:
            continue

        time_match = TIME_RE.match(cleaned)
        if time_match:
            reminders.append(
                {
                    "time": _normalize_time(time_match.group("hour"), time_match.group("minute")),
                    "task": time_match.group("task").strip(),
                }
            )
            continue

        minute_match = re.search(r"(?:每天|每日|daily)?\s*(\d{2,3})\s*(?:分钟|min)", cleaned, re.I)
        if minute_match:
            daily_minutes = int(minute_match.group(1))
            continue

        goal_match = re.match(r"^(?:目标|学习目标|goal)\s*[:：]\s*(.+)$", cleaned, re.I)
        goal_lines.append(goal_match.group(1).strip() if goal_match else cleaned)

    goals: list[dict[str, Any]] = []
    if goal_lines:
        title = goal_lines[0]
        description = "\n".join(goal_lines[1:])
        goals.append(
            {
                "title": title,
                "description": description,
                "daily_minutes": daily_minutes,
            }
        )

    return ParsedGoalInput(goals=goals, reminders=reminders)


def _parse_json(text: str) -> ParsedGoalInput | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    result = ParsedGoalInput()
    if isinstance(payload, list):
        for item in payload:
            _collect_json_item(item, result)
    elif isinstance(payload, dict):
        for item in payload.get("goals", []):
            _collect_goal(item, result)
        for item in payload.get("schedule", payload.get("reminders", [])):
            _collect_reminder(item, result)
        if not result.goals and any(key in payload for key in ("title", "goal", "task")):
            _collect_goal(payload, result)
    return result


def _collect_json_item(item: Any, result: ParsedGoalInput) -> None:
    if isinstance(item, dict) and "time" in item:
        _collect_reminder(item, result)
    else:
        _collect_goal(item, result)


def _collect_goal(item: Any, result: ParsedGoalInput) -> None:
    if isinstance(item, str):
        result.goals.append({"title": item, "description": "", "daily_minutes": 60})
        return
    if not isinstance(item, dict):
        return
    title = str(item.get("title") or item.get("goal") or item.get("task") or "").strip()
    if not title:
        return
    result.goals.append(
        {
            "title": title,
            "description": str(item.get("description", "")).strip(),
            "daily_minutes": int(item.get("daily_minutes", item.get("minutes", 60))),
        }
    )


def _collect_reminder(item: Any, result: ParsedGoalInput) -> None:
    if not isinstance(item, dict):
        return
    raw_time = str(item.get("time", "")).strip()
    task = str(item.get("task") or item.get("title") or item.get("content") or "").strip()
    match = re.match(r"^([01]?\d|2[0-3])[:：]([0-5]\d)$", raw_time)
    if match and task:
        result.reminders.append({"time": _normalize_time(match.group(1), match.group(2)), "task": task})


def _normalize_time(hour: str, minute: str) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def _clean_line(line: str) -> str:
    return re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
