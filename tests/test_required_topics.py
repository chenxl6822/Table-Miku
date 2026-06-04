"""Verify all 10 required computer science topics can enter the knowledge engine.

Covers:
- All 10 topics have knowledge_cards in the database.
- All 10 topics have fallback data (offline coverage).
- All 10 topics have review_states after initialization.
- New topics (软件工程, 算法设计与分析, 计算机安全, 分布式系统) are specifically covered.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_db, knowledge_repository as repo
from table_miku.knowledge_base import (
    DEFAULT_COMPUTER_TOPICS,
    FALLBACK_KNOWLEDGE,
    _fallback_card,
)

REQUIRED_TOPICS = DEFAULT_COMPUTER_TOPICS  # All 10 topics


class TestRequiredTopics:
    """All 10 topics must have fallback data and be insertable into SQLite."""

    def test_all_10_topics_have_fallback_data(self):
        """Every required topic must have an entry in FALLBACK_KNOWLEDGE."""
        for topic in REQUIRED_TOPICS:
            fb = FALLBACK_KNOWLEDGE.get(topic)
            assert fb is not None, f"Missing FALLBACK_KNOWLEDGE entry for: {topic}"
            assert len(fb["overview"]) >= 60, f"Overview too short for {topic}"
            assert len(fb["key_points"]) >= 3, f"Too few key_points for {topic}"
            assert len(fb["glossary"]) >= 3, f"Too few glossary entries for {topic}"
            assert len(fb["review_questions"]) >= 2, f"Too few review_questions for {topic}"
            assert len(fb["examples"]) >= 1, f"Missing examples for {topic}"

    def test_fallback_card_generates_for_all_topics(self):
        """_fallback_card must return valid cards for every topic."""
        for topic in REQUIRED_TOPICS:
            card = _fallback_card(topic)
            assert card["offline"] is True
            assert card["topic"] == topic
            assert len(card["overview"]) >= 30
            assert len(card["sections"]) >= 2
            assert len(card["key_points"]) >= 3
            assert len(card["review_questions"]) >= 2

    def test_all_topics_insertable_into_db(self, tmp_path, monkeypatch):
        """Every required topic can be upserted into SQLite."""
        _use_tmp_db(tmp_path, monkeypatch)

        for topic in REQUIRED_TOPICS:
            card = _fallback_card(topic)
            card_id = repo.upsert_card(card)
            assert card_id, f"Failed to upsert card for {topic}"

        # Verify count
        cards = repo.list_cards(limit=20)
        assert len(cards) >= 10, f"Expected >=10 cards, got {len(cards)}"

    def test_all_topics_have_review_states(self, tmp_path, monkeypatch):
        """After upserting cards, ensure_review_state covers all 10."""
        _use_tmp_db(tmp_path, monkeypatch)

        conn = repo._connect()
        try:
            for topic in REQUIRED_TOPICS:
                card = _fallback_card(topic)
                repo.upsert_card(card)

            for topic in REQUIRED_TOPICS:
                card = repo.get_card_by_topic(topic)
                assert card is not None, f"Card not found for {topic}"
                state = repo.ensure_review_state(conn, card["id"])
                assert state["card_id"] == card["id"]
                assert "mastery" in state

            conn.commit()
        finally:
            conn.close()

    def test_new_topics_have_complete_fallback(self):
        """Specifically verify the 4 newly-added topics have rich fallback data."""
        new_topics = ["软件工程", "算法设计与分析", "计算机安全", "分布式系统"]
        for topic in new_topics:
            fb = FALLBACK_KNOWLEDGE.get(topic)
            assert fb is not None, f"Missing fallback for new topic: {topic}"
            assert len(fb["overview"]) >= 80, f"Overview too short for new topic {topic}"
            assert len(fb["key_points"]) >= 4, f"Too few key_points for new topic {topic}"
            assert len(fb["glossary"]) >= 4, f"Too few glossary for new topic {topic}"
            assert len(fb["review_questions"]) >= 2
            assert len(fb["examples"]) >= 1

    def test_new_topics_searchable(self, tmp_path, monkeypatch):
        """New topics must be searchable by topic name and keywords."""
        _use_tmp_db(tmp_path, monkeypatch)

        for topic in REQUIRED_TOPICS:
            repo.upsert_card(_fallback_card(topic))

        # Search by topic name
        for topic in ["软件工程", "算法设计与分析", "计算机安全", "分布式系统"]:
            results = repo.search_cards(topic, limit=5)
            assert len(results) >= 1, f"Topic '{topic}' not found in search"

        # Search by keywords that appear in overview text
        keyword_checks = [
            ("需求分析", "软件工程"),
            ("动态规划", "算法设计与分析"),
            ("密码学", "计算机安全"),
            ("一致性", "分布式系统"),
        ]
        for keyword, topic in keyword_checks:
            results = repo.search_cards(keyword, limit=10)
            found = any(r.get("topic") == topic for r in results)
            assert found, f"Keyword '{keyword}' should find topic '{topic}'"

    def test_due_reviews_include_new_topics(self, tmp_path, monkeypatch):
        """After initialization, the new topics should appear in due reviews."""
        _use_tmp_db(tmp_path, monkeypatch)

        # Upsert all cards first (each call opens/closes its own connection)
        card_ids: dict[str, str] = {}
        for topic in REQUIRED_TOPICS:
            card = _fallback_card(topic)
            card_id = repo.upsert_card(card)
            card_ids[topic] = card_id

        # Then ensure review states in a single connection
        conn = repo._connect()
        try:
            for topic, card_id in card_ids.items():
                repo.ensure_review_state(conn, card_id)
            conn.commit()
        finally:
            conn.close()

        due = repo.get_due_reviews(limit=20)
        due_topic_names: set[str] = set()
        for item in due:
            card = repo.get_card(item["card_id"])
            if card:
                due_topic_names.add(card.get("topic", "unknown"))

        # All topics should be immediately due (next_review_at = now)
        for topic in REQUIRED_TOPICS:
            assert topic in due_topic_names, f"Topic '{topic}' not in due reviews"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _use_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_required.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    conn = knowledge_db.connect()
    try:
        knowledge_db.init_db(conn)
    finally:
        conn.close()
