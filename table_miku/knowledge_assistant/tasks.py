from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .auth import ConflictError, PermissionDenied, Principal, ResourceNotFound
from .database import AssistantDatabase
from .documents import DocumentParser, DocumentService, request_digest
from .observability import TraceRecorder, utc_now
from .rag import RagService


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "rejected", "cancelled"})
_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|token|password)\s*[:=]\s*[^\s,;]+")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    write: bool
    permission: str
    validator: Callable[[Principal, dict[str, Any]], tuple[dict[str, Any], bytes | None]]


class TaskService:
    def __init__(
        self,
        database: AssistantDatabase,
        documents: DocumentService,
        rag: RagService,
        traces: TraceRecorder,
        *,
        approval_ttl_minutes: int = 10,
    ) -> None:
        self.database = database
        self.documents = documents
        self.rag = rag
        self.traces = traces
        self.approval_ttl_minutes = min(max(int(approval_ttl_minutes), 1), 1440)
        self.tools = {
            "query_knowledge": ToolSpec(
                "query_knowledge", False, "knowledge:read", self._validate_query
            ),
            "ingest_text": ToolSpec(
                "ingest_text", True, "knowledge:write", self._validate_ingest_text
            ),
            "archive_document": ToolSpec(
                "archive_document", True, "knowledge:write", self._validate_archive
            ),
        }
        self._recover_interrupted_tasks()

    def create(
        self,
        principal: Principal,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        principal.require("task:create")
        idempotency_key = idempotency_key.strip()
        if len(idempotency_key) < 8:
            raise ValueError("idempotency_key must contain at least 8 characters")
        spec = self.tools.get(tool_name)
        if spec is None:
            raise ValueError(f"unknown tool: {tool_name}")
        principal.require(spec.permission)
        normalized, payload = spec.validator(principal, arguments)
        request_hash = request_digest({"tool_name": tool_name, "arguments": normalized})
        existing = self._existing_idempotent_task(principal, idempotency_key, request_hash)
        if existing is not None:
            existing["idempotent_replay"] = True
            return existing

        task_id = f"task-{uuid.uuid4().hex}"
        now = utc_now()
        status = "awaiting_approval" if spec.write else "queued"
        try:
            with self.database.connect() as conn:
                conn.execute(
                    "INSERT INTO tasks(id, tenant_id, requested_by, tool_name, arguments_json, request_hash, "
                    "idempotency_key, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        principal.tenant_id,
                        principal.user_id,
                        tool_name,
                        json.dumps(normalized, ensure_ascii=False),
                        request_hash,
                        idempotency_key,
                        status,
                        now,
                        now,
                    ),
                )
                if payload is not None:
                    conn.execute(
                        "INSERT INTO task_payloads(task_id, payload) VALUES(?, ?)",
                        (task_id, payload),
                    )
                if spec.write:
                    expires_at = (
                        datetime.now(timezone.utc) + timedelta(minutes=self.approval_ttl_minutes)
                    ).isoformat(timespec="milliseconds")
                    conn.execute(
                        "INSERT INTO approvals(id, task_id, tenant_id, status, requested_by, requested_at, "
                        "expires_at) VALUES(?, ?, ?, 'pending', ?, ?, ?)",
                        (
                            f"approval-{uuid.uuid4().hex}",
                            task_id,
                            principal.tenant_id,
                            principal.user_id,
                            now,
                            expires_at,
                        ),
                    )
        except sqlite3.IntegrityError:
            existing = self._existing_idempotent_task(principal, idempotency_key, request_hash)
            if existing is not None:
                existing["idempotent_replay"] = True
                return existing
            raise
        if not spec.write:
            return self._execute(task_id, principal)
        task = self.get(principal, task_id)
        task["idempotent_replay"] = False
        return task

    def approve(self, principal: Principal, task_id: str) -> dict[str, Any]:
        principal.require("task:approve")
        expired = False
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT t.status, t.requested_by, a.status AS approval_status, a.expires_at "
                "FROM tasks t JOIN approvals a ON a.task_id = t.id "
                "WHERE t.id = ? AND t.tenant_id = ?",
                (task_id, principal.tenant_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("task or approval not found")
            if row["status"] == "succeeded":
                return self.get(principal, task_id)
            if row["status"] in TERMINAL_STATUSES:
                raise ConflictError(f"task is already {row['status']}")
            if row["requested_by"] == principal.user_id:
                raise PermissionDenied("requester cannot approve their own write task")
            if row["approval_status"] != "pending" or row["status"] != "awaiting_approval":
                raise ConflictError("task is not awaiting approval")
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
            if expires_at <= datetime.now(timezone.utc):
                conn.execute(
                    "UPDATE approvals SET status = 'expired', decided_at = ? WHERE task_id = ?",
                    (utc_now(), task_id),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'cancelled', error_code = 'approval_expired', "
                    "error_message = 'approval expired', updated_at = ?, finished_at = ? WHERE id = ?",
                    (utc_now(), utc_now(), task_id),
                )
                conn.execute("DELETE FROM task_payloads WHERE task_id = ?", (task_id,))
                expired = True
            else:
                decided_at = utc_now()
                conn.execute(
                    "UPDATE approvals SET status = 'approved', decided_by = ?, decided_at = ? WHERE task_id = ?",
                    (principal.user_id, decided_at, task_id),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'queued', updated_at = ? WHERE id = ?",
                    (decided_at, task_id),
                )
        if expired:
            raise ConflictError("approval expired")
        execution_principal = Principal(
            tenant_id=principal.tenant_id,
            user_id=str(row["requested_by"]),
            roles=frozenset({"editor"}),
        )
        return self._execute(task_id, execution_principal, approved_by=principal.user_id)

    def reject(self, principal: Principal, task_id: str, reason: str = "") -> dict[str, Any]:
        principal.require("task:approve")
        safe_reason = self._safe_error(reason, 300)
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT t.status, t.requested_by, a.status AS approval_status FROM tasks t "
                "JOIN approvals a ON a.task_id = t.id WHERE t.id = ? AND t.tenant_id = ?",
                (task_id, principal.tenant_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("task or approval not found")
            if row["status"] == "rejected":
                return self.get(principal, task_id)
            if row["status"] in TERMINAL_STATUSES:
                raise ConflictError(f"task is already {row['status']}")
            if row["requested_by"] == principal.user_id:
                raise PermissionDenied("requester cannot decide their own write task")
            if row["approval_status"] != "pending" or row["status"] != "awaiting_approval":
                raise ConflictError("task is not awaiting approval")
            decided_at = utc_now()
            conn.execute(
                "UPDATE approvals SET status = 'rejected', decided_by = ?, decided_at = ?, reason = ? "
                "WHERE task_id = ?",
                (principal.user_id, decided_at, safe_reason, task_id),
            )
            conn.execute(
                "UPDATE tasks SET status = 'rejected', result_json = ?, updated_at = ?, finished_at = ? "
                "WHERE id = ?",
                (json.dumps({"reason": safe_reason}, ensure_ascii=False), decided_at, decided_at, task_id),
            )
            conn.execute("DELETE FROM task_payloads WHERE task_id = ?", (task_id,))
        return self.get(principal, task_id)

    def get(self, principal: Principal, task_id: str) -> dict[str, Any]:
        principal.require("task:read")
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND tenant_id = ?",
                (task_id, principal.tenant_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("task not found")
            approval = conn.execute(
                "SELECT id, status, requested_by, decided_by, requested_at, decided_at, expires_at, reason "
                "FROM approvals WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            receipt = conn.execute(
                "SELECT operation_id, tool_name, approved_by, completed_at FROM operation_receipts "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        result = dict(row)
        result["arguments"] = json.loads(result.pop("arguments_json"))
        result["result"] = json.loads(result.pop("result_json"))
        result["approval"] = dict(approval) if approval is not None else None
        result["receipt"] = dict(receipt) if receipt is not None else None
        return result

    def list(self, principal: Principal, limit: int = 100) -> list[dict[str, Any]]:
        principal.require("task:read")
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                (principal.tenant_id, min(max(int(limit), 1), 500)),
            ).fetchall()
        return [self.get(principal, str(row["id"])) for row in rows]

    def _execute(
        self,
        task_id: str,
        principal: Principal,
        *,
        approved_by: str = "",
    ) -> dict[str, Any]:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND tenant_id = ?",
                (task_id, principal.tenant_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("task not found")
            if row["status"] == "succeeded":
                return self.get(principal, task_id)
            if row["status"] != "queued":
                raise ConflictError(f"task cannot run from status {row['status']}")
            started_at = utc_now()
            updated = conn.execute(
                "UPDATE tasks SET status = 'running', started_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (started_at, started_at, task_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("task was claimed by another worker")
            payload_row = conn.execute(
                "SELECT payload FROM task_payloads WHERE task_id = ?", (task_id,)
            ).fetchone()
        tool_name = str(row["tool_name"])
        arguments = json.loads(row["arguments_json"])
        try:
            with self.traces.trace("task.execute", principal, {"tool_name": tool_name}) as trace:
                with trace.span(f"tool.{tool_name}"):
                    result = self._invoke(
                        principal,
                        task_id,
                        tool_name,
                        arguments,
                        bytes(payload_row["payload"]) if payload_row is not None else None,
                    )
        except Exception as exc:
            finished_at = utc_now()
            with self.database.connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'failed', error_code = ?, error_message = ?, "
                    "updated_at = ?, finished_at = ? WHERE id = ?",
                    (
                        type(exc).__name__[:120],
                        self._safe_error(str(exc), 500),
                        finished_at,
                        finished_at,
                        task_id,
                    ),
                )
                conn.execute("DELETE FROM task_payloads WHERE task_id = ?", (task_id,))
            return self.get(principal, task_id)
        finished_at = utc_now()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'succeeded', result_json = ?, error_code = '', error_message = '', "
                "updated_at = ?, finished_at = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), finished_at, finished_at, task_id),
            )
            if approved_by:
                conn.execute(
                    "INSERT OR IGNORE INTO operation_receipts(operation_id, tenant_id, task_id, tool_name, "
                    "result_json, approved_by, completed_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        principal.tenant_id,
                        task_id,
                        tool_name,
                        json.dumps(result, ensure_ascii=False),
                        approved_by,
                        finished_at,
                    ),
                )
            conn.execute("DELETE FROM task_payloads WHERE task_id = ?", (task_id,))
        return self.get(principal, task_id)

    def _invoke(
        self,
        principal: Principal,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        payload: bytes | None,
    ) -> dict[str, Any]:
        if tool_name == "query_knowledge":
            return self.rag.query(
                principal,
                str(arguments["query"]),
                collection_ids=arguments.get("collection_ids"),
                top_k=int(arguments.get("top_k", 5)),
            )
        if tool_name == "ingest_text":
            if payload is None:
                raise RuntimeError("approved task payload is missing")
            return self.documents.upload(
                principal,
                filename=str(arguments["filename"]),
                content=payload,
                collection_id=str(arguments["collection_id"]),
                idempotency_key=task_id,
            )
        if tool_name == "archive_document":
            return self.documents.archive(principal, str(arguments["document_id"]))
        raise ValueError(f"unknown tool: {tool_name}")

    def _existing_idempotent_task(
        self,
        principal: Principal,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id, request_hash FROM tasks WHERE tenant_id = ? AND idempotency_key = ?",
                (principal.tenant_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ConflictError("idempotency key was already used with a different task request")
        return self.get(principal, str(row["id"]))

    def _recover_interrupted_tasks(self) -> int:
        """Fail closed after process loss; interrupted tools are never retried automatically."""
        finished_at = utc_now()
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status IN ('queued', 'running')"
            ).fetchall()
            task_ids = [str(row["id"]) for row in rows]
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(
                f"UPDATE tasks SET status = 'failed', error_code = 'interrupted', "
                f"error_message = 'process stopped before task completion; inspect side effects before retry', "
                f"updated_at = ?, finished_at = ? WHERE id IN ({placeholders})",
                (finished_at, finished_at, *task_ids),
            )
            conn.execute(
                f"DELETE FROM task_payloads WHERE task_id IN ({placeholders})",
                task_ids,
            )
        return len(task_ids)

    @staticmethod
    def _validate_query(
        principal: Principal, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes | None]:
        query = str(arguments.get("query", "")).strip()
        if len(query) < 2 or len(query) > 1000:
            raise ValueError("query must contain 2 to 1000 characters")
        raw_collections = arguments.get("collection_ids")
        collections = None
        if raw_collections is not None:
            if not isinstance(raw_collections, list):
                raise ValueError("collection_ids must be a list")
            collections = [
                DocumentService._collection_id(str(item))
                for item in raw_collections
                if str(item).strip()
            ]
            for collection_id in collections:
                principal.require_collection(collection_id)
        top_k = min(max(int(arguments.get("top_k", 5)), 1), 8)
        return {"query": query, "collection_ids": collections, "top_k": top_k}, None

    @staticmethod
    def _validate_ingest_text(
        principal: Principal, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes | None]:
        filename = DocumentParser.safe_filename(str(arguments.get("filename", "")))
        if not filename.casefold().endswith((".txt", ".md", ".markdown", ".rst", ".json")):
            raise ValueError("ingest_text only accepts text document extensions")
        content = str(arguments.get("content", ""))
        payload = content.encode("utf-8")
        if not payload or len(payload) > 1024 * 1024:
            raise ValueError("ingest_text content must contain 1 byte to 1 MiB")
        collection_id = DocumentService._collection_id(
            str(arguments.get("collection_id", "default"))
        )
        principal.require_collection(collection_id)
        normalized = {
            "filename": filename,
            "collection_id": collection_id,
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
        }
        return normalized, payload

    def _validate_archive(
        self, principal: Principal, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes | None]:
        document_id = str(arguments.get("document_id", "")).strip()
        if not document_id:
            raise ValueError("document_id is required")
        self.documents.get_document(principal, document_id)
        return {"document_id": document_id}, None

    @staticmethod
    def _safe_error(value: str, limit: int) -> str:
        return _SECRET.sub("[REDACTED]", value)[:limit]
