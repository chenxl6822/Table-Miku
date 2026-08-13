from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any

from .auth import ConflictError, PermissionDenied, Principal, ResourceNotFound
from .database import AssistantDatabase
from .documents import MAX_DOCUMENT_BYTES, DocumentService, PreparedDocument, request_digest, utc_now
from .embeddings import estimate_tokens
from .observability import TraceRecorder


MAX_ACTIVE_JOBS_PER_TENANT = 100
MAX_ACTIVE_JOBS_GLOBAL = 1_000
MAX_STAGED_BYTES_PER_TENANT = 100 * 1024 * 1024
MAX_STAGED_BYTES_GLOBAL = 512 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 2_000_000
MAX_CHUNKS = 5_000
MAX_JOB_ATTEMPTS = 3
MAX_IDEMPOTENCY_KEY_LENGTH = 200
WORKER_CLOSE_TIMEOUT_SECONDS = 1.0
WORKER_ERROR_BACKOFF_SECONDS = 0.1
WORKER_LEASE_SECONDS = 5.0
WORKER_HEARTBEAT_SECONDS = 0.5
CANCEL_POLL_INTERVAL_SECONDS = 0.05
PROGRESS_UPDATE_INTERVAL_SECONDS = 0.1
WORKER_LEASE_NAME = "knowledge-assistant-ingestion"
PROCESS_BOOT_ID = f"process-{os.getpid()}-{uuid.uuid4().hex}"
ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
LOGGER = logging.getLogger("table_miku.knowledge_assistant.ingestion")


class _CancellationRequested(RuntimeError):
    pass


class _ClaimLost(RuntimeError):
    pass


class _WorkerStopping(RuntimeError):
    pass


class _PayloadIntegrityError(RuntimeError):
    pass


class IngestionService:
    """Persistent, single-worker ingestion queue for one Knowledge Assistant service."""

    def __init__(
        self,
        database: AssistantDatabase,
        documents: DocumentService,
        traces: TraceRecorder,
    ) -> None:
        self.database = database
        self.documents = documents
        self.traces = traces
        self.service_instance_id = database.service_instance_id
        self.worker_owner_id = f"{PROCESS_BOOT_ID}-worker-{uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._started = False
        self._worker: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

    def start(self) -> None:
        """Recover interrupted rows and start exactly one bounded daemon worker."""

        with self._lifecycle_lock:
            if self._worker is not None:
                if self._worker.is_alive():
                    self._started = True
                    return
                self._worker = None
            heartbeat = self._heartbeat_thread
            if heartbeat is not None and heartbeat.is_alive():
                heartbeat.join(timeout=WORKER_CLOSE_TIMEOUT_SECONDS)
            self._heartbeat_thread = None
            if not self._acquire_worker_lease():
                raise ConflictError("ingestion worker is already owned by another service")
            try:
                self._recover_interrupted()
            except Exception:
                self._release_worker_lease()
                raise
            self._stop.clear()
            self._heartbeat_stop.clear()
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="knowledge-assistant-ingestion-heartbeat",
                daemon=True,
            )
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="knowledge-assistant-ingestion",
                daemon=True,
            )
            try:
                self._heartbeat_thread.start()
                self._worker.start()
                self._started = True
            except Exception:
                self._stop.set()
                self._heartbeat_stop.set()
                self._release_worker_lease()
                self._worker = None
                self._heartbeat_thread = None
                self._started = False
                raise

    def close(self, timeout: float = WORKER_CLOSE_TIMEOUT_SECONDS) -> bool:
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        with self._lifecycle_lock:
            worker = self._worker
            self._stop.set()
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lifecycle_lock:
            if self._worker is worker and (worker is None or not worker.is_alive()):
                self._worker = None
                self._heartbeat_stop.set()
                heartbeat = self._heartbeat_thread
                if heartbeat is not None and heartbeat.is_alive():
                    heartbeat.join(timeout=max(0.0, deadline - time.monotonic()))
                if heartbeat is not None and heartbeat.is_alive():
                    return False
                self._heartbeat_thread = None
                self._release_worker_lease()
                self._started = False
                return True
            return False

    def health(self) -> dict[str, Any]:
        """Return a read-only dispatch readiness snapshot without changing worker state."""

        with self._lifecycle_lock:
            started = self._started
            worker = self._worker
            heartbeat = self._heartbeat_thread
        worker_alive = bool(worker is not None and worker.is_alive())
        heartbeat_alive = bool(heartbeat is not None and heartbeat.is_alive())
        try:
            lease_owned = self._owns_worker_lease()
        except sqlite3.Error:
            lease_owned = False
        ready = started and worker_alive and heartbeat_alive and lease_owned
        return {
            "status": "ready" if ready else "degraded",
            "started": started,
            "worker_alive": worker_alive,
            "heartbeat_alive": heartbeat_alive,
            "lease_owned": lease_owned,
        }

    def create(
        self,
        principal: Principal,
        *,
        filename: str,
        content: bytes,
        collection_id: str = "default",
        idempotency_key: str,
    ) -> dict[str, Any]:
        principal.require("knowledge:write")
        safe_name = self.documents.parser.safe_filename(filename)
        collection_id = self.documents._collection_id(collection_id)
        principal.require_collection(collection_id)
        if not content:
            raise ValueError("document must not be empty")
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
        idempotency_key = idempotency_key.strip()
        if len(idempotency_key) < 8 or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError(
                f"idempotency_key must contain 8 to {MAX_IDEMPOTENCY_KEY_LENGTH} characters"
            )
        checksum = hashlib.sha256(content).hexdigest()
        request_hash = request_digest(
            {"filename": safe_name, "collection_id": collection_id, "checksum": checksum}
        )
        job_id = f"ingest-{uuid.uuid4().hex}"
        now = utc_now()
        try:
            with self.database.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM ingestion_jobs WHERE tenant_id = ? AND idempotency_key = ?",
                    (principal.tenant_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_hash"]) != request_hash:
                        raise ConflictError(
                            "idempotency key was already used with a different ingestion request"
                        )
                    result = self._public_job(existing)
                    result["idempotent_replay"] = True
                    return result
                active_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM ingestion_jobs WHERE tenant_id = ? "
                        "AND status IN ('queued', 'running', 'cancelling')",
                        (principal.tenant_id,),
                    ).fetchone()[0]
                )
                if active_count >= MAX_ACTIVE_JOBS_PER_TENANT:
                    raise ConflictError("tenant active ingestion job limit reached")
                global_active_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM ingestion_jobs "
                        "WHERE status IN ('queued', 'running', 'cancelling')"
                    ).fetchone()[0]
                )
                if global_active_count >= MAX_ACTIVE_JOBS_GLOBAL:
                    raise ConflictError("global active ingestion job limit reached")
                tenant_bytes = int(
                    conn.execute(
                        "SELECT COALESCE(SUM(length(p.content)), 0) FROM ingestion_payloads p "
                        "JOIN ingestion_jobs j ON j.id = p.job_id WHERE j.tenant_id = ?",
                        (principal.tenant_id,),
                    ).fetchone()[0]
                )
                global_bytes = int(
                    conn.execute(
                        "SELECT COALESCE(SUM(length(content)), 0) FROM ingestion_payloads"
                    ).fetchone()[0]
                )
                if tenant_bytes + len(content) > MAX_STAGED_BYTES_PER_TENANT:
                    raise ConflictError("tenant staged ingestion byte limit reached")
                if global_bytes + len(content) > MAX_STAGED_BYTES_GLOBAL:
                    raise ConflictError("global staged ingestion byte limit reached")
                conn.execute(
                    "INSERT INTO ingestion_jobs(id, tenant_id, requested_by, collection_id, filename, "
                    "checksum, byte_size, request_hash, idempotency_key, status, progress_phase, "
                    "created_at, updated_at, max_attempts) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?)",
                    (
                        job_id,
                        principal.tenant_id,
                        principal.user_id,
                        collection_id,
                        safe_name,
                        checksum,
                        len(content),
                        request_hash,
                        idempotency_key,
                        now,
                        now,
                        MAX_JOB_ATTEMPTS,
                    ),
                )
                conn.execute(
                    "INSERT INTO ingestion_payloads(job_id, content) VALUES(?, ?)",
                    (job_id, content),
                )
                row = conn.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            existing = self._existing_idempotent(principal, idempotency_key, request_hash)
            if existing is not None:
                existing["idempotent_replay"] = True
                return existing
            raise ConflictError("ingestion request conflicted with another request") from exc
        if row is None:
            raise RuntimeError("ingestion job could not be read after creation")
        result = self._public_job(row)
        result["idempotent_replay"] = False
        return result

    def get(self, principal: Principal, job_id: str) -> dict[str, Any]:
        principal.require("knowledge:read")
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ingestion_jobs WHERE id = ? AND tenant_id = ?",
                (job_id, principal.tenant_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFound("ingestion job not found")
        principal.require_collection(str(row["collection_id"]))
        return self._public_job(row)

    def list(self, principal: Principal, limit: int = 100) -> list[dict[str, Any]]:
        principal.require("knowledge:read")
        query = "SELECT * FROM ingestion_jobs WHERE tenant_id = ?"
        params: list[Any] = [principal.tenant_id]
        if principal.collection_ids is not None:
            if not principal.collection_ids:
                return []
            placeholders = ",".join("?" for _ in principal.collection_ids)
            query += f" AND collection_id IN ({placeholders})"
            params.extend(sorted(principal.collection_ids))
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 500))
        with self.database.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._public_job(row) for row in rows]

    def cancel(self, principal: Principal, job_id: str) -> dict[str, Any]:
        principal.require("knowledge:write")
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM ingestion_jobs WHERE id = ? AND tenant_id = ?",
                (job_id, principal.tenant_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("ingestion job not found")
            principal.require_collection(str(row["collection_id"]))
            if str(row["requested_by"]) != principal.user_id:
                raise PermissionDenied("only the ingestion requester can cancel this job")
            now = utc_now()
            status = str(row["status"])
            if status == "queued":
                conn.execute(
                    "UPDATE ingestion_jobs SET status = 'cancelled', progress_phase = 'cancelled', "
                    "error_code = 'cancelled_by_requester', error_message = 'Cancelled by requester.', "
                    "cancel_requested_at = COALESCE(cancel_requested_at, ?), "
                    "cancel_outcome = 'cancelled', updated_at = ?, finished_at = ? WHERE id = ?",
                    (now, now, now, job_id),
                )
                conn.execute("DELETE FROM ingestion_payloads WHERE job_id = ?", (job_id,))
            elif status == "running":
                conn.execute(
                    "UPDATE ingestion_jobs SET status = 'cancelling', progress_phase = 'cancelling', "
                    "cancel_requested_at = COALESCE(cancel_requested_at, ?), "
                    "cancel_outcome = 'requested', updated_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (now, now, job_id),
                )
            elif status == "cancelling":
                conn.execute(
                    "UPDATE ingestion_jobs SET cancel_requested_at = COALESCE(cancel_requested_at, ?), "
                    "cancel_outcome = 'requested', updated_at = ? WHERE id = ?",
                    (now, now, job_id),
                )
            else:
                outcome = "too_late" if status == "succeeded" else "already_terminal"
                conn.execute(
                    "UPDATE ingestion_jobs SET cancel_requested_at = COALESCE(cancel_requested_at, ?), "
                    "cancel_outcome = ?, updated_at = ? WHERE id = ?",
                    (now, outcome, now, job_id),
                )
            updated = conn.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        if updated is None:
            raise RuntimeError("ingestion job disappeared during cancellation")
        return self._public_job(updated)

    def run_one(self, job_id: str | None = None) -> dict[str, Any] | None:
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            ephemeral_lease = False
            if not self._owns_worker_lease():
                if not self._acquire_worker_lease():
                    return None
                ephemeral_lease = True
                try:
                    self._recover_interrupted()
                except Exception:
                    self._release_worker_lease()
                    raise
            try:
                return self._run_one_owned(job_id)
            finally:
                if ephemeral_lease:
                    self._release_worker_lease()
        finally:
            self._run_lock.release()

    def _run_one_owned(self, job_id: str | None) -> dict[str, Any] | None:
        claimed = self._claim(job_id)
        if claimed is None:
            return None
        row, content, run_token = claimed
        job_id = str(row["id"])
        principal = Principal(
            tenant_id=str(row["tenant_id"]),
            user_id=str(row["requested_by"]),
            roles=frozenset({"editor"}),
            collection_ids=frozenset({str(row["collection_id"])}),
        )
        last_cancel_check = 0.0
        last_progress_update = 0.0
        last_progress_phase = ""

        def check(*, force: bool = False) -> None:
            nonlocal last_cancel_check
            if self._stop.is_set():
                raise _WorkerStopping()
            checked_at = time.monotonic()
            if not force and checked_at - last_cancel_check < CANCEL_POLL_INTERVAL_SECONDS:
                return
            last_cancel_check = checked_at
            self._check_claim(job_id, run_token)

        def report(phase: str, current: int, total: int) -> None:
            nonlocal last_progress_phase, last_progress_update
            reported_at = time.monotonic()
            phase_changed = phase != last_progress_phase
            completed = int(current) >= max(1, int(total))
            if (
                not phase_changed
                and not completed
                and reported_at - last_progress_update < PROGRESS_UPDATE_INTERVAL_SECONDS
            ):
                return
            last_progress_phase = phase
            last_progress_update = reported_at
            self._update_progress(job_id, run_token, phase, current, total)

        try:
            with self.traces.trace(
                "ingestion.job",
                principal,
                {"job_id": job_id, "collection_id": str(row["collection_id"])},
            ) as trace:
                self._set_trace_id(job_id, run_token, trace.trace_id)
                if hashlib.sha256(content).hexdigest() != str(row["checksum"]):
                    raise _PayloadIntegrityError()
                trace.add_tokens(
                    input_tokens=estimate_tokens(content.decode("utf-8", errors="ignore"))
                )
                with trace.span("ingestion.prepare"):
                    prepared = self.documents.prepare_index(
                        str(row["filename"]),
                        content,
                        max_extracted_characters=MAX_EXTRACTED_CHARACTERS,
                        max_chunks=MAX_CHUNKS,
                        cancel_check=check,
                        progress=report,
                    )
                check(force=True)
                with trace.span("ingestion.commit"):
                    return self._commit_success(row, content, run_token, principal, prepared)
        except _CancellationRequested:
            self._finish_cancel(job_id, run_token)
        except _WorkerStopping:
            self._release_for_shutdown(job_id, run_token)
        except _ClaimLost:
            pass
        except sqlite3.Error as exc:
            try:
                self._requeue_after_database_error(job_id, run_token)
            except _ClaimLost:
                pass
            raise exc
        except Exception as exc:
            self._finish_failed(job_id, run_token, exc)
        return self._get_unscoped(job_id)

    def _worker_loop(self) -> None:
        release_lease = True
        try:
            while not self._stop.is_set():
                try:
                    if self.run_one() is None:
                        self._stop.wait(0.1)
                except _ClaimLost:
                    # Another owner fenced this service. Stop both loops; silently leaving a
                    # live heartbeat beside a dead worker would misreport queue availability.
                    self._stop.set()
                    self._heartbeat_stop.set()
                    release_lease = False
                    return
                except sqlite3.Error as exc:
                    LOGGER.warning("transient ingestion database error: %s", type(exc).__name__)
                    if self._stop.wait(WORKER_ERROR_BACKOFF_SECONDS):
                        continue
                    try:
                        self._recover_interrupted()
                    except sqlite3.Error as recovery_exc:
                        LOGGER.warning(
                            "transient ingestion recovery database error: %s",
                            type(recovery_exc).__name__,
                        )
        finally:
            self._heartbeat_stop.set()
            if release_lease:
                try:
                    self._release_worker_lease()
                except sqlite3.Error as exc:
                    LOGGER.warning(
                        "ingestion worker lease release database error: %s", type(exc).__name__
                    )

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(WORKER_HEARTBEAT_SECONDS):
            try:
                if not self._renew_worker_lease():
                    self._stop.set()
                    return
            except sqlite3.Error as exc:
                LOGGER.warning("transient ingestion heartbeat database error: %s", type(exc).__name__)

    def _acquire_worker_lease(self) -> bool:
        now = time.time()
        lease_expires_at = now + WORKER_LEASE_SECONDS
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_id, lease_expires_at FROM worker_leases WHERE name = ?",
                (WORKER_LEASE_NAME,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO worker_leases(name, owner_id, process_boot_id, heartbeat_at, "
                    "lease_expires_at) VALUES(?, ?, ?, ?, ?)",
                    (
                        WORKER_LEASE_NAME,
                        self.worker_owner_id,
                        PROCESS_BOOT_ID,
                        now,
                        lease_expires_at,
                    ),
                )
                return True
            if str(row["owner_id"]) == self.worker_owner_id or float(
                row["lease_expires_at"]
            ) <= now:
                conn.execute(
                    "UPDATE worker_leases SET owner_id = ?, process_boot_id = ?, heartbeat_at = ?, "
                    "lease_expires_at = ? WHERE name = ?",
                    (
                        self.worker_owner_id,
                        PROCESS_BOOT_ID,
                        now,
                        lease_expires_at,
                        WORKER_LEASE_NAME,
                    ),
                )
                return True
        return False

    def _renew_worker_lease(self) -> bool:
        now = time.time()
        with self.database.connect() as conn:
            updated = conn.execute(
                "UPDATE worker_leases SET heartbeat_at = ?, lease_expires_at = ? "
                "WHERE name = ? AND owner_id = ?",
                (
                    now,
                    now + WORKER_LEASE_SECONDS,
                    WORKER_LEASE_NAME,
                    self.worker_owner_id,
                ),
            )
        return updated.rowcount == 1

    def _release_worker_lease(self) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "DELETE FROM worker_leases WHERE name = ? AND owner_id = ?",
                (WORKER_LEASE_NAME, self.worker_owner_id),
            )

    def _owns_worker_lease(self) -> bool:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT owner_id, lease_expires_at FROM worker_leases WHERE name = ?",
                (WORKER_LEASE_NAME,),
            ).fetchone()
        return bool(
            row is not None
            and str(row["owner_id"]) == self.worker_owner_id
            and float(row["lease_expires_at"]) > time.time()
        )

    def _assert_worker_lease(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT owner_id FROM worker_leases WHERE name = ?",
            (WORKER_LEASE_NAME,),
        ).fetchone()
        # Expiry permits a different owner to take the row; it does not by itself revoke the
        # unchanged owner. BEGIN IMMEDIATE gates every terminal mutation, so an owner-id match
        # is the fencing decision and avoids abandoning work after a host sleep/clock jump.
        if row is None or str(row["owner_id"]) != self.worker_owner_id:
            raise _ClaimLost()

    def _claim(self, job_id: str | None) -> tuple[sqlite3.Row, bytes, str] | None:
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_worker_lease(conn)
            if job_id is None:
                row = conn.execute(
                    "SELECT * FROM ingestion_jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM ingestion_jobs WHERE id = ? AND status = 'queued'", (job_id,)
                ).fetchone()
            if row is None:
                return None
            run_token = uuid.uuid4().hex
            now = utc_now()
            updated = conn.execute(
                "UPDATE ingestion_jobs SET status = 'running', progress_phase = 'starting', "
                "attempt_count = attempt_count + 1, run_token = ?, worker_instance_id = ?, "
                "retryable = 0, started_at = ?, updated_at = ?, error_code = '', error_message = '' "
                "WHERE id = ? AND status = 'queued'",
                (run_token, self.worker_owner_id, now, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            payload = conn.execute(
                "SELECT content FROM ingestion_payloads WHERE job_id = ?", (row["id"],)
            ).fetchone()
            claimed_row = conn.execute(
                "SELECT * FROM ingestion_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
        if payload is None or claimed_row is None:
            self._finish_failed(str(row["id"]), run_token, RuntimeError("missing payload"))
            return None
        return claimed_row, bytes(payload["content"]), run_token

    def _check_claim(self, job_id: str, run_token: str) -> None:
        with self.database.connect() as conn:
            self._assert_worker_lease(conn)
            row = conn.execute(
                "SELECT status, run_token, worker_instance_id FROM ingestion_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if (
            row is None
            or str(row["run_token"]) != run_token
            or str(row["worker_instance_id"]) != self.worker_owner_id
        ):
            raise _ClaimLost()
        if row["status"] == "cancelling":
            raise _CancellationRequested()
        if row["status"] != "running":
            raise _ClaimLost()

    def _update_progress(
        self, job_id: str, run_token: str, phase: str, current: int, total: int
    ) -> None:
        with self.database.connect() as conn:
            updated = conn.execute(
                "UPDATE ingestion_jobs SET progress_phase = ?, progress_current = ?, progress_total = ?, "
                "updated_at = ? WHERE id = ? AND run_token = ? AND status = 'running' "
                "AND worker_instance_id = ? AND EXISTS (SELECT 1 FROM worker_leases "
                "WHERE name = ? AND owner_id = ?)",
                (
                    phase[:40],
                    max(0, int(current)),
                    max(1, int(total)),
                    utc_now(),
                    job_id,
                    run_token,
                    self.worker_owner_id,
                    WORKER_LEASE_NAME,
                    self.worker_owner_id,
                ),
            )
        if updated.rowcount != 1:
            self._check_claim(job_id, run_token)

    def _set_trace_id(self, job_id: str, run_token: str, trace_id: str) -> None:
        with self.database.connect() as conn:
            updated = conn.execute(
                "UPDATE ingestion_jobs SET trace_id = ?, updated_at = ? "
                "WHERE id = ? AND run_token = ? AND status = 'running' AND worker_instance_id = ? "
                "AND EXISTS (SELECT 1 FROM worker_leases WHERE name = ? AND owner_id = ?)",
                (
                    trace_id,
                    utc_now(),
                    job_id,
                    run_token,
                    self.worker_owner_id,
                    WORKER_LEASE_NAME,
                    self.worker_owner_id,
                ),
            )
        if updated.rowcount != 1:
            self._check_claim(job_id, run_token)

    def _commit_success(
        self,
        row: sqlite3.Row,
        content: bytes,
        run_token: str,
        principal: Principal,
        prepared: PreparedDocument,
    ) -> dict[str, Any]:
        job_id = str(row["id"])
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_worker_lease(conn)
            current = conn.execute(
                "SELECT status, run_token, worker_instance_id FROM ingestion_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if (
                current is None
                or str(current["run_token"]) != run_token
                or str(current["worker_instance_id"]) != self.worker_owner_id
            ):
                raise _ClaimLost()
            if current["status"] == "cancelling":
                now = utc_now()
                conn.execute(
                    "UPDATE ingestion_jobs SET status = 'cancelled', progress_phase = 'cancelled', "
                    "error_code = 'cancelled_by_requester', error_message = 'Cancelled by requester.', "
                    "cancel_outcome = 'cancelled', run_token = '', worker_instance_id = '', "
                    "updated_at = ?, finished_at = ? WHERE id = ?",
                    (now, now, job_id),
                )
                conn.execute("DELETE FROM ingestion_payloads WHERE job_id = ?", (job_id,))
            elif current["status"] == "running":
                document_id, deduplicated = self.documents.persist_prepared(
                    conn,
                    principal,
                    filename=str(row["filename"]),
                    content=content,
                    collection_id=str(row["collection_id"]),
                    checksum=str(row["checksum"]),
                    prepared=prepared,
                )
                now = utc_now()
                updated = conn.execute(
                    "UPDATE ingestion_jobs SET status = 'succeeded', progress_phase = 'completed', "
                    "progress_current = progress_total, document_id = ?, deduplicated = ?, "
                    "run_token = '', worker_instance_id = '', updated_at = ?, finished_at = ? "
                    "WHERE id = ? AND status = 'running' AND run_token = ?",
                    (document_id, int(deduplicated), now, now, job_id, run_token),
                )
                if updated.rowcount != 1:
                    raise _ClaimLost()
                conn.execute("DELETE FROM ingestion_payloads WHERE job_id = ?", (job_id,))
            else:
                raise _ClaimLost()
            result = conn.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        if result is None:
            raise RuntimeError("ingestion job disappeared during completion")
        return self._public_job(result)

    def _finish_cancel(self, job_id: str, run_token: str) -> None:
        now = utc_now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_worker_lease(conn)
            except _ClaimLost:
                return
            conn.execute(
                "UPDATE ingestion_jobs SET status = 'cancelled', progress_phase = 'cancelled', "
                "error_code = 'cancelled_by_requester', error_message = 'Cancelled by requester.', "
                "cancel_outcome = 'cancelled', run_token = '', worker_instance_id = '', "
                "updated_at = ?, finished_at = ? "
                "WHERE id = ? AND run_token = ? AND status IN ('running', 'cancelling')",
                (now, now, job_id, run_token),
            )
            conn.execute(
                "DELETE FROM ingestion_payloads WHERE job_id = ? AND EXISTS "
                "(SELECT 1 FROM ingestion_jobs WHERE id = ? AND status = 'cancelled')",
                (job_id, job_id),
            )

    def _release_for_shutdown(self, job_id: str, run_token: str) -> None:
        now = utc_now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_worker_lease(conn)
            except _ClaimLost:
                return
            row = conn.execute(
                "SELECT status FROM ingestion_jobs WHERE id = ? AND run_token = ?", (job_id, run_token)
            ).fetchone()
            if row is None:
                return
            if row["status"] == "cancelling":
                conn.execute(
                    "UPDATE ingestion_jobs SET status = 'cancelled', progress_phase = 'cancelled', "
                    "error_code = 'cancelled_by_requester', error_message = 'Cancelled by requester.', "
                    "cancel_outcome = 'cancelled', run_token = '', worker_instance_id = '', "
                    "updated_at = ?, finished_at = ? WHERE id = ?",
                    (now, now, job_id),
                )
                conn.execute("DELETE FROM ingestion_payloads WHERE job_id = ?", (job_id,))
            else:
                conn.execute(
                    "UPDATE ingestion_jobs SET status = 'queued', progress_phase = 'queued', "
                    "attempt_count = MAX(0, attempt_count - 1), run_token = '', worker_instance_id = '', "
                    "started_at = NULL, updated_at = ? WHERE id = ? AND status = 'running'",
                    (now, job_id),
                )

    def _finish_failed(self, job_id: str, run_token: str, exc: Exception) -> None:
        if isinstance(exc, _PayloadIntegrityError):
            code, message = "payload_integrity_failed", "Staged document integrity check failed."
        elif isinstance(exc, ValueError) and "extracted text limit" in str(exc):
            code, message = "extracted_text_limit", "Extracted text exceeds the safety limit."
        elif isinstance(exc, ValueError) and "chunk count limit" in str(exc):
            code, message = "chunk_limit", "Document produces too many chunks."
        elif isinstance(exc, ValueError):
            code, message = "invalid_document", "Document validation or parsing failed."
        elif isinstance(exc, ConflictError):
            code, message = "document_conflict", "Document could not be committed due to a conflict."
        else:
            code, message = "processing_failed", "Document processing failed."
        now = utc_now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_worker_lease(conn)
            except _ClaimLost:
                return
            row = conn.execute(
                "SELECT status FROM ingestion_jobs WHERE id = ? AND run_token = ?",
                (job_id, run_token),
            ).fetchone()
            if row is None:
                return
            if row["status"] == "cancelling":
                conn.execute(
                    "UPDATE ingestion_jobs SET status = 'cancelled', progress_phase = 'cancelled', "
                    "retryable = 0, error_code = 'cancelled_by_requester', "
                    "error_message = 'Cancelled by requester.', cancel_outcome = 'cancelled', "
                    "run_token = '', worker_instance_id = '', "
                    "updated_at = ?, finished_at = ? WHERE id = ? AND run_token = ?",
                    (now, now, job_id, run_token),
                )
            elif row["status"] == "running":
                conn.execute(
                    "UPDATE ingestion_jobs SET status = 'failed', progress_phase = 'failed', retryable = 0, "
                    "error_code = ?, error_message = ?, run_token = '', worker_instance_id = '', "
                    "updated_at = ?, finished_at = ? WHERE id = ? AND run_token = ?",
                    (code, message, now, now, job_id, run_token),
                )
            conn.execute(
                "DELETE FROM ingestion_payloads WHERE job_id = ? AND EXISTS "
                "(SELECT 1 FROM ingestion_jobs WHERE id = ? AND status IN ('failed', 'cancelled'))",
                (job_id, job_id),
            )

    def _requeue_after_database_error(self, job_id: str, run_token: str) -> None:
        now = utc_now()
        last_error: sqlite3.Error | None = None
        for _attempt in range(3):
            try:
                with self.database.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._assert_worker_lease(conn)
                    row = conn.execute(
                        "SELECT status, attempt_count, max_attempts FROM ingestion_jobs "
                        "WHERE id = ? AND run_token = ? AND worker_instance_id = ?",
                        (job_id, run_token, self.worker_owner_id),
                    ).fetchone()
                    if row is None:
                        raise _ClaimLost()
                    if row["status"] == "cancelling":
                        conn.execute(
                            "UPDATE ingestion_jobs SET status = 'cancelled', "
                            "progress_phase = 'cancelled', retryable = 0, "
                            "error_code = 'cancelled_by_requester', "
                            "error_message = 'Cancelled by requester.', "
                            "cancel_outcome = 'cancelled', run_token = '', "
                            "worker_instance_id = '', updated_at = ?, finished_at = ? "
                            "WHERE id = ? AND run_token = ?",
                            (now, now, job_id, run_token),
                        )
                        conn.execute("DELETE FROM ingestion_payloads WHERE job_id = ?", (job_id,))
                    elif row["status"] == "running" and int(row["attempt_count"]) < int(
                        row["max_attempts"]
                    ):
                        conn.execute(
                            "UPDATE ingestion_jobs SET status = 'queued', progress_phase = 'queued', "
                            "retryable = 1, error_code = 'transient_database_error', "
                            "error_message = 'A transient database error interrupted processing; retry queued.', "
                            "run_token = '', worker_instance_id = '', started_at = NULL, updated_at = ? "
                            "WHERE id = ? AND run_token = ? AND status = 'running'",
                            (now, job_id, run_token),
                        )
                    elif row["status"] == "running":
                        conn.execute(
                            "UPDATE ingestion_jobs SET status = 'failed', progress_phase = 'failed', "
                            "retryable = 0, "
                            "error_code = 'transient_database_retries_exhausted', "
                            "error_message = 'Transient database retries were exhausted; "
                            "create a new job after review.', run_token = '', worker_instance_id = '', "
                            "updated_at = ?, finished_at = ? "
                            "WHERE id = ? AND run_token = ? AND status = 'running'",
                            (now, now, job_id, run_token),
                        )
                        conn.execute("DELETE FROM ingestion_payloads WHERE job_id = ?", (job_id,))
                return
            except sqlite3.Error as exc:
                last_error = exc
                time.sleep(WORKER_ERROR_BACKOFF_SECONDS)
        if last_error is not None:
            raise last_error

    def _recover_interrupted(self) -> None:
        now = utc_now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_worker_lease(conn)
            interrupted = conn.execute(
                "SELECT j.id, j.tenant_id, j.status, j.attempt_count, j.max_attempts, j.trace_id "
                "FROM ingestion_jobs j LEFT JOIN traces t "
                "ON t.id = j.trace_id AND t.tenant_id = j.tenant_id "
                "WHERE j.status IN ('running', 'cancelling') "
                "OR (j.status = 'queued' AND t.status = 'running')"
            ).fetchall()
            terminal_with_running_trace = conn.execute(
                "SELECT j.tenant_id, j.status, j.trace_id FROM ingestion_jobs j "
                "JOIN traces t ON t.id = j.trace_id AND t.tenant_id = j.tenant_id "
                "WHERE j.status IN ('succeeded', 'failed', 'cancelled') AND t.status = 'running'"
            ).fetchall()
            cancelling_ids = [str(row["id"]) for row in interrupted if row["status"] == "cancelling"]
            recoverable_ids = [
                str(row["id"])
                for row in interrupted
                if row["status"] == "running" and int(row["attempt_count"]) < int(row["max_attempts"])
            ]
            exhausted_ids = [
                str(row["id"])
                for row in interrupted
                if row["status"] == "running" and int(row["attempt_count"]) >= int(row["max_attempts"])
            ]
            for row in interrupted:
                trace_id = str(row["trace_id"] or "")
                if not trace_id:
                    continue
                if row["status"] == "cancelling":
                    trace_error = "cancelled_by_requester"
                elif int(row["attempt_count"]) >= int(row["max_attempts"]):
                    trace_error = "interrupted_retries_exhausted"
                else:
                    trace_error = "interrupted_requeued"
                trace_owner = conn.execute(
                    "SELECT tenant_id FROM traces WHERE id = ?", (trace_id,)
                ).fetchone()
                if trace_owner is None or str(trace_owner["tenant_id"]) != str(row["tenant_id"]):
                    continue
                conn.execute(
                    "UPDATE traces SET status = 'error', finished_at = ?, "
                    "latency_ms = COALESCE(MAX(0.0, "
                    "(julianday(?) - julianday(started_at)) * 86400000.0), 0.0), error_code = ? "
                    "WHERE id = ? AND tenant_id = ? AND status = 'running'",
                    (now, now, trace_error, trace_id, str(row["tenant_id"])),
                )
                conn.execute(
                    "UPDATE spans SET status = 'error', finished_at = ?, "
                    "latency_ms = COALESCE(MAX(0.0, "
                    "(julianday(?) - julianday(started_at)) * 86400000.0), 0.0) "
                    "WHERE trace_id = ? AND status = 'running'",
                    (now, now, trace_id),
                )
            for row in terminal_with_running_trace:
                trace_id = str(row["trace_id"])
                terminal_status = "ok" if row["status"] == "succeeded" else "error"
                error_code = "" if terminal_status == "ok" else f"ingestion_{row['status']}"
                conn.execute(
                    "UPDATE traces SET status = ?, finished_at = ?, "
                    "latency_ms = COALESCE(MAX(0.0, "
                    "(julianday(?) - julianday(started_at)) * 86400000.0), 0.0), error_code = ? "
                    "WHERE id = ? AND tenant_id = ? AND status = 'running'",
                    (
                        terminal_status,
                        now,
                        now,
                        error_code,
                        trace_id,
                        str(row["tenant_id"]),
                    ),
                )
                conn.execute(
                    "UPDATE spans SET status = ?, finished_at = ?, "
                    "latency_ms = COALESCE(MAX(0.0, "
                    "(julianday(?) - julianday(started_at)) * 86400000.0), 0.0) "
                    "WHERE trace_id = ? AND status = 'running'",
                    (terminal_status, now, now, trace_id),
                )
            if cancelling_ids:
                placeholders = ",".join("?" for _ in cancelling_ids)
                conn.execute(
                    f"UPDATE ingestion_jobs SET status = 'cancelled', progress_phase = 'cancelled', "
                    f"error_code = 'cancelled_by_requester', error_message = 'Cancelled by requester.', "
                    f"cancel_outcome = 'cancelled', run_token = '', worker_instance_id = '', "
                    f"updated_at = ?, finished_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, now, *cancelling_ids),
                )
                conn.execute(
                    f"DELETE FROM ingestion_payloads WHERE job_id IN ({placeholders})", cancelling_ids
                )
            if recoverable_ids:
                placeholders = ",".join("?" for _ in recoverable_ids)
                conn.execute(
                    f"UPDATE ingestion_jobs SET status = 'queued', progress_phase = 'queued', "
                    f"run_token = '', worker_instance_id = '', started_at = NULL, updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, *recoverable_ids),
                )
            if exhausted_ids:
                placeholders = ",".join("?" for _ in exhausted_ids)
                conn.execute(
                    f"UPDATE ingestion_jobs SET status = 'failed', progress_phase = 'failed', "
                    f"retryable = 0, error_code = 'interrupted_retries_exhausted', "
                    f"error_message = 'Processing was interrupted repeatedly; create a new job after review.', "
                    f"run_token = '', worker_instance_id = '', updated_at = ?, finished_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, now, *exhausted_ids),
                )
                conn.execute(
                    f"DELETE FROM ingestion_payloads WHERE job_id IN ({placeholders})", exhausted_ids
                )

    def _existing_idempotent(
        self, principal: Principal, idempotency_key: str, request_hash: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ingestion_jobs WHERE tenant_id = ? AND idempotency_key = ?",
                (principal.tenant_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise ConflictError("idempotency key was already used with a different ingestion request")
        return self._public_job(row)

    def _get_unscoped(self, job_id: str) -> dict[str, Any]:
        with self.database.connect() as conn:
            row = conn.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ResourceNotFound("ingestion job not found")
        return self._public_job(row)

    @staticmethod
    def _public_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "tenant_id": str(row["tenant_id"]),
            "requested_by": str(row["requested_by"]),
            "collection_id": str(row["collection_id"]),
            "filename": str(row["filename"]),
            "checksum": str(row["checksum"]),
            "byte_size": int(row["byte_size"]),
            "status": str(row["status"]),
            "progress": {
                "phase": str(row["progress_phase"]),
                "current": int(row["progress_current"]),
                "total": int(row["progress_total"]),
            },
            "attempt_count": int(row["attempt_count"]),
            "max_attempts": int(row["max_attempts"]),
            "retryable": bool(row["retryable"]),
            "error_code": str(row["error_code"]),
            "error_message": str(row["error_message"]),
            "trace_id": str(row["trace_id"]) if row["trace_id"] else None,
            "document_id": str(row["document_id"]) if row["document_id"] else None,
            "deduplicated": bool(row["deduplicated"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "started_at": str(row["started_at"]) if row["started_at"] else None,
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
            "cancel_requested_at": (
                str(row["cancel_requested_at"]) if row["cancel_requested_at"] else None
            ),
            "cancel_outcome": str(row["cancel_outcome"]),
        }
