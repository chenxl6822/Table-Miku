"""SQLite knowledge database — connection, schema, FTS5, and migration versioning.

All schema DDL lives here.  Import-time side effects are avoided — callers must
explicitly call ``init_db()`` or ``connect()``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .paths import runtime_path

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------
CURRENT_SCHEMA_VERSION = 3

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS: list[str] = [
    # ── knowledge_cards ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS knowledge_cards (
        id              TEXT PRIMARY KEY,
        title           TEXT NOT NULL,
        topic           TEXT NOT NULL,
        normalized_topic TEXT NOT NULL DEFAULT '',
        overview        TEXT NOT NULL DEFAULT '',
        difficulty      TEXT NOT NULL DEFAULT 'normal',
        tags            TEXT NOT NULL DEFAULT '[]',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        archived        INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ── knowledge_sources ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS knowledge_sources (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        kind          TEXT NOT NULL,
        url           TEXT NOT NULL DEFAULT '',
        license_note  TEXT NOT NULL DEFAULT '',
        fetched_at    TEXT,
        status        TEXT NOT NULL DEFAULT 'active'
    )
    """,
    # ── knowledge_chunks ───────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id            TEXT PRIMARY KEY,
        card_id       TEXT NOT NULL,
        source_id     TEXT,
        heading       TEXT NOT NULL DEFAULT '',
        content       TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        quality_score REAL NOT NULL DEFAULT 0.5,
        created_at    TEXT NOT NULL,
        FOREIGN KEY(card_id)   REFERENCES knowledge_cards(id),
        FOREIGN KEY(source_id) REFERENCES knowledge_sources(id)
    )
    """,
    # ── review_states ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS review_states (
        card_id          TEXT PRIMARY KEY,
        mastery          REAL    NOT NULL DEFAULT 0,
        review_stage     INTEGER NOT NULL DEFAULT 0,
        next_review_at   TEXT,
        last_reviewed_at TEXT,
        review_count     INTEGER NOT NULL DEFAULT 0,
        updated_at       TEXT    NOT NULL,
        FOREIGN KEY(card_id) REFERENCES knowledge_cards(id)
    )
    """,
    # ── review_history ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS review_history (
        id           TEXT PRIMARY KEY,
        card_id      TEXT NOT NULL,
        reviewed_at  TEXT NOT NULL,
        result       TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT '',
        mastery_after REAL NOT NULL,
        stage_after  INTEGER NOT NULL,
        FOREIGN KEY(card_id) REFERENCES knowledge_cards(id)
    )
    """,
    # ── knowledge_qa_pairs ─────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS knowledge_qa_pairs (
        id              TEXT PRIMARY KEY,
        card_id         TEXT NOT NULL,
        question        TEXT NOT NULL,
        answer          TEXT NOT NULL,
        source_chunk_id TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        FOREIGN KEY(card_id) REFERENCES knowledge_cards(id)
    )
    """,
    # ── source document fingerprints ──────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS knowledge_documents (
        id              TEXT PRIMARY KEY,
        source_id       TEXT,
        card_id         TEXT,
        vault_root      TEXT NOT NULL,
        relative_path   TEXT NOT NULL,
        content_hash    TEXT NOT NULL,
        mtime_ns        INTEGER NOT NULL DEFAULT 0,
        size            INTEGER NOT NULL DEFAULT 0,
        note_type       TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'active',
        last_indexed_at TEXT NOT NULL,
        error           TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(source_id) REFERENCES knowledge_sources(id),
        FOREIGN KEY(card_id) REFERENCES knowledge_cards(id),
        UNIQUE(vault_root, relative_path)
    )
    """,
    # ── question-level spaced repetition ──────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS question_review_states (
        qa_id               TEXT PRIMARY KEY,
        mastery             REAL NOT NULL DEFAULT 0,
        review_stage         INTEGER NOT NULL DEFAULT 0,
        next_review_at       TEXT,
        last_reviewed_at     TEXT,
        review_count         INTEGER NOT NULL DEFAULT 0,
        correct_streak       INTEGER NOT NULL DEFAULT 0,
        wrong_count          INTEGER NOT NULL DEFAULT 0,
        in_mistake_book      INTEGER NOT NULL DEFAULT 0,
        last_user_answer     TEXT NOT NULL DEFAULT '',
        last_matched_points  TEXT NOT NULL DEFAULT '[]',
        updated_at           TEXT NOT NULL,
        FOREIGN KEY(qa_id) REFERENCES knowledge_qa_pairs(id)
    )
    """,
    # ── immutable answer attempts ─────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS review_attempts (
        id              TEXT PRIMARY KEY,
        qa_id           TEXT NOT NULL,
        answered_at     TEXT NOT NULL,
        user_answer     TEXT NOT NULL DEFAULT '',
        result          TEXT NOT NULL,
        matched_points  TEXT NOT NULL DEFAULT '[]',
        answer_snapshot TEXT NOT NULL DEFAULT '',
        mastery_after   REAL NOT NULL,
        stage_after     INTEGER NOT NULL,
        FOREIGN KEY(qa_id) REFERENCES knowledge_qa_pairs(id)
    )
    """,
    # ── all provenance records for a canonical question ───────────────
    """
    CREATE TABLE IF NOT EXISTS knowledge_qa_sources (
        qa_id         TEXT NOT NULL,
        document_id   TEXT NOT NULL,
        source_label  TEXT NOT NULL DEFAULT '',
        quality_score REAL NOT NULL DEFAULT 0.5,
        PRIMARY KEY(qa_id, document_id),
        FOREIGN KEY(qa_id) REFERENCES knowledge_qa_pairs(id),
        FOREIGN KEY(document_id) REFERENCES knowledge_documents(id)
    )
    """,
    # ── ingest_jobs ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ingest_jobs (
        id          TEXT PRIMARY KEY,
        source_kind TEXT NOT NULL,
        query       TEXT NOT NULL,
        status      TEXT NOT NULL,
        started_at  TEXT,
        finished_at TEXT,
        error       TEXT NOT NULL DEFAULT ''
    )
    """,
    # ── dedupe_links ───────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS dedupe_links (
        id                TEXT PRIMARY KEY,
        winner_card_id    TEXT NOT NULL,
        duplicate_card_id TEXT NOT NULL,
        score             REAL NOT NULL,
        reason            TEXT NOT NULL,
        created_at        TEXT NOT NULL
    )
    """,
]

# ---------------------------------------------------------------------------
# FTS5 (optional — degrade gracefully)
# ---------------------------------------------------------------------------

_CREATE_FTS5: str = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title,
    topic,
    overview,
    content,
    tokenize='unicode61'
)
"""

# When FTS5 is unavailable we fall back to LIKE queries.
_fts5_available: bool | None = None  # tri-state: None = not yet checked


def _check_fts5(conn: sqlite3.Connection) -> bool:
    """Return True when the connected SQLite build includes FTS5."""
    global _fts5_available
    if _fts5_available is not None:
        return _fts5_available
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        _fts5_available = True
    except sqlite3.OperationalError:
        _fts5_available = False
    return _fts5_available


# ---------------------------------------------------------------------------
# Path & connection
# ---------------------------------------------------------------------------

_lock = threading.Lock()


def knowledge_db_path() -> Path:
    """Return the canonical path for the SQLite knowledge database."""
    return runtime_path("knowledge.db")


def connect(*, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (or create) the knowledge database.

    Callers are responsible for closing the connection.
    """
    path = knowledge_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ---------------------------------------------------------------------------
# Initialisation & migration
# ---------------------------------------------------------------------------


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create all tables (idempotent) and run pending migrations.

    If *conn* is ``None`` a temporary connection is opened and closed.
    """
    with _lock:
        _own_conn = conn is None
        if _own_conn:
            conn = connect()

        try:
            current_version = get_schema_version(conn)
            if 1 <= current_version < 3:
                _backup_database_before_v3(conn)
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)

            if _check_fts5(conn):
                try:
                    conn.execute(_CREATE_FTS5)
                except sqlite3.OperationalError:
                    pass  # already exists or genuinely unavailable

            _ensure_schema_version_table(conn)
            migrate(conn)
            conn.commit()
        finally:
            if _own_conn:
                conn.close()


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version, or 0 if not yet initialised."""
    try:
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations sequentially."""
    current = get_schema_version(conn)
    if current >= CURRENT_SCHEMA_VERSION:
        return

    from datetime import datetime

    ts = datetime.now().isoformat(timespec="seconds")

    if current == 0:
        conn.execute(
            "INSERT OR IGNORE INTO _schema_version(version, applied_at) VALUES(?, ?)",
            (1, ts),
        )
        current = 1

    if current < 2:
        _migrate_v2(conn)
        conn.execute(
            "INSERT OR IGNORE INTO _schema_version(version, applied_at) VALUES(?, ?)",
            (2, ts),
        )
        current = 2

    if current < 3:
        _migrate_v3(conn)
        conn.execute(
            "INSERT OR IGNORE INTO _schema_version(version, applied_at) VALUES(?, ?)",
            (3, ts),
        )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Deduplicate related rows, add lookup indexes, and rebuild FTS content."""
    duplicate_chunks = conn.execute(
        """
        SELECT id, MIN(id) OVER (
            PARTITION BY card_id, content_hash, heading, COALESCE(source_id, '')
        ) AS keeper_id
        FROM knowledge_chunks
        """
    ).fetchall()
    for chunk_id, keeper_id in duplicate_chunks:
        if chunk_id == keeper_id:
            continue
        conn.execute(
            "UPDATE knowledge_qa_pairs SET source_chunk_id = ? WHERE source_chunk_id = ?",
            (keeper_id, chunk_id),
        )
        conn.execute("DELETE FROM knowledge_chunks WHERE id = ?", (chunk_id,))

    conn.execute(
        """
        DELETE FROM knowledge_qa_pairs
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM knowledge_qa_pairs
            GROUP BY card_id, question
        )
        """
    )

    index_statements = [
        "CREATE INDEX IF NOT EXISTS idx_cards_topic ON knowledge_cards(normalized_topic, archived)",
        "CREATE INDEX IF NOT EXISTS idx_cards_updated ON knowledge_cards(archived, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sources_url ON knowledge_sources(url)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_card ON knowledge_chunks(card_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_source ON knowledge_chunks(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_hash ON knowledge_chunks(content_hash)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_identity "
        "ON knowledge_chunks(card_id, content_hash, heading, COALESCE(source_id, ''))",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_qa_card_question "
        "ON knowledge_qa_pairs(card_id, question)",
        "CREATE INDEX IF NOT EXISTS idx_reviews_due ON review_states(next_review_at, mastery)",
        "CREATE INDEX IF NOT EXISTS idx_review_history_card "
        "ON review_history(card_id, reviewed_at DESC)",
    ]
    for statement in index_statements:
        conn.execute(statement)

    if _check_fts5(conn):
        try:
            conn.execute("DELETE FROM knowledge_fts")
            conn.execute(
                """
                INSERT INTO knowledge_fts(rowid, title, topic, overview, content)
                SELECT kc.rowid, kc.title, kc.topic, kc.overview,
                       COALESCE(GROUP_CONCAT(kch.content, ' '), '')
                FROM knowledge_cards kc
                LEFT JOIN knowledge_chunks kch ON kch.card_id = kc.id
                GROUP BY kc.id
                """
            )
        except sqlite3.OperationalError:
            pass


def _backup_database_before_v3(conn: sqlite3.Connection) -> Path | None:
    """Create a one-time recoverable copy before the question-level migration."""
    path = knowledge_db_path()
    if not path.exists() or path.stat().st_size == 0:
        return None
    existing = sorted(path.parent.glob(f"{path.stem}.pre-v3-*.bak"))
    if existing:
        return existing[-1]

    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.stem}.pre-v3-{stamp}.bak")
    destination = sqlite3.connect(str(backup_path))
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return backup_path


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Add structured QA metadata and question-level review persistence."""
    qa_columns = {
        "canonical_key": "TEXT NOT NULL DEFAULT ''",
        "question_type": "TEXT NOT NULL DEFAULT 'high-frequency'",
        "difficulty": "TEXT NOT NULL DEFAULT 'normal'",
        "answer_summary": "TEXT NOT NULL DEFAULT ''",
        "answer_detail": "TEXT NOT NULL DEFAULT ''",
        "key_points": "TEXT NOT NULL DEFAULT '[]'",
        "pitfalls": "TEXT NOT NULL DEFAULT '[]'",
        "follow_ups": "TEXT NOT NULL DEFAULT '[]'",
        "source_label": "TEXT NOT NULL DEFAULT ''",
        "document_id": "TEXT NOT NULL DEFAULT ''",
        "active": "INTEGER NOT NULL DEFAULT 1",
    }
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(knowledge_qa_pairs)")}
    for name, declaration in qa_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE knowledge_qa_pairs ADD COLUMN {name} {declaration}")

    document_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(knowledge_documents)")}
    if "card_id" not in document_columns:
        conn.execute("ALTER TABLE knowledge_documents ADD COLUMN card_id TEXT")

    now = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR IGNORE INTO question_review_states
            (qa_id, mastery, review_stage, next_review_at, last_reviewed_at,
             review_count, correct_streak, wrong_count, in_mistake_book,
             last_user_answer, last_matched_points, updated_at)
        SELECT qa.id,
               COALESCE(rs.mastery, 0), COALESCE(rs.review_stage, 0),
               COALESCE(rs.next_review_at, ?), rs.last_reviewed_at,
               COALESCE(rs.review_count, 0), 0,
               CASE WHEN COALESCE(rs.mastery, 0) <= 0 AND COALESCE(rs.review_count, 0) > 0 THEN 1 ELSE 0 END,
               CASE WHEN COALESCE(rs.mastery, 0) <= 0 AND COALESCE(rs.review_count, 0) > 0 THEN 1 ELSE 0 END,
               '', '[]', ?
        FROM knowledge_qa_pairs qa
        LEFT JOIN review_states rs ON rs.card_id = qa.card_id
        """,
        (now, now),
    )

    index_statements = [
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_path "
        "ON knowledge_documents(vault_root, relative_path)",
        "CREATE INDEX IF NOT EXISTS idx_documents_status "
        "ON knowledge_documents(status, last_indexed_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_qa_canonical_key "
        "ON knowledge_qa_pairs(canonical_key) WHERE canonical_key <> ''",
        "CREATE INDEX IF NOT EXISTS idx_question_reviews_due "
        "ON question_review_states(next_review_at, mastery)",
        "CREATE INDEX IF NOT EXISTS idx_question_reviews_mistakes "
        "ON question_review_states(in_mistake_book, next_review_at)",
        "CREATE INDEX IF NOT EXISTS idx_attempts_question "
        "ON review_attempts(qa_id, answered_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_qa_sources_document "
        "ON knowledge_qa_sources(document_id, qa_id)",
    ]
    for statement in index_statements:
        conn.execute(statement)
