# Agent evals

`run_agent_evals.py` loads 18 synthetic cases from `cases.jsonl` and validates deterministic engineering contracts:

- read-resource permission gates;
- single-Agent and multi-Agent tool topology;
- write-tool approval requirements;
- absence of Shell, PowerShell, raw Vault, arbitrary filesystem, and Web Search tools.

Run it from the repository root:

```powershell
python evals/run_agent_evals.py
```

The default result is written to ignored `evals/results/latest.json`. The harness uses a synthetic model identifier,
never reads the Vault, API keys, or production databases, and never calls a real API.

These contract evals do not measure DeepSeek answer quality, latency, token use, or actual tool selection. Compare those
only after explicitly starting the synthetic capability test and the "对比单/多 Agent 质量" action in the Agent Center.
The runtime comparison uses three synthetic cases, never reads user data, and caps the comparison at 12 model responses.
Specialists are enabled only when every specialist route is correct, the multi-Agent score is at least 80, and it strictly
beats the single-Agent score. Do not add a real API call to CI, and do not commit generated results.
