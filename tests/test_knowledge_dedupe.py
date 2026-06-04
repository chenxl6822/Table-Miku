"""Tests for deduplication — URL, hash, and title-based dedup."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_db, knowledge_repository as repo


class TestDedupeByUrl:
    def test_find_duplicates_by_url(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        # Insert two cards linked to the same source URL
        sid = repo.add_source({
            "name": "Wikipedia",
            "kind": "wikipedia",
            "url": "https://zh.wikipedia.org/wiki/计算机网络",
        })

        repo.upsert_card({
            "id": "card-a",
            "topic": "计算机网络",
            "title": "计算机网络",
            "overview": "计算机网络是连接多台计算机实现资源共享和数据通信的系统。它是现代信息技术基础设施的核心组成部分。",
        })
        repo.upsert_card({
            "id": "card-b",
            "topic": "计算机网络-V2",
            "title": "计算机网络",
            "overview": "计算机网络是连接多台计算机实现资源共享和数据通信的系统。它是现代信息技术基础设施的核心组成部分。",
        })

        repo.add_chunk("card-a", sid, {
            "heading": "概述",
            "content": "计算机网络是连接多台计算机实现资源共享和数据通信的系统。",
        })
        repo.add_chunk("card-b", sid, {
            "heading": "概述",
            "content": "计算机网络是连接多台计算机实现资源共享和数据通信的系统。",
        })

        dupes = repo.find_duplicates_by_url("https://zh.wikipedia.org/wiki/计算机网络")
        assert len(dupes) >= 2

    def test_no_duplicates_when_url_unique(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        sid = repo.add_source({
            "name": "Wikipedia",
            "kind": "wikipedia",
            "url": "https://unique.url/only-one",
        })
        repo.upsert_card({
            "id": "card-solo",
            "topic": "Solo",
            "title": "Solo",
            "overview": "This is a unique card that should not have duplicates based on URL matching.",
        })
        repo.add_chunk("card-solo", sid, {
            "heading": "Test",
            "content": "This is a unique card that should not have duplicates.",
        })

        dupes = repo.find_duplicates_by_url("https://unique.url/only-one")
        assert len(dupes) == 1


class TestDedupeByHash:
    def test_find_duplicates_by_hash(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        content = "完全相同的测试内容用于验证哈希去重功能是否正常工作。"
        content_hash = repo._hash_content(content)

        repo.upsert_card({
            "id": "card-x",
            "topic": "测试X",
            "title": "测试X",
            "overview": "这是一个测试卡片用于验证去重功能。",
        })
        repo.upsert_card({
            "id": "card-y",
            "topic": "测试Y",
            "title": "测试Y",
            "overview": "这是另一个测试卡片用于验证去重功能。",
        })

        repo.add_chunk("card-x", "", {"heading": "H", "content": content})
        repo.add_chunk("card-y", "", {"heading": "H", "content": content})

        dupes = repo.find_duplicates_by_hash(content_hash)
        assert len(dupes) >= 2

    def test_no_duplicates_with_different_content(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "card-p",
            "topic": "唯一卡片P",
            "title": "唯一卡片P",
            "overview": "这张卡片的内容是独一无二的，不应该与其他卡片产生哈希冲突。",
        })
        repo.add_chunk("card-p", "", {
            "heading": "H",
            "content": "这张卡片的内容是独一无二的，不应该与其他卡片产生哈希冲突。",
        })

        # Search with a hash that doesn't exist
        dupes = repo.find_duplicates_by_hash("deadbeef00000000")
        assert len(dupes) == 0


class TestDedupeLinks:
    def test_record_dedupe_link(self, tmp_path, monkeypatch):
        _use_tmp_db(tmp_path, monkeypatch)

        repo.upsert_card({
            "id": "winner",
            "topic": "胜者",
            "title": "胜者卡片",
            "overview": "这是胜者卡片，拥有更多的复习历史和完整的知识内容。",
        })
        repo.upsert_card({
            "id": "loser",
            "topic": "败者",
            "title": "败者卡片",
            "overview": "这是败者卡片，内容与胜者高度相似但数据较少。",
        })

        did = repo.record_dedupe("winner", "loser", 0.92, "标题和内容高度相似")
        assert did
        assert did.startswith("dd-")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _use_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_dedupe.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    conn = knowledge_db.connect()
    try:
        knowledge_db.init_db(conn)
    finally:
        conn.close()
