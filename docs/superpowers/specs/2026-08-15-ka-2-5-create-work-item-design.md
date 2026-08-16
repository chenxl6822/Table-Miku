# 2026-08-15 Knowledge Assistant 2.5 Create Work Item Design

## Goal

Add the first real write tool besides ingest/archive: `create_work_item`. After independent approval it inserts a tenant-scoped row into a local SQLite work-item ledger. The ledger is a stand-in for an external ticket system. No outbound HTTP.

## In scope

1. Schema version 3 and `work_items` table with `UNIQUE(tenant_id, remote_idempotency_key)`.
2. Agent tool `create_work_item` (`knowledge:write`, `awaiting_approval`).
3. Staged untrusted `summary`; list/get/receipt expose `summary_sha256` + `byte_size` only.
4. Exact Action Preview: summary in the untrusted pane; consequences name the local ledger.
5. Remote idempotency on execute: same tenant+key+request hash returns the original item; same key different request is 409/`ConflictError`.
6. Task-tab dialog + desktop helper. Keep the existing ingest dialog.

## Out of scope

- Outbound HTTP, webhooks, or allowlisted URLs
- Compensation / undo
- OS toasts
- Public work-item list API
- Changing ingest/archive contracts

## Arguments

| Field | Rule |
|---|---|
| `title` | 1–120 characters after strip; no NUL/CR/LF |
| `summary` | staged UTF-8 payload; 1–2000 characters |
| `collection_id` | `DocumentService._collection_id`; empty allowlist is deny-all |
| `remote_idempotency_key` | 8–128, `[A-Za-z0-9._:-]+`; distinct from HTTP `Idempotency-Key` |

Stored task arguments: `title`, `collection_id`, `remote_idempotency_key`, `summary_sha256`, `byte_size`.

## Execute result (no summary)

`id`, `title`, `collection_id`, `status` (`open`), `remote_idempotency_key`, `idempotent_replay`.

Ledger row may store the approved summary. Task/list/receipt/notice/trace must not.

## Non-negotiables

- No self-approval.
- Preview hash remains HMAC-bound to approver, action, and request hash.
- Do not put remote/HTTP keys or summary bodies in Trace attributes.
- Old schema/data must survive `initialize()`.
