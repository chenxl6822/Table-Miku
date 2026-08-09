from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Callable, Iterable
from wsgiref.simple_server import WSGIServer, make_server

from .auth import ConflictError, PermissionDenied, Principal, ResourceNotFound
from .database import SCHEMA_VERSION
from .service import KnowledgeAssistantService


LOGGER = logging.getLogger("table_miku.knowledge_assistant")
MAX_REQUEST_BYTES = 16 * 1024 * 1024
_TASK_PATH = re.compile(r"^/v1/tasks/([^/]+)$")
_TASK_APPROVAL_PREVIEW_PATH = re.compile(r"^/v1/tasks/([^/]+)/approval-preview$")
_TASK_DECISION_PATH = re.compile(r"^/v1/tasks/([^/]+)/(approve|reject)$")
_DOCUMENT_PATH = re.compile(r"^/v1/documents/([^/]+)$")
_TRACE_PATH = re.compile(r"^/v1/traces/([^/]+)$")


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class KnowledgeAssistantApi:
    def __init__(
        self,
        service: KnowledgeAssistantService | None = None,
        *,
        api_token: str | None = None,
    ) -> None:
        self.service = service or KnowledgeAssistantService()
        self.api_token = api_token if api_token is not None else os.getenv("KNOWLEDGE_ASSISTANT_API_TOKEN", "")

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> Iterable[bytes]:
        request_id = environ.get("HTTP_X_REQUEST_ID", "")[:120]
        try:
            status, payload = self.dispatch(environ)
        except PermissionDenied as exc:
            status, payload = "403 Forbidden", self._error("permission_denied", str(exc), request_id)
        except ResourceNotFound as exc:
            status, payload = "404 Not Found", self._error("not_found", str(exc), request_id)
        except ConflictError as exc:
            status, payload = "409 Conflict", self._error("conflict", str(exc), request_id)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            status, payload = "400 Bad Request", self._error("invalid_request", str(exc), request_id)
        except Exception:
            LOGGER.exception("unhandled Knowledge Assistant API error request_id=%s", request_id)
            status, payload = "500 Internal Server Error", self._error(
                "internal_error", "request failed; inspect the server trace", request_id
            )
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        start_response(
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(encoded))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [encoded]

    def dispatch(self, environ: dict[str, Any]) -> tuple[str, dict[str, Any] | list[Any]]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        if method == "GET" and path == "/health":
            return "200 OK", {
                "status": "ok",
                "schema_version": SCHEMA_VERSION,
                "embedding_model": self.service.embedding.name,
            }
        self._check_api_token(environ)
        principal = self._principal(environ)
        if method == "POST" and path == "/v1/documents":
            body = self._json_body(environ)
            idempotency_key = str(environ.get("HTTP_IDEMPOTENCY_KEY", ""))
            result = self.service.documents.upload(
                principal,
                filename=str(body.get("filename", "")),
                content=self.service.documents.decode_base64(str(body.get("content_base64", ""))),
                collection_id=str(body.get("collection_id", "default")),
                idempotency_key=idempotency_key,
            )
            return "201 Created", result
        if method == "GET" and path == "/v1/documents":
            return "200 OK", {"items": self.service.documents.list_documents(principal)}
        document_match = _DOCUMENT_PATH.match(path)
        if method == "GET" and document_match:
            return "200 OK", self.service.documents.get_document(principal, document_match.group(1))
        if method == "POST" and path == "/v1/query":
            body = self._json_body(environ)
            result = self.service.rag.query(
                principal,
                str(body.get("query", "")),
                collection_ids=body.get("collection_ids"),
                top_k=int(body.get("top_k", 5)),
                min_score=float(body["min_score"]) if "min_score" in body else None,
            )
            return "200 OK", result
        if method == "POST" and path == "/v1/tasks":
            body = self._json_body(environ)
            result = self.service.tasks.create(
                principal,
                tool_name=str(body.get("tool_name", "")),
                arguments=body.get("arguments") if isinstance(body.get("arguments"), dict) else {},
                idempotency_key=str(environ.get("HTTP_IDEMPOTENCY_KEY", "")),
            )
            return "202 Accepted", result
        if method == "GET" and path == "/v1/tasks":
            return "200 OK", {"items": self.service.tasks.list(principal)}
        preview_match = _TASK_APPROVAL_PREVIEW_PATH.match(path)
        if method == "GET" and preview_match:
            return "200 OK", self.service.tasks.preview(principal, preview_match.group(1))
        decision_match = _TASK_DECISION_PATH.match(path)
        if method == "POST" and decision_match:
            task_id, action = decision_match.groups()
            if action == "approve":
                body = self._json_body(environ)
                result = self.service.tasks.approve(
                    principal,
                    task_id,
                    str(body.get("preview_hash", "")),
                )
            else:
                body = self._json_body(environ, allow_empty=True)
                result = self.service.tasks.reject(principal, task_id, str(body.get("reason", "")))
            return "200 OK", result
        task_match = _TASK_PATH.match(path)
        if method == "GET" and task_match:
            return "200 OK", self.service.tasks.get(principal, task_match.group(1))
        if method == "GET" and path == "/v1/metrics":
            return "200 OK", self.service.traces.metrics(principal)
        trace_match = _TRACE_PATH.match(path)
        if method == "GET" and trace_match:
            return "200 OK", self.service.traces.get_trace(principal, trace_match.group(1))
        raise ResourceNotFound("route not found")

    def _check_api_token(self, environ: dict[str, Any]) -> None:
        if not self.api_token:
            return
        authorization = str(environ.get("HTTP_AUTHORIZATION", ""))
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, self.api_token):
            raise PermissionDenied("valid bearer authentication is required")

    @staticmethod
    def _principal(environ: dict[str, Any]) -> Principal:
        headers = {
            key[5:].replace("_", "-").lower(): str(value)
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
        if not headers.get("x-tenant-id") or not headers.get("x-user-id"):
            raise PermissionDenied("X-Tenant-ID and X-User-ID headers are required")
        return Principal.from_headers(headers)

    @staticmethod
    def _json_body(environ: dict[str, Any], *, allow_empty: bool = False) -> dict[str, Any]:
        raw_length = str(environ.get("CONTENT_LENGTH", "0") or "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError(f"request body exceeds the {MAX_REQUEST_BYTES} byte limit")
        raw = environ["wsgi.input"].read(length) if length else b""
        if not raw and allow_empty:
            return {}
        if not raw:
            raise ValueError("JSON request body is required")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON request body must be an object")
        return payload

    @staticmethod
    def _error(code: str, message: str, request_id: str) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message[:500]}
        if request_id:
            error["request_id"] = request_id
        return {"error": error}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Table-Miku Knowledge Assistant 2.0 API")
    parser.add_argument("--host", default=os.getenv("KNOWLEDGE_ASSISTANT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("KNOWLEDGE_ASSISTANT_PORT", "8080")))
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ["KNOWLEDGE_ASSISTANT_DB"])
        if os.getenv("KNOWLEDGE_ASSISTANT_DB")
        else None,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    service = KnowledgeAssistantService(args.database)
    application = KnowledgeAssistantApi(service)
    with make_server(
        args.host,
        args.port,
        application,
        server_class=ThreadingWSGIServer,
    ) as server:
        LOGGER.info("Knowledge Assistant API listening on http://%s:%s", args.host, args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOGGER.info("Knowledge Assistant API stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
