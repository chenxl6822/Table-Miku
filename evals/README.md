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

## Knowledge Assistant 2.0 离线评测

企业知识库纵向切片使用固定语料和固定问答集验证检索、首条引用与拒答，不调用真实模型 API：

```powershell
.\.venv\Scripts\python.exe evals\run_knowledge_assistant_evals.py
```

输入为 `knowledge_assistant_corpus.jsonl` 与 `knowledge_assistant_cases.jsonl`，结果写入已忽略的
`evals/results/knowledge_assistant_latest.json`。质量门要求：检索召回率至少 95%、首条引用准确率至少
85%、引用覆盖率 100%、拒答准确率 100%。
