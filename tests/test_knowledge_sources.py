"""Tests for knowledge_sources — adapter interface and Wikipedia mock."""

import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku import knowledge_sources


class FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._raw


class TestWikipediaSource:
    def test_source_has_name_and_kind(self):
        src = knowledge_sources.WikipediaSource("zh")
        assert src.name == "Wikipedia"
        assert src.kind == "wikipedia"

    def test_search_returns_results(self, monkeypatch):
        def fake_urlopen(request, timeout):
            url = request.full_url
            if "list=search" in url:
                return FakeResponse({
                    "query": {
                        "search": [
                            {"title": "计算机网络", "snippet": "计算机网络是..."},
                            {"title": "TCP/IP协议", "snippet": "TCP/IP是..."},
                        ]
                    }
                })
            raise urllib.error.URLError("unexpected")

        monkeypatch.setattr(knowledge_sources.urllib.request, "urlopen", fake_urlopen)

        src = knowledge_sources.WikipediaSource("zh")
        results = src.search("计算机网络", limit=5)
        assert len(results) == 2
        assert results[0]["title"] == "计算机网络"
        assert "snippet" in results[0]
        assert "url" in results[0]

    def test_search_handles_network_error(self, monkeypatch):
        def fake_urlopen(request, timeout):
            raise OSError("offline")

        monkeypatch.setattr(knowledge_sources.urllib.request, "urlopen", fake_urlopen)

        src = knowledge_sources.WikipediaSource("zh")
        results = src.search("计算机网络")
        assert results == []

    def test_fetch_returns_full_record(self, monkeypatch):
        def fake_urlopen(request, timeout):
            url = request.full_url
            if "w/api.php" in url:
                return FakeResponse({
                    "query": {
                        "pages": [{
                            "title": "计算机网络",
                            "fullurl": "https://zh.wikipedia.org/wiki/计算机网络",
                            "extract": "计算机网络是允许节点共享资源的数字通信网络。它通过分层模型组织协议，是现代信息技术基础设施的核心组成部分。",
                        }]
                    }
                })
            raise urllib.error.URLError("fallback not needed")

        monkeypatch.setattr(knowledge_sources.urllib.request, "urlopen", fake_urlopen)

        src = knowledge_sources.WikipediaSource("zh")
        record = src.fetch({"title": "计算机网络"})
        assert record["title"] == "计算机网络"
        assert len(record["overview"]) > 30
        assert "url" in record

    def test_fetch_fails_gracefully(self, monkeypatch):
        def fake_urlopen(request, timeout):
            raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)

        monkeypatch.setattr(knowledge_sources.urllib.request, "urlopen", fake_urlopen)

        src = knowledge_sources.WikipediaSource("zh")
        try:
            src.fetch({"title": "完全不存在的页面XYZ"})
            assert False, "Should have raised"
        except RuntimeError:
            pass

    def test_fetch_requires_title(self):
        src = knowledge_sources.WikipediaSource("zh")
        try:
            src.fetch({"url": "https://..."})
            assert False, "Should have raised"
        except ValueError:
            pass


class TestSourceRegistry:
    def test_wikipedia_registered_by_default(self):
        src = knowledge_sources.get_source("Wikipedia")
        assert src is not None
        assert src.name == "Wikipedia"

    def test_list_sources(self):
        names = knowledge_sources.list_sources()
        assert "Wikipedia" in names

    def test_register_custom_source(self):
        class TestSource(knowledge_sources.KnowledgeSource):
            name = "TestSource"
            kind = "test"

            def search(self, query, limit=5):
                return [{"title": query, "url": "", "snippet": ""}]

            def fetch(self, ref):
                return {"title": ref["title"], "url": "", "overview": "", "raw": ""}

        knowledge_sources.register_source(TestSource())
        src = knowledge_sources.get_source("TestSource")
        assert src is not None
        assert src.name == "TestSource"
