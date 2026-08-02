from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import runtime_path


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|authorization|token|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{12,}\b"),
)


def redact_text(value: str, limit: int = 12_000) -> str:
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


class AgentStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or runtime_path("agent.db", migrate_legacy=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.cancel_interrupted_runs()
        self.cleanup_expired_sessions()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    source_ids TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_agent_messages_session
                    ON messages(session_id, created_at, id);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS receipts(
                    operation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    authorized_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reversible INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS capabilities(
                    cache_key TEXT PRIMARY KEY,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    tested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_grants(
                    resource TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL
                );
                """
            )
            defaults = {"knowledge": 1, "review": 1, "goals": 0, "timetable": 0, "interviews": 0}
            conn.executemany(
                "INSERT OR IGNORE INTO resource_grants(resource, enabled) VALUES(?, ?)",
                defaults.items(),
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def create_session(self, title: str = "新会话") -> str:
        session_id = f"session-{uuid.uuid4().hex}"
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (session_id, redact_text(title, 80) or "新会话", now, now),
            )
        return session_id

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def delete_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cursor.rowcount > 0

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        run_id: str = "",
        source_ids: list[str] | None = None,
    ) -> str:
        message_id = f"message-{uuid.uuid4().hex}"
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages(id, session_id, role, content, created_at, run_id, source_ids) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    session_id,
                    role,
                    redact_text(content),
                    now,
                    run_id,
                    json.dumps(source_ids or [], ensure_ascii=False),
                ),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            stale = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET 100",
                (session_id,),
            ).fetchall()
            if stale:
                conn.executemany("DELETE FROM messages WHERE id = ?", [(row[0],) for row in stale])
        return message_id

    def list_messages(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (session_id, min(max(limit, 1), 100)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source_ids"] = json.loads(item.get("source_ids") or "[]")
            result.append(item)
        return result

    def start_run(self, session_id: str) -> str:
        run_id = f"run-{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs(id, session_id, status, started_at) VALUES(?, ?, 'running', ?)",
                (run_id, session_id, self._now()),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, *, error: str = "", metadata: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ?, metadata = ? WHERE id = ?",
                (
                    status,
                    self._now(),
                    redact_text(error, 1000),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    run_id,
                ),
            )

    def cancel_interrupted_runs(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'cancelled', finished_at = ?, error = '应用重启，未完成审批已取消' "
                "WHERE status IN ('running', 'awaiting_approval')",
                (self._now(),),
            )
        return cursor.rowcount

    def cleanup_expired_sessions(self, days: int = 90) -> int:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        return cursor.rowcount

    def resource_grants(self) -> dict[str, bool]:
        with self._connect() as conn:
            rows = conn.execute("SELECT resource, enabled FROM resource_grants").fetchall()
        return {str(row[0]): bool(row[1]) for row in rows}

    def set_resource_grant(self, resource: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO resource_grants(resource, enabled) VALUES(?, ?) "
                "ON CONFLICT(resource) DO UPDATE SET enabled = excluded.enabled",
                (resource, int(enabled)),
            )

    def get_receipt(self, operation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM receipts WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["preview"] = json.loads(item.pop("preview_json"))
        item["result"] = json.loads(item.pop("result_json"))
        item["reversible"] = bool(item["reversible"])
        return item

    def save_receipt(
        self,
        *,
        operation_id: str,
        session_id: str,
        tool_name: str,
        preview: dict[str, Any],
        result: dict[str, Any],
        authorized_at: str,
        status: str,
        reversible: bool,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO receipts(operation_id, session_id, tool_name, preview_json, result_json, "
                "authorized_at, completed_at, status, reversible) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    session_id,
                    tool_name,
                    json.dumps(preview, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    authorized_at,
                    self._now(),
                    status,
                    int(reversible),
                ),
            )
        return self.get_receipt(operation_id) or {}

    @staticmethod
    def capability_key(base_url: str, model: str) -> str:
        return hashlib.sha256(f"{base_url.rstrip('/')}\n{model}".encode()).hexdigest()

    def load_capability(self, base_url: str, model: str) -> dict[str, Any] | None:
        key = self.capability_key(base_url, model)
        with self._connect() as conn:
            row = conn.execute("SELECT result_json, tested_at FROM capabilities WHERE cache_key = ?", (key,)).fetchone()
        if row is None:
            return None
        result = json.loads(row[0])
        result["tested_at"] = row[1]
        return result

    def save_capability(self, base_url: str, model: str, result: dict[str, Any]) -> None:
        key = self.capability_key(base_url, model)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO capabilities(cache_key, base_url, model, result_json, tested_at) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET result_json = excluded.result_json, tested_at = excluded.tested_at",
                (key, base_url.rstrip("/"), model, json.dumps(result, ensure_ascii=False), self._now()),
            )
