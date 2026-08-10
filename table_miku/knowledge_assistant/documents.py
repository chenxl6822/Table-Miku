from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from .auth import ConflictError, Principal, ResourceNotFound
from .database import AssistantDatabase
from .embeddings import HashingEmbedding, estimate_tokens
from .observability import TraceRecorder


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 500
SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".rst", ".json", ".pdf"})
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def request_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ParsedUnit:
    text: str
    heading: str = ""
    page_number: int | None = None


@dataclass(frozen=True)
class DocumentChunk:
    ordinal: int
    content: str
    heading: str = ""
    page_number: int | None = None


class DocumentParser:
    def parse(self, filename: str, content: bytes) -> tuple[str, list[ParsedUnit]]:
        safe_name = self.safe_filename(filename)
        if not content:
            raise ValueError("document must not be empty")
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
        suffix = PurePosixPath(safe_name).suffix.casefold()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError(f"unsupported document type: {suffix or '<none>'}")
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        if suffix == ".pdf":
            return "application/pdf", self._parse_pdf(content)
        text = self._decode_text(content)
        if suffix == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON document at line {exc.lineno}") from exc
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            content_type = "application/json"
        if suffix in {".md", ".markdown"}:
            return content_type, self._parse_markdown(text)
        return content_type, [ParsedUnit(text=text.strip())]

    @staticmethod
    def safe_filename(filename: str) -> str:
        normalized = filename.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if not normalized or path.name != normalized or path.name in {".", ".."}:
            raise ValueError("filename must be a plain file name without a path")
        if any(character in path.name for character in ("\x00", "\r", "\n")):
            raise ValueError("filename contains invalid characters")
        return path.name[:240]

    @staticmethod
    def _decode_text(content: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("text document must use UTF-8 encoding")

    @staticmethod
    def _parse_markdown(text: str) -> list[ParsedUnit]:
        units: list[ParsedUnit] = []
        heading = ""
        lines: list[str] = []
        for line in text.splitlines():
            match = _HEADING.match(line)
            if match:
                body = "\n".join(lines).strip()
                if body:
                    units.append(ParsedUnit(text=body, heading=heading))
                heading = match.group(2).strip()
                lines = []
            else:
                lines.append(line)
        body = "\n".join(lines).strip()
        if body:
            units.append(ParsedUnit(text=body, heading=heading))
        return units or [ParsedUnit(text=text.strip())]

    @staticmethod
    def _parse_pdf(content: bytes) -> list[ParsedUnit]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF parsing requires pypdf") from exc
        try:
            reader = PdfReader(io.BytesIO(content))
        except Exception as exc:
            raise ValueError("invalid or unreadable PDF document") from exc
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    raise ValueError("encrypted PDF documents are not supported")
            except Exception as exc:
                raise ValueError("encrypted PDF documents are not supported") from exc
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES} page limit")
        units = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception as exc:
                raise ValueError(f"failed to extract PDF page {page_number}") from exc
            if text:
                units.append(ParsedUnit(text=text, page_number=page_number))
        if not units:
            raise ValueError("PDF does not contain extractable text; OCR is not enabled")
        return units


class TextChunker:
    def __init__(self, chunk_size: int = 900, overlap: int = 120) -> None:
        if chunk_size < 200 or chunk_size > 4000:
            raise ValueError("chunk_size must be between 200 and 4000")
        if overlap < 0 or overlap >= chunk_size // 2:
            raise ValueError("overlap must be non-negative and less than half of chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap

    def split(self, units: list[ParsedUnit]) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        for unit in units:
            text = re.sub(r"[ \t]+", " ", unit.text).strip()
            start = 0
            while start < len(text):
                end = min(start + self.chunk_size, len(text))
                if end < len(text):
                    lower_bound = start + int(self.chunk_size * 0.6)
                    candidates = [
                        text.rfind("\n", lower_bound, end),
                        text.rfind("。", lower_bound, end),
                        text.rfind(". ", lower_bound, end),
                        text.rfind(" ", lower_bound, end),
                    ]
                    boundary = max(candidates)
                    if boundary >= lower_bound:
                        end = boundary + 1
                content = text[start:end].strip()
                if content:
                    chunks.append(
                        DocumentChunk(
                            ordinal=len(chunks),
                            content=content,
                            heading=unit.heading,
                            page_number=unit.page_number,
                        )
                    )
                if end >= len(text):
                    break
                next_start = max(start + 1, end - self.overlap)
                while next_start < end and text[next_start].isspace():
                    next_start += 1
                start = next_start
        if not chunks:
            raise ValueError("document does not contain indexable text")
        return chunks


class DocumentService:
    def __init__(
        self,
        database: AssistantDatabase,
        embedding: HashingEmbedding,
        traces: TraceRecorder,
        *,
        parser: DocumentParser | None = None,
        chunker: TextChunker | None = None,
    ) -> None:
        self.database = database
        self.embedding = embedding
        self.traces = traces
        self.parser = parser or DocumentParser()
        self.chunker = chunker or TextChunker()

    def upload(
        self,
        principal: Principal,
        *,
        filename: str,
        content: bytes,
        collection_id: str = "default",
        idempotency_key: str,
    ) -> dict[str, Any]:
        principal.require("knowledge:write")
        collection_id = self._collection_id(collection_id)
        principal.require_collection(collection_id)
        idempotency_key = idempotency_key.strip()
        if len(idempotency_key) < 8:
            raise ValueError("idempotency_key must contain at least 8 characters")
        safe_name = self.parser.safe_filename(filename)
        checksum = hashlib.sha256(content).hexdigest()
        request_hash = request_digest(
            {"filename": safe_name, "collection_id": collection_id, "checksum": checksum}
        )
        cached = self._claim_idempotency(principal, "document-upload", idempotency_key, request_hash)
        if cached is not None:
            cached["idempotent_replay"] = True
            return cached
        try:
            with self.traces.trace(
                "document.upload",
                principal,
                {"filename": safe_name, "collection_id": collection_id, "byte_size": len(content)},
            ) as trace:
                trace.add_tokens(input_tokens=estimate_tokens(content.decode("utf-8", errors="ignore")))
                with trace.span("document.reserve"):
                    document_id, deduplicated = self._reserve_document(
                        principal,
                        safe_name,
                        content,
                        collection_id,
                        checksum,
                    )
                if deduplicated:
                    response = self.get_document(principal, document_id)
                    response.update({"deduplicated": True, "idempotent_replay": False})
                    self._save_idempotency(
                        principal, "document-upload", idempotency_key, request_hash, response
                    )
                    return response
                try:
                    with trace.span("document.parse"):
                        content_type, units = self.parser.parse(safe_name, content)
                    with trace.span("document.chunk"):
                        chunks = self.chunker.split(units)
                    with trace.span("document.embed", {"chunk_count": len(chunks)}):
                        embedded = [
                            (chunk, self.embedding.pack(self.embedding.embed(chunk.content)))
                            for chunk in chunks
                        ]
                    with trace.span("document.persist"):
                        self._persist_chunks(document_id, content_type, embedded)
                except Exception as exc:
                    self._mark_failed(document_id, exc)
                    raise
                response = self.get_document(principal, document_id)
                response.update({"deduplicated": False, "idempotent_replay": False})
                self._save_idempotency(principal, "document-upload", idempotency_key, request_hash, response)
                return response
        except Exception:
            self._release_idempotency_claim(
                principal, "document-upload", idempotency_key, request_hash
            )
            raise

    def get_document(self, principal: Principal, document_id: str) -> dict[str, Any]:
        principal.require("knowledge:read")
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count "
                "FROM documents d WHERE d.id = ? AND d.tenant_id = ?",
                (document_id, principal.tenant_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFound("document not found")
        item = dict(row)
        principal.require_collection(str(item["collection_id"]))
        item["archived"] = bool(item["archived"])
        return item

    def list_documents(self, principal: Principal, limit: int = 100) -> list[dict[str, Any]]:
        principal.require("knowledge:read")
        query = "SELECT * FROM documents WHERE tenant_id = ? AND archived = 0"
        params: list[Any] = [principal.tenant_id]
        if principal.collection_ids is not None:
            if not principal.collection_ids:
                return []
            placeholders = ",".join("?" for _ in principal.collection_ids)
            query += f" AND collection_id IN ({placeholders})"
            params.extend(sorted(principal.collection_ids))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 500))
        with self.database.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def archive(self, principal: Principal, document_id: str) -> dict[str, Any]:
        principal.require("knowledge:write")
        document = self.get_document(principal, document_id)
        if document["archived"]:
            return document
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE documents SET archived = 1, updated_at = ? WHERE id = ? AND tenant_id = ?",
                (utc_now(), document_id, principal.tenant_id),
            )
        return self.get_document(principal, document_id)

    def _reserve_document(
        self,
        principal: Principal,
        filename: str,
        content: bytes,
        collection_id: str,
        checksum: str,
    ) -> tuple[str, bool]:
        now = utc_now()
        with self.database.connect() as conn:
            existing = conn.execute(
                "SELECT id, status FROM documents WHERE tenant_id = ? AND collection_id = ? "
                "AND checksum = ? AND archived = 0",
                (principal.tenant_id, collection_id, checksum),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "indexed":
                    return str(existing["id"]), True
                if existing["status"] == "processing":
                    raise ConflictError("the same document is already being processed")
                document_id = str(existing["id"])
                conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
                conn.execute(
                    "UPDATE documents SET filename = ?, byte_size = ?, status = 'processing', error = '', "
                    "created_by = ?, updated_at = ? WHERE id = ?",
                    (filename, len(content), principal.user_id, now, document_id),
                )
                conn.execute(
                    "INSERT INTO document_blobs(document_id, content) VALUES(?, ?) "
                    "ON CONFLICT(document_id) DO UPDATE SET content = excluded.content",
                    (document_id, content),
                )
                return document_id, False
            document_id = f"doc-{uuid.uuid4().hex}"
            try:
                conn.execute(
                    "INSERT INTO documents(id, tenant_id, collection_id, filename, content_type, checksum, "
                    "byte_size, status, created_by, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, 'application/octet-stream', ?, ?, 'processing', ?, ?, ?)",
                    (
                        document_id,
                        principal.tenant_id,
                        collection_id,
                        filename,
                        checksum,
                        len(content),
                        principal.user_id,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO document_blobs(document_id, content) VALUES(?, ?)",
                    (document_id, content),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("document reservation conflicted with another upload") from exc
        return document_id, False

    def _persist_chunks(
        self,
        document_id: str,
        content_type: str,
        embedded: list[tuple[DocumentChunk, bytes]],
    ) -> None:
        now = utc_now()
        with self.database.connect() as conn:
            document = conn.execute(
                "SELECT tenant_id, collection_id FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if document is None:
                raise ResourceNotFound("reserved document disappeared")
            conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            conn.executemany(
                "INSERT INTO chunks(id, document_id, tenant_id, collection_id, ordinal, heading, page_number, "
                "content, content_hash, embedding, embedding_model, embedding_dimension, token_count, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        f"chunk-{uuid.uuid4().hex}",
                        document_id,
                        document["tenant_id"],
                        document["collection_id"],
                        chunk.ordinal,
                        chunk.heading,
                        chunk.page_number,
                        chunk.content,
                        hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                        blob,
                        self.embedding.name,
                        self.embedding.dimension,
                        estimate_tokens(chunk.content),
                        now,
                    )
                    for chunk, blob in embedded
                ],
            )
            conn.execute(
                "UPDATE documents SET content_type = ?, status = 'indexed', error = '', updated_at = ? WHERE id = ?",
                (content_type, now, document_id),
            )

    def _mark_failed(self, document_id: str, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {str(exc)}"[:500]
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE documents SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (message, utc_now(), document_id),
            )

    def _claim_idempotency(
        self,
        principal: Principal,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO idempotency_records(tenant_id, scope, idempotency_key, request_hash, "
                "response_json, created_at) VALUES(?, ?, ?, ?, '', ?)",
                (principal.tenant_id, scope, idempotency_key, request_hash, utc_now()),
            )
            if inserted.rowcount == 1:
                return None
            row = conn.execute(
                "SELECT request_hash, response_json FROM idempotency_records "
                "WHERE tenant_id = ? AND scope = ? AND idempotency_key = ?",
                (principal.tenant_id, scope, idempotency_key),
            ).fetchone()
        if row is None:
            raise ConflictError("idempotency claim could not be read")
        if row["request_hash"] != request_hash:
            raise ConflictError("idempotency key was already used with a different request")
        if not row["response_json"]:
            raise ConflictError("a request with this idempotency key is already in progress")
        return json.loads(row["response_json"])

    def _save_idempotency(
        self,
        principal: Principal,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> None:
        with self.database.connect() as conn:
            updated = conn.execute(
                "UPDATE idempotency_records SET response_json = ? WHERE tenant_id = ? AND scope = ? "
                "AND idempotency_key = ? AND request_hash = ? AND response_json = ''",
                (
                    json.dumps(response, ensure_ascii=False),
                    principal.tenant_id,
                    scope,
                    idempotency_key,
                    request_hash,
                ),
            )
            if updated.rowcount != 1:
                raise ConflictError("idempotency response could not be finalized")

    def _release_idempotency_claim(
        self,
        principal: Principal,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "DELETE FROM idempotency_records WHERE tenant_id = ? AND scope = ? AND idempotency_key = ? "
                "AND request_hash = ? AND response_json = ''",
                (principal.tenant_id, scope, idempotency_key, request_hash),
            )

    @staticmethod
    def decode_base64(value: str) -> bytes:
        try:
            return base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ValueError("content_base64 is not valid Base64") from exc

    @staticmethod
    def _collection_id(value: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 120:
            raise ValueError("collection_id must contain 1 to 120 characters")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", cleaned):
            raise ValueError("collection_id contains unsupported characters")
        return cleaned
