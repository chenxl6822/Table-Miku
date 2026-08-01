"""Tests for knowledge_migration — JSON cards and review states → SQLite."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_db, knowledge_migration


class TestMigrateJsonToSqlite:
    def test_migration_creates_cards(self, tmp_path, monkeypatch):
        """Migration reads knowledge_base.json and inserts cards."""
        db_path = tmp_path / "test_migrate.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        # Prepare mock JSON
        cards_json = [
            {
                "id": "wiki-%E8%AE%A1%E7%AE%97%E6%9C%BA%E7%BD%91%E7%BB%9C",
                "topic": "计算机网络",
                "title": "计算机网络",
                "overview": "计算机网络研究计算机与网络设备如何通信。",
                "source": "https://zh.wikipedia.org/wiki/计算机网络",
                "source_name": "Wikipedia",
                "source_url": "https://zh.wikipedia.org/wiki/计算机网络",
                "offline": False,
                "sections": [{"heading": "分层模型", "content": "OSI七层和TCP/IP四层。"}],
                "key_points": ["分层模型降低协议设计复杂度", "TCP可靠传输"],
                "glossary": [{"term": "TCP", "explanation": "面向连接协议"}],
                "review_questions": ["TCP为什么需要三次握手？"],
                "examples": ["浏览器访问网站"],
                "fetched_at": "2026-06-01T12:00:00",
                "updated_at": "2026-06-01T12:00:00",
                "encoding_status": "ok",
            },
            {
                "id": "wiki-%E6%95%B0%E6%8D%AE%E7%BB%93%E6%9E%84",
                "topic": "数据结构",
                "title": "数据结构",
                "overview": "数据结构研究数据组织方式及其效率。",
                "offline": True,
                "source_name": "offline",
                "sections": [],
                "key_points": ["数组随机访问快"],
                "glossary": [{"term": "栈", "explanation": "后进先出"}],
                "review_questions": ["数组和链表的取舍？"],
            },
        ]
        reviews_json = [
            {
                "card_id": "wiki-%E8%AE%A1%E7%AE%97%E6%9C%BA%E7%BD%91%E7%BB%9C",
                "mastery": 0.5,
                "review_stage": 2,
                "next_review_at": (datetime.now() + timedelta(days=1)).isoformat(),
                "last_reviewed_at": datetime.now().isoformat(),
                "review_count": 3,
                "updated_at": datetime.now().isoformat(),
                "history": [
                    {"at": "2026-06-01T10:00:00", "result": "known", "note": ""},
                    {"at": "2026-06-02T10:00:00", "result": "fuzzy", "note": "不太确定"},
                ],
            },
        ]

        # Mock read-only legacy JSON loader
        def mock_read_json(filename):
            if "knowledge_base.json" in filename:
                return cards_json
            if "knowledge_reviews.json" in filename:
                return reviews_json
            return []

        monkeypatch.setattr(knowledge_migration, "_read_legacy_json", mock_read_json)

        result = knowledge_migration.migrate_json_to_sqlite(force=True)
        assert result["cards"] >= 1
        assert result["review_states"] >= 1
        assert result["review_history"] >= 1
        assert result["skipped"] == 0

        # Verify cards in DB
        conn = knowledge_db.connect()
        try:
            knowledge_db.init_db(conn)
            cnt = conn.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()[0]
            assert cnt >= 2

            cnt_rs = conn.execute("SELECT COUNT(*) FROM review_states").fetchone()[0]
            assert cnt_rs >= 1

            cnt_rh = conn.execute("SELECT COUNT(*) FROM review_history").fetchone()[0]
            assert cnt_rh >= 2
        finally:
            conn.close()

    def test_migration_skips_after_completed_marker(self, tmp_path, monkeypatch):
        """An explicit marker, rather than unrelated cards, makes migration idempotent."""
        db_path = tmp_path / "test_skip.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        # Pre-populate
        conn = knowledge_db.connect()
        knowledge_db.init_db(conn)
        conn.execute(
            "INSERT INTO knowledge_cards (id, title, topic, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("existing", "Existing", "existing", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
        conn.close()

        # Mock empty JSON
        monkeypatch.setattr(knowledge_migration, "_read_legacy_json", lambda _filename: [])

        first = knowledge_migration.migrate_json_to_sqlite(force=False)
        second = knowledge_migration.migrate_json_to_sqlite(force=False)
        assert first["skipped"] == 0
        assert second["cards"] == 0
        assert second["skipped"] >= 1

    def test_migration_force_reimports(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test_force.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        # Pre-populate
        conn = knowledge_db.connect()
        knowledge_db.init_db(conn)
        conn.execute(
            "INSERT INTO knowledge_cards (id, title, topic, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("existing", "Existing", "existing", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
        conn.close()

        cards = [{"id": "new-card", "topic": "NewTopic", "title": "NewTopic", "overview": "Content here that is long enough for testing purposes and validation checks."}]
        monkeypatch.setattr(
            knowledge_migration,
            "_read_legacy_json",
            lambda filename: cards if "base" in str(filename) else [],
        )

        result = knowledge_migration.migrate_json_to_sqlite(force=True)
        assert result["cards"] >= 1

    def test_invalid_legacy_json_is_never_modified(self, tmp_path, monkeypatch):
        source = tmp_path / "knowledge_base.json"
        source.write_bytes(b"{invalid-json")
        monkeypatch.setattr(knowledge_migration, "runtime_path", lambda _filename: source)
        before = (source.read_bytes(), source.stat().st_mtime_ns)

        assert knowledge_migration._read_legacy_json("knowledge_base.json") == []
        assert (source.read_bytes(), source.stat().st_mtime_ns) == before
