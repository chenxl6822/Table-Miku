from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from table_miku.knowledge_assistant import KnowledgeAssistantService, Principal  # noqa: E402
from table_miku.knowledge_assistant.embeddings import (  # noqa: E402
    EmbeddingProvider,
    create_embedding,
)


EVAL_ROOT = Path(__file__).resolve().parent
DEFAULT_CORPUS = EVAL_ROOT / "knowledge_assistant_corpus.jsonl"
DEFAULT_CASES = EVAL_ROOT / "knowledge_assistant_cases.jsonl"
GOLD_CORPUS = EVAL_ROOT / "knowledge_assistant_gold_corpus.jsonl"
GOLD_CASES = EVAL_ROOT / "knowledge_assistant_gold_cases.jsonl"
DEFAULT_OUTPUT = EVAL_ROOT / "results" / "knowledge_assistant_latest.json"
THRESHOLDS = {
    "retrieval_recall": 0.95,
    "first_citation_accuracy": 0.85,
    "refusal_accuracy": 1.0,
    "citation_coverage": 1.0,
}
GOLD_THRESHOLDS = {
    **THRESHOLDS,
    "citation_faithfulness": 0.85,
    "conflict_pair_handling": 0.5,
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


def _citation_text(response: dict[str, Any]) -> str:
    parts = [str(response.get("answer") or "")]
    for item in response.get("citations") or []:
        parts.append(str(item.get("excerpt") or ""))
        parts.append(str(item.get("filename") or ""))
    return "\n".join(parts)


def _faithfulness_ok(case: dict[str, Any], response: dict[str, Any]) -> bool:
    phrases = case.get("required_phrases") or []
    if not phrases:
        return True
    if response.get("refused"):
        return bool(case.get("should_refuse"))
    haystack = _citation_text(response)
    return all(str(phrase) in haystack for phrase in phrases)


def _conflict_ok(case: dict[str, Any], response: dict[str, Any]) -> bool:
    conflict_filenames = case.get("conflict_filenames") or []
    if not conflict_filenames:
        return True
    cited = {str(item.get("filename") or "") for item in response.get("citations") or []}
    if response.get("refused"):
        return True
    return all(str(name) in cited for name in conflict_filenames)


def evaluate_case(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    filenames = [str(item["filename"]) for item in response["citations"]]
    should_refuse = bool(case.get("should_refuse"))
    expected = case.get("expected_filename")
    conflict_filenames = case.get("conflict_filenames") or []
    refusal_correct = bool(response["refused"]) is should_refuse
    if conflict_filenames:
        retrieval_hit = _conflict_ok(case, response)
        first_citation_correct = retrieval_hit
    else:
        retrieval_hit = bool(should_refuse or expected in filenames)
        first_citation_correct = bool(should_refuse or (filenames and filenames[0] == expected))
    citation_covered = bool(should_refuse or (not response["refused"] and filenames))
    faithfulness = _faithfulness_ok(case, response)
    conflict_handling = _conflict_ok(case, response)
    passed = (
        refusal_correct
        and retrieval_hit
        and citation_covered
        and faithfulness
        and conflict_handling
    )
    return {
        "name": str(case["name"]),
        "passed": passed,
        "should_refuse": should_refuse,
        "refused": bool(response["refused"]),
        "expected_filename": expected,
        "conflict_filenames": list(conflict_filenames),
        "citation_filenames": filenames,
        "top_score": response["retrieval"]["top_score"],
        "checks": {
            "refusal_correct": refusal_correct,
            "retrieval_hit": retrieval_hit,
            "first_citation_correct": first_citation_correct,
            "citation_covered": citation_covered,
            "citation_faithfulness": faithfulness,
            "conflict_pair_handling": conflict_handling,
        },
    }


def run_evaluation(
    corpus: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    *,
    database_path: Path,
    embedding: EmbeddingProvider | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    active_thresholds = dict(thresholds or THRESHOLDS)
    service = KnowledgeAssistantService(database_path, embedding=embedding)
    principal = Principal("offline-eval", "eval-runner", frozenset({"editor"}))
    started = time.perf_counter()
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
        results.append(evaluate_case(case, response))
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)

    answerable = [result for result in results if not result["should_refuse"]]
    conflict_cases = [result for result in results if result.get("conflict_filenames")]
    faithfulness_cases = [
        result
        for result, case in zip(results, cases, strict=True)
        if case.get("required_phrases")
    ]
    metrics = {
        "retrieval_recall": _ratio(answerable, "retrieval_hit"),
        "first_citation_accuracy": _ratio(answerable, "first_citation_correct"),
        "refusal_accuracy": _ratio(results, "refusal_correct"),
        "citation_coverage": _ratio(answerable, "citation_covered"),
        "citation_faithfulness": _ratio(faithfulness_cases, "citation_faithfulness")
        if faithfulness_cases
        else 1.0,
        "conflict_pair_handling": _ratio(conflict_cases, "conflict_pair_handling")
        if conflict_cases
        else 1.0,
    }
    threshold_checks = {
        name: metrics[name] >= threshold
        for name, threshold in active_thresholds.items()
        if name in metrics
    }
    return {
        "synthetic": True,
        "real_model_api_called": False,
        "embedding_model": service.embedding.name,
        "elapsed_ms": elapsed_ms,
        "summary": {
            "total": len(results),
            "passed": sum(int(result["passed"]) for result in results),
            "failed": sum(int(not result["passed"]) for result in results),
            "quality_gate_passed": all(threshold_checks.values()),
        },
        "metrics": metrics,
        "thresholds": active_thresholds,
        "threshold_checks": threshold_checks,
        "results": results,
    }


def run_ab_evaluation(
    corpus: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    *,
    providers: Sequence[str],
    database_dir: Path,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    comparisons: list[dict[str, Any]] = []
    for provider_name in providers:
        embedding = create_embedding(provider_name)
        payload = run_evaluation(
            corpus,
            cases,
            database_path=database_dir / f"{provider_name.replace('/', '_')}.db",
            embedding=embedding,
            thresholds=thresholds,
        )
        comparisons.append(
            {
                "provider": provider_name,
                "embedding_model": payload["embedding_model"],
                "elapsed_ms": payload["elapsed_ms"],
                "summary": payload["summary"],
                "metrics": payload["metrics"],
                "threshold_checks": payload["threshold_checks"],
                "results": payload["results"],
            }
        )
    return {
        "synthetic": True,
        "real_model_api_called": False,
        "mode": "ab",
        "providers": list(providers),
        "comparisons": comparisons,
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
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--suite",
        choices=("regression", "gold"),
        default="regression",
        help="regression keeps the CI 8-case gate; gold uses the expanded enterprise set",
    )
    parser.add_argument(
        "--provider",
        default="hash",
        help="embedding provider: hash | bow | semantic",
    )
    parser.add_argument(
        "--ab",
        default="",
        help="comma-separated providers for A/B compare, e.g. hash,bow",
    )
    args = parser.parse_args(argv)
    if args.suite == "gold":
        corpus_path = args.corpus or GOLD_CORPUS
        cases_path = args.cases or GOLD_CASES
        thresholds = GOLD_THRESHOLDS
    else:
        corpus_path = args.corpus or DEFAULT_CORPUS
        cases_path = args.cases or DEFAULT_CASES
        thresholds = THRESHOLDS

    corpus = load_jsonl(corpus_path)
    cases = load_jsonl(cases_path)
    with tempfile.TemporaryDirectory(prefix="table-miku-ka2-eval-") as directory:
        root = Path(directory)
        ab_providers = [item.strip() for item in str(args.ab).split(",") if item.strip()]
        if ab_providers:
            payload = run_ab_evaluation(
                corpus,
                cases,
                providers=ab_providers,
                database_dir=root,
                thresholds=thresholds,
            )
            write_results(payload, args.output)
            lines = ["Knowledge Assistant offline A/B evals:"]
            for item in payload["comparisons"]:
                summary = item["summary"]
                lines.append(
                    f"- {item['provider']} ({item['embedding_model']}): "
                    f"{summary['passed']}/{summary['total']} passed; "
                    f"quality_gate={summary['quality_gate_passed']}; "
                    f"elapsed_ms={item['elapsed_ms']}"
                )
            print("\n".join(lines))
            return 0 if all(item["summary"]["quality_gate_passed"] for item in payload["comparisons"]) else 1

        payload = run_evaluation(
            corpus,
            cases,
            database_path=root / "assistant.db",
            embedding=create_embedding(args.provider),
            thresholds=thresholds,
        )
    write_results(payload, args.output)
    summary = payload["summary"]
    print(
        f"Knowledge Assistant offline evals: {summary['passed']}/{summary['total']} cases passed; "
        f"quality_gate={summary['quality_gate_passed']}; "
        f"embedding={payload['embedding_model']}; real model API not called."
    )
    return 0 if summary["failed"] == 0 and summary["quality_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
