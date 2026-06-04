"""SQLite knowledge database — connection, schema, FTS5, and migration versioning.

All schema DDL lives here.  Import-time side effects are avoided — callers must
explicitly call ``init_db()`` or ``connect()``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .paths import user_data_dir

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------
CURRENT_SCHEMA_VERSION = 1

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
    return user_data_dir() / "knowledge.db"


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

    # Future migrations go here:
    # if current < 2:
    #     conn.execute("ALTER TABLE ...")
    #     conn.execute(
    #         "INSERT INTO _schema_version(version, applied_at) VALUES(?, ?)",
    #         (2, ts),
    #     )

    # Seed the initial version marker.
    if current == 0:
        conn.execute(
            "INSERT OR IGNORE INTO _schema_version(version, applied_at) VALUES(?, ?)",
            (CURRENT_SCHEMA_VERSION, ts),
        )
