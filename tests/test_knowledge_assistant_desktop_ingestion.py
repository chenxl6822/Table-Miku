from __future__ import annotations

import hashlib
from pathlib import Path
import os
import threading
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

import table_miku.knowledge_assistant_desktop as desktop_module
import table_miku.knowledge_assistant_outbox as outbox_module
from table_miku.knowledge_assistant import KnowledgeAssistantService
from table_miku.knowledge_assistant.auth import Principal
from table_miku.knowledge_assistant.client import KnowledgeAssistantApiError
from table_miku.knowledge_assistant_outbox import SecureIngestionOutbox


class _FakeProtector:
    def protect(self, value: bytes) -> bytes:
        return b"encrypted:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value.removeprefix(b"encrypted:")[::-1]


def _principal(user_id: str = "editor-a") -> Principal:
    return Principal(
        tenant_id="tenant-a",
        user_id=user_id,
        roles=frozenset({"editor"}),
        collection_ids=frozenset({"engineering"}),
    )


def _command(path: Path, *, local_id: str = "local-test") -> dict[str, object]:
    stat_result = path.stat()
    content = path.read_bytes()
    return {
        "principal": _principal(),
        "generation": 7,
        "items": [
            {
                "local_id": local_id,
                "path": str(path),
                "expected_snapshot": {
                    "canonical_path": str(path),
                    "size": stat_result.st_size,
                    "mtime_ns": stat_result.st_mtime_ns,
                    "device": stat_result.st_dev,
                    "inode": stat_result.st_ino,
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
                "filename": path.name,
                "collection_id": "engineering",
                "idempotency_key": "desktop-ingestion-test-001",
            }
        ],
    }


def _snapshot(path: Path) -> dict[str, object]:
    canonical = path.resolve(strict=True)
    stat_result = canonical.stat()
    return {
        "canonical_path": str(canonical),
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
        "device": stat_result.st_dev,
        "inode": stat_result.st_ino,
        "sha256": hashlib.sha256(canonical.read_bytes()).hexdigest(),
    }


def test_controller_principal_preserves_explicit_empty_collection_scope():
    unrestricted = desktop_module.KnowledgeAssistantDesktopController.principal(
        "tenant-a", "editor-a", ["editor"], ""
    )
    deny_all = desktop_module.KnowledgeAssistantDesktopController.principal(
        "tenant-a", "editor-a", ["editor"], frozenset()
    )

    assert unrestricted.collection_ids is None
    assert deny_all.collection_ids == frozenset()


def test_external_recovery_binding_rejects_same_instance_at_different_origin(
    tmp_path: Path,
):
    binding_a = desktop_module._recovery_binding_id(
        "external", "https://A.example:443", "same-instance"
    )
    binding_a_normalized = desktop_module._recovery_binding_id(
        "external", "https://a.example", "same-instance"
    )
    binding_b = desktop_module._recovery_binding_id(
        "external", "https://b.example", "same-instance"
    )
    assert binding_a == binding_a_normalized
    assert binding_a != binding_b

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    request = {
        "operation": "create_ingestion_job",
        "service_instance_id": binding_a,
        "principal": desktop_module._principal_payload(_principal()),
        "filename": "origin.md",
        "collection_id": "engineering",
        "content": b"origin bound",
        "idempotency_key": "origin-binding-key",
    }
    pending = outbox.enqueue(request)

    with pytest.raises(PermissionError, match="service"):
        outbox.load_for_replay(
            pending.entry_id,
            service_instance_id=binding_b,
            principal=request["principal"],
        )
    assert b"a.example" not in pending.path.read_bytes()
    assert b"b.example" not in pending.path.read_bytes()


def test_embedded_recovery_binding_ignores_random_loopback_port():
    assert desktop_module._recovery_binding_id(
        "embedded", "http://127.0.0.1:49152", "instance-1"
    ) == desktop_module._recovery_binding_id(
        "embedded", "http://127.0.0.1:61234", "instance-1"
    )


def test_worker_persists_and_fsyncs_before_the_first_network_write(tmp_path: Path):
    events: list[str] = []

    class RecordingOutbox(SecureIngestionOutbox):
        def enqueue(self, request):
            entry = super().enqueue(request)
            events.append("outbox_durable")
            return entry

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            events.append("network")
            return {"id": "job-1", "status": "queued"}

    path = tmp_path / "source.md"
    path.write_bytes(b"exact first bytes")
    outbox = RecordingOutbox(tmp_path / "outbox", protector=_FakeProtector())
    registry = desktop_module._IngestionCancellationRegistry()
    worker = desktop_module._IngestionWorker(Controller(), outbox, "instance-1", registry)

    worker.process_batch(_command(path))

    assert events == ["outbox_durable", "network"]
    records = outbox.scan()
    assert len(records) == 1
    assert records[0].payload["state"] == "tracking"
    assert "content" not in records[0].payload


def test_worker_does_not_send_when_secure_persistence_fails(tmp_path: Path):
    calls: list[str] = []

    class FailingProtector:
        @staticmethod
        def protect(_value: bytes) -> bytes:
            raise OSError("DPAPI failure")

        @staticmethod
        def unprotect(_value: bytes) -> bytes:
            raise AssertionError("not called")

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            calls.append("network")
            return {"id": "job-1", "status": "queued"}

    path = tmp_path / "source.md"
    path.write_bytes(b"must never leave the machine")
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=FailingProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(path))

    assert calls == []
    assert updates[-1]["status"] == "failed"
    assert "DPAPI" not in updates[-1]["message"]


def test_queued_cancellation_is_final_only_because_no_request_was_sent(tmp_path: Path):
    calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            calls.append("network")
            return {"id": "job-1", "status": "queued"}

    path = tmp_path / "source.md"
    path.write_bytes(b"cancel before send")
    registry = desktop_module._IngestionCancellationRegistry()
    registry.request_cancel("local-cancel")
    worker = desktop_module._IngestionWorker(
        Controller(),
        SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
        registry,
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(path, local_id="local-cancel"))

    assert calls == []
    assert updates[-1]["status"] == "cancelled"
    assert not list((tmp_path / "outbox").glob("*.json"))


def test_cancel_that_loses_the_race_to_success_is_reported_as_success(tmp_path: Path):
    path = tmp_path / "source.md"
    path.write_bytes(b"success wins")
    registry = desktop_module._IngestionCancellationRegistry()
    cancel_calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            registry.request_cancel("local-race")
            return {"id": "job-success", "status": "succeeded"}

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            cancel_calls.append(job_id)
            return {"id": job_id, "status": "cancelled"}

    worker = desktop_module._IngestionWorker(
        Controller(),
        SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
        registry,
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(path, local_id="local-race"))

    assert updates[-1]["status"] == "succeeded"
    assert cancel_calls == []
    assert not list((tmp_path / "outbox").glob("*.json"))


@pytest.mark.parametrize("status_code", [400, 401, 403, 409, 422, 429])
def test_definite_client_rejection_is_failed_not_outcome_unknown(
    tmp_path: Path,
    status_code: int,
):
    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            raise KnowledgeAssistantApiError(status_code, "rejected", "unsafe raw detail")

    source = tmp_path / f"rejected-{status_code}.md"
    source.write_bytes(b"rejected")
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(source))

    assert updates[-1]["status"] == "failed"
    assert "unsafe raw detail" not in updates[-1]["message"]
    assert outbox.scan() == []


@pytest.mark.parametrize(
    "error",
    [
        KnowledgeAssistantApiError(408, "timeout", "raw timeout"),
        KnowledgeAssistantApiError(500, "server_error", "raw server error"),
        KnowledgeAssistantApiError(200, "invalid_response", "raw malformed success"),
        ConnectionError("raw network failure"),
    ],
)
def test_ambiguous_create_failure_retains_pending_outbox(
    tmp_path: Path,
    error: Exception,
):
    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            raise error

    source = tmp_path / "unknown.md"
    source.write_bytes(b"unknown")
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(source))

    assert updates[-1]["status"] == "outcome_unknown"
    assert outbox.scan()[0].payload["state"] == "pending"


def test_cancel_intent_during_unknown_create_is_durably_preserved(tmp_path: Path):
    source = tmp_path / "unknown-cancel.md"
    source.write_bytes(b"unknown cancellation")
    registry = desktop_module._IngestionCancellationRegistry()

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            registry.request_cancel("local-unknown-cancel")
            raise ConnectionError("response lost")

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(Controller(), outbox, "instance-1", registry)

    worker.process_batch(_command(source, local_id="local-unknown-cancel"))

    assert outbox.scan()[0].payload["cancel_after_submit"] is True


def test_cancel_arriving_with_create_response_is_persisted_before_tracking(
    tmp_path: Path,
):
    source = tmp_path / "cancel-after-response.md"
    source.write_bytes(b"cancel response")
    registry = desktop_module._IngestionCancellationRegistry()
    events: list[str] = []

    class RecordingOutbox(SecureIngestionOutbox):
        def mark_cancel_requested(self, *args, **kwargs):
            events.append("cancel-durable")
            return super().mark_cancel_requested(*args, **kwargs)

        def mark_submitted(self, *args, **kwargs):
            events.append("tracking")
            return super().mark_submitted(*args, **kwargs)

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            registry.request_cancel("local-cancel-after-response")
            return {"id": "job-cancel-after-response", "status": "queued"}

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            events.append(f"cancel-network:{job_id}")
            return {"id": job_id, "status": "cancelled"}

    outbox = RecordingOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(Controller(), outbox, "instance-1", registry)

    worker.process_batch(_command(source, local_id="local-cancel-after-response"))

    assert events == [
        "cancel-durable",
        "tracking",
        "cancel-network:job-cancel-after-response",
    ]


def test_create_response_cancel_does_not_send_when_delivering_persistence_fails(
    tmp_path: Path,
):
    source = tmp_path / "cancel-delivering-fails.md"
    source.write_bytes(b"cancel delivering fails")
    registry = desktop_module._IngestionCancellationRegistry()
    cancel_calls: list[str] = []

    class DeliveringFailOutbox(SecureIngestionOutbox):
        def mark_cancel_delivering(self, *args, **kwargs):
            del args, kwargs
            raise OSError("delivering persistence failed")

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            registry.request_cancel("local-cancel-delivering-fails")
            return {"id": "job-cancel-delivering-fails", "status": "queued"}

        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            cancel_calls.append("cancel")

    outbox = DeliveringFailOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(Controller(), outbox, "instance-1", registry)
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(source, local_id="local-cancel-delivering-fails"))

    assert cancel_calls == []
    assert outbox.scan()[0].payload["cancel_delivery_state"] == "requested"
    assert updates[-1]["status"] == "outcome_unknown"
    assert "尚未发送" in updates[-1]["message"]


def test_create_response_cancel_crash_restarts_without_duplicate_cancel(tmp_path: Path):
    source = tmp_path / "cancel-delivering-crash.md"
    source.write_bytes(b"cancel delivering crash")
    directory = tmp_path / "outbox"
    registry = desktop_module._IngestionCancellationRegistry()
    cancel_calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            registry.request_cancel("local-cancel-delivering-crash")
            return {"id": "job-cancel-delivering-crash", "status": "queued"}

        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-cancel-delivering-crash", "status": "running"}]

        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            cancel_calls.append("cancel")
            raise SystemExit("simulated crash after create-response cancel send")

    outbox = SecureIngestionOutbox(directory, protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(Controller(), outbox, "instance-1", registry)

    with pytest.raises(SystemExit, match="create-response cancel send"):
        worker.process_batch(_command(source, local_id="local-cancel-delivering-crash"))

    assert cancel_calls == ["cancel"]
    assert outbox.load(outbox.scan()[0].entry_id).payload["cancel_delivery_state"] == (
        "delivering"
    )
    restarted_outbox = SecureIngestionOutbox(directory, protector=_FakeProtector())
    restarted = desktop_module._IngestionWorker(
        Controller(),
        restarted_outbox,
        "instance-1",
        desktop_module._IngestionCancellationRegistry(),
    )

    restarted.poll({"principal": _principal(), "generation": 8})

    assert restarted_outbox.scan()[0].payload["cancel_delivery_state"] == "unknown"
    assert cancel_calls == ["cancel"]


def test_known_job_cancel_4xx_never_becomes_create_failed_or_deletes_tracking(
    tmp_path: Path,
):
    source = tmp_path / "known-job.md"
    source.write_bytes(b"known")
    registry = desktop_module._IngestionCancellationRegistry()

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            registry.request_cancel("local-known-job")
            return {"id": "job-known", "status": "queued"}

        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            raise KnowledgeAssistantApiError(403, "permission_denied", "unsafe detail")

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(Controller(), outbox, "instance-1", registry)
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(source, local_id="local-known-job"))

    final = updates[-1]
    assert final["job_id"] == "job-known"
    assert final["status"] == "cancel_rejected"
    assert "未创建" not in final["message"]
    tracked = outbox.scan()[0].payload
    assert tracked["state"] == "tracking"
    assert tracked["cancel_after_submit"] is True


def test_known_create_result_is_not_downgraded_when_tracking_transition_fails(
    tmp_path: Path,
):
    class TrackingFailingOutbox(SecureIngestionOutbox):
        def mark_submitted(self, entry_id: str, *, job_id: str):
            del entry_id, job_id
            raise OSError("tracking replace failed")

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            return {"id": "job-known", "status": "queued"}

    source = tmp_path / "tracking-fails.md"
    source.write_bytes(b"known result")
    outbox = TrackingFailingOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(source))

    assert updates[-1]["job_id"] == "job-known"
    assert updates[-1]["status"] == "queued"
    assert "跟踪" in updates[-1]["message"]
    assert outbox.scan()[0].payload["state"] == "pending"


def test_known_create_result_survives_cancel_intent_persistence_failure(
    tmp_path: Path,
):
    registry = desktop_module._IngestionCancellationRegistry()

    class CancelPersistFailingOutbox(SecureIngestionOutbox):
        def mark_cancel_requested(self, *args, **kwargs):
            del args, kwargs
            raise OSError("cancel persistence failed")

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            registry.request_cancel("local-cancel-persist-fail")
            return {"id": "job-known", "status": "queued"}

        @staticmethod
        def cancel_ingestion_job(*_args, **_kwargs):
            raise AssertionError("cancel must not be sent without durable intent")

    source = tmp_path / "cancel-persist-fails.md"
    source.write_bytes(b"known result")
    outbox = CancelPersistFailingOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(Controller(), outbox, "instance-1", registry)
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(source, local_id="local-cancel-persist-fail"))

    assert updates[-1]["job_id"] == "job-known"
    assert updates[-1]["status"] == "queued"
    assert "取消意图未能持久化" in updates[-1]["message"]
    assert outbox.scan()[0].payload["state"] == "pending"


def test_explicit_recovery_cancel_only_persists_intent(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "pending.md",
            "collection_id": "engineering",
            "content": b"pending",
            "idempotency_key": "pending-cancel-key",
        }
    )

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            raise AssertionError("cancel intent must not submit")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.cancel_recovery(
        {"principal": _principal(), "entry_id": pending.entry_id, "generation": 3}
    )

    assert outbox.scan()[0].payload["cancel_after_submit"] is True
    assert updates[-1]["status"] == "outcome_unknown"
    assert "已取消" not in updates[-1]["message"]


def test_replay_with_cancel_intent_submits_then_requests_server_cancel(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "pending.md",
            "collection_id": "engineering",
            "content": b"pending",
            "idempotency_key": "pending-replay-key",
        }
    )
    outbox.mark_cancel_requested(
        pending.entry_id,
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
    )
    calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            calls.append("create")
            return {"id": "job-replayed", "status": "queued"}

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            calls.append(f"cancel:{job_id}")
            return {"id": job_id, "status": "cancelled"}

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.replay({"principal": _principal(), "entry_id": pending.entry_id, "generation": 3})

    assert calls == ["create", "cancel:job-replayed"]
    assert updates[-1]["status"] == "cancelled"
    assert outbox.scan() == []


def test_replay_cancel_does_not_send_when_delivering_persistence_fails(tmp_path: Path):
    cancel_calls: list[str] = []

    class DeliveringFailOutbox(SecureIngestionOutbox):
        def mark_cancel_delivering(self, *args, **kwargs):
            del args, kwargs
            raise OSError("delivering persistence failed")

    outbox = DeliveringFailOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "pending.md",
            "collection_id": "engineering",
            "content": b"pending",
            "idempotency_key": "pending-replay-delivering-fails",
        }
    )
    outbox.mark_cancel_requested(
        pending.entry_id,
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
    )

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            return {"id": "job-replayed", "status": "queued"}

        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            cancel_calls.append("cancel")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.replay({"principal": _principal(), "entry_id": pending.entry_id, "generation": 3})

    assert cancel_calls == []
    assert outbox.scan()[0].payload["cancel_delivery_state"] == "requested"
    assert updates[-1]["status"] == "outcome_unknown"
    assert "尚未发送" in updates[-1]["message"]


def test_replay_cancel_crash_restarts_without_duplicate_cancel(tmp_path: Path):
    directory = tmp_path / "outbox"
    cancel_calls: list[str] = []
    outbox = SecureIngestionOutbox(directory, protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "pending.md",
            "collection_id": "engineering",
            "content": b"pending",
            "idempotency_key": "pending-replay-delivering-crash",
        }
    )
    outbox.mark_cancel_requested(
        pending.entry_id,
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
    )

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            return {"id": "job-replayed", "status": "queued"}

        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-replayed", "status": "running"}]

        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            cancel_calls.append("cancel")
            raise SystemExit("simulated crash after replay cancel send")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    with pytest.raises(SystemExit, match="replay cancel send"):
        worker.replay(
            {"principal": _principal(), "entry_id": pending.entry_id, "generation": 3}
        )

    assert cancel_calls == ["cancel"]
    assert outbox.load(pending.entry_id).payload["cancel_delivery_state"] == "delivering"
    restarted_outbox = SecureIngestionOutbox(directory, protector=_FakeProtector())
    restarted = desktop_module._IngestionWorker(
        Controller(),
        restarted_outbox,
        "instance-1",
        desktop_module._IngestionCancellationRegistry(),
    )

    restarted.poll({"principal": _principal(), "generation": 4})

    assert restarted_outbox.scan()[0].payload["cancel_delivery_state"] == "unknown"
    assert cancel_calls == ["cancel"]


def test_replay_known_result_handles_tracking_transition_failure(tmp_path: Path):
    class TrackingFailingOutbox(SecureIngestionOutbox):
        def mark_submitted(self, entry_id: str, *, job_id: str):
            del entry_id, job_id
            raise OSError("tracking failed")

    outbox = TrackingFailingOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "pending.md",
            "collection_id": "engineering",
            "content": b"pending",
            "idempotency_key": "pending-replay-track-key",
        }
    )

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            return {"id": "job-known", "status": "queued"}

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.replay({"principal": _principal(), "entry_id": pending.entry_id, "generation": 3})

    assert updates[-1]["job_id"] == "job-known"
    assert updates[-1]["status"] == "queued"
    assert "跟踪" in updates[-1]["message"]
    assert outbox.scan()[0].payload["state"] == "pending"


def test_poll_reconciles_and_removes_terminal_tracking_record(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "tracked.md",
            "collection_id": "engineering",
            "content": b"tracked",
            "idempotency_key": "tracked-key",
        }
    )
    outbox.mark_submitted(pending.entry_id, job_id="job-terminal")

    class Controller:
        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-terminal", "status": "succeeded"}]

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )

    worker.poll({"principal": _principal(), "generation": 4})

    assert outbox.scan() == []


def test_tracking_job_beyond_first_hundred_is_reconciled_by_get(tmp_path: Path):
    outbox = SecureIngestionOutbox(
        tmp_path / "outbox",
        protector=_FakeProtector(),
        max_entries=200,
    )
    for index in range(101):
        pending = outbox.enqueue(
            {
                "operation": "create_ingestion_job",
                "service_instance_id": "instance-1",
                "principal": desktop_module._principal_payload(_principal()),
                "filename": f"tracked-{index}.md",
                "collection_id": "engineering",
                "content": f"tracked-{index}".encode(),
                "idempotency_key": f"tracked-key-{index:03d}",
            }
        )
        outbox.mark_submitted(pending.entry_id, job_id=f"job-{index:03d}")
    get_calls: list[str] = []

    class Controller:
        @staticmethod
        def list_ingestion_jobs(_principal):
            return [
                {"id": f"job-{index:03d}", "status": "running"}
                for index in range(100)
            ]

        @staticmethod
        def get_ingestion_job(_principal, job_id):
            get_calls.append(job_id)
            return {"id": job_id, "status": "succeeded"}

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )

    worker.poll({"principal": _principal(), "generation": 4})

    assert get_calls == ["job-100"]
    remaining_ids = {
        item.payload["job_id"] for item in outbox.scan() if item.payload is not None
    }
    assert "job-100" not in remaining_ids
    assert len(remaining_ids) == 100


def test_poll_interruption_after_slow_list_stops_get_and_cancel_writes(tmp_path: Path):
    calls: list[str] = []

    class Controller:
        @staticmethod
        def list_ingestion_jobs(_principal):
            calls.append("list")
            return []

        @staticmethod
        def get_ingestion_job(*_args):
            calls.append("get")
            return {"id": "job-running", "status": "running"}

        @staticmethod
        def cancel_ingestion_job(*_args):
            calls.append("cancel")

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "tracked.md",
            "collection_id": "engineering",
            "content": b"tracked",
            "idempotency_key": "poll-interrupt-key",
        }
    )
    outbox.mark_cancel_requested(
        pending.entry_id,
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
    )
    outbox.mark_submitted(pending.entry_id, job_id="job-running")
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    checks = iter((False, True))
    worker._interrupted = lambda: next(checks)

    worker.poll({"principal": _principal(), "generation": 4})

    assert calls == ["list"]


def test_reconcile_tracking_reports_definite_cancel_rejection_without_unknown(
    tmp_path: Path,
):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "tracked.md",
            "collection_id": "engineering",
            "content": b"tracked",
            "idempotency_key": "tracked-cancel-key",
        }
    )
    outbox.mark_cancel_requested(
        pending.entry_id,
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
    )
    outbox.mark_submitted(pending.entry_id, job_id="job-running")
    cancel_calls: list[str] = []

    class Controller:
        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-running", "status": "running"}]

        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            cancel_calls.append("cancel")
            raise KnowledgeAssistantApiError(403, "permission_denied", "unsafe")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.poll({"principal": _principal(), "generation": 4})
    worker.poll({"principal": _principal(), "generation": 4})

    rejected = next(item for item in updates if item["status"] == "cancel_rejected")
    assert "明确拒绝" in rejected["message"]
    assert "unsafe" not in rejected["message"]
    assert outbox.scan()[0].payload["cancel_after_submit"] is True
    assert outbox.scan()[0].payload["cancel_delivery_state"] == "rejected"
    assert cancel_calls == ["cancel"]

    restarted = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    restarted.poll({"principal": _principal(), "generation": 5})
    assert cancel_calls == ["cancel"]

    restarted.cancel_job(
        {"principal": _principal(), "job_id": "job-running", "generation": 5}
    )
    assert cancel_calls == ["cancel", "cancel"]
    assert outbox.scan()[0].payload["cancel_delivery_state"] == "rejected"


def test_reconcile_does_not_send_when_delivering_persistence_fails(tmp_path: Path):
    cancel_calls: list[str] = []

    class DeliveringFailOutbox(SecureIngestionOutbox):
        def mark_cancel_delivering(self, *args, **kwargs):
            del args, kwargs
            raise OSError("replace failed")

    outbox = DeliveringFailOutbox(tmp_path / "outbox", protector=_FakeProtector())
    tracked = outbox.create_cancel_tracking(
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
        job_id="job-running",
        filename="tracked.md",
        collection_id="engineering",
    )

    class Controller:
        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-running", "status": "running"}]

        @staticmethod
        def cancel_ingestion_job(*_args):
            cancel_calls.append("cancel")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.poll({"principal": _principal(), "generation": 4})

    assert cancel_calls == []
    assert outbox.load(tracked.entry_id).payload["cancel_delivery_state"] == "requested"
    assert any(item.get("status") == "outcome_unknown" for item in updates)


def test_crash_after_cancel_send_leaves_delivering_and_restart_never_resends(
    tmp_path: Path,
):
    cancel_calls: list[str] = []
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    tracked = outbox.create_cancel_tracking(
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
        job_id="job-running",
        filename="tracked.md",
        collection_id="engineering",
    )

    class Controller:
        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-running", "status": "running"}]

        @staticmethod
        def cancel_ingestion_job(*_args):
            cancel_calls.append("cancel")
            raise SystemExit("simulated process death after send")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )

    with pytest.raises(SystemExit, match="process death"):
        worker.poll({"principal": _principal(), "generation": 4})

    assert cancel_calls == ["cancel"]
    assert outbox.load(tracked.entry_id).payload["cancel_delivery_state"] == "delivering"
    assert outbox.scan()[0].payload["cancel_delivery_state"] == "unknown"

    app = QApplication.instance() or QApplication([])
    restarted = desktop_module.IngestionCoordinator(Controller(), outbox, "instance-1")
    updates: list[dict] = []
    restarted.updated.connect(updates.append)
    try:
        restarted.scan_recovery()
        restarted.refresh(_principal(), generation=5)
        deadline = time.monotonic() + 2
        while not any(item.get("status") == "snapshot" for item in updates) and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert any(item.get("status") == "snapshot" for item in updates)
        assert cancel_calls == ["cancel"]
    finally:
        assert restarted.shutdown(2000)


def test_recovery_scan_exposes_non_sensitive_cancel_intent(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "pending.md",
            "collection_id": "engineering",
            "content": b"pending",
            "idempotency_key": "pending-visible-cancel",
        }
    )
    outbox.mark_cancel_requested(
        pending.entry_id,
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
    )
    worker = desktop_module._IngestionWorker(
        object(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    recovered: list[list[dict]] = []
    worker.recovery.connect(recovered.append)

    worker.scan_recovery()

    assert recovered[0][0]["cancel_after_submit"] is True
    assert recovered[0][0]["cancel_delivery_state"] == "requested"


def test_cancel_job_forwards_only_safe_too_late_receipt_fields(tmp_path: Path):
    class Controller:
        @staticmethod
        def get_ingestion_job(_principal, job_id):
            return {
                "id": job_id,
                "status": "running",
                "filename": "existing.md",
                "collection_id": "engineering",
            }

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            return {
                "id": job_id,
                "status": "succeeded",
                "cancel_outcome": "too_late",
                "document_id": "doc-1",
                "requested_by": "editor-a",
                "progress": 100,
                "content_base64": "must-not-leak",
                "debug_trace": "must-not-leak",
            }

    worker = desktop_module._IngestionWorker(
        Controller(),
        SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
        desktop_module._IngestionCancellationRegistry(),
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.cancel_job(
        {"principal": _principal(), "job_id": "job-too-late", "generation": 2}
    )

    final = updates[-1]
    assert final["status"] == "succeeded"
    assert final["cancel_outcome"] == "too_late"
    assert final["document_id"] == "doc-1"
    assert final["requested_by"] == "editor-a"
    assert final["progress"] == 100
    assert "content_base64" not in final
    assert "debug_trace" not in final


def test_cancel_job_definite_4xx_is_rejected_not_outcome_unknown(tmp_path: Path):
    cancel_calls: list[str] = []

    class Controller:
        @staticmethod
        def get_ingestion_job(_principal, job_id):
            return {
                "id": job_id,
                "status": "running",
                "filename": "existing.md",
                "collection_id": "engineering",
            }

        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            cancel_calls.append("cancel")
            raise KnowledgeAssistantApiError(403, "permission_denied", "unsafe detail")

        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-1", "status": "running"}]

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(),
        outbox,
        "instance-1",
        desktop_module._IngestionCancellationRegistry(),
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.cancel_job({"principal": _principal(), "job_id": "job-1", "generation": 2})

    assert updates[-1]["status"] == "cancel_rejected"
    assert "unsafe detail" not in updates[-1]["message"]
    assert outbox.scan()[0].payload["cancel_delivery_state"] == "rejected"

    app = QApplication.instance() or QApplication([])
    restarted = desktop_module.IngestionCoordinator(Controller(), outbox, "instance-1")
    restart_updates: list[dict] = []
    restarted.updated.connect(restart_updates.append)
    try:
        restarted.scan_recovery()
        restarted.refresh(_principal(), generation=3)
        deadline = time.monotonic() + 2
        while not any(
            item.get("status") == "snapshot" for item in restart_updates
        ) and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert any(item.get("status") == "snapshot" for item in restart_updates)
        assert cancel_calls == ["cancel"]
    finally:
        assert restarted.shutdown(2000)


def test_cancel_without_local_tracking_is_durable_before_http_and_survives_restart(
    tmp_path: Path,
):
    events: list[str] = []

    class RecordingOutbox(SecureIngestionOutbox):
        def create_cancel_tracking(self, **kwargs):
            result = super().create_cancel_tracking(**kwargs)
            events.append("cancel-intent-durable")
            return result

    class Controller:
        @staticmethod
        def get_ingestion_job(_principal, job_id):
            events.append(f"get:{job_id}")
            return {
                "id": job_id,
                "status": "running",
                "filename": "existing.md",
                "collection_id": "engineering",
                "requested_by": "editor-a",
            }

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            events.append(f"cancel-http:{job_id}")
            raise ConnectionError("response lost")

        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-existing", "status": "running"}]

    outbox = RecordingOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.cancel_job(
        {"principal": _principal(), "job_id": "job-existing", "generation": 2}
    )

    assert events == [
        "get:job-existing",
        "cancel-intent-durable",
        "cancel-http:job-existing",
    ]
    assert updates[-1]["status"] == "outcome_unknown"
    stored = outbox.scan()[0].payload
    assert stored["state"] == "tracking"
    assert stored["cancel_delivery_state"] == "unknown"
    assert stored["job_id"] == "job-existing"

    app = QApplication.instance() or QApplication([])
    restarted = desktop_module.IngestionCoordinator(
        Controller(), outbox, "instance-1"
    )
    recovered: list[list[dict]] = []
    restarted.recovery_updated.connect(recovered.append)
    try:
        restarted.scan_recovery()
        restarted.refresh(_principal(), generation=3)
        deadline = time.monotonic() + 2
        while not recovered and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)

        assert recovered[0][0]["job_id"] == "job-existing"
        assert recovered[0][0]["requested_by"] == "editor-a"
        assert recovered[0][0]["cancel_delivery_state"] == "unknown"
        assert events.count("cancel-http:job-existing") == 1
    finally:
        assert restarted.shutdown(2000)


def test_direct_cancel_does_not_send_when_delivering_replace_fails(
    tmp_path: Path,
    monkeypatch,
):
    cancel_calls: list[str] = []
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    tracked = outbox.create_cancel_tracking(
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
        job_id="job-existing",
        filename="existing.md",
        collection_id="engineering",
    )
    original_replace = outbox_module._replace_with_write_through
    replace_calls = 0

    def fail_delivering_replace(source: Path, destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("delivering replace failed")
        original_replace(source, destination)

    monkeypatch.setattr(
        outbox_module,
        "_replace_with_write_through",
        fail_delivering_replace,
    )

    class Controller:
        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            cancel_calls.append("cancel")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.cancel_job(
        {"principal": _principal(), "job_id": "job-existing", "generation": 2}
    )

    assert cancel_calls == []
    assert outbox.load(tracked.entry_id).payload["cancel_delivery_state"] == "requested"
    assert updates[-1]["status"] == "outcome_unknown"
    assert "尚未发送" in updates[-1]["message"]


def test_direct_cancel_crash_leaves_delivering_and_restart_does_not_resend(
    tmp_path: Path,
):
    cancel_calls: list[str] = []
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    tracked = outbox.create_cancel_tracking(
        service_instance_id="instance-1",
        principal=desktop_module._principal_payload(_principal()),
        job_id="job-existing",
        filename="existing.md",
        collection_id="engineering",
    )

    class Controller:
        @staticmethod
        def list_ingestion_jobs(_principal):
            return [{"id": "job-existing", "status": "running"}]

        @staticmethod
        def cancel_ingestion_job(_principal, _job_id):
            cancel_calls.append("cancel")
            raise SystemExit("simulated process death after direct cancel send")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )

    with pytest.raises(SystemExit, match="direct cancel send"):
        worker.cancel_job(
            {"principal": _principal(), "job_id": "job-existing", "generation": 2}
        )

    assert cancel_calls == ["cancel"]
    assert outbox.load(tracked.entry_id).payload["cancel_delivery_state"] == "delivering"
    assert outbox.scan()[0].payload["cancel_delivery_state"] == "unknown"

    app = QApplication.instance() or QApplication([])
    restarted = desktop_module.IngestionCoordinator(Controller(), outbox, "instance-1")
    updates: list[dict] = []
    restarted.updated.connect(updates.append)
    try:
        restarted.scan_recovery()
        restarted.refresh(_principal(), generation=3)
        deadline = time.monotonic() + 2
        while not any(item.get("status") == "snapshot" for item in updates) and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert any(item.get("status") == "snapshot" for item in updates)
        assert cancel_calls == ["cancel"]
    finally:
        assert restarted.shutdown(2000)


def test_cancel_without_tracking_fails_closed_when_durable_write_fails(tmp_path: Path):
    calls: list[str] = []

    class FailingOutbox(SecureIngestionOutbox):
        def create_cancel_tracking(self, **kwargs):
            del kwargs
            raise OSError("disk unavailable")

    class Controller:
        @staticmethod
        def get_ingestion_job(_principal, job_id):
            return {
                "id": job_id,
                "status": "running",
                "filename": "existing.md",
                "collection_id": "engineering",
            }

        @staticmethod
        def cancel_ingestion_job(*_args):
            calls.append("cancel-http")

    worker = desktop_module._IngestionWorker(
        Controller(),
        FailingOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
        desktop_module._IngestionCancellationRegistry(),
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.cancel_job(
        {"principal": _principal(), "job_id": "job-existing", "generation": 2}
    )

    assert calls == []
    assert updates[-1]["status"] == "outcome_unknown"
    assert "尚未发送" in updates[-1]["message"]


def test_cancel_without_tracking_terminal_result_clears_new_tracking(tmp_path: Path):
    class Controller:
        @staticmethod
        def get_ingestion_job(_principal, job_id):
            return {
                "id": job_id,
                "status": "running",
                "filename": "existing.md",
                "collection_id": "engineering",
            }

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            return {"id": job_id, "status": "cancelled"}

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )

    worker.cancel_job(
        {"principal": _principal(), "job_id": "job-existing", "generation": 2}
    )

    assert outbox.scan() == []


def test_delete_failure_does_not_downgrade_known_success(tmp_path: Path):
    class DeleteFailingOutbox(SecureIngestionOutbox):
        def delete(self, entry_id: str) -> None:
            del entry_id
            raise OSError("disk busy")

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            return {"id": "job-success", "status": "succeeded"}

    source = tmp_path / "success.md"
    source.write_bytes(b"success")
    worker = desktop_module._IngestionWorker(
        Controller(),
        DeleteFailingOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
        desktop_module._IngestionCancellationRegistry(),
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(source))

    assert updates[-1]["status"] == "succeeded"
    assert "本地恢复记录" in updates[-1]["message"]


def test_snapshot_mismatch_fails_closed_before_outbox_or_network(tmp_path: Path):
    source = tmp_path / "changed.md"
    source.write_bytes(b"before")
    command = _command(source)
    source.write_bytes(b"after-content-with-different-size")
    calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            calls.append("network")

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(command)

    assert calls == []
    assert outbox.scan() == []
    assert updates[-1]["status"] == "failed"


def test_same_size_same_mtime_rewrite_fails_sha256_snapshot(tmp_path: Path):
    source = tmp_path / "rewritten.md"
    source.write_bytes(b"before")
    original = source.stat()
    command = _command(source)
    source.write_bytes(b"after!")
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            calls.append("network")

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(command)

    assert calls == []
    assert outbox.scan() == []
    assert updates[-1]["status"] == "failed"


def test_shutdown_after_read_but_before_outbox_never_persists_or_sends(tmp_path: Path):
    source = tmp_path / "shutdown-read.md"
    source.write_bytes(b"source")
    calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            calls.append("network")

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    worker._read_file = lambda *_args: b"source"
    worker._interrupted = lambda: True

    worker.process_batch(_command(source))

    assert calls == []
    assert outbox.scan() == []


def test_shutdown_after_outbox_keeps_pending_record_and_never_sends(tmp_path: Path):
    source = tmp_path / "shutdown-persisted.md"
    source.write_bytes(b"source")
    calls: list[str] = []
    interruption_checks = iter((False, True))

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            calls.append("network")

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    worker._read_file = lambda *_args: b"source"
    worker._interrupted = lambda: next(interruption_checks)
    updates: list[dict] = []
    worker.update.connect(updates.append)

    worker.process_batch(_command(source))

    assert calls == []
    assert outbox.scan()[0].payload["state"] == "pending"
    assert updates[-1]["status"] == "pending"


def test_queued_replay_does_not_write_after_shutdown_interruption(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "pending.md",
            "collection_id": "engineering",
            "content": b"pending",
            "idempotency_key": "pending-after-shutdown",
        }
    )
    calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            calls.append("network")

    worker = desktop_module._IngestionWorker(
        Controller(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    worker._interrupted = lambda: True

    worker.replay({"principal": _principal(), "entry_id": pending.entry_id, "generation": 3})

    assert calls == []
    assert outbox.scan()[0].payload["state"] == "pending"


def test_recovery_scan_does_not_expose_unattributable_corrupt_records(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    directory = tmp_path / "outbox"
    directory.mkdir()
    (directory / ("outbox-" + "a" * 32 + ".json")).write_text(
        "TOP SECRET malformed record", encoding="utf-8"
    )
    worker = desktop_module._IngestionWorker(
        object(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    scans: list[list[dict]] = []
    worker.recovery.connect(scans.append)

    worker.scan_recovery()

    assert scans == [[]]
    assert (directory / ("outbox-" + "a" * 32 + ".json")).exists()


def test_recovery_scan_hides_records_bound_to_another_service(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "other-instance",
            "principal": desktop_module._principal_payload(_principal()),
            "filename": "other-secret.md",
            "collection_id": "secret-collection",
            "content": b"secret",
            "idempotency_key": "other-instance-key",
        }
    )
    worker = desktop_module._IngestionWorker(
        object(), outbox, "instance-1", desktop_module._IngestionCancellationRegistry()
    )
    scans: list[list[dict]] = []
    worker.recovery.connect(scans.append)

    worker.scan_recovery()

    assert scans == [[]]
    assert len(outbox.scan()) == 1


def test_coordinator_construction_and_recovery_scan_never_submit_requests(tmp_path: Path):
    QApplication.instance() or QApplication([])

    class Controller:
        create_calls = 0

        def create_ingestion_job(self, *_args, **_kwargs):
            self.create_calls += 1
            raise AssertionError("must not auto replay")

    controller = Controller()
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    outbox.enqueue(
        {
            "operation": "create_ingestion_job",
            "service_instance_id": "instance-1",
            "principal": {
                "tenant_id": "tenant-a",
                "user_id": "editor-a",
                "roles": ["editor"],
                "collection_ids": ["engineering"],
            },
            "filename": "pending.md",
            "collection_id": "engineering",
            "content": b"pending exact bytes",
            "idempotency_key": "desktop-ingestion-pending-001",
        }
    )
    coordinator = desktop_module.IngestionCoordinator(controller, outbox, "instance-1")
    try:
        coordinator.scan_recovery()
        QApplication.processEvents()
        assert controller.create_calls == 0
    finally:
        assert coordinator.shutdown(2000)


def test_real_coordinator_api_and_service_complete_without_freezing_qt(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    controller = desktop_module.KnowledgeAssistantDesktopController(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )
    coordinator = desktop_module.IngestionCoordinator(
        controller,
        SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        controller.service_instance_id,
    )
    source = tmp_path / "real.md"
    source.write_text("background ingestion remains responsive", encoding="utf-8")
    updates: list[dict] = []
    coordinator.updated.connect(updates.append)
    try:
        coordinator.submit_files(
            _principal(),
            [source],
            collection_id="engineering",
            generation=1,
            expected_snapshots=[_snapshot(source)],
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            app.processEvents()
            coordinator.refresh(_principal(), generation=1)
            QTest.qWait(100)
            if any(
                item.get("status") == "snapshot"
                and any(job.get("status") == "succeeded" for job in item.get("jobs", []))
                for item in updates
            ):
                break

        assert any(item.get("status") == "queued" for item in updates)
        assert any(
            item.get("status") == "snapshot"
            and any(job.get("status") == "succeeded" for job in item.get("jobs", []))
            for item in updates
        )
        assert controller.list_documents(_principal())[0]["filename"] == "real.md"
    finally:
        assert coordinator.shutdown(2000)
        controller.close()


def test_late_local_cancel_after_worker_completion_is_durably_handed_off_once(
    tmp_path: Path,
):
    app = QApplication.instance() or QApplication([])
    worker_finished = threading.Event()
    delivery_events: list[str] = []

    class RecordingOutbox(SecureIngestionOutbox):
        def mark_cancel_requested(self, *args, **kwargs):
            result = super().mark_cancel_requested(*args, **kwargs)
            delivery_events.append("cancel-intent-durable")
            return result

        def mark_cancel_delivering(self, *args, **kwargs):
            result = super().mark_cancel_delivering(*args, **kwargs)
            delivery_events.append("cancel-delivering-durable")
            return result

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            return {"id": "job-late-cancel", "status": "queued"}

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            delivery_events.append(f"cancel-http:{job_id}")
            return {"id": job_id, "status": "cancelled"}

    source = tmp_path / "late-cancel.md"
    source.write_bytes(b"late cancel")
    outbox = RecordingOutbox(tmp_path / "outbox", protector=_FakeProtector())
    coordinator = desktop_module.IngestionCoordinator(
        Controller(),
        outbox,
        "instance-1",
    )
    coordinator._worker.item_finished.connect(
        worker_finished.set,
        Qt.ConnectionType.DirectConnection,
    )
    updates: list[dict] = []
    coordinator.updated.connect(updates.append)
    try:
        [local_id] = coordinator.submit_files(
            _principal(),
            [source],
            collection_id="engineering",
            generation=1,
            expected_snapshots=[_snapshot(source)],
        )
        assert worker_finished.wait(2)
        assert not any(update.get("job_id") for update in updates)

        assert coordinator.request_cancel_local(local_id) == "cancelling"

        deadline = time.monotonic() + 3
        while (
            delivery_events[-1:] != ["cancel-http:job-late-cancel"]
            or coordinator._control_outstanding
        ) and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)

        assert delivery_events == [
            "cancel-intent-durable",
            "cancel-delivering-durable",
            "cancel-http:job-late-cancel",
        ]
        assert sum(event.startswith("cancel-http:") for event in delivery_events) == 1
        assert any(
            update.get("status") == "cancelled" and update.get("local_id") == local_id
            for update in updates
        )
        assert outbox.scan() == []
    finally:
        deadline = time.monotonic() + 2
        while coordinator._item_active and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert coordinator.shutdown(2000)


def test_worker_handled_local_cancel_is_not_handed_off_twice(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    create_started = threading.Event()
    release_create = threading.Event()
    cancel_calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(*_args, **_kwargs):
            create_started.set()
            assert release_create.wait(2)
            return {"id": "job-worker-cancel", "status": "queued"}

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            cancel_calls.append(job_id)
            return {"id": job_id, "status": "cancelled"}

    source = tmp_path / "worker-cancel.md"
    source.write_bytes(b"worker cancel")
    coordinator = desktop_module.IngestionCoordinator(
        Controller(),
        SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
    )
    try:
        [local_id] = coordinator.submit_files(
            _principal(),
            [source],
            collection_id="engineering",
            generation=1,
            expected_snapshots=[_snapshot(source)],
        )
        assert create_started.wait(2)
        assert coordinator.request_cancel_local(local_id) == "cancelling"
        release_create.set()

        deadline = time.monotonic() + 3
        while (coordinator._item_active or coordinator._control_outstanding) and (
            time.monotonic() < deadline
        ):
            app.processEvents()
            QTest.qWait(10)

        assert cancel_calls == ["job-worker-cancel"]
    finally:
        release_create.set()
        deadline = time.monotonic() + 2
        while coordinator._item_active and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert coordinator.shutdown(2000)


def test_batch_items_yield_to_polling_between_files(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    first_started = threading.Event()
    release_first = threading.Event()
    events: list[str] = []

    class Controller:
        def create_ingestion_job(self, _principal, *, filename, **_kwargs):
            events.append(f"create:{filename}")
            if filename == "one.md":
                first_started.set()
                assert release_first.wait(2)
            return {"id": f"job-{filename}", "status": "queued"}

        @staticmethod
        def list_ingestion_jobs(_principal):
            events.append("poll")
            return []

    one = tmp_path / "one.md"
    two = tmp_path / "two.md"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    coordinator = desktop_module.IngestionCoordinator(
        Controller(),
        SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
    )
    try:
        coordinator.submit_files(
            _principal(),
            [one, two],
            collection_id="engineering",
            generation=1,
            expected_snapshots=[_snapshot(one), _snapshot(two)],
        )
        assert first_started.wait(2)
        coordinator.refresh(_principal(), generation=1)
        release_first.set()
        deadline = time.monotonic() + 3
        while (len(events) < 3 or coordinator._item_active) and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)

        assert events == ["create:one.md", "poll", "create:two.md"]
    finally:
        release_first.set()
        assert coordinator.shutdown(2000)


def test_shutdown_refuses_to_drop_unpersisted_following_batch_item(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    class Controller:
        def create_ingestion_job(self, _principal, *, filename, **_kwargs):
            calls.append(filename)
            if filename == "one.md":
                first_started.set()
                assert release_first.wait(2)
            return {"id": f"job-{filename}", "status": "queued"}

    one = tmp_path / "one.md"
    two = tmp_path / "two.md"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    coordinator = desktop_module.IngestionCoordinator(
        Controller(),
        SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
    )
    coordinator.submit_files(
        _principal(),
        [one, two],
        collection_id="engineering",
        generation=1,
        expected_snapshots=[_snapshot(one), _snapshot(two)],
    )
    assert first_started.wait(2)

    assert coordinator.shutdown(10) is False
    release_first.set()
    deadline = time.monotonic() + 3
    while (len(calls) < 2 or coordinator._item_active) and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)

    assert coordinator.shutdown(2000)
    assert calls == ["one.md", "two.md"]


def test_shutdown_refuses_to_drop_active_item_before_durable_outbox(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    read_started = threading.Event()
    release_read = threading.Event()
    calls: list[str] = []

    class Controller:
        @staticmethod
        def create_ingestion_job(_principal, *, filename, **_kwargs):
            calls.append(filename)
            return {"id": "job-one", "status": "queued"}

    source = tmp_path / "active.md"
    source.write_bytes(b"active")
    coordinator = desktop_module.IngestionCoordinator(
        Controller(),
        SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector()),
        "instance-1",
    )
    real_read = coordinator._worker._read_file

    def blocking_read(*args, **kwargs):
        read_started.set()
        assert release_read.wait(2)
        return real_read(*args, **kwargs)

    coordinator._worker._read_file = blocking_read
    coordinator.submit_files(
        _principal(),
        [source],
        collection_id="engineering",
        generation=1,
        expected_snapshots=[_snapshot(source)],
    )
    assert read_started.wait(2)

    assert coordinator.shutdown(10) is False
    release_read.set()
    deadline = time.monotonic() + 3
    while (coordinator._item_active or not calls) and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)

    assert calls == ["active.md"]
    assert coordinator.shutdown(2000)


def test_shutdown_refuses_to_drop_queued_cancel_control_command(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    cancel_calls: list[str] = []

    class Controller:
        @staticmethod
        def get_ingestion_job(_principal, job_id):
            return {
                "id": job_id,
                "status": "running",
                "filename": "existing.md",
                "collection_id": "engineering",
            }

        @staticmethod
        def cancel_ingestion_job(_principal, job_id):
            cancel_calls.append(job_id)
            return {"id": job_id, "status": "cancelled"}

    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    coordinator = desktop_module.IngestionCoordinator(
        Controller(), outbox, "instance-1"
    )
    coordinator.request_cancel_job(_principal(), "job-existing", generation=1)

    assert coordinator._control_outstanding == 1
    assert coordinator.shutdown(10) is False
    deadline = time.monotonic() + 2
    while coordinator._control_outstanding and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)

    assert cancel_calls == ["job-existing"]
    assert coordinator._control_outstanding == 0
    assert outbox.scan() == []
    assert coordinator.shutdown(2000)


def test_controller_stops_ingestion_worker_before_closing_owned_endpoint():
    events: list[str] = []

    class Coordinator:
        @staticmethod
        def shutdown(timeout_ms: int) -> bool:
            assert timeout_ms == 2000
            events.append("coordinator")
            return True

    class Endpoint:
        @staticmethod
        def close() -> bool:
            events.append("endpoint")
            return True

    controller = object.__new__(desktop_module.KnowledgeAssistantDesktopController)
    controller._ingestion_coordinator = Coordinator()
    controller.endpoint = Endpoint()

    controller.close()

    assert events == ["coordinator", "endpoint"]


def test_controller_keeps_endpoint_alive_when_worker_does_not_stop_in_time():
    events: list[str] = []

    class Coordinator:
        @staticmethod
        def shutdown(_timeout_ms: int) -> bool:
            events.append("worker-timeout")
            return False

    class Endpoint:
        @staticmethod
        def close() -> None:
            events.append("unsafe-endpoint-close")

    controller = object.__new__(desktop_module.KnowledgeAssistantDesktopController)
    coordinator = Coordinator()
    controller._ingestion_coordinator = coordinator
    controller.endpoint = Endpoint()

    assert not controller.close()
    assert controller._ingestion_coordinator is coordinator
    assert events == ["worker-timeout"]


def test_endpoint_and_controller_retain_slow_service_until_close_succeeds():
    events: list[str] = []

    class Service:
        outcomes = iter((False, True))

        def close(self) -> bool:
            outcome = next(self.outcomes)
            events.append(f"service:{outcome}")
            return outcome

    endpoint = object.__new__(desktop_module.ManagedKnowledgeAssistantEndpoint)
    endpoint.mode = "embedded"
    endpoint._server = None
    endpoint._thread = None
    service = Service()
    endpoint._service = service

    controller = object.__new__(desktop_module.KnowledgeAssistantDesktopController)
    controller._ingestion_coordinator = None
    controller.endpoint = endpoint

    assert controller.close() is False
    assert endpoint._service is service
    assert controller.close() is True
    assert endpoint._service is None
    assert events == ["service:False", "service:True"]


def test_external_endpoint_close_is_immediately_successful():
    endpoint = object.__new__(desktop_module.ManagedKnowledgeAssistantEndpoint)
    endpoint.mode = "external"
    endpoint._server = None
    endpoint._thread = None
    endpoint._service = None

    assert endpoint.close() is True
