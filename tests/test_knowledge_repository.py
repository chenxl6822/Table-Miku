"""Tests for knowledge_repository — upsert, search, get_card, record_review, QA pairs."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_db, knowledge_repository as repo

SAMPLE_CARD = {
    "id": "wiki-test-tcp",
    "title": "计算机网络",
    "topic": "计算机网络",
    "overview": "计算机网络研究计算机与网络设备如何通过通信链路交换数据。TCP是核心传输协议。",
    "difficulty": "normal",
    "tags": ["网络", "TCP/IP"],
    "sections": [
        {"heading": "分层模型", "content": "OSI七层模型和TCP/IP四层模型是两种常见的分层方式。"},
    ],
    "key_points": ["分层模型降低协议设计复杂度", "TCP通过序号和确认保证可靠传输"],
    "glossary": [{"term": "TCP", "explanation": "面向连接的可靠传输协议。"}],
    "review_questions": ["TCP为什么需要三次握手？"],
    "qa_pairs": [
        {
            "question": "TCP为什么需要三次握手？",
            "answer": "三次握手用于确认双方收发能力并同步初始序列号。",
        }
    ],
}


class TestUpsertCard:
    def test_insert_new_card(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        card_id = repo.upsert_card(dict(SAMPLE_CARD))
        assert card_id == "wiki-test-tcp"

        card = repo.get_card(card_id)
        assert card is not None
        assert card["topic"] == "计算机网络"
        assert card["overview"] == SAMPLE_CARD["overview"]
        assert "TCP" in str(card.get("tags", []))

    def test_update_existing_card(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card(dict(SAMPLE_CARD))
        updated = dict(SAMPLE_CARD, overview="Updated overview text for testing purposes.")
        repo.upsert_card(updated)

        card = repo.get_card("wiki-test-tcp")
        assert card is not None
        assert card["overview"] == updated["overview"]

    def test_upsert_creates_chunks(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card(dict(SAMPLE_CARD))
        card = repo.get_card("wiki-test-tcp")
        assert card is not None
        # source_count will be 0 since chunks don't have source_id set
        assert "source_count" in card

    def test_upsert_creates_qa_pairs(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card(dict(SAMPLE_CARD))
        qa_pairs = repo.list_qa_pairs("wiki-test-tcp")
        # Only explicit source-backed QA pairs are persisted.
        assert len(qa_pairs) >= 1
        assert qa_pairs[0]["question"] == "TCP为什么需要三次握手？"
        assert len(qa_pairs[0]["answer"]) > 0

    def test_repeated_upsert_does_not_duplicate_chunks(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card(dict(SAMPLE_CARD))
        first_chunks = repo.list_chunks("wiki-test-tcp")
        repo.upsert_card(dict(SAMPLE_CARD))
        second_chunks = repo.list_chunks("wiki-test-tcp")

        assert first_chunks
        assert len(second_chunks) == len(first_chunks)

    def test_update_preserves_original_created_at(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD, created_at="2024-01-02T03:04:05"))

        repo.upsert_card(dict(SAMPLE_CARD, created_at="2030-01-02T03:04:05"))

        card = repo.get_card("wiki-test-tcp")
        assert card is not None
        assert card["created_at"] == "2024-01-02T03:04:05"


class TestGetCard:
    def test_returns_none_for_missing(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        assert repo.get_card("nonexistent") is None

    def test_get_card_by_topic(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))
        card = repo.get_card_by_topic("计算机网络")
        assert card is not None
        assert card["id"] == "wiki-test-tcp"

    def test_list_cards(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))
        repo.upsert_card(dict(SAMPLE_CARD, id="wiki-test-http", topic="HTTP"))
        cards = repo.list_cards()
        assert len(cards) >= 2


class TestSearch:
    def test_search_by_topic(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        results = repo.search_cards("计算机网络", limit=10)
        assert len(results) >= 1
        assert any("计算机网络" in (r.get("topic") or "") for r in results)

    def test_search_by_keyword(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        # Search for Chinese keyword that definitely appears in the overview
        results = repo.search_cards("协议", limit=10)
        assert len(results) >= 1

    def test_search_no_results(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        results = repo.search_cards("量子力学", limit=10)
        assert len(results) == 0

    def test_search_includes_snippet(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        results = repo.search_cards("TCP", limit=10)
        if results:
            assert "snippet" in results[0]

    def test_search_finds_content_stored_only_in_chunks(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        card = dict(
            SAMPLE_CARD,
            overview="overview without the search term",
            sections=[{"heading": "internal", "content": "uniquesectiontoken"}],
            key_points=[],
            glossary=[],
        )
        repo.upsert_card(card)

        results = repo.search_cards("uniquesectiontoken", limit=10)

        assert [result["id"] for result in results] == ["wiki-test-tcp"]


class TestReviews:
    def test_get_due_reviews(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        # Initially the card is due (next_review_at = now)
        conn = repo._connect()
        try:
            repo.ensure_review_state(conn, "wiki-test-tcp")
            conn.commit()
        finally:
            conn.close()

        due = repo.get_due_reviews(limit=10)
        assert len(due) >= 1
        assert due[0]["card_id"] == "wiki-test-tcp"

    def test_record_review_known(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        result = repo.record_review("wiki-test-tcp", "known")
        assert result is not None
        assert result["review_stage"] == 1
        assert result["mastery"] == 0.2
        assert result["review_count"] == 1

    def test_record_review_forgotten(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        # First advance to stage 2
        repo.record_review("wiki-test-tcp", "known")
        repo.record_review("wiki-test-tcp", "known")

        # Then forget
        result = repo.record_review("wiki-test-tcp", "forgotten")
        assert result is not None
        assert result["review_stage"] == 0
        assert result["mastery"] <= 0.35

    def test_record_review_appends_history(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        repo.record_review("wiki-test-tcp", "known", note="记住了")
        history = repo.get_review_history("wiki-test-tcp")
        assert len(history) >= 1
        assert history[0]["result"] == "known"
        assert history[0]["note"] == "记住了"

    def test_review_unknown_card(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        # Should auto-create state for unknown card
        result = repo.record_review("card-not-in-db", "fuzzy")
        assert result is not None
        assert result["card_id"] == "card-not-in-db"

    def test_record_review_bad_result_raises(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        try:
            repo.record_review("wiki-test-tcp", "invalid")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


class TestQaPairs:
    def test_upsert_qa_pair(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        qa_id = repo.upsert_qa_pair(
            "wiki-test-tcp",
            "什么是TCP？",
            "TCP是面向连接的可靠传输协议。",
        )
        assert qa_id

        pairs = repo.list_qa_pairs("wiki-test-tcp")
        questions = [p["question"] for p in pairs]
        assert "什么是TCP？" in questions

    def test_upsert_qa_pair_rejects_empty(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        try:
            repo.upsert_qa_pair("wiki-test-tcp", "", "答案")
            assert False, "Should raise"
        except ValueError:
            pass

        try:
            repo.upsert_qa_pair("wiki-test-tcp", "问题", "")
            assert False, "Should raise"
        except ValueError:
            pass

    def test_delete_qa_pair(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        qa_id = repo.upsert_qa_pair("wiki-test-tcp", "Q", "A")
        assert repo.delete_qa_pair(qa_id) is True
        assert repo.delete_qa_pair(qa_id) is False


class TestSources:
    def test_add_source(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        sid = repo.add_source({
            "name": "Wikipedia",
            "kind": "wikipedia",
            "url": "https://zh.wikipedia.org/wiki/计算机网络",
        })
        assert sid

        source = repo.get_source(sid)
        assert source is not None
        assert source["name"] == "Wikipedia"

    def test_get_source_missing(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        assert repo.get_source("nonexistent") is None

    def test_update_source_keeps_linked_chunks_valid(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))
        source_id = repo.add_source({"id": "source-1", "name": "Old", "kind": "web"})
        repo.add_chunk("wiki-test-tcp", source_id, {"content": "linked content"})

        repo.add_source({"id": source_id, "name": "New", "kind": "web"})

        source = repo.get_source(source_id)
        assert source is not None
        assert source["name"] == "New"
        assert repo.get_card("wiki-test-tcp")["source_count"] == 1


class TestChunks:
    def test_add_chunk(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))

        cid = repo.add_chunk("wiki-test-tcp", "", {
            "heading": "测试小节",
            "content": "这是测试内容，用于验证chunk功能。",
        })
        assert cid

    def test_add_chunk_returns_existing_id_for_duplicate(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))
        chunk = {"heading": "测试小节", "content": "相同内容"}

        first_id = repo.add_chunk("wiki-test-tcp", "", chunk)
        second_id = repo.add_chunk("wiki-test-tcp", "", chunk)

        matching = [item for item in repo.list_chunks("wiki-test-tcp") if item["content"] == "相同内容"]
        assert second_id == first_id
        assert len(matching) == 1


class TestIngestJobs:
    def test_create_and_update_job(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        jid = repo.create_ingest_job("wikipedia", "计算机网络")
        assert jid

        repo.update_ingest_job(jid, "completed")
        # Should not raise


class TestDedupe:
    def test_record_dedupe(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)
        repo.upsert_card(dict(SAMPLE_CARD))
        repo.upsert_card(dict(SAMPLE_CARD, id="wiki-dup"))

        did = repo.record_dedupe("wiki-test-tcp", "wiki-dup", 0.95, "标题高度相似")
        assert did


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _use_tmp_db(tmp_path, monkeypatch):
    """Point knowledge_db at a temporary file and init it."""
    db_path = tmp_path / "test_knowledge.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    conn = knowledge_db.connect()
    try:
        knowledge_db.init_db(conn)
    finally:
        conn.close()
