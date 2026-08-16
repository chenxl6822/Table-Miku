# 2.5 Create Work Item Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Add approved `create_work_item` writes to a local tenant-scoped work-item ledger with remote idempotency and a task-tab dialog.

**Architecture:** Reuse ingest_text staging (payload + sha256), collection scope, and Action Preview. Persist `work_items` in SQLite. Remote uniqueness is `(tenant_id, remote_idempotency_key)` and is distinct from HTTP `Idempotency-Key`.

**Tech Stack:** Python 3.12, PySide6, SQLite, pytest.

## Global Constraints

- No outbound HTTP or compensation.
- Empty collection allowlist is deny-all.
- List/receipt/notice/trace never include summary or tokens.
- SCHEMA_VERSION becomes 3; old documents survive.
- Ordinary merge later; this slice does not deploy.

## File map

- Modify: `table_miku/knowledge_assistant/database.py`
- Modify: `table_miku/knowledge_assistant/tasks.py`
- Modify: `table_miku/knowledge_assistant_desktop.py`
- Modify: `table_miku/knowledge_assistant_ui.py`
- Modify: `docs/KNOWLEDGE_ASSISTANT_2.md`
- Test: `tests/test_knowledge_assistant_tasks.py`
- Test: `tests/test_knowledge_assistant_ingestion.py`
- Test: `tests/test_knowledge_assistant_api.py`
- Test: `tests/test_knowledge_assistant_ui.py`

---

### Task 1: Schema v3 + create_work_item tool

**Files:** database.py, tasks.py, tests above

- [x] Write failing tests for schema bump, create/preview/approve, remote replay/conflict, deny-all, tenant isolation, no summary leak.
- [x] Run tests; expect unknown tool / SCHEMA_VERSION == 2.
- [x] Add `work_items` and SCHEMA_VERSION 3.
- [x] Register `create_work_item`; stage summary; execute insert with remote idempotency.
- [x] Run targeted pytest until green.

### Task 2: Desktop UI + docs

**Files:** knowledge_assistant_ui.py, knowledge_assistant_desktop.py, KNOWLEDGE_ASSISTANT_2.md, UI tests

- [x] Write failing UI tests for dialog, labels, allowlists, untrusted summary pane.
- [x] Add `WorkItemTaskDialog`, button, allowlists, `create_work_item_task`.
- [x] Update docs §13 item 6.
- [x] Ruff + targeted pytest.
- [ ] Commit exact paths. Push/PR only after tests; do not merge.
