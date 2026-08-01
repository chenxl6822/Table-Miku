import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import storage


def test_load_settings_appends_required_knowledge_topics(monkeypatch):
    old_settings = {
        "knowledge": {
            "enabled": True,
            "topics": ["计算机网络", "计算机组成原理", "自定义主题"],
        }
    }
    monkeypatch.setattr(storage, "read_json", lambda filename, default: old_settings)

    settings = storage.load_settings()
    topics = settings["knowledge"]["topics"]

    for topic in storage.DEFAULT_KNOWLEDGE_TOPICS:
        assert topic in topics
    assert "自定义主题" in topics
    assert topics.index("计算机网络") < topics.index("Java 后端基础")
