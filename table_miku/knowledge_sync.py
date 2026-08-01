"""Read-only Obsidian parsing and incremental knowledge synchronization."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import knowledge_db, knowledge_repository as repo
from .knowledge_trusted_sources import _is_relative_to, _is_sensitive_path
from .paths import PROJECT_ROOT
from .storage import load_settings


PARSER_VERSION = "obsidian-v2"
ALLOWED_SUBDIRECTORIES = ("计算机知识", "05-Interview")
ELIGIBLE_NOTE_TYPES = {"knowledge", "question", "algorithm", "interview"}
MAX_NOTE_BYTES = 2 * 1024 * 1024
QUESTION_HEADINGS = ("题目", "常见面试问法", "面试中最常见问法", "面试官追问")
ANSWER_HEADINGS = (
    "标准答案", "我的回答模板", "面试口述稿", "一句话理解", "小白解释", "核心概念", "解法"
)
PITFALL_HEADINGS = ("易错点", "常见误区", "常见错误")
TOPIC_ALIASES = {
    "数据库": "数据库原理",
    "mysql": "MySQL",
    "编译器": "编译原理",
    "计算机组成": "计算机组成原理",
    "java": "Java 后端基础",
    "jvm": "Java 后端基础",
    "spring": "Java 后端基础",
    "redis": "Redis",
    "go": "Go 后端基础",
    "算法": "算法设计与分析",
    "哈希表": "算法设计与分析",
    "数组": "算法设计与分析",
    "链表": "算法设计与分析",
    "动态规划": "算法设计与分析",
}


@dataclass
class ParsedQuestion:
    topic: str
    question: str
    answer: str
    answer_summary: str
    answer_detail: str
    key_points: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    question_type: str = "high-frequency"
    difficulty: str = "normal"
    source_label: str = ""
    canonical_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_topic": self.topic,
            "question": self.question,
            "answer": self.answer,
            "answer_summary": self.answer_summary,
            "answer_detail": self.answer_detail,
            "key_points": self.key_points,
            "pitfalls": self.pitfalls,
            "follow_ups": self.follow_ups,
            "question_type": self.question_type,
            "difficulty": self.difficulty,
            "source_label": self.source_label,
            "canonical_key": self.canonical_key,
        }


@dataclass
class ParsedNote:
    title: str
    topic: str
    note_type: str
    status: str
    overview: str
    sections: list[dict[str, str]]
    key_points: list[str]
    pitfalls: list[str]
    questions: list[ParsedQuestion]
    tags: list[str]
    source_label: str
    quality_score: float


def discover_obsidian_vault(settings: dict[str, Any] | None = None) -> Path | None:
    """Resolve the configured Vault or the portable sibling Vault location."""
    settings = settings or load_settings()
    trusted = ((settings.get("knowledge") or {}).get("trusted_sources") or {})
    if not trusted.get("enabled", True):
        return None
    configured = str(trusted.get("obsidian_vault") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_dir() else None
    sibling = PROJECT_ROOT.parent / "Obsidian Vault"
    return sibling.resolve() if sibling.is_dir() else None


def parse_obsidian_note(path: Path, vault_root: Path) -> ParsedNote | None:
    """Parse one eligible Markdown note without mutating it."""
    resolved_root = vault_root.resolve()
    resolved_path = path.resolve()
    if not _is_relative_to(resolved_path, resolved_root) or _is_sensitive_path(resolved_path):
        return None
    if not resolved_path.is_file() or resolved_path.suffix.lower() != ".md":
        return None
    if resolved_path.stat().st_size > MAX_NOTE_BYTES:
        return None

    text = resolved_path.read_text(encoding="utf-8-sig")
    metadata, body = _split_frontmatter(text)
    note_type = str(metadata.get("type") or "").strip().lower()
    if note_type not in ELIGIBLE_NOTE_TYPES:
        return None
    status = str(metadata.get("status") or "active").strip().lower()
    if status in {"duplicated", "archived", "deprecated"}:
        return None

    title = _title_from_body(body) or resolved_path.stem
    tags = _metadata_list(metadata, "tags")
    declared_topics = _unique_items(
        [canonical_topic(item) for item in _metadata_list(metadata, "topic")],
        limit=8,
    )
    topic = " / ".join(declared_topics) if declared_topics else canonical_topic(
        _infer_topic(resolved_path, title, tags)
    )
    sections = _markdown_sections(body)
    section_map = _group_sections(sections)
    overview = _first_section(section_map, ("一句话理解", "小白解释", "题目")) or _first_prose(body)
    overview = _clip(_plain_text(overview), 700)
    key_points = _unique_items(
        _items_for_headings(section_map, ("考察点", "核心概念", "关键点")), limit=10
    )
    pitfalls = _unique_items(_items_for_headings(section_map, PITFALL_HEADINGS), limit=8)
    source_label = str(resolved_path.relative_to(resolved_root)).replace("\\", "/")
    questions = _questions_from_note(
        title=title,
        topic=topic,
        declared_topics=declared_topics,
        note_type=note_type,
        metadata=metadata,
        body=body,
        sections=sections,
        section_map=section_map,
        key_points=key_points,
        pitfalls=pitfalls,
        source_label=source_label,
    )
    content_sections = [
        {"heading": heading, "content": _clip(_plain_text(content), 1600)}
        for heading, content in sections
        if content.strip() and not _heading_matches(heading, QUESTION_HEADINGS)
    ][:12]
    completeness = sum(
        bool(value)
        for value in (overview, key_points, pitfalls, questions, section_map.get("一句话理解"))
    )
    quality_score = min(0.55 + completeness * 0.08, 0.95)
    return ParsedNote(
        title=title,
        topic=topic,
        note_type=note_type,
        status=status,
        overview=overview or title,
        sections=content_sections,
        key_points=key_points,
        pitfalls=pitfalls,
        questions=questions,
        tags=_unique_items(tags + [note_type, "obsidian-readonly"], limit=20),
        source_label=source_label,
        quality_score=quality_score,
    )


def preview_obsidian_sync(vault_root: str | Path | None = None) -> dict[str, Any]:
    """Analyze eligible notes without opening the Table Miku database."""
    root = Path(vault_root).resolve() if vault_root else discover_obsidian_vault()
    if root is None or not root.is_dir():
        return {"available": False, "files": 0, "eligible": 0, "questions": 0, "errors": []}
    eligible = 0
    questions = 0
    errors: list[str] = []
    files = list(_iter_markdown_files(root))
    for path in files:
        try:
            parsed = parse_obsidian_note(path, root)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if parsed is not None:
            eligible += 1
            questions += len(parsed.questions)
    return {
        "available": True,
        "vault_root": str(root),
        "files": len(files),
        "eligible": eligible,
        "questions": questions,
        "errors": errors[:20],
    }


def sync_obsidian_knowledge(vault_root: str | Path | None = None) -> dict[str, Any]:
    """Incrementally ingest whitelisted notes into Table Miku's local SQLite DB."""
    root = Path(vault_root).resolve() if vault_root else discover_obsidian_vault()
    if root is None or not root.is_dir():
        return {
            "available": False,
            "vault_root": "",
            "scanned": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "deleted": 0,
            "questions": 0,
            "errors": ["未找到可用的 Obsidian Vault。"],
        }

    conn = knowledge_db.connect(check_same_thread=False)
    conn.row_factory = sqlite3.Row
    knowledge_db.init_db(conn)
    root_key = str(root)
    existing = {
        str(row["relative_path"]): dict(row)
        for row in conn.execute(
            "SELECT * FROM knowledge_documents WHERE vault_root = ?", (root_key,)
        ).fetchall()
    }
    summary: dict[str, Any] = {
        "available": True,
        "vault_root": root_key,
        "scanned": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "deleted": 0,
        "questions": 0,
        "errors": [],
    }
    seen: set[str] = set()
    try:
        for path in _iter_markdown_files(root):
            summary["scanned"] += 1
            relative = str(path.relative_to(root)).replace("\\", "/")
            seen.add(relative)
            try:
                raw = path.read_bytes()
                stat = path.stat()
                fingerprint = hashlib.sha256(PARSER_VERSION.encode("utf-8") + b"\0" + raw).hexdigest()
                previous = existing.get(relative)
                if previous and previous.get("content_hash") == fingerprint and previous.get("status") == "active":
                    summary["unchanged"] += 1
                    continue
                parsed = parse_obsidian_note(path, root)
                if parsed is None:
                    if previous and previous.get("status") == "active":
                        _deactivate_document(conn, str(previous["id"]))
                    _upsert_document_status(
                        conn, root_key, relative, fingerprint, stat, "", "skipped", "", None, None
                    )
                    summary["skipped"] += 1
                    continue
                created = previous is None
                count = _store_parsed_note(conn, root, relative, fingerprint, stat, parsed)
                summary["created" if created else "updated"] += 1
                summary["questions"] += count
                conn.commit()
            except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
                conn.rollback()
                summary["errors"].append(f"{relative}: {exc}")

        for relative, document in existing.items():
            if relative in seen or document.get("status") == "deleted":
                continue
            _deactivate_document(conn, str(document["id"]))
            conn.execute(
                "UPDATE knowledge_documents SET status = 'deleted', last_indexed_at = ?, error = '' WHERE id = ?",
                (_now(), document["id"]),
            )
            summary["deleted"] += 1
        conn.commit()
        summary["errors"] = summary["errors"][:20]
        return summary
    finally:
        conn.close()


def knowledge_sync_status() -> dict[str, Any]:
    knowledge_db.init_db()
    conn = knowledge_db.connect()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) total,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) active,
                   SUM(CASE WHEN status = 'deleted' THEN 1 ELSE 0 END) deleted,
                   SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) skipped,
                   MAX(last_indexed_at) last_indexed_at
            FROM knowledge_documents
            """
        ).fetchone()
        questions = conn.execute(
            "SELECT COUNT(*) FROM knowledge_qa_pairs WHERE active = 1"
        ).fetchone()[0]
        return {
            "documents": int(row[0] or 0),
            "active": int(row[1] or 0),
            "deleted": int(row[2] or 0),
            "skipped": int(row[3] or 0),
            "last_indexed_at": str(row[4] or ""),
            "questions": int(questions or 0),
        }
    finally:
        conn.close()


def canonical_topic(topic: str) -> str:
    cleaned = " ".join(str(topic).strip().split())
    if not cleaned:
        return "计算机基础"
    return TOPIC_ALIASES.get(cleaned.lower(), TOPIC_ALIASES.get(cleaned, cleaned))


def canonical_question_key(topic: str, question: str) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", question.lower())
    payload = f"{canonical_topic(topic).lower()}\0{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def matched_key_points(user_answer: str, key_points: Iterable[str]) -> list[str]:
    """Return deterministic offline coverage hints; never grade correctness."""
    normalized_answer = _normalized_match_text(user_answer)
    matched: list[str] = []
    for point in key_points:
        tokens = [token for token in _tokens(str(point)) if len(token) >= 2]
        if tokens and any(_normalized_match_text(token) in normalized_answer for token in tokens):
            matched.append(str(point))
    return matched


def _store_parsed_note(
    conn,
    root: Path,
    relative: str,
    fingerprint: str,
    stat,
    parsed: ParsedNote,
) -> int:
    now = _now()
    root_key = str(root)
    document_id = _stable_id("doc", f"{root_key}\0{relative}")
    source_id = _stable_id("src", f"obsidian-readonly\0{root_key}\0{relative}")
    card_id = _stable_id("obs", f"{parsed.topic}\0{_normalized_match_text(parsed.title)}")
    previous_document = conn.execute(
        "SELECT card_id FROM knowledge_documents WHERE id = ?", (document_id,)
    ).fetchone()
    previous_card_id = str(previous_document["card_id"] or "") if previous_document else ""
    source_url = str(root / Path(relative))
    conn.execute(
        """
        INSERT INTO knowledge_sources(id, name, kind, url, license_note, fetched_at, status)
        VALUES (?, ?, 'obsidian-readonly', ?, ?, ?, 'active')
        ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url,
            license_note=excluded.license_note, fetched_at=excluded.fetched_at, status='active'
        """,
        (
            source_id, parsed.title, source_url,
            "Local Obsidian note; read-only analysis; content remains local.", now,
        ),
    )
    conn.execute(
        """
        INSERT INTO knowledge_cards
            (id, title, topic, normalized_topic, overview, difficulty, tags,
             created_at, updated_at, archived)
        VALUES (?, ?, ?, ?, ?, 'normal', ?, ?, ?, 0)
        ON CONFLICT(id) DO UPDATE SET title=excluded.title, topic=excluded.topic,
            normalized_topic=excluded.normalized_topic, overview=excluded.overview,
            tags=excluded.tags, updated_at=excluded.updated_at, archived=0
        """,
        (
            card_id, parsed.title, parsed.topic, parsed.topic.strip().lower(), parsed.overview,
            json.dumps(parsed.tags, ensure_ascii=False), now, now,
        ),
    )
    _upsert_document_status(
        conn, root_key, relative, fingerprint, stat, parsed.note_type, "active", "", source_id, card_id
    )
    old_question_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT qa_id FROM knowledge_qa_sources WHERE document_id = ?", (document_id,)
        ).fetchall()
    ]
    conn.execute("DELETE FROM knowledge_qa_sources WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM knowledge_chunks WHERE source_id = ?", (source_id,))

    chunks = parsed.sections or [{"heading": "概览", "content": parsed.overview}]
    for chunk in chunks:
        content = str(chunk.get("content") or "").strip()
        if not content:
            continue
        heading = str(chunk.get("heading") or "知识片段")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        chunk_id = _stable_id("chunk", f"{document_id}\0{heading}\0{content_hash}")
        conn.execute(
            """
            INSERT OR REPLACE INTO knowledge_chunks
                (id, card_id, source_id, heading, content, content_hash, quality_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chunk_id, card_id, source_id, heading, content, content_hash, parsed.quality_score, now),
        )

    question_ids: set[str] = set()
    for question in parsed.questions:
        qa_id = repo.upsert_structured_qa(
            card_id,
            question.to_dict(),
            document_id=document_id,
            quality_score=parsed.quality_score,
            conn=conn,
        )
        question_ids.add(qa_id)
    for qa_id in set(old_question_ids) - question_ids:
        _deactivate_question_if_orphan(conn, qa_id)
    if previous_card_id and previous_card_id != card_id:
        _archive_card_if_orphan(conn, previous_card_id)
    repo._refresh_card_fts(conn, card_id)
    return len(question_ids)


def _upsert_document_status(
    conn,
    root_key: str,
    relative: str,
    fingerprint: str,
    stat,
    note_type: str,
    status: str,
    error: str,
    source_id: str | None,
    card_id: str | None,
) -> str:
    document_id = _stable_id("doc", f"{root_key}\0{relative}")
    conn.execute(
        """
        INSERT INTO knowledge_documents
            (id, source_id, card_id, vault_root, relative_path, content_hash,
             mtime_ns, size, note_type, status, last_indexed_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vault_root, relative_path) DO UPDATE SET
            source_id=excluded.source_id, card_id=excluded.card_id,
            content_hash=excluded.content_hash, mtime_ns=excluded.mtime_ns,
            size=excluded.size, note_type=excluded.note_type, status=excluded.status,
            last_indexed_at=excluded.last_indexed_at, error=excluded.error
        """,
        (
            document_id, source_id, card_id, root_key, relative, fingerprint,
            int(getattr(stat, "st_mtime_ns", 0)), int(getattr(stat, "st_size", 0)),
            note_type, status, _now(), error,
        ),
    )
    return document_id


def _deactivate_document(conn, document_id: str) -> None:
    row = conn.execute(
        "SELECT source_id, card_id FROM knowledge_documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        return
    question_ids = [
        str(item[0])
        for item in conn.execute(
            "SELECT qa_id FROM knowledge_qa_sources WHERE document_id = ?", (document_id,)
        ).fetchall()
    ]
    conn.execute("DELETE FROM knowledge_qa_sources WHERE document_id = ?", (document_id,))
    if row["source_id"]:
        conn.execute("DELETE FROM knowledge_chunks WHERE source_id = ?", (row["source_id"],))
        conn.execute("UPDATE knowledge_sources SET status = 'inactive' WHERE id = ?", (row["source_id"],))
    for qa_id in question_ids:
        _deactivate_question_if_orphan(conn, qa_id)
    card_id = str(row["card_id"] or "")
    if card_id:
        _archive_card_if_orphan(conn, card_id, excluded_document_id=document_id)


def _archive_card_if_orphan(
    conn,
    card_id: str,
    *,
    excluded_document_id: str = "",
) -> None:
    query = "SELECT 1 FROM knowledge_documents WHERE card_id = ? AND status = 'active'"
    params: tuple[str, ...] = (card_id,)
    if excluded_document_id:
        query += " AND id <> ?"
        params += (excluded_document_id,)
    active = conn.execute(query + " LIMIT 1", params).fetchone()
    if active is None:
        conn.execute("UPDATE knowledge_cards SET archived = 1 WHERE id = ?", (card_id,))


def _deactivate_question_if_orphan(conn, qa_id: str) -> None:
    remaining = conn.execute(
        "SELECT 1 FROM knowledge_qa_sources WHERE qa_id = ? LIMIT 1", (qa_id,)
    ).fetchone()
    if remaining is None:
        conn.execute("UPDATE knowledge_qa_pairs SET active = 0 WHERE id = ? AND document_id <> ''", (qa_id,))


def _iter_markdown_files(root: Path) -> Iterable[Path]:
    root = root.resolve()
    bases: list[Path] = []
    if root.name in ALLOWED_SUBDIRECTORIES:
        bases.append(root)
    else:
        bases.extend(root / name for name in ALLOWED_SUBDIRECTORIES if (root / name).is_dir())
    for base in bases:
        for path in sorted(base.rglob("*.md"), key=lambda item: str(item).lower()):
            try:
                relative_parts = path.relative_to(root).parts
                resolved = path.resolve()
            except (OSError, ValueError):
                continue
            if any(part.startswith(".") for part in relative_parts):
                continue
            if _is_sensitive_path(path) or not _is_relative_to(resolved, root):
                continue
            if not resolved.is_file() or resolved.stat().st_size > MAX_NOTE_BYTES:
                continue
            yield path


def _questions_from_note(
    *,
    title: str,
    topic: str,
    declared_topics: list[str],
    note_type: str,
    metadata: dict[str, Any],
    body: str,
    sections: list[tuple[str, str]],
    section_map: dict[str, list[str]],
    key_points: list[str],
    pitfalls: list[str],
    source_label: str,
) -> list[ParsedQuestion]:
    specifications: list[dict[str, Any]] = []
    structured_groups = _structured_question_groups(body)
    if note_type == "algorithm":
        problem = _first_section(section_map, ("题目",))
        if problem:
            specifications.append(
                {
                    "candidate": (f"请口述「{title}」的最优解、复杂度和易错点。", ""),
                    "context": title,
                    "sections": sections,
                    "section_map": section_map,
                    "key_points": key_points,
                    "pitfalls": pitfalls,
                    "follow_ups": [],
                    "allow_generic": True,
                }
            )
    elif structured_groups:
        for group_title, group_sections, group_map in structured_groups:
            candidates = _question_lines(_first_section(group_map, ("题目",)))
            if not candidates:
                candidates = _question_lines(group_title)
            group_points = _unique_items(
                _items_for_headings(group_map, ("考察点", "核心概念", "关键点")) + key_points,
                limit=10,
            )
            group_pitfalls = _unique_items(
                _items_for_headings(group_map, PITFALL_HEADINGS) + pitfalls,
                limit=8,
            )
            group_follow_ups = [
                _ensure_question(_plain_text(question))
                for question, _answer in _question_lines(
                    _first_section(group_map, ("面试官追问", "追问"))
                )
            ]
            for candidate in candidates:
                specifications.append(
                    {
                        "candidate": candidate,
                        "context": f"{group_title}\n{_first_section(group_map, ('题目',))}",
                        "sections": group_sections,
                        "section_map": group_map,
                        "key_points": group_points,
                        "pitfalls": group_pitfalls,
                        "follow_ups": group_follow_ups,
                        "allow_generic": True,
                    }
                )
    else:
        candidates: list[tuple[str, str]] = []
        for heading, content in sections:
            if not _heading_matches(heading, QUESTION_HEADINGS):
                continue
            candidates.extend(_question_lines(content))
        if note_type in {"question", "interview"} and not candidates:
            candidates.extend(_question_lines(title))
        follow_ups = [
            _ensure_question(_plain_text(question))
            for question, _answer in _question_lines(
                _first_section(section_map, ("面试官追问", "追问"))
            )
        ]
        for candidate in candidates:
            specifications.append(
                {
                    "candidate": candidate,
                    "context": title,
                    "sections": sections,
                    "section_map": section_map,
                    "key_points": key_points,
                    "pitfalls": pitfalls,
                    "follow_ups": follow_ups,
                    "allow_generic": len(candidates) == 1,
                }
            )

    question_type = "interview-real" if any(
        metadata.get(key) for key in ("company", "interview_source", "provenance", "source_interview")
    ) else "high-frequency"
    difficulty = str(metadata.get("difficulty") or "normal")
    result: list[ParsedQuestion] = []
    seen: set[str] = set()
    for specification in specifications:
        question, inline_answer = specification["candidate"]
        cleaned_question = _ensure_question(_plain_text(question))
        question_topic = _topic_for_question(
            f"{specification['context']}\n{cleaned_question}",
            declared_topics,
            topic,
        )
        key = canonical_question_key(question_topic, cleaned_question)
        if not cleaned_question or key in seen:
            continue
        answer_summary, answer_detail, answer_points = _answer_for_question(
            cleaned_question,
            inline_answer,
            specification["sections"],
            specification["section_map"],
            specification["key_points"],
            allow_generic_answer=specification["allow_generic"],
        )
        if not answer_summary or not answer_detail:
            continue
        structured = _render_answer(
            answer_summary,
            answer_detail,
            answer_points,
            specification["pitfalls"],
            [item for item in specification["follow_ups"] if item != cleaned_question][:3],
            source_label,
            _engineering_example_for_note(
                specification["section_map"], answer_detail, answer_points
            ),
        )
        result.append(
            ParsedQuestion(
                topic=question_topic,
                question=cleaned_question,
                answer=structured,
                answer_summary=answer_summary,
                answer_detail=answer_detail,
                key_points=answer_points,
                pitfalls=specification["pitfalls"][:6],
                follow_ups=[
                    item for item in specification["follow_ups"] if item != cleaned_question
                ][:3],
                question_type=question_type,
                difficulty=difficulty,
                source_label=source_label,
                canonical_key=key,
            )
        )
        seen.add(key)
    return result


def _structured_question_groups(
    body: str,
) -> list[tuple[str, list[tuple[str, str]], dict[str, list[str]]]]:
    """Split daily-practice notes into independent H2 question blocks."""
    question_heading = re.compile(
        r"^##\s+(?:第\s*(?:[一二三四五六七八九十百]+|\d+)\s*题|题目\s*\d+|Q\s*\d+)"
        r"\s*[:：｜|.\-]?\s*(.*?)\s*$",
        flags=re.IGNORECASE,
    )
    any_h2 = re.compile(r"^##\s+")
    groups: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []
    in_code = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else question_heading.match(line)
        if match:
            if current_title:
                groups.append((current_title, current_lines))
            current_title = match.group(1).strip() or _plain_text(line)
            current_lines = []
            continue
        if current_title and not in_code and any_h2.match(line):
            groups.append((current_title, current_lines))
            current_title = ""
            current_lines = []
            continue
        if current_title:
            current_lines.append(line)
    if current_title:
        groups.append((current_title, current_lines))

    result: list[tuple[str, list[tuple[str, str]], dict[str, list[str]]]] = []
    for group_title, lines in groups:
        group_sections = _markdown_sections("\n".join(lines))
        result.append((group_title, group_sections, _group_sections(group_sections)))
    return result


def _topic_for_question(text: str, declared_topics: list[str], fallback: str) -> str:
    if not declared_topics:
        return fallback
    if len(declared_topics) == 1:
        return declared_topics[0]
    normalized = _normalized_match_text(text)
    scores = {
        topic: normalized.count(_normalized_match_text(topic))
        for topic in declared_topics
        if _normalized_match_text(topic)
    }
    best = max(scores.values(), default=0)
    winners = [topic for topic, score in scores.items() if score == best and score > 0]
    return winners[0] if len(winners) == 1 else fallback


def _answer_for_question(
    question: str,
    inline_answer: str,
    sections: list[tuple[str, str]],
    section_map: dict[str, list[str]],
    generic_points: list[str],
    *,
    allow_generic_answer: bool,
) -> tuple[str, str, list[str]]:
    blocks: list[tuple[float, str, str, int]] = []
    question_tokens = set(_tokens(question))
    for heading, content in sections:
        if _heading_matches(heading, QUESTION_HEADINGS + PITFALL_HEADINGS):
            continue
        plain = _plain_text(content)
        if not plain:
            continue
        overlap = len(question_tokens & set(_tokens(f"{heading} {plain}")))
        bonus = 2 if _heading_matches(heading, ANSWER_HEADINGS) else 0
        blocks.append((overlap + bonus, heading, plain, overlap))
    blocks.sort(key=lambda item: (item[0], len(item[2])), reverse=True)
    relevant_blocks = [block for block in blocks if block[3] > 0]

    summary = _plain_text(inline_answer)
    if not summary:
        answer_template = _first_section(section_map, ("我的回答模板", "面试口述稿"))
        if relevant_blocks:
            summary = _first_sentence(relevant_blocks[0][2])
        elif allow_generic_answer and answer_template:
            summary = _first_sentence(_plain_text(answer_template))
        elif allow_generic_answer and blocks and blocks[0][0] > 0:
            summary = _first_sentence(blocks[0][2])
        elif allow_generic_answer:
            summary = _first_sentence(_plain_text(_first_section(section_map, ("一句话理解",))))
    if not summary:
        return "", "", []

    selected = relevant_blocks if relevant_blocks else blocks if allow_generic_answer or inline_answer else []
    chosen = [block[2] for block in selected[:3] if block[0] > 0]
    if not chosen and (allow_generic_answer or inline_answer):
        fallback = _first_section(section_map, ("小白解释", "核心概念", "解法"))
        if fallback:
            chosen.append(_plain_text(fallback))
    detail = "\n".join(_clip(block, 900) for block in chosen if block)
    if not detail:
        detail = summary
    points = _unique_items(
        _extract_list_items("\n".join(chosen)) + generic_points,
        limit=6,
    )
    if not points:
        points = [_first_sentence(detail)] if detail else []
    return _clip(summary, 360), _clip(detail, 1800), points


def _render_answer(
    summary: str,
    detail: str,
    key_points: list[str],
    pitfalls: list[str],
    follow_ups: list[str],
    source_label: str,
    engineering_example: str,
) -> str:
    parts = [
        f"一句话结论：{summary}",
        f"原理拆解：{detail}",
        f"工程示例：{engineering_example}",
    ]
    if key_points:
        parts.append("回答要点：\n" + "\n".join(f"- {item}" for item in key_points[:6]))
    if pitfalls:
        parts.append("易错点：\n" + "\n".join(f"- {item}" for item in pitfalls[:5]))
    if follow_ups:
        parts.append("面试追问：\n" + "\n".join(f"- {item}" for item in follow_ups[:3]))
    parts.append(f"来源：Obsidian 只读笔记 / {source_label}")
    return "\n\n".join(parts)


def _engineering_example_for_note(
    section_map: dict[str, list[str]],
    answer_detail: str,
    key_points: list[str],
) -> str:
    explicit = _first_section(section_map, ("工程示例", "工程实践", "实战", "示例"))
    if explicit:
        return _clip(_plain_text(explicit), 700)
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?])\s*", answer_detail) if item.strip()]
    if len(sentences) >= 2:
        return _clip(sentences[-1], 700)
    focus = "、".join(key_points[:2]) or "关键行为"
    return f"可用最小代码、日志或测试验证“{focus}”，并记录输入、结果和边界条件。"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}, text
    lines = parts[1].splitlines()
    metadata: dict[str, Any] = {}
    current_list = ""
    for raw in lines:
        line = raw.rstrip()
        item = re.match(r"^\s*-\s+(.+)$", line)
        if item and current_list:
            metadata.setdefault(current_list, []).append(item.group(1).strip().strip("'\""))
            continue
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        current_list = key if not value else ""
        if not value:
            metadata[key] = []
        elif value.startswith("[") and value.endswith("]"):
            metadata[key] = [part.strip().strip("'\"") for part in value[1:-1].split(",") if part.strip()]
        else:
            metadata[key] = value.strip("'\"")
    return metadata, parts[2].strip()


def _metadata_list(metadata: dict[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value:
        return [part.strip() for part in str(value).split(",") if part.strip()]
    return []


def _markdown_sections(body: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading = "正文"
    lines: list[str] = []
    in_code = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else re.match(r"^#{2,4}\s+(.+?)\s*$", line)
        if match:
            if lines and "\n".join(lines).strip():
                sections.append((heading, "\n".join(lines).strip()))
            heading = match.group(1).strip()
            lines = []
        elif not re.match(r"^#\s+", line):
            lines.append(line)
    if lines and "\n".join(lines).strip():
        sections.append((heading, "\n".join(lines).strip()))
    return sections


def _group_sections(sections: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for heading, content in sections:
        grouped.setdefault(heading, []).append(content)
    return grouped


def _first_section(section_map: dict[str, list[str]], names: Iterable[str]) -> str:
    for wanted in names:
        for heading, values in section_map.items():
            if wanted in heading and values:
                return "\n".join(values)
    return ""


def _items_for_headings(section_map: dict[str, list[str]], names: Iterable[str]) -> list[str]:
    items: list[str] = []
    for wanted in names:
        for heading, values in section_map.items():
            if wanted in heading:
                items.extend(_extract_list_items("\n".join(values)))
    return items


def _question_lines(content: str) -> list[tuple[str, str]]:
    questions: list[tuple[str, str]] = []
    for raw in content.splitlines():
        line = raw.strip().strip("|")
        if not line or re.fullmatch(r"[:\-\s|]+", line):
            continue
        line = re.sub(r"^(?:[-*+]\s+|\d+[.)、]\s*|>\s*)", "", line).strip()
        cells = [cell.strip().strip("\"") for cell in line.split("|") if cell.strip()]
        if cells:
            line = cells[0]
        inline_answer = ""
        if "→" in line:
            line, inline_answer = (part.strip() for part in line.split("→", 1))
        quoted = re.search(r"[\"“](.+?)[\"”]", line)
        if quoted:
            line = quoted.group(1)
        if "?" in line or "？" in line or quoted or re.search(r"(什么|为什么|如何|怎么|区别|流程|原理|讲一下|说一下)$", line):
            questions.append((_ensure_question(line), _plain_text(inline_answer)))
    return questions


def _extract_list_items(content: str) -> list[str]:
    items: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        match = re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)(.+)$", line)
        if match:
            cleaned = _plain_text(match.group(1))
            if cleaned:
                items.append(cleaned)
    return items


def _infer_topic(path: Path, title: str, tags: list[str]) -> str:
    candidates = tags + list(reversed(path.parts)) + [title]
    for candidate in candidates:
        lower = str(candidate).lower()
        for alias, canonical in TOPIC_ALIASES.items():
            if alias in lower:
                return canonical
    if "05-Interview" in path.parts:
        return "算法设计与分析" if "算法" in str(path) else "Java 后端基础"
    return "计算机基础"


def _title_from_body(body: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    return match.group(1).strip() if match else ""


def _first_prose(body: str) -> str:
    chunks: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "---", "```", "|")):
            continue
        cleaned = _plain_text(line)
        if cleaned:
            chunks.append(cleaned)
        if sum(len(item) for item in chunks) >= 240:
            break
    return " ".join(chunks)


def _plain_text(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
    cleaned = re.sub(r"!\[([^]]*)]\([^)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"\[\[([^]|]+)(?:\|([^]]+))?]]", lambda m: m.group(2) or m.group(1), cleaned)
    cleaned = re.sub(r"[*_`>#]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -|\n\t")


def _first_sentence(text: str) -> str:
    cleaned = _plain_text(text)
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[。！？!?])\s*", cleaned, maxsplit=1)
    return parts[0].strip()


def _tokens(text: str) -> list[str]:
    lower = text.lower()
    latin = re.findall(r"[a-z][a-z0-9+.#/-]{1,}", lower)
    chinese_runs = re.findall(r"[\u4e00-\u9fff]{2,}", lower)
    chinese: list[str] = []
    for run in chinese_runs:
        chinese.extend(run[index:index + 2] for index in range(max(1, len(run) - 1)))
    stop = {"什么", "为什么", "如何", "怎么", "区别", "一下", "讲一", "说一", "面试"}
    return [token for token in latin + chinese if token not in stop]


def _normalized_match_text(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())


def _heading_matches(heading: str, names: Iterable[str]) -> bool:
    return any(name in heading for name in names)


def _ensure_question(text: str) -> str:
    cleaned = text.strip().strip("\"“”'")
    return cleaned if "?" in cleaned or "？" in cleaned else cleaned + "？"


def _unique_items(items: Iterable[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = _plain_text(str(item))
        key = _normalized_match_text(cleaned)
        if not cleaned or not key or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
