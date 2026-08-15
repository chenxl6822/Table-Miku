# 2026-08-15 Knowledge Assistant 2.4-C Directory Import Design

## Goal

Let Editors drop or choose a folder in `BatchUploadDialog`, expand it into a bounded file list, and reuse the 2.4-A cancellable SHA-256 precheck. Do not create outbox or HTTP traffic before confirm.

## In scope

1. Pure path expander with fail-closed caps: max 20 supported files (`MAX_BATCH_FILES`), max 200 visited entries, max depth 3 from each selected directory.
2. Skip hidden names (`.*`), skip symlinks/junctions, skip unsupported suffixes; do not follow links out of the dropped tree.
3. Directory drop and a “选择文件夹…” button both feed the expander, then the existing precheck path.
4. Mixed file+directory drops expand directories then union unique files.
5. Update intro copy and replace the current “不支持目录” rejection.

## Out of scope (later slices)

- Collection selector / MRU
- Duplicate-content warnings
- Batch failure summary beyond current per-file precheck exclude
- Raising `MAX_BATCH_FILES`, OCR, malware scan, or sandboxing
- Recursive import of an entire drive / uncapped network shares

## Non-negotiables

- `MAX_BATCH_FILES` stays 20; exceeding it fails closed (no partial join).
- Visit or depth overflow fails closed (no silent truncation).
- GUI thread must not SHA-256; hashing stays on `FilePrecheckController`.
- Expansion only lists metadata (`iterdir` / `is_file` / `is_dir` / `is_symlink`); it does not read file bytes.
- Confirm-before-send and ingest-worker final hash re-verification stay unchanged.
- Tenant/collection/RBAC unchanged; this is still user-initiated Editor write.

## Expansion rules

- Selected/dropped directory is depth 0; its children are depth 1; refuse if a directory is encountered at depth greater than 3.
- Each filesystem entry visited (file or directory, including skipped hidden/unsupported) increments the visit counter; refuse after 200.
- Unique files are keyed by resolved path casefold, same as current file selection.
- Unreadable path (`resolve`/`iterdir` OSError) fails the whole batch.

## Acceptance

- Unit tests cover expander caps, symlink skip, hidden skip, mixed files+dirs, and fail-closed over-quota.
- UI tests: directory drop and choose-folder enter precheck; over-quota directory does not enable submit or create snapshots.
- Existing file-drop and confirm/cancel precheck tests still pass.
