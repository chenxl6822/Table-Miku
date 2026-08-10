from __future__ import annotations

import os
import secrets
import stat
import threading
import uuid
from pathlib import Path
from typing import Iterable
from wsgiref.simple_server import WSGIRequestHandler, make_server

from .knowledge_assistant import KnowledgeAssistantService, Principal
from .knowledge_assistant.api import KnowledgeAssistantApi, ThreadingWSGIServer
from .knowledge_assistant.client import KnowledgeAssistantApiClient, KnowledgeAssistantApiError
from .knowledge_assistant.documents import MAX_DOCUMENT_BYTES


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:
        del format, args


class ManagedKnowledgeAssistantEndpoint:
    """Own a private loopback API, or connect to an explicitly configured one."""

    def __init__(self, service: KnowledgeAssistantService | None = None) -> None:
        self.mode = "embedded"
        self.database_path: Path | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        configured_url = os.getenv("KNOWLEDGE_ASSISTANT_DESKTOP_URL", "").strip()
        configured_token = os.getenv("KNOWLEDGE_ASSISTANT_API_TOKEN", "")
        if service is None and configured_url:
            if not configured_token:
                raise PermissionError(
                    "KNOWLEDGE_ASSISTANT_API_TOKEN is required for an external desktop connection"
                )
            health_client = KnowledgeAssistantApiClient(
                configured_url,
                configured_token,
                timeout_seconds=5.0,
            )
            health = health_client.health()
            if health.get("status") != "ok":
                raise ConnectionError("configured Knowledge Assistant service is not healthy")
            self.client = KnowledgeAssistantApiClient(configured_url, configured_token)
            self.mode = "external"
            return

        if service is None:
            default_client = KnowledgeAssistantApiClient(
                "http://127.0.0.1:8080",
                "",
                timeout_seconds=1.0,
            )
            try:
                health = default_client.health()
            except (ConnectionError, KnowledgeAssistantApiError):
                health = {}
            if health.get("status") == "ok" and health.get("schema_version") is not None:
                raise PermissionError(
                    "检测到 127.0.0.1:8080 Knowledge Assistant；为避免连接错误实例或同时打开同一数据库，"
                    "请显式设置 KNOWLEDGE_ASSISTANT_DESKTOP_URL 与同一 API Token，或先关闭外部服务"
                )

        embedded_service = service or KnowledgeAssistantService()
        token = secrets.token_urlsafe(32)
        application = KnowledgeAssistantApi(embedded_service, api_token=token)
        server = make_server(
            "127.0.0.1",
            0,
            application,
            server_class=ThreadingWSGIServer,
            handler_class=_QuietRequestHandler,
        )
        self._server = server
        self.database_path = embedded_service.database.path
        self.client = KnowledgeAssistantApiClient(
            f"http://127.0.0.1:{server.server_port}",
            token,
        )
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="knowledge-assistant-private-api",
            daemon=True,
        )
        self._thread.start()
        try:
            health = self.client.health()
        except Exception:
            self.close()
            raise
        if health.get("status") != "ok":
            self.close()
            raise ConnectionError("private Knowledge Assistant service failed to start")

    @property
    def base_url(self) -> str:
        return self.client.base_url

    def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


class KnowledgeAssistantDesktopController:
    """Small, testable adapter used by the local visual administration console."""

    def __init__(self, service: KnowledgeAssistantService | None = None) -> None:
        self.endpoint = ManagedKnowledgeAssistantEndpoint(service)
        self.client = self.endpoint.client

    @property
    def database_path(self) -> Path | None:
        return self.endpoint.database_path

    @property
    def connection_label(self) -> str:
        return f"{self.endpoint.mode} · {self.endpoint.base_url}"

    def close(self) -> None:
        self.endpoint.close()

    @staticmethod
    def principal(
        tenant_id: str,
        user_id: str,
        roles: str | Iterable[str],
        collection_ids: str | Iterable[str] = "",
    ) -> Principal:
        role_values = roles.split(",") if isinstance(roles, str) else roles
        collection_values = (
            collection_ids.split(",") if isinstance(collection_ids, str) else collection_ids
        )
        normalized_roles = frozenset(
            str(item).strip().lower() for item in role_values if str(item).strip()
        )
        normalized_collections = frozenset(
            str(item).strip() for item in collection_values if str(item).strip()
        )
        return Principal(
            tenant_id=tenant_id.strip(),
            user_id=user_id.strip(),
            roles=normalized_roles,
            collection_ids=normalized_collections or None,
        )

    @staticmethod
    def collection_list(value: str) -> list[str] | None:
        collections = [item.strip() for item in value.split(",") if item.strip()]
        return collections or None

    @staticmethod
    def new_idempotency_key(prefix: str) -> str:
        safe_prefix = "".join(
            character
            for character in prefix.lower()
            if character.isalnum() or character == "-"
        )
        return f"{safe_prefix or 'desktop'}-{uuid.uuid4().hex}"

    def list_documents(self, principal: Principal, *, limit: int = 200) -> list[dict]:
        del limit
        return self.client.list_documents(principal)

    def upload_file(
        self,
        principal: Principal,
        *,
        path: Path,
        collection_id: str,
        idempotency_key: str,
    ) -> dict:
        filename, content = self.prepare_upload(path)
        return self.upload_content(
            principal,
            filename=filename,
            content=content,
            collection_id=collection_id,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def prepare_upload(path: Path) -> tuple[str, bytes]:
        source = Path(path)
        try:
            with source.open("rb") as stream:
                source_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(source_stat.st_mode):
                    raise ValueError("请选择一个可读取的文档文件")
                if source_stat.st_size > MAX_DOCUMENT_BYTES:
                    raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
                content = stream.read(MAX_DOCUMENT_BYTES + 1)
        except OSError as exc:
            raise ValueError("请选择一个可读取的文档文件") from exc
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
        return source.name, content

    def upload_content(
        self,
        principal: Principal,
        *,
        filename: str,
        content: bytes,
        collection_id: str,
        idempotency_key: str,
    ) -> dict:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
        return self.client.upload_document(
            principal,
            filename=filename,
            content=content,
            collection_id=collection_id,
            idempotency_key=idempotency_key,
        )

    def query(
        self,
        principal: Principal,
        *,
        query: str,
        collection_ids: str = "",
        top_k: int = 5,
    ) -> dict:
        return self.client.query(
            principal,
            query=query,
            collection_ids=self.collection_list(collection_ids),
            top_k=top_k,
        )

    def list_tasks(self, principal: Principal, *, limit: int = 200) -> list[dict]:
        del limit
        return self.client.list_tasks(principal)

    def create_ingest_task(
        self,
        principal: Principal,
        *,
        filename: str,
        collection_id: str,
        content: str,
        idempotency_key: str,
    ) -> dict:
        return self.client.create_task(
            principal,
            tool_name="ingest_text",
            arguments={
                "filename": filename,
                "collection_id": collection_id,
                "content": content,
            },
            idempotency_key=idempotency_key,
        )

    def create_archive_task(
        self,
        principal: Principal,
        *,
        document_id: str,
        idempotency_key: str,
    ) -> dict:
        return self.client.create_task(
            principal,
            tool_name="archive_document",
            arguments={"document_id": document_id},
            idempotency_key=idempotency_key,
        )

    def approval_preview(self, principal: Principal, task_id: str) -> dict:
        return self.client.approval_preview(principal, task_id)

    def approve_task(self, principal: Principal, task_id: str, preview_hash: str) -> dict:
        return self.client.approve_task(principal, task_id, preview_hash)

    def reject_task(self, principal: Principal, task_id: str, reason: str = "") -> dict:
        return self.client.reject_task(principal, task_id, reason)

    def metrics(self, principal: Principal, *, limit: int = 1000) -> dict:
        del limit
        return self.client.metrics(principal)

    def get_trace(self, principal: Principal, trace_id: str) -> dict:
        return self.client.get_trace(principal, trace_id.strip())
