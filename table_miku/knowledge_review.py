from __future__ import annotations

from datetime import datetime
from typing import Any

from .knowledge_base import load_knowledge
from .review_scheduler import (
    apply_review_result,
    default_review_state,
    due_sort_key,
    is_due,
)
from .storage import read_json, write_json

REVIEWS_FILE = "knowledge_reviews.json"


def load_review_states() -> list[dict[str, Any]]:
    """Load review states from persistent storage."""
    data = read_json(REVIEWS_FILE, [])
    if not isinstance(data, list):
        return []
    return data


def save_review_states(states: list[dict[str, Any]]) -> None:
    """Persist review states to storage."""
    write_json(REVIEWS_FILE, states)


def ensure_review_states(cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Ensure every knowledge card has a review state. Returns updated states list."""
    cards = cards if cards is not None else load_knowledge()
    states = load_review_states()
    existing_ids = {s.get("card_id") for s in states}
    now = datetime.now()
    for card in cards:
        card_id = card.get("id", "")
        if not card_id:
            continue
        if card_id not in existing_ids:
            states.append(default_review_state(card_id, now))
            existing_ids.add(card_id)
    save_review_states(states)
    return states


def due_review_items(now: datetime | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Return knowledge cards + states that are due for review."""
    now = now or datetime.now()
    cards = load_knowledge()
    states = ensure_review_states(cards)
    card_map = {c.get("id"): c for c in cards if c.get("id")}

    due = []
    for state in states:
        if not is_due(state, now):
            continue
        card = card_map.get(state["card_id"])
        if card is None:
            continue
        due.append({"card": card, "state": state})

    due.sort(key=due_sort_key)
    return due[:limit]


def record_review(card_id: str, result: str, note: str = "", now: datetime | None = None) -> dict[str, Any] | None:
    """Record a review result for a card. Returns the updated state or None."""
    now = now or datetime.now()
    states = load_review_states()
    for state in states:
        if state.get("card_id") == card_id:
            apply_review_result(state, result, note, now)
            save_review_states(states)
            return state
    # Unknown card_id: initialize it as a new state
    new_state = default_review_state(card_id, now)
    apply_review_result(new_state, result, note, now)
    states.append(new_state)
    save_review_states(states)
    return new_state


def review_summary(now: datetime | None = None) -> str:
    """Return a summary string for daily brief. Empty string if nothing due."""
    due = due_review_items(now, limit=20)
    if not due:
        return ""
    topics = [item["card"].get("topic", item["card"].get("title", "未知")) for item in due[:5]]
    count = len(due)
    names = "、".join(topics)
    if count > 5:
        names += f"等{count}个"
    return f"知识复习：今日待复习 {count} 个：{names}。"
