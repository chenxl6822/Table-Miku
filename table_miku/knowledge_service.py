"""Unified knowledge entry points backed by SQLite with JSON fallback."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any

from . import knowledge_db, knowledge_repository as repo
from .encoding_utils import normalize_zh_text
from .knowledge_base import (
    _fallback_card,
    compact_card_for_context as legacy_compact_card_for_context,
    fetch_wikipedia_summary,
    format_knowledge as legacy_format_knowledge,
)
from .knowledge_migration import migrate_json_to_sqlite
from .knowledge_sync import (
    discover_obsidian_vault,
    knowledge_sync_status,
    matched_key_points,
    preview_obsidian_sync,
    sync_obsidian_knowledge,
)
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


def qa_pairs_for_card(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Return source-backed QA pairs; never invent an answer from an overview."""
    pairs: list[dict[str, Any]] = []
    for item in card.get("qa_pairs") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question and answer:
            pairs.append({**item, "question": question, "answer": answer})
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
    card_ids: list[str] = []
    for topic in selected_topics:
        seed_card = _fallback_card(topic)
        seed_id = str(seed_card.get("id") or "")
        if seed_id:
            repo.deactivate_unstructured_qa(seed_id)
        card_ids.append(repo.upsert_card(seed_card))

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
        return []


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
        return []


def refresh_knowledge_repository(topics: list[str] | None = None) -> dict[str, Any]:
    """Backward-compatible manual online refresh with bounded concurrency."""
    return refresh_online_knowledge(topics)


def sync_local_knowledge(vault_root: str | Path | None = None) -> dict[str, Any]:
    """Run the read-only local incremental sync; no network calls are made."""
    ensure_knowledge_repository()
    return sync_obsidian_knowledge(vault_root)


def preview_local_knowledge(vault_root: str | Path | None = None) -> dict[str, Any]:
    return preview_obsidian_sync(vault_root)


def local_knowledge_status() -> dict[str, Any]:
    return knowledge_sync_status()


def refresh_online_knowledge(
    topics: list[str] | None = None,
    *,
    batch_timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Fetch online summaries manually, returning partial success on timeout."""
    selected_topics = _normalize_knowledge_topics(topics)
    ensure_knowledge_repository(selected_topics)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="knowledge-online")
    future_topics = {
        executor.submit(fetch_wikipedia_summary, topic): topic
        for topic in selected_topics
    }
    done, pending = wait(future_topics, timeout=max(1.0, batch_timeout_seconds))
    for future in done:
        topic = future_topics[future]
        try:
            records.append(future.result())
        except Exception as exc:
            errors.append(f"{topic}: {exc}")
    for future in pending:
        future.cancel()
        errors.append(f"{future_topics[future]}: 整批在线更新超时")
    executor.shutdown(wait=False, cancel_futures=True)
    for record in records:
        if not record.get("offline"):
            # Wikipedia may add useful searchable fragments, but it must not
            # replace an existing local/curated overview for the same card.
            existing = repo.get_card(str(record.get("id") or ""))
            if existing and str(existing.get("overview") or "").strip():
                record = {
                    **record,
                    "overview": existing["overview"],
                    "summary": existing["overview"],
                }
            repo.upsert_card(record)
    online = sum(1 for record in records if not record.get("offline"))
    return {
        "topics": len(selected_topics),
        "online": online,
        "offline": sum(1 for record in records if record.get("offline")),
        "completed": len(records),
        "timed_out": len(pending),
        "errors": errors[:20],
    }


def due_question_items(now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
    ensure_knowledge_repository()
    return repo.list_due_questions(now=now, limit=limit)


def mistake_question_items(limit: int = 100) -> list[dict[str, Any]]:
    ensure_knowledge_repository()
    return repo.list_mistake_questions(limit=limit)


def practice_question_items(card_id: str, limit: int = 50) -> list[dict[str, Any]]:
    ensure_knowledge_repository()
    return repo.list_questions_for_card(card_id, limit=limit)


def mark_knowledge_card_learned(card_id: str, now: datetime | None = None) -> int:
    ensure_knowledge_repository()
    return repo.mark_card_learned(card_id, now=now)


def answer_key_point_hints(question: dict[str, Any], user_answer: str) -> list[str]:
    return matched_key_points(user_answer, question.get("key_points") or [])


def record_question_answer(
    qa_id: str,
    result: str,
    user_answer: str,
    matched_points: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        return repo.record_question_attempt(
            qa_id, result, user_answer, matched_points=matched_points, now=now
        )
    except (sqlite3.Error, OSError) as ex:
        raise KnowledgeStorageError("知识作答记录未能写入 SQLite。") from ex


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
    return discover_obsidian_vault()
