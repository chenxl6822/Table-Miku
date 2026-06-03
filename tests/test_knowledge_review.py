"""Tests for knowledge_review service — no Qt, no network."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_review
from table_miku.review_scheduler import default_review_state

SAMPLE_CARDS = [
    {
        "id": "wiki-1",
        "topic": "计算机网络",
        "title": "计算机网络",
        "overview": "计算机网络研究计算机与网络设备如何通过通信链路交换数据。",
        "summary": "计算机网络研究计算机与网络设备如何通过通信链路交换数据。",
        "key_points": ["分层模型降低协议设计复杂度"],
        "glossary": [{"term": "TCP", "explanation": "面向连接的可靠传输协议。"}],
        "review_questions": ["TCP 为什么需要三次握手？"],
        "sections": [{"heading": "分层模型", "content": "OSI 七层和 TCP/IP 四层。"}],
        "examples": ["浏览器访问网站经历 DNS 解析。"],
        "source_url": "",
        "source_name": "offline",
        "offline": True,
        "source": "offline",
        "fetched_at": "",
        "updated_at": "",
        "encoding_status": "ok",
    },
    {
        "id": "wiki-2",
        "topic": "数据结构",
        "title": "数据结构",
        "overview": "数据结构研究数据的组织方式及其操作效率。",
        "summary": "数据结构研究数据的组织方式及其操作效率。",
        "key_points": ["数组随机访问快"],
        "glossary": [{"term": "栈", "explanation": "后进先出。"}],
        "review_questions": ["数组和链表的核心取舍是什么？"],
        "sections": [{"heading": "线性结构", "content": "数组适合随机访问。"}],
        "examples": ["括号匹配用栈。"],
        "source_url": "",
        "source_name": "offline",
        "offline": True,
        "source": "offline",
        "fetched_at": "",
        "updated_at": "",
        "encoding_status": "ok",
    },
    {
        "id": "wiki-3",
        "topic": "操作系统",
        "title": "操作系统",
        "overview": "操作系统管理硬件资源并为应用提供抽象接口。",
        "summary": "操作系统管理硬件资源并为应用提供抽象接口。",
        "key_points": ["进程隔离提升安全性"],
        "glossary": [{"term": "进程", "explanation": "拥有独立资源的程序运行实例。"}],
        "review_questions": ["进程和线程有什么区别？"],
        "sections": [{"heading": "进程与线程", "content": "进程是资源分配单位。"}],
        "examples": ["多线程需锁保护。"],
        "source_url": "",
        "source_name": "offline",
        "offline": True,
        "source": "offline",
        "fetched_at": "",
        "updated_at": "",
        "encoding_status": "ok",
    },
]


class TestEnsureReviewStates:
    def test_initializes_new_cards(self, monkeypatch):
        monkeypatch.setattr(knowledge_review, "load_knowledge", lambda: SAMPLE_CARDS)
        monkeypatch.setattr(knowledge_review, "load_review_states", lambda: [])
        written = []
        monkeypatch.setattr(knowledge_review, "save_review_states", lambda s: written.append(list(s)))

        states = knowledge_review.ensure_review_states(SAMPLE_CARDS)
        assert len(states) == 3
        assert all(s["card_id"] in ("wiki-1", "wiki-2", "wiki-3") for s in states)
        assert all(s["mastery"] == 0.0 for s in states)
        assert len(written) == 1

    def test_preserves_existing_states(self, monkeypatch):
        existing = [default_review_state("wiki-1")]
        existing[0]["mastery"] = 0.5
        monkeypatch.setattr(knowledge_review, "load_knowledge", lambda: SAMPLE_CARDS)
        monkeypatch.setattr(knowledge_review, "load_review_states", lambda: list(existing))
        monkeypatch.setattr(knowledge_review, "save_review_states", lambda s: None)

        states = knowledge_review.ensure_review_states(SAMPLE_CARDS)
        target = [s for s in states if s["card_id"] == "wiki-1"][0]
        assert target["mastery"] == 0.5

    def test_skips_cards_without_id(self, monkeypatch):
        cards_no_id = [{"topic": "无ID卡片", "title": "测试"}]
        monkeypatch.setattr(knowledge_review, "load_knowledge", lambda: cards_no_id)
        monkeypatch.setattr(knowledge_review, "load_review_states", lambda: [])
        monkeypatch.setattr(knowledge_review, "save_review_states", lambda s: None)

        states = knowledge_review.ensure_review_states(cards_no_id)
        assert len(states) == 0


class TestDueReviewItems:
    def test_returns_due_items(self, monkeypatch):
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        state = default_review_state("wiki-1")
        state["next_review_at"] = past

        monkeypatch.setattr(knowledge_review, "load_knowledge", lambda: SAMPLE_CARDS)
        monkeypatch.setattr(knowledge_review, "ensure_review_states", lambda cards=None: [state])

        items = knowledge_review.due_review_items()
        assert len(items) == 1
        assert items[0]["card"]["id"] == "wiki-1"
        assert items[0]["state"]["card_id"] == "wiki-1"

    def test_excludes_future_items(self, monkeypatch):
        future = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
        state = default_review_state("wiki-1")
        state["next_review_at"] = future

        monkeypatch.setattr(knowledge_review, "load_knowledge", lambda: SAMPLE_CARDS)
        monkeypatch.setattr(knowledge_review, "ensure_review_states", lambda cards=None: [state])

        items = knowledge_review.due_review_items()
        assert len(items) == 0

    def test_respects_limit(self, monkeypatch):
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        states = []
        for card in SAMPLE_CARDS:
            s = default_review_state(card["id"])
            s["next_review_at"] = past
            states.append(s)

        monkeypatch.setattr(knowledge_review, "load_knowledge", lambda: SAMPLE_CARDS)
        monkeypatch.setattr(knowledge_review, "ensure_review_states", lambda cards=None: states)

        items = knowledge_review.due_review_items(limit=2)
        assert len(items) == 2


class TestRecordReview:
    def test_updates_existing_state(self, monkeypatch):
        state = default_review_state("wiki-1")
        monkeypatch.setattr(knowledge_review, "load_review_states", lambda: [state])
        saved = []
        monkeypatch.setattr(knowledge_review, "save_review_states", lambda s: saved.append(list(s)))

        updated = knowledge_review.record_review("wiki-1", "known")
        assert updated is not None
        assert updated["review_stage"] == 1
        assert updated["mastery"] == 0.2
        assert updated["review_count"] == 1
        assert len(saved) == 1

    def test_unknown_card_id_creates_new_state(self, monkeypatch):
        monkeypatch.setattr(knowledge_review, "load_review_states", lambda: [])
        saved = []
        monkeypatch.setattr(knowledge_review, "save_review_states", lambda s: saved.append(list(s)))

        updated = knowledge_review.record_review("wiki-new", "known")
        assert updated is not None
        assert updated["card_id"] == "wiki-new"
        assert updated["review_stage"] == 1
        assert len(saved) == 1

    def test_record_with_note(self, monkeypatch):
        state = default_review_state("wiki-1")
        monkeypatch.setattr(knowledge_review, "load_review_states", lambda: [state])
        monkeypatch.setattr(knowledge_review, "save_review_states", lambda s: None)

        updated = knowledge_review.record_review("wiki-1", "fuzzy", note="还需加强")
        assert len(updated["history"]) == 1
        assert updated["history"][0]["note"] == "还需加强"


class TestReviewSummary:
    def test_no_due_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(knowledge_review, "due_review_items", lambda now=None, limit=20: [])
        assert knowledge_review.review_summary() == ""

    def test_with_due_returns_summary(self, monkeypatch):
        item = {"card": SAMPLE_CARDS[0], "state": default_review_state("wiki-1")}
        monkeypatch.setattr(knowledge_review, "due_review_items", lambda now=None, limit=20: [item])
        summary = knowledge_review.review_summary()
        assert "计算机网络" in summary
        assert "待复习" in summary
        assert "1" in summary

    def test_with_multiple_due(self, monkeypatch):
        items = [
            {"card": SAMPLE_CARDS[0], "state": default_review_state("wiki-1")},
            {"card": SAMPLE_CARDS[1], "state": default_review_state("wiki-2")},
        ]
        monkeypatch.setattr(knowledge_review, "due_review_items", lambda now=None, limit=20: items)
        summary = knowledge_review.review_summary()
        assert "计算机网络" in summary
        assert "数据结构" in summary
        assert "2" in summary
