from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from table_miku.knowledge_assistant import KnowledgeAssistantService, PermissionDenied, Principal
from table_miku.knowledge_assistant.auth import ConflictError, ResourceNotFound


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
        service.tasks.approve(requester, task["id"])

    completed = service.tasks.approve(approver(), task["id"])
    repeated = service.tasks.approve(approver(), task["id"])

    assert completed["status"] == "succeeded"
    assert completed["result"]["status"] == "indexed"
    assert completed["receipt"]["operation_id"] == task["id"]
    assert completed["receipt"]["approved_by"] == "human-1"
    assert repeated["result"]["id"] == completed["result"]["id"]
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_payloads").fetchone()[0] == 0


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

    failed = service.tasks.approve(approver(), task["id"])

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
    completed = service.tasks.approve(approver(), task["id"])
    assert completed["status"] == "succeeded"
    assert completed["result"]["archived"] is True
    assert service.documents.list_documents(requester) == []


def test_cross_tenant_task_access_and_approval_are_hidden(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor())

    with pytest.raises(ResourceNotFound):
        service.tasks.get(editor(tenant="tenant-b"), task["id"])
    with pytest.raises(ResourceNotFound):
        service.tasks.approve(approver(tenant="tenant-b"), task["id"])


def test_expired_approval_cancels_task_and_removes_payload(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    task = create_ingest_task(service, editor())
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="milliseconds")
    with service.database.connect() as conn:
        conn.execute("UPDATE approvals SET expires_at = ? WHERE task_id = ?", (expired, task["id"]))

    with pytest.raises(ConflictError, match="expired"):
        service.tasks.approve(approver(), task["id"])
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
