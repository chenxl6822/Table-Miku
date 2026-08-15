# 2.4-C Directory Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand dropped or chosen folders into a bounded file list and reuse the existing cancellable SHA-256 precheck.

**Architecture:** A Qt-free expander returns either unique supported files or a fail-closed error. `BatchUploadDialog` calls it from drop/choose-folder/choose-files, then `_start_precheck` as today.

**Tech Stack:** Python 3.12, PySide6, pytest, existing `FilePrecheckController`.

## Global Constraints

- `MAX_BATCH_FILES` remains 20; do not raise quotas.
- Max directory visits 200; max depth 3 from each selected directory.
- Do not follow symlinks/junctions; skip names starting with `.`.
- No outbox/HTTP before user confirm; no GUI-thread SHA-256.
- Isolated tests: `QT_QPA_PLATFORM=offscreen`, unique `TABLE_MIKU_DATA_DIR` and `--basetemp`.
- Do not commit `.env`, tokens, coverage, or pytest cache.

---

### Task 1: Pure expander

**Files:**
- Create: `table_miku/knowledge_assistant_batch_paths.py`
- Create: `tests/test_knowledge_assistant_batch_paths.py`

**Interfaces:**
- Produces: `MAX_DIRECTORY_VISITS = 200`, `MAX_DIRECTORY_DEPTH = 3`
- Produces: `BatchPathExpansion(files: tuple[Path, ...], skipped_unsupported: tuple[str, ...], error: str | None)`
- Produces: `expand_batch_upload_paths(raw_paths: list[Path], *, suffixes: frozenset[str], max_files: int, max_visits: int = 200, max_depth: int = 3) -> BatchPathExpansion`
- Error codes: `too_many_files`, `too_many_visits`, `directory_too_deep`, `unreadable`

- [ ] **Step 1: Write failing unit tests** for unique files, nested dirs, hidden/symlink skip, 21 files fail closed, visit cap, depth cap.
- [ ] **Step 2: Run tests and confirm they fail** because the module is missing.
- [ ] **Step 3: Implement the expander** with the rules in the spec.
- [ ] **Step 4: Run unit tests and confirm they pass.**

### Task 2: Wire BatchUploadDialog

**Files:**
- Modify: `table_miku/knowledge_assistant_ui.py`
- Modify: `tests/test_knowledge_assistant_ui.py`
- Modify: `docs/KNOWLEDGE_ASSISTANT_2.md`

**Interfaces:**
- Consumes: `expand_batch_upload_paths` and `BatchPathExpansion`
- Replace `_ingest_path_strings` directory rejection with expander + existing `_start_precheck`
- Add “选择文件夹…” calling `QFileDialog.getExistingDirectory`

- [ ] **Step 1: Rewrite `test_batch_upload_drop_rejects_directory_without_outbox`** into a drop-directory-enters-precheck test; add over-quota and choose-folder tests.
- [ ] **Step 2: Run UI tests and confirm the old rejection assertion fails.**
- [ ] **Step 3: Wire expander, folder button, intro copy, fail-closed warnings.**
- [ ] **Step 4: Run `tests/test_knowledge_assistant_ui.py` and `tests/test_knowledge_assistant_batch_paths.py` plus ruff on touched files.**
