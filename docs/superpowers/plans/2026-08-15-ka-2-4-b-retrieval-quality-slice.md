# KA 2.4-B Slice-1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use TDD.

**Goal:** Gold-set eval skeleton + versioned embedding providers + A/B compare; keep hash as default.

**Architecture:** Extract `EmbeddingProvider` protocol; inject providers into `KnowledgeAssistantService` / evals; expand gold JSONL; extend eval runner with faithfulness/conflict metrics and `--ab`.

**Tech stack:** Python 3.12, existing SQLite KA stack, optional `sentence-transformers` extra.

## File map

| File | Responsibility |
|---|---|
| `table_miku/knowledge_assistant/embeddings.py` | Protocol + HashingEmbedding + BowEmbedding + optional LocalSemanticEmbedding |
| `table_miku/knowledge_assistant/service.py` | Accept optional embedding injection; default hash |
| `table_miku/knowledge_assistant/rag.py` / `documents.py` | Type against protocol |
| `evals/knowledge_assistant_gold_corpus.jsonl` | Expanded synthetic corpus |
| `evals/knowledge_assistant_gold_cases.jsonl` | Gold cases with faithfulness/conflict fields |
| `evals/run_knowledge_assistant_evals.py` | Metrics + `--ab` + provider factory |
| `requirements-ka2-semantic.txt` | Optional semantic deps |
| `tests/test_knowledge_assistant_embeddings.py` | Provider contracts |
| `tests/test_knowledge_assistant_evals.py` | Gold metrics / A/B |
| `docs/KNOWLEDGE_ASSISTANT_2.md` | Document slice status |

## Tasks

### Task 1: EmbeddingProvider + BowEmbedding

- RED: provider name/dimension/pack roundtrip tests
- GREEN: Protocol + BowEmbedding (different projection than hash)
- Keep HashingEmbedding API stable

### Task 2: Service injection

- Allow `KnowledgeAssistantService(..., embedding=provider)`
- Default remains HashingEmbedding

### Task 3: Gold corpus/cases + metrics

- Add gold JSONL with conflict pair, no-answer, `required_phrases` for faithfulness
- Extend eval scoring; keep legacy 8-case CI gate path

### Task 4: A/B runner + optional semantic

- `--ab hash,bow` offline
- Optional LocalSemanticEmbedding behind extras; fail closed if missing

### Task 5: Docs + verification

- Update KA docs; run targeted tests + existing offline eval gate
