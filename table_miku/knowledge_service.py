"""Unified knowledge entry points backed by SQLite with JSON fallback."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from . import knowledge_db, knowledge_repository as repo
from .knowledge_base import (
    DEFAULT_KNOWLEDGE_TOPICS,
    _fallback_card,
    compact_card_for_context as legacy_compact_card_for_context,
    format_knowledge as legacy_format_knowledge,
    load_knowledge as legacy_load_knowledge,
    normalize_knowledge_topics,
    refresh_computer_knowledge as legacy_refresh_computer_knowledge,
)
from .knowledge_ingest import ingest_trusted_topics
from .knowledge_migration import migrate_json_to_sqlite
from .storage import load_settings


def ensure_knowledge_repository(topics: list[str] | None = None) -> None:
    """Initialize SQLite knowledge data and seed missing default topics."""
    knowledge_db.init_db()
    migrate_json_to_sqlite(force=False)
    selected_topics = normalize_knowledge_topics(topics or DEFAULT_KNOWLEDGE_TOPICS)
    card_ids: list[str] = []
    for topic in selected_topics:
        existing = repo.get_card_by_topic(topic)
        if existing is not None:
            card_ids.append(str(existing["id"]))
            continue
        card_ids.append(repo.upsert_card(_fallback_card(topic)))

    conn = repo._connect()
    try:
        for card_id in card_ids:
            repo.ensure_review_state(conn, card_id)
        conn.commit()
    finally:
        conn.close()


def load_knowledge_cards(limit: int = 100) -> list[dict[str, Any]]:
    """Return knowledge cards for UI/assistant use, preferring SQLite."""
    try:
        ensure_knowledge_repository()
        cards = repo.list_cards(limit=limit)
        if cards:
            return [_repository_card_to_legacy(card) for card in cards]
    except Exception:
        pass
    return legacy_load_knowledge()[:limit]


def search_knowledge_cards(query: str, limit: int = 20) -> list[dict[str, Any]]:
    try:
        ensure_knowledge_repository()
        return [_repository_card_to_legacy(card) for card in repo.search_cards(query, limit=limit)]
    except Exception:
        query_lower = query.lower()
        return [
            card for card in legacy_load_knowledge()
            if query_lower in str(card.get("topic") or card.get("title") or card.get("overview") or "").lower()
        ][:limit]


def refresh_knowledge_repository(topics: list[str] | None = None) -> dict[str, Any]:
    """Refresh legacy online cards, upsert into SQLite, then add trusted sources."""
    selected_topics = normalize_knowledge_topics(topics)
    records = legacy_refresh_computer_knowledge(selected_topics)
    ensure_knowledge_repository(selected_topics)
    for record in records:
        repo.upsert_card(record)

    obsidian_root = _configured_obsidian_root()
    trusted_results = ingest_trusted_topics(selected_topics, obsidian_root=obsidian_root)
    online = sum(1 for record in records if not record.get("offline"))
    return {
        "topics": len(selected_topics),
        "online": online,
        "trusted_sources": sum(item["official_sources"] + item["obsidian_sources"] for item in trusted_results),
        "trusted_chunks": sum(item["chunks"] for item in trusted_results),
        "obsidian_enabled": bool(obsidian_root),
    }


def format_knowledge(records: list[dict[str, Any]] | None = None, limit: int = 12) -> str:
    return legacy_format_knowledge(records if records is not None else load_knowledge_cards(limit), limit=limit)


def knowledge_context(limit: int = 6) -> str:
    records = load_knowledge_cards(limit)
    if not records:
        return ""
    lines = [legacy_compact_card_for_context(record) for record in records[:limit]]
    return "计算机知识参考：\n" + "\n".join(lines)


def due_review_items(now: datetime | None = None, limit: int = 10) -> list[dict[str, Any]]:
    try:
        ensure_knowledge_repository()
        due = repo.get_due_reviews(now=now, limit=limit)
        items: list[dict[str, Any]] = []
        for state in due:
            card = repo.get_card(str(state.get("card_id") or ""))
            if not card:
                continue
            items.append({"card": _repository_card_to_legacy(card), "state": state})
        return items
    except Exception:
        from .knowledge_review import due_review_items as legacy_due_review_items

        return legacy_due_review_items(now=now, limit=limit)


def record_review(
    card_id: str,
    result: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    try:
        return repo.record_review(card_id, result, note=note, now=now)
    except Exception:
        from .knowledge_review import record_review as legacy_record_review

        return legacy_record_review(card_id, result, note=note, now=now)


def review_summary(now: datetime | None = None) -> str:
    due = due_review_items(now, limit=20)
    if not due:
        return ""
    topics = [item["card"].get("topic", item["card"].get("title", "未知")) for item in due[:5]]
    count = len(due)
    names = "、".join(topics)
    if count > 5:
        names += f"等{count}个"
    return f"知识复习：今日待复习 {count} 个：{names}。"


def _repository_card_to_legacy(card: dict[str, Any]) -> dict[str, Any]:
    card_id = str(card.get("id") or "")
    chunks = repo.list_chunks(card_id) if card_id else []
    sources = repo.list_sources_for_card(card_id) if card_id else []
    qa_pairs = repo.list_qa_pairs(card_id) if card_id else []
    key_points = [chunk["content"] for chunk in chunks if str(chunk.get("heading") or "") == "关键点"]
    sections = [
        {"heading": str(chunk.get("heading") or "知识片段"), "content": str(chunk.get("content") or "")}
        for chunk in chunks
        if str(chunk.get("heading") or "") != "关键点"
    ][:6]
    source_name = sources[0]["name"] if sources else card.get("source_name", "SQLite")
    source_url = sources[0]["url"] if sources else card.get("source_url", "")
    return {
        **card,
        "summary": card.get("overview", ""),
        "sections": sections,
        "key_points": key_points[:8],
        "review_questions": [pair["question"] for pair in qa_pairs],
        "qa_pairs": qa_pairs,
        "sources": sources,
        "source_name": source_name,
        "source_url": source_url,
        "source": source_url or source_name,
        "offline": source_name in {"SQLite", "offline"},
    }


def _configured_obsidian_root() -> Path | None:
    settings = load_settings()
    trusted = ((settings.get("knowledge") or {}).get("trusted_sources") or {})
    if not trusted.get("enabled", True):
        return None
    raw = str(trusted.get("obsidian_vault") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() and path.is_dir() else None
