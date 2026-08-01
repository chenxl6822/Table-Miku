import json
import sys
import urllib.error
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
                        "计算机网络是允许节点共享资源的数字通信网络，通过通信链路和分组交换技术实现数据传输，"
                        "是现代信息技术基础设施的核心组成部分，广泛应用于教育、科研、商业和日常生活各个领域。"
                        "\n它通过分层模型组织协议，降低系统设计复杂度，"
                        "每一层为上一层提供服务并隐藏实现细节。"
                        "\n传输层负责端到端通信，提供可靠的数据传输服务，"
                        "通过序列号和确认机制保证数据完整性和顺序。"
                        "\n网络层负责寻址和路由，确定数据包的转发路径，IP协议是网络层的核心。"
                        "\n应用层协议包括超文本传输协议HTTP和域名系统DNS，为用户提供网络应用接口。"
                        "\n网络安全关注身份认证、数据加密和访问控制机制，防火墙和入侵检测系统是常见的安全防护手段。"
                    ),
                }
            ]
        }
    }

    def fake_urlopen(request, timeout):
        url = request.full_url
        if "w/api.php" in url:
            assert "variant=zh-hans" in url
            return FakeResponse(payload)
        # Simulate failure for REST API and English Wikipedia fallback
        raise urllib.error.URLError("test: fallback not available")

    monkeypatch.setattr(knowledge_base.urllib.request, "urlopen", fake_urlopen)

    card = knowledge_base.fetch_wikipedia_summary("计算机网络")

    assert card["offline"] is False
    assert card["source_url"] == "https://zh.wikipedia.org/wiki/计算机网络"
    assert len(card["overview"]) > 30
    assert len(card["sections"]) >= 1
    assert len(card["key_points"]) >= 3
    assert len(card["glossary"]) >= 5
    assert len(card["examples"]) >= 1
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


# ── 新增：多数据源和内容验证测试 ──


def test_english_wikipedia_fallback(monkeypatch):
    """中文 Wikipedia 失败时回退到英文 Wikipedia"""
    en_payload = {
        "title": "Computer_network",
        "extract": (
            "A computer network is a set of computers sharing resources located on or provided "
            "by network nodes. Computers use common communication protocols over digital "
            "interconnections to communicate with each other. These interconnections are made up "
            "of telecommunication network technologies based on physically wired, optical, and "
            "wireless radio-frequency methods that may be arranged in a variety of network "
            "topologies. The nodes of a computer network include personal computers, servers, "
            "networking hardware, or other specialized or general-purpose hosts. They are "
            "identified by network addresses and may have hostnames. Hostnames serve as memorable "
            "labels for the nodes and are rarely changed after initial assignment. Network addresses "
            "serve for locating and identifying the nodes by communication protocols. Computer "
            "networks may be classified by many criteria including transmission medium, bandwidth, "
            "communications protocol, and scale. They support applications such as access to the "
            "World Wide Web, digital video and audio, shared use of application and storage servers, "
            "printers, and fax machines, and use of email and instant messaging applications."
        ),
        "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Computer_network"}},
    }

    def fake_urlopen(request, timeout):
        url = request.full_url
        # zh.wikipedia always fails
        if "zh.wikipedia.org" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        # en.wikipedia succeeds
        if "en.wikipedia.org" in url:
            return FakeResponse(en_payload)
        raise OSError("offline")

    monkeypatch.setattr(knowledge_base.urllib.request, "urlopen", fake_urlopen)

    card = knowledge_base.fetch_wikipedia_summary("计算机网络")
    assert card["offline"] is False
    assert card["source_name"] == "Wikipedia (en)"
    assert "computer network" in card["overview"].lower() or "sharing resources" in card["overview"]


def test_verify_card_content_rejects_empty(monkeypatch):
    """内容验证拒绝信息不足的卡片"""
    # Card with too-short overview should fail verification
    bad_card = {
        "topic": "测试",
        "overview": "太短",
        "key_points": ["点1", "点2"],
        "source_name": "Wikipedia",
    }
    assert knowledge_base._verify_card_content(bad_card) is False

    # Card with enough content should pass
    good_card = {
        "topic": "计算机网络",
        "overview": "计算机网络是连接多台计算机实现资源共享和数据通信的系统。它包含硬件、软件和协议等多个层面，广泛应用于各个行业和领域。计算机网络的核心价值在于让分散的计算设备能够高效协作，是现代信息社会的基础设施。",
        "key_points": ["分层模型降低协议设计复杂度", "IP地址和路由决定数据包去向", "TCP通过序号和确认保证可靠传输", "HTTP和DNS是应用层核心协议", "网络安全关注认证加密和访问控制"],
        "source_name": "Wikipedia",
    }
    assert knowledge_base._verify_card_content(good_card) is True


def test_topic_alias_resolution():
    """验证主题别名映射正确"""
    assert knowledge_base.TOPIC_ALIASES["编译原理"] == "Compiler"
    assert knowledge_base.TOPIC_ALIASES["计算机组成原理"] == "Computer_architecture"
    assert knowledge_base.TOPIC_ALIASES["数据库原理"] == "Database"
    assert knowledge_base.TOPIC_ALIASES["软件工程"] == "Software_engineering"
    assert knowledge_base.TOPIC_ALIASES["分布式系统"] == "Distributed_computing"


def test_decomposed_topic_fetching(monkeypatch):
    """分解查询：主主题失败时通过子主题组装"""
    call_count = [0]

    def fake_urlopen(request, timeout):
        url = request.full_url
        # 直接查询编译原理失败 (percent-encoded)
        if "Compiler" in url or "编译原理" in url or "%E7%BC%96%E8%AF%91%E5%8E%9F%E7%90%86" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        # 子主题返回模拟数据
        call_count[0] += 1
        if "编译器" in url or "%E7%BC%96%E8%AF%91%E5%99%A8" in url:
            return FakeResponse({
                "query": {"pages": [{
                    "title": "编译器",
                    "fullurl": "https://zh.wikipedia.org/wiki/编译器",
                    "extract": "编译器是将高级编程语言编写的源代码翻译为机器语言或中间代码的程序，是软件开发工具链中的核心组件。编译过程通常包括词法分析、语法分析、语义分析、中间代码生成、代码优化和目标代码生成等阶段，每个阶段都有特定的理论和算法支撑。",
                }]}
            })
        if "词法分析" in url or "%E8%AF%8D%E6%B3%95%E5%88%86%E6%9E%90" in url:
            return FakeResponse({
                "query": {"pages": [{
                    "title": "词法分析",
                    "fullurl": "https://zh.wikipedia.org/wiki/词法分析",
                    "extract": "词法分析是编译过程的第一阶段，负责将源代码的字符序列转换为有意义的Token序列。词法分析器使用正则表达式和有限自动机来识别关键字、标识符、运算符和常量等词法单元。",
                }]}
            })
        if "语法分析" in url or "%E8%AF%AD%E6%B3%95%E5%88%86%E6%9E%90" in url:
            return FakeResponse({
                "query": {"pages": [{
                    "title": "语法分析",
                    "fullurl": "https://zh.wikipedia.org/wiki/语法分析",
                    "extract": "语法分析是编译过程的第二阶段，根据程序设计语言的文法规则检查Token序列的合法性并构建语法树。常见的语法分析方法包括自顶向下的递归下降分析和自底向上的LR分析，语法分析的结果是抽象语法树AST。",
                }]}
            })
        # Other sub-topics fail
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(knowledge_base.urllib.request, "urlopen", fake_urlopen)

    card = knowledge_base.fetch_wikipedia_summary("编译原理")
    # Should succeed via decomposition
    assert card["offline"] is False
    assert card["source_name"] == "Wikipedia (decomposed)"
    assert len(card["overview"]) >= 80
    assert len(card["sections"]) >= 1
    # Should have called at least the 3 successful sub-topics
    assert call_count[0] >= 3


def test_cross_validate_low_overlap(monkeypatch):
    """交叉验证：内容与fallback重叠率低时标记"""
    card = {
        "topic": "计算机网络",
        "overview": "一些完全无关的内容描述，与计算机网络没有任何关联。只是随机文本填充。",
        "key_points": ["天气很好", "今天吃了饭", "这是一条测试"],
        "source_name": "Wikipedia",
    }
    result = knowledge_base._cross_validate_card(card)
    assert result["encoding_status"] == "low_confidence"
    assert "重叠率低" in result.get("validation_note", "")


def test_new_topics_have_fallback():
    """验证4个新增主题都有 fallback 数据"""
    for topic in ["软件工程", "算法设计与分析", "计算机安全", "分布式系统"]:
        fb = knowledge_base.FALLBACK_KNOWLEDGE.get(topic)
        assert fb is not None, f"Missing fallback for {topic}"
        assert len(fb["overview"]) >= 60
        assert len(fb["key_points"]) >= 4
        assert len(fb["glossary"]) >= 4
        assert len(fb["review_questions"]) >= 2
        assert len(fb["examples"]) >= 1


def test_practical_topics_have_fallback_and_qa_pairs():
    """Java/Go/架构主题必须能离线进入知识库和复习答案页。"""
    for topic in knowledge_base.PRACTICAL_TOPICS:
        card = knowledge_base._fallback_card(topic)
        assert card["topic"] == topic
        assert len(card["overview"]) >= 60
        assert len(card["key_points"]) >= 4
        assert len(card["review_questions"]) >= 3
        assert len(card["qa_pairs"]) == len(card["review_questions"])
        for pair in card["qa_pairs"]:
            assert pair["question"]
            assert pair["answer"]


def test_load_knowledge_auto_appends_missing_long_term_topics(monkeypatch):
    """旧 JSON 只有 6 个主题时，读取层也要补齐长期主题。"""
    old_payload = [
        knowledge_base._fallback_card(topic)
        for topic in knowledge_base.DEFAULT_COMPUTER_TOPICS[:6]
    ]
    monkeypatch.setattr(knowledge_base, "read_json", lambda filename, default: old_payload)

    records = knowledge_base.load_knowledge()
    topics = {record["topic"] for record in records}

    for topic in knowledge_base.DEFAULT_KNOWLEDGE_TOPICS:
        assert topic in topics
