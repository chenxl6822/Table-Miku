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


def test_qa_pairs_for_legacy_card_synthesizes_nonempty_answers():
    pairs = knowledge_service.qa_pairs_for_card(
        {
            "overview": "TCP 通过握手建立连接并协商双方的初始序列号。",
            "review_questions": ["TCP 为什么需要握手？", ""],
        },
    )

    assert pairs == [
        {
            "question": "TCP 为什么需要握手？",
            "answer": "TCP 通过握手建立连接并协商双方的初始序列号。",
        },
    ]


def test_ensure_repository_seeds_topic(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)

    knowledge_service.ensure_knowledge_repository(["计算机网络"])
    card = repo.get_card_by_topic("计算机网络")

    assert card is not None
    assert card["topic"] == "计算机网络"


def test_load_knowledge_cards_returns_ui_shape(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    knowledge_service.ensure_knowledge_repository(["计算机网络"])

    cards = knowledge_service.load_knowledge_cards(limit=5)

    assert cards
    assert "sections" in cards[0]
    assert "qa_pairs" in cards[0]
    assert "source_name" in cards[0]


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


def test_refresh_repository_ingests_trusted_sources(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "MOC-计算机网络.md").write_text(
        "# MOC-计算机网络\n\n## 核心知识点\n\n计算机网络包括 TCP、HTTP、DNS。",
        encoding="utf-8",
    )
    monkeypatch.setattr(knowledge_service, "_configured_obsidian_root", lambda: vault)
    monkeypatch.setattr(
        knowledge_service,
        "legacy_refresh_computer_knowledge",
        lambda topics: [_fallback_card(topic) for topic in topics],
    )

    summary = knowledge_service.refresh_knowledge_repository(["计算机网络"])
    card = repo.get_card_by_topic("计算机网络")

    assert summary["trusted_sources"] >= 2
    assert summary["trusted_chunks"] >= 2
    assert card is not None
    assert card["source_count"] >= 2


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
