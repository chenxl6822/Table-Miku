from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from table_miku import knowledge_db, knowledge_repository as repo
from table_miku.app import MistakeBookDialog, ReviewDialog


def _app():
    return QApplication.instance() or QApplication([])


def _question(tmp_path, monkeypatch):
    db_path = tmp_path / "ui-review.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    knowledge_db.init_db()
    repo.upsert_card({"id": "card-ui", "topic": "Java 后端基础", "title": "Spring", "overview": "Spring"})
    qa_id = repo.upsert_structured_qa(
        "card-ui",
        {
            "question": "Spring IoC 解决了什么问题？",
            "answer": (
                "一句话结论：IoC 把对象创建和依赖装配交给容器。\n\n"
                "原理拆解：容器负责 Bean 创建、依赖注入和生命周期。\n\n"
                "工程示例：业务服务通过构造器接收仓储接口，测试时可替换实现。\n\n"
                "来源：计算机知识/Spring.md"
            ),
            "answer_summary": "对象控制权交给容器。",
            "answer_detail": "容器负责 Bean 创建、依赖注入和生命周期。",
            "key_points": ["依赖注入", "降低耦合"],
            "pitfalls": ["IoC 不等于容器本身"],
            "follow_ups": ["Bean 生命周期有哪些阶段？"],
            "canonical_key": "ui-spring-ioc",
            "source_label": "计算机知识/Spring.md",
        },
    )
    return qa_id, repo.list_questions_for_card("card-ui")


def test_review_hides_answer_until_submission_and_persists_attempt(tmp_path, monkeypatch):
    _app()
    qa_id, items = _question(tmp_path, monkeypatch)
    dialog = ReviewDialog(items)

    assert "对象控制权交给容器" not in dialog._question_area.toPlainText()
    assert dialog._known_btn.isEnabled() is False

    dialog._answer_editor.setPlainText("通过依赖注入降低业务代码耦合")
    dialog._reveal_answer()

    assert "对象控制权交给容器" in dialog._question_area.toPlainText()
    assert "业务服务通过构造器接收仓储接口" in dialog._question_area.toPlainText()
    assert dialog._known_btn.isEnabled() is True
    assert "命中" in dialog._coverage_label.text()

    dialog._grade("forgotten")
    conn = knowledge_db.connect()
    try:
        attempt = conn.execute(
            "SELECT user_answer, result FROM review_attempts WHERE qa_id = ?", (qa_id,)
        ).fetchone()
        assert attempt == ("通过依赖注入降低业务代码耦合", "forgotten")
    finally:
        conn.close()


def test_mistake_book_shows_previous_answer_and_missing_points(tmp_path, monkeypatch):
    _app()
    qa_id, _items = _question(tmp_path, monkeypatch)
    repo.record_question_attempt(qa_id, "forgotten", "只知道容器", [])
    mistakes = repo.list_mistake_questions()

    dialog = MistakeBookDialog(mistakes)

    text = dialog._detail.toPlainText()
    assert "只知道容器" in text
    assert "依赖注入" in text
    assert "连续答对：0/2" in text
