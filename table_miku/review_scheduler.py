from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

REVIEW_INTERVALS = [
    timedelta(hours=1),   # stage 0 -> 1h
    timedelta(days=1),    # stage 1 -> 1d
    timedelta(days=3),    # stage 2 -> 3d
    timedelta(days=7),    # stage 3 -> 7d
    timedelta(days=14),   # stage 4 -> 14d
    timedelta(days=30),   # stage 5 -> 30d
]

MAX_MASTERY = 1.0
MIN_MASTERY = 0.0
MAX_STAGE = len(REVIEW_INTERVALS) - 1


def default_review_state(card_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Create initial review state for a knowledge card."""
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    return {
        "card_id": card_id,
        "mastery": MIN_MASTERY,
        "review_stage": 0,
        "next_review_at": ts,
        "last_reviewed_at": None,
        "review_count": 0,
        "created_at": ts,
        "updated_at": ts,
        "history": [],
    }


def apply_review_result(
    state: dict[str, Any],
    result: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a review feedback result and return the updated state.

    result must be one of: 'known', 'fuzzy', 'forgotten'.
    """
    now = now or datetime.now()
    ts = now.isoformat(timespec="seconds")

    if result == "known":
        state["review_stage"] = min(state["review_stage"] + 1, MAX_STAGE)
        state["mastery"] = min(state["mastery"] + 0.2, MAX_MASTERY)
    elif result == "fuzzy":
        state["mastery"] = min(state["mastery"] + 0.05, MAX_MASTERY)
    elif result == "forgotten":
        state["review_stage"] = 0
        state["mastery"] = max(state["mastery"] - 0.15, MIN_MASTERY)
    else:
        raise ValueError(f"Unknown review result: {result}")

    state["review_count"] = state.get("review_count", 0) + 1
    state["last_reviewed_at"] = ts
    state["next_review_at"] = (now + REVIEW_INTERVALS[state["review_stage"]]).isoformat(timespec="seconds")
    state["updated_at"] = ts
    state.setdefault("history", []).append({
        "at": ts,
        "result": result,
        "note": note,
    })
    return state


def is_due(state: dict[str, Any], now: datetime | None = None) -> bool:
    """Check if a review item is due (next_review_at is in the past)."""
    now = now or datetime.now()
    next_at_str = state.get("next_review_at")
    if not next_at_str:
        return True
    try:
        next_at = datetime.fromisoformat(next_at_str)
    except (TypeError, ValueError):
        return True
    return now >= next_at


def due_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    """Sort key for due items: nearest deadline first, then lowest mastery."""
    state = item.get("state", {})
    next_at = state.get("next_review_at", "9999")
    mastery = state.get("mastery", 0.0)
    return (next_at, f"{mastery:.3f}")
