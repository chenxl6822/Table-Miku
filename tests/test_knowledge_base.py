import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_base


class FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._raw


def test_fetch_mediawiki_builds_structured_card(monkeypatch):
    payload = {
        "query": {
            "pages": [
                {
                    "title": "计算机网络",
                    "fullurl": "https://zh.wikipedia.org/wiki/计算机网络",
                    "extract": (
                        "计算机网络是允许节点共享资源的数字通信网络。"
                        "它通过分层模型组织协议。"
                        "传输层负责端到端通信。"
                        "网络层负责寻址和路由。"
                        "应用层协议包括 HTTP 和 DNS。"
                        "网络安全关注认证、加密和访问控制。"
                    ),
                }
            ]
        }
    }

    def fake_urlopen(request, timeout):
        assert "w/api.php" in request.full_url
        assert "variant=zh-hans" in request.full_url
        return FakeResponse(payload)

    monkeypatch.setattr(knowledge_base.urllib.request, "urlopen", fake_urlopen)

    card = knowledge_base.fetch_wikipedia_summary("计算机网络")

    assert card["offline"] is False
    assert card["encoding_status"] == "ok"
    assert card["source_url"] == "https://zh.wikipedia.org/wiki/计算机网络"
    assert len(card["overview"]) > 30
    assert len(card["sections"]) >= 3
    assert len(card["key_points"]) >= 5
    assert len(card["glossary"]) >= 5
    assert len(card["examples"]) >= 2
    assert len(card["review_questions"]) >= 3


def test_network_failure_uses_structured_fallback(monkeypatch):
    def fake_urlopen(request, timeout):
        raise OSError("offline")

    monkeypatch.setattr(knowledge_base.urllib.request, "urlopen", fake_urlopen)

    card = knowledge_base.fetch_wikipedia_summary("操作系统")

    assert card["offline"] is True
    assert card["source_name"] == "offline"
    assert len(card["sections"]) >= 3
    assert len(card["key_points"]) >= 5
    assert len(card["review_questions"]) >= 3
    assert "进程" in knowledge_base.format_knowledge([card])


def test_legacy_record_is_migrated_to_card():
    legacy = {
        "topic": "数据结构",
        "summary": "数据结构是在计算机中存储、组织数据的方式。",
        "source": "https://example.test/wiki",
        "offline": False,
    }

    card = knowledge_base.migrate_legacy_record(legacy)

    assert card["topic"] == "数据结构"
    assert card["summary"] == card["overview"]
    assert card["source_url"] == "https://example.test/wiki"
    assert len(card["key_points"]) >= 5
    assert len(card["glossary"]) >= 5


def test_context_is_compact_but_structured():
    card = knowledge_base.migrate_legacy_record(
        {"topic": "数据库原理", "summary": "数据库原理关注 SQL、索引和事务。", "offline": True}
    )

    context = knowledge_base.compact_card_for_context(card)

    assert context.startswith("数据库原理：")
    assert "要点：" in context
    assert "SQL" in context or "索引" in context
