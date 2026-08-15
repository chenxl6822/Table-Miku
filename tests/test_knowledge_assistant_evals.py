from __future__ import annotations

import json
from pathlib import Path

from evals.run_knowledge_assistant_evals import (
    GOLD_THRESHOLDS,
    THRESHOLDS,
    load_jsonl,
    main,
    run_ab_evaluation,
    run_evaluation,
)
from table_miku.knowledge_assistant.embeddings import BowEmbedding, HashingEmbedding


def test_offline_knowledge_assistant_quality_gate_passes(tmp_path: Path):
    payload = run_evaluation(
        load_jsonl(Path("evals/knowledge_assistant_corpus.jsonl")),
        load_jsonl(Path("evals/knowledge_assistant_cases.jsonl")),
        database_path=tmp_path / "assistant.db",
    )

    assert payload["summary"] == {
        "total": 8,
        "passed": 8,
        "failed": 0,
        "quality_gate_passed": True,
    }
    assert payload["real_model_api_called"] is False
    assert payload["embedding_model"] == "local-hash-v1-384"
    assert all(payload["metrics"][name] >= threshold for name, threshold in THRESHOLDS.items())
    assert all(payload["threshold_checks"].values())


def test_offline_eval_cli_writes_auditable_result(tmp_path: Path):
    output = tmp_path / "result.json"

    assert main(["--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"]["failed"] == 0
    assert payload["summary"]["quality_gate_passed"] is True
    assert len(payload["results"]) == 8


def test_gold_suite_reports_faithfulness_and_conflict_metrics(tmp_path: Path):
    payload = run_evaluation(
        load_jsonl(Path("evals/knowledge_assistant_gold_corpus.jsonl")),
        load_jsonl(Path("evals/knowledge_assistant_gold_cases.jsonl")),
        database_path=tmp_path / "gold.db",
        embedding=HashingEmbedding(),
        thresholds=GOLD_THRESHOLDS,
    )
    assert payload["summary"]["total"] == 11
    assert "citation_faithfulness" in payload["metrics"]
    assert "conflict_pair_handling" in payload["metrics"]
    assert payload["real_model_api_called"] is False


def test_ab_compare_hash_and_bow_without_neural_deps(tmp_path: Path):
    payload = run_ab_evaluation(
        load_jsonl(Path("evals/knowledge_assistant_corpus.jsonl")),
        load_jsonl(Path("evals/knowledge_assistant_cases.jsonl")),
        providers=("hash", "bow"),
        database_dir=tmp_path,
        thresholds=THRESHOLDS,
    )
    assert payload["mode"] == "ab"
    assert [item["provider"] for item in payload["comparisons"]] == ["hash", "bow"]
    assert payload["comparisons"][0]["embedding_model"] == "local-hash-v1-384"
    assert payload["comparisons"][1]["embedding_model"] == "local-bow-v1-384"
    assert all(item["summary"]["quality_gate_passed"] for item in payload["comparisons"])


def test_service_accepts_injected_bow_embedding(tmp_path: Path):
    from table_miku.knowledge_assistant import KnowledgeAssistantService, Principal

    service = KnowledgeAssistantService(tmp_path / "bow.db", embedding=BowEmbedding())
    principal = Principal("t", "u", frozenset({"editor"}))
    service.documents.upload(
        principal,
        filename="note.md",
        content=b"awaiting_approval operation_id",
        collection_id="default",
        idempotency_key="bow-doc-1",
    )
    assert service.embedding.name == "local-bow-v1-384"
    assert service.health()["embedding_model"] == "local-bow-v1-384"
