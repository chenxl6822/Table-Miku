from __future__ import annotations

import json
from pathlib import Path

from table_miku.knowledge_assistant.auth import Principal
from table_miku.knowledge_assistant_collection_mru import (
    MAX_COLLECTION_MRU,
    CollectionMruStore,
    collection_suggestions,
)


def _editor(*, tenant: str = "tenant-a", user: str = "editor-1", collections=None) -> Principal:
    return Principal(tenant, user, frozenset({"editor"}), collections)


def test_suggestions_unrestricted_include_default_and_mru_order():
    remembered = ["engineering", "ops"]
    assert collection_suggestions(_editor(), remembered) == ["engineering", "ops", "default"]


def test_suggestions_restricted_are_allowlist_with_mru_first():
    principal = _editor(collections=frozenset({"ops", "engineering", "legal"}))
    assert collection_suggestions(principal, ["legal", "missing"]) == [
        "legal",
        "engineering",
        "ops",
    ]


def test_suggestions_deny_all_are_empty():
    assert collection_suggestions(_editor(collections=frozenset()), ["engineering"]) == []


def test_store_is_isolated_by_tenant_and_user(tmp_path: Path):
    store = CollectionMruStore(tmp_path / "collection_mru.json")
    first = _editor()
    other_user = _editor(user="editor-2")
    other_tenant = _editor(tenant="tenant-b")
    store.remember(first, "engineering")
    store.remember(other_user, "hr")
    store.remember(other_tenant, "finance")
    assert store.suggestions(first)[0] == "engineering"
    assert "hr" not in store.suggestions(first)
    assert "finance" not in store.suggestions(first)
    assert store.suggestions(other_user)[0] == "hr"


def test_store_caps_and_moves_latest_to_front(tmp_path: Path):
    store = CollectionMruStore(tmp_path / "collection_mru.json")
    principal = _editor()
    for index in range(MAX_COLLECTION_MRU + 2):
        store.remember(principal, f"col-{index:02d}")
    suggestions = [item for item in store.suggestions(principal) if item != "default"]
    assert suggestions[0] == f"col-{MAX_COLLECTION_MRU + 1:02d}"
    assert len(suggestions) == MAX_COLLECTION_MRU
    store.remember(principal, "col-02")
    assert store.suggestions(principal)[0] == "col-02"


def test_store_ignores_invalid_and_out_of_scope_ids(tmp_path: Path):
    store = CollectionMruStore(tmp_path / "collection_mru.json")
    restricted = _editor(collections=frozenset({"engineering"}))
    store.remember(restricted, "engineering")
    store.remember(restricted, "secret")
    store.remember(restricted, "bad id")
    assert store.suggestions(restricted) == ["engineering"]


def test_store_corrupt_file_fails_open(tmp_path: Path):
    path = tmp_path / "collection_mru.json"
    path.write_text("{not-json", encoding="utf-8")
    store = CollectionMruStore(path)
    assert store.suggestions(_editor()) == ["default"]
    store.remember(_editor(), "engineering")
    assert "engineering" in store.suggestions(_editor())
