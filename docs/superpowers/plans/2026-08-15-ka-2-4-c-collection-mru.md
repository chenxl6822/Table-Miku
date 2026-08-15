# 2.4-C Collection MRU Implementation Plan

> **For agentic workers:** Use executing-plans. This slice is implemented in `codex/ka-2-4-c-collection-mru`.

**Goal:** Identity-scoped collection MRU combo on ingest dialogs.

**Architecture:** Qt-free `CollectionMruStore` plus `CollectionIdEdit` wrapping `QComboBox` with the old `text`/`setText`/`setReadOnly` API. Remember only after successful upload or batch queue submit.

**Tech Stack:** Python 3.12, PySide6, pytest.

## Global Constraints

- Server remains permission authority; empty allowlist is deny-all.
- MRU keyed by tenant+user; max 8 IDs; no paths/bodies/tokens.
- Tests use `TABLE_MIKU_DATA_DIR`.
