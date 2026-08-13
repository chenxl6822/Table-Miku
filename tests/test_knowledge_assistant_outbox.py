from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import table_miku.knowledge_assistant_outbox as outbox_module
from table_miku.knowledge_assistant_outbox import SecureIngestionOutbox


class _FakeProtector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"protected:"):
            raise ValueError("invalid protected payload")
        return value.removeprefix(b"protected:")[::-1]


def _request(content: bytes = b"private document bytes") -> dict[str, object]:
    return {
        "operation": "create_ingestion_job",
        "service_instance_id": "instance-test-001",
        "principal": {
            "tenant_id": "tenant-secret",
            "user_id": "editor-secret",
            "roles": ["editor"],
            "collection_ids": ["engineering"],
        },
        "filename": "private-plan.md",
        "collection_id": "engineering",
        "content": content,
        "idempotency_key": "desktop-ingestion-secret-key",
    }


def _empty_scope_request() -> dict[str, object]:
    request = _request()
    request["principal"] = {
        "tenant_id": "tenant-secret",
        "user_id": "editor-secret",
        "roles": ["editor"],
        "collection_ids": [],
    }
    return request


def test_outbox_round_trip_encrypts_the_complete_request(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())

    entry = outbox.enqueue(_request())
    restored = outbox.load(entry.entry_id)

    assert restored.payload["content"] == b"private document bytes"
    assert restored.payload["idempotency_key"] == "desktop-ingestion-secret-key"
    assert restored.payload["payload_sha256"]
    disk = entry.path.read_bytes()
    for secret in (
        b"private document bytes",
        b"private-plan.md",
        b"tenant-secret",
        b"editor-secret",
        b"engineering",
        b"desktop-ingestion-secret-key",
        b"create_ingestion_job",
    ):
        assert secret not in disk
    envelope = json.loads(disk)
    assert set(envelope) == {"schema_version", "entry_id", "created_at", "ciphertext"}


def test_outbox_rejects_arbitrary_operations_and_oversized_payloads(tmp_path: Path):
    outbox = SecureIngestionOutbox(
        tmp_path / "outbox",
        protector=_FakeProtector(),
        max_content_bytes=8,
    )
    arbitrary = _request(b"small")
    arbitrary["operation"] = "DELETE https://example.test/all"

    with pytest.raises(ValueError, match="operation"):
        outbox.enqueue(arbitrary)
    with pytest.raises(ValueError, match="byte limit"):
        outbox.enqueue(_request(b"x" * 9))
    assert list((tmp_path / "outbox").glob("*")) == []


def test_outbox_atomic_write_is_fsynced_before_replace(tmp_path: Path, monkeypatch):
    events: list[str] = []
    real_fsync = outbox_module.os.fsync
    real_replace = outbox_module._replace_with_write_through

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def recording_replace(source, target) -> None:
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(outbox_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(outbox_module, "_replace_with_write_through", recording_replace)
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())

    outbox.enqueue(_request())

    assert "fsync" in events
    assert events.index("fsync") < events.index("replace")


def test_outbox_protection_or_disk_failure_fails_closed(tmp_path: Path, monkeypatch):
    class FailingProtector:
        @staticmethod
        def protect(_value: bytes) -> bytes:
            raise OSError("DPAPI unavailable")

        @staticmethod
        def unprotect(_value: bytes) -> bytes:
            raise AssertionError("not called")

    outbox = SecureIngestionOutbox(tmp_path / "protect-fail", protector=FailingProtector())
    with pytest.raises(OSError, match="DPAPI"):
        outbox.enqueue(_request())
    assert list((tmp_path / "protect-fail").glob("*")) == []

    outbox = SecureIngestionOutbox(tmp_path / "disk-fail", protector=_FakeProtector())
    monkeypatch.setattr(
        outbox_module,
        "_replace_with_write_through",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        outbox.enqueue(_request())
    assert not list((tmp_path / "disk-fail").glob("*.json"))


def test_outbox_corruption_is_reported_and_never_deleted(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    entry = outbox.enqueue(_request())
    entry.path.write_text("{broken", encoding="utf-8")

    scanned = outbox.scan()

    assert len(scanned) == 1
    assert scanned[0].entry_id == entry.entry_id
    assert scanned[0].payload is None
    assert scanned[0].error
    assert entry.path.exists()


def test_pending_record_rejects_an_injected_method_or_url_field(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    entry = outbox.enqueue(_request())
    envelope = json.loads(entry.path.read_text(encoding="utf-8"))
    plaintext = outbox.protector.unprotect(
        base64.b64decode(envelope["ciphertext"])
    )
    payload = json.loads(plaintext)
    payload["method"] = "DELETE"
    forged = outbox.protector.protect(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    envelope["ciphertext"] = base64.b64encode(forged).decode("ascii")
    entry.path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="pending"):
        outbox.load(entry.entry_id)


def test_outbox_requires_exact_principal_and_service_binding(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    entry = outbox.enqueue(_request())
    principal = _request()["principal"]

    assert outbox.load_for_replay(
        entry.entry_id,
        service_instance_id="instance-test-001",
        principal=principal,
    ).payload["content"] == b"private document bytes"
    with pytest.raises(PermissionError, match="service"):
        outbox.load_for_replay(
            entry.entry_id,
            service_instance_id="other-instance",
            principal=principal,
        )
    with pytest.raises(PermissionError, match="identity"):
        outbox.load_for_replay(
            entry.entry_id,
            service_instance_id="instance-test-001",
            principal={**principal, "user_id": "other-editor"},
        )


def test_tracking_transition_removes_sensitive_payload_only_after_atomic_replace(
    tmp_path: Path,
):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    entry = outbox.enqueue(_request())

    tracked = outbox.mark_submitted(entry.entry_id, job_id="ingest-job-001")

    assert tracked.payload["state"] == "tracking"
    assert tracked.payload["job_id"] == "ingest-job-001"
    assert "content" not in tracked.payload
    assert "idempotency_key" not in tracked.payload
    disk = entry.path.read_bytes()
    assert b"private document bytes" not in disk
    assert b"ingest-job-001" not in disk


def test_cancel_intent_is_encrypted_and_survives_pending_to_tracking_transition(
    tmp_path: Path,
):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    pending = outbox.enqueue(_request())

    cancelled = outbox.mark_cancel_requested(
        pending.entry_id,
        service_instance_id="instance-test-001",
        principal=_request()["principal"],
    )
    tracked = outbox.mark_submitted(pending.entry_id, job_id="ingest-job-002")

    assert cancelled.payload["cancel_after_submit"] is True
    assert tracked.payload["cancel_after_submit"] is True
    assert tracked.payload["filename"] == "private-plan.md"
    assert tracked.payload["collection_id"] == "engineering"
    assert b"cancel_after_submit" not in pending.path.read_bytes()


def test_standalone_cancel_tracking_is_encrypted_and_contains_only_safe_metadata(
    tmp_path: Path,
):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())

    tracked = outbox.create_cancel_tracking(
        service_instance_id="instance-test-001",
        principal=_request()["principal"],
        job_id="job-existing-001",
        filename="existing.md",
        collection_id="engineering",
    )

    assert tracked.payload["state"] == "tracking"
    assert tracked.payload["job_id"] == "job-existing-001"
    assert tracked.payload["cancel_after_submit"] is True
    assert tracked.payload["cancel_delivery_state"] == "requested"
    assert "content" not in tracked.payload
    assert "idempotency_key" not in tracked.payload
    disk = tracked.path.read_bytes()
    for secret in (
        b"job-existing-001",
        b"existing.md",
        b"engineering",
        b"tenant-secret",
        b"editor-secret",
    ):
        assert secret not in disk


def test_delivering_cancel_is_exposed_as_unknown_after_restart(tmp_path: Path):
    directory = tmp_path / "outbox"
    outbox = SecureIngestionOutbox(directory, protector=_FakeProtector())
    tracked = outbox.create_cancel_tracking(
        service_instance_id="instance-test-001",
        principal=_request()["principal"],
        job_id="job-existing-001",
        filename="existing.md",
        collection_id="engineering",
    )

    outbox.mark_cancel_delivering(
        tracked.entry_id,
        service_instance_id="instance-test-001",
        principal=_request()["principal"],
    )

    restarted = SecureIngestionOutbox(directory, protector=_FakeProtector())
    assert restarted.load(tracked.entry_id).payload["cancel_delivery_state"] == "delivering"
    assert restarted.scan()[0].payload["cancel_delivery_state"] == "unknown"


def test_delivering_replace_failure_preserves_requested_record(
    tmp_path: Path,
    monkeypatch,
):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    tracked = outbox.create_cancel_tracking(
        service_instance_id="instance-test-001",
        principal=_request()["principal"],
        job_id="job-existing-001",
        filename="existing.md",
        collection_id="engineering",
    )
    monkeypatch.setattr(
        outbox_module,
        "_replace_with_write_through",
        lambda *_args: (_ for _ in ()).throw(OSError("replace interrupted")),
    )

    with pytest.raises(OSError, match="replace interrupted"):
        outbox.mark_cancel_delivering(
            tracked.entry_id,
            service_instance_id="instance-test-001",
            principal=_request()["principal"],
        )

    assert outbox.load(tracked.entry_id).payload["cancel_delivery_state"] == "requested"
    assert list((tmp_path / "outbox").glob(".*.tmp"))


def test_scan_returns_only_recovery_metadata_and_does_not_decode_document_bytes(
    tmp_path: Path,
    monkeypatch,
):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    outbox.enqueue(_request(b"document-body-sentinel"))

    def fail_decode(*_args, **_kwargs):
        raise AssertionError("scan must not base64-decode document bodies")

    monkeypatch.setattr(SecureIngestionOutbox, "_decode_content", staticmethod(fail_decode))

    scanned = outbox.scan()

    assert scanned[0].error == ""
    assert scanned[0].payload == {
        "state": "pending",
        "service_instance_id": "instance-test-001",
        "principal": _request()["principal"],
        "filename": "private-plan.md",
        "collection_id": "engineering",
        "job_id": "",
        "cancel_after_submit": False,
        "cancel_delivery_state": "none",
    }


def test_outbox_enforces_entry_and_total_disk_limits_before_persisting(tmp_path: Path):
    by_count = SecureIngestionOutbox(
        tmp_path / "count",
        protector=_FakeProtector(),
        max_entries=1,
    )
    by_count.enqueue(_request(b"first"))
    second = _request(b"second")
    second["idempotency_key"] = "second-key"
    with pytest.raises(OSError, match="entry limit"):
        by_count.enqueue(second)

    probe = SecureIngestionOutbox(tmp_path / "probe", protector=_FakeProtector())
    first = probe.enqueue(_request(b"first"))
    first_size = first.path.stat().st_size
    by_bytes = SecureIngestionOutbox(
        tmp_path / "bytes",
        protector=_FakeProtector(),
        max_disk_bytes=first_size + 8,
    )
    by_bytes.enqueue(_request(b"first"))
    with pytest.raises(OSError, match="disk byte limit"):
        by_bytes.enqueue(second)


def test_empty_collection_scope_remains_distinct_from_unrestricted_scope(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    empty = outbox.enqueue(_empty_scope_request())

    assert empty.payload["principal"]["collection_ids"] == []
    with pytest.raises(PermissionError, match="identity"):
        outbox.load_for_replay(
            empty.entry_id,
            service_instance_id="instance-test-001",
            principal={**_empty_scope_request()["principal"], "collection_ids": None},
        )


def test_failed_replace_retains_encrypted_tmp_for_conservative_recovery(
    tmp_path: Path,
    monkeypatch,
):
    outbox = SecureIngestionOutbox(tmp_path / "outbox", protector=_FakeProtector())
    monkeypatch.setattr(
        outbox_module,
        "_replace_with_write_through",
        lambda *_args: (_ for _ in ()).throw(OSError("replace interrupted")),
    )

    with pytest.raises(OSError, match="replace interrupted"):
        outbox.enqueue(_request())

    temporary = list((tmp_path / "outbox").glob(".*.tmp"))
    assert len(temporary) == 1
    assert b"private document bytes" not in temporary[0].read_bytes()
    assert any(item.error for item in outbox.scan())


def test_default_protector_fails_closed_when_dpapi_is_unavailable(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(outbox_module, "_WINDOWS_DPAPI_AVAILABLE", False)
    outbox = SecureIngestionOutbox(tmp_path / "outbox")

    with pytest.raises(OSError, match="Windows DPAPI"):
        outbox.enqueue(_request())


@pytest.mark.skipif(not outbox_module._WINDOWS_DPAPI_AVAILABLE, reason="Windows DPAPI only")
def test_real_windows_dpapi_round_trip(tmp_path: Path):
    outbox = SecureIngestionOutbox(tmp_path / "outbox")

    entry = outbox.enqueue(_request())

    assert outbox.load(entry.entry_id).payload["content"] == b"private document bytes"
