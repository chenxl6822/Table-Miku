"""Tests for knowledge_db — schema init, idempotent init, FTS5 detection."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_db


class TestInitDb:
    def test_init_creates_all_tables(self, tmp_path, monkeypatch):
        """init_db on a fresh database creates all expected tables."""
        db_path = tmp_path / "test_knowledge.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        conn = knowledge_db.connect()
        try:
            knowledge_db.init_db(conn)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "knowledge_cards" in tables
            assert "knowledge_sources" in tables
            assert "knowledge_chunks" in tables
            assert "review_states" in tables
            assert "review_history" in tables
            assert "knowledge_qa_pairs" in tables
            assert "ingest_jobs" in tables
            assert "dedupe_links" in tables
            assert "_schema_version" in tables
        finally:
            conn.close()

    def test_init_is_idempotent(self, tmp_path, monkeypatch):
        """Calling init_db twice should not raise."""
        db_path = tmp_path / "test_knowledge.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        conn = knowledge_db.connect()
        try:
            knowledge_db.init_db(conn)
            knowledge_db.init_db(conn)  # second call
            cnt = conn.execute(
                "SELECT COUNT(*) FROM knowledge_cards"
            ).fetchone()[0]
            assert cnt == 0
        finally:
            conn.close()

    def test_foreign_keys_enabled(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test_fk.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        conn = knowledge_db.connect()
        try:
            knowledge_db.init_db(conn)
            fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert fk_on == 1
        finally:
            conn.close()

    def test_schema_version_starts_at_current(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test_ver.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        conn = knowledge_db.connect()
        try:
            knowledge_db.init_db(conn)
            ver = knowledge_db.get_schema_version(conn)
            assert ver == knowledge_db.CURRENT_SCHEMA_VERSION
        finally:
            conn.close()

    def test_get_schema_version_zero_on_empty(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test_empty.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("CREATE TABLE foo (x INTEGER)")
            ver = knowledge_db.get_schema_version(conn)
            assert ver == 0
        finally:
            conn.close()

    def test_fts5_detection(self, tmp_path, monkeypatch):
        """_check_fts5 returns a bool and does not raise."""
        db_path = tmp_path / "test_fts5.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        conn = knowledge_db.connect()
        try:
            result = knowledge_db._check_fts5(conn)
            assert isinstance(result, bool)
        finally:
            conn.close()

    def test_knowledge_db_path_in_user_data_dir(self, monkeypatch):
        """Path lives under user_data_dir."""
        from table_miku.paths import user_data_dir

        path = knowledge_db.knowledge_db_path()
        assert path.parent == user_data_dir()
        assert path.name == "knowledge.db"
