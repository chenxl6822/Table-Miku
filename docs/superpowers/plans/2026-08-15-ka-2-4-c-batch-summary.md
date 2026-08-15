# 2.4-C Batch Failure Summary Implementation Plan

> **For agentic workers:** Use executing-plans. This slice is implemented in `codex/ka-2-4-c-batch-summary`.

**Goal:** Show the last submitted ingest batch’s live counts and failed filenames on the ingestion tab.

**Architecture:** Qt-free summarizer plus a plain-text `QLabel`. Track original `local_id`s; copy them onto `job:*` snapshot items so refresh still matches the same batch.

**Tech Stack:** Python 3.12, PySide6, pytest.

## Global Constraints

- No modal, no auto-retry, no outbox/HTTP contract change.
- Filenames only; no full paths, tokens, or idempotency keys.
- Identity switch clears the tracker.
