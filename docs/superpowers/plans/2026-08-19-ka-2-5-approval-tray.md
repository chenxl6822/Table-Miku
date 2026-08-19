# 2.5 Approval Tray Implementation Plan

> **For agentic workers:** Use executing-plans. This slice is implemented in `codex/ka-2-5-approval-tray`.

**Goal:** Rising-edge Windows tray balloon for Approver/Admin inbox counts, without extra polling.

**Architecture:** Pure format + `ApprovalTrayGate` in `knowledge_assistant_approval_inbox.py`. Dialog emits `approval_tray_requested(title, message)` after the existing notice update. `TableMiku` shows `QSystemTrayIcon.showMessage` and opens the inbox on `messageClicked`.

**Tech Stack:** Python 3.12, PySide6, pytest.

## Global Constraints

- Counts only; no filenames/bodies/tokens/ids in the balloon.
- Visible only with `task:approve`.
- Rising edge per identity; no new polling; no second tray.
- No schema/API/HTTP tool changes.

## Files

- Modify: `table_miku/knowledge_assistant_approval_inbox.py`
- Modify: `table_miku/knowledge_assistant_ui.py`
- Modify: `table_miku/app.py`
- Modify: `docs/KNOWLEDGE_ASSISTANT_2.md`
- Test: `tests/test_knowledge_assistant_approval_inbox.py`
- Test: `tests/test_knowledge_assistant_ui.py`
- Test: `tests/test_knowledge_assistant_app_integration.py`

### Task 1: Format + rising-edge gate

- [ ] Failing tests for `format_approval_tray_message` and `ApprovalTrayGate`
- [ ] Minimal helpers
- [ ] Targeted pytest

### Task 2: Dialog emit + app tray

- [ ] Failing UI/app tests: rising-edge emit, no leak, editor skip, click opens inbox, no tray skip
- [ ] Signal + `showMessage` + `messageClicked`
- [ ] Docs §13 item 6
- [ ] Ruff + targeted pytest
