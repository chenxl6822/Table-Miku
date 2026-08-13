from __future__ import annotations

import base64
import threading
import stat
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from urllib.error import HTTPError

import pytest

import table_miku.knowledge_assistant.client as client_module
import table_miku.knowledge_assistant_desktop as desktop_module
from table_miku.knowledge_assistant import KnowledgeAssistantService
from table_miku.knowledge_assistant.client import (
    KnowledgeAssistantApiClient,
    KnowledgeAssistantApiError,
)
from table_miku.knowledge_assistant.documents import MAX_DOCUMENT_BYTES
from table_miku.knowledge_assistant_desktop import (
    KnowledgeAssistantDesktopController,
    ManagedKnowledgeAssistantEndpoint,
)
from table_miku.knowledge_assistant.auth import Principal


def test_client_ingestion_job_contract_uses_bounded_json_routes(monkeypatch):
    client = KnowledgeAssistantApiClient("http://127.0.0.1:8080", "token")
    principal = KnowledgeAssistantDesktopController.principal(
        "tenant-a", "editor-a", "editor", "engineering"
    )
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and path == "/v1/ingestion-jobs":
            return {"items": [{"id": "ingest-1", "status": "queued"}]}
        return {"id": "ingest-1", "status": "queued"}

    monkeypatch.setattr(client, "_request", fake_request)

    created = client.create_ingestion_job(
        principal,
        filename="safe.md",
        content=b"exact bytes",
        collection_id="engineering",
        idempotency_key="client-ingestion-001",
    )
    listed = client.list_ingestion_jobs(principal)
    fetched = client.get_ingestion_job(principal, "ingest-1")
    cancelled = client.cancel_ingestion_job(principal, "ingest-1")

    assert created["id"] == listed[0]["id"] == fetched["id"] == cancelled["id"]
    assert calls[0][0:2] == ("POST", "/v1/ingestion-jobs")
    assert base64.b64decode(calls[0][2]["body"]["content_base64"], validate=True) == b"exact bytes"
    assert calls[0][2]["idempotency_key"] == "client-ingestion-001"
    assert calls[2][1] == "/v1/ingestion-jobs/ingest-1"
    assert calls[3][1] == "/v1/ingestion-jobs/ingest-1/cancel"


@contextmanager
def _response_server(
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    status: int = 200,
    request_headers: dict[str, str] | None = None,
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def _respond(self):
            if request_headers is not None:
                request_headers.update(dict(self.headers.items()))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._respond()

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length:
                self.rfile.read(content_length)
            self._respond()

        def log_message(self, format: str, *args) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_client_round_trips_empty_collection_scope_as_explicit_deny_all():
    captured: dict[str, str] = {}
    with _response_server(b'{"items":[]}', request_headers=captured) as base_url:
        client = KnowledgeAssistantApiClient(base_url, "token")
        principal = Principal(
            tenant_id="tenant-a",
            user_id="viewer-a",
            roles=frozenset({"viewer"}),
            collection_ids=frozenset(),
        )

        assert client.list_documents(principal) == []

    assert captured["X-Collection-Scope"] == "restricted"


def test_client_rejects_unsafe_or_ambiguous_base_urls():
    with pytest.raises(ValueError, match="HTTPS"):
        KnowledgeAssistantApiClient("http://knowledge.example.test", "token")
    with pytest.raises(ValueError, match="credentials"):
        KnowledgeAssistantApiClient("https://user:password@knowledge.example.test", "token")
    with pytest.raises(ValueError, match="path"):
        KnowledgeAssistantApiClient("https://knowledge.example.test/api", "token")
    with pytest.raises(ValueError, match="valid port"):
        KnowledgeAssistantApiClient("https://knowledge.example.test:not-a-port", "token")
    with pytest.raises(ValueError, match="valid port"):
        KnowledgeAssistantApiClient("https://knowledge.example.test:0", "token")
    with pytest.raises(ValueError, match="valid port"):
        KnowledgeAssistantApiClient("https://knowledge.example.test:", "token")


def test_controller_normalizes_identity_scope_and_idempotency_keys(tmp_path: Path):
    controller = KnowledgeAssistantDesktopController(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )
    try:
        principal = controller.principal(
            " tenant-a ",
            " alice ",
            " Editor ",
            " engineering, docs, engineering ",
        )

        assert principal.tenant_id == "tenant-a"
        assert principal.user_id == "alice"
        assert principal.roles == frozenset({"editor"})
        assert principal.collection_ids == frozenset({"engineering", "docs"})
        assert controller.collection_list(" engineering, docs ") == ["engineering", "docs"]
        assert controller.collection_list("") is None
        first = controller.new_idempotency_key("Desktop Upload")
        second = controller.new_idempotency_key("Desktop Upload")
        assert first.startswith("desktopupload-")
        assert len(first) > 8
        assert first != second
    finally:
        controller.close()


def test_http_controller_upload_query_refusal_and_approval_flow(tmp_path: Path):
    controller = KnowledgeAssistantDesktopController(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )
    editor = controller.principal("tenant-a", "alice-editor", "editor", "engineering")
    viewer = controller.principal("tenant-a", "victor-viewer", "viewer", "engineering")
    approver = controller.principal("tenant-a", "amy-approver", "approver", "engineering")
    source = tmp_path / "spring.md"
    source.write_text(
        "# Spring IoC\nSpring IoC 管理对象创建、依赖关系和生命周期。构造器注入表达必需依赖。",
        encoding="utf-8",
    )
    try:
        health = controller.client.health()
        assert health["status"] == "ok"

        document = controller.upload_file(
            editor,
            path=source,
            collection_id="engineering",
            idempotency_key="desktop-upload-001",
        )
        assert document["status"] == "indexed"

        answer = controller.query(
            viewer,
            query="Spring IoC 管理哪些职责？",
            collection_ids="engineering",
            top_k=5,
        )
        assert answer["refused"] is False
        assert answer["citations"][0]["filename"] == "spring.md"

        refusal = controller.query(
            viewer,
            query="公司火星基地的门禁密码是什么？",
            collection_ids="engineering",
            top_k=5,
        )
        assert refusal["refused"] is True
        assert refusal["citations"] == []

        content = "# Agent note\n<script>must remain plain text</script>"
        task = controller.create_ingest_task(
            editor,
            filename="agent-note.md",
            collection_id="engineering",
            content=content,
            idempotency_key="desktop-task-001",
        )
        assert task["status"] == "awaiting_approval"
        assert content not in str(controller.list_tasks(editor))

        preview = controller.approval_preview(approver, task["id"])
        assert preview["action"]["parameters"]["content"] == content
        completed = controller.approve_task(approver, task["id"], preview["preview_hash"])
        assert completed["status"] == "succeeded"
        assert completed["receipt"]["approved_preview_hash"] == preview["preview_hash"]
        assert content not in str(completed["receipt"])
        assert len(controller.list_documents(viewer)) == 2

        tenant_viewer = controller.principal("tenant-a", "tenant-auditor", "viewer")
        metrics = controller.metrics(tenant_viewer)
        assert metrics["trace_count"] >= 4
        assert metrics["latency_ms"]["p95"] >= 0

        with pytest.raises(KnowledgeAssistantApiError, match="collection-scoped trace metrics"):
            controller.metrics(viewer)
    finally:
        controller.close()


def test_http_client_maps_permission_errors_without_exposing_token(tmp_path: Path):
    controller = KnowledgeAssistantDesktopController(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )
    try:
        wrong_token = "not-the-managed-token"
        client = KnowledgeAssistantApiClient(controller.endpoint.base_url, wrong_token)
        principal = controller.principal("tenant-a", "viewer", "viewer")

        with pytest.raises(KnowledgeAssistantApiError) as raised:
            client.list_documents(principal)

        assert raised.value.status_code == 403
        assert raised.value.code == "permission_denied"
        assert wrong_token not in str(raised.value)
    finally:
        controller.close()


def test_controller_rejects_missing_upload_file(tmp_path: Path):
    controller = KnowledgeAssistantDesktopController(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )
    editor = controller.principal("tenant-a", "editor", "editor")
    try:
        with pytest.raises(ValueError, match="可读取"):
            controller.upload_file(
                editor,
                path=tmp_path / "missing.md",
                collection_id="default",
                idempotency_key="desktop-missing-001",
            )
    finally:
        controller.close()


def test_client_does_not_forward_identity_or_token_across_redirects():
    received: dict[str, str] = {}

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.update({key.lower(): value for key, value in self.headers.items()})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"items":[]}')

        def log_message(self, format: str, *args) -> None:
            del format, args

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()
    sink_url = f"http://127.0.0.1:{sink.server_port}/capture"

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", sink_url)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            del format, args

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        token = "redirect-sensitive-token"
        client = KnowledgeAssistantApiClient(
            f"http://127.0.0.1:{redirect.server_port}",
            token,
        )
        principal = KnowledgeAssistantDesktopController.principal(
            "tenant-a",
            "viewer-a",
            "viewer",
            "engineering",
        )

        with pytest.raises(KnowledgeAssistantApiError) as raised:
            client.list_documents(principal)

        assert raised.value.status_code == 302
        assert received == {}
        assert token not in str(raised.value)
    finally:
        redirect.shutdown()
        redirect.server_close()
        sink.shutdown()
        sink.server_close()
        redirect_thread.join(timeout=2)
        sink_thread.join(timeout=2)


@pytest.mark.parametrize(
    ("method_name", "body"),
    (
        ("list_documents", b"{}"),
        ("list_tasks", b'{"items":[{"id":"valid"},null]}'),
        ("list_documents", b'{"items":[{}]}'),
    ),
)
def test_client_rejects_malformed_collection_items(method_name: str, body: bytes):
    principal = KnowledgeAssistantDesktopController.principal(
        "tenant-a",
        "viewer-a",
        "viewer",
        "engineering",
    )
    with _response_server(body) as base_url:
        client = KnowledgeAssistantApiClient(base_url, "synthetic-token")

        with pytest.raises(KnowledgeAssistantApiError) as raised:
            getattr(client, method_name)(principal)

    assert raised.value.status_code == 200
    assert raised.value.code == "invalid_response"


@pytest.mark.parametrize("operation", ("upload", "create_task"))
def test_client_rejects_malformed_success_for_write_operations(operation: str):
    principal = KnowledgeAssistantDesktopController.principal(
        "tenant-a",
        "editor-a",
        "editor",
        "engineering",
    )
    with _response_server(b"{}") as base_url:
        client = KnowledgeAssistantApiClient(base_url, "synthetic-token")

        with pytest.raises(KnowledgeAssistantApiError) as raised:
            if operation == "upload":
                client.upload_document(
                    principal,
                    filename="synthetic.md",
                    content=b"synthetic content",
                    collection_id="engineering",
                    idempotency_key="malformed-upload-001",
                )
            else:
                client.create_task(
                    principal,
                    tool_name="ingest_text",
                    arguments={
                        "filename": "synthetic.md",
                        "collection_id": "engineering",
                        "content": "synthetic content",
                    },
                    idempotency_key="malformed-task-001",
                )

    assert raised.value.status_code == 200
    assert raised.value.code == "invalid_response"


@pytest.mark.parametrize("content_length", ("not-a-number", "-1"))
def test_client_rejects_malformed_content_length(content_length: str):
    with _response_server(
        b'{"status":"ok"}',
        headers={"Content-Length": content_length},
    ) as base_url:
        client = KnowledgeAssistantApiClient(base_url, "synthetic-token")

        with pytest.raises(KnowledgeAssistantApiError) as raised:
            client.health()

    assert raised.value.status_code == 200
    assert raised.value.code == "invalid_response"


@pytest.mark.parametrize("advertise_length", (False, True))
def test_client_rejects_responses_over_safety_limit(
    monkeypatch: pytest.MonkeyPatch,
    advertise_length: bool,
):
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 64)
    body = b'{"padding":"' + (b"x" * 80) + b'"}'
    headers = {"Content-Length": str(len(body))} if advertise_length else None
    with _response_server(body, headers=headers) as base_url:
        client = KnowledgeAssistantApiClient(base_url, "synthetic-token")

        with pytest.raises(KnowledgeAssistantApiError) as raised:
            client.health()

    assert raised.value.status_code == 200
    assert raised.value.code == "response_too_large"


def test_client_never_uses_environment_proxy_for_loopback_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    proxy_requests: list[dict[str, str]] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            proxy_requests.append({key.lower(): value for key, value in self.headers.items()})
            self.send_response(502)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            del format, args

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        with _response_server(b'{"items":[]}') as base_url:
            client = KnowledgeAssistantApiClient(base_url, "synthetic-loopback-token")
            principal = KnowledgeAssistantDesktopController.principal(
                "tenant-a",
                "viewer-a",
                "viewer",
                "engineering",
            )

            assert client.list_documents(principal) == []

        assert proxy_requests == []
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2)


def test_client_closes_http_error_stream_before_raising(monkeypatch: pytest.MonkeyPatch):
    error_stream = BytesIO(b'{"error":{"code":"synthetic","message":"failure"}}')
    http_error = HTTPError(
        "http://127.0.0.1:9/health",
        500,
        "synthetic failure",
        {},
        error_stream,
    )

    class FailingOpener:
        @staticmethod
        def open(*_args, **_kwargs):
            raise http_error

    client = KnowledgeAssistantApiClient("http://127.0.0.1:9", "synthetic-token")
    monkeypatch.setattr(client, "_opener", FailingOpener())

    with pytest.raises(KnowledgeAssistantApiError) as raised:
        client.health()

    assert raised.value.code == "synthetic"
    assert error_stream.closed


def test_client_maps_truncated_chunked_success_to_unknown_connection_result():
    class TruncatedChunkHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length:
                self.rfile.read(content_length)
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"20\r\n{\"id\":\"truncated\"}")
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, format: str, *args) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), TruncatedChunkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = KnowledgeAssistantApiClient(
            f"http://127.0.0.1:{server.server_port}",
            "synthetic-token",
        )
        principal = KnowledgeAssistantDesktopController.principal(
            "tenant-a",
            "editor-a",
            "editor",
            "engineering",
        )

        with pytest.raises(ConnectionError, match="unavailable"):
            client.upload_document(
                principal,
                filename="truncated.md",
                content=b"synthetic content",
                collection_id="engineering",
                idempotency_key="truncated-upload-001",
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_client_wraps_truncated_chunked_server_error_with_http_status():
    class TruncatedErrorHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length:
                self.rfile.read(content_length)
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"40\r\n{\"error\":{\"code\":\"truncated\"")
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, format: str, *args) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), TruncatedErrorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = KnowledgeAssistantApiClient(
            f"http://127.0.0.1:{server.server_port}",
            "synthetic-token",
        )
        principal = KnowledgeAssistantDesktopController.principal(
            "tenant-a",
            "editor-a",
            "editor",
            "engineering",
        )

        with pytest.raises(KnowledgeAssistantApiError) as raised:
            client.create_task(
                principal,
                tool_name="ingest_text",
                arguments={
                    "filename": "truncated.md",
                    "collection_id": "engineering",
                    "content": "synthetic content",
                },
                idempotency_key="truncated-task-001",
            )

        assert raised.value.status_code == 500
        assert raised.value.code == "invalid_response"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_controller_rejects_oversized_upload_before_reading_file(
    tmp_path: Path,
):
    controller = KnowledgeAssistantDesktopController(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )
    source = tmp_path / "oversized.pdf"
    with source.open("wb") as stream:
        stream.truncate(MAX_DOCUMENT_BYTES + 1)

    editor = controller.principal("tenant-a", "editor-a", "editor", "engineering")
    try:
        with pytest.raises(ValueError, match="byte limit"):
            controller.upload_file(
                editor,
                path=source,
                collection_id="engineering",
                idempotency_key="oversized-upload-001",
            )
    finally:
        controller.close()


def test_prepare_upload_bounds_the_actual_stream_read(monkeypatch: pytest.MonkeyPatch):
    read_sizes: list[int] = []

    class SyntheticStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        @staticmethod
        def fileno() -> int:
            return 123

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return b"x" * size

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: SyntheticStream())
    monkeypatch.setattr(
        desktop_module,
        "os",
        SimpleNamespace(
            fstat=lambda _descriptor: SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_size=0,
            )
        ),
    )

    with pytest.raises(ValueError, match="byte limit"):
        KnowledgeAssistantDesktopController.prepare_upload(Path("synthetic-growing-file"))

    assert read_sizes == [MAX_DOCUMENT_BYTES + 1]


def test_upload_content_rechecks_the_final_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    controller = KnowledgeAssistantDesktopController(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )
    monkeypatch.setattr(desktop_module, "MAX_DOCUMENT_BYTES", 64)
    editor = controller.principal("tenant-a", "editor-a", "editor", "engineering")
    try:
        with pytest.raises(ValueError, match="byte limit"):
            controller.upload_content(
                editor,
                filename="oversized.md",
                content=b"x" * 65,
                collection_id="engineering",
                idempotency_key="oversized-sink-001",
            )
    finally:
        controller.close()


def test_explicit_external_endpoint_uses_short_health_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    observed_health_timeouts: list[float] = []

    class HealthyClient:
        def __init__(
            self,
            base_url: str,
            api_token: str,
            *,
            timeout_seconds: float = 60.0,
        ) -> None:
            assert api_token == "synthetic-external-token"
            self.base_url = base_url
            self.timeout_seconds = timeout_seconds

        def health(self) -> dict[str, str]:
            observed_health_timeouts.append(self.timeout_seconds)
            return {"status": "ok", "service_instance_id": "external-instance-1"}

    monkeypatch.setenv("KNOWLEDGE_ASSISTANT_DESKTOP_URL", "https://knowledge.example.test")
    monkeypatch.setenv("KNOWLEDGE_ASSISTANT_API_TOKEN", "synthetic-external-token")
    monkeypatch.setattr(desktop_module, "KnowledgeAssistantApiClient", HealthyClient)

    endpoint = ManagedKnowledgeAssistantEndpoint()
    try:
        assert endpoint.mode == "external"
        assert endpoint.service_instance_id == "external-instance-1"
        assert endpoint.recovery_binding_id.startswith("external-")
        assert observed_health_timeouts
        assert max(observed_health_timeouts) <= 5.0
    finally:
        endpoint.close()


def test_explicit_external_endpoint_without_stable_instance_id_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    class MissingInstanceClient:
        def __init__(
            self,
            base_url: str,
            api_token: str,
            *,
            timeout_seconds: float = 60.0,
        ) -> None:
            del api_token, timeout_seconds
            self.base_url = base_url

        @staticmethod
        def health() -> dict[str, str]:
            return {"status": "ok"}

    monkeypatch.setenv("KNOWLEDGE_ASSISTANT_DESKTOP_URL", "https://knowledge.example.test")
    monkeypatch.setenv("KNOWLEDGE_ASSISTANT_API_TOKEN", "synthetic-external-token")
    monkeypatch.setattr(desktop_module, "KnowledgeAssistantApiClient", MissingInstanceClient)

    with pytest.raises(ValueError, match="service_instance_id"):
        ManagedKnowledgeAssistantEndpoint()


def test_default_endpoint_refuses_implicit_healthy_8080_without_sending_token(
    monkeypatch: pytest.MonkeyPatch,
):
    probes: list[tuple[str, str, float]] = []

    class ProbeClient:
        def __init__(
            self,
            base_url: str,
            api_token: str,
            *,
            timeout_seconds: float = 60.0,
        ) -> None:
            probes.append((base_url, api_token, timeout_seconds))
            self.base_url = base_url

        @staticmethod
        def health() -> dict[str, object]:
            return {"status": "ok", "schema_version": 1}

    monkeypatch.delenv("KNOWLEDGE_ASSISTANT_DESKTOP_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_ASSISTANT_API_TOKEN", raising=False)
    monkeypatch.setattr(desktop_module, "KnowledgeAssistantApiClient", ProbeClient)

    with pytest.raises(PermissionError, match="显式设置"):
        ManagedKnowledgeAssistantEndpoint()

    assert probes == [("http://127.0.0.1:8080", "", 1.0)]


def test_embedded_endpoint_uses_private_random_loopback_port(tmp_path: Path):
    endpoint = ManagedKnowledgeAssistantEndpoint(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )
    try:
        assert endpoint.mode == "embedded"
        assert endpoint.base_url.startswith("http://127.0.0.1:")
        assert endpoint.base_url != "http://127.0.0.1:8080"
        assert endpoint.client.health()["status"] == "ok"
        assert endpoint.client._api_token
    finally:
        endpoint.close()


def test_private_endpoint_cleans_up_when_health_check_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeServer:
        server_port = 54321

        def __init__(self) -> None:
            self.stop = threading.Event()
            self.thread: threading.Thread | None = None
            self.shutdown_called = False
            self.server_close_called = False

        def serve_forever(self, *, poll_interval: float) -> None:
            del poll_interval
            self.thread = threading.current_thread()
            self.stop.wait(timeout=5)

        def shutdown(self) -> None:
            self.shutdown_called = True
            self.stop.set()

        def server_close(self) -> None:
            self.server_close_called = True

    class FailingHealthClient:
        def __init__(
            self,
            base_url: str,
            api_token: str,
            *,
            timeout_seconds: float = 60.0,
        ) -> None:
            del api_token, timeout_seconds
            self.base_url = base_url

        def health(self) -> dict[str, str]:
            raise ConnectionError("synthetic startup failure")

    server = FakeServer()
    monkeypatch.setattr(desktop_module, "make_server", lambda *args, **kwargs: server)
    monkeypatch.setattr(desktop_module, "KnowledgeAssistantApiClient", FailingHealthClient)
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    try:
        with pytest.raises(ConnectionError, match="synthetic startup failure"):
            ManagedKnowledgeAssistantEndpoint(service)

        assert server.shutdown_called
        assert server.server_close_called
        assert server.thread is not None
        assert not server.thread.is_alive()
    finally:
        server.stop.set()
        if server.thread is not None:
            server.thread.join(timeout=2)
