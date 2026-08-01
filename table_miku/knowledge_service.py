"""Unified knowledge entry points backed by SQLite with JSON fallback."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from . import knowledge_db, knowledge_repository as repo
from .encoding_utils import normalize_zh_text
from .knowledge_base import (
    _fallback_card,
    compact_card_for_context as legacy_compact_card_for_context,
    format_knowledge as legacy_format_knowledge,
    load_knowledge as legacy_load_knowledge,
    refresh_computer_knowledge as legacy_refresh_computer_knowledge,
)
from .knowledge_ingest import ingest_trusted_topics
from .knowledge_migration import migrate_json_to_sqlite
from .storage import DEFAULT_KNOWLEDGE_TOPICS, load_settings


_initialized_repository_keys: set[tuple[str, tuple[str, ...]]] = set()


class KnowledgeStorageError(RuntimeError):
    """Raised when a knowledge mutation could not be persisted safely."""


def _normalize_knowledge_topics(topics: list[str] | None = None) -> list[str]:
    """Return required topics plus caller-provided extras without duplicates."""
    normalized: list[str] = []
    for topic in DEFAULT_KNOWLEDGE_TOPICS + (topics or []):
        cleaned = normalize_zh_text(str(topic)).strip()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def qa_pairs_for_card(card: dict[str, Any]) -> list[dict[str, str]]:
    """Return complete QA pairs, synthesizing answers for legacy cards."""
    pairs: list[dict[str, str]] = []
    for item in card.get("qa_pairs") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question and answer:
            pairs.append({"question": question, "answer": answer})
    if pairs:
        return pairs

    for item in card.get("review_questions") or []:
        question = str(item).strip()
        if not question:
            continue
        answer = repo._generate_fallback_answer(question, card)
        if answer:
            pairs.append({"question": question, "answer": answer})
    return pairs


def ensure_knowledge_repository(topics: list[str] | None = None) -> None:
    """Initialize SQLite knowledge data and seed missing default topics."""
    selected_topics = _normalize_knowledge_topics(topics)
    cache_key = (
        str(knowledge_db.knowledge_db_path().resolve()),
        tuple(sorted(topic.strip().lower() for topic in selected_topics)),
    )
    if cache_key in _initialized_repository_keys and knowledge_db.knowledge_db_path().exists():
        return

    knowledge_db.init_db()
    migrate_json_to_sqlite(force=False)
    existing_by_topic = repo.find_card_ids_by_topics(selected_topics)
    card_ids: list[str] = []
    for topic in selected_topics:
        existing_id = existing_by_topic.get(topic.strip().lower())
        if existing_id:
            card_ids.append(existing_id)
            continue
        card_ids.append(repo.upsert_card(_fallback_card(topic)))

    conn = repo._connect()
    try:
        for card_id in card_ids:
            repo.ensure_review_state(conn, card_id)
        conn.commit()
    finally:
        conn.close()
    _initialized_repository_keys.add(cache_key)


def load_knowledge_cards(limit: int = 100) -> list[dict[str, Any]]:
    """Return knowledge cards for UI/assistant use, preferring SQLite."""
    try:
        ensure_knowledge_repository()
        cards = repo.list_cards(limit=limit)
        if cards:
            details = repo.load_card_details([str(card.get("id") or "") for card in cards])
            return [
                _repository_card_to_legacy(card, details.get(str(card.get("id") or "")))
                for card in cards
            ]
    except Exception:
        pass
    return legacy_load_knowledge()[:limit]


def search_knowledge_cards(query: str, limit: int = 20) -> list[dict[str, Any]]:
    try:
        ensure_knowledge_repository()
        cards = repo.search_cards(query, limit=limit)
        details = repo.load_card_details([str(card.get("id") or "") for card in cards])
        return [
            _repository_card_to_legacy(card, details.get(str(card.get("id") or "")))
            for card in cards
        ]
    except Exception:
        query_lower = query.lower()
        return [
            card for card in legacy_load_knowledge()
            if query_lower in str(card.get("topic") or card.get("title") or card.get("overview") or "").lower()
        ][:limit]


def refresh_knowledge_repository(topics: list[str] | None = None) -> dict[str, Any]:
    """Refresh legacy online cards, upsert into SQLite, then add trusted sources."""
    selected_topics = _normalize_knowledge_topics(topics)
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
        card_ids = [str(state.get("card_id") or "") for state in due]
        cards = {str(card["id"]): card for card in repo.get_cards(card_ids)}
        details = repo.load_card_details(card_ids)
        items: list[dict[str, Any]] = []
        for state in due:
            card_id = str(state.get("card_id") or "")
            card = cards.get(card_id)
            if not card:
                continue
            items.append(
                {
                    "card": _repository_card_to_legacy(card, details.get(card_id)),
                    "state": state,
                }
            )
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
    except (sqlite3.Error, OSError) as ex:
        raise KnowledgeStorageError("知识复习结果未能写入 SQLite，未改写旧 JSON 数据。") from ex


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


def _repository_card_to_legacy(
    card: dict[str, Any],
    details: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    card_id = str(card.get("id") or "")
    chunks = details.get("chunks", []) if details is not None else repo.list_chunks(card_id) if card_id else []
    sources = details.get("sources", []) if details is not None else repo.list_sources_for_card(card_id) if card_id else []
    qa_pairs = details.get("qa_pairs", []) if details is not None else repo.list_qa_pairs(card_id) if card_id else []
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
