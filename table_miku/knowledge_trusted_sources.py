"""Trusted knowledge source adapters.

The adapters in this module do not mutate source material.  The Obsidian
adapter is deliberately read-only and skips hidden/sensitive paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

TRUST_RANK: dict[str, int] = {
    "official": 100,
    "standard": 95,
    "rfc": 95,
    "paper": 90,
    "obsidian-readonly": 85,
    "wikipedia": 55,
    "offline": 40,
}

SENSITIVE_NAME_RE = re.compile(
    r"(^\.env\b|token|secret|password|passwd|credential|apikey|api[-_]?key|\bkey\b|密钥|令牌|凭据|授权码)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceMetadata:
    name: str
    kind: str
    url: str = ""
    trust_level: int = 50
    access_date: str = ""
    license_note: str = ""
    freshness_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "trust_level": self.trust_level,
            "access_date": self.access_date or date.today().isoformat(),
            "license_note": self.license_note,
            "freshness_note": self.freshness_note,
        }


def source_priority(kind: str) -> int:
    return TRUST_RANK.get(kind, 50)


def trusted_metadata_for_topic(topic: str) -> list[SourceMetadata]:
    """Return official/standard/paper metadata hints for a topic."""
    today = date.today().isoformat()
    hints: dict[str, list[SourceMetadata]] = {
        "计算机网络": [
            SourceMetadata("RFC 9110 HTTP Semantics", "rfc", "https://www.rfc-editor.org/rfc/rfc9110", 95, today, "RFC; store metadata and summaries only."),
            SourceMetadata("RFC 9293 Transmission Control Protocol", "rfc", "https://www.rfc-editor.org/rfc/rfc9293", 95, today, "RFC; store metadata and summaries only."),
            SourceMetadata("MDN HTTP Documentation", "official", "https://developer.mozilla.org/en-US/docs/Web/HTTP", 100, today, "Official documentation; store summary/link only."),
        ],
        "数据库原理": [
            SourceMetadata("PostgreSQL Documentation", "official", "https://www.postgresql.org/docs/", 100, today, "Official documentation; store summary/link only."),
            SourceMetadata("MySQL Documentation", "official", "https://dev.mysql.com/doc/", 100, today, "Official documentation; store summary/link only."),
        ],
        "Java 后端基础": [
            SourceMetadata("Oracle Java Documentation", "official", "https://docs.oracle.com/en/java/", 100, today, "Official documentation; store summary/link only."),
            SourceMetadata("Spring Framework Reference", "official", "https://docs.spring.io/spring-framework/reference/", 100, today, "Official documentation; store summary/link only."),
        ],
        "Go 后端基础": [
            SourceMetadata("The Go Programming Language Documentation", "official", "https://go.dev/doc/", 100, today, "Official documentation; store summary/link only."),
            SourceMetadata("Effective Go", "official", "https://go.dev/doc/effective_go", 100, today, "Official documentation; store summary/link only."),
        ],
        "算法设计与分析": [
            SourceMetadata("Introduction to Algorithms reference topic", "paper", "https://mitpress.mit.edu/9780262046305/introduction-to-algorithms/", 90, today, "Bibliographic metadata only."),
        ],
        "分布式系统": [
            SourceMetadata("Google MapReduce Paper", "paper", "https://research.google/pubs/mapreduce-simplified-data-processing-on-large-clusters/", 90, today, "Research paper metadata/summary only."),
            SourceMetadata("The Raft Consensus Algorithm", "paper", "https://raft.github.io/", 90, today, "Research paper metadata/summary only."),
        ],
    }
    return sorted(hints.get(topic, []), key=lambda item: item.trust_level, reverse=True)


class ObsidianReadOnlySource:
    name = "Obsidian Vault"
    kind = "obsidian-readonly"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        cleaned = query.strip().lower()
        if not cleaned or not self.root.exists():
            return []

        results: list[dict[str, Any]] = []
        for path in self._iter_markdown_files():
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            haystack = f"{path.stem}\n{text}".lower()
            if cleaned not in haystack:
                continue
            results.append({
                "title": _title_from_markdown(path, text),
                "url": str(path),
                "path": str(path),
                "snippet": _snippet(text, query),
                "source_kind": self.kind,
                "trust_level": source_priority(self.kind),
            })
            if len(results) >= limit:
                break
        return results

    def fetch(self, ref: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(ref.get("path") or ref.get("url") or "")).resolve()
        if not _is_relative_to(path, self.root):
            raise ValueError("Obsidian ref is outside the configured read-only root")
        if _is_sensitive_path(path):
            raise ValueError("Sensitive Obsidian path is not readable")
        text = path.read_text(encoding="utf-8-sig")
        body = _strip_frontmatter(text)
        return {
            "title": _title_from_markdown(path, text),
            "url": str(path),
            "path": str(path),
            "overview": _clip(_plain_text(body), 520),
            "sections": _sections_from_markdown(body),
            "raw": body,
            "source_kind": self.kind,
            "metadata": SourceMetadata(
                name=path.stem,
                kind=self.kind,
                url=str(path),
                trust_level=source_priority(self.kind),
                access_date=date.today().isoformat(),
                license_note="Local Obsidian note, read-only import.",
                freshness_note="May reflect the user's local learning state.",
            ).to_dict(),
        }

    def _iter_markdown_files(self):
        for path in self.root.rglob("*.md"):
            if _is_sensitive_path(path):
                continue
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            yield path


def _is_sensitive_path(path: Path) -> bool:
    return any(SENSITIVE_NAME_RE.search(part) for part in path.parts)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def _title_from_markdown(path: Path, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem


def _sections_from_markdown(text: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_heading and current_lines:
                sections.append({"heading": current_heading, "content": _clip(_plain_text("\n".join(current_lines)), 240)})
            current_heading = line[3:].strip()
            current_lines = []
        elif current_heading:
            current_lines.append(line)
        if len(sections) >= 6:
            break
    if current_heading and current_lines and len(sections) < 6:
        sections.append({"heading": current_heading, "content": _clip(_plain_text("\n".join(current_lines)), 240)})
    return [section for section in sections if section["content"]]


def _plain_text(text: str) -> str:
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[[^\]]+\]\([^)]*\)", "", text)
    return " ".join(text.split())


def _snippet(text: str, query: str, limit: int = 160) -> str:
    plain = _plain_text(_strip_frontmatter(text))
    idx = plain.lower().find(query.lower())
    if idx < 0:
        return _clip(plain, limit)
    start = max(0, idx - 40)
    end = min(len(plain), idx + len(query) + 90)
    prefix = "..." if start else ""
    suffix = "..." if end < len(plain) else ""
    return prefix + plain[start:end] + suffix


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join(str(text).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"
