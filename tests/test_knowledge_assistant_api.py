from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import table_miku.knowledge_assistant.api as api_module
from table_miku.knowledge_assistant.api import KnowledgeAssistantApi
from table_miku.knowledge_assistant.service import KnowledgeAssistantService


def call_api(
    api: KnowledgeAssistantApi,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
    }
    for key, value in (headers or {}).items():
        environ[f"HTTP_{key.upper().replace('-', '_')}"] = value
    captured: dict[str, Any] = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    response = b"".join(api(environ, start_response))
    if response_headers is not None:
        response_headers.update(captured["headers"])
    return str(captured["status"]), json.loads(response)


def auth_headers(role: str = "editor", user: str = "user-1") -> dict[str, str]:
    return {"X-Tenant-ID": "tenant-a", "X-User-ID": user, "X-Roles": role}


def test_health_is_public_but_data_routes_require_identity(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    service.start()
    try:
        api = KnowledgeAssistantApi(service)

        health_status, health = call_api(api, "GET", "/health")
        denied_status, denied = call_api(api, "GET", "/v1/documents")

        assert health_status == "200 OK"
        assert health["status"] == "ok"
        assert health["schema_version"] == 3
        assert health["service_instance_id"].startswith("ka-")
        assert health["embedding_model"] == "local-hash-v1-384"
        assert health["ingestion"] == {
            "status": "ready",
            "started": True,
            "worker_alive": True,
            "heartbeat_alive": True,
            "lease_owned": True,
        }
        assert denied_status == "403 Forbidden"
        assert denied["error"]["code"] == "permission_denied"
    finally:
        service.close()


def test_health_is_503_before_service_start(tmp_path: Path):
    api = KnowledgeAssistantApi(KnowledgeAssistantService(tmp_path / "assistant.db"))

    status, health = call_api(api, "GET", "/health")

    assert status == "503 Service Unavailable"
    assert health["status"] == "degraded"
    assert health["ingestion"]["status"] == "degraded"
    assert health["ingestion"]["started"] is False


def test_health_is_503_when_started_worker_stops(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    service.start()
    worker = service.ingestion._worker
    assert worker is not None
    service.ingestion._stop.set()
    worker.join(timeout=3)
    try:
        status, health = call_api(KnowledgeAssistantApi(service), "GET", "/health")

        assert status == "503 Service Unavailable"
        assert health["status"] == "degraded"
        assert health["ingestion"]["started"] is True
        assert health["ingestion"]["worker_alive"] is False
    finally:
        service.close()


def test_health_is_503_when_worker_lease_is_lost(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    service.start()
    try:
        with service.database.connect() as conn:
            conn.execute(
                "UPDATE worker_leases SET owner_id = 'fenced-worker' "
                "WHERE name = 'knowledge-assistant-ingestion'"
            )

        status, health = call_api(KnowledgeAssistantApi(service), "GET", "/health")

        assert status == "503 Service Unavailable"
        assert health["status"] == "degraded"
        assert health["ingestion"]["lease_owned"] is False
    finally:
        service.close()


def test_explicit_empty_collection_scope_is_deny_all_not_unrestricted(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    unrestricted = auth_headers("editor", "editor-a")
    unrestricted["Idempotency-Key"] = "empty-scope-seed-001"
    upload_status, _ = call_api(
        KnowledgeAssistantApi(service),
        "POST",
        "/v1/documents",
        body={
            "filename": "visible.txt",
            "content_base64": base64.b64encode(b"visible only when unrestricted").decode(),
        },
        headers=unrestricted,
    )
    deny_all = auth_headers("viewer", "viewer-empty")
    deny_all["X-Collection-Scope"] = "restricted"
    list_status, listed = call_api(
        KnowledgeAssistantApi(service), "GET", "/v1/documents", headers=deny_all
    )

    assert upload_status == "201 Created"
    assert list_status == "200 OK"
    assert listed["items"] == []


def test_api_main_explicitly_starts_and_closes_service(tmp_path: Path, monkeypatch):
    events: list[str] = []

    class FakeService:
        def __init__(self, _database_path):
            self.embedding = type("Embedding", (), {"name": "fake"})()

        def start(self):
            events.append("start")

        def close(self):
            events.append("close")

    class FakeServer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append("server-close")

        @staticmethod
        def serve_forever():
            raise KeyboardInterrupt()

    monkeypatch.setattr(api_module, "KnowledgeAssistantService", FakeService)
    monkeypatch.setattr(api_module, "make_server", lambda *_args, **_kwargs: FakeServer())

    assert api_module.main(["--database", str(tmp_path / "unused.db")]) == 0
    assert events == ["start", "server-close", "close"]


def test_api_persistent_ingestion_create_get_list_cancel_contract(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    api = KnowledgeAssistantApi(service)
    headers = auth_headers("editor", "editor-a")
    headers["Idempotency-Key"] = "api-ingestion-001"
    created_status, created = call_api(
        api,
        "POST",
        "/v1/ingestion-jobs",
        body={
            "filename": "queued.md",
            "collection_id": "engineering",
            "content_base64": base64.b64encode(b"queued content").decode(),
        },
        headers=headers,
    )
    replay_status, replay = call_api(
        api,
        "POST",
        "/v1/ingestion-jobs",
        body={
            "filename": "queued.md",
            "collection_id": "engineering",
            "content_base64": base64.b64encode(b"queued content").decode(),
        },
        headers=headers,
    )
    get_status, fetched = call_api(
        api,
        "GET",
        f"/v1/ingestion-jobs/{created['id']}",
        headers=auth_headers("viewer", "viewer-a"),
    )
    list_status, listed = call_api(
        api,
        "GET",
        "/v1/ingestion-jobs",
        headers=auth_headers("viewer", "viewer-a"),
    )
    wrong_requester_status, _ = call_api(
        api,
        "POST",
        f"/v1/ingestion-jobs/{created['id']}/cancel",
        body={},
        headers=auth_headers("editor", "editor-b"),
    )
    cancel_status, cancelled = call_api(
        api,
        "POST",
        f"/v1/ingestion-jobs/{created['id']}/cancel",
        body={},
        headers=auth_headers("editor", "editor-a"),
    )

    assert created_status == "202 Accepted"
    assert created["status"] == "queued"
    assert replay_status == "202 Accepted"
    assert replay["id"] == created["id"]
    assert replay["idempotent_replay"] is True
    assert get_status == "200 OK" and fetched["id"] == created["id"]
    assert list_status == "200 OK" and listed["items"][0]["id"] == created["id"]
    assert wrong_requester_status == "403 Forbidden"
    assert cancel_status == "200 OK"
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_outcome"] == "cancelled"


def test_api_upload_query_metrics_and_trace_round_trip(tmp_path: Path):
    api = KnowledgeAssistantApi(KnowledgeAssistantService(tmp_path / "assistant.db"))
    headers = auth_headers()
    headers["Idempotency-Key"] = "api-upload-001"
    upload_status, document = call_api(
        api,
        "POST",
        "/v1/documents",
        body={
            "filename": "api.md",
            "collection_id": "engineering",
            "content_base64": base64.b64encode("# RAG\nRAG 回答必须提供来源引用。".encode()).decode(),
        },
        headers=headers,
    )
    query_status, result = call_api(
        api,
        "POST",
        "/v1/query",
        body={"query": "RAG 回答必须提供什么？", "collection_ids": ["engineering"]},
        headers=auth_headers("viewer"),
    )
    metrics_status, metrics = call_api(
        api, "GET", "/v1/metrics", headers=auth_headers("viewer")
    )
    trace_status, trace = call_api(
        api,
        "GET",
        f"/v1/traces/{result['trace_id']}",
        headers=auth_headers("viewer"),
    )

    assert upload_status == "201 Created"
    assert document["status"] == "indexed"
    assert query_status == "200 OK"
    assert result["refused"] is False
    assert result["citations"][0]["filename"] == "api.md"
    assert metrics_status == "200 OK"
    assert metrics["trace_count"] == 2
    assert trace_status == "200 OK"
    assert trace["operation"] == "rag.query"


def test_api_write_task_approval_flow(tmp_path: Path):
    api = KnowledgeAssistantApi(KnowledgeAssistantService(tmp_path / "assistant.db"))
    task_headers = auth_headers("editor", "agent-1")
    task_headers["Idempotency-Key"] = "api-task-001"
    created_status, task = call_api(
        api,
        "POST",
        "/v1/tasks",
        body={
            "tool_name": "ingest_text",
            "arguments": {"filename": "approved.txt", "content": "审批后才能写入。"},
        },
        headers=task_headers,
    )
    preview_response_headers: dict[str, str] = {}
    preview_status, preview = call_api(
        api,
        "GET",
        f"/v1/tasks/{task['id']}/approval-preview",
        headers=auth_headers("approver", "human-1"),
        response_headers=preview_response_headers,
    )
    missing_hash_status, missing_hash = call_api(
        api,
        "POST",
        f"/v1/tasks/{task['id']}/approve",
        headers=auth_headers("approver", "human-1"),
    )
    wrong_hash_status, _ = call_api(
        api,
        "POST",
        f"/v1/tasks/{task['id']}/approve",
        body={"preview_hash": "0" * 64},
        headers=auth_headers("approver", "human-1"),
    )
    approved_status, approved = call_api(
        api,
        "POST",
        f"/v1/tasks/{task['id']}/approve",
        body={"preview_hash": preview["preview_hash"]},
        headers=auth_headers("approver", "human-1"),
    )

    assert created_status == "202 Accepted"
    assert task["status"] == "awaiting_approval"
    assert "审批后才能写入。" not in str(task)
    assert preview_status == "200 OK"
    assert preview["action"]["parameters"]["content"] == "审批后才能写入。"
    assert preview_response_headers["Cache-Control"] == "no-store"
    assert missing_hash_status == "400 Bad Request"
    assert missing_hash["error"]["code"] == "invalid_request"
    assert wrong_hash_status == "409 Conflict"
    assert approved_status == "200 OK"
    assert approved["status"] == "succeeded"
    assert approved["receipt"]["approved_by"] == "human-1"
    assert approved["receipt"]["approved_preview_hash"] == preview["preview_hash"]


def test_api_optional_bearer_token_and_error_shape(tmp_path: Path):
    api = KnowledgeAssistantApi(
        KnowledgeAssistantService(tmp_path / "assistant.db"), api_token="server-secret"
    )
    missing_status, missing = call_api(api, "GET", "/v1/documents", headers=auth_headers())
    authorized = auth_headers()
    authorized["Authorization"] = "Bearer server-secret"
    ok_status, _ = call_api(api, "GET", "/v1/documents", headers=authorized)
    bad_status, bad = call_api(
        api,
        "POST",
        "/v1/query",
        body={"query": "x"},
        headers=authorized,
    )

    assert missing_status == "403 Forbidden"
    assert "bearer" in missing["error"]["message"]
    assert ok_status == "200 OK"
    assert bad_status == "400 Bad Request"
    assert bad["error"]["code"] == "invalid_request"
    assert "traceback" not in json.dumps(bad).casefold()


def test_api_rejects_non_list_collection_scope(tmp_path: Path):
    api = KnowledgeAssistantApi(KnowledgeAssistantService(tmp_path / "assistant.db"))

    status, result = call_api(
        api,
        "POST",
        "/v1/query",
        body={"query": "Spring IoC", "collection_ids": "engineering"},
        headers=auth_headers("viewer"),
    )

    assert status == "400 Bad Request"
    assert result["error"]["code"] == "invalid_request"
    assert "must be a list" in result["error"]["message"]


def test_document_checksum_lookup_is_read_only_and_scoped(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    headers = auth_headers("editor", "editor-a")
    headers["Idempotency-Key"] = "lookup-seed-001"
    content = b"lookup-bytes"
    digest = hashlib.sha256(content).hexdigest()
    upload_status, created = call_api(
        KnowledgeAssistantApi(service),
        "POST",
        "/v1/documents",
        body={
            "filename": "seed.md",
            "collection_id": "engineering",
            "content_base64": base64.b64encode(content).decode(),
        },
        headers=headers,
    )
    status, payload = call_api(
        KnowledgeAssistantApi(service),
        "POST",
        "/v1/documents/lookup",
        body={"collection_id": "engineering", "checksums": [digest]},
        headers=auth_headers("viewer", "viewer-a"),
    )
    other_tenant = auth_headers("editor", "editor-b")
    other_tenant["X-Tenant-ID"] = "tenant-b"
    miss_status, missed = call_api(
        KnowledgeAssistantApi(service),
        "POST",
        "/v1/documents/lookup",
        body={"collection_id": "engineering", "checksums": [digest]},
        headers=other_tenant,
    )

    assert upload_status == "201 Created"
    assert status == "200 OK"
    assert payload["items"][0]["id"] == created["id"]
    assert payload["items"][0]["filename"] == "seed.md"
    assert "content" not in payload["items"][0]
    assert miss_status == "200 OK"
    assert missed["items"] == []
