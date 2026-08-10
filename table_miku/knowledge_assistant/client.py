from __future__ import annotations

import base64
import json
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .auth import Principal


MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class KnowledgeAssistantApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        del request, fp, code, msg, headers, new_url
        return None


class KnowledgeAssistantApiClient:
    """Minimal JSON client for the Knowledge Assistant HTTP contract."""

    def __init__(self, base_url: str, api_token: str, *, timeout_seconds: float = 60.0) -> None:
        self.base_url = self._validated_base_url(base_url)
        self._api_token = api_token
        self.timeout_seconds = min(max(float(timeout_seconds), 1.0), 300.0)
        self._opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    @staticmethod
    def _validated_base_url(value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Knowledge Assistant URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("Knowledge Assistant URL must not contain credentials")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Knowledge Assistant URL must contain a valid port") from exc
        if port == 0 or parsed.netloc.endswith(":"):
            raise ValueError("Knowledge Assistant URL must contain a valid port")
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Knowledge Assistant URL must not contain a path, query, or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("non-loopback Knowledge Assistant connections require HTTPS")
        return value.strip().rstrip("/")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", authenticate=False)

    def list_documents(self, principal: Principal) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v1/documents", principal=principal)
        items = payload.get("items")
        return self._validated_items(items, "documents")

    def upload_document(
        self,
        principal: Principal,
        *,
        filename: str,
        content: bytes,
        collection_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._validated_resource(
            self._request(
                "POST",
                "/v1/documents",
                principal=principal,
                body={
                    "filename": filename,
                    "collection_id": collection_id,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                },
                idempotency_key=idempotency_key,
            ),
            "document",
        )

    def query(
        self,
        principal: Principal,
        *,
        query: str,
        collection_ids: list[str] | None,
        top_k: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if collection_ids is not None:
            body["collection_ids"] = collection_ids
        return self._request("POST", "/v1/query", principal=principal, body=body)

    def list_tasks(self, principal: Principal) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v1/tasks", principal=principal)
        items = payload.get("items")
        return self._validated_items(items, "tasks")

    def create_task(
        self,
        principal: Principal,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._validated_resource(
            self._request(
                "POST",
                "/v1/tasks",
                principal=principal,
                body={"tool_name": tool_name, "arguments": arguments},
                idempotency_key=idempotency_key,
            ),
            "task",
        )

    def approval_preview(self, principal: Principal, task_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/tasks/{quote(task_id, safe='')}/approval-preview",
            principal=principal,
        )

    def approve_task(
        self,
        principal: Principal,
        task_id: str,
        preview_hash: str,
    ) -> dict[str, Any]:
        return self._validated_resource(
            self._request(
                "POST",
                f"/v1/tasks/{quote(task_id, safe='')}/approve",
                principal=principal,
                body={"preview_hash": preview_hash},
            ),
            "approved task",
        )

    def reject_task(
        self,
        principal: Principal,
        task_id: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._validated_resource(
            self._request(
                "POST",
                f"/v1/tasks/{quote(task_id, safe='')}/reject",
                principal=principal,
                body={"reason": reason},
            ),
            "rejected task",
        )

    def metrics(self, principal: Principal) -> dict[str, Any]:
        return self._request("GET", "/v1/metrics", principal=principal)

    def get_trace(self, principal: Principal, trace_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/traces/{quote(trace_id, safe='')}",
            principal=principal,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        principal: Principal | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str = "",
        authenticate: bool = True,
    ) -> dict[str, Any]:
        encoded = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        headers = {"Accept": "application/json"}
        if encoded is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if authenticate and self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        if principal is not None:
            headers.update(
                {
                    "X-Tenant-ID": principal.tenant_id,
                    "X-User-ID": principal.user_id,
                    "X-Roles": ",".join(sorted(principal.roles)),
                }
            )
            if principal.collection_ids is not None:
                headers["X-Collection-IDs"] = ",".join(sorted(principal.collection_ids))
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            f"{self.base_url}{path}",
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status_code = int(response.status)
                raw_length = response.headers.get("Content-Length", "")
                if raw_length:
                    try:
                        declared_length = int(raw_length)
                    except ValueError as exc:
                        raise KnowledgeAssistantApiError(
                            status_code,
                            "invalid_response",
                            "Knowledge Assistant returned an invalid Content-Length",
                        ) from exc
                    if declared_length < 0:
                        raise KnowledgeAssistantApiError(
                            status_code,
                            "invalid_response",
                            "Knowledge Assistant returned an invalid Content-Length",
                        )
                    if declared_length > MAX_RESPONSE_BYTES:
                        raise KnowledgeAssistantApiError(
                            status_code,
                            "response_too_large",
                            "Knowledge Assistant response exceeds the desktop safety limit",
                        )
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise KnowledgeAssistantApiError(
                        status_code,
                        "response_too_large",
                        "Knowledge Assistant response exceeds the desktop safety limit",
                    )
        except HTTPError as exc:
            try:
                try:
                    raw = exc.read(MAX_RESPONSE_BYTES + 1)
                except (HTTPException, OSError, TimeoutError) as read_error:
                    raise KnowledgeAssistantApiError(
                        exc.code,
                        "invalid_response",
                        "Knowledge Assistant returned a truncated error response",
                    ) from read_error
            finally:
                exc.close()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise KnowledgeAssistantApiError(
                    exc.code,
                    "response_too_large",
                    "Knowledge Assistant response exceeds the desktop safety limit",
                ) from exc
            self._raise_api_error(exc.code, raw)
            raise AssertionError("unreachable") from exc
        except (HTTPException, URLError, OSError, TimeoutError) as exc:
            raise ConnectionError("Knowledge Assistant service is unavailable") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KnowledgeAssistantApiError(
                status_code,
                "invalid_response",
                "Knowledge Assistant returned invalid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise KnowledgeAssistantApiError(
                status_code,
                "invalid_response",
                "Knowledge Assistant returned an unexpected response",
            )
        return payload

    @staticmethod
    def _validated_items(value: object, resource: str) -> list[dict[str, Any]]:
        malformed = not isinstance(value, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            for item in value
        )
        if malformed:
            raise KnowledgeAssistantApiError(
                200,
                "invalid_response",
                f"Knowledge Assistant returned malformed {resource}",
            )
        return value

    @staticmethod
    def _validated_resource(value: object, resource: str) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not value["id"].strip()
            or not isinstance(value.get("status"), str)
            or not value["status"].strip()
        ):
            raise KnowledgeAssistantApiError(
                200,
                "invalid_response",
                f"Knowledge Assistant returned malformed {resource}",
            )
        return value

    @staticmethod
    def _raise_api_error(status_code: int, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        detail = error if isinstance(error, dict) else {}
        raise KnowledgeAssistantApiError(
            status_code,
            str(detail.get("code") or "http_error"),
            str(detail.get("message") or f"Knowledge Assistant request failed ({status_code})"),
            str(detail.get("request_id") or ""),
        )
