import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_db, knowledge_ingest, knowledge_repository as repo
from table_miku.knowledge_trusted_sources import (
    ObsidianReadOnlySource,
    source_priority,
    trusted_metadata_for_topic,
)


def test_source_priority_orders_trusted_sources():
    assert source_priority("official") > source_priority("obsidian-readonly")
    assert source_priority("rfc") > source_priority("wikipedia")
    assert source_priority("wikipedia") > source_priority("offline")


def test_trusted_metadata_for_topic_includes_official_sources():
    sources = trusted_metadata_for_topic("计算机网络")

    assert any(source.kind == "rfc" for source in sources)
    assert any(source.kind == "official" for source in sources)
    assert sources[0].trust_level >= sources[-1].trust_level


def test_obsidian_source_search_is_read_only_and_skips_sensitive_files(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    note = root / "TCP三次握手.md"
    note.write_text("# TCP三次握手\n\n## 核心概念\n\n计算机网络里 TCP 建连需要三次握手。", encoding="utf-8")
    secret = root / "api_key_notes.md"
    secret.write_text("# secret\n\n计算机网络 secret token", encoding="utf-8")
    before = note.read_text(encoding="utf-8")

    source = ObsidianReadOnlySource(root)
    results = source.search("计算机网络", limit=10)

    assert len(results) == 1
    assert results[0]["title"] == "TCP三次握手"
    assert note.read_text(encoding="utf-8") == before


def test_obsidian_source_rejects_outside_root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# 计算机网络", encoding="utf-8")

    source = ObsidianReadOnlySource(root)
    try:
        source.fetch({"path": str(outside)})
        assert False, "Should reject paths outside root"
    except ValueError:
        pass


def test_ingest_trusted_topic_adds_sources_and_chunks(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "MOC-计算机网络.md"
    note.write_text(
        "# MOC-计算机网络\n\n## 核心知识点\n\n计算机网络包括 TCP、HTTP、DNS 和拥塞控制。",
        encoding="utf-8",
    )
    before = note.read_text(encoding="utf-8")

    summary = knowledge_ingest.ingest_trusted_topic("计算机网络", obsidian_root=vault)
    card = repo.get_card(summary["card_id"])

    assert summary["official_sources"] >= 1
    assert summary["obsidian_sources"] == 1
    assert summary["chunks"] >= 2
    assert card is not None
    assert card["source_count"] >= 2
    assert note.read_text(encoding="utf-8") == before


def _use_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trusted_knowledge.db"
    monkeypatch.setattr(knowledge_db, "knowledge_db_path", lambda: db_path)
    conn = knowledge_db.connect()
    try:
        knowledge_db.init_db(conn)
    finally:
        conn.close()
