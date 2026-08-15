# 2.5 Approval Inbox Implementation Plan

> **For agentic workers:** Use executing-plans. This slice is implemented in `codex/ka-2-5-approval-inbox`.

**Goal:** Show a “待我审批” queue of other people’s awaiting tasks on the existing task tab.

**Architecture:** Qt-free inbox selector plus a visible-only filter combo. Reuse `GET /v1/tasks`. Copy `local` list into `_render_task_items` so filter changes do not refetch.

**Tech Stack:** Python 3.12, PySide6, pytest.

## Global Constraints

- No API/schema change; no notifications; no new write tools.
- Inbox = `awaiting_approval` and `requested_by != current user`.
- Filter visible only with `task:approve`. Self-approval remains forbidden.
- Filenames/bodies/tokens/preview hashes stay out of the table.

## Tasks

1. Helper `select_inbox_tasks` / `format_expiry_cell` with unit tests.
2. Task tab combo + expiry column + `_render_task_items`.
3. Docs: mark 2.5 first slice in `docs/KNOWLEDGE_ASSISTANT_2.md`.
