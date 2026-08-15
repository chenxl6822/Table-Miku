# 2026-08-15 Knowledge Assistant 2.5 Approval Inbox Design

## Goal

Let an Approver (or Admin) see a “待我审批” queue of other people’s `awaiting_approval` tasks on the existing task tab, sorted by expiry. Do not change approval crypto, tools, or HTTP contracts.

## In scope

1. Qt-free inbox selector: `awaiting_approval` and `requested_by != current user`.
2. Task-tab filter combo visible only when the applied identity has `task:approve`.
3. Add an expiry column; inbox rows sort by `approval.expires_at` ascending; past expiry shows `已过期` plus the timestamp.
4. Viewer/Editor without approve do not see the inbox filter. A user’s own pending writes never appear in their inbox.

## Out of scope

- Desktop/OS notifications
- New `GET /v1/tasks/inbox` or schema/migration
- Auto-approve, auto-reject, or changing preview_hash / self-approval rules
- Real external tools, remote idempotency, compensation
- Changing the 10-minute approval window

## Non-negotiables

- Server remains the authority for preview/approve/reject; expired approve still fails with the existing `approval expired` conflict.
- Empty collection allowlist is still deny-all; collection-scoped identities keep existing list 403/skip behavior.
- Ordinary task list responses still must not leak staged bodies, tokens, or preview hashes.
- Escape still must not approve.

## Acceptance

- Unit tests: inbox excludes own requests and non-pending statuses; sort by expiry; expired label; no approve permission helper is false.
- UI tests: Approver filter shows only others’ awaiting tasks; Editor/Viewer hide the filter; Admin inbox excludes self-created tasks; expiry column is plain text.
