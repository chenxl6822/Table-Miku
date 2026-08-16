from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path

from table_miku.paths import runtime_path


SCHEMA_VERSION = 4


class ClosingConnection(sqlite3.Connection):
    """A sqlite connection whose context manager also releases the file handle."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class AssistantDatabase:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or runtime_path("knowledge_assistant_2.db", migrate_legacy=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def initialize(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_versions(
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS worker_leases(
                    name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    process_boot_id TEXT NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_ka2_documents_scope
                    ON documents(tenant_id, collection_id, archived, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ka2_documents_content
                    ON documents(tenant_id, collection_id, checksum)
                    WHERE archived = 0;

                CREATE TABLE IF NOT EXISTS document_blobs(
                    document_id TEXT PRIMARY KEY,
                    content BLOB NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    heading TEXT NOT NULL DEFAULT '',
                    page_number INTEGER,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    embedding_model TEXT NOT NULL,
                    embedding_dimension INTEGER NOT NULL,
                    token_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                    UNIQUE(document_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_ka2_chunks_scope
                    ON chunks(tenant_id, collection_id, document_id);

                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(tenant_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_ka2_tasks_scope
                    ON tasks(tenant_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS task_payloads(
                    task_id TEXT PRIMARY KEY,
                    payload BLOB NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS approvals(
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    decided_by TEXT,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT,
                    expires_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS operation_receipts(
                    operation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    tool_name TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS idempotency_records(
                    tenant_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, scope, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS traces(
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    attributes_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_ka2_traces_scope
                    ON traces(tenant_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS spans(
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(trace_id) REFERENCES traces(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_ka2_spans_trace ON spans(trace_id, started_at);

                CREATE TABLE IF NOT EXISTS ingestion_jobs(
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    request_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress_phase TEXT NOT NULL DEFAULT 'queued',
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 1,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    run_token TEXT NOT NULL DEFAULT '',
                    worker_instance_id TEXT NOT NULL DEFAULT '',
                    retryable INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    trace_id TEXT,
                    document_id TEXT,
                    deduplicated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    cancel_requested_at TEXT,
                    cancel_outcome TEXT NOT NULL DEFAULT '',
                    UNIQUE(tenant_id, idempotency_key),
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_ka2_ingestion_jobs_scope
                    ON ingestion_jobs(tenant_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ka2_ingestion_jobs_dispatch
                    ON ingestion_jobs(status, created_at, id);

                CREATE TABLE IF NOT EXISTS ingestion_payloads(
                    job_id TEXT PRIMARY KEY,
                    content BLOB NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES ingestion_jobs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS work_items(
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    summary_sha256 TEXT NOT NULL,
                    remote_idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    closed_at TEXT,
                    closed_by TEXT NOT NULL DEFAULT '',
                    close_task_id TEXT NOT NULL DEFAULT '',
                    close_request_hash TEXT NOT NULL DEFAULT '',
                    UNIQUE(tenant_id, remote_idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_ka2_work_items_scope
                    ON work_items(tenant_id, collection_id, created_at DESC);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO service_metadata(key, value) VALUES('service_instance_id', ?)",
                (f"ka-{uuid.uuid4().hex}",),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions(version, applied_at) VALUES(?, datetime('now'))",
                (SCHEMA_VERSION,),
            )
            self._ensure_work_item_close_columns(conn)

    @staticmethod
    def _ensure_work_item_close_columns(conn: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(work_items)")}
        for name, definition in (
            ("status", "TEXT NOT NULL DEFAULT 'open'"),
            ("closed_at", "TEXT"),
            ("closed_by", "TEXT NOT NULL DEFAULT ''"),
            ("close_task_id", "TEXT NOT NULL DEFAULT ''"),
            ("close_request_hash", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE work_items ADD COLUMN {name} {definition}")

    @property
    def service_instance_id(self) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM service_metadata WHERE key = 'service_instance_id'"
            ).fetchone()
        if row is None or not str(row["value"]).strip():
            raise RuntimeError("Knowledge Assistant service identity is unavailable")
        return str(row["value"])
