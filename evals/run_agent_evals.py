from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from table_miku.agent_policy import required_ungranted_resource  # noqa: E402
from table_miku.agent_runtime import AgentsSDKBackend, DeepSeekConfig, DeepSeekModelProvider  # noqa: E402


EVAL_ROOT = Path(__file__).resolve().parent
DEFAULT_CASES = EVAL_ROOT / "cases.jsonl"
DEFAULT_OUTPUT = EVAL_ROOT / "results" / "latest.json"
SPECIALIST_TOOLS = {
    "consult_knowledge_tutor",
    "consult_practice_analyst",
    "consult_review_planner",
}
FORBIDDEN_TOOL_MARKERS = ("shell", "powershell", "filesystem", "vault", "web_search")


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    checks: list[str]
    failures: list[str]


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        cleaned = line.strip()
        if not cleaned:
            continue
        payload = json.loads(cleaned)
        if not isinstance(payload, dict) or not payload.get("name") or not payload.get("prompt"):
            raise ValueError(f"Invalid eval case at {path}:{line_number}")
        cases.append(payload)
    return cases


def agent_tool_contracts() -> dict[str, dict[str, bool]]:
    provider = DeepSeekModelProvider(
        DeepSeekConfig(api_key="synthetic-eval", base_url="https://synthetic.invalid", model="synthetic-model")
    )
    provider._model = "synthetic-model"
    backend = AgentsSDKBackend(provider)
    single = backend._agent(False, {})
    multi = backend._agent(True, {})
    return {
        "single": {str(tool.name): bool(tool.needs_approval) for tool in single.tools},
        "multi": {str(tool.name): bool(tool.needs_approval) for tool in multi.tools},
    }


def evaluate_case(case: dict[str, Any], contracts: dict[str, dict[str, bool]]) -> EvalResult:
    checks: list[str] = []
    failures: list[str] = []
    grants = {str(key): bool(value) for key, value in (case.get("grants") or {}).items()}
    expected_resource = case.get("expected_resource")
    detected = required_ungranted_resource(str(case["prompt"]), grants)
    detected_value = detected.value if detected is not None else None
    if detected_value == expected_resource:
        checks.append("resource_policy")
    else:
        failures.append(f"resource_policy expected={expected_resource!r} actual={detected_value!r}")

    expected_tool = str(case.get("expected_tool") or "")
    if expected_tool == "refuse":
        exposed = [
            name
            for name in contracts["multi"]
            if any(marker in name.lower() for marker in FORBIDDEN_TOOL_MARKERS)
        ]
        if exposed:
            failures.append(f"forbidden_tools_exposed={exposed}")
        else:
            checks.append("forbidden_tools_absent")
    elif expected_tool:
        if expected_tool in contracts["single"]:
            checks.append("single_tool_registered")
        else:
            failures.append(f"single_tool_missing={expected_tool}")
        expected_approval = bool(case.get("requires_approval"))
        actual_approval = contracts["single"].get(expected_tool)
        if actual_approval == expected_approval:
            checks.append("approval_contract")
        else:
            failures.append(
                f"approval_contract tool={expected_tool} expected={expected_approval} actual={actual_approval}"
            )

    expected_specialist = str(case.get("expected_specialist") or "")
    if expected_specialist:
        if expected_specialist in contracts["multi"] and expected_specialist not in contracts["single"]:
            checks.append("specialist_only_in_multi")
        else:
            failures.append(f"specialist_contract={expected_specialist}")

    return EvalResult(str(case["name"]), not failures, checks, failures)


def run_contract_evals(cases: list[dict[str, Any]]) -> dict[str, Any]:
    contracts = agent_tool_contracts()
    results = [evaluate_case(case, contracts) for case in cases]
    passed = sum(item.passed for item in results)
    return {
        "synthetic": True,
        "real_api_called": False,
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "topology": {
            "single_tool_count": len(contracts["single"]),
            "multi_tool_count": len(contracts["multi"]),
            "single_specialists": sorted(SPECIALIST_TOOLS.intersection(contracts["single"])),
            "multi_specialists": sorted(SPECIALIST_TOOLS.intersection(contracts["multi"])),
            "quality_comparison": "requires_explicit_real_deepseek_run",
        },
        "results": [asdict(item) for item in results],
    }


def write_results(payload: dict[str, Any], path: Path = DEFAULT_OUTPUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run synthetic Table Miku Agent contract evals.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    payload = run_contract_evals(load_cases(args.cases))
    write_results(payload, args.output)
    summary = payload["summary"]
    print(
        f"Synthetic agent contract evals: {summary['passed']}/{summary['total']} passed; "
        "real DeepSeek API not called."
    )
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
