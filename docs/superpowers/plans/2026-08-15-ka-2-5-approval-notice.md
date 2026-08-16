# 2.5 Approval Notice Implementation Plan

> **For agentic workers:** Use executing-plans. This slice is implemented in `codex/ka-2-5-approval-notice`.

**Goal:** In-app inbox notice and tab badge for Approver/Admin.

**Architecture:** Reuse inbox counts. QLabel + button above the tabs. Click selects the existing inbox filter. No OS notifications.

**Tech Stack:** Python 3.12, PySide6, pytest.

## Global Constraints

- Counts only; no filenames/bodies/tokens.
- Visible only with `task:approve`.
- No tray, no auto-preview, no API/schema change.
