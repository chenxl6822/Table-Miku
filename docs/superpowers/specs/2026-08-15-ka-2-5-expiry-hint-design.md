# 2026-08-15 Knowledge Assistant 2.5 Expiry Hint Design

## Goal

Show Approver/Admin when inbox items are already expired or will expire within two minutes, without OS notifications or changing approval crypto.

## In scope

1. `format_expiry_cell` prefixes `即将到期` when remaining time is ≤ 120 seconds.
2. Plain-text `taskExpiryHint` counts inbox tasks: `待我审批 N 个：已过期 x，即将到期 y。`
3. Hint is visible only with `task:approve`. Own pending writes are excluded (same inbox rule).

## Out of scope

- System tray / toast notifications
- Auto-reject or auto-refresh polling
- Changing the 10-minute server window, preview_hash, or approve/reject contracts

## Non-negotiables

- Server still rejects expired approve/preview.
- No staged bodies, tokens, or preview hashes in the hint.
- Identity switch clears the hint.
