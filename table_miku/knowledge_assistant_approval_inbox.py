from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

INBOX_STATUS = "awaiting_approval"


def can_use_approval_inbox(permissions: frozenset[str] | set[str]) -> bool:
    return "task:approve" in permissions


def is_inbox_task(task: Mapping[str, Any], user_id: str) -> bool:
    if str(task.get("status") or "") != INBOX_STATUS:
        return False
    return str(task.get("requested_by") or "") != str(user_id)


def approval_expires_at(task: Mapping[str, Any]) -> str:
    approval = task.get("approval") if isinstance(task.get("approval"), dict) else {}
    return str(approval.get("expires_at") or "")


def format_expiry_cell(task: Mapping[str, Any], *, now: datetime | None = None) -> str:
    expires = approval_expires_at(task)
    if not expires:
        return "—"
    if _is_expired(expires, now=now):
        return f"已过期 {expires}"
    return expires


def select_inbox_tasks(
    tasks: Sequence[Mapping[str, Any]],
    user_id: str,
) -> list[Mapping[str, Any]]:
    matched = [task for task in tasks if is_inbox_task(task, user_id)]
    return sorted(
        matched,
        key=lambda task: (approval_expires_at(task), str(task.get("id") or "")),
    )


def _is_expired(expires_at: str, *, now: datetime | None = None) -> bool:
    parsed = _parse_expires_at(expires_at)
    if parsed is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return parsed <= current.astimezone(timezone.utc)


def _parse_expires_at(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
