import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_db, knowledge_repository as repo, knowledge_service
from table_miku.knowledge_base import _fallback_card


def test_normalize_topics_includes_required_defaults_and_deduplicates():
    topics = knowledge_service._normalize_knowledge_topics(
        [" 计算机网络 ", "自定义主题", "自定义主题", ""],
    )

    assert topics[:len(knowledge_service.DEFAULT_KNOWLEDGE_TOPICS)] == knowledge_service.DEFAULT_KNOWLEDGE_TOPICS
    assert topics.count("计算机网络") == 1
    assert topics[-1] == "自定义主题"


def test_qa_pairs_for_legacy_card_does_not_invent_answers():
    pairs = knowledge_service.qa_pairs_for_card(
        {
            "overview": "TCP 通过握手建立连接并协商双方的初始序列号。",
            "review_questions": ["TCP 为什么需要握手？", ""],
        },
    )

    assert pairs == []


def test_ensure_repository_seeds_topic(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)

    knowledge_service.ensure_knowledge_repository(["计算机网络"])
    card = repo.get_card_by_topic("计算机网络")

    assert card is not None
    assert card["topic"] == "计算机网络"


def test_seed_upgrade_replaces_legacy_questions_and_preserves_card_review_state(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    seed = _fallback_card("Java 后端基础")
    repo.upsert_card(
        {
            **seed,
            "qa_pairs": [{"question": "旧的泛化问题？", "answer": "整张卡片概览。"}],
        }
    )
    conn = repo._connect()
    try:
        repo.ensure_review_state(conn, seed["id"])
        conn.execute(
            """
            UPDATE review_states
            SET mastery = 0.6, review_stage = 2, review_count = 3
            WHERE card_id = ?
            """,
            (seed["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    knowledge_service.ensure_knowledge_repository(["Java 后端基础"])

    pairs = repo.list_qa_pairs(seed["id"])
    states = repo.list_questions_for_card(seed["id"])
    assert pairs
    assert all(pair["canonical_key"] for pair in pairs)
    assert all(pair["question"] != "旧的泛化问题？" for pair in pairs)
    assert all(state["mastery"] == pytest.approx(0.6) for state in states)
    assert all(state["review_stage"] == 2 for state in states)
    assert all(state["review_count"] == 3 for state in states)


def test_load_knowledge_cards_returns_ui_shape(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    knowledge_service.ensure_knowledge_repository(["计算机网络"])

    cards = knowledge_service.load_knowledge_cards(limit=5)

    assert cards
    assert "sections" in cards[0]
    assert "qa_pairs" in cards[0]
    assert "source_name" in cards[0]


def test_display_does_not_fall_back_to_legacy_json_after_sqlite_failure(monkeypatch):
    monkeypatch.setattr(
        knowledge_service,
        "ensure_knowledge_repository",
        lambda topics=None: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )

    assert knowledge_service.load_knowledge_cards() == []
    assert knowledge_service.search_knowledge_cards("TCP") == []


def test_due_review_items_returns_legacy_shape(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    knowledge_service.ensure_knowledge_repository(["计算机网络"])

    due = knowledge_service.due_review_items(limit=5)

    assert due
    assert "card" in due[0]
    assert "state" in due[0]
    assert due[0]["card"]["id"] == due[0]["state"]["card_id"]


def test_record_review_updates_sqlite(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    knowledge_service.ensure_knowledge_repository(["计算机网络"])
    card = repo.get_card_by_topic("计算机网络")
    assert card is not None

    updated = knowledge_service.record_review(card["id"], "known", note="ok")
    history = repo.get_review_history(card["id"])

    assert updated is not None
    assert updated["review_stage"] == 1
    assert history[0]["note"] == "ok"


def test_record_review_does_not_fall_back_to_json_on_sqlite_error(monkeypatch):
    def fail_review(*args, **kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(repo, "record_review", fail_review)

    with pytest.raises(knowledge_service.KnowledgeStorageError, match="未改写旧 JSON"):
        knowledge_service.record_review("card-1", "known")


def test_sync_local_repository_ingests_read_only_obsidian_note(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    note = vault / "计算机知识" / "TCP.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: knowledge\ntopic: 计算机网络\n---\n"
        "# TCP 三次握手\n\n## 一句话理解\n三次握手用于确认双方收发能力。\n\n"
        "## 常见面试问法\n1. TCP 为什么需要三次握手？\n\n"
        "## 核心概念\n- 同步双方初始序列号\n- 确认双向通信能力\n",
        encoding="utf-8",
    )
    before = note.read_text(encoding="utf-8")

    summary = knowledge_service.sync_local_knowledge(vault)
    cards = knowledge_service.search_knowledge_cards("TCP", limit=10)

    assert summary["created"] == 1
    assert summary["questions"] == 1
    assert any(card["topic"] == "计算机网络" for card in cards)
    assert note.read_text(encoding="utf-8") == before


def test_online_refresh_supplements_without_overwriting_curated_overview(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    original = _fallback_card("计算机网络")
    repo.upsert_card(original)
    monkeypatch.setattr(
        knowledge_service,
        "_normalize_knowledge_topics",
        lambda topics=None: ["计算机网络"],
    )
    monkeypatch.setattr(
        knowledge_service,
        "fetch_wikipedia_summary",
        lambda topic: {
            **original,
            "overview": "来自 Wikipedia 的概览，不应覆盖本地高质量概览。",
            "summary": "来自 Wikipedia 的概览，不应覆盖本地高质量概览。",
            "offline": False,
            "sections": [{"heading": "在线补充", "content": "一个可搜索的在线补充片段。"}],
        },
    )

    result = knowledge_service.refresh_online_knowledge(["计算机网络"])
    stored = repo.get_card(original["id"])

    assert result["online"] == 1
    assert stored is not None
    assert stored["overview"] == original["overview"]
    assert any(chunk["heading"] == "在线补充" for chunk in repo.list_chunks(original["id"]))


def test_load_knowledge_cards_batches_related_reads(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    for index in range(3):
        repo.upsert_card(dict(_fallback_card(f"topic-{index}"), id=f"card-{index}"))

    monkeypatch.setattr(knowledge_service, "ensure_knowledge_repository", lambda topics=None: None)
    original_connect = repo._connect
    connection_count = 0

    def counted_connect():
        nonlocal connection_count
        connection_count += 1
        return original_connect()

    monkeypatch.setattr(repo, "_connect", counted_connect)

    cards = knowledge_service.load_knowledge_cards(limit=3)

    assert len(cards) == 3
    assert connection_count == 2


def _use_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "knowledge_service.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    monkeypatch.setattr(knowledge_service, "migrate_json_to_sqlite", lambda force=False: {"skipped": 0})
    conn = knowledge_db.connect()
    try:
        knowledge_db.init_db(conn)
    finally:
        conn.close()
