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
            assert "knowledge_documents" in tables
            assert "knowledge_qa_sources" in tables
            assert "question_review_states" in tables
            assert "review_attempts" in tables
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

    def test_v3_migration_creates_recoverable_database_backup(self, tmp_path, monkeypatch):
        db_path = tmp_path / "knowledge.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        conn = knowledge_db.connect()
        knowledge_db.init_db(conn)
        conn.execute("DELETE FROM _schema_version WHERE version >= 3")
        conn.commit()
        conn.close()

        conn = knowledge_db.connect()
        try:
            knowledge_db.init_db(conn)
            backups = list(tmp_path.glob("knowledge.pre-v3-*.bak"))
            assert len(backups) == 1
            assert backups[0].stat().st_size > 0
            assert knowledge_db.get_schema_version(conn) == knowledge_db.CURRENT_SCHEMA_VERSION
        finally:
            conn.close()

    def test_v4_migration_adds_question_topic_and_backup(self, tmp_path, monkeypatch):
        db_path = tmp_path / "knowledge.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)

        conn = knowledge_db.connect()
        knowledge_db.init_db(conn)
        conn.execute("DROP INDEX idx_qa_question_topic")
        conn.execute("ALTER TABLE knowledge_qa_pairs DROP COLUMN question_topic")
        conn.execute("DELETE FROM _schema_version WHERE version = 4")
        conn.commit()
        conn.close()

        conn = knowledge_db.connect()
        try:
            knowledge_db.init_db(conn)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(knowledge_qa_pairs)")
            }
            backups = list(tmp_path.glob("knowledge.pre-v4-*.bak"))
            assert "question_topic" in columns
            assert len(backups) == 1
            assert backups[0].stat().st_size > 0
            assert knowledge_db.get_schema_version(conn) == knowledge_db.CURRENT_SCHEMA_VERSION
            with sqlite3.connect(backups[0]) as backup:
                backup_columns = {
                    row[1]
                    for row in backup.execute("PRAGMA table_info(knowledge_qa_pairs)")
                }
                assert "question_topic" not in backup_columns
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

    def test_v2_migration_deduplicates_related_rows(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test_v1.db"
        monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
        conn = knowledge_db.connect()
        try:
            for statement in knowledge_db._SCHEMA_STATEMENTS:
                conn.execute(statement)
            knowledge_db._ensure_schema_version_table(conn)
            conn.execute(
                "INSERT INTO _schema_version(version, applied_at) VALUES(1, '2026-01-01T00:00:00')"
            )
            conn.execute(
                """
                INSERT INTO knowledge_cards
                    (id, title, topic, normalized_topic, created_at, updated_at)
                VALUES ('card-1', 'Card', 'Topic', 'topic', '2026-01-01', '2026-01-01')
                """
            )
            for chunk_id in ("chunk-a", "chunk-b"):
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks
                        (id, card_id, heading, content, content_hash, created_at)
                    VALUES (?, 'card-1', 'H', 'same', 'hash-1', '2026-01-01')
                    """,
                    (chunk_id,),
                )
            for qa_id, chunk_id in (("qa-a", "chunk-a"), ("qa-b", "chunk-b")):
                conn.execute(
                    """
                    INSERT INTO knowledge_qa_pairs
                        (id, card_id, question, answer, source_chunk_id, created_at, updated_at)
                    VALUES (?, 'card-1', 'Q', 'A', ?, '2026-01-01', '2026-01-01')
                    """,
                    (qa_id, chunk_id),
                )

            knowledge_db.migrate(conn)

            assert knowledge_db.get_schema_version(conn) == knowledge_db.CURRENT_SCHEMA_VERSION
            assert conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM knowledge_qa_pairs").fetchone()[0] == 1
        finally:
            conn.close()
