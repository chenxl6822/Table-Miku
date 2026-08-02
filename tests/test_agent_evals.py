from __future__ import annotations

import json
from pathlib import Path

from evals.run_agent_evals import agent_tool_contracts, load_cases, main, run_contract_evals


def test_synthetic_agent_contract_eval_matrix_passes():
    cases = load_cases()
    payload = run_contract_evals(cases)

    assert len(cases) == 18
    assert payload["summary"] == {"total": 18, "passed": 18, "failed": 0}
    assert payload["real_api_called"] is False
    assert payload["topology"]["single_specialists"] == []
    assert payload["topology"]["multi_specialists"] == [
        "consult_knowledge_tutor",
        "consult_practice_analyst",
        "consult_review_planner",
    ]
    assert payload["topology"]["quality_comparison"] == "explicit_runtime_ab_evaluation_available"
    assert payload["topology"]["activation_gate"] == {
        "minimum_multi_score": 80,
        "must_beat_single": True,
        "all_specialist_routes_required": True,
        "request_limit": 12,
    }


def test_agent_tool_contracts_keep_writes_approved_and_forbidden_tools_absent():
    contracts = agent_tool_contracts()

    assert contracts["single"]["record_review_answer"] is True
    assert contracts["single"]["apply_learning_plan"] is True
    assert contracts["single"]["search_local_knowledge"] is False
    assert not any("shell" in name or "vault" in name or "web_search" in name for name in contracts["multi"])


def test_eval_cli_writes_ignored_result_without_real_api(tmp_path: Path):
    output = tmp_path / "latest.json"

    assert main(["--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"]["failed"] == 0
    assert payload["real_api_called"] is False
