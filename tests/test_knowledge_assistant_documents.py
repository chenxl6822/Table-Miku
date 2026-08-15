from __future__ import annotations

import hashlib
import json
import io
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from table_miku.knowledge_assistant import KnowledgeAssistantService, PermissionDenied, Principal
from table_miku.knowledge_assistant.auth import ConflictError, ResourceNotFound
from table_miku.knowledge_assistant.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_PDF_PAGES,
    DocumentParser,
    ParsedUnit,
    TextChunker,
)


def principal(
    user_id: str = "editor-1",
    *,
    tenant_id: str = "tenant-a",
    roles: frozenset[str] = frozenset({"editor"}),
    collections: frozenset[str] | None = None,
) -> Principal:
    return Principal(tenant_id, user_id, roles, collections)


def text_pdf(*pages: str) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_markdown_parser_preserves_headings_and_chunker_bounds():
    parser = DocumentParser()
    first_section = "依赖注入用于解耦对象。" * 80
    second_section = "初始化和销毁。" * 40
    content_type, units = parser.parse(
        "guide.md",
        f"# Spring IoC\n{first_section}\n## Bean 生命周期\n{second_section}".encode(),
    )
    chunks = TextChunker(chunk_size=240, overlap=40).split(units)

    assert content_type == "text/markdown"
    assert [unit.heading for unit in units] == ["Spring IoC", "Bean 生命周期"]
    assert len(chunks) > 2
    assert all(0 < len(chunk.content) <= 240 for chunk in chunks)
    assert chunks[0].heading == "Spring IoC"
    assert chunks[-1].heading == "Bean 生命周期"


@pytest.mark.parametrize("filename", ("../secret.txt", "folder/note.md", "folder\\note.md", ""))
def test_parser_rejects_path_like_filenames(filename: str):
    with pytest.raises(ValueError, match="filename"):
        DocumentParser().parse(filename, b"content")


def test_upload_persists_source_chunks_vectors_and_is_idempotent(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = principal()
    content = "# Spring IoC\nSpring IoC 通过依赖注入管理 Bean，降低对象之间的耦合。".encode()

    created = service.documents.upload(
        actor,
        filename="spring.md",
        content=content,
        collection_id="engineering",
        idempotency_key="upload-spring-001",
    )
    replay = service.documents.upload(
        actor,
        filename="spring.md",
        content=content,
        collection_id="engineering",
        idempotency_key="upload-spring-001",
    )
    deduplicated = service.documents.upload(
        actor,
        filename="spring-copy.md",
        content=content,
        collection_id="engineering",
        idempotency_key="upload-spring-002",
    )

    assert created["status"] == "indexed"
    assert created["chunk_count"] == 1
    assert created["deduplicated"] is False
    assert replay["id"] == created["id"]
    assert replay["idempotent_replay"] is True
    assert deduplicated["id"] == created["id"]
    assert deduplicated["deduplicated"] is True
    with service.database.connect() as conn:
        blob = conn.execute(
            "SELECT content FROM document_blobs WHERE document_id = ?", (created["id"],)
        ).fetchone()[0]
        chunk = conn.execute(
            "SELECT embedding, embedding_dimension, embedding_model FROM chunks WHERE document_id = ?",
            (created["id"],),
        ).fetchone()
    assert blob == content
    assert chunk["embedding_dimension"] == 384
    assert chunk["embedding_model"] == "local-hash-v1-384"
    assert len(chunk["embedding"]) == struct.calcsize("<384f")


def test_upload_idempotency_conflict_is_rejected(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = principal()
    service.documents.upload(
        actor,
        filename="one.txt",
        content=b"first document",
        idempotency_key="same-key-001",
    )

    with pytest.raises(ConflictError, match="different request"):
        service.documents.upload(
            actor,
            filename="two.txt",
            content=b"second document",
            idempotency_key="same-key-001",
        )


def test_concurrent_upload_with_same_idempotency_key_is_claimed_once(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = principal()
    entered = threading.Event()
    release = threading.Event()
    original_parse = service.documents.parser.parse

    def delayed_parse(filename: str, content: bytes):
        entered.set()
        assert release.wait(timeout=3)
        return original_parse(filename, content)

    service.documents.parser.parse = delayed_parse  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.documents.upload,
            actor,
            filename="concurrent.txt",
            content=b"concurrent idempotency",
            idempotency_key="concurrent-key-001",
        )
        assert entered.wait(timeout=3)
        with pytest.raises(ConflictError, match="already in progress"):
            service.documents.upload(
                actor,
                filename="concurrent.txt",
                content=b"concurrent idempotency",
                idempotency_key="concurrent-key-001",
            )
        release.set()
        created = future.result(timeout=3)

    assert created["status"] == "indexed"
    with service.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_failed_upload_releases_idempotency_claim_for_explicit_retry(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = principal()
    with pytest.raises(ValueError):
        service.documents.upload(
            actor,
            filename="invalid.exe",
            content=b"first attempt",
            idempotency_key="retry-after-failure",
        )

    retried = service.documents.upload(
        actor,
        filename="valid.txt",
        content=b"second explicit attempt",
        idempotency_key="retry-after-failure",
    )

    assert retried["status"] == "indexed"


def test_failed_parse_is_persisted_as_failed_without_chunks(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = principal()

    with pytest.raises(ValueError, match="unsupported"):
        service.documents.upload(
            actor,
            filename="malware.exe",
            content=b"not executable content",
            idempotency_key="upload-invalid-001",
        )

    documents = service.documents.list_documents(actor)
    assert len(documents) == 1
    assert documents[0]["status"] == "failed"
    assert documents[0]["error"].startswith("ValueError:")
    with service.database.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert count == 0


def test_tenant_and_collection_boundaries_are_enforced(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    engineering_editor = principal(collections=frozenset({"engineering"}))
    created = service.documents.upload(
        engineering_editor,
        filename="private.txt",
        content="租户 A 的内部架构".encode(),
        collection_id="engineering",
        idempotency_key="tenant-boundary-001",
    )

    with pytest.raises(PermissionDenied, match="collection"):
        service.documents.upload(
            engineering_editor,
            filename="hr.txt",
            content="人事制度".encode(),
            collection_id="hr",
            idempotency_key="tenant-boundary-002",
        )
    with pytest.raises(ResourceNotFound):
        service.documents.get_document(
            principal(tenant_id="tenant-b"),
            created["id"],
        )
    with pytest.raises(PermissionDenied, match="collection"):
        service.documents.get_document(
            principal(collections=frozenset({"hr"})),
            created["id"],
        )


def test_viewer_cannot_upload_or_archive(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = principal()
    created = service.documents.upload(
        actor,
        filename="note.txt",
        content=b"safe knowledge",
        idempotency_key="permission-doc-001",
    )
    viewer = principal("viewer", roles=frozenset({"viewer"}))

    with pytest.raises(PermissionDenied):
        service.documents.upload(
            viewer,
            filename="blocked.txt",
            content=b"blocked",
            idempotency_key="permission-doc-002",
        )
    with pytest.raises(PermissionDenied):
        service.documents.archive(viewer, created["id"])


def test_json_document_is_validated_and_normalized(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    created = service.documents.upload(
        principal(),
        filename="policy.json",
        content=json.dumps({"retention_days": 30}, ensure_ascii=False).encode(),
        idempotency_key="upload-json-001",
    )

    assert created["content_type"] == "application/json"
    with service.database.connect() as conn:
        content = conn.execute(
            "SELECT content FROM chunks WHERE document_id = ?", (created["id"],)
        ).fetchone()[0]
    assert '"retention_days": 30' in content


def test_pdf_text_pages_are_indexed_with_page_citations(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = principal()
    document = service.documents.upload(
        actor,
        filename="runbook.pdf",
        content=text_pdf("General introduction", "AuroraRecoveryCode requires two approvers"),
        idempotency_key="upload-pdf-001",
    )

    result = service.rag.query(actor, "What does AuroraRecoveryCode require?")

    assert document["content_type"] == "application/pdf"
    assert document["chunk_count"] == 2
    assert result["refused"] is False
    assert result["citations"][0]["filename"] == "runbook.pdf"
    assert result["citations"][0]["page_number"] == 2


def test_pdf_parser_rejects_malformed_and_encrypted_documents():
    parser = DocumentParser()

    with pytest.raises(ValueError, match="invalid or unreadable PDF"):
        parser.parse("truncated.pdf", b"%PDF-1.7\n1 0 obj\n<<")

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("reviewer-secret")
    output = io.BytesIO()
    writer.write(output)

    with pytest.raises(ValueError, match="encrypted PDF"):
        parser.parse("encrypted.pdf", output.getvalue())


def test_pdf_parser_enforces_file_size_before_reader(monkeypatch: pytest.MonkeyPatch):
    def unexpected_reader(*_args, **_kwargs):
        raise AssertionError("oversized input must be rejected before PdfReader")

    monkeypatch.setattr("pypdf.PdfReader", unexpected_reader)

    with pytest.raises(ValueError, match="byte limit"):
        DocumentParser().parse("oversized.pdf", b"%PDF" + b"0" * MAX_DOCUMENT_BYTES)


def test_pdf_parser_rejects_excessive_page_count():
    writer = PdfWriter()
    for _ in range(MAX_PDF_PAGES + 1):
        writer.add_blank_page(width=1, height=1)
    output = io.BytesIO()
    writer.write(output)

    with pytest.raises(ValueError, match="page limit"):
        DocumentParser().parse("too-many-pages.pdf", output.getvalue())


def test_pdf_parser_checks_cancel_between_pages(monkeypatch: pytest.MonkeyPatch):
    extracted: list[int] = []
    checks = 0

    class Page:
        def __init__(self, number: int) -> None:
            self.number = number

        def extract_text(self, *, visitor_text=None) -> str:
            extracted.append(self.number)
            text = f"page {self.number}"
            if visitor_text is not None:
                visitor_text(text, None, None, None, None)
            return text

    class Reader:
        is_encrypted = False
        pages = [Page(1), Page(2), Page(3)]

    def check_cancel() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("cancelled at a page boundary")

    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: Reader())

    with pytest.raises(RuntimeError, match="page boundary"):
        DocumentParser().parse(
            "cancel.pdf",
            b"%PDF fake",
            cancel_check=check_cancel,
            max_extracted_characters=1_000,
        )

    assert extracted == [1]


def test_pdf_parser_stops_when_cumulative_extracted_text_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    extracted: list[int] = []

    class Page:
        def __init__(self, number: int) -> None:
            self.number = number

        def extract_text(self, *, visitor_text=None) -> str:
            extracted.append(self.number)
            text = "1234"
            if visitor_text is not None:
                visitor_text(text, None, None, None, None)
            return text

    class Reader:
        is_encrypted = False
        pages = [Page(1), Page(2), Page(3)]

    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: Reader())

    with pytest.raises(ValueError, match="extracted text limit"):
        DocumentParser().parse(
            "bounded.pdf",
            b"%PDF fake",
            max_extracted_characters=5,
        )

    assert extracted == [1, 2]


def test_pdf_parser_stops_inside_one_page_after_multiple_fragments_exceed_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    visited: list[str] = []

    class Page:
        @staticmethod
        def extract_text(*, visitor_text=None) -> str:
            for fragment in ("123", "456", "must-not-run"):
                visited.append(fragment)
                if visitor_text is not None:
                    visitor_text(fragment, None, None, None, None)
            return "123456must-not-run"

    class Reader:
        is_encrypted = False
        pages = [Page()]

    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: Reader())

    with pytest.raises(ValueError, match="extracted text limit"):
        DocumentParser().parse(
            "one-large-page.pdf",
            b"%PDF fake",
            max_extracted_characters=5,
        )

    assert visited == ["123", "456"]


def test_chunker_rejects_empty_units():
    with pytest.raises(ValueError, match="indexable"):
        TextChunker().split([ParsedUnit(text="   ")])


def test_find_indexed_by_checksums_is_scoped_and_skips_archived(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    actor = principal()
    other_tenant = principal("editor-2", tenant_id="tenant-b")
    content = b"duplicate-bytes"
    digest = hashlib.sha256(content).hexdigest()
    created = service.documents.upload(
        actor,
        filename="original.md",
        content=content,
        collection_id="engineering",
        idempotency_key="dup-original",
    )
    matches = service.documents.find_indexed_by_checksums(
        actor,
        collection_id="engineering",
        checksums=[digest],
    )
    assert matches == [
        {
            "id": created["id"],
            "filename": "original.md",
            "collection_id": "engineering",
            "checksum": digest,
        }
    ]
    assert service.documents.find_indexed_by_checksums(
        actor, collection_id="legal", checksums=[digest]
    ) == []
    assert service.documents.find_indexed_by_checksums(
        other_tenant, collection_id="engineering", checksums=[digest]
    ) == []
    service.documents.archive(actor, created["id"])
    assert service.documents.find_indexed_by_checksums(
        actor, collection_id="engineering", checksums=[digest]
    ) == []
    restricted = principal(collections=frozenset({"legal"}))
    with pytest.raises(PermissionDenied):
        service.documents.find_indexed_by_checksums(
            restricted, collection_id="engineering", checksums=[digest]
        )
    deny_all = principal(collections=frozenset())
    with pytest.raises(PermissionDenied):
        service.documents.find_indexed_by_checksums(
            deny_all, collection_id="engineering", checksums=[digest]
        )
    with pytest.raises(ValueError, match="64-character"):
        service.documents.find_indexed_by_checksums(
            actor, collection_id="engineering", checksums=["not-a-digest"]
        )
    with pytest.raises(ValueError, match="exceed"):
        service.documents.find_indexed_by_checksums(
            actor,
            collection_id="engineering",
            checksums=[f"{index:064x}" for index in range(21)],
        )
