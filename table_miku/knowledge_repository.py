"""Repository layer for knowledge cards, sources, chunks, search, reviews, and QA pairs.

All mutations go through this module.  The callers (UI, migration, tests) should
not need to write SQL directly.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from . import knowledge_db as _db
from .review_scheduler import (
    apply_review_result,
    default_review_state,
    REVIEW_INTERVALS,
    MAX_STAGE,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DT_FMT = "%Y-%m-%dT%H:%M:%S"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _connect() -> sqlite3.Connection:
    conn = _db.connect()
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def upsert_card(card: dict[str, Any]) -> str:
    """Insert or update a knowledge card.  Returns the card id."""
    conn = _connect()
    try:
        card_id = str(card.get("id") or _uid("card-"))
        title = str(card.get("title") or card.get("topic", ""))
        topic = str(card.get("topic", ""))
        normalized_topic = _normalize_topic(topic)
        overview = str(card.get("overview", ""))
        difficulty = str(card.get("difficulty", "normal"))
        tags = json.dumps(card.get("tags") or [], ensure_ascii=False)
        now = _now()
        created_at = str(card.get("created_at") or now)
        updated_at = now
        archived = int(card.get("archived", 0))

        conn.execute(
            """
            INSERT INTO knowledge_cards
                (id, title, topic, normalized_topic, overview, difficulty,
                 tags, created_at, updated_at, archived)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                topic = excluded.topic,
                normalized_topic = excluded.normalized_topic,
                overview = excluded.overview,
                difficulty = excluded.difficulty,
                tags = excluded.tags,
                updated_at = excluded.updated_at,
                archived = excluded.archived
            """,
            (card_id, title, topic, normalized_topic, overview, difficulty,
             tags, created_at, updated_at, archived),
        )

        # Upsert related data from card dict
        _upsert_card_extras(conn, card_id, card)
        _refresh_card_fts(conn, card_id)

        conn.commit()
        return card_id
    finally:
        conn.close()


def _upsert_card_extras(conn: sqlite3.Connection, card_id: str, card: dict[str, Any]) -> None:
    """Persist sections, key_points, glossary, examples, review_questions
    as chunks and QA pairs if present in the card dict."""
    now = _now()

    # Sections as chunks
    for section in card.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading", ""))
        content = str(section.get("content", ""))
        if not content:
            continue
        chunk_id = _uid("chunk-")
        content_hash = _hash_content(content)
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_chunks
                (id, card_id, source_id, heading, content, content_hash,
                 quality_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chunk_id, card_id, None, heading, content, content_hash, 0.7, now),
        )

    # Key points as chunks
    for kp in card.get("key_points") or []:
        text = str(kp).strip()
        if not text:
            continue
        chunk_id = _uid("chunk-")
        content_hash = _hash_content(text)
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_chunks
                (id, card_id, source_id, heading, content, content_hash,
                 quality_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chunk_id, card_id, None, "关键点", text, content_hash, 0.6, now),
        )

    # Glossary as chunks
    for item in card.get("glossary") or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", ""))
        explanation = str(item.get("explanation", ""))
        text = f"{term}：{explanation}"
        if not term or not explanation:
            continue
        chunk_id = _uid("chunk-")
        content_hash = _hash_content(text)
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_chunks
                (id, card_id, source_id, heading, content, content_hash,
                 quality_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chunk_id, card_id, None, f"术语：{term}", text, content_hash, 0.5, now),
        )

    # Review questions → QA pairs (without answers initially)
    existing_qa_questions = _existing_qa_questions(conn, card_id)
    for q in card.get("review_questions") or []:
        question = str(q).strip()
        if not question or question in existing_qa_questions:
            continue
        # Try to generate a fallback answer from card data
        answer = _generate_fallback_answer(question, card)
        if not answer:
            continue
        qa_id = _uid("qa-")
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_qa_pairs
                (id, card_id, question, answer, source_chunk_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (qa_id, card_id, question, answer, "", now, now),
        )


def _existing_qa_questions(conn: sqlite3.Connection, card_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT question FROM knowledge_qa_pairs WHERE card_id = ?", (card_id,)
    ).fetchall()
    return {r[0] for r in rows}


def _generate_fallback_answer(question: str, card: dict[str, Any]) -> str:
    """Generate a fallback answer from card overview / key points / sections."""
    parts: list[str] = []
    overview = str(card.get("overview", ""))
    if overview:
        parts.append(overview[:200])
    for kp in card.get("key_points") or []:
        text = str(kp).strip()
        if text:
            parts.append(text)
    for section in card.get("sections") or []:
        if isinstance(section, dict):
            content = str(section.get("content", ""))
            if content:
                parts.append(content)
    return "；".join(parts[:3]) if parts else ""


def get_card(card_id: str) -> dict[str, Any] | None:
    """Return a single knowledge card by id."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM knowledge_cards WHERE id = ? AND archived = 0",
            (card_id,),
        ).fetchone()
        if row is None:
            return None
        card = _row_to_dict(row)
        card["tags"] = _parse_tags(card.get("tags"))
        _enrich_card(conn, card)
        return card
    finally:
        conn.close()


def get_card_by_topic(topic: str) -> dict[str, Any] | None:
    """Return the first unarchived card matching *topic*."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM knowledge_cards WHERE normalized_topic = ? AND archived = 0 LIMIT 1",
            (_normalize_topic(topic),),
        ).fetchone()
        if row is None:
            return None
        card = _row_to_dict(row)
        card["tags"] = _parse_tags(card.get("tags"))
        _enrich_card(conn, card)
        return card
    finally:
        conn.close()


def get_cards(card_ids: list[str]) -> list[dict[str, Any]]:
    """Return unarchived cards for the requested ids using one connection."""
    unique_ids = list(dict.fromkeys(card_id for card_id in card_ids if card_id))
    if not unique_ids:
        return []
    placeholders = ",".join("?" for _ in unique_ids)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM knowledge_cards WHERE archived = 0 AND id IN ({placeholders})",
            unique_ids,
        ).fetchall()
        cards_by_id = {str(row["id"]): dict(row) for row in rows}
        cards = [cards_by_id[card_id] for card_id in unique_ids if card_id in cards_by_id]
        for card in cards:
            card["tags"] = _parse_tags(card.get("tags"))
        _enrich_cards(conn, cards)
        return cards
    finally:
        conn.close()


def find_card_ids_by_topics(topics: list[str]) -> dict[str, str]:
    """Map normalized topics to existing unarchived card ids."""
    normalized = list(dict.fromkeys(_normalize_topic(topic) for topic in topics if topic.strip()))
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT normalized_topic, id
            FROM knowledge_cards
            WHERE archived = 0 AND normalized_topic IN ({placeholders})
            ORDER BY updated_at DESC
            """,
            normalized,
        ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result.setdefault(str(row["normalized_topic"]), str(row["id"]))
        return result
    finally:
        conn.close()


def list_cards(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """List unarchived cards, most-recently-updated first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM knowledge_cards WHERE archived = 0 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        cards = _rows_to_dicts(rows)
        for c in cards:
            c["tags"] = _parse_tags(c.get("tags"))
        _enrich_cards(conn, cards)
        return cards
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def add_source(source: dict[str, Any]) -> str:
    """Insert or replace a knowledge source.  Returns the source id."""
    conn = _connect()
    try:
        sid = str(source.get("id") or _uid("src-"))
        name = str(source.get("name", ""))
        kind = str(source.get("kind", "unknown"))
        url = str(source.get("url", ""))
        license_note = str(source.get("license_note", ""))
        fetched_at = str(source.get("fetched_at") or _now())
        status = str(source.get("status", "active"))

        conn.execute(
            """
            INSERT INTO knowledge_sources
                (id, name, kind, url, license_note, fetched_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                kind = excluded.kind,
                url = excluded.url,
                license_note = excluded.license_note,
                fetched_at = excluded.fetched_at,
                status = excluded.status
            """,
            (sid, name, kind, url, license_note, fetched_at, status),
        )
        conn.commit()
        return sid
    finally:
        conn.close()


def get_source(source_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


def add_chunk(card_id: str, source_id: str | None, chunk: dict[str, Any]) -> str:
    """Add a content chunk linked to a card and source.  Returns the chunk id."""
    conn = _connect()
    try:
        cid = str(chunk.get("id") or _uid("chunk-"))
        heading = str(chunk.get("heading", ""))
        content = str(chunk.get("content", ""))
        content_hash = _hash_content(content)
        quality = float(chunk.get("quality_score", 0.5))
        created_at = str(chunk.get("created_at") or _now())
        # Treat empty string as NULL for FK constraint
        sid = source_id if source_id else None

        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_chunks
                (id, card_id, source_id, heading, content, content_hash,
                 quality_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, card_id, sid, heading, content, content_hash, quality, created_at),
        )

        existing = conn.execute(
            """
            SELECT id FROM knowledge_chunks
            WHERE card_id = ? AND content_hash = ? AND heading = ?
              AND COALESCE(source_id, '') = COALESCE(?, '')
            """,
            (card_id, content_hash, heading, sid),
        ).fetchone()
        if existing:
            cid = str(existing[0])

        _refresh_card_fts(conn, card_id)

        conn.commit()
        return cid
    finally:
        conn.close()


def list_chunks(card_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Return chunks for a card, oldest first."""
    _own = conn is None
    if _own:
        conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM knowledge_chunks
            WHERE card_id = ?
            ORDER BY created_at ASC
            """,
            (card_id,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        if _own:
            conn.close()


def list_sources_for_card(card_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Return distinct source records linked to a card."""
    _own = conn is None
    if _own:
        conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT ks.*
            FROM knowledge_sources ks
            JOIN knowledge_chunks kc ON kc.source_id = ks.id
            WHERE kc.card_id = ?
            ORDER BY ks.fetched_at DESC, ks.name ASC
            """,
            (card_id,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        if _own:
            conn.close()


def load_card_details(card_ids: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Load chunks, sources, and QA pairs for many cards with one connection."""
    unique_ids = list(dict.fromkeys(card_id for card_id in card_ids if card_id))
    details = {
        card_id: {"chunks": [], "sources": [], "qa_pairs": []}
        for card_id in unique_ids
    }
    if not unique_ids:
        return details

    placeholders = ",".join("?" for _ in unique_ids)
    conn = _connect()
    try:
        chunk_rows = conn.execute(
            f"""
            SELECT * FROM knowledge_chunks
            WHERE card_id IN ({placeholders})
            ORDER BY created_at ASC
            """,
            unique_ids,
        ).fetchall()
        for row in chunk_rows:
            details[str(row["card_id"])]["chunks"].append(dict(row))

        source_rows = conn.execute(
            f"""
            SELECT DISTINCT kc.card_id, ks.*
            FROM knowledge_chunks kc
            JOIN knowledge_sources ks ON ks.id = kc.source_id
            WHERE kc.card_id IN ({placeholders})
            ORDER BY ks.fetched_at DESC, ks.name ASC
            """,
            unique_ids,
        ).fetchall()
        for row in source_rows:
            source = dict(row)
            card_id = str(source.pop("card_id"))
            details[card_id]["sources"].append(source)

        qa_rows = conn.execute(
            f"""
            SELECT * FROM knowledge_qa_pairs
            WHERE card_id IN ({placeholders})
            ORDER BY created_at ASC
            """,
            unique_ids,
        ).fetchall()
        for row in qa_rows:
            details[str(row["card_id"])]["qa_pairs"].append(dict(row))
        return details
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_cards(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Full-text search across cards.  Falls back to LIKE when FTS5 unavailable."""
    conn = _connect()
    try:
        if _db._check_fts5(conn):
            results = _fts_search(conn, query, limit)
        else:
            results = _like_search(conn, query, limit)
        return results
    finally:
        conn.close()


def _fts_search(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    # Sanitize: FTS5 query syntax characters need escaping
    safe_query = _escape_fts5(query)
    try:
        rows = conn.execute(
            """
            SELECT kc.*, kg_fts.rank
            FROM knowledge_fts kg_fts
            JOIN knowledge_cards kc ON kg_fts.rowid = kc.rowid
            WHERE knowledge_fts MATCH ? AND kc.archived = 0
            ORDER BY kg_fts.rank
            LIMIT ?
            """,
            (safe_query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return _like_search(conn, query, limit)

    # FTS5 with unicode61 treats each CJK char as a separate token,
    # so multi-char Chinese queries may return 0 results.  Fall back to LIKE.
    if not rows:
        return _like_search(conn, query, limit)

    results = [dict(row) for row in rows]
    _enrich_cards(conn, results)
    for card in results:
        card.pop("rank", None)
        card["tags"] = _parse_tags(card.get("tags"))
        card["snippet"] = _build_snippet(card, query)
    return results


def _like_search(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    pattern = f"%{query}%"
    rows = conn.execute(
        """
        SELECT * FROM knowledge_cards
        WHERE archived = 0
          AND (
              title LIKE ? OR topic LIKE ? OR overview LIKE ?
              OR EXISTS (
                  SELECT 1 FROM knowledge_chunks
                  WHERE knowledge_chunks.card_id = knowledge_cards.id
                    AND knowledge_chunks.content LIKE ?
              )
          )
        ORDER BY
            CASE WHEN title LIKE ? THEN 0
                 WHEN topic LIKE ? THEN 1
                 ELSE 2 END,
            updated_at DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, pattern, pattern, pattern, limit),
    ).fetchall()

    results = [dict(row) for row in rows]
    _enrich_cards(conn, results)
    for card in results:
        card["tags"] = _parse_tags(card.get("tags"))
        card["snippet"] = _build_snippet(card, query)
    return results


def _escape_fts5(query: str) -> str:
    """Escape special FTS5 query characters and wrap tokens in quotes."""
    # Strip dangerous characters
    safe = query.replace('"', "").replace("*", "").replace("(", "").replace(")", "")
    if not safe.strip():
        return '""'
    # Quote each token for prefix matching
    tokens = safe.split()
    return " AND ".join(f'"{t}"' for t in tokens)


def _build_snippet(card: dict[str, Any], query: str) -> str:
    overview = str(card.get("overview", ""))
    if not overview:
        return ""
    # Simple snippet: find query position and show surrounding text
    idx = overview.lower().find(query.lower())
    if idx < 0:
        return overview[:120]
    start = max(0, idx - 30)
    end = min(len(overview), idx + len(query) + 60)
    snippet = overview[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(overview):
        snippet += "…"
    return snippet


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def ensure_review_state(conn: sqlite3.Connection, card_id: str) -> dict[str, Any]:
    """Ensure *card_id* has a row in review_states.  Idempotent.

    If the card does not yet exist in ``knowledge_cards``, a minimal row is
    created first so the FK constraint is satisfied.
    """
    row = conn.execute(
        "SELECT * FROM review_states WHERE card_id = ?", (card_id,)
    ).fetchone()
    if row:
        return dict(row)

    # Ensure the card exists so FK does not fail
    card_row = conn.execute(
        "SELECT id FROM knowledge_cards WHERE id = ?", (card_id,)
    ).fetchone()
    if not card_row:
        now = _now()
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_cards
                (id, title, topic, normalized_topic, overview, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (card_id, card_id, card_id, _normalize_topic(card_id), "", now, now),
        )

    now = _now()
    state = {
        "card_id": card_id,
        "mastery": 0.0,
        "review_stage": 0,
        "next_review_at": now,
        "last_reviewed_at": None,
        "review_count": 0,
        "updated_at": now,
    }
    conn.execute(
        """
        INSERT INTO review_states
            (card_id, mastery, review_stage, next_review_at,
             last_reviewed_at, review_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (card_id, state["mastery"], state["review_stage"], state["next_review_at"],
         state["last_reviewed_at"], state["review_count"], state["updated_at"]),
    )
    return state


def get_due_reviews(now: datetime | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Return cards + review_states that are due for review."""
    now = now or datetime.now()
    now_str = now.isoformat(timespec="seconds")
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT rs.*, kc.topic, kc.title, kc.overview
            FROM review_states rs
            JOIN knowledge_cards kc ON rs.card_id = kc.id
            WHERE kc.archived = 0
              AND rs.next_review_at <= ?
            ORDER BY rs.next_review_at ASC, rs.mastery ASC
            LIMIT ?
            """,
            (now_str, limit),
        ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["qa_pairs"] = list_qa_pairs(item["card_id"], conn=conn)
            results.append(item)
        return results
    finally:
        conn.close()


def record_review(
    card_id: str,
    result: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Record a review result.  Updates review_states and appends to review_history."""
    now = now or datetime.now()
    now_str = now.isoformat(timespec="seconds")
    conn = _connect()
    try:
        state = ensure_review_state(conn, card_id)

        # Apply scheduler logic
        old_stage = state["review_stage"]
        old_mastery = state["mastery"]
        review_count = state["review_count"] + 1

        if result == "known":
            new_stage = min(old_stage + 1, MAX_STAGE)
            new_mastery = min(old_mastery + 0.2, 1.0)
        elif result == "fuzzy":
            new_stage = old_stage
            new_mastery = min(old_mastery + 0.05, 1.0)
        elif result == "forgotten":
            new_stage = 0
            new_mastery = max(old_mastery - 0.15, 0.0)
        else:
            raise ValueError(f"Unknown review result: {result}")

        # Use existing scheduler intervals
        stage_idx = min(new_stage, len(REVIEW_INTERVALS) - 1)
        next_review_at = (now + REVIEW_INTERVALS[stage_idx]).isoformat(timespec="seconds")

        conn.execute(
            """
            UPDATE review_states
            SET mastery = ?, review_stage = ?, next_review_at = ?,
                last_reviewed_at = ?, review_count = ?, updated_at = ?
            WHERE card_id = ?
            """,
            (new_mastery, new_stage, next_review_at, now_str,
             review_count, now_str, card_id),
        )

        # Append to review_history
        history_id = _uid("rh-")
        conn.execute(
            """
            INSERT INTO review_history
                (id, card_id, reviewed_at, result, note, mastery_after, stage_after)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (history_id, card_id, now_str, result, note, new_mastery, new_stage),
        )

        conn.commit()
        return {
            "card_id": card_id,
            "mastery": new_mastery,
            "review_stage": new_stage,
            "next_review_at": next_review_at,
            "last_reviewed_at": now_str,
            "review_count": review_count,
            "updated_at": now_str,
        }
    finally:
        conn.close()


def get_review_history(card_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent review history entries for a card."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM review_history
            WHERE card_id = ?
            ORDER BY reviewed_at DESC
            LIMIT ?
            """,
            (card_id, limit),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# QA Pairs
# ---------------------------------------------------------------------------


def list_qa_pairs(card_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Return all QA pairs for a card, ordered by creation time."""
    _own = conn is None
    if _own:
        conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM knowledge_qa_pairs
            WHERE card_id = ?
            ORDER BY created_at ASC
            """,
            (card_id,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        if _own:
            conn.close()


def upsert_qa_pair(
    card_id: str,
    question: str,
    answer: str,
    source_chunk_id: str = "",
) -> str:
    """Insert or update a QA pair.  Returns the pair id."""
    if not question.strip() or not answer.strip():
        raise ValueError("question and answer must both be non-empty")

    conn = _connect()
    try:
        # Check for existing pair with same question
        existing = conn.execute(
            "SELECT id FROM knowledge_qa_pairs WHERE card_id = ? AND question = ?",
            (card_id, question.strip()),
        ).fetchone()

        now = _now()
        if existing:
            qa_id = existing[0]
            conn.execute(
                """
                UPDATE knowledge_qa_pairs
                SET answer = ?, source_chunk_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (answer.strip(), source_chunk_id, now, qa_id),
            )
        else:
            qa_id = _uid("qa-")
            conn.execute(
                """
                INSERT INTO knowledge_qa_pairs
                    (id, card_id, question, answer, source_chunk_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (qa_id, card_id, question.strip(), answer.strip(),
                 source_chunk_id, now, now),
            )

        conn.commit()
        return qa_id
    finally:
        conn.close()


def delete_qa_pair(qa_id: str) -> bool:
    """Delete a QA pair.  Returns True if a row was deleted."""
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM knowledge_qa_pairs WHERE id = ?", (qa_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def find_duplicates_by_url(url: str) -> list[dict[str, Any]]:
    """Find cards that have chunks from a source with the given URL."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT kc.*
            FROM knowledge_cards kc
            JOIN knowledge_chunks kch ON kc.id = kch.card_id
            JOIN knowledge_sources ks ON kch.source_id = ks.id
            WHERE ks.url = ? AND kc.archived = 0
            """,
            (url,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def find_duplicates_by_hash(content_hash: str) -> list[dict[str, Any]]:
    """Find cards with chunks having the same content hash."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT kc.*
            FROM knowledge_cards kc
            JOIN knowledge_chunks kch ON kc.id = kch.card_id
            WHERE kch.content_hash = ? AND kc.archived = 0
            """,
            (content_hash,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def record_dedupe(
    winner_card_id: str,
    duplicate_card_id: str,
    score: float,
    reason: str,
) -> str:
    """Record a deduplication relationship."""
    conn = _connect()
    try:
        did = _uid("dd-")
        now = _now()
        conn.execute(
            """
            INSERT OR IGNORE INTO dedupe_links
                (id, winner_card_id, duplicate_card_id, score, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (did, winner_card_id, duplicate_card_id, score, reason, now),
        )
        conn.commit()
        return did
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ingest jobs
# ---------------------------------------------------------------------------


def create_ingest_job(source_kind: str, query: str) -> str:
    conn = _connect()
    try:
        jid = _uid("job-")
        now = _now()
        conn.execute(
            """
            INSERT INTO ingest_jobs (id, source_kind, query, status, started_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (jid, source_kind, query, now),
        )
        conn.commit()
        return jid
    finally:
        conn.close()


def update_ingest_job(job_id: str, status: str, error: str = "") -> None:
    conn = _connect()
    try:
        now = _now()
        if status in ("completed", "failed"):
            conn.execute(
                "UPDATE ingest_jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status, now, error, job_id),
            )
        else:
            conn.execute(
                "UPDATE ingest_jobs SET status = ?, error = ? WHERE id = ?",
                (status, error, job_id),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_topic(topic: str) -> str:
    return topic.strip().lower()


def _hash_content(content: str) -> str:
    import hashlib
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _parse_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _enrich_card(conn: sqlite3.Connection, card: dict[str, Any]) -> None:
    """Add computed fields to a card dict: source_count, mastery, next_review_at."""
    _enrich_cards(conn, [card])


def _enrich_cards(conn: sqlite3.Connection, cards: list[dict[str, Any]]) -> None:
    """Add source/review metadata to many cards with one aggregate query."""
    card_by_id = {str(card.get("id")): card for card in cards if card.get("id")}
    for card in cards:
        card["source_count"] = 0
        card["mastery"] = 0.0
        card["next_review_at"] = None
    if not card_by_id:
        return

    placeholders = ",".join("?" for _ in card_by_id)
    rows = conn.execute(
        f"""
        SELECT kc.id,
               COUNT(DISTINCT kch.source_id) AS source_count,
               COALESCE(rs.mastery, 0.0) AS mastery,
               rs.next_review_at
        FROM knowledge_cards kc
        LEFT JOIN knowledge_chunks kch
               ON kch.card_id = kc.id AND kch.source_id IS NOT NULL
        LEFT JOIN review_states rs ON rs.card_id = kc.id
        WHERE kc.id IN ({placeholders})
        GROUP BY kc.id, rs.mastery, rs.next_review_at
        """,
        list(card_by_id),
    ).fetchall()
    for row in rows:
        card = card_by_id[str(row["id"])]
        card["source_count"] = int(row["source_count"] or 0)
        card["mastery"] = float(row["mastery"] or 0.0)
        card["next_review_at"] = row["next_review_at"]


def _refresh_card_fts(conn: sqlite3.Connection, card_id: str) -> None:
    card = conn.execute(
        "SELECT title, topic, overview FROM knowledge_cards WHERE id = ?",
        (card_id,),
    ).fetchone()
    if card is None:
        return
    chunks = conn.execute(
        "SELECT content FROM knowledge_chunks WHERE card_id = ? ORDER BY created_at ASC",
        (card_id,),
    ).fetchall()
    full_content = " ".join(str(row[0]) for row in chunks if row[0])
    _upsert_fts(
        conn,
        card_id,
        title=str(card[0]),
        topic=str(card[1]),
        overview=str(card[2]),
        content=full_content,
    )


def _upsert_fts(
    conn: sqlite3.Connection,
    card_id: str,
    *,
    title: str,
    topic: str,
    overview: str,
    content: str,
) -> None:
    """Update the FTS index for *card_id* (no-op when FTS5 is unavailable)."""
    if not _db._check_fts5(conn):
        return
    try:
        # Map card_id → FTS rowid via knowledge_cards.rowid
        row = conn.execute(
            "SELECT rowid FROM knowledge_cards WHERE id = ?", (card_id,)
        ).fetchone()
        if row is None:
            return
        kc_rowid = row[0]

        # Delete existing FTS entry for this rowid
        conn.execute(
            "DELETE FROM knowledge_fts WHERE rowid = ?", (kc_rowid,)
        )
        # Insert / replace
        conn.execute(
            """
            INSERT INTO knowledge_fts (rowid, title, topic, overview, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kc_rowid, title, topic, overview, content),
        )
    except sqlite3.OperationalError:
        pass  # FTS5 unavailable — silently skip
