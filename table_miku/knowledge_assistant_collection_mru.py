from __future__ import annotations

import json
import re
from pathlib import Path

from .knowledge_assistant.auth import Principal
from .paths import user_data_dir

MAX_COLLECTION_MRU = 8
_COLLECTION_ID_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def collection_suggestions(principal: Principal, remembered: list[str]) -> list[str]:
    if principal.collection_ids is not None and not principal.collection_ids:
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in remembered:
        if item in seen:
            continue
        if principal.collection_ids is not None and item not in principal.collection_ids:
            continue
        ordered.append(item)
        seen.add(item)
    if principal.collection_ids is not None:
        for item in sorted(principal.collection_ids):
            if item not in seen:
                ordered.append(item)
        return ordered
    if "default" not in seen:
        ordered.append("default")
    return ordered


class CollectionMruStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else user_data_dir() / "knowledge_assistant" / "collection_mru.json"

    def suggestions(self, principal: Principal) -> list[str]:
        return collection_suggestions(principal, self._load_for(principal))

    def remember(self, principal: Principal, collection_id: str) -> None:
        try:
            cleaned = _normalize_collection_id(collection_id)
        except ValueError:
            return
        if not principal.can_access_collection(cleaned):
            return
        payload = self._read_payload()
        identities = payload.setdefault("by_identity", {})
        key = _identity_key(principal)
        current = [
            item
            for item in identities.get(key, [])
            if isinstance(item, str) and item != cleaned
        ]
        identities[key] = [cleaned, *current][:MAX_COLLECTION_MRU]
        payload["version"] = 1
        self._write_payload(payload)

    def _load_for(self, principal: Principal) -> list[str]:
        values = self._read_payload().get("by_identity", {}).get(_identity_key(principal), [])
        remembered: list[str] = []
        for item in values:
            if not isinstance(item, str):
                continue
            try:
                remembered.append(_normalize_collection_id(item))
            except ValueError:
                continue
        return remembered

    def _read_payload(self) -> dict[str, object]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {"version": 1, "by_identity": {}}
        if not isinstance(raw, dict):
            return {"version": 1, "by_identity": {}}
        identities = raw.get("by_identity")
        if not isinstance(identities, dict):
            return {"version": 1, "by_identity": {}}
        return {"version": 1, "by_identity": identities}

    def _write_payload(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def _identity_key(principal: Principal) -> str:
    return f"{principal.tenant_id}\x1f{principal.user_id}"


def _normalize_collection_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 120 or _COLLECTION_ID_RE.fullmatch(cleaned) is None:
        raise ValueError("collection_id is invalid")
    return cleaned
