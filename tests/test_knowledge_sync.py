from __future__ import annotations

import hashlib
import sqlite3

from table_miku import knowledge_db
from table_miku.knowledge_sync import (
    canonical_topic,
    matched_key_points,
    parse_obsidian_note,
    preview_obsidian_sync,
    sync_obsidian_knowledge,
)


NOTE = """---
type: knowledge
topic: Java
status: active
tags:
  - Java
  - HashMap
---

# HashMap 原理与扩容

## 一句话理解

HashMap 通过数组、链表和红黑树保存键值对。

## put() 流程

- 计算扰动后的哈希值并定位桶。
- 桶内存在相同 key 时替换 value，否则插入新节点。

## 常见面试问法

1. "HashMap 的 put 流程是怎样的？"

## 易错点

- HashMap 在并发写入时并不安全。
"""


MULTI_TOPIC_DAILY_NOTE = """---
type: question
topic:
  - MySQL
  - Redis
difficulty: 中等
status: todo
---

# 大厂面试每日三题｜2026-07-21｜星期二｜MySQL + Redis

## 第一题：Redis big key / hot key 的发现与处理

### 题目

> Redis 中什么是 big key？什么是 hot key？如何发现它们？如何处理？

### 考察点

- Redis 大键和热键
- 生产运维

### 标准答案

big key 是值或元素数量过大的键，hot key 是访问频率过高的键。可用 SCAN、MEMORY USAGE 和客户端统计发现。

### 追问

**Q1: 删除 big key 时为什么优先使用 UNLINK？**

## 第二题：Redis Pub/Sub vs Stream 消息模型对比

### 题目

> Redis 的发布订阅和 Stream 有什么区别？什么场景选哪个？

### 标准答案

Pub/Sub 不持久化且没有 ACK；Stream 支持持久化、消费者组、ACK 和消息回溯。

## 第三题：MySQL 8.0 核心新特性

### 题目

> MySQL 8.0 引入了哪些关键新特性？请说明窗口函数、CTE、原子 DDL 和 Hash Join。

### 标准答案

MySQL 8.0 增加窗口函数、递归 CTE、原子 DDL 和无索引等值连接可用的 Hash Join。
"""


def _vault(tmp_path):
    root = tmp_path / "Obsidian Vault"
    target = root / "计算机知识" / "知识卡片" / "Java" / "HashMap.md"
    target.parent.mkdir(parents=True)
    target.write_text(NOTE, encoding="utf-8")
    return root, target


def _use_db(tmp_path, monkeypatch):
    db_path = tmp_path / "knowledge.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    return db_path


def test_parser_extracts_specific_interview_answer_without_writing(tmp_path):
    root, note = _vault(tmp_path)
    before = (hashlib.sha256(note.read_bytes()).hexdigest(), note.stat().st_mtime_ns)

    parsed = parse_obsidian_note(note, root)

    assert parsed is not None
    assert parsed.topic == "Java 后端基础"
    assert len(parsed.questions) == 1
    question = parsed.questions[0]
    assert question.question == "HashMap 的 put 流程是怎样的？"
    assert "哈希值" in question.answer
    assert "并发写入" in question.answer
    assert "工程示例" in question.answer
    after = (hashlib.sha256(note.read_bytes()).hexdigest(), note.stat().st_mtime_ns)
    assert after == before


def test_preview_is_read_only_and_filters_non_learning_notes(tmp_path):
    root, note = _vault(tmp_path)
    moc = root / "计算机知识" / "MOC.md"
    moc.write_text("---\ntype: moc\n---\n# 导航", encoding="utf-8")

    summary = preview_obsidian_sync(root)

    assert summary["files"] == 2
    assert summary["eligible"] == 1
    assert summary["questions"] == 1
    assert note.exists()


def test_incremental_sync_is_idempotent_and_archives_deleted_note(tmp_path, monkeypatch):
    root, note = _vault(tmp_path)
    db_path = _use_db(tmp_path, monkeypatch)

    first = sync_obsidian_knowledge(root)
    second = sync_obsidian_knowledge(root)

    assert first["created"] == 1
    assert first["questions"] == 1
    assert second["unchanged"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_documents WHERE status='active'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM knowledge_qa_pairs WHERE active=1").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] > 0

    note.unlink()
    deleted = sync_obsidian_knowledge(root)
    assert deleted["deleted"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_documents WHERE status='deleted'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM knowledge_qa_pairs WHERE active=1").fetchone()[0] == 0


def test_aliases_and_key_point_hints_are_deterministic():
    assert canonical_topic("数据库") == "数据库原理"
    assert canonical_topic("编译器") == "编译原理"
    points = ["HashMap 通过哈希定位桶", "并发写入需要 ConcurrentHashMap"]
    assert matched_key_points("我会先计算 HashMap 的哈希，然后定位桶。", points) == [points[0]]


def test_multi_question_note_excludes_question_without_matching_answer(tmp_path):
    root = tmp_path / "Obsidian Vault"
    note = root / "05-Interview" / "mixed.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: interview\ntopic: 计算机网络\n---\n"
        "# TCP 专题\n\n## 常见面试问法\n"
        "1. TCP 为什么需要三次握手？\n"
        "2. HashMap 为什么需要扩容？\n\n"
        "## TCP 握手原理\nTCP 三次握手确认双方收发能力并同步初始序列号。\n",
        encoding="utf-8",
    )

    parsed = parse_obsidian_note(note, root)

    assert parsed is not None
    assert [question.question for question in parsed.questions] == ["TCP 为什么需要三次握手？"]


def test_modified_and_invalid_notes_report_incremental_results(tmp_path, monkeypatch):
    root, note = _vault(tmp_path)
    db_path = _use_db(tmp_path, monkeypatch)
    original = (hashlib.sha256(note.read_bytes()).hexdigest(), note.stat().st_mtime_ns)

    sync_obsidian_knowledge(root)
    note.write_text(NOTE + "\n\n## 工程示例\n服务端通过抓包确认握手序列号。\n", encoding="utf-8")
    broken = root / "05-Interview" / "broken.md"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"---\ntype: interview\n---\n# \xff")

    result = sync_obsidian_knowledge(root)

    assert result["updated"] == 1
    assert len(result["errors"]) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_documents WHERE status='active'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM knowledge_qa_pairs WHERE active=1").fetchone()[0] == 1
        answer_detail, question_topic = conn.execute(
            "SELECT answer_detail, question_topic FROM knowledge_qa_pairs WHERE active=1"
        ).fetchone()
        assert "哈希值" in answer_detail
        assert question_topic == "Java 后端基础"
    assert (hashlib.sha256(note.read_bytes()).hexdigest(), note.stat().st_mtime_ns) != original


def test_sensitive_paths_and_non_markdown_files_are_skipped(tmp_path):
    root, _note = _vault(tmp_path)
    sensitive = root / "计算机知识" / "secret-token" / "credentials.md"
    sensitive.parent.mkdir(parents=True)
    sensitive.write_text(NOTE, encoding="utf-8")
    text_file = root / "计算机知识" / "知识卡片" / "plain.txt"
    text_file.write_text(NOTE, encoding="utf-8")

    preview = preview_obsidian_sync(root)

    assert preview["files"] == 1
    assert preview["eligible"] == 1


def test_daily_multi_topic_note_splits_real_questions_and_topics(tmp_path):
    root = tmp_path / "Obsidian Vault"
    note = root / "计算机知识" / "每日练习" / "MySQL-Redis" / "daily.md"
    note.parent.mkdir(parents=True)
    note.write_text(MULTI_TOPIC_DAILY_NOTE, encoding="utf-8")

    parsed = parse_obsidian_note(note, root)

    assert parsed is not None
    assert parsed.topic == "MySQL / Redis"
    assert [question.topic for question in parsed.questions] == ["Redis", "Redis", "MySQL"]
    assert [question.question for question in parsed.questions] == [
        "Redis 中什么是 big key？什么是 hot key？如何发现它们？如何处理？",
        "Redis 的发布订阅和 Stream 有什么区别？什么场景选哪个？",
        "MySQL 8.0 引入了哪些关键新特性？请说明窗口函数、CTE、原子 DDL 和 Hash Join。",
    ]
    assert all(parsed.title not in question.question for question in parsed.questions)
    assert "MEMORY USAGE" in parsed.questions[0].answer
    assert "消费者组" in parsed.questions[1].answer
    assert "窗口函数" in parsed.questions[2].answer


def test_multi_topic_sync_persists_question_topics(tmp_path, monkeypatch):
    root = tmp_path / "Obsidian Vault"
    note = root / "计算机知识" / "每日练习" / "MySQL-Redis" / "daily.md"
    note.parent.mkdir(parents=True)
    note.write_text(MULTI_TOPIC_DAILY_NOTE, encoding="utf-8")
    db_path = _use_db(tmp_path, monkeypatch)

    result = sync_obsidian_knowledge(root)

    assert result["questions"] == 3
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT question_topic, question FROM knowledge_qa_pairs WHERE active = 1 ORDER BY question"
        ).fetchall()
    assert sorted(topic for topic, _question in rows) == ["MySQL", "Redis", "Redis"]


def test_sync_archives_previous_card_when_parser_rekeys_note(tmp_path, monkeypatch):
    root, note = _vault(tmp_path)
    db_path = _use_db(tmp_path, monkeypatch)
    sync_obsidian_knowledge(root)

    note.write_text(MULTI_TOPIC_DAILY_NOTE, encoding="utf-8")
    result = sync_obsidian_knowledge(root)

    assert result["updated"] == 1
    with sqlite3.connect(db_path) as conn:
        cards = conn.execute(
            "SELECT title, archived FROM knowledge_cards ORDER BY title"
        ).fetchall()
        assert cards == [
            ("HashMap 原理与扩容", 1),
            ("大厂面试每日三题｜2026-07-21｜星期二｜MySQL + Redis", 0),
        ]
        active_card = conn.execute(
            "SELECT kc.title FROM knowledge_documents kd "
            "JOIN knowledge_cards kc ON kc.id = kd.card_id "
            "WHERE kd.status = 'active'"
        ).fetchone()
        assert active_card[0] == "大厂面试每日三题｜2026-07-21｜星期二｜MySQL + Redis"
