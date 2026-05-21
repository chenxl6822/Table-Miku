from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .storage import read_json, write_json


WEEKDAY_ALIASES = {
    "周一": "周一",
    "星期一": "周一",
    "Mon": "周一",
    "Monday": "周一",
    "周二": "周二",
    "星期二": "周二",
    "Tue": "周二",
    "Tuesday": "周二",
    "周三": "周三",
    "星期三": "周三",
    "Wed": "周三",
    "Wednesday": "周三",
    "周四": "周四",
    "星期四": "周四",
    "Thu": "周四",
    "Thursday": "周四",
    "周五": "周五",
    "星期五": "周五",
    "Fri": "周五",
    "Friday": "周五",
    "周六": "周六",
    "星期六": "周六",
    "Sat": "周六",
    "Saturday": "周六",
    "周日": "周日",
    "周天": "周日",
    "星期日": "周日",
    "星期天": "周日",
    "Sun": "周日",
    "Sunday": "周日",
}

TIME_RANGE_RE = re.compile(
    r"(?P<start>[01]?\d|2[0-3])[:：](?P<start_min>[0-5]\d)"
    r"\s*(?:-|~|—|至|到)\s*"
    r"(?P<end>[01]?\d|2[0-3])[:：](?P<end_min>[0-5]\d)"
)


def add_application_record(raw: str) -> dict[str, Any]:
    record = _parse_structured_note(raw)
    record.setdefault("company", record.get("公司") or record.get("company") or record.get("title") or "未命名公司")
    record.setdefault("position", record.get("岗位") or record.get("position") or record.get("role") or "")
    record.setdefault("status", record.get("状态") or record.get("status") or "已投递")
    record.setdefault("source", record.get("渠道") or record.get("source") or "")
    record.setdefault("next_step", record.get("下一步") or record.get("next_step") or "")
    record["created_at"] = datetime.now().isoformat(timespec="seconds")
    record["id"] = _record_id("app")

    records = load_application_records()
    records.append(record)
    write_json("applications.json", records)
    return record


def load_application_records() -> list[dict[str, Any]]:
    payload = read_json("applications.json", [])
    return payload if isinstance(payload, list) else []


def add_interview_review(raw: str) -> dict[str, Any]:
    record = _parse_structured_note(raw)
    record.setdefault("company", record.get("公司") or record.get("company") or record.get("title") or "未命名面试")
    record.setdefault("round", record.get("轮次") or record.get("round") or record.get("round_name") or "")
    record.setdefault("summary", record.get("复盘") or record.get("summary") or raw.strip())
    record.setdefault("next_step", record.get("下一步") or record.get("next_step") or "")
    record["created_at"] = datetime.now().isoformat(timespec="seconds")
    record["id"] = _record_id("interview")

    records = load_interview_reviews()
    records.append(record)
    write_json("interviews.json", records)
    return record


def load_interview_reviews() -> list[dict[str, Any]]:
    payload = read_json("interviews.json", [])
    return payload if isinstance(payload, list) else []


def import_timetable_text(raw: str, source: str = "manual") -> list[dict[str, Any]]:
    entries = parse_timetable_text(raw)
    if not entries:
        return []
    timetable = load_timetable()
    timetable.extend({**entry, "source": source, "imported_at": datetime.now().isoformat(timespec="seconds")} for entry in entries)
    write_json("timetable.json", timetable)
    return entries


def import_timetable_pdf(path: Path) -> list[dict[str, Any]]:
    text = extract_pdf_text(path)
    return import_timetable_text(text, path.name)


def load_timetable() -> list[dict[str, Any]]:
    payload = read_json("timetable.json", [])
    return payload if isinstance(payload, list) else []


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("缺少 pypdf 依赖，请先安装 requirements.txt。") from exc

    reader = PdfReader(str(path))
    chunks: list[str] = []
    for page in reader.pages:
        chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


def parse_timetable_text(raw: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        cleaned = " ".join(line.strip().split())
        if not cleaned:
            continue
        weekday = _find_weekday(cleaned)
        time_match = TIME_RANGE_RE.search(cleaned)
        if not time_match:
            continue
        course = cleaned
        if weekday:
            course = course.replace(weekday, "", 1)
        course = TIME_RANGE_RE.sub("", course, count=1)
        course = re.sub(r"^[,，:：;\-\s]+", "", course).strip()
        course = course or "课程"
        entries.append(
            {
                "weekday": weekday or "",
                "start": _normalize_time(time_match.group("start"), time_match.group("start_min")),
                "end": _normalize_time(time_match.group("end"), time_match.group("end_min")),
                "course": course,
            }
        )
    return entries


def assistant_context() -> str:
    applications = format_application_summary(load_application_records(), 3)
    interviews = format_interview_summary(load_interview_reviews(), 3)
    timetable = format_timetable(load_timetable(), 5)
    parts = [part for part in (applications, interviews, timetable) if part]
    return "\n".join(parts) if parts else "暂时没有课程表、投递记录或面试复盘。"


def format_application_summary(records: list[dict[str, Any]], limit: int = 5) -> str:
    if not records:
        return ""
    lines = []
    for record in records[-limit:]:
        lines.append(
            f"{record.get('company', '公司')} - {record.get('position', '')}"
            f"（{record.get('status', '已投递')}）下一步：{record.get('next_step', '待补充')}"
        )
    return "最近投递：\n" + "\n".join(lines)


def format_interview_summary(records: list[dict[str, Any]], limit: int = 5) -> str:
    if not records:
        return ""
    lines = []
    for record in records[-limit:]:
        summary = str(record.get("summary", "")).replace("\n", " ")
        if len(summary) > 58:
            summary = summary[:55] + "..."
        lines.append(f"{record.get('company', '面试')} {record.get('round', '')}：{summary}")
    return "最近面试复盘：\n" + "\n".join(lines)


def format_timetable(entries: list[dict[str, Any]], limit: int = 8) -> str:
    if not entries:
        return ""
    lines = [
        f"{entry.get('weekday') or '未定'} {entry.get('start')}-{entry.get('end')} {entry.get('course')}"
        for entry in entries[-limit:]
    ]
    return "课程表：\n" + "\n".join(lines)


def _parse_structured_note(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}

    record: dict[str, Any] = {}
    free_lines: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip().strip("-* ")
        if not cleaned:
            continue
        if ":" in cleaned or "：" in cleaned:
            key, value = re.split(r"[:：]", cleaned, maxsplit=1)
            record[key.strip()] = value.strip()
        else:
            free_lines.append(cleaned)
    if free_lines:
        record.setdefault("title", free_lines[0])
        record.setdefault("summary", "\n".join(free_lines))
    return record


def _find_weekday(text: str) -> str:
    for raw, normalized in WEEKDAY_ALIASES.items():
        if raw in text:
            return normalized
    return ""


def _normalize_time(hour: str, minute: str) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def _record_id(prefix: str) -> str:
    return f"{prefix}-{date.today().strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"
