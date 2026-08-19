from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

INBOX_STATUS = "awaiting_approval"
EXPIRING_SOON_SECONDS = 120
APPROVAL_TRAY_TITLE = "企业知识助手"


@dataclass(frozen=True)
class ApprovalTrayNotice:
    title: str
    message: str
    count: int


class ApprovalTrayGate:
    def __init__(self) -> None:
        self._identity_key = ""
        self._last_count = 0

    def observe(
        self,
        *,
        identity_key: str,
        can_approve: bool,
        count: int,
        message: str,
    ) -> ApprovalTrayNotice | None:
        if not can_approve:
            self._identity_key = ""
            self._last_count = 0
            return None
        if identity_key != self._identity_key:
            self._identity_key = identity_key
            self._last_count = 0
        if count <= self._last_count:
            self._last_count = count
            return None
        self._last_count = count
        if not message:
            return None
        return ApprovalTrayNotice(APPROVAL_TRAY_TITLE, message, count)


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
    urgency = expiry_urgency(task, now=now)
    if urgency == "expired":
        return f"已过期 {expires}"
    if urgency == "soon":
        return f"即将到期 {expires}"
    return expires


def format_inbox_expiry_hint(
    tasks: Sequence[Mapping[str, Any]],
    user_id: str,
    *,
    now: datetime | None = None,
) -> str:
    inbox = select_inbox_tasks(tasks, user_id)
    if not inbox:
        return ""
    expired = soon = 0
    for task in inbox:
        urgency = expiry_urgency(task, now=now)
        if urgency == "expired":
            expired += 1
        elif urgency == "soon":
            soon += 1
    text = f"待我审批 {len(inbox)} 个"
    details: list[str] = []
    if expired:
        details.append(f"已过期 {expired}")
    if soon:
        details.append(f"即将到期 {soon}")
    if details:
        text += "：" + "，".join(details)
    return text + "。"


def format_approval_notice(
    tasks: Sequence[Mapping[str, Any]],
    user_id: str,
    *,
    now: datetime | None = None,
) -> str:
    summary = format_inbox_expiry_hint(tasks, user_id, now=now)
    if not summary:
        return ""
    return f"有待你审批的任务。{summary}点击打开收件箱。"


def format_approval_tray_message(
    tasks: Sequence[Mapping[str, Any]],
    user_id: str,
    *,
    now: datetime | None = None,
) -> str:
    return format_inbox_expiry_hint(tasks, user_id, now=now)


def format_tasks_tab_title(count: int) -> str:
    if count <= 0:
        return "任务与审批"
    return f"任务与审批（{int(count)}）"


def expiry_urgency(task: Mapping[str, Any], *, now: datetime | None = None) -> str:
    expires = approval_expires_at(task)
    if not expires:
        return "none"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    parsed = _parse_expires_at(expires)
    if parsed is None:
        return "none"
    remaining = (parsed - current).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining <= EXPIRING_SOON_SECONDS:
        return "soon"
    return "ok"


def select_inbox_tasks(
    tasks: Sequence[Mapping[str, Any]],
    user_id: str,
) -> list[Mapping[str, Any]]:
    matched = [task for task in tasks if is_inbox_task(task, user_id)]
    return sorted(
        matched,
        key=lambda task: (approval_expires_at(task), str(task.get("id") or "")),
    )


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
