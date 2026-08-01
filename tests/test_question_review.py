from __future__ import annotations

from datetime import datetime

from table_miku import knowledge_db, knowledge_repository as repo


def _use_db(tmp_path, monkeypatch):
    db_path = tmp_path / "question-review.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    knowledge_db.init_db()
    repo.upsert_card({"id": "java-card", "topic": "Java 后端基础", "title": "Java", "overview": "Java"})
    return repo.upsert_structured_qa(
        "java-card",
        {
            "question": "Spring IoC 解决了什么问题？",
            "answer": "IoC 把对象创建和依赖装配交给容器，从而降低业务代码耦合。",
            "answer_summary": "IoC 将对象控制权交给容器。",
            "answer_detail": "容器负责创建 Bean、解析依赖和管理生命周期。",
            "key_points": ["依赖注入", "降低耦合", "Bean 生命周期"],
            "pitfalls": ["IoC 不等于依赖注入本身"],
            "follow_ups": ["Bean 的作用域有哪些？"],
            "canonical_key": "spring-ioc",
        },
    )


def test_question_review_enters_and_exits_mistake_book_after_two_correct(tmp_path, monkeypatch):
    qa_id = _use_db(tmp_path, monkeypatch)
    now = datetime(2026, 8, 1, 10, 0, 0)

    forgotten = repo.record_question_attempt(qa_id, "forgotten", "不知道", [], now)
    assert forgotten["in_mistake_book"] is True
    assert forgotten["wrong_count"] == 1
    assert len(repo.list_mistake_questions()) == 1

    first_known = repo.record_question_attempt(
        qa_id, "known", "通过依赖注入降低耦合", ["依赖注入", "降低耦合"], now
    )
    assert first_known["correct_streak"] == 1
    assert first_known["in_mistake_book"] is True

    second_known = repo.record_question_attempt(
        qa_id, "known", "容器管理 Bean 和依赖注入", ["依赖注入", "Bean 生命周期"], now
    )
    assert second_known["correct_streak"] == 2
    assert second_known["in_mistake_book"] is False
    assert repo.list_mistake_questions() == []


def test_due_question_contains_structured_answer_and_attempt(tmp_path, monkeypatch):
    qa_id = _use_db(tmp_path, monkeypatch)

    due = repo.list_due_questions(datetime(2030, 1, 1), limit=5)
    assert len(due) == 1
    assert due[0]["id"] == qa_id
    assert due[0]["key_points"] == ["依赖注入", "降低耦合", "Bean 生命周期"]

    repo.record_question_attempt(qa_id, "fuzzy", "容器创建对象", ["依赖注入"])
    conn = knowledge_db.connect()
    try:
        row = conn.execute("SELECT user_answer, result, matched_points FROM review_attempts").fetchone()
        assert row[0] == "容器创建对象"
        assert row[1] == "fuzzy"
        assert "依赖注入" in row[2]
    finally:
        conn.close()
