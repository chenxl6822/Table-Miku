from __future__ import annotations

from pathlib import Path

import pytest

from table_miku.knowledge_assistant import KnowledgeAssistantService, Principal


@pytest.mark.parametrize(
    ("latencies", "expected_p95"),
    [
        ([], 0.0),
        ([1.0], 1.0),
        ([1.0, 2.0], 2.0),
        ([1.0, 2.0, 100.0], 100.0),
        ([float(value) for value in range(1, 21)], 19.0),
    ],
)
def test_metrics_uses_nearest_rank_p95(
    tmp_path: Path,
    latencies: list[float],
    expected_p95: float,
):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    principal = Principal("tenant-a", "viewer-1", frozenset({"viewer"}))
    with service.database.connect() as conn:
        for index, latency_ms in enumerate(latencies):
            conn.execute(
                "INSERT INTO traces(id, tenant_id, user_id, operation, status, started_at, finished_at, "
                "latency_ms) VALUES(?, ?, ?, 'test.operation', 'ok', ?, ?, ?)",
                (
                    f"trace-{index}",
                    principal.tenant_id,
                    principal.user_id,
                    "2026-08-09T00:00:00+00:00",
                    "2026-08-09T00:00:01+00:00",
                    latency_ms,
                ),
            )

    metrics = service.traces.metrics(principal)

    assert metrics["trace_count"] == len(latencies)
    assert metrics["latency_ms"]["p95"] == expected_p95
