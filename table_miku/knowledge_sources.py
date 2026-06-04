"""Multi-source adapter interface for knowledge ingestion.

Defines the ``KnowledgeSource`` abstract base and includes a Wikipedia adapter
migrated from ``knowledge_base.py``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from .encoding_utils import repair_mojibake, normalize_zh_text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIKIPEDIA_API = "https://zh.wikipedia.org/w/api.php"
WIKIPEDIA_REST_SUMMARY = "https://zh.wikipedia.org/api/rest_v1/page/summary/"
EN_WIKIPEDIA_REST = "https://en.wikipedia.org/api/rest_v1/page/summary/"
USER_AGENT = "Table-Miku/0.2 (knowledge engine)"

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class KnowledgeSource(ABC):
    """Protocol for knowledge sources.

    Subclasses must provide ``name`` and ``kind`` and implement ``search`` and
    ``fetch``.
    """

    name: str
    kind: str

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return a list of search-result dicts with at least::

            {"title": str, "url": str, "snippet": str}
        """
        ...

    @abstractmethod
    def fetch(self, ref: dict[str, Any]) -> dict[str, Any]:
        """Given a search-result ref, return a full record dict with at least::

            {"title": str, "url": str, "overview": str, "sections": [...],
             "key_points": [...], "raw": str}
        """
        ...


# ---------------------------------------------------------------------------
# Wikipedia adapter
# ---------------------------------------------------------------------------


class WikipediaSource(KnowledgeSource):
    """Knowledge source backed by Wikipedia (zh primary, en fallback)."""

    name = "Wikipedia"
    kind = "wikipedia"

    def __init__(self, language: str = "zh") -> None:
        self._language = language

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search Wikipedia for *query*."""
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": str(limit),
            "format": "json",
            "formatversion": "2",
        }
        api_url = (
            WIKIPEDIA_API if self._language == "zh"
            else f"https://{self._language}.wikipedia.org/w/api.php"
        )
        try:
            payload = _get_json(api_url + "?" + urllib.parse.urlencode(params))
        except Exception:
            return []

        results: list[dict[str, Any]] = []
        for item in (payload.get("query") or {}).get("search") or []:
            results.append({
                "title": str(item.get("title", "")),
                "url": f"https://{self._language}.wikipedia.org/wiki/{urllib.parse.quote(str(item.get('title', '')))}",
                "snippet": str(item.get("snippet", "")),
            })
        return results[:limit]

    def fetch(self, ref: dict[str, Any]) -> dict[str, Any]:
        """Fetch full Wikipedia page content for *ref* (must have 'title')."""
        title = str(ref.get("title", ""))
        if not title:
            raise ValueError("ref must include 'title'")

        # Try zh Wikipedia MediaWiki API
        try:
            page = _fetch_mediawiki_extract(title)
            return {
                "title": title,
                "url": page["url"],
                "overview": page["extract"][:520],
                "raw": page["extract"],
                "source_kind": "wikipedia-zh",
            }
        except Exception:
            pass

        # Try zh Wikipedia REST summary
        try:
            page = _fetch_rest_summary(title)
            return {
                "title": title,
                "url": page["url"],
                "overview": page["extract"][:520],
                "raw": page["extract"],
                "source_kind": "wikipedia-zh",
            }
        except Exception:
            pass

        # Try English Wikipedia
        try:
            page = _fetch_en_summary(title)
            return {
                "title": title,
                "url": page["url"],
                "overview": page["extract"][:520],
                "raw": page["extract"],
                "source_kind": "wikipedia-en",
            }
        except Exception:
            pass

        raise RuntimeError(f"Failed to fetch Wikipedia page for {title}")


# ---------------------------------------------------------------------------
# Internal Wikipedia helpers
# ---------------------------------------------------------------------------


def _fetch_mediawiki_extract(title: str) -> dict[str, str]:
    params = {
        "action": "query",
        "prop": "extracts|info",
        "explaintext": "1",
        "redirects": "1",
        "format": "json",
        "formatversion": "2",
        "utf8": "1",
        "variant": "zh-hans",
        "inprop": "url",
        "titles": title,
    }
    payload = _get_json(WIKIPEDIA_API + "?" + urllib.parse.urlencode(params))
    pages = ((payload.get("query") or {}).get("pages") or [])
    if not pages:
        raise KeyError("pages")
    page = pages[0]
    extract = normalize_zh_text(str(page.get("extract") or ""))
    if not extract:
        raise KeyError("extract")
    return {"extract": extract, "url": str(page.get("fullurl") or "")}


def _fetch_rest_summary(title: str) -> dict[str, str]:
    payload = _get_json(WIKIPEDIA_REST_SUMMARY + urllib.parse.quote(title))
    extract = normalize_zh_text(str(payload.get("extract") or ""))
    if not extract:
        raise KeyError("extract")
    page_url = ((payload.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    return {"extract": extract, "url": str(page_url)}


def _fetch_en_summary(title: str) -> dict[str, str]:
    from .knowledge_base import TOPIC_ALIASES
    alias = TOPIC_ALIASES.get(title, title.replace(" ", "_"))
    payload = _get_json(EN_WIKIPEDIA_REST + urllib.parse.quote(alias, safe=""))
    extract = str(payload.get("extract") or "")
    if not extract or len(extract) < 60:
        raise KeyError("extract too short")
    page_url = ((payload.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    return {"extract": extract, "url": str(page_url)}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json; charset=utf-8",
            "Accept-Language": "zh-Hans-CN, zh-CN, zh, en",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_BUILTIN_SOURCES: dict[str, KnowledgeSource] = {}


def register_source(source: KnowledgeSource) -> None:
    """Register a knowledge source by name."""
    _BUILTIN_SOURCES[source.name] = source


def get_source(name: str) -> KnowledgeSource | None:
    return _BUILTIN_SOURCES.get(name)


def list_sources() -> list[str]:
    return list(_BUILTIN_SOURCES.keys())


# Auto-register built-in sources
register_source(WikipediaSource("zh"))
