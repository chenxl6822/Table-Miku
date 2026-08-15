# 2026-08-15 Knowledge Assistant 2.4-C Batch Failure Summary Design

## Goal

After a confirmed batch submit, show this last batch’s live counts and failed filenames on the ingestion tab. Confirmation, outbox, and HTTP contracts stay unchanged.

## In scope

1. Qt-free `summarize_ingestion_batch` that classifies last-batch items by status and formats a plain-text line.
2. `KnowledgeAssistantDialog` remembers the last submitted `local_id`s, copies `local_id` onto later `job:*` snapshots, and updates a `QLabel` on the ingestion tab.
3. Identity switch / close clears the batch tracker. No auto-retry.

## Out of scope

- Modal failure dialog
- Auto-retry or new outbox writes
- Changing ingest HTTP/outbox contracts
- Global “all jobs” badge (the existing filter already covers that)

## Non-negotiables

- Filenames are `Path.name` only; no full paths, tokens, or idempotency keys.
- Failed list is capped (5 lines, truncated error text).
- Server remains the job-status authority; this is a local view of the last submit.
- Empty collection allowlist remains deny-all; this slice does not change RBAC.

## Status buckets

- succeeded: `succeeded`
- failed: `failed`, `cancel_rejected`, `reconciliation_required`, `unavailable`
- unknown: `outcome_unknown`, `pending`, `tracking`
- cancelled: `cancelled`, `abandoned`
- otherwise, plus unmatched submitted ids: active / in progress

## Acceptance

- Unit tests: path-stripped filenames, unmatched ids count as in-progress, unknown/cancelled labels, empty batch is blank.
- UI tests: after submit, “本批 1 个” with in-progress; failed update shows basename + error; job snapshot still counts as the same batch; identity change clears the label.
