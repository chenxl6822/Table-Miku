from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .auth import Principal
from .database import AssistantDatabase
from .embeddings import EmbeddingProvider, estimate_tokens, text_tokens
from .observability import TraceRecorder


MAX_CANDIDATE_CHUNKS = 20_000


@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: str
    document_id: str
    filename: str
    collection_id: str
    ordinal: int
    heading: str
    page_number: int | None
    content: str
    score: float
    vector_score: float
    lexical_score: float


class RagService:
    def __init__(
        self,
        database: AssistantDatabase,
        embedding: EmbeddingProvider,
        traces: TraceRecorder,
        *,
        default_min_score: float = 0.24,
    ) -> None:
        if not 0 <= default_min_score <= 1:
            raise ValueError("default_min_score must be between 0 and 1")
        self.database = database
        self.embedding = embedding
        self.traces = traces
        self.default_min_score = default_min_score

    def query(
        self,
        principal: Principal,
        query: str,
        *,
        collection_ids: list[str] | None = None,
        top_k: int = 5,
        min_score: float | None = None,
    ) -> dict[str, Any]:
        principal.require("knowledge:read")
        cleaned_query = re.sub(r"\s+", " ", query).strip()
        if len(cleaned_query) < 2 or len(cleaned_query) > 1000:
            raise ValueError("query must contain 2 to 1000 characters")
        top_k = min(max(int(top_k), 1), 8)
        threshold = self.default_min_score if min_score is None else float(min_score)
        if not 0 <= threshold <= 1:
            raise ValueError("min_score must be between 0 and 1")
        requested_collections = self._allowed_collections(principal, collection_ids)
        with self.traces.trace(
            "rag.query",
            principal,
            {"top_k": top_k, "collection_count": len(requested_collections or ())},
        ) as trace:
            trace.add_tokens(input_tokens=estimate_tokens(cleaned_query))
            with trace.span("rag.retrieve"):
                hits = self._retrieve(
                    principal,
                    cleaned_query,
                    requested_collections,
                    top_k=top_k,
                )
            accepted = [hit for hit in hits if hit.score >= threshold]
            with trace.span("rag.grounding", {"accepted_count": len(accepted)}):
                if not accepted:
                    answer = (
                        "现有知识库中没有找到足够可靠的证据来回答这个问题。"
                        "请补充相关文档、缩小问题范围，或明确要求使用其他来源。"
                    )
                    trace.add_tokens(output_tokens=estimate_tokens(answer))
                    return {
                        "answer": answer,
                        "refused": True,
                        "reason": "insufficient_evidence",
                        "citations": [],
                        "retrieval": {
                            "candidate_count": len(hits),
                            "accepted_count": 0,
                            "threshold": threshold,
                            "top_score": round(hits[0].score, 6) if hits else 0.0,
                        },
                        "trace_id": trace.trace_id,
                    }
                citations = [self._citation(index, hit) for index, hit in enumerate(accepted, start=1)]
                answer = self._grounded_answer(accepted)
                trace.add_tokens(output_tokens=estimate_tokens(answer))
                return {
                    "answer": answer,
                    "refused": False,
                    "reason": "grounded_in_indexed_sources",
                    "citations": citations,
                    "retrieval": {
                        "candidate_count": len(hits),
                        "accepted_count": len(accepted),
                        "threshold": threshold,
                        "top_score": round(accepted[0].score, 6),
                    },
                    "trace_id": trace.trace_id,
                }

    def _retrieve(
        self,
        principal: Principal,
        query: str,
        collection_ids: tuple[str, ...] | None,
        *,
        top_k: int,
    ) -> list[RetrievalHit]:
        sql = (
            "SELECT c.*, d.filename FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE c.tenant_id = ? AND d.status = 'indexed' AND d.archived = 0"
        )
        params: list[Any] = [principal.tenant_id]
        if collection_ids is not None:
            if not collection_ids:
                return []
            placeholders = ",".join("?" for _ in collection_ids)
            sql += f" AND c.collection_id IN ({placeholders})"
            params.extend(collection_ids)
        sql += " ORDER BY d.updated_at DESC, c.ordinal ASC LIMIT ?"
        params.append(MAX_CANDIDATE_CHUNKS)
        with self.database.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        query_vector = self.embedding.embed(query)
        query_tokens = set(text_tokens(query))
        query_anchors = {
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9_+#.-]{3,}", query)
        }
        hits: list[RetrievalHit] = []
        for row in rows:
            dimension = int(row["embedding_dimension"])
            if row["embedding_model"] != self.embedding.name or dimension != self.embedding.dimension:
                continue
            chunk_vector = self.embedding.unpack(row["embedding"], dimension)
            vector_score = max(0.0, min(1.0, self.embedding.cosine(query_vector, chunk_vector)))
            chunk_tokens = set(text_tokens(str(row["content"])))
            shared = query_tokens.intersection(chunk_tokens)
            query_coverage = len(shared) / max(1, len(query_tokens))
            length_normalized_overlap = len(shared) / max(
                1.0, math.sqrt(len(query_tokens) * len(chunk_tokens))
            )
            lexical_score = max(query_coverage, length_normalized_overlap)
            lexical_score = max(0.0, min(1.0, lexical_score))
            matched_anchors = query_anchors.intersection(chunk_tokens)
            if query_anchors and not matched_anchors:
                score = 0.0
            elif query_anchors:
                anchor_coverage = len(matched_anchors) / len(query_anchors)
                score = vector_score * 0.45 + lexical_score * 0.25 + anchor_coverage * 0.3
            elif not shared:
                score = vector_score * 0.25
            else:
                score = vector_score * 0.65 + lexical_score * 0.35
            hits.append(
                RetrievalHit(
                    chunk_id=str(row["id"]),
                    document_id=str(row["document_id"]),
                    filename=str(row["filename"]),
                    collection_id=str(row["collection_id"]),
                    ordinal=int(row["ordinal"]),
                    heading=str(row["heading"]),
                    page_number=int(row["page_number"]) if row["page_number"] is not None else None,
                    content=str(row["content"]),
                    score=score,
                    vector_score=vector_score,
                    lexical_score=lexical_score,
                )
            )
        hits.sort(key=lambda item: (-item.score, item.document_id, item.ordinal))
        return hits[:top_k]

    @staticmethod
    def _citation(index: int, hit: RetrievalHit) -> dict[str, Any]:
        return {
            "id": f"S{index}",
            "document_id": hit.document_id,
            "chunk_id": hit.chunk_id,
            "filename": hit.filename,
            "collection_id": hit.collection_id,
            "heading": hit.heading,
            "page_number": hit.page_number,
            "excerpt": RagService._excerpt(hit.content, 360),
            "score": round(hit.score, 6),
            "vector_score": round(hit.vector_score, 6),
            "lexical_score": round(hit.lexical_score, 6),
        }

    @staticmethod
    def _grounded_answer(hits: list[RetrievalHit]) -> str:
        lines = ["根据已索引文档中的证据："]
        for index, hit in enumerate(hits, start=1):
            context = RagService._excerpt(hit.content, 420)
            label = f"（{hit.heading}）" if hit.heading else ""
            lines.append(f"- {context}{label} [S{index}]")
        lines.append("以上回答只基于所列来源；未在来源中出现的内容没有被补充推断。")
        return "\n".join(lines)

    @staticmethod
    def _excerpt(content: str, limit: int) -> str:
        compact = re.sub(r"\s+", " ", content).strip()
        if len(compact) <= limit:
            return compact
        return compact[: max(1, limit - 1)].rstrip() + "…"

    @staticmethod
    def _allowed_collections(
        principal: Principal,
        requested: list[str] | None,
    ) -> tuple[str, ...] | None:
        if requested is not None:
            if not isinstance(requested, list):
                raise ValueError("collection_ids must be a list")
            if any(not isinstance(item, str) for item in requested):
                raise ValueError("collection_ids must contain strings")
            cleaned = tuple(dict.fromkeys(item.strip() for item in requested if item.strip()))
            for collection_id in cleaned:
                principal.require_collection(collection_id)
            return cleaned
        if principal.collection_ids is None:
            return None
        return tuple(sorted(principal.collection_ids))
