from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import table_miku.knowledge_assistant.tasks as tasks_module

from table_miku.knowledge_assistant import KnowledgeAssistantService, PermissionDenied, Principal
from table_miku.knowledge_assistant.auth import ConflictError, ResourceNotFound
from table_miku.knowledge_assistant.documents import request_digest
from table_miku.knowledge_assistant.tasks import APPROVAL_PREVIEW_VERSION


def editor(user_id: str = "agent-1", tenant: str = "tenant-a") -> Principal:
    return Principal(tenant, user_id, frozenset({"editor"}))


def approver(user_id: str = "human-1", tenant: str = "tenant-a") -> Principal:
    return Principal(tenant, user_id, frozenset({"approver"}))


def create_ingest_task(service: KnowledgeAssistantService, actor: Principal, key: str = "task-ingest-001"):
    return service.tasks.create(
        actor,
        tool_name="ingest_text",
        arguments={
            "filename": "agent-note.md",
            "collection_id": "engineering",
            "content": "# 幂等保护\n相同 operation_id 只能产生一次写入。",
        },
        idempotency_key=key,
    )


def create_work_item_task(
    service: KnowledgeAssistantService,
    actor: Principal,
    key: str = "task-work-item-001",
    *,
    title: str = "Follow up vendor contract",
    summary: str = "Open a local work item after legal review.",
    collection_id: str = "engineering",
    remote_idempotency_key: str = "remote-work-001",
):
    return service.tasks.create(
        actor,
        tool_name="create_work_item",
        arguments={
            "title": title,
            "summary": summary,
            "collection_id": collection_id,
            "remote_idempotency_key": remote_idempotency_key,
        },
        idempotency_key=key,
    )


def preview_and_approve(
    service: KnowledgeAssistantService,
    reviewer: Principal,
    task_id: str,
):
    preview = service.tasks.preview(reviewer, task_id)
    return preview, service.tasks.approve(reviewer, task_id, preview["preview_hash"])


def test_write_task_requires_separate_approval_and_creates_once_receipt(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = Principal("tenant-a", "agent-1", frozenset({"editor", "approver"}))
    task = create_ingest_task(service, requester)

    assert task["status"] == "awaiting_approval"
    assert task["approval"]["status"] == "pending"
    assert "content" not in task["arguments"]
    assert task["arguments"]["content_sha256"]
    with service.database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_payloads WHERE task_id = ?", (task["id"],)
        ).fetchone()[0] == 1

    with pytest.raises(PermissionDenied, match="own"):
        service.tasks.preview(requester, task["id"])

    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])
    with pytest.raises(PermissionDenied, match="own"):
        service.tasks.approve(requester, task["id"], preview["preview_hash"])
    completed = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])
    repeated = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])

    assert completed["status"] == "succeeded"
    assert completed["result"]["status"] == "indexed"
    assert completed["receipt"]["operation_id"] == task["id"]
    assert completed["receipt"]["approved_by"] == "human-1"
    assert completed["receipt"]["approved_preview_hash"] == preview["preview_hash"]
    assert "content" not in completed["receipt"]["arguments"]
    assert repeated["result"]["id"] == completed["result"]["id"]
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_payloads").fetchone()[0] == 0
        receipt_record = json.loads(
            conn.execute("SELECT result_json FROM operation_receipts").fetchone()[0]
        )
    assert receipt_record["approved_preview_hash"] == preview["preview_hash"]
    assert "相同 operation_id" not in json.dumps(receipt_record, ensure_ascii=False)


def test_task_creation_is_idempotent_and_key_reuse_conflicts(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    first = create_ingest_task(service, actor, "stable-task-key")
    replay = create_ingest_task(service, actor, "stable-task-key")

    assert replay["id"] == first["id"]
    assert replay["idempotent_replay"] is True
    with pytest.raises(ConflictError, match="different task"):
        service.tasks.create(
            actor,
            tool_name="ingest_text",
            arguments={
                "filename": "different.md",
                "collection_id": "engineering",
                "content": "different content",
            },
            idempotency_key="stable-task-key",
        )


def test_rejected_task_never_writes_and_discards_staged_payload(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor())

    rejected = service.tasks.reject(approver(), task["id"], "evidence is incomplete")
    repeated = service.tasks.reject(approver(), task["id"], "ignored")

    assert rejected["status"] == "rejected"
    assert rejected["approval"]["reason"] == "evidence is incomplete"
    assert repeated["status"] == "rejected"
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_payloads").fetchone()[0] == 0


def test_approved_tool_failure_is_persisted_without_retry_or_receipt(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = service.tasks.create(
        editor(),
        tool_name="ingest_text",
        arguments={"filename": "broken.json", "content": "{not-json", "collection_id": "default"},
        idempotency_key="task-failure-001",
    )

    _, failed = preview_and_approve(service, approver(), task["id"])

    assert failed["status"] == "failed"
    assert failed["error_code"] == "ValueError"
    assert "invalid JSON" in failed["error_message"]
    assert failed["receipt"] is None
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_payloads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM traces WHERE status = 'error'").fetchone()[0] >= 1


def test_read_tool_runs_without_approval_and_keeps_task_state(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    service.documents.upload(
        actor,
        filename="rag.txt",
        content="RAG 必须返回来源引用。".encode(),
        idempotency_key="read-tool-doc-001",
    )

    task = service.tasks.create(
        actor,
        tool_name="query_knowledge",
        arguments={"query": "RAG 为什么必须返回来源引用？"},
        idempotency_key="read-tool-task-001",
    )

    assert task["status"] == "succeeded"
    assert task["approval"] is None
    assert task["receipt"] is None
    assert task["result"]["refused"] is False


def test_archive_tool_is_soft_delete_and_requires_approval(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = editor()
    document = service.documents.upload(
        requester,
        filename="old.txt",
        content=b"legacy system",
        idempotency_key="archive-doc-001",
    )
    task = service.tasks.create(
        requester,
        tool_name="archive_document",
        arguments={"document_id": document["id"]},
        idempotency_key="archive-task-001",
    )

    assert task["status"] == "awaiting_approval"
    preview, completed = preview_and_approve(service, approver(), task["id"])
    assert preview["action"]["target"] == {
        "tenant_id": "tenant-a",
        "collection_id": "default",
        "document_id": document["id"],
        "filename": "old.txt",
        "checksum": document["checksum"],
    }
    assert preview["action"]["reversibility"] == "administrative_restore_required"
    assert completed["status"] == "succeeded"
    assert completed["result"]["archived"] is True
    assert service.documents.list_documents(requester) == []


def test_legacy_pending_archive_task_can_use_new_approval_preview(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = editor()
    document = service.documents.upload(
        requester,
        filename="legacy.txt",
        content=b"legacy pending task",
        idempotency_key="legacy-archive-document",
    )
    task = service.tasks.create(
        requester,
        tool_name="archive_document",
        arguments={"document_id": document["id"]},
        idempotency_key="legacy-archive-task",
    )
    legacy_arguments = {"document_id": document["id"]}
    legacy_hash = request_digest({"tool_name": "archive_document", "arguments": legacy_arguments})
    with service.database.connect() as conn:
        conn.execute(
            "UPDATE tasks SET arguments_json = ?, request_hash = ? WHERE id = ?",
            (
                json.dumps(legacy_arguments),
                legacy_hash,
                task["id"],
            ),
        )

    preview, completed = preview_and_approve(service, approver(), task["id"])

    assert preview["action"]["target"]["filename"] == "legacy.txt"
    assert preview["action"]["target"]["collection_id"] == "default"
    assert completed["status"] == "succeeded"
    assert completed["receipt"]["arguments"] == {
        "document_id": document["id"],
        "filename": "legacy.txt",
        "collection_id": "default",
        "checksum": document["checksum"],
    }


def test_legacy_archive_preview_binds_enriched_document_metadata(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = editor()
    document = service.documents.upload(
        requester,
        filename="legacy-bound.txt",
        content=b"legacy approval target",
        idempotency_key="legacy-bound-document",
    )
    task = service.tasks.create(
        requester,
        tool_name="archive_document",
        arguments={"document_id": document["id"]},
        idempotency_key="legacy-bound-task",
    )
    legacy_arguments = {"document_id": document["id"]}
    legacy_hash = request_digest({"tool_name": "archive_document", "arguments": legacy_arguments})
    with service.database.connect() as conn:
        conn.execute(
            "UPDATE tasks SET arguments_json = ?, request_hash = ? WHERE id = ?",
            (json.dumps(legacy_arguments), legacy_hash, task["id"]),
        )
    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])
    with service.database.connect() as conn:
        conn.execute(
            "UPDATE documents SET filename = ? WHERE id = ?",
            ("changed-after-preview.txt", document["id"]),
        )

    with pytest.raises(ConflictError, match="preview"):
        service.tasks.approve(reviewer, task["id"], preview["preview_hash"])

    unchanged = service.documents.get_document(requester, document["id"])
    assert unchanged["archived"] is False
    assert service.tasks.get(requester, task["id"])["receipt"] is None


def test_legacy_receipt_without_preview_contract_remains_readable(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor(), "legacy-receipt-task")
    _, completed = preview_and_approve(service, approver(), task["id"])
    with service.database.connect() as conn:
        conn.execute(
            "UPDATE operation_receipts SET result_json = ? WHERE task_id = ?",
            (json.dumps(completed["result"], ensure_ascii=False), task["id"]),
        )

    restored = service.tasks.get(editor(), task["id"])

    assert restored["receipt"]["approved_preview_hash"] is None
    assert restored["receipt"]["result"] == completed["result"]


def test_cross_tenant_task_access_and_approval_are_hidden(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor())

    with pytest.raises(ResourceNotFound):
        service.tasks.get(editor(tenant="tenant-b"), task["id"])
    with pytest.raises(ResourceNotFound):
        service.tasks.preview(approver(tenant="tenant-b"), task["id"])
    with pytest.raises(ResourceNotFound):
        service.tasks.approve(approver(tenant="tenant-b"), task["id"], "0" * 64)


def test_expired_approval_cancels_task_and_removes_payload(tmp_path: Path, monkeypatch):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor())
    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])
    expires_at = datetime.fromisoformat(task["approval"]["expires_at"])

    class ExpiredDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            expired = expires_at + timedelta(seconds=1)
            return expired if tz is not None else expired.replace(tzinfo=None)

    monkeypatch.setattr(tasks_module, "datetime", ExpiredDateTime)
    with pytest.raises(ConflictError, match="expired"):
        service.tasks.approve(reviewer, task["id"], preview["preview_hash"])
    cancelled = service.tasks.get(editor(), task["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["approval"]["status"] == "expired"
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_payloads").fetchone()[0] == 0


def test_viewer_cannot_create_agent_task(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    viewer = Principal("tenant-a", "viewer-1", frozenset({"viewer"}))

    with pytest.raises(PermissionDenied, match="task:create"):
        service.tasks.create(
            viewer,
            tool_name="query_knowledge",
            arguments={"query": "Spring IoC"},
            idempotency_key="viewer-task-001",
        )


def test_process_restart_fails_interrupted_task_without_automatic_retry(tmp_path: Path):
    database_path = tmp_path / "assistant.db"
    service = KnowledgeAssistantService(database_path)
    task = create_ingest_task(service, editor())
    with service.database.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task["id"],))

    restored = KnowledgeAssistantService(database_path)
    interrupted = restored.tasks.get(editor(), task["id"])

    assert interrupted["status"] == "failed"
    assert interrupted["error_code"] == "interrupted"
    assert "inspect side effects" in interrupted["error_message"]
    with restored.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_payloads").fetchone()[0] == 0


def test_approval_preview_is_exact_and_not_exposed_by_regular_task_reads(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = editor()
    content = "# Action Preview\n<script>render as plain text</script>\n审批人必须看到完整正文。"
    task = service.tasks.create(
        requester,
        tool_name="ingest_text",
        arguments={
            "filename": "preview.md",
            "collection_id": "engineering",
            "content": content,
        },
        idempotency_key="approval-preview-001",
    )
    reviewer = Principal(
        "tenant-a",
        "human-1",
        frozenset({"approver"}),
        frozenset({"engineering"}),
    )

    assert content not in str(task)
    regular_task = service.tasks.get(requester, task["id"])
    assert content not in str(regular_task)
    assert "request_hash" not in regular_task
    assert "preview_hash" not in str(regular_task)
    assert content not in str(service.tasks.list(requester))
    with pytest.raises(PermissionDenied, match="task:approve"):
        service.tasks.preview(Principal("tenant-a", "viewer-1", frozenset({"viewer"})), task["id"])
    with pytest.raises(PermissionDenied, match="own"):
        service.tasks.preview(
            Principal("tenant-a", "agent-1", frozenset({"editor", "approver"})),
            task["id"],
        )

    preview = service.tasks.preview(reviewer, task["id"])

    assert preview["task_id"] == task["id"]
    assert preview["preview_version"] == APPROVAL_PREVIEW_VERSION
    assert len(preview["preview_hash"]) == 64
    assert preview["provenance"] == {
        "origin": "agent_tool_request",
        "requested_by": "agent-1",
        "input_trust": "unverified",
    }
    assert preview["decision"]["bound_approver"] == "human-1"
    assert preview["action"]["intent"] == "ensure_indexed"
    assert preview["action"]["target"] == {
        "tenant_id": "tenant-a",
        "collection_id": "engineering",
        "filename": "preview.md",
    }
    assert preview["action"]["parameters"]["content"] == content
    assert preview["action"]["parameters"]["render_as"] == "plain_text"
    assert preview["action"]["parameters"]["content_sha256"] == task["arguments"]["content_sha256"]
    assert preview["action"]["consequences"]


def test_approval_preview_hash_is_server_issued_and_bound_to_approver(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor(), "approval-preview-token")
    reviewer = approver("reviewer-a")
    other_reviewer = approver("reviewer-b")
    regular_task = service.tasks.get(reviewer, task["id"])
    computed_request_hash = request_digest(
        {
            "tool_name": regular_task["tool_name"],
            "arguments": regular_task["arguments"],
        }
    )
    client_computed_hash = request_digest(
        {
            "preview_version": APPROVAL_PREVIEW_VERSION,
            "task_id": regular_task["id"],
            "tenant_id": regular_task["tenant_id"],
            "approval_id": regular_task["approval"]["id"],
            "request_hash": computed_request_hash,
            "expires_at": regular_task["approval"]["expires_at"],
        }
    )

    with pytest.raises(ConflictError, match="preview"):
        service.tasks.approve(reviewer, task["id"], client_computed_hash)

    preview = service.tasks.preview(reviewer, task["id"])
    other_preview = service.tasks.preview(other_reviewer, task["id"])
    assert preview["decision"]["bound_approver"] == "reviewer-a"
    assert other_preview["decision"]["bound_approver"] == "reviewer-b"
    assert other_preview["preview_hash"] != preview["preview_hash"]
    with pytest.raises(ConflictError, match="preview"):
        service.tasks.approve(other_reviewer, task["id"], preview["preview_hash"])

    completed = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])
    assert completed["status"] == "succeeded"
    with pytest.raises(ConflictError, match="preview"):
        service.tasks.approve(other_reviewer, task["id"], preview["preview_hash"])


def test_pending_preview_and_completed_replay_survive_restart(tmp_path: Path):
    database_path = tmp_path / "assistant.db"
    service = KnowledgeAssistantService(database_path)
    reviewer = approver()
    pending = create_ingest_task(service, editor(), "restart-pending-preview")
    preview = service.tasks.preview(reviewer, pending["id"])

    restarted = KnowledgeAssistantService(database_path)
    completed = restarted.tasks.approve(
        reviewer,
        pending["id"],
        preview["preview_hash"],
    )

    restarted_again = KnowledgeAssistantService(database_path)
    replayed = restarted_again.tasks.approve(
        reviewer,
        pending["id"],
        preview["preview_hash"],
    )
    assert replayed["status"] == "succeeded"
    assert replayed["receipt"] == completed["receipt"]


def test_invalid_persisted_approval_signing_key_fails_closed(tmp_path: Path):
    database_path = tmp_path / "assistant.db"
    KnowledgeAssistantService(database_path)
    key_path = database_path.with_name(f"{database_path.name}.approval-hmac-key")
    key_path.write_bytes(b"invalid")

    with pytest.raises(RuntimeError, match="signing key"):
        KnowledgeAssistantService(database_path)


def test_approval_hash_and_staged_payload_integrity_fail_closed(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor(), "approval-integrity-001")
    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])

    with pytest.raises(ValueError, match="preview_hash"):
        service.tasks.approve(reviewer, task["id"], "")
    with pytest.raises(ConflictError, match="preview"):
        service.tasks.approve(reviewer, task["id"], "0" * 64)

    with service.database.connect() as conn:
        conn.execute("UPDATE task_payloads SET payload = ? WHERE task_id = ?", (b"tampered", task["id"]))
    with pytest.raises(ConflictError, match="payload"):
        service.tasks.approve(reviewer, task["id"], preview["preview_hash"])

    unchanged = service.tasks.get(editor(), task["id"])
    assert unchanged["status"] == "awaiting_approval"
    assert unchanged["receipt"] is None
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0

    second = create_ingest_task(service, editor(), "approval-integrity-002")
    second_preview = service.tasks.preview(reviewer, second["id"])
    with service.database.connect() as conn:
        conn.execute(
            "UPDATE tasks SET arguments_json = ? WHERE id = ?",
            ('{"filename":"changed.md","collection_id":"engineering"}', second["id"]),
        )
    with pytest.raises(ConflictError, match="request"):
        service.tasks.approve(reviewer, second["id"], second_preview["preview_hash"])


def test_execution_revalidates_payload_after_approval_transition(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor(), "approval-execute-integrity")
    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])
    original_execute = service.tasks._execute

    def tampering_execute(
        task_id: str,
        principal: Principal,
        *,
        approved_by: str = "",
        approved_preview_hash: str = "",
        approved_arguments: dict | None = None,
    ):
        with service.database.connect() as conn:
            conn.execute("UPDATE task_payloads SET payload = ? WHERE task_id = ?", (b"tampered", task_id))
        return original_execute(
            task_id,
            principal,
            approved_by=approved_by,
            approved_preview_hash=approved_preview_hash,
            approved_arguments=approved_arguments,
        )

    service.tasks._execute = tampering_execute  # type: ignore[method-assign]
    failed = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])

    assert failed["status"] == "failed"
    assert failed["error_code"] == "ConflictError"
    assert "payload" in failed["error_message"]
    assert failed["receipt"] is None
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 0


def test_execution_revalidates_archive_target_after_approval_transition(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = editor()
    document = service.documents.upload(
        requester,
        filename="archive-target.txt",
        content=b"archive target",
        idempotency_key="archive-target-document",
    )
    task = service.tasks.create(
        requester,
        tool_name="archive_document",
        arguments={"document_id": document["id"]},
        idempotency_key="archive-target-task",
    )
    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])
    original_execute = service.tasks._execute

    def tampering_execute(task_id: str, principal: Principal, **kwargs):
        with service.database.connect() as conn:
            conn.execute(
                "UPDATE documents SET filename = ? WHERE id = ?",
                ("changed-before-execution.txt", document["id"]),
            )
        return original_execute(task_id, principal, **kwargs)

    service.tasks._execute = tampering_execute  # type: ignore[method-assign]
    failed = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])

    assert failed["status"] == "failed"
    assert failed["error_code"] == "ConflictError"
    assert "archive target" in failed["error_message"]
    assert service.documents.get_document(requester, document["id"])["archived"] is False
    assert failed["receipt"] is None


def test_corrupt_arguments_after_approval_return_failed_state_without_breaking_list(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor(), "approval-execute-arguments-integrity")
    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])
    original_execute = service.tasks._execute

    def tampering_execute(task_id: str, principal: Principal, **kwargs):
        with service.database.connect() as conn:
            conn.execute("UPDATE tasks SET arguments_json = '{}' WHERE id = ?", (task_id,))
        return original_execute(task_id, principal, **kwargs)

    service.tasks._execute = tampering_execute  # type: ignore[method-assign]
    failed = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])

    assert failed["status"] == "failed"
    assert failed["error_code"] == "ConflictError"
    assert failed["arguments"] == {}
    assert failed["arguments_integrity"] == "failed"
    unrestricted = service.tasks.get(reviewer, task["id"])
    assert unrestricted["status"] == "failed"
    assert [item["id"] for item in service.tasks.list(reviewer)] == [task["id"]]

    restricted_reviewer = Principal(
        "tenant-a",
        "scoped-reviewer",
        frozenset({"approver"}),
        frozenset({"engineering"}),
    )
    with pytest.raises(PermissionDenied, match="scope"):
        service.tasks.get(restricted_reviewer, task["id"])
    assert service.tasks.list(restricted_reviewer) == []


def test_concurrent_duplicate_approval_executes_write_once(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor(), "concurrent-approval-task")
    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])
    entered = threading.Event()
    release = threading.Event()
    original_upload = service.documents.upload

    def delayed_upload(principal: Principal, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original_upload(principal, **kwargs)

    service.documents.upload = delayed_upload  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service.tasks.approve,
            reviewer,
            task["id"],
            preview["preview_hash"],
        )
        assert entered.wait(timeout=3)
        duplicate = executor.submit(
            service.tasks.approve,
            reviewer,
            task["id"],
            preview["preview_hash"],
        ).result(timeout=3)
        release.set()
        completed = first.result(timeout=3)

    repeated = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])

    assert duplicate["status"] == "running"
    assert completed["status"] == "succeeded"
    assert repeated["receipt"] == completed["receipt"]
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 1


def test_task_collection_scope_applies_to_read_preview_reject_and_execution(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = editor()
    engineering_task = service.tasks.create(
        requester,
        tool_name="ingest_text",
        arguments={"filename": "eng.txt", "collection_id": "engineering", "content": "engineering"},
        idempotency_key="scope-task-engineering",
    )
    hr_task = service.tasks.create(
        requester,
        tool_name="ingest_text",
        arguments={"filename": "hr.txt", "collection_id": "hr", "content": "human resources"},
        idempotency_key="scope-task-human-resources",
    )
    engineering_reviewer = Principal(
        "tenant-a",
        "engineering-reviewer",
        frozenset({"approver"}),
        frozenset({"engineering"}),
    )
    engineering_viewer = Principal(
        "tenant-a",
        "engineering-viewer",
        frozenset({"viewer"}),
        frozenset({"engineering"}),
    )
    global_reviewer = approver("global-reviewer")
    unrestricted_preview = service.tasks.preview(global_reviewer, hr_task["id"])

    with pytest.raises(PermissionDenied, match="collection"):
        service.tasks.get(engineering_viewer, hr_task["id"])
    assert [item["id"] for item in service.tasks.list(engineering_viewer)] == [engineering_task["id"]]
    with pytest.raises(PermissionDenied, match="collection"):
        service.tasks.preview(engineering_reviewer, hr_task["id"])
    with pytest.raises(PermissionDenied, match="collection"):
        service.tasks.approve(
            engineering_reviewer,
            hr_task["id"],
            unrestricted_preview["preview_hash"],
        )
    with pytest.raises(PermissionDenied, match="collection"):
        service.tasks.reject(engineering_reviewer, hr_task["id"], "outside scope")

    rejected = service.tasks.reject(global_reviewer, hr_task["id"], "not approved")
    assert rejected["status"] == "rejected"
    with pytest.raises(PermissionDenied, match="collection"):
        service.tasks.preview(engineering_reviewer, hr_task["id"])
    with pytest.raises(PermissionDenied, match="collection"):
        service.tasks.reject(engineering_reviewer, hr_task["id"], "outside scope")

    seen_execution_scopes: list[frozenset[str] | None] = []
    original_upload = service.documents.upload

    def recording_upload(principal: Principal, **kwargs):
        seen_execution_scopes.append(principal.collection_ids)
        return original_upload(principal, **kwargs)

    service.documents.upload = recording_upload  # type: ignore[method-assign]
    _, completed = preview_and_approve(service, engineering_reviewer, engineering_task["id"])

    assert completed["status"] == "succeeded"
    assert seen_execution_scopes == [frozenset({"engineering"})]
    assert service.tasks.get(requester, hr_task["id"])["status"] == "rejected"


def test_restricted_query_task_persists_effective_collection_scope(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    restricted_editor = Principal(
        "tenant-a",
        "scoped-editor",
        frozenset({"editor"}),
        frozenset({"engineering"}),
    )

    task = service.tasks.create(
        restricted_editor,
        tool_name="query_knowledge",
        arguments={"query": "Spring IoC"},
        idempotency_key="restricted-query-task",
    )

    assert task["arguments"]["collection_ids"] == ["engineering"]


def test_create_work_item_requires_approval_and_writes_ledger_once(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = Principal("tenant-a", "agent-1", frozenset({"editor", "approver"}))
    summary = "Open a local work item after legal review."
    task = create_work_item_task(service, requester, summary=summary)

    assert task["status"] == "awaiting_approval"
    assert "summary" not in task["arguments"]
    assert "content" not in task["arguments"]
    assert task["arguments"]["title"] == "Follow up vendor contract"
    assert task["arguments"]["collection_id"] == "engineering"
    assert task["arguments"]["remote_idempotency_key"] == "remote-work-001"
    assert task["arguments"]["summary_sha256"]
    assert task["arguments"]["byte_size"] == len(summary.encode("utf-8"))
    with pytest.raises(PermissionDenied, match="own"):
        service.tasks.preview(requester, task["id"])
    with service.database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_payloads WHERE task_id = ?", (task["id"],)
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0

    reviewer = approver()
    preview = service.tasks.preview(reviewer, task["id"])
    with pytest.raises(PermissionDenied, match="own"):
        service.tasks.approve(requester, task["id"], preview["preview_hash"])
    completed = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])
    repeated = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])

    assert completed["status"] == "succeeded"
    assert completed["result"]["title"] == "Follow up vendor contract"
    assert completed["result"]["status"] == "open"
    assert completed["result"]["remote_idempotency_key"] == "remote-work-001"
    assert completed["result"]["idempotent_replay"] is False
    assert "summary" not in completed["result"]
    assert "summary" not in completed["arguments"]
    assert summary not in str(completed)
    assert completed["receipt"]["approved_by"] == "human-1"
    assert repeated["result"]["id"] == completed["result"]["id"]
    with service.database.connect() as conn:
        row = conn.execute("SELECT * FROM work_items").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_payloads").fetchone()[0] == 0
        assert row["title"] == "Follow up vendor contract"
        assert row["summary"] == summary
        assert row["tenant_id"] == "tenant-a"
        assert row["approved_by"] == "human-1"
        assert row["task_id"] == task["id"]


def test_create_work_item_preview_keeps_summary_out_of_task_reads(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    requester = editor()
    summary = "UNTRUSTED WORK ITEM BODY <script>alert(1)</script>"
    task = create_work_item_task(service, requester, summary=summary)
    reviewer = approver()

    listed = service.tasks.list(requester)
    loaded = service.tasks.get(requester, task["id"])
    assert summary not in str(task)
    assert summary not in str(listed)
    assert summary not in str(loaded)
    assert "request_hash" not in loaded

    preview = service.tasks.preview(reviewer, task["id"])
    assert preview["action"]["tool_name"] == "create_work_item"
    assert preview["action"]["intent"] == "ensure_work_item"
    assert preview["action"]["target"] == {
        "tenant_id": "tenant-a",
        "collection_id": "engineering",
        "title": "Follow up vendor contract",
        "remote_idempotency_key": "remote-work-001",
    }
    assert preview["action"]["parameters"]["content"] == summary
    assert preview["action"]["parameters"]["render_as"] == "plain_text"
    assert preview["action"]["parameters"]["summary_sha256"] == task["arguments"]["summary_sha256"]
    assert any("work-item ledger" in str(item).casefold() for item in preview["action"]["consequences"])
    assert preview["decision"]["bound_approver"] == "human-1"

    completed = service.tasks.approve(reviewer, task["id"], preview["preview_hash"])
    with service.database.connect() as conn:
        attributes = " ".join(
            str(row[0])
            for row in conn.execute(
                "SELECT attributes_json FROM traces UNION ALL SELECT attributes_json FROM spans"
            )
        )
    assert summary not in attributes
    assert "remote-work-001" not in attributes
    assert summary not in str(completed["receipt"])


def test_create_work_item_remote_key_replays_same_request_and_conflicts_on_different_request(
    tmp_path: Path,
):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    reviewer = approver()
    first = create_work_item_task(service, actor, "http-work-item-a")
    duplicate = create_work_item_task(service, actor, "http-work-item-b")
    _, first_completed = preview_and_approve(service, reviewer, first["id"])
    _, replayed = preview_and_approve(service, reviewer, duplicate["id"])

    assert first_completed["status"] == "succeeded"
    assert replayed["status"] == "succeeded"
    assert replayed["result"]["id"] == first_completed["result"]["id"]
    assert replayed["result"]["idempotent_replay"] is True
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1

    conflicting = create_work_item_task(
        service,
        actor,
        "http-work-item-c",
        summary="A different request using the same remote key.",
    )
    _, failed = preview_and_approve(service, reviewer, conflicting["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "ConflictError"
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1


def test_create_work_item_empty_collection_allowlist_is_deny_all(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    deny_all = Principal("tenant-a", "editor-1", frozenset({"editor"}), frozenset())
    with pytest.raises(PermissionDenied, match="collection"):
        create_work_item_task(service, deny_all)


def test_create_work_item_is_tenant_isolated(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    tenant_a = editor()
    tenant_b = editor(tenant="tenant-b")
    reviewer_a = approver()
    reviewer_b = approver(tenant="tenant-b")
    first = create_work_item_task(service, tenant_a, "http-tenant-a")
    _, completed_a = preview_and_approve(service, reviewer_a, first["id"])
    with pytest.raises(ResourceNotFound):
        service.tasks.get(tenant_b, first["id"])
    second = create_work_item_task(service, tenant_b, "http-tenant-b")
    _, completed_b = preview_and_approve(service, reviewer_b, second["id"])
    assert completed_a["result"]["id"] != completed_b["result"]["id"]
    with service.database.connect() as conn:
        rows = conn.execute(
            "SELECT tenant_id, remote_idempotency_key FROM work_items ORDER BY tenant_id"
        ).fetchall()
        assert [(row["tenant_id"], row["remote_idempotency_key"]) for row in rows] == [
            ("tenant-a", "remote-work-001"),
            ("tenant-b", "remote-work-001"),
        ]


def test_create_work_item_validation_bounds(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = editor()
    with pytest.raises(ValueError, match="title"):
        create_work_item_task(service, actor, "bad-title", title="")
    with pytest.raises(ValueError, match="summary"):
        create_work_item_task(service, actor, "bad-summary", summary="")
    with pytest.raises(ValueError, match="remote_idempotency_key"):
        create_work_item_task(
            service,
            actor,
            "bad-remote",
            remote_idempotency_key="short",
        )
    with pytest.raises(ValueError, match="unknown tool"):
        service.tasks.create(
            actor,
            tool_name="create_ticket_http",
            arguments={"title": "no"},
            idempotency_key="unknown-tool-key",
        )
