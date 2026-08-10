from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from table_miku.knowledge_assistant import KnowledgeAssistantService, Principal  # noqa: E402


EVAL_ROOT = Path(__file__).resolve().parent
DEFAULT_CORPUS = EVAL_ROOT / "knowledge_assistant_corpus.jsonl"
DEFAULT_CASES = EVAL_ROOT / "knowledge_assistant_cases.jsonl"
DEFAULT_OUTPUT = EVAL_ROOT / "results" / "knowledge_assistant_latest.json"
THRESHOLDS = {
    "retrieval_recall": 0.95,
    "first_citation_accuracy": 0.85,
    "refusal_accuracy": 1.0,
    "citation_coverage": 1.0,
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        cleaned = line.strip()
        if not cleaned:
            continue
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise ValueError(f"invalid JSON object at {path}:{line_number}")
        records.append(payload)
    return records


def run_evaluation(
    corpus: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    *,
    database_path: Path,
) -> dict[str, Any]:
    service = KnowledgeAssistantService(database_path)
    principal = Principal("offline-eval", "eval-runner", frozenset({"editor"}))
    for index, document in enumerate(corpus, start=1):
        service.documents.upload(
            principal,
            filename=str(document["filename"]),
            content=str(document["content"]).encode("utf-8"),
            collection_id=str(document.get("collection_id", "default")),
            idempotency_key=f"offline-eval-document-{index:04d}",
        )

    results: list[dict[str, Any]] = []
    for case in cases:
        response = service.rag.query(principal, str(case["query"]), top_k=5)
        filenames = [str(item["filename"]) for item in response["citations"]]
        should_refuse = bool(case.get("should_refuse"))
        expected = case.get("expected_filename")
        refusal_correct = bool(response["refused"]) is should_refuse
        retrieval_hit = bool(should_refuse or expected in filenames)
        first_citation_correct = bool(should_refuse or (filenames and filenames[0] == expected))
        citation_covered = bool(should_refuse or (not response["refused"] and filenames))
        passed = refusal_correct and retrieval_hit and citation_covered
        results.append(
            {
                "name": str(case["name"]),
                "passed": passed,
                "should_refuse": should_refuse,
                "refused": bool(response["refused"]),
                "expected_filename": expected,
                "citation_filenames": filenames,
                "top_score": response["retrieval"]["top_score"],
                "checks": {
                    "refusal_correct": refusal_correct,
                    "retrieval_hit": retrieval_hit,
                    "first_citation_correct": first_citation_correct,
                    "citation_covered": citation_covered,
                },
            }
        )

    answerable = [result for result in results if not result["should_refuse"]]
    metrics = {
        "retrieval_recall": _ratio(answerable, "retrieval_hit"),
        "first_citation_accuracy": _ratio(answerable, "first_citation_correct"),
        "refusal_accuracy": _ratio(results, "refusal_correct"),
        "citation_coverage": _ratio(answerable, "citation_covered"),
    }
    threshold_checks = {
        name: metrics[name] >= threshold for name, threshold in THRESHOLDS.items()
    }
    return {
        "synthetic": True,
        "real_model_api_called": False,
        "embedding_model": service.embedding.name,
        "summary": {
            "total": len(results),
            "passed": sum(int(result["passed"]) for result in results),
            "failed": sum(int(not result["passed"]) for result in results),
            "quality_gate_passed": all(threshold_checks.values()),
        },
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "threshold_checks": threshold_checks,
        "results": results,
    }


def _ratio(records: list[dict[str, Any]], check: str) -> float:
    if not records:
        return 1.0
    return round(sum(int(record["checks"][check]) for record in records) / len(records), 6)


def write_results(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline Knowledge Assistant 2.0 quality evals")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="table-miku-ka2-eval-") as directory:
        payload = run_evaluation(
            load_jsonl(args.corpus),
            load_jsonl(args.cases),
            database_path=Path(directory) / "assistant.db",
        )
    write_results(payload, args.output)
    summary = payload["summary"]
    print(
        f"Knowledge Assistant offline evals: {summary['passed']}/{summary['total']} cases passed; "
        f"quality_gate={summary['quality_gate_passed']}; real model API not called."
    )
    return 0 if summary["failed"] == 0 and summary["quality_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
