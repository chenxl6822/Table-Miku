from __future__ import annotations

from datetime import datetime, timezone

from table_miku.knowledge_assistant_approval_inbox import (
    can_use_approval_inbox,
    format_expiry_cell,
    is_inbox_task,
    select_inbox_tasks,
)


def _task(
    task_id: str,
    *,
    status: str = "awaiting_approval",
    requested_by: str = "editor-a",
    expires_at: str = "2026-08-15T12:00:00Z",
) -> dict:
    return {
        "id": task_id,
        "status": status,
        "requested_by": requested_by,
        "approval": {"status": "pending", "expires_at": expires_at},
    }


def test_inbox_excludes_own_requests_and_non_pending_statuses():
    own = _task("t-own", requested_by="approver-b")
    other = _task("t-other")
    done = _task("t-done", status="succeeded")
    assert is_inbox_task(other, "approver-b")
    assert not is_inbox_task(own, "approver-b")
    assert not is_inbox_task(done, "approver-b")
    selected = select_inbox_tasks([own, other, done], "approver-b")
    assert [item["id"] for item in selected] == ["t-other"]


def test_inbox_sorts_by_expiry_ascending():
    later = _task("t-later", expires_at="2026-08-15T12:10:00Z")
    sooner = _task("t-sooner", expires_at="2026-08-15T12:00:00Z")
    selected = select_inbox_tasks([later, sooner], "approver-b")
    assert [item["id"] for item in selected] == ["t-sooner", "t-later"]


def test_expiry_cell_marks_past_deadlines():
    now = datetime(2026, 8, 15, 12, 5, tzinfo=timezone.utc)
    expired = _task("t-exp", expires_at="2026-08-15T12:00:00Z")
    open_task = _task("t-open", expires_at="2026-08-15T12:10:00Z")
    assert format_expiry_cell(expired, now=now) == "已过期 2026-08-15T12:00:00Z"
    assert format_expiry_cell(open_task, now=now) == "2026-08-15T12:10:00Z"
    assert "C:\\" not in format_expiry_cell(expired, now=now)


def test_inbox_requires_approve_permission():
    assert can_use_approval_inbox(frozenset({"task:approve", "task:read"}))
    assert not can_use_approval_inbox(frozenset({"task:read", "task:create"}))
