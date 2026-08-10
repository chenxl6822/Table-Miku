from __future__ import annotations

from pathlib import Path

from table_miku.knowledge_assistant import KnowledgeAssistantService, Principal


def actor(
    tenant_id: str = "tenant-a",
    *,
    collections: frozenset[str] | None = None,
) -> Principal:
    return Principal(tenant_id, "user-1", frozenset({"editor"}), collections)


def upload(
    service: KnowledgeAssistantService,
    principal: Principal,
    filename: str,
    text: str,
    collection: str,
) -> dict:
    return service.documents.upload(
        principal,
        filename=filename,
        content=text.encode(),
        collection_id=collection,
        idempotency_key=f"upload-{principal.tenant_id}-{filename}-001",
    )


def test_rag_returns_grounded_answer_and_structured_citations(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    principal = actor()
    spring = upload(
        service,
        principal,
        "spring.md",
        "# Spring IoC\nSpring IoC 容器通过依赖注入创建和装配 Bean，从而降低组件耦合。",
        "engineering",
    )
    upload(
        service,
        principal,
        "redis.md",
        "# Redis\nRedis 使用内存数据结构，并可通过 RDB 或 AOF 提供持久化。",
        "engineering",
    )

    result = service.rag.query(principal, "Spring IoC 依赖注入如何降低 Bean 耦合？")

    assert result["refused"] is False
    assert result["reason"] == "grounded_in_indexed_sources"
    assert "[S1]" in result["answer"]
    assert "未在来源中出现的内容没有被补充推断" in result["answer"]
    assert result["citations"][0]["document_id"] == spring["id"]
    assert result["citations"][0]["filename"] == "spring.md"
    assert result["citations"][0]["heading"] == "Spring IoC"
    assert result["citations"][0]["chunk_id"].startswith("chunk-")
    assert result["citations"][0]["score"] >= result["retrieval"]["threshold"]
    assert result["trace_id"].startswith("trace-")


def test_rag_refuses_when_evidence_is_missing(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    principal = actor()
    upload(
        service,
        principal,
        "spring.txt",
        "Spring IoC 通过依赖注入管理 Bean。",
        "engineering",
    )

    result = service.rag.query(principal, "量子引力中的弦理论有哪些实验结论？")

    assert result["refused"] is True
    assert result["reason"] == "insufficient_evidence"
    assert result["citations"] == []
    assert result["retrieval"]["accepted_count"] == 0


def test_rag_filters_tenants_and_collection_grants(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    upload(service, actor("tenant-a"), "a.txt", "内部代号 Aurora 只属于租户 A。", "secret")
    upload(service, actor("tenant-b"), "b.txt", "内部代号 Borealis 只属于租户 B。", "secret")

    tenant_b_result = service.rag.query(actor("tenant-b"), "内部代号 Aurora 是什么？")
    scoped_result = service.rag.query(
        actor("tenant-a", collections=frozenset({"public"})),
        "内部代号 Aurora 是什么？",
    )

    assert tenant_b_result["refused"] is True
    assert scoped_result["refused"] is True


def test_rag_explicit_collection_scope_changes_result(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    principal = actor()
    upload(service, principal, "public.txt", "发布流程使用蓝绿部署。", "public")
    upload(service, principal, "private.txt", "发布流程必须由安全负责人 Alice 审批。", "private")

    public = service.rag.query(principal, "发布流程如何审批？", collection_ids=["public"])
    private = service.rag.query(principal, "发布流程如何审批？", collection_ids=["private"])

    assert all(item["collection_id"] == "public" for item in public["citations"])
    assert private["refused"] is False
    assert all(item["collection_id"] == "private" for item in private["citations"])
    assert "Alice" in private["answer"]


def test_archived_document_is_excluded_from_retrieval(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    principal = actor()
    document = upload(service, principal, "obsolete.txt", "旧系统代号 LegacyPhoenix。", "engineering")
    before = service.rag.query(principal, "旧系统代号 LegacyPhoenix 是什么？")
    service.documents.archive(principal, document["id"])
    after = service.rag.query(principal, "旧系统代号 LegacyPhoenix 是什么？")

    assert before["refused"] is False
    assert after["refused"] is True


def test_trace_contains_nested_spans_and_metrics_without_prompt_content(tmp_path: Path):
    service = KnowledgeAssistantService(tmp_path / "assistant.db")
    principal = actor()
    upload(service, principal, "metrics.txt", "可观测性记录延迟、Token 和错误率。", "ops")
    result = service.rag.query(principal, "可观测性记录哪些指标？")

    trace = service.traces.get_trace(principal, result["trace_id"])
    metrics = service.traces.metrics(principal)

    assert trace["status"] == "ok"
    assert [span["name"] for span in trace["spans"]] == ["rag.retrieve", "rag.grounding"]
    assert trace["input_tokens"] > 0
    assert trace["output_tokens"] > 0
    assert "query" not in trace["attributes"]
    assert metrics["trace_count"] >= 2
    assert metrics["tokens"]["total"] > 0
    assert metrics["latency_ms"]["max"] >= 0
    assert metrics["operations"]["rag.query"]["count"] == 1
