# 2.5 Close Work Item Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Approved `close_work_item` marks a local work-item ledger row closed without HTTP.

**Architecture:** Mirror `archive_document`: snapshot target metadata, revalidate on preview/execute, soft-close the row. Schema v4 adds close columns via `ALTER TABLE` so v3 rows survive.

**Tech Stack:** Python 3.12, PySide6, SQLite, pytest.

## Global Constraints

- No outbound HTTP or reopen.
- Empty collection allowlist is deny-all.
- No summary in list/receipt/notice/trace.
- Do not merge #19 or this PR unless asked.

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

### Task 1: Schema v4 + close tool

- [ ] Failing tests: schema bump, approve close, replay, conflict, deny-all, tenant hide, metadata mismatch.
- [ ] SCHEMA_VERSION 4; ALTER missing close columns; `close_work_item` execute.

### Task 2: UI + docs

- [ ] Failing UI tests: allowlist, labels, prefill, untrusted pane empty.
- [ ] Dialog, short button, desktop helper, docs §13 item 6.
- [ ] Ruff + targeted pytest; commit; PR; no merge.
