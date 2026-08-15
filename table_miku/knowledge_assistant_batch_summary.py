from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

FAILED_STATUSES = frozenset(
    {"failed", "cancel_rejected", "reconciliation_required", "unavailable"}
)
UNKNOWN_STATUSES = frozenset({"outcome_unknown", "pending", "tracking"})
CANCELLED_STATUSES = frozenset({"cancelled", "abandoned"})
SUCCEEDED_STATUSES = frozenset({"succeeded"})
MAX_FAILED_LINES = 5
MAX_ERROR_CHARS = 80


@dataclass(frozen=True)
class BatchIngestionSummary:
    total: int
    succeeded: int
    failed: int
    unknown: int
    active: int
    cancelled: int
    failed_lines: tuple[str, ...]

    def as_text(self) -> str:
        if self.total <= 0:
            return ""
        text = (
            f"本批 {self.total} 个：成功 {self.succeeded}，失败 {self.failed}，"
            f"待确认 {self.unknown}，进行中 {self.active}"
        )
        if self.cancelled:
            text += f"，已取消 {self.cancelled}"
        if self.failed_lines:
            shown = list(self.failed_lines[:MAX_FAILED_LINES])
            suffix = "…" if len(self.failed_lines) > MAX_FAILED_LINES else ""
            text += "。失败：" + "；".join(shown) + suffix
        else:
            text += "。"
        return text


def summarize_ingestion_batch(
    batch_ids: tuple[str, ...],
    items: Mapping[str, Mapping[str, Any]],
) -> BatchIngestionSummary:
    if not batch_ids:
        return BatchIngestionSummary(0, 0, 0, 0, 0, 0, ())
    wanted = {item for item in batch_ids if item}
    matched: dict[str, Mapping[str, Any]] = {}
    for key, item in items.items():
        local_id = str(item.get("local_id") or "")
        job_id = str(item.get("job_id") or "").strip()
        job_key = f"job:{job_id}" if job_id else ""
        aliases = {key, local_id, job_key} - {""}
        if aliases & wanted:
            matched[key] = item
    succeeded = failed = unknown = cancelled = active = 0
    failed_lines: list[str] = []
    for item in matched.values():
        status = str(item.get("status") or "")
        if status in SUCCEEDED_STATUSES:
            succeeded += 1
        elif status in FAILED_STATUSES:
            failed += 1
            failed_lines.append(_failed_line(item))
        elif status in UNKNOWN_STATUSES:
            unknown += 1
        elif status in CANCELLED_STATUSES:
            cancelled += 1
        else:
            active += 1
    active += max(0, len(batch_ids) - len(matched))
    return BatchIngestionSummary(
        total=len(batch_ids),
        succeeded=succeeded,
        failed=failed,
        unknown=unknown,
        active=active,
        cancelled=cancelled,
        failed_lines=tuple(failed_lines),
    )


def safe_ingest_filename(value: object) -> str:
    name = Path(str(value or "")).name.strip()
    return name or "未命名文件"


def _failed_line(item: Mapping[str, Any]) -> str:
    filename = safe_ingest_filename(item.get("filename"))
    error = " ".join(str(item.get("error_message") or "").split())
    if len(error) > MAX_ERROR_CHARS:
        error = error[: MAX_ERROR_CHARS - 1] + "…"
    if not error:
        error = str(item.get("status") or "failed")
    return f"{filename}：{error}"
