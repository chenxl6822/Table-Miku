# 2026-08-19 Knowledge Assistant 2.5 Approval Tray Design

## Goal

When an Approver/Admin inbox count rises after a normal task refresh, show one Windows tray balloon with counts only. Clicking it opens the console inbox. No extra polling.

## In scope

1. Pure helpers: tray title/message formatting and a rising-edge gate keyed by local identity.
2. After the existing `_update_approval_notice` path, emit a Qt signal when the gate says notify.
3. Reuse the main window `QSystemTrayIcon`. No second tray.
4. Click opens the Knowledge Assistant console and selects「待我审批」.
5. Missing tray or `supportsMessages() == False`: skip silently; in-app notice unchanged.

## Out of scope

- Polling after the console is closed
- Per-task balloons, custom sound, notification persistence
- Real HTTP tools, schema/API changes, inbox filter/notice copy changes

## When to notify

- Same task snapshot as the in-app notice (`select_inbox_tasks`).
- Rising edge only: for the current identity, notify iff `count > last_notified_count`.
- Count drop updates the baseline and does not notify.
- Identity switch or loss of `task:approve` resets the baseline; do not compare against the previous identity.
- Closing/hiding the console does not reset the gate (the dialog instance is reused). Same count after reopen does not notify.
- No new timer. Closed console does not toast.

## Copy

- Title: `企业知识助手`
- Body: `format_inbox_expiry_hint` text (counts and expiry totals only), e.g. `待我审批 2 个：即将到期 1。`
- Forbidden in title/body: task ids, titles, summaries, filenames, paths, tokens, preview hashes, tenant/user ids.

## Non-negotiables

- Viewer/Editor without `task:approve`: never notify.
- Self-created pending writes stay out of the count.
- Server remains the authority for preview/approve/reject.
- Click must not auto-load Action Preview.
- Do not log or trace the balloon body beyond the existing count-only notice rules.
