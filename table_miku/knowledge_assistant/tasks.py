from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
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
APPROVAL_PREVIEW_VERSION = 2
_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|token|password)\s*[:=]\s*[^\s,;]+")
_REMOTE_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]+")
_APPROVAL_SIGNING_KEY_BYTES = 32


def _load_or_create_approval_signing_key(database: AssistantDatabase) -> bytes:
    key_path = database.path.with_name(f"{database.path.name}.approval-hmac-key")
    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        candidate = secrets.token_bytes(_APPROVAL_SIGNING_KEY_BYTES)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(key_path, flags, 0o600)
        except FileExistsError:
            key = key_path.read_bytes()
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            key = candidate
    if len(key) != _APPROVAL_SIGNING_KEY_BYTES:
        raise RuntimeError("approval signing key has an invalid length")
    return key


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
        self._approval_signing_key = _load_or_create_approval_signing_key(database)
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
            "create_work_item": ToolSpec(
                "create_work_item", True, "knowledge:write", self._validate_create_work_item
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

    def preview(self, principal: Principal, task_id: str) -> dict[str, Any]:
        principal.require("task:approve")
        with self.database.connect() as conn:
            row = self._approval_row(conn, principal.tenant_id, task_id)
        if row is None:
            raise ResourceNotFound("task or approval not found")
        arguments = self._validated_task_arguments(row)
        self._task_collection_scope(principal, str(row["tool_name"]), arguments)
        if row["requested_by"] == principal.user_id:
            raise PermissionDenied("requester cannot preview or approve their own write task")
        if row["approval_status"] != "pending" or row["status"] != "awaiting_approval":
            raise ConflictError("task is not awaiting approval")
        if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(timezone.utc):
            raise ConflictError("approval expired")

        payload = self._validated_staged_payload(str(row["tool_name"]), arguments, row["staged_payload"])
        preview_arguments = self._preview_arguments(principal, str(row["tool_name"]), arguments)
        return self._build_approval_preview(
            row,
            preview_arguments,
            payload,
            approver_user_id=principal.user_id,
        )

    def approve(self, principal: Principal, task_id: str, preview_hash: str) -> dict[str, Any]:
        principal.require("task:approve")
        supplied_preview_hash = preview_hash.strip()
        if not supplied_preview_hash:
            raise ValueError("preview_hash is required; fetch the approval preview before approving")
        expired = False
        execution_scope: frozenset[str] | None = None
        approved_arguments: dict[str, Any] | None = None
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._approval_row(conn, principal.tenant_id, task_id)
            if row is None:
                raise ResourceNotFound("task or approval not found")
            arguments = self._validated_task_arguments(row)
            execution_scope = self._task_collection_scope(
                principal,
                str(row["tool_name"]),
                arguments,
            )
            if row["requested_by"] == principal.user_id:
                raise PermissionDenied("requester cannot approve their own write task")
            if row["status"] == "succeeded":
                receipt_approver, receipt_preview_hash = self._completed_approval_contract(
                    conn,
                    task_id,
                )
                if (
                    receipt_approver != principal.user_id
                    or not receipt_preview_hash
                    or not hmac.compare_digest(supplied_preview_hash, receipt_preview_hash)
                ):
                    raise ConflictError("approval preview is stale or does not match this task")
                return self.get(principal, task_id)
            if row["status"] in TERMINAL_STATUSES:
                raise ConflictError(f"task is already {row['status']}")
            if row["approval_status"] != "pending" or row["status"] != "awaiting_approval":
                if not (
                    row["approval_status"] == "approved"
                    and row["status"] in {"queued", "running"}
                ):
                    raise ConflictError("task is not awaiting approval")
            payload = self._validated_staged_payload(
                str(row["tool_name"]),
                arguments,
                row["staged_payload"],
            )
            approved_arguments = self._preview_arguments(
                principal,
                str(row["tool_name"]),
                arguments,
            )
            expected_preview = self._build_approval_preview(
                row,
                approved_arguments,
                payload,
                approver_user_id=principal.user_id,
            )
            if not hmac.compare_digest(
                supplied_preview_hash,
                str(expected_preview["preview_hash"]),
            ):
                raise ConflictError("approval preview is stale or does not match this task")
            if row["approval_status"] == "approved" and row["status"] in {"queued", "running"}:
                return self.get(principal, task_id)
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
            collection_ids=execution_scope,
        )
        return self._execute(
            task_id,
            execution_principal,
            approved_by=principal.user_id,
            approved_preview_hash=supplied_preview_hash,
            approved_arguments=approved_arguments,
        )

    def reject(self, principal: Principal, task_id: str, reason: str = "") -> dict[str, Any]:
        principal.require("task:approve")
        safe_reason = self._safe_error(reason, 300)
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT t.status, t.requested_by, t.tool_name, t.arguments_json, t.request_hash, "
                "a.status AS approval_status FROM tasks t "
                "JOIN approvals a ON a.task_id = t.id WHERE t.id = ? AND t.tenant_id = ?",
                (task_id, principal.tenant_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("task or approval not found")
            arguments = self._validated_task_arguments(row)
            self._task_collection_scope(principal, str(row["tool_name"]), arguments)
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

    def get(
        self,
        principal: Principal,
        task_id: str,
        *,
        _allow_unverified_scope: bool = False,
    ) -> dict[str, Any]:
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
                "SELECT operation_id, tool_name, result_json, approved_by, completed_at "
                "FROM operation_receipts "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        result = dict(row)
        result.pop("request_hash")
        try:
            arguments = self._validated_task_arguments(row)
        except ConflictError:
            if principal.collection_ids is not None and not _allow_unverified_scope:
                raise PermissionDenied("task collection scope cannot be verified") from None
            arguments = {}
            result["arguments_integrity"] = "failed"
        else:
            self._task_collection_scope(principal, str(result["tool_name"]), arguments)
        result["arguments"] = arguments
        result.pop("arguments_json")
        result["result"] = json.loads(result.pop("result_json"))
        approval_result = dict(approval) if approval is not None else None
        result["approval"] = approval_result
        if receipt is None:
            result["receipt"] = None
        else:
            receipt_result = dict(receipt)
            receipt_record = json.loads(receipt_result.pop("result_json"))
            if isinstance(receipt_record, dict) and "approved_preview_hash" in receipt_record:
                receipt_result["approved_preview_hash"] = receipt_record["approved_preview_hash"]
                receipt_result["arguments"] = receipt_record.get("arguments", arguments)
                receipt_result["result"] = receipt_record.get("result", result["result"])
            else:
                receipt_result["approved_preview_hash"] = None
                receipt_result["arguments"] = arguments
                receipt_result["result"] = receipt_record
            result["receipt"] = receipt_result
        return result

    def list(self, principal: Principal, limit: int = 100) -> list[dict[str, Any]]:
        principal.require("task:read")
        requested_limit = min(max(int(limit), 1), 500)
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 500",
                (principal.tenant_id,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                items.append(self.get(principal, str(row["id"])))
            except PermissionDenied:
                continue
            if len(items) >= requested_limit:
                break
        return items

    @staticmethod
    def _approval_row(
        conn: sqlite3.Connection,
        tenant_id: str,
        task_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT t.id, t.tenant_id, t.status, t.requested_by, t.tool_name, t.arguments_json, "
            "t.request_hash, a.id AS approval_id, a.status AS approval_status, a.requested_at, "
            "a.expires_at, p.payload AS staged_payload FROM tasks t "
            "JOIN approvals a ON a.task_id = t.id "
            "LEFT JOIN task_payloads p ON p.task_id = t.id "
            "WHERE t.id = ? AND t.tenant_id = ?",
            (task_id, tenant_id),
        ).fetchone()

    @staticmethod
    def _completed_approval_contract(
        conn: sqlite3.Connection,
        task_id: str,
    ) -> tuple[str, str]:
        receipt = conn.execute(
            "SELECT approved_by, result_json FROM operation_receipts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if receipt is None:
            return "", ""
        try:
            receipt_record = json.loads(str(receipt["result_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return str(receipt["approved_by"]), ""
        if not isinstance(receipt_record, dict):
            return str(receipt["approved_by"]), ""
        return str(receipt["approved_by"]), str(
            receipt_record.get("approved_preview_hash", "")
        )

    @staticmethod
    def _validated_task_arguments(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        try:
            arguments = json.loads(str(row["arguments_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConflictError("stored task request is invalid") from exc
        if not isinstance(arguments, dict):
            raise ConflictError("stored task request is invalid")
        expected_hash = request_digest(
            {"tool_name": str(row["tool_name"]), "arguments": arguments}
        )
        if not hmac.compare_digest(str(row["request_hash"]), expected_hash):
            raise ConflictError("stored task request no longer matches its integrity hash")
        return arguments

    def _approval_preview_hash(
        self,
        preview_contract: dict[str, Any],
        request_hash: str,
    ) -> str:
        encoded = json.dumps(
            {
                "preview_contract": preview_contract,
                "request_hash": request_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        domain = f"table-miku/approval-preview/v{APPROVAL_PREVIEW_VERSION}\0".encode("ascii")
        return hmac.new(
            self._approval_signing_key,
            domain + encoded,
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _validated_staged_payload(
        tool_name: str,
        arguments: dict[str, Any],
        staged_payload: Any,
    ) -> bytes | None:
        hash_field = {
            "ingest_text": "content_sha256",
            "create_work_item": "summary_sha256",
        }.get(tool_name)
        if hash_field is None:
            if staged_payload is not None:
                raise ConflictError("write task contains an unexpected staged payload")
            return None
        if staged_payload is None:
            raise ConflictError("write task payload is missing")
        payload = bytes(staged_payload)
        try:
            expected_size = int(arguments["byte_size"])
            expected_hash = str(arguments[hash_field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConflictError("stored task payload metadata is invalid") from exc
        if len(payload) != expected_size or not hmac.compare_digest(
            hashlib.sha256(payload).hexdigest(),
            expected_hash,
        ):
            raise ConflictError("write task payload no longer matches the approval preview")
        return payload

    def _task_collection_scope(
        self,
        principal: Principal,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> frozenset[str] | None:
        if tool_name == "query_knowledge":
            raw_collections = arguments.get("collection_ids")
            if raw_collections is None:
                if principal.collection_ids is not None:
                    raise PermissionDenied("task collection scope is broader than the granted scope")
                return None
            collections = frozenset(str(item) for item in raw_collections)
            for collection_id in collections:
                principal.require_collection(collection_id)
            return collections
        if tool_name in {"ingest_text", "create_work_item"}:
            collection_id = str(arguments["collection_id"])
        elif tool_name == "archive_document":
            collection_id = str(arguments.get("collection_id", ""))
            if not collection_id:
                document = self.documents.get_document(principal, str(arguments["document_id"]))
                collection_id = str(document["collection_id"])
        else:
            raise PermissionDenied("task collection scope cannot be determined")
        principal.require_collection(collection_id)
        return frozenset({collection_id})

    def _build_approval_preview(
        self,
        row: sqlite3.Row,
        arguments: dict[str, Any],
        payload: bytes | None,
        *,
        approver_user_id: str,
    ) -> dict[str, Any]:
        tool_name = str(row["tool_name"])
        if tool_name == "ingest_text":
            if payload is None:
                raise ConflictError("write task payload is missing")
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConflictError("write task payload is not valid UTF-8") from exc
            action = {
                "tool_name": tool_name,
                "intent": "ensure_indexed",
                "target": {
                    "tenant_id": str(row["tenant_id"]),
                    "collection_id": str(arguments["collection_id"]),
                    "filename": str(arguments["filename"]),
                },
                "parameters": {
                    "content": content,
                    "render_as": "plain_text",
                    "content_sha256": str(arguments["content_sha256"]),
                    "byte_size": int(arguments["byte_size"]),
                },
                "consequences": [
                    "The exact UTF-8 content will be parsed, chunked, embedded, and made searchable.",
                    "Identical active content in the same collection may reuse the existing document.",
                ],
                "reversibility": "soft_archive_available_after_indexing",
            }
        elif tool_name == "create_work_item":
            if payload is None:
                raise ConflictError("write task payload is missing")
            try:
                summary = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConflictError("write task payload is not valid UTF-8") from exc
            action = {
                "tool_name": tool_name,
                "intent": "ensure_work_item",
                "target": {
                    "tenant_id": str(row["tenant_id"]),
                    "collection_id": str(arguments["collection_id"]),
                    "title": str(arguments["title"]),
                    "remote_idempotency_key": str(arguments["remote_idempotency_key"]),
                },
                "parameters": {
                    "content": summary,
                    "render_as": "plain_text",
                    "summary_sha256": str(arguments["summary_sha256"]),
                    "byte_size": int(arguments["byte_size"]),
                },
                "consequences": [
                    "The exact UTF-8 summary will be written to the local work-item ledger, a stand-in for an external ticket system.",
                    "The same tenant and remote idempotency key with the same request returns the original work item.",
                    "The same remote idempotency key with a different request is rejected.",
                ],
                "reversibility": "administrative_restore_required",
            }
        elif tool_name == "archive_document":
            action = {
                "tool_name": tool_name,
                "intent": "ensure_archived",
                "target": {
                    "tenant_id": str(row["tenant_id"]),
                    "collection_id": str(arguments["collection_id"]),
                    "document_id": str(arguments["document_id"]),
                    "filename": str(arguments["filename"]),
                    "checksum": str(arguments["checksum"]),
                },
                "parameters": {},
                "consequences": [
                    "The document will be excluded from document listings and knowledge retrieval.",
                    "The current API does not expose a self-service restore operation.",
                ],
                "reversibility": "administrative_restore_required",
            }
        else:
            raise ConflictError("task does not support approval preview")
        preview_contract = {
            "preview_version": APPROVAL_PREVIEW_VERSION,
            "task_id": str(row["id"]),
            "approval_id": str(row["approval_id"]),
            "requested_at": str(row["requested_at"]),
            "expires_at": str(row["expires_at"]),
            "provenance": {
                "origin": "agent_tool_request",
                "requested_by": str(row["requested_by"]),
                "input_trust": "unverified",
            },
            "decision": {
                "bound_approver": approver_user_id,
                "requester_separation_required": True,
                "approve_label": "Approve this exact action",
                "reject_label": "Reject without side effects",
            },
            "action": action,
        }
        return {
            **preview_contract,
            "preview_hash": self._approval_preview_hash(
                preview_contract,
                str(row["request_hash"]),
            ),
        }

    def _preview_arguments(
        self,
        principal: Principal,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name != "archive_document":
            return arguments
        document = self.documents.get_document(principal, str(arguments["document_id"]))
        current_metadata = {
            "filename": str(document["filename"]),
            "collection_id": str(document["collection_id"]),
            "checksum": str(document["checksum"]),
        }
        for key, value in current_metadata.items():
            if key in arguments and str(arguments[key]) != value:
                raise ConflictError("archive target no longer matches the stored task metadata")
        enriched = dict(arguments)
        enriched.update(current_metadata)
        return enriched

    def _execute(
        self,
        task_id: str,
        principal: Principal,
        *,
        approved_by: str = "",
        approved_preview_hash: str = "",
        approved_arguments: dict[str, Any] | None = None,
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
        try:
            arguments = self._validated_task_arguments(row)
            payload = self._validated_staged_payload(
                tool_name,
                arguments,
                payload_row["payload"] if payload_row is not None else None,
            )
            if approved_by:
                current_approved_arguments = self._preview_arguments(
                    principal,
                    tool_name,
                    arguments,
                )
                if approved_arguments is None or current_approved_arguments != approved_arguments:
                    raise ConflictError("write task action no longer matches the approved preview")
            with self.traces.trace("task.execute", principal, {"tool_name": tool_name}) as trace:
                with trace.span(f"tool.{tool_name}"):
                    result = self._invoke(
                        principal,
                        task_id,
                        tool_name,
                        arguments,
                        payload,
                        request_hash=str(row["request_hash"]),
                        requested_by=str(row["requested_by"]),
                        approved_by=approved_by,
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
            return self.get(principal, task_id, _allow_unverified_scope=True)
        finished_at = utc_now()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'succeeded', result_json = ?, error_code = '', error_message = '', "
                "updated_at = ?, finished_at = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), finished_at, finished_at, task_id),
            )
            if approved_by:
                receipt_record = {
                    "result": result,
                    "approved_preview_hash": approved_preview_hash,
                    "preview_version": APPROVAL_PREVIEW_VERSION,
                    "arguments": approved_arguments or arguments,
                }
                conn.execute(
                    "INSERT OR IGNORE INTO operation_receipts(operation_id, tenant_id, task_id, tool_name, "
                    "result_json, approved_by, completed_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        principal.tenant_id,
                        task_id,
                        tool_name,
                        json.dumps(receipt_record, ensure_ascii=False),
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
        *,
        request_hash: str = "",
        requested_by: str = "",
        approved_by: str = "",
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
        if tool_name == "create_work_item":
            if payload is None:
                raise RuntimeError("approved task payload is missing")
            return self._ensure_work_item(
                principal,
                task_id,
                arguments,
                payload,
                request_hash=request_hash,
                requested_by=requested_by,
                approved_by=approved_by,
            )
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
        collections = (
            sorted(principal.collection_ids)
            if raw_collections is None and principal.collection_ids is not None
            else None
        )
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
        document = self.documents.get_document(principal, document_id)
        return {
            "document_id": document_id,
            "filename": str(document["filename"]),
            "collection_id": str(document["collection_id"]),
            "checksum": str(document["checksum"]),
        }, None

    @staticmethod
    def _validate_create_work_item(
        principal: Principal, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes | None]:
        title = str(arguments.get("title", "")).strip()
        if not title or len(title) > 120:
            raise ValueError("title must contain 1 to 120 characters")
        if any(character in title for character in ("\x00", "\r", "\n")):
            raise ValueError("title contains invalid characters")
        summary = str(arguments.get("summary", ""))
        if not summary or len(summary) > 2000:
            raise ValueError("summary must contain 1 to 2000 characters")
        payload = summary.encode("utf-8")
        collection_id = DocumentService._collection_id(
            str(arguments.get("collection_id", "default"))
        )
        principal.require_collection(collection_id)
        remote_idempotency_key = str(arguments.get("remote_idempotency_key", "")).strip()
        if (
            len(remote_idempotency_key) < 8
            or len(remote_idempotency_key) > 128
            or _REMOTE_IDEMPOTENCY_KEY.fullmatch(remote_idempotency_key) is None
        ):
            raise ValueError(
                "remote_idempotency_key must contain 8 to 128 URL-safe characters"
            )
        return {
            "title": title,
            "collection_id": collection_id,
            "remote_idempotency_key": remote_idempotency_key,
            "summary_sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
        }, payload

    def _ensure_work_item(
        self,
        principal: Principal,
        task_id: str,
        arguments: dict[str, Any],
        payload: bytes,
        *,
        request_hash: str,
        requested_by: str,
        approved_by: str,
    ) -> dict[str, Any]:
        try:
            summary = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConflictError("write task payload is not valid UTF-8") from exc
        work_item_id = f"wi-{uuid.uuid4().hex}"
        created_at = utc_now()
        record = {
            "id": work_item_id,
            "title": str(arguments["title"]),
            "collection_id": str(arguments["collection_id"]),
            "status": "open",
            "remote_idempotency_key": str(arguments["remote_idempotency_key"]),
            "idempotent_replay": False,
        }
        try:
            with self.database.connect() as conn:
                conn.execute(
                    "INSERT INTO work_items(id, tenant_id, collection_id, title, summary, "
                    "summary_sha256, remote_idempotency_key, request_hash, task_id, "
                    "created_by, approved_by, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        work_item_id,
                        principal.tenant_id,
                        record["collection_id"],
                        record["title"],
                        summary,
                        str(arguments["summary_sha256"]),
                        record["remote_idempotency_key"],
                        request_hash,
                        task_id,
                        requested_by or principal.user_id,
                        approved_by,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError:
            with self.database.connect() as conn:
                existing = conn.execute(
                    "SELECT id, request_hash FROM work_items "
                    "WHERE tenant_id = ? AND remote_idempotency_key = ?",
                    (principal.tenant_id, record["remote_idempotency_key"]),
                ).fetchone()
            if existing is None:
                raise
            if str(existing["request_hash"]) != request_hash:
                raise ConflictError(
                    "remote idempotency key was already used with a different work item"
                )
            return {
                "id": str(existing["id"]),
                "title": record["title"],
                "collection_id": record["collection_id"],
                "status": "open",
                "remote_idempotency_key": record["remote_idempotency_key"],
                "idempotent_replay": True,
            }
        return record

    @staticmethod
    def _safe_error(value: str, limit: int) -> str:
        return _SECRET.sub("[REDACTED]", value)[:limit]
