# 2026-08-15 Knowledge Assistant 2.4-C Collection MRU Design

## Goal

Replace hand-typed collection fields on ingest dialogs with an editable combo seeded by the current identity’s allowlist and a tenant/user-scoped local MRU, without treating the UI as the permission authority.

## In scope

1. Qt-free `CollectionMruStore` in `user_data_dir()/knowledge_assistant/collection_mru.json`.
2. Key records by `tenant_id` + `user_id`; keep at most 8 valid collection IDs per identity.
3. `CollectionIdEdit` (`QComboBox`) exposing `text` / `setText` / `setReadOnly` / `isReadOnly` so existing tests keep working.
4. Wire `BatchUploadDialog` and `UploadDocumentDialog`. Remember only after a successful upload or successful batch queue submit.
5. Restricted identities: list only allowlist (MRU order first). Deny-all (`collection_ids=frozenset()`): empty list, block accept. Unrestricted (`None`): MRU plus `default`, editable.

## Out of scope

- Duplicate-content warnings, batch failure summary
- Server collection-directory API
- Task-dialog collection field
- Cross-device sync, encryption of MRU (IDs only, not secrets)

## Non-negotiables

- Server remains the permission authority.
- Empty allowlist is deny-all; do not fold it into unrestricted.
- Do not write document bodies, paths, tokens, or idempotency keys into the MRU file.
- Tests use `TABLE_MIKU_DATA_DIR`; never real AppData/Vault.
- Corrupt MRU file fails open to empty suggestions, never crashes the dialog.

## Acceptance

- Unit tests: isolation by tenant/user, cap 8, deny-all vs unrestricted, corrupt file, skip collections outside allowlist.
- UI tests: combo object names unchanged; restricted options; deny-all cannot accept; successful batch submit records MRU for that identity only.
