from __future__ import annotations

from table_miku.knowledge_assistant_batch_summary import (
    summarize_ingestion_batch,
    safe_ingest_filename,
)


def test_safe_filename_strips_windows_and_posix_paths():
    assert safe_ingest_filename(r"D:\vault\secret\notes.md") == "notes.md"
    assert safe_ingest_filename("/tmp/notes.md") == "notes.md"
    assert safe_ingest_filename("") == "未命名文件"


def test_summary_counts_and_failed_lines_without_full_paths():
    summary = summarize_ingestion_batch(
        ("local-1", "local-2", "local-3"),
        {
            "local-1": {
                "local_id": "local-1",
                "status": "succeeded",
                "filename": "ok.md",
            },
            "job:job-2": {
                "local_id": "local-2",
                "job_id": "job-2",
                "status": "failed",
                "filename": r"C:\users\me\docs\bad.md",
                "error_message": "Document validation failed.",
            },
        },
    )
    assert summary.total == 3
    assert summary.succeeded == 1
    assert summary.failed == 1
    assert summary.active == 1
    assert summary.unknown == 0
    text = summary.as_text()
    assert "本批 3 个：成功 1，失败 1" in text
    assert "进行中 1" in text
    assert "bad.md：Document validation failed." in text
    assert "C:\\users" not in text
    assert "local-3" not in text


def test_summary_treats_unknown_and_cancel_states():
    summary = summarize_ingestion_batch(
        ("local-a", "local-b"),
        {
            "local-a": {"local_id": "local-a", "status": "outcome_unknown", "filename": "a.md"},
            "local-b": {"local_id": "local-b", "status": "cancelled", "filename": "b.md"},
        },
    )
    assert summary.unknown == 1
    assert summary.cancelled == 1
    assert summary.failed == 0
    assert "待确认 1" in summary.as_text()
    assert "已取消 1" in summary.as_text()


def test_empty_batch_is_blank():
    assert summarize_ingestion_batch((), {}).as_text() == ""
