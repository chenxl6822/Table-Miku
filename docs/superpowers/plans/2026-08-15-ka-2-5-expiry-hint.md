# 2.5 Expiry Hint Implementation Plan

> **For agentic workers:** Use executing-plans. This slice is implemented in `codex/ka-2-5-expiry-hint`.

**Goal:** Mark inbox tasks that are expired or expiring within two minutes.

**Architecture:** Extend the Qt-free inbox helper; add a plain-text hint label on the task tab. No API/schema change.

**Tech Stack:** Python 3.12, PySide6, pytest.

## Global Constraints

- `EXPIRING_SOON_SECONDS = 120`
- Hint visible only with `task:approve`
- No notifications, no auto-reject, no HTTP contract change
