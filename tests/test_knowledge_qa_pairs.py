"""Tests for QA pairs — question-answer one-to-one correspondence, empty filtering, source tracking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_db, knowledge_repository as repo


class TestQaPairCorrespondence:
    """QA pairs must maintain one-to-one question-answer correspondence."""

    def test_upsert_creates_corresponding_pair(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-qa-1",
            "topic": "测试QA",
            "title": "测试QA",
            "overview": "这是测试QA对应的卡片。",
            "review_questions": [
                "什么是单元测试？",
                "TDD的核心流程是什么？",
            ],
            "qa_pairs": [
                {"question": "什么是单元测试？", "answer": "单元测试验证一个最小可测试单元的行为。"},
                {"question": "TDD的核心流程是什么？", "answer": "先写失败测试，再实现通过，最后重构。"},
            ],
        })

        pairs = repo.list_qa_pairs("card-qa-1")
        # Each question should have exactly one answer
        questions = [p["question"] for p in pairs]
        answers = [p["answer"] for p in pairs]

        assert "什么是单元测试？" in questions
        assert "TDD的核心流程是什么？" in questions
        assert all(len(a) > 0 for a in answers), "All answers must be non-empty"
        assert len(pairs) == len(set(questions)), "No duplicate questions"

    def test_question_answer_one_to_one(self, tmp_path, monkeypatch):
        """For every question there must be exactly one answer, and they must not be mismatched."""
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-qa-2",
            "topic": "计算机网络",
            "title": "计算机网络QA",
            "overview": "网络基础知识QA测试。",
            "review_questions": [
                "TCP为什么需要三次握手？",
                "DNS的作用是什么？",
                "HTTP和HTTPS的区别？",
            ],
            "qa_pairs": [
                {"question": "TCP为什么需要三次握手？", "answer": "确认双方收发能力并同步序列号。"},
                {"question": "DNS的作用是什么？", "answer": "把域名解析为可路由的地址。"},
                {"question": "HTTP和HTTPS的区别？", "answer": "HTTPS 在 HTTP 外增加 TLS 机密性和身份认证。"},
            ],
        })

        pairs = repo.list_qa_pairs("card-qa-2")
        assert len(pairs) == 3, f"Expected 3 pairs, got {len(pairs)}"

        # Verify each question has a non-empty answer
        for p in pairs:
            assert len(p["question"]) > 0, "Question must not be empty"
            assert len(p["answer"]) > 0, f"No answer for question: {p['question']}"
            assert p["card_id"] == "card-qa-2"

    def test_three_questions_produce_three_answers(self, tmp_path, monkeypatch):
        """If there are 3 questions, there must be 3 answers — no missing, no extra."""
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-qa-3",
            "topic": "数据结构",
            "title": "数据结构QA",
            "overview": "基础数据结构知识问答。",
            "review_questions": ["Q1?", "Q2?", "Q3?"],
            "qa_pairs": [
                {"question": "Q1?", "answer": "A1"},
                {"question": "Q2?", "answer": "A2"},
                {"question": "Q3?", "answer": "A3"},
            ],
        })

        pairs = repo.list_qa_pairs("card-qa-3")
        assert len(pairs) == 3


class TestEmptyAnswerFiltering:
    """Empty answers must not enter the database or UI."""

    def test_upsert_rejects_empty_answer(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-empty",
            "topic": "空答案测试",
            "title": "空答案测试",
            "overview": "用于测试空答案过滤。",
        })

        try:
            repo.upsert_qa_pair("card-empty", "问题", "")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_upsert_rejects_empty_question(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-empty-q",
            "topic": "空问题测试",
            "title": "空问题测试",
            "overview": "用于测试空问题过滤。",
        })

        try:
            repo.upsert_qa_pair("card-empty-q", "", "答案")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_list_qa_pairs_excludes_empty_answers(self, tmp_path, monkeypatch):
        """list_qa_pairs should only return pairs with non-empty answers."""
        _use_tmp_db(tmp_path, monkeypatch)

        # Insert a card without review_questions (so no auto-generated QA pairs)
        repo.upsert_card({
            "id": "card-filter",
            "topic": "过滤测试",
            "title": "过滤测试",
            "overview": "验证QA对列表不包含空答案的卡片。",
        })

        # All manually-added pairs via upsert_qa_pair are guaranteed non-empty
        repo.upsert_qa_pair("card-filter", "有效问题1", "有效答案1")
        repo.upsert_qa_pair("card-filter", "有效问题2", "有效答案2")

        pairs = repo.list_qa_pairs("card-filter")
        assert len(pairs) == 2
        for p in pairs:
            assert len(p["answer"]) > 0


class TestSourceChunkTracking:
    """QA pairs can optionally track which source chunk they came from."""

    def test_upsert_records_source_chunk_id(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-src",
            "topic": "来源追踪",
            "title": "来源追踪",
            "overview": "测试QA来源追踪功能是否正常工作。",
        })

        qa_id = repo.upsert_qa_pair(
            "card-src",
            "来源问题",
            "来源答案",
            source_chunk_id="chunk-abc123",
        )
        assert qa_id

        pairs = repo.list_qa_pairs("card-src")
        target = [p for p in pairs if p["question"] == "来源问题"][0]
        assert target["source_chunk_id"] == "chunk-abc123"

    def test_upsert_without_source_chunk(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-nochunk",
            "topic": "无来源",
            "title": "无来源",
            "overview": "测试不提供来源chunk的情况。",
        })

        qa_id = repo.upsert_qa_pair("card-nochunk", "问题", "答案")
        assert qa_id

        pairs = repo.list_qa_pairs("card-nochunk")
        target = [p for p in pairs if p["question"] == "问题"][0]
        assert target["source_chunk_id"] == ""


class TestQaPairUpdate:
    """Updating an existing QA pair preserves the question but updates the answer."""

    def test_update_existing_question(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-update",
            "topic": "更新测试",
            "title": "更新测试",
            "overview": "测试更新QA对的功能。",
        })

        first_id = repo.upsert_qa_pair("card-update", "同一个问题", "第一个答案")
        second_id = repo.upsert_qa_pair("card-update", "同一个问题", "更新后的答案")

        # Should be the same ID (updated, not duplicated)
        assert first_id == second_id

        pairs = repo.list_qa_pairs("card-update")
        questions_count = sum(1 for p in pairs if p["question"] == "同一个问题")
        assert questions_count == 1, "Should not create duplicate questions"
        target = [p for p in pairs if p["question"] == "同一个问题"][0]
        assert target["answer"] == "更新后的答案"


class TestQaPairsFromCardUpsert:
    """Only source-backed explicit QA pairs are persisted from cards."""

    def test_card_without_review_questions(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-noq",
            "topic": "无问题卡片",
            "title": "无问题卡片",
            "overview": "这张卡片没有预定义复习问题。",
        })

        pairs = repo.list_qa_pairs("card-noq")
        # No review_questions → no auto-generated QA pairs
        # (But could have manually added ones, which here there are none)
        assert len(pairs) == 0

    def test_card_qa_pairs_isolated_per_card(self, tmp_path, monkeypatch):
        """QA pairs for one card must not appear in another card's list."""
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-a-iso",
            "topic": "卡A",
            "title": "卡A",
            "overview": "卡片A的概述内容。",
            "review_questions": ["卡A的问题？"],
            "qa_pairs": [{"question": "卡A的问题？", "answer": "卡A的答案。"}],
        })
        repo.upsert_card({
            "id": "card-b-iso",
            "topic": "卡B",
            "title": "卡B",
            "overview": "卡片B的概述内容。",
            "review_questions": ["卡B的问题？"],
            "qa_pairs": [{"question": "卡B的问题？", "answer": "卡B的答案。"}],
        })

        pairs_a = repo.list_qa_pairs("card-a-iso")
        pairs_b = repo.list_qa_pairs("card-b-iso")

        questions_a = {p["question"] for p in pairs_a}
        questions_b = {p["question"] for p in pairs_b}

        assert "卡A的问题？" in questions_a
        assert "卡B的问题？" in questions_b
        assert "卡A的问题？" not in questions_b
        assert "卡B的问题？" not in questions_a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _use_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_qa.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    conn = knowledge_db.connect()
    try:
        knowledge_db.init_db(conn)
    finally:
        conn.close()
