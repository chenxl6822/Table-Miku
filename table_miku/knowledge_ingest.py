"""Trusted knowledge ingestion into the SQLite repository."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import knowledge_db
from .knowledge_base import _fallback_card
from .knowledge_repository import _connect, add_chunk, add_source, ensure_review_state, upsert_card
from .knowledge_trusted_sources import (
    ObsidianReadOnlySource,
    SourceMetadata,
    source_priority,
    trusted_metadata_for_topic,
)


def ingest_trusted_topic(
    topic: str,
    *,
    obsidian_root: str | Path | None = None,
    obsidian_limit: int = 3,
) -> dict[str, Any]:
    """Ingest trusted metadata and optional read-only Obsidian notes for *topic*."""
    knowledge_db.init_db()
    card = _fallback_card(topic)
    card.setdefault("tags", [])
    if "trusted-source" not in card["tags"]:
        card["tags"].append("trusted-source")
    card_id = upsert_card(card)

    official_count = 0
    chunk_count = 0
    for metadata in trusted_metadata_for_topic(topic):
        source_id = _upsert_source_metadata(metadata)
        add_chunk(
            card_id,
            source_id,
            {
                "heading": f"可信来源：{metadata.name}",
                "content": _metadata_chunk_content(metadata),
                "quality_score": metadata.trust_level / 100,
            },
        )
        official_count += 1
        chunk_count += 1

    obsidian_count = 0
    if obsidian_root:
        obsidian = ObsidianReadOnlySource(obsidian_root)
        for result in obsidian.search(topic, limit=obsidian_limit):
            record = obsidian.fetch(result)
            metadata = SourceMetadata(
                name=str(record.get("title") or result.get("title") or topic),
                kind="obsidian-readonly",
                url=str(record.get("path") or record.get("url") or ""),
                trust_level=source_priority("obsidian-readonly"),
                license_note="Local Obsidian note, read-only import.",
                freshness_note="User-maintained local learning note.",
            )
            source_id = _upsert_source_metadata(metadata)
            overview = str(record.get("overview") or "")
            if overview:
                add_chunk(
                    card_id,
                    source_id,
                    {
                        "heading": f"Obsidian：{metadata.name}",
                        "content": overview,
                        "quality_score": metadata.trust_level / 100,
                    },
                )
                chunk_count += 1
            obsidian_count += 1

    conn = _connect()
    try:
        ensure_review_state(conn, card_id)
        conn.commit()
    finally:
        conn.close()

    return {
        "topic": topic,
        "card_id": card_id,
        "official_sources": official_count,
        "obsidian_sources": obsidian_count,
        "chunks": chunk_count,
    }


def ingest_trusted_topics(
    topics: list[str],
    *,
    obsidian_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    return [ingest_trusted_topic(topic, obsidian_root=obsidian_root) for topic in topics]


def _upsert_source_metadata(metadata: SourceMetadata) -> str:
    payload = metadata.to_dict()
    return add_source({
        "id": _source_id(payload["kind"], payload["url"] or payload["name"]),
        "name": payload["name"],
        "kind": payload["kind"],
        "url": payload["url"],
        "license_note": (
            f"trust={payload['trust_level']}; access_date={payload['access_date']}; "
            f"{payload['license_note']} {payload['freshness_note']}"
        ).strip(),
        "fetched_at": payload["access_date"],
        "status": "active",
    })


def _metadata_chunk_content(metadata: SourceMetadata) -> str:
    payload = metadata.to_dict()
    parts = [
        f"{payload['name']} 是 {payload['kind']} 来源。",
        f"可信度 {payload['trust_level']}。",
    ]
    if payload["url"]:
        parts.append(f"链接：{payload['url']}。")
    if payload["freshness_note"]:
        parts.append(payload["freshness_note"])
    return " ".join(parts)


def _source_id(kind: str, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}:{identity}".encode("utf-8")).hexdigest()[:16]
    return f"src-{digest}"
