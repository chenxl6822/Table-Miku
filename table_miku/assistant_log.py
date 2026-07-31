from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import runtime_path


LOG_FILE = "assistant_events.jsonl"
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 3
_log_lock = threading.Lock()
_secret_assignment = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|authorization)"
    r"[a-z0-9_-]*)([\"']?\s*[:=]\s*[\"']?)([^\s\"',;}]+)"
)
_bearer_token = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_provider_token = re.compile(r"\b(?:sk|sess)-[A-Za-z0-9_-]{8,}\b")
_url_credentials = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")


def append_event(kind: str, title: str, detail: str = "", payload: dict[str, Any] | None = None) -> None:
    path = runtime_path(LOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _redact_value({
        "time": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "title": title,
        "detail": detail,
        "payload": payload or {},
    })
    line = json.dumps(record, ensure_ascii=False) + "\n"
    encoded_size = len(line.encode("utf-8"))
    with _log_lock:
        _rotate_if_needed(path, encoded_size)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def recent_events(limit: int = 8) -> list[dict[str, Any]]:
    path = runtime_path(LOG_FILE)
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


def _redact_value(value: Any, key: str = "") -> Any:
    normalized_key = key.lower().replace("-", "_")
    if any(marker in normalized_key for marker in ("api_key", "token", "password", "authorization")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if not isinstance(value, str):
        return value
    redacted = _secret_assignment.sub(r"\1\2[REDACTED]", value)
    redacted = _bearer_token.sub("Bearer [REDACTED]", redacted)
    redacted = _provider_token.sub("[REDACTED]", redacted)
    return _url_credentials.sub(r"\1[REDACTED]@", redacted)


def _rotate_if_needed(path: Path, incoming_size: int) -> None:
    try:
        current_size = path.stat().st_size
    except OSError:
        current_size = 0
    if current_size + incoming_size <= LOG_MAX_BYTES:
        return

    for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            os.replace(source, target)
    if path.exists():
        os.replace(path, path.with_name(f"{path.name}.1"))
