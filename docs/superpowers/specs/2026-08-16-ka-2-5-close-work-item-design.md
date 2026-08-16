# 2026-08-16 Knowledge Assistant 2.5 Close Work Item Design

## Goal

Add `close_work_item`: after independent approval, mark a tenant-scoped local ledger row `closed`. Soft compensation only. No outbound HTTP.

## In scope

1. Schema version 4. Existing `work_items` rows keep data; new close columns default `status='open'`.
2. Agent tool `close_work_item` (`knowledge:write`, `awaiting_approval`). Argument: `work_item_id`.
3. Validate/preview reload `title`, `collection_id`, `remote_idempotency_key`. Mismatch fails closed.
4. Execute: `open` → `closed` with `closed_by`, `close_task_id`, `close_request_hash`. Same hash replay; different hash conflict.
5. Cross-tenant `ResourceNotFound`. Empty collection allowlist is deny-all. No self-approval.
6. Task-tab dialog; prefill `result.id` from a selected succeeded `create_work_item` task.

## Out of scope

- Reopen/restore, row deletion, HTTP rollback, work-item list API, OS toasts, ingest/archive changes

## Stored arguments (no summary)

`work_item_id`, `title`, `collection_id`, `remote_idempotency_key`

## Result (no summary)

`id`, `title`, `collection_id`, `status` (`closed`), `remote_idempotency_key`, `idempotent_replay`

## Preview

- Intent `ensure_closed`
- Target: tenant, collection, work_item_id, title, remote_idempotency_key
- Empty untrusted body
- Consequences: local ledger status becomes closed; row is not deleted; no HTTP to a real ticket system
- Reversibility: `administrative_restore_required`

## Non-negotiables

- List/receipt/notice/trace never include summary or tokens
- Preview hash remains HMAC-bound
- Old schema/data survive `initialize()`
