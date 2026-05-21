from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .paths import user_data_dir


LOG_FILE = "assistant_events.jsonl"


def append_event(kind: str, title: str, detail: str = "", payload: dict[str, Any] | None = None) -> None:
    path = user_data_dir() / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "title": title,
        "detail": detail,
        "payload": payload or {},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def recent_events(limit: int = 8) -> list[dict[str, Any]]:
    path = user_data_dir() / LOG_FILE
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    for line in lines[-max(limit, 1) :]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events
