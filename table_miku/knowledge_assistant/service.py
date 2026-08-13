from __future__ import annotations

from pathlib import Path
from typing import Any

from .database import SCHEMA_VERSION, AssistantDatabase
from .documents import DocumentService
from .embeddings import HashingEmbedding
from .ingestion import IngestionService
from .observability import TraceRecorder
from .rag import RagService
from .tasks import TaskService


class KnowledgeAssistantService:
    """Composition root for the Knowledge Assistant 2.0 vertical slice."""

    def __init__(
        self,
        database_path: Path | None = None,
        *,
        embedding_dimension: int = 384,
        rag_min_score: float = 0.24,
        approval_ttl_minutes: int = 10,
    ) -> None:
        self.database = AssistantDatabase(database_path)
        self.embedding = HashingEmbedding(embedding_dimension)
        self.traces = TraceRecorder(self.database)
        self.documents = DocumentService(self.database, self.embedding, self.traces)
        self.ingestion = IngestionService(self.database, self.documents, self.traces)
        self.rag = RagService(
            self.database,
            self.embedding,
            self.traces,
            default_min_score=rag_min_score,
        )
        self.tasks = TaskService(
            self.database,
            self.documents,
            self.rag,
            self.traces,
            approval_ttl_minutes=approval_ttl_minutes,
        )

    @property
    def service_instance_id(self) -> str:
        return self.database.service_instance_id

    def start(self) -> None:
        self.ingestion.start()

    def close(self, timeout: float = 1.0) -> bool:
        return self.ingestion.close(timeout=timeout)

    def health(self) -> dict[str, Any]:
        ingestion = self.ingestion.health()
        return {
            "status": "ok" if ingestion["status"] == "ready" else "degraded",
            "schema_version": SCHEMA_VERSION,
            "service_instance_id": self.service_instance_id,
            "embedding_model": self.embedding.name,
            "ingestion": ingestion,
        }
