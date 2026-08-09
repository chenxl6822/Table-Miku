from __future__ import annotations

import json
import math
import statistics
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from .auth import Principal
from .database import AssistantDatabase


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        lowered = str(key).casefold()
        if any(marker in lowered for marker in ("content", "prompt", "password", "token", "secret", "key")):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[str(key)] = value
    return result


@dataclass
class TraceContext:
    database: AssistantDatabase
    trace_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    _span_stack: list[str] = field(default_factory=list)

    def add_tokens(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[str]:
        span_id = f"span-{uuid.uuid4().hex}"
        parent_span_id = self._span_stack[-1] if self._span_stack else None
        started_at = utc_now()
        started = time.perf_counter()
        with self.database.connect() as conn:
            conn.execute(
                "INSERT INTO spans(id, trace_id, parent_span_id, name, status, started_at, attributes_json) "
                "VALUES(?, ?, ?, ?, 'running', ?, ?)",
                (
                    span_id,
                    self.trace_id,
                    parent_span_id,
                    name[:120],
                    started_at,
                    json.dumps(safe_attributes(attributes), ensure_ascii=False),
                ),
            )
        self._span_stack.append(span_id)
        try:
            yield span_id
        except Exception:
            self._finish_span(span_id, "error", started)
            raise
        else:
            self._finish_span(span_id, "ok", started)
        finally:
            if self._span_stack and self._span_stack[-1] == span_id:
                self._span_stack.pop()

    def _finish_span(self, span_id: str, status: str, started: float) -> None:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE spans SET status = ?, finished_at = ?, latency_ms = ? WHERE id = ?",
                (status, utc_now(), latency_ms, span_id),
            )


class TraceRecorder:
    def __init__(self, database: AssistantDatabase) -> None:
        self.database = database

    @contextmanager
    def trace(
        self,
        operation: str,
        principal: Principal,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[TraceContext]:
        trace_id = f"trace-{uuid.uuid4().hex}"
        started_at = utc_now()
        started = time.perf_counter()
        with self.database.connect() as conn:
            conn.execute(
                "INSERT INTO traces(id, tenant_id, user_id, operation, status, started_at, attributes_json) "
                "VALUES(?, ?, ?, ?, 'running', ?, ?)",
                (
                    trace_id,
                    principal.tenant_id,
                    principal.user_id,
                    operation[:120],
                    started_at,
                    json.dumps(safe_attributes(attributes), ensure_ascii=False),
                ),
            )
        context = TraceContext(self.database, trace_id)
        try:
            yield context
        except Exception as exc:
            self._finish_trace(context, "error", started, type(exc).__name__)
            raise
        else:
            self._finish_trace(context, "ok", started, "")

    def _finish_trace(self, context: TraceContext, status: str, started: float, error_code: str) -> None:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE traces SET status = ?, finished_at = ?, latency_ms = ?, input_tokens = ?, "
                "output_tokens = ?, error_code = ? WHERE id = ?",
                (
                    status,
                    utc_now(),
                    latency_ms,
                    context.input_tokens,
                    context.output_tokens,
                    error_code[:120],
                    context.trace_id,
                ),
            )

    def metrics(self, principal: Principal, limit: int = 1000) -> dict[str, Any]:
        principal.require("trace:read")
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT operation, status, latency_ms, input_tokens, output_tokens "
                "FROM traces WHERE tenant_id = ? AND status <> 'running' ORDER BY started_at DESC LIMIT ?",
                (principal.tenant_id, min(max(int(limit), 1), 10_000)),
            ).fetchall()
        latencies = [float(row["latency_ms"]) for row in rows]
        sorted_latencies = sorted(latencies)
        p95_index = max(0, math.ceil(len(sorted_latencies) * 0.95) - 1)
        operations: dict[str, dict[str, int]] = {}
        for row in rows:
            operation = str(row["operation"])
            bucket = operations.setdefault(operation, {"count": 0, "errors": 0})
            bucket["count"] += 1
            bucket["errors"] += int(row["status"] == "error")
        return {
            "trace_count": len(rows),
            "error_count": sum(int(row["status"] == "error") for row in rows),
            "latency_ms": {
                "average": round(statistics.fmean(latencies), 3) if latencies else 0.0,
                "p95": round(sorted_latencies[p95_index], 3) if sorted_latencies else 0.0,
                "max": round(max(latencies), 3) if latencies else 0.0,
            },
            "tokens": {
                "input": sum(int(row["input_tokens"]) for row in rows),
                "output": sum(int(row["output_tokens"]) for row in rows),
                "total": sum(int(row["input_tokens"]) + int(row["output_tokens"]) for row in rows),
            },
            "operations": operations,
        }

    def get_trace(self, principal: Principal, trace_id: str) -> dict[str, Any]:
        principal.require("trace:read")
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM traces WHERE id = ? AND tenant_id = ?",
                (trace_id, principal.tenant_id),
            ).fetchone()
            if row is None:
                from .auth import ResourceNotFound

                raise ResourceNotFound("trace not found")
            spans = conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at, id",
                (trace_id,),
            ).fetchall()
        result = dict(row)
        result["attributes"] = json.loads(result.pop("attributes_json"))
        result["spans"] = []
        for span in spans:
            item = dict(span)
            item["attributes"] = json.loads(item.pop("attributes_json"))
            result["spans"].append(item)
        return result
