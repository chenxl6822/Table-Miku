from __future__ import annotations

import json
from pathlib import Path

from evals.run_knowledge_assistant_evals import (
    THRESHOLDS,
    load_jsonl,
    main,
    run_evaluation,
)


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
