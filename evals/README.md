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
only after explicitly starting the synthetic capability test and manual prompt matrix in the Agent Center. Do not add a
real API call to CI, and do not commit generated results.
