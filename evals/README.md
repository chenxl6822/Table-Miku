# Agent evals

`run_agent_evals.py` uses synthetic cases only. It never reads the Vault, API keys, or production databases.
CI runs the fake/static suite; any real DeepSeek evaluation must be started explicitly in the Agent Center.
Generated results belong under `evals/results/` and are not committed.
