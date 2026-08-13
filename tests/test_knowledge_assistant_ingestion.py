from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

import table_miku.knowledge_assistant.ingestion as ingestion_module
from table_miku.knowledge_assistant.auth import ConflictError, PermissionDenied, Principal, ResourceNotFound
from table_miku.knowledge_assistant.database import AssistantDatabase, SCHEMA_VERSION
from table_miku.knowledge_assistant.documents import DocumentChunk, PreparedDocument
from table_miku.knowledge_assistant.embeddings import estimate_tokens
from table_miku.knowledge_assistant.service import KnowledgeAssistantService


def editor(
    *,
    tenant: str = "tenant-a",
    user: str = "editor-a",
    collections: frozenset[str] | None = None,
) -> Principal:
    return Principal(
        tenant_id=tenant,
        user_id=user,
        roles=frozenset({"editor"}),
        collection_ids=collections,
    )


def test_schema_v2_has_stable_service_instance_id(tmp_path: Path):
    path = tmp_path / "assistant.db"
    first = AssistantDatabase(path)
    first_id = first.service_instance_id
    second = AssistantDatabase(path)

    assert SCHEMA_VERSION == 2
    assert first_id.startswith("ka-")
    assert second.service_instance_id == first_id
    with second.connect() as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'ingestion_jobs'"
        ).fetchone()[0] == 1


def test_principal_and_ingestion_fields_have_bounded_lengths(tmp_path: Path):
    with pytest.raises(ValueError, match="tenant_id"):
        editor(tenant="t" * 121)
    with pytest.raises(ValueError, match="user_id"):
        editor(user="u" * 121)
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    with pytest.raises(ValueError, match="filename"):
        service.ingestion.create(
            editor(),
            filename=f"{'f' * 241}.txt",
            content=b"bounded",
            idempotency_key="bounded-filename-001",
        )
    with pytest.raises(ValueError, match="idempotency_key"):
        service.ingestion.create(
            editor(),
            filename="bounded.txt",
            content=b"bounded",
            idempotency_key="k" * 201,
        )


def test_schema_v1_database_migrates_without_discarding_existing_documents(tmp_path: Path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_versions(version, applied_at) VALUES(1, 'legacy');
            CREATE TABLE documents(
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, collection_id TEXT NOT NULL,
                filename TEXT NOT NULL, content_type TEXT NOT NULL, checksum TEXT NOT NULL,
                byte_size INTEGER NOT NULL, status TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO documents(
                id, tenant_id, collection_id, filename, content_type, checksum, byte_size,
                status, created_by, created_at, updated_at
            ) VALUES(
                'doc-legacy', 'tenant-a', 'default', 'legacy.txt', 'text/plain',
                'legacy-checksum', 6, 'indexed', 'editor-a', 'legacy', 'legacy'
            );
            """
        )

    migrated = AssistantDatabase(path)

    assert migrated.service_instance_id.startswith("ka-")
    with migrated.connect() as conn:
        assert conn.execute("SELECT filename FROM documents WHERE id = 'doc-legacy'").fetchone()[0] == (
            "legacy.txt"
        )
        assert conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 2


def test_create_run_and_permanent_idempotency_binding(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor(collections=frozenset({"engineering"}))
    content = b"# Async ingestion\nThe final commit is atomic."

    created = service.ingestion.create(
        actor,
        filename="async.md",
        content=content,
        collection_id="engineering",
        idempotency_key="async-ingest-001",
    )
    queued_replay = service.ingestion.create(
        actor,
        filename="async.md",
        content=content,
        collection_id="engineering",
        idempotency_key="async-ingest-001",
    )

    assert created["status"] == "queued"
    assert created["idempotent_replay"] is False
    assert "idempotency_key" not in created
    assert queued_replay["id"] == created["id"]
    assert queued_replay["idempotent_replay"] is True
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0

    completed = service.ingestion.run_one(created["id"])

    assert completed is not None
    assert completed["status"] == "succeeded"
    assert completed["document_id"]
    assert completed["trace_id"]
    assert service.documents.get_document(actor, completed["document_id"])["status"] == "indexed"
    trace = service.traces.get_trace(editor(), completed["trace_id"])
    assert trace["operation"] == "ingestion.job"
    expected_input_tokens = estimate_tokens(content.decode("utf-8", errors="ignore"))
    assert trace["input_tokens"] == expected_input_tokens
    assert content.decode() not in str(trace["attributes"])
    assert [span["name"] for span in trace["spans"]] == [
        "ingestion.prepare",
        "ingestion.commit",
    ]
    metrics = service.traces.metrics(editor())
    assert metrics["tokens"]["input"] == expected_input_tokens
    assert metrics["tokens"]["input"] > 0
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_payloads").fetchone()[0] == 0
    final_replay = service.ingestion.create(
        actor,
        filename="async.md",
        content=content,
        collection_id="engineering",
        idempotency_key="async-ingest-001",
    )
    assert final_replay["id"] == created["id"]
    assert final_replay["status"] == "succeeded"

    with pytest.raises(ConflictError, match="different ingestion request"):
        service.ingestion.create(
            actor,
            filename="different.md",
            content=b"different",
            collection_id="engineering",
            idempotency_key="async-ingest-001",
        )


def test_running_cancel_wins_before_atomic_document_commit(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="cancel.md",
        content=b"content that must never become visible",
        idempotency_key="cancel-running-001",
    )
    prepared = threading.Event()
    release = threading.Event()
    original_prepare = service.documents.prepare_index

    def delayed_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        prepared.set()
        assert release.wait(timeout=3)
        return result

    monkeypatch.setattr(service.documents, "prepare_index", delayed_prepare)
    result: list[dict | None] = []
    worker = threading.Thread(target=lambda: result.append(service.ingestion.run_one(job["id"])))
    worker.start()
    assert prepared.wait(timeout=3)

    cancelling = service.ingestion.cancel(actor, job["id"])
    assert cancelling["status"] == "cancelling"
    assert cancelling["cancel_outcome"] == "requested"
    release.set()
    worker.join(timeout=3)

    assert result[0] is not None
    assert result[0]["status"] == "cancelled"
    assert result[0]["cancel_outcome"] == "cancelled"
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ingestion_payloads").fetchone()[0] == 0


def test_same_job_can_only_be_claimed_by_one_worker(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="single-claim.txt",
        content=b"only one worker may index this",
        idempotency_key="single-claim-001",
    )
    entered = threading.Event()
    release = threading.Event()
    original_prepare = service.documents.prepare_index

    def delayed_prepare(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(service.documents, "prepare_index", delayed_prepare)
    results: list[dict | None] = []
    first = threading.Thread(target=lambda: results.append(service.ingestion.run_one(job["id"])))
    first.start()
    assert entered.wait(timeout=3)

    assert service.ingestion.run_one(job["id"]) is None
    release.set()
    first.join(timeout=3)

    assert results[0] is not None and results[0]["status"] == "succeeded"
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_missing_or_tampered_staged_payload_fails_closed(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    missing = service.ingestion.create(
        actor,
        filename="missing.txt",
        content=b"missing payload",
        idempotency_key="missing-payload-001",
    )
    tampered = service.ingestion.create(
        actor,
        filename="tampered.txt",
        content=b"original payload",
        idempotency_key="tampered-payload-001",
    )
    with service.database.connect() as conn:
        conn.execute("DELETE FROM ingestion_payloads WHERE job_id = ?", (missing["id"],))
        conn.execute(
            "UPDATE ingestion_payloads SET content = ? WHERE job_id = ?",
            (b"substituted secret bytes", tampered["id"]),
        )

    assert service.ingestion.run_one(missing["id"]) is None
    missing_result = service.ingestion.get(actor, missing["id"])
    tampered_result = service.ingestion.run_one(tampered["id"])

    assert missing_result["status"] == "failed"
    assert missing_result["error_code"] == "processing_failed"
    assert tampered_result is not None and tampered_result["status"] == "failed"
    assert tampered_result["error_code"] == "payload_integrity_failed"
    assert "substituted" not in tampered_result["error_message"]
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_stale_run_token_cannot_commit_prepared_document(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="stale.txt",
        content=b"stale worker result",
        idempotency_key="stale-token-001",
    )
    assert service.ingestion._acquire_worker_lease()
    try:
        claimed = service.ingestion._claim(job["id"])
        assert claimed is not None
        row, content, stale_token = claimed
        prepared = service.documents.prepare_index(
            "stale.txt",
            content,
            max_extracted_characters=ingestion_module.MAX_EXTRACTED_CHARACTERS,
            max_chunks=ingestion_module.MAX_CHUNKS,
        )
        with service.database.connect() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET run_token = 'new-owner-token' WHERE id = ?",
                (job["id"],),
            )

        with pytest.raises(RuntimeError):
            service.ingestion._commit_success(row, content, stale_token, actor, prepared)
    finally:
        service.ingestion._release_worker_lease()

    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute(
            "SELECT run_token FROM ingestion_jobs WHERE id = ?", (job["id"],)
        ).fetchone()[0] == "new-owner-token"


def test_cancel_then_parser_error_finishes_cancelled_instead_of_sticking(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="cancel-error.md",
        content=b"cancel and parser error race",
        idempotency_key="cancel-error-race-001",
    )
    entered = threading.Event()
    release = threading.Event()

    def failing_prepare(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=3)
        raise ValueError("synthetic parser failure with no user data")

    monkeypatch.setattr(service.documents, "prepare_index", failing_prepare)
    result: list[dict | None] = []
    worker = threading.Thread(target=lambda: result.append(service.ingestion.run_one(job["id"])))
    worker.start()
    assert entered.wait(timeout=3)
    assert service.ingestion.cancel(actor, job["id"])["status"] == "cancelling"
    release.set()
    worker.join(timeout=3)

    assert result[0] is not None and result[0]["status"] == "cancelled"
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_payloads").fetchone()[0] == 0


def test_bounded_close_does_not_allow_a_second_worker(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    service.ingestion.create(
        actor,
        filename="blocked.md",
        content=b"blocked parser",
        idempotency_key="blocked-close-001",
    )
    entered = threading.Event()
    release = threading.Event()
    original_prepare = service.documents.prepare_index

    def blocking_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=3)
        return result

    monkeypatch.setattr(service.documents, "prepare_index", blocking_prepare)
    monkeypatch.setattr(ingestion_module, "WORKER_CLOSE_TIMEOUT_SECONDS", 0.02)
    service.start()
    assert entered.wait(timeout=3)
    first_worker = service.ingestion._worker
    assert first_worker is not None and first_worker.is_alive()

    assert service.close() is False
    assert service.ingestion._worker is first_worker
    assert first_worker.is_alive()
    service.start()
    assert service.ingestion._worker is first_worker

    release.set()
    first_worker.join(timeout=3)
    assert service.close() is True
    assert service.ingestion._worker is None
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM ingestion_jobs").fetchone()[0] == "queued"


def test_second_service_cannot_recover_or_claim_a_live_database_worker(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "assistant.db"
    first = KnowledgeAssistantService(path)
    second = KnowledgeAssistantService(path)
    actor = editor()
    job = first.ingestion.create(
        actor,
        filename="owned.txt",
        content=b"owned by the first worker",
        idempotency_key="worker-owner-001",
    )
    entered = threading.Event()
    release = threading.Event()
    original_prepare = first.documents.prepare_index

    def blocking_prepare(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(first.documents, "prepare_index", blocking_prepare)
    first.start()
    try:
        assert entered.wait(timeout=3)
        with pytest.raises(ConflictError, match="worker.*owned"):
            second.start()
        assert second.ingestion.run_one(job["id"]) is None
        assert first.ingestion.get(actor, job["id"])["status"] == "running"
    finally:
        release.set()
        first.close()
        second.close()


def test_worker_loop_survives_transient_database_error(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    recovered = threading.Event()
    attempts = 0
    original_run_one = service.ingestion.run_one

    def transient_then_continue(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is temporarily busy")
        recovered.set()
        return original_run_one(*args, **kwargs)

    monkeypatch.setattr(service.ingestion, "run_one", transient_then_continue)
    monkeypatch.setattr(ingestion_module, "WORKER_ERROR_BACKOFF_SECONDS", 0.01, raising=False)
    service.start()
    try:
        assert recovered.wait(timeout=3)
        assert service.ingestion._worker is not None
        assert service.ingestion._worker.is_alive()
    finally:
        service.close()


def test_transient_database_error_during_processing_requeues_without_losing_payload(
    tmp_path: Path, monkeypatch
):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="transient.txt",
        content=b"retry after transient database failure",
        idempotency_key="transient-process-001",
    )
    original_update = service.ingestion._update_progress
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is temporarily busy")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(service.ingestion, "_update_progress", fail_once)

    with pytest.raises(sqlite3.OperationalError):
        service.ingestion.run_one(job["id"])

    queued = service.ingestion.get(actor, job["id"])
    assert queued["status"] == "queued"
    assert queued["attempt_count"] == 1
    assert queued["error_code"] == "transient_database_error"
    with service.database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ingestion_payloads WHERE job_id = ?", (job["id"],)
        ).fetchone()[0] == 1

    completed = service.ingestion.run_one(job["id"])

    assert completed is not None and completed["status"] == "succeeded"
    assert completed["attempt_count"] == 2
    assert completed["retryable"] is False
    assert completed["error_code"] == ""
    assert completed["error_message"] == ""


def test_recovery_closes_running_trace_left_by_double_database_failure(
    tmp_path: Path, monkeypatch
):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="double-database-failure.txt",
        content=b"queued work and observability must reconcile",
        idempotency_key="double-database-failure-001",
    )

    def fail_progress(*_args, **_kwargs):
        raise sqlite3.OperationalError("progress database failure")

    def fail_trace_finish(*_args, **_kwargs):
        raise sqlite3.OperationalError("trace finalization database failure")

    monkeypatch.setattr(service.ingestion, "_update_progress", fail_progress)
    monkeypatch.setattr(service.traces, "_finish_trace", fail_trace_finish)

    with pytest.raises(sqlite3.OperationalError, match="trace finalization"):
        service.ingestion.run_one(job["id"])

    queued_before_recovery = service.ingestion.get(actor, job["id"])
    trace_id = queued_before_recovery["trace_id"]
    assert queued_before_recovery["status"] == "queued"
    assert queued_before_recovery["attempt_count"] == 1
    assert trace_id
    with service.database.connect() as conn:
        trace_before = conn.execute(
            "SELECT status FROM traces WHERE id = ?", (trace_id,)
        ).fetchone()
        assert trace_before is not None and trace_before["status"] == "running"
        assert conn.execute(
            "SELECT COUNT(*) FROM ingestion_payloads WHERE job_id = ?", (job["id"],)
        ).fetchone()[0] == 1

    assert service.ingestion._acquire_worker_lease()
    try:
        service.ingestion._recover_interrupted()
    finally:
        service.ingestion._release_worker_lease()

    queued_after_recovery = service.ingestion.get(actor, job["id"])
    assert queued_after_recovery["status"] == "queued"
    assert queued_after_recovery["attempt_count"] == 1
    with service.database.connect() as conn:
        trace = conn.execute(
            "SELECT status, finished_at, latency_ms, error_code FROM traces WHERE id = ?",
            (trace_id,),
        ).fetchone()
        assert trace is not None and trace["status"] == "error"
        assert trace["error_code"] == "interrupted_requeued"
        assert trace["finished_at"] and float(trace["latency_ms"]) >= 0
        assert conn.execute(
            "SELECT COUNT(*) FROM spans WHERE trace_id = ? AND status = 'running'", (trace_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ingestion_payloads WHERE job_id = ?", (job["id"],)
        ).fetchone()[0] == 1
    metrics = service.traces.metrics(actor)
    assert metrics["trace_count"] == 1
    assert metrics["error_count"] == 1
    assert metrics["latency_ms"]["p95"] >= 0


def test_worker_and_heartbeat_stop_when_lease_is_fenced(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    fenced = threading.Event()

    def lose_claim(*_args, **_kwargs):
        fenced.set()
        raise ingestion_module._ClaimLost()

    monkeypatch.setattr(service.ingestion, "run_one", lose_claim)
    service.start()
    assert fenced.wait(timeout=3)
    worker = service.ingestion._worker
    assert worker is not None
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert service.ingestion._stop.is_set()
    assert service.ingestion._heartbeat_stop.is_set()
    service.close()


def test_run_one_propagates_preclaim_lease_loss_without_touching_job(
    tmp_path: Path, monkeypatch
):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")

    def lose_before_claim(*_args, **_kwargs):
        raise ingestion_module._ClaimLost()

    monkeypatch.setattr(service.ingestion, "_claim", lose_before_claim)

    with pytest.raises(ingestion_module._ClaimLost):
        service.ingestion.run_one()


def test_fenced_old_worker_cannot_delete_the_new_owner_lease(tmp_path: Path, monkeypatch):
    path = tmp_path / "assistant.db"
    service = KnowledgeAssistantService(path)
    entered = threading.Event()
    release = threading.Event()

    def lose_after_new_owner_arrives(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=3)
        raise ingestion_module._ClaimLost()

    monkeypatch.setattr(service.ingestion, "run_one", lose_after_new_owner_arrives)
    service.start()
    assert entered.wait(timeout=3)
    new_owner = "new-process-worker"
    with service.database.connect() as conn:
        conn.execute(
            "UPDATE worker_leases SET owner_id = ?, process_boot_id = ?, lease_expires_at = ? "
            "WHERE name = ?",
            (
                new_owner,
                "new-process",
                time.time() + 60,
                ingestion_module.WORKER_LEASE_NAME,
            ),
        )
    release.set()
    worker = service.ingestion._worker
    assert worker is not None
    worker.join(timeout=3)

    with service.database.connect() as conn:
        assert conn.execute(
            "SELECT owner_id FROM worker_leases WHERE name = ?",
            (ingestion_module.WORKER_LEASE_NAME,),
        ).fetchone()[0] == new_owner


def test_claim_lost_during_prepare_does_not_relabel_the_new_owner_job(
    tmp_path: Path, monkeypatch
):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="fenced-prepare.txt",
        content=b"old worker must not finalize",
        idempotency_key="fenced-prepare-001",
    )

    def lose_claim(*_args, **_kwargs):
        with service.database.connect() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET run_token = 'new-token', "
                "worker_instance_id = 'new-worker' WHERE id = ?",
                (job["id"],),
            )
        raise ingestion_module._ClaimLost()

    monkeypatch.setattr(service.documents, "prepare_index", lose_claim)

    result = service.ingestion.run_one(job["id"])

    assert result is not None and result["status"] == "running"
    with service.database.connect() as conn:
        row = conn.execute(
            "SELECT status, run_token, worker_instance_id FROM ingestion_jobs WHERE id = ?",
            (job["id"],),
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("running", "new-token", "new-worker")
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_progress_and_cancel_database_checks_are_throttled(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="progress.txt",
        content=b"progress",
        idempotency_key="progress-throttle-001",
    )
    progress_updates = 0
    claim_checks = 0
    original_update = service.ingestion._update_progress
    original_check = service.ingestion._check_claim

    def counted_update(*args, **kwargs):
        nonlocal progress_updates
        progress_updates += 1
        return original_update(*args, **kwargs)

    def counted_check(*args, **kwargs):
        nonlocal claim_checks
        claim_checks += 1
        return original_check(*args, **kwargs)

    def noisy_prepare(*_args, cancel_check, progress, **_kwargs):
        for index in range(1_000):
            cancel_check()
            progress("embedding", index, 1_000)
        return PreparedDocument(
            content_type="text/plain",
            embedded=(
                (
                    DocumentChunk(ordinal=0, content="progress"),
                    service.embedding.pack(service.embedding.embed("progress")),
                ),
            ),
        )

    monkeypatch.setattr(service.ingestion, "_update_progress", counted_update)
    monkeypatch.setattr(service.ingestion, "_check_claim", counted_check)
    monkeypatch.setattr(service.documents, "prepare_index", noisy_prepare)

    result = service.ingestion.run_one(job["id"])

    assert result is not None and result["status"] == "succeeded"
    assert progress_updates < 20
    assert claim_checks < 20


def test_single_service_run_one_is_serialized_even_with_one_lease(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    first_job = service.ingestion.create(
        actor,
        filename="first-serialized.txt",
        content=b"first",
        idempotency_key="serialized-first-001",
    )
    second_job = service.ingestion.create(
        actor,
        filename="second-serialized.txt",
        content=b"second",
        idempotency_key="serialized-second-001",
    )
    entered = threading.Event()
    release = threading.Event()
    original_prepare = service.documents.prepare_index

    def blocking_prepare(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(service.documents, "prepare_index", blocking_prepare)
    result: list[dict | None] = []
    first = threading.Thread(
        target=lambda: result.append(service.ingestion.run_one(first_job["id"]))
    )
    first.start()
    assert entered.wait(timeout=3)

    assert service.ingestion.run_one(second_job["id"]) is None
    assert service.ingestion.get(actor, second_job["id"])["status"] == "queued"
    release.set()
    first.join(timeout=3)

    assert result[0] is not None and result[0]["status"] == "succeeded"


def test_global_active_job_limit_applies_across_tenants(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    monkeypatch.setattr(ingestion_module, "MAX_ACTIVE_JOBS_GLOBAL", 1, raising=False)
    service.ingestion.create(
        editor(tenant="tenant-a"),
        filename="one.txt",
        content=b"one",
        idempotency_key="global-active-a",
    )

    with pytest.raises(ConflictError, match="global active ingestion job limit"):
        service.ingestion.create(
            editor(tenant="tenant-b"),
            filename="two.txt",
            content=b"two",
            idempotency_key="global-active-b",
        )


def test_cancel_after_commit_is_too_late_and_never_relabels_success(tmp_path: Path):
    path = tmp_path / "assistant.db"
    service = KnowledgeAssistantService(path)
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="completed.txt",
        content=b"already committed",
        idempotency_key="cancel-too-late-001",
    )
    completed = service.ingestion.run_one(job["id"])
    cancelled = service.ingestion.cancel(actor, job["id"])

    assert completed is not None and completed["status"] == "succeeded"
    assert cancelled["status"] == "succeeded"
    assert cancelled["cancel_outcome"] == "too_late"
    assert cancelled["cancel_requested_at"]
    assert service.ingestion.get(actor, job["id"])["cancel_outcome"] == "too_late"
    assert service.ingestion.list(actor)[0]["cancel_outcome"] == "too_late"

    restarted = KnowledgeAssistantService(path)

    assert restarted.ingestion.get(actor, job["id"])["cancel_outcome"] == "too_late"


def test_final_commit_longer_than_lease_is_fenced_by_owner_not_wall_clock(
    tmp_path: Path, monkeypatch
):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    job = service.ingestion.create(
        actor,
        filename="long-commit.txt",
        content=b"legitimate long transaction",
        idempotency_key="long-commit-001",
    )
    monkeypatch.setattr(ingestion_module, "WORKER_LEASE_SECONDS", 0.01)
    original_persist = service.documents.persist_prepared

    def slow_persist(*args, **kwargs):
        time.sleep(0.03)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(service.documents, "persist_prepared", slow_persist)

    completed = service.ingestion.run_one(job["id"])

    assert completed is not None and completed["status"] == "succeeded"
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1


def test_different_jobs_with_same_content_atomically_deduplicate(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    first = service.ingestion.create(
        actor,
        filename="first.txt",
        content=b"same content",
        idempotency_key="dedupe-job-first",
    )
    second = service.ingestion.create(
        actor,
        filename="second.txt",
        content=b"same content",
        idempotency_key="dedupe-job-second",
    )

    first_result = service.ingestion.run_one(first["id"])
    second_result = service.ingestion.run_one(second["id"])

    assert first_result is not None and second_result is not None
    assert second_result["deduplicated"] is True
    assert second_result["document_id"] == first_result["document_id"]
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_restart_requeues_running_up_to_three_attempts_and_finishes_cancelling(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    recoverable = service.ingestion.create(
        actor,
        filename="recover.txt",
        content=b"recover me",
        idempotency_key="restart-recover-001",
    )
    exhausted = service.ingestion.create(
        actor,
        filename="exhausted.txt",
        content=b"do not loop forever",
        idempotency_key="restart-exhaust-001",
    )
    cancelling = service.ingestion.create(
        actor,
        filename="cancelling.txt",
        content=b"cancel on restart",
        idempotency_key="restart-cancel-001",
    )
    with service.database.connect() as conn:
        trace_ids: dict[str, str] = {}
        for name, job in (("recover", recoverable), ("exhausted", exhausted), ("cancel", cancelling)):
            trace_id = f"trace-{name}"
            trace_ids[name] = trace_id
            conn.execute(
                "INSERT INTO traces(id, tenant_id, user_id, operation, status, started_at) "
                "VALUES(?, 'tenant-a', 'editor-a', 'ingestion.job', 'running', 'legacy')",
                (trace_id,),
            )
            conn.execute(
                "INSERT INTO spans(id, trace_id, name, status, started_at) "
                "VALUES(?, ?, 'ingestion.prepare', 'running', 'legacy')",
                (f"span-{name}", trace_id),
            )
            conn.execute(
                "UPDATE ingestion_jobs SET trace_id = ? WHERE id = ?", (trace_id, job["id"])
            )
        conn.execute(
            "UPDATE ingestion_jobs SET status = 'running', attempt_count = 1, run_token = 'lost-1' "
            "WHERE id = ?",
            (recoverable["id"],),
        )
        conn.execute(
            "UPDATE ingestion_jobs SET status = 'running', attempt_count = 3, run_token = 'lost-3' "
            "WHERE id = ?",
            (exhausted["id"],),
        )
        conn.execute(
            "UPDATE ingestion_jobs SET status = 'cancelling', attempt_count = 1, run_token = 'lost-cancel' "
            "WHERE id = ?",
            (cancelling["id"],),
        )

    assert service.ingestion._acquire_worker_lease()
    try:
        service.ingestion._recover_interrupted()

        assert service.ingestion.get(actor, recoverable["id"])["status"] == "queued"
        exhausted_result = service.ingestion.get(actor, exhausted["id"])
        assert exhausted_result["status"] == "failed"
        assert exhausted_result["error_code"] == "interrupted_retries_exhausted"
        assert service.ingestion.get(actor, cancelling["id"])["status"] == "cancelled"
        assert service.ingestion.get(actor, cancelling["id"])["cancel_outcome"] == "cancelled"
        with service.database.connect() as conn:
            for trace_id in trace_ids.values():
                trace_row = conn.execute(
                    "SELECT status, finished_at, latency_ms FROM traces WHERE id = ?", (trace_id,)
                ).fetchone()
                span_row = conn.execute(
                    "SELECT status, finished_at, latency_ms FROM spans WHERE trace_id = ?", (trace_id,)
                ).fetchone()
                assert trace_row is not None
                assert trace_row["status"] == "error"
                assert trace_row["finished_at"]
                assert float(trace_row["latency_ms"]) >= 0
                assert span_row is not None
                assert span_row["status"] == "error"
                assert span_row["finished_at"]
                assert float(span_row["latency_ms"]) >= 0
        metrics = service.traces.metrics(actor)
        assert metrics["trace_count"] == 3
        assert metrics["error_count"] == 3
        assert metrics["latency_ms"]["p95"] >= 0
    finally:
        service.ingestion._release_worker_lease()


def test_recovery_reconciles_running_traces_for_terminal_jobs(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    jobs = {
        status: service.ingestion.create(
            actor,
            filename=f"{status}.txt",
            content=status.encode(),
            idempotency_key=f"terminal-trace-{status}",
        )
        for status in ("succeeded", "failed", "cancelled")
    }
    with service.database.connect() as conn:
        for status, job in jobs.items():
            trace_id = f"trace-terminal-{status}"
            conn.execute(
                "INSERT INTO traces(id, tenant_id, user_id, operation, status, started_at) "
                "VALUES(?, 'tenant-a', 'editor-a', 'ingestion.job', 'running', 'legacy')",
                (trace_id,),
            )
            conn.execute(
                "INSERT INTO spans(id, trace_id, name, status, started_at) "
                "VALUES(?, ?, 'ingestion.commit', 'running', 'legacy')",
                (f"span-terminal-{status}", trace_id),
            )
            conn.execute(
                "UPDATE ingestion_jobs SET status = ?, trace_id = ? WHERE id = ?",
                (status, trace_id, job["id"]),
            )

    assert service.ingestion._acquire_worker_lease()
    try:
        service.ingestion._recover_interrupted()
    finally:
        service.ingestion._release_worker_lease()

    with service.database.connect() as conn:
        for status in jobs:
            expected = "ok" if status == "succeeded" else "error"
            trace = conn.execute(
                "SELECT status, finished_at, latency_ms FROM traces WHERE id = ?",
                (f"trace-terminal-{status}",),
            ).fetchone()
            span = conn.execute(
                "SELECT status, finished_at, latency_ms FROM spans WHERE id = ?",
                (f"span-terminal-{status}",),
            ).fetchone()
            assert trace is not None and trace["status"] == expected
            assert trace["finished_at"] and float(trace["latency_ms"]) >= 0
            assert span is not None and span["status"] == expected
            assert span["finished_at"] and float(span["latency_ms"]) >= 0


def test_quota_scope_and_requester_cancellation_are_enforced(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor(collections=frozenset({"engineering"}))
    job = service.ingestion.create(
        actor,
        filename="one.txt",
        content=b"one",
        collection_id="engineering",
        idempotency_key="quota-one-001",
    )
    monkeypatch.setattr(ingestion_module, "MAX_ACTIVE_JOBS_PER_TENANT", 1)
    with pytest.raises(ConflictError, match="active ingestion job limit"):
        service.ingestion.create(
            actor,
            filename="two.txt",
            content=b"two",
            collection_id="engineering",
            idempotency_key="quota-two-001",
        )
    with pytest.raises(PermissionDenied, match="requester"):
        service.ingestion.cancel(
            editor(user="other", collections=frozenset({"engineering"})), job["id"]
        )
    with pytest.raises(ResourceNotFound):
        service.ingestion.get(editor(tenant="tenant-b"), job["id"])
    with pytest.raises(ResourceNotFound):
        service.ingestion.cancel(editor(tenant="tenant-b"), job["id"])
    with pytest.raises(PermissionDenied, match="collection"):
        service.ingestion.get(
            editor(collections=frozenset({"human-resources"})), job["id"]
        )
    with pytest.raises(PermissionDenied, match="collection"):
        service.ingestion.cancel(
            editor(collections=frozenset({"human-resources"})), job["id"]
        )
    assert service.ingestion.list(
        editor(collections=frozenset({"human-resources"}))
    ) == []
    assert [item["id"] for item in service.ingestion.list(actor)] == [job["id"]]


def test_staged_byte_quotas_apply_per_tenant_and_globally(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    monkeypatch.setattr(ingestion_module, "MAX_STAGED_BYTES_PER_TENANT", 3)
    monkeypatch.setattr(ingestion_module, "MAX_STAGED_BYTES_GLOBAL", 4)
    service.ingestion.create(
        editor(),
        filename="tenant.txt",
        content=b"123",
        idempotency_key="tenant-bytes-001",
    )
    with pytest.raises(ConflictError, match="tenant staged ingestion byte limit"):
        service.ingestion.create(
            editor(),
            filename="tenant-over.txt",
            content=b"4",
            idempotency_key="tenant-bytes-002",
        )
    service.ingestion.create(
        editor(tenant="tenant-b"),
        filename="global.txt",
        content=b"4",
        idempotency_key="global-bytes-001",
    )
    with pytest.raises(ConflictError, match="global staged ingestion byte limit"):
        service.ingestion.create(
            editor(tenant="tenant-c"),
            filename="global-over.txt",
            content=b"5",
            idempotency_key="global-bytes-002",
        )


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "expected_code"),
    [
        ("MAX_EXTRACTED_CHARACTERS", 3, "extracted_text_limit"),
        ("MAX_CHUNKS", 0, "chunk_limit"),
    ],
)
def test_processing_limits_fail_without_document_side_effects(
    tmp_path: Path,
    monkeypatch,
    limit_name: str,
    limit_value: int,
    expected_code: str,
):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    monkeypatch.setattr(ingestion_module, limit_name, limit_value)
    job = service.ingestion.create(
        actor,
        filename="bounded.txt",
        content=b"content beyond synthetic limit",
        idempotency_key=f"limit-{expected_code}",
    )

    failed = service.ingestion.run_one(job["id"])

    assert failed is not None and failed["status"] == "failed"
    assert failed["error_code"] == expected_code
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_terminal_error_is_safe_and_payload_is_removed(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    secret_content = b"TOP-SECRET-CONTENT"
    secret_key = "secret-key-001"
    job = service.ingestion.create(
        actor,
        filename="sensitive-name.exe",
        content=secret_content,
        idempotency_key=secret_key,
    )

    failed = service.ingestion.run_one(job["id"])

    assert failed is not None and failed["status"] == "failed"
    assert failed["error_code"] == "invalid_document"
    rendered = str(failed)
    assert secret_content.decode() not in rendered
    assert secret_key not in rendered
    assert "sensitive-name.exe" not in failed["error_message"]
    with service.database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ingestion_payloads WHERE job_id = ?", (job["id"],)
        ).fetchone()[0] == 0
