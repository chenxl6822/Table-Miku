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
SECTION_RE = re.compile(r"[（(](?P<section>\d{1,2}\s*[-~到至]\s*\d{1,2}|\d{1,2})\s*节[）)]")
COURSE_TITLE_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9ⅠⅡⅢⅣⅤ（）()、]+")


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


def parse_course_time_slots(raw: str, season: str = "default") -> list[dict[str, str]]:
    text = raw.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        season = str(payload.get("season") or season or "default")
        items = payload.get("slots") or payload.get("course_time_slots") or []
        return [_normalize_slot(item, season) for item in items if _normalize_slot(item, season)]
    if isinstance(payload, list):
        return [_normalize_slot(item, season) for item in payload if _normalize_slot(item, season)]

    slots: list[dict[str, str]] = []
    for line in text.splitlines():
        cleaned = " ".join(line.strip().split())
        if not cleaned:
            continue
        time_match = TIME_RANGE_RE.search(cleaned)
        if not time_match:
            continue
        local_season = season
        season_match = re.search(r"(冬季|夏季|冬令|夏令|winter|summer|default|默认)", cleaned, re.I)
        if season_match:
            local_season = _normalize_season(season_match.group(1))
        section_match = re.search(r"(\d{1,2}\s*[-~到至]\s*\d{1,2}|\d{1,2})\s*(?:节|课)?", cleaned)
        if not section_match:
            continue
        slots.append(
            {
                "season": local_season,
                "section": _normalize_section(section_match.group(1)),
                "start": _normalize_time(time_match.group("start"), time_match.group("start_min")),
                "end": _normalize_time(time_match.group("end"), time_match.group("end_min")),
            }
        )
    return _dedupe_slots(slots)


def format_course_time_slots(slots: list[dict[str, Any]], limit: int = 20) -> str:
    if not slots:
        return ""
    lines = [
        f"{slot.get('season', 'default')} {slot.get('section')} {slot.get('start')}-{slot.get('end')}"
        for slot in slots[:limit]
    ]
    return "\n".join(lines)


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("缺少 pypdf 依赖，请先安装 requirements.txt。") from exc

    reader = PdfReader(str(path))
    chunks: list[str] = []
    for page in reader.pages:
        layout = page.extract_text(extraction_mode="layout") or ""
        plain = page.extract_text() or ""
        chunks.append(layout if len(layout) >= len(plain) * 0.8 else plain)
    return "\n".join(chunks)


def parse_timetable_text(raw: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    entries.extend(_parse_layout_timetable(raw))
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
            course = _remove_weekday_alias(course)
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
    return _dedupe_entries(entries)


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
    lines = []
    for entry in entries[-limit:]:
        if entry.get("section"):
            when = str(entry.get("section"))
        else:
            when = f"{entry.get('start')}-{entry.get('end')}"
        lines.append(f"{entry.get('weekday') or '未定'} {when} {entry.get('course')}")
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
    for raw, normalized in sorted(WEEKDAY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if raw in text:
            return normalized
    return ""


def _remove_weekday_alias(text: str) -> str:
    for raw in sorted(WEEKDAY_ALIASES, key=len, reverse=True):
        if raw in text:
            return text.replace(raw, "", 1)
    return text


def _parse_layout_timetable(raw: str) -> list[dict[str, Any]]:
    lines = raw.splitlines()
    header = next((line for line in lines if "星期一" in line and "星期日" in line), "")
    if not header:
        return []

    columns = _weekday_columns(header)
    if not columns:
        return []

    column_chunks = {weekday: [] for weekday, _, _ in columns}
    for line in lines[lines.index(header) + 1 :]:
        for match in re.finditer(r"\S(?:.*?\S)?(?=\s{2,}|\s*$)", line.rstrip()):
            chunk = match.group(0).strip()
            if not chunk or chunk in {"上午", "下午", "晚上"} or chunk.isdigit():
                continue
            weekday = _weekday_for_position(match.start(), columns)
            if weekday:
                column_chunks[weekday].append(chunk)

    entries: list[dict[str, Any]] = []
    for weekday, chunks in column_chunks.items():
        text = _repair_pdf_text(" ".join(chunks))
        entries.extend(_parse_course_blocks(weekday, text))
    return entries


def _weekday_columns(header: str) -> list[tuple[str, int, int]]:
    points: list[tuple[str, int]] = []
    for raw, normalized in WEEKDAY_ALIASES.items():
        if not raw.startswith("星期"):
            continue
        index = header.find(raw)
        if index >= 0:
            points.append((normalized, index))
    points.sort(key=lambda item: item[1])
    if not points:
        return []

    columns: list[tuple[str, int, int]] = []
    for index, (weekday, start) in enumerate(points):
        previous_start = points[index - 1][1] if index else max(0, start - 16)
        next_start = points[index + 1][1] if index + 1 < len(points) else start + 32
        left = max(0, (previous_start + start) // 2) if index else max(0, start - 8)
        right = (start + next_start) // 2 if index + 1 < len(points) else start + 36
        columns.append((weekday, left, right))
    return columns


def _weekday_for_position(position: int, columns: list[tuple[str, int, int]]) -> str:
    for weekday, left, right in columns:
        if left <= position < right:
            return weekday
    nearest = min(columns, key=lambda item: abs(position - item[1]), default=None)
    return nearest[0] if nearest and abs(position - nearest[1]) <= 10 else ""


def _parse_course_blocks(weekday: str, text: str) -> list[dict[str, Any]]:
    matches = list(SECTION_RE.finditer(text))
    entries: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        prefix = text[: match.start()]
        previous_end = matches[index - 1].end() if index else 0
        title_area = text[previous_end : match.start()].strip()
        title = _last_course_title(title_area) or _last_course_title(prefix)
        if not title or _looks_like_metadata(title):
            continue
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        detail = text[match.end() : next_start].strip(" /")
        entries.append(
            {
                "weekday": weekday,
                "section": _normalize_section(match.group("section")),
                "start": "",
                "end": "",
                "course": title,
                "detail": detail,
            }
        )
    return entries


def _last_course_title(text: str) -> str:
    candidates = [candidate.strip(" /:：-") for candidate in COURSE_TITLE_RE.findall(text)]
    candidates = [candidate for candidate in candidates if len(candidate) >= 2 and not _looks_like_metadata(candidate)]
    if len(candidates) >= 2 and candidates[-1].startswith(("（", "(")):
        return candidates[-2] + candidates[-1]
    return candidates[-1] if candidates else ""


def _looks_like_metadata(text: str) -> bool:
    normalized = text.strip(" /:：-")
    if normalized in {"考试", "考查", "未安排", "教师", "学分", "校本部", "本部"}:
        return True
    if ("节" in normalized or "(" in normalized or "（" in normalized) and re.search(r"\d", normalized):
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]{2,3}", normalized) and normalized not in {"足球"}:
        return True
    if re.fullmatch(r"\d+周", normalized):
        return True
    return any(
        marker in normalized
        for marker in (
            "校区",
            "场地",
            "教师",
            "考核方式",
            "学分",
            "本部",
            "打印时间",
            "兴教楼",
            "逸夫楼",
            "计算中心",
            "足球场",
            "报告厅",
            "阶梯",
        )
    )


def _repair_pdf_text(text: str) -> str:
    return (
        text.replace("（ JAVA ）", "（JAVA）")
        .replace("（JAVA ）", "（JAVA）")
        .replace("国际安 全", "国际安全")
    )


def _normalize_section(section: str) -> str:
    compact = re.sub(r"\s+", "", section).replace("到", "-").replace("至", "-").replace("~", "-")
    return f"{compact}节"


def _normalize_slot(item: Any, season: str) -> dict[str, str]:
    if not isinstance(item, dict):
        return {}
    section = str(item.get("section") or item.get("节次") or item.get("class") or "").strip()
    start = str(item.get("start") or item.get("开始") or "").strip()
    end = str(item.get("end") or item.get("结束") or "").strip()
    if not section or not re.match(r"^[0-2]?\d:[0-5]\d$", start) or not re.match(r"^[0-2]?\d:[0-5]\d$", end):
        return {}
    return {
        "season": _normalize_season(str(item.get("season") or item.get("季节") or season or "default")),
        "section": _normalize_section(section.replace("节", "")),
        "start": start,
        "end": end,
    }


def _normalize_season(value: str) -> str:
    value = value.strip().lower()
    if value in {"冬季", "冬令", "winter"}:
        return "winter"
    if value in {"夏季", "夏令", "summer"}:
        return "summer"
    return "default"


def _dedupe_slots(slots: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for slot in slots:
        key = (slot.get("season", ""), slot.get("section", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(slot)
    return unique


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        key = (
            str(entry.get("weekday", "")),
            str(entry.get("section", "")),
            str(entry.get("start", "")),
            str(entry.get("end", "")),
            str(entry.get("course", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _normalize_time(hour: str, minute: str) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def _record_id(prefix: str) -> str:
    return f"{prefix}-{date.today().strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"
