from __future__ import annotations

from datetime import datetime, timezone

from table_miku.knowledge_assistant_approval_inbox import (
    APPROVAL_TRAY_TITLE,
    ApprovalTrayGate,
    can_use_approval_inbox,
    format_approval_notice,
    format_approval_tray_message,
    format_expiry_cell,
    format_inbox_expiry_hint,
    format_tasks_tab_title,
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


def test_expiry_cell_marks_deadlines_within_two_minutes():
    now = datetime(2026, 8, 15, 12, 9, tzinfo=timezone.utc)
    soon = _task("t-soon", expires_at="2026-08-15T12:10:00Z")
    assert format_expiry_cell(soon, now=now) == "即将到期 2026-08-15T12:10:00Z"


def test_inbox_expiry_hint_counts_expired_and_soon():
    now = datetime(2026, 8, 15, 12, 9, tzinfo=timezone.utc)
    tasks = [
        _task("t-own", requested_by="approver-b", expires_at="2026-08-15T12:00:00Z"),
        _task("t-exp", expires_at="2026-08-15T12:00:00Z"),
        _task("t-soon", expires_at="2026-08-15T12:10:00Z"),
        _task("t-ok", expires_at="2026-08-15T12:20:00Z"),
        _task("t-done", status="succeeded", expires_at="2026-08-15T12:00:00Z"),
    ]
    text = format_inbox_expiry_hint(tasks, "approver-b", now=now)
    assert text == "待我审批 3 个：已过期 1，即将到期 1。"
    assert format_inbox_expiry_hint([], "approver-b", now=now) == ""


def test_approval_notice_uses_counts_only_and_excludes_own_tasks():
    now = datetime(2026, 8, 15, 12, 9, tzinfo=timezone.utc)
    secret = _task("t-exp", expires_at="2026-08-15T12:00:00Z")
    secret["filename"] = "secret.md"
    tasks = [
        _task("t-own", requested_by="approver-b", expires_at="2026-08-15T12:00:00Z"),
        secret,
        _task("t-ok", expires_at="2026-08-15T12:20:00Z"),
    ]
    text = format_approval_notice(tasks, "approver-b", now=now)
    assert text.startswith("有待你审批的任务。")
    assert "待我审批 2 个" in text
    assert "已过期 1" in text
    assert "打开收件箱" in text
    assert "secret.md" not in text
    assert "t-exp" not in text
    assert format_approval_notice([], "approver-b", now=now) == ""
    assert format_tasks_tab_title(0) == "任务与审批"
    assert format_tasks_tab_title(2) == "任务与审批（2）"


def test_inbox_requires_approve_permission():
    assert can_use_approval_inbox(frozenset({"task:approve", "task:read"}))
    assert not can_use_approval_inbox(frozenset({"task:read", "task:create"}))


def test_tray_message_reuses_count_hint_and_omits_secrets():
    now = datetime(2026, 8, 15, 12, 9, tzinfo=timezone.utc)
    secret = _task("t-exp", expires_at="2026-08-15T12:00:00Z")
    secret["filename"] = "secret.md"
    secret["summary"] = "token-abc"
    tasks = [
        _task("t-own", requested_by="approver-b", expires_at="2026-08-15T12:00:00Z"),
        secret,
        _task("t-ok", expires_at="2026-08-15T12:20:00Z"),
    ]
    text = format_approval_tray_message(tasks, "approver-b", now=now)
    assert text == "待我审批 2 个：已过期 1。"
    assert text == format_inbox_expiry_hint(tasks, "approver-b", now=now)
    assert "secret.md" not in text
    assert "token-abc" not in text
    assert "t-exp" not in text
    assert format_approval_tray_message([], "approver-b", now=now) == ""
    assert APPROVAL_TRAY_TITLE == "企业知识助手"


def test_tray_gate_notifies_only_on_rising_edge_per_identity():
    gate = ApprovalTrayGate()
    first = gate.observe(
        identity_key="tenant-a|approver-b",
        can_approve=True,
        count=2,
        message="待我审批 2 个。",
    )
    assert first is not None
    assert first.title == APPROVAL_TRAY_TITLE
    assert first.message == "待我审批 2 个。"
    assert first.count == 2
    assert (
        gate.observe(
            identity_key="tenant-a|approver-b",
            can_approve=True,
            count=2,
            message="待我审批 2 个。",
        )
        is None
    )
    assert (
        gate.observe(
            identity_key="tenant-a|approver-b",
            can_approve=True,
            count=1,
            message="待我审批 1 个。",
        )
        is None
    )
    second = gate.observe(
        identity_key="tenant-a|approver-b",
        can_approve=True,
        count=3,
        message="待我审批 3 个。",
    )
    assert second is not None
    assert second.count == 3
    assert (
        gate.observe(
            identity_key="tenant-a|editor-a",
            can_approve=False,
            count=3,
            message="待我审批 3 个。",
        )
        is None
    )
    switched = gate.observe(
        identity_key="tenant-a|approver-c",
        can_approve=True,
        count=1,
        message="待我审批 1 个。",
    )
    assert switched is not None
    assert switched.count == 1
