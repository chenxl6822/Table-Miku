# 2026-08-15 Knowledge Assistant 2.5 Approval Notice Design

## Goal

Tell an Approver/Admin that inbox work exists even when they are not on the task tab. Stay in-app; do not use OS toasts.

## In scope

1. Cross-tab plain-text notice with counts only, plus an “打开收件箱” button that switches to the task tab and selects “待我审批”.
2. Tab title `任务与审批（N）` when inbox count > 0.
3. Hide notice and reset the tab title without `task:approve`, on identity switch, and when the inbox is empty.

## Out of scope

- Windows tray / toast / sound
- Auto-loading Action Preview
- Background polling beyond existing refresh-on-show / manual refresh
- Real external tools, remote idempotency, compensation

## Non-negotiables

- No filenames, bodies, tokens, paths, or preview hashes in the notice.
- Server remains the authority for preview/approve/reject.
- Self-created pending writes stay out of the count.
