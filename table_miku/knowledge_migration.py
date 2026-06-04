"""Migrate existing JSON knowledge data into SQLite.

Reads ``knowledge_base.json`` and ``knowledge_reviews.json`` from the user data
directory and inserts / upserts rows into the SQLite knowledge database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import knowledge_db as _db
from .knowledge_repository import (
    _connect,
    _hash_content,
    _normalize_topic,
    _now,
    _uid,
    add_source,
    ensure_review_state,
    upsert_card,
    upsert_qa_pair,
)
from .storage import read_json, write_json


def migrate_json_to_sqlite(force: bool = False) -> dict[str, Any]:
    """Migrate JSON knowledge cards and review states into SQLite.

    Parameters
    ----------
    force : bool
        If True, re-migrate even when cards already exist in the database.

    Returns
    -------
    dict with keys: cards, review_states, review_history, skipped
    """
    conn = _connect()
    try:
        _db.init_db(conn)

        if not force:
            existing = conn.execute(
                "SELECT COUNT(*) FROM knowledge_cards"
            ).fetchone()[0]
            if existing > 0:
                return {
                    "cards": 0,
                    "review_states": 0,
                    "review_history": 0,
                    "skipped": existing,
                }

        cards_count = _migrate_cards(conn)
        states_count, history_count = _migrate_reviews(conn)
        conn.commit()

        return {
            "cards": cards_count,
            "review_states": states_count,
            "review_history": history_count,
            "skipped": 0,
        }
    finally:
        conn.close()


def _migrate_cards(conn) -> int:
    """Migrate knowledge_base.json → knowledge_cards + sources + chunks + fts + qa."""
    cards = read_json("knowledge_base.json", [])
    if not isinstance(cards, list):
        return 0

    count = 0
    for raw_card in cards:
        if not isinstance(raw_card, dict):
            continue
        card_id = upsert_card(raw_card)
        if card_id:
            count += 1

        # Record source
        source_url = str(raw_card.get("source_url") or raw_card.get("source", ""))
        source_name = str(raw_card.get("source_name", "offline"))
        if source_url:
            add_source({
                "id": _uid("src-"),
                "name": source_name,
                "kind": "wikipedia" if "wiki" in source_url.lower() else "offline",
                "url": source_url,
                "fetched_at": raw_card.get("fetched_at") or _now(),
                "status": "active",
            })

    return count


def _migrate_reviews(conn) -> tuple[int, int]:
    """Migrate knowledge_reviews.json → review_states + review_history."""
    reviews = read_json("knowledge_reviews.json", [])
    if not isinstance(reviews, list):
        return 0, 0

    states_count = 0
    history_count = 0

    for state in reviews:
        if not isinstance(state, dict):
            continue
        card_id = str(state.get("card_id", ""))
        if not card_id:
            continue

        # Ensure the card exists first
        card_row = conn.execute(
            "SELECT id FROM knowledge_cards WHERE id = ?", (card_id,)
        ).fetchone()
        if not card_row:
            # Create a minimal card entry so FK constraint is satisfied
            now = _now()
            conn.execute(
                """
                INSERT OR IGNORE INTO knowledge_cards
                    (id, title, topic, normalized_topic, overview,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (card_id, card_id, card_id, _normalize_topic(card_id), "", now, now),
            )

        # Upsert review_state
        now = _now()
        conn.execute(
            """
            INSERT OR REPLACE INTO review_states
                (card_id, mastery, review_stage, next_review_at,
                 last_reviewed_at, review_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                float(state.get("mastery", 0.0)),
                int(state.get("review_stage", 0)),
                str(state.get("next_review_at") or now),
                state.get("last_reviewed_at"),
                int(state.get("review_count", 0)),
                str(state.get("updated_at") or now),
            ),
        )
        states_count += 1

        # Migrate history entries
        for entry in state.get("history") or []:
            if not isinstance(entry, dict):
                continue
            hid = _uid("rh-")
            reviewed_at = str(entry.get("at") or now)
            result = str(entry.get("result", "fuzzy"))
            note = str(entry.get("note", ""))
            # We can't know the exact mastery/stage after each historical entry,
            # so use conservative estimates
            conn.execute(
                """
                INSERT OR IGNORE INTO review_history
                    (id, card_id, reviewed_at, result, note,
                     mastery_after, stage_after)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (hid, card_id, reviewed_at, result, note, 0.0, 0),
            )
            history_count += 1

    return states_count, history_count
