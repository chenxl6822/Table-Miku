# 2026-08-15 Knowledge Assistant 2.4-B Slice-1 Design

## Goal

Close the retrieval-quality loop enough to compare providers on a synthetic enterprise gold set, without switching the production default away from `local-hash-v1-384`.

## In scope

1. `EmbeddingProvider` protocol (name, dimension, embed/pack/unpack/cosine).
2. Expanded synthetic gold corpus/cases: answerable, no-answer, conflicting documents, citation faithfulness phrases.
3. Offline eval metrics: existing recall/first-citation/refusal/coverage plus `citation_faithfulness` and `conflict_pair_handling`.
4. A/B eval mode comparing two providers on the same gold set.
5. Optional local semantic provider (`sentence-transformers` MiniLM, 384-d) behind `requirements-ka2-semantic.txt`.
6. Deterministic CI-safe alternate provider (`local-bow-v1-384`) only for harness A/B without neural deps — explicitly not a semantic quality claim.

## Out of scope (later slices)

- Reranker
- User feedback / knowledge-gap telemetry UI
- Switching default embedding in `KnowledgeAssistantService`
- Cloud embedding APIs
- 2.4-C ingest UX

## Non-negotiables

- Default runtime embedding remains `local-hash-v1-384`.
- Tenant/collection filters still apply before scoring.
- Offline CI quality gate continues to use hash + existing regression thresholds; do not weaken thresholds or delete failing cases to pass.
- No real model API calls in CI; no logging of document bodies/paths/tokens into traces for eval.
- Switching default requires both quality and cost gates later — not this slice.

## Acceptance

- Unit tests for protocol adapters and new metrics.
- `evals/run_knowledge_assistant_evals.py` still passes CI gate on hash.
- `--ab` produces comparable metrics for `hash` vs `bow` without semantic extras installed.
- Optional semantic provider loads only when extras installed; otherwise fails closed with a clear error.
