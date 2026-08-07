from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

from table_miku.knowledge_assistant.api import KnowledgeAssistantApi
from table_miku.knowledge_assistant.service import KnowledgeAssistantService


def call_api(
    api: KnowledgeAssistantApi,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
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
    return str(captured["status"]), json.loads(response)


def auth_headers(role: str = "editor", user: str = "user-1") -> dict[str, str]:
    return {"X-Tenant-ID": "tenant-a", "X-User-ID": user, "X-Roles": role}


def test_health_is_public_but_data_routes_require_identity(tmp_path: Path):
    api = KnowledgeAssistantApi(KnowledgeAssistantService(tmp_path / "assistant.db"))

    health_status, health = call_api(api, "GET", "/health")
    denied_status, denied = call_api(api, "GET", "/v1/documents")

    assert health_status == "200 OK"
    assert health["status"] == "ok"
    assert health["embedding_model"] == "local-hash-v1-384"
    assert denied_status == "403 Forbidden"
    assert denied["error"]["code"] == "permission_denied"


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
    approved_status, approved = call_api(
        api,
        "POST",
        f"/v1/tasks/{task['id']}/approve",
        headers=auth_headers("approver", "human-1"),
    )

    assert created_status == "202 Accepted"
    assert task["status"] == "awaiting_approval"
    assert approved_status == "200 OK"
    assert approved["status"] == "succeeded"
    assert approved["receipt"]["approved_by"] == "human-1"


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
