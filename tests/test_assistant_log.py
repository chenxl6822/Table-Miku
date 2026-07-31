from __future__ import annotations

import json

from table_miku import assistant_log


def test_append_event_redacts_secrets(tmp_path, monkeypatch):
    log_path = tmp_path / "assistant_events.jsonl"
    monkeypatch.setattr(assistant_log, "runtime_path", lambda _filename: log_path)

    assistant_log.append_event(
        "test",
        "secret check",
        "OPENAI_API_KEY=sk-example123456 Bearer abc.def.ghi",
        {"password": "do-not-store", "nested": {"access_token": "token-value"}},
    )

    raw = log_path.read_text(encoding="utf-8")
    record = json.loads(raw)
    assert "sk-example123456" not in raw
    assert "abc.def.ghi" not in raw
    assert "do-not-store" not in raw
    assert "token-value" not in raw
    assert record["payload"]["password"] == "[REDACTED]"


def test_append_event_rotates_bounded_log(tmp_path, monkeypatch):
    log_path = tmp_path / "assistant_events.jsonl"
    monkeypatch.setattr(assistant_log, "runtime_path", lambda _filename: log_path)
    monkeypatch.setattr(assistant_log, "LOG_MAX_BYTES", 120)

    assistant_log.append_event("test", "first", "x" * 100)
    assistant_log.append_event("test", "second", "y" * 100)

    assert log_path.exists()
    assert log_path.with_name("assistant_events.jsonl.1").exists()
    assert "second" in log_path.read_text(encoding="utf-8")
