from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from .encoding_utils import looks_mojibake, normalize_zh_text, repair_mojibake
from .storage import read_json, write_json


DEFAULT_COMPUTER_TOPICS = [
    "计算机网络",
    "计算机组成原理",
    "数据结构",
    "操作系统",
    "编译原理",
    "数据库原理",
]

WIKIPEDIA_API = "https://zh.wikipedia.org/w/api.php"
WIKIPEDIA_REST_SUMMARY = "https://zh.wikipedia.org/api/rest_v1/page/summary/"
USER_AGENT = "Table-Miku/0.7 (knowledge hotfix)"

FALLBACK_KNOWLEDGE: dict[str, dict[str, Any]] = {
    "计算机网络": {
        "overview": "计算机网络研究计算机与网络设备如何通过通信链路交换数据。学习时应先掌握分层模型，再理解寻址、路由、可靠传输、拥塞控制、DNS、HTTP 与网络安全之间的关系。",
        "sections": [
            {"heading": "分层模型", "content": "常用视角包括 OSI 七层模型和 TCP/IP 四层模型。分层能把复杂通信拆成应用、传输、网络、链路等职责。"},
            {"heading": "核心协议", "content": "IP 负责寻址与路由，TCP 负责可靠传输，UDP 提供低开销传输，HTTP 负责 Web 应用层交互。"},
            {"heading": "工程实践", "content": "排查网络问题时通常从 DNS、连通性、端口、防火墙、代理和应用日志逐层定位。"},
        ],
        "key_points": ["分层模型降低协议设计复杂度", "IP 地址和路由决定数据包去向", "TCP 通过序号、确认和重传保证可靠性", "DNS 把域名解析为地址", "HTTPS 在 HTTP 之上加入加密和身份校验"],
        "glossary": [
            {"term": "TCP", "explanation": "面向连接的可靠传输协议。"},
            {"term": "UDP", "explanation": "无连接、低开销但不保证可靠性的传输协议。"},
            {"term": "DNS", "explanation": "把域名解析为 IP 地址的系统。"},
            {"term": "路由", "explanation": "为数据包选择转发路径的过程。"},
            {"term": "拥塞控制", "explanation": "避免网络过载并调节发送速率的机制。"},
        ],
        "examples": ["浏览器访问网站时会经历 DNS 解析、TCP/TLS 建连、HTTP 请求和响应。", "如果百度可访问但 Google 不可访问，常见原因包括代理、DNS 或出口网络限制。"],
        "review_questions": ["TCP 为什么需要三次握手？", "DNS 解析失败会怎样影响 Web 访问？", "HTTP 和 HTTPS 的核心区别是什么？"],
    },
    "计算机组成原理": {
        "overview": "计算机组成原理关注计算机硬件系统如何协同完成计算任务，核心包括 CPU、存储层次、指令系统、流水线、总线、输入输出和性能评价。",
        "sections": [
            {"heading": "CPU 与指令", "content": "CPU 通过取指、译码、执行等步骤运行指令，指令系统定义软件和硬件之间的基本接口。"},
            {"heading": "存储层次", "content": "寄存器、缓存、内存和外存容量递增、速度递减，通过局部性原理提升整体性能。"},
            {"heading": "性能评价", "content": "性能与时钟周期、CPI、指令数、缓存命中率和 I/O 等因素有关。"},
        ],
        "key_points": ["指令系统是硬件向软件暴露的抽象", "缓存利用时间局部性和空间局部性", "流水线提高吞吐但可能产生冲突", "总线连接各硬件部件", "I/O 性能会显著影响系统体验"],
        "glossary": [
            {"term": "CPU", "explanation": "执行指令和控制计算流程的核心部件。"},
            {"term": "缓存", "explanation": "位于 CPU 和内存之间的高速存储。"},
            {"term": "流水线", "explanation": "把指令执行拆成多个阶段并重叠处理。"},
            {"term": "CPI", "explanation": "平均每条指令需要的时钟周期数。"},
            {"term": "总线", "explanation": "部件之间传输数据、地址和控制信号的通道。"},
        ],
        "examples": ["缓存命中率升高通常会减少访问内存的等待时间。", "流水线遇到数据相关时可能需要暂停或转发。"],
        "review_questions": ["为什么缓存能提升性能？", "流水线有哪些典型冲突？", "如何用 CPI 估算程序执行时间？"],
    },
    "数据结构": {
        "overview": "数据结构研究数据的组织方式及其操作效率。学习重点是理解数组、链表、栈、队列、树、图、散列表和堆的适用场景，并能用时间复杂度和空间复杂度评估方案。",
        "sections": [
            {"heading": "线性结构", "content": "数组适合随机访问，链表适合局部插入删除，栈和队列适合约束访问顺序。"},
            {"heading": "树与图", "content": "树常用于层级组织和搜索，图适合表达关系网络、路径和依赖。"},
            {"heading": "复杂度", "content": "复杂度用于描述输入规模增长时算法成本的增长趋势。"},
        ],
        "key_points": ["数组随机访问快但中间插入成本高", "链表插入删除灵活但访问需要遍历", "栈是后进先出", "队列是先进先出", "散列表用哈希函数换取近似常数查询"],
        "glossary": [
            {"term": "数组", "explanation": "连续存储、支持下标访问的数据结构。"},
            {"term": "链表", "explanation": "通过指针连接节点的数据结构。"},
            {"term": "栈", "explanation": "后进先出的线性结构。"},
            {"term": "图", "explanation": "由顶点和边构成的关系结构。"},
            {"term": "堆", "explanation": "常用于优先队列的完全二叉树结构。"},
        ],
        "examples": ["括号匹配可以用栈解决。", "最短路径问题通常建模为图问题。"],
        "review_questions": ["数组和链表的核心取舍是什么？", "什么场景适合使用栈？", "哈希冲突可以如何处理？"],
    },
    "操作系统": {
        "overview": "操作系统管理硬件资源并为应用提供抽象接口。核心内容包括进程线程、调度、同步互斥、内存管理、文件系统、I/O、虚拟化和系统调用。",
        "sections": [
            {"heading": "进程与线程", "content": "进程是资源分配单位，线程是调度执行单位，同一进程内线程共享部分资源。"},
            {"heading": "内存管理", "content": "虚拟内存把程序看到的地址空间与物理内存隔离，并支持分页、换页和保护。"},
            {"heading": "并发控制", "content": "锁、信号量和条件变量用于协调并发访问，避免竞态条件。"},
        ],
        "key_points": ["进程隔离提升安全性和稳定性", "线程切换通常比进程切换轻量", "调度算法影响响应时间和吞吐量", "虚拟内存提供地址空间抽象", "死锁需要同时满足四个必要条件"],
        "glossary": [
            {"term": "进程", "explanation": "拥有独立资源的程序运行实例。"},
            {"term": "线程", "explanation": "进程内的执行流。"},
            {"term": "虚拟内存", "explanation": "为程序提供连续地址空间的内存抽象。"},
            {"term": "系统调用", "explanation": "用户程序请求内核服务的接口。"},
            {"term": "死锁", "explanation": "多个任务互相等待资源而无法继续执行。"},
        ],
        "examples": ["多个线程同时修改同一变量时需要锁保护。", "内存不足时系统可能把部分页面换出到磁盘。"],
        "review_questions": ["进程和线程有什么区别？", "虚拟内存解决了什么问题？", "死锁的四个必要条件是什么？"],
    },
    "编译原理": {
        "overview": "编译原理研究如何把源程序转换为目标代码。典型流程包括词法分析、语法分析、语义分析、中间表示、优化、代码生成和运行时系统。",
        "sections": [
            {"heading": "前端分析", "content": "词法分析把字符流变成记号，语法分析按文法构建结构，语义分析检查类型和作用域。"},
            {"heading": "中间表示", "content": "中间表示让优化和后端生成更独立，常见形式包括三地址码和语法树。"},
            {"heading": "代码生成", "content": "后端把中间表示转换为机器相关代码，并处理寄存器分配等问题。"},
        ],
        "key_points": ["词法分析关注 token", "语法分析关注结构", "语义分析关注含义和约束", "中间表示降低前后端耦合", "优化需要在正确性和性能之间取舍"],
        "glossary": [
            {"term": "Token", "explanation": "词法分析输出的记号。"},
            {"term": "AST", "explanation": "抽象语法树，用于表示程序结构。"},
            {"term": "IR", "explanation": "中间表示，连接编译前端和后端。"},
            {"term": "类型检查", "explanation": "验证表达式和语句是否满足类型规则。"},
            {"term": "寄存器分配", "explanation": "决定哪些变量放入 CPU 寄存器。"},
        ],
        "examples": ["表达式 a + b * c 的语法树会体现乘法优先级。", "常量折叠会把 2 + 3 优化为 5。"],
        "review_questions": ["词法分析和语法分析的区别是什么？", "为什么需要中间表示？", "编译优化为什么不能改变语义？"],
    },
    "数据库原理": {
        "overview": "数据库原理关注数据如何被建模、存储、查询和保护。核心包括关系模型、SQL、索引、事务、并发控制、恢复、查询优化和数据库设计。",
        "sections": [
            {"heading": "关系模型", "content": "关系模型用表、行、列、键和约束组织数据，SQL 是主要查询语言。"},
            {"heading": "事务", "content": "事务提供 ACID 语义，保证一组操作在并发和故障场景下仍可控。"},
            {"heading": "索引与优化", "content": "索引提升查询速度，但会增加写入和维护成本，查询优化器负责选择执行计划。"},
        ],
        "key_points": ["主键唯一标识一行数据", "索引适合高频查询字段", "事务具有原子性、一致性、隔离性和持久性", "隔离级别影响并发现象", "规范化能减少冗余但可能增加连接成本"],
        "glossary": [
            {"term": "SQL", "explanation": "关系数据库的结构化查询语言。"},
            {"term": "索引", "explanation": "加速查询的数据结构。"},
            {"term": "事务", "explanation": "作为一个逻辑单元执行的一组数据库操作。"},
            {"term": "ACID", "explanation": "事务的四个关键性质。"},
            {"term": "查询优化器", "explanation": "选择较优 SQL 执行计划的组件。"},
        ],
        "examples": ["给用户表 email 字段建立唯一索引可以加速登录查询并避免重复。", "转账操作需要事务保证扣款和入账同时成功或同时失败。"],
        "review_questions": ["索引为什么会降低写入性能？", "事务 ACID 分别代表什么？", "什么情况下需要调整隔离级别？"],
    },
}

FALLBACK_SUMMARIES = {topic: data["overview"] for topic, data in FALLBACK_KNOWLEDGE.items()}


def load_knowledge() -> list[dict[str, Any]]:
    payload = read_json("knowledge_base.json", [])
    if not isinstance(payload, list):
        return []
    return [migrate_legacy_record(record) for record in payload if isinstance(record, dict)]


def refresh_computer_knowledge(topics: list[str] | None = None) -> list[dict[str, Any]]:
    records = [fetch_wikipedia_summary(topic) for topic in topics or DEFAULT_COMPUTER_TOPICS]
    write_json("knowledge_base.json", records)
    return records


def fetch_wikipedia_summary(topic: str) -> dict[str, Any]:
    title = normalize_zh_text(topic)
    try:
        page = _fetch_mediawiki_extract(title)
        return build_knowledge_card(title, page["extract"], page["url"], offline=False, source="Wikipedia")
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        try:
            page = _fetch_rest_summary(title)
            return build_knowledge_card(title, page["extract"], page["url"], offline=False, source="Wikipedia")
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError, UnicodeDecodeError, KeyError):
            return _fallback_card(title)


def build_knowledge_card(topic: str, raw_text: str, source_url: str, offline: bool, source: str = "Wikipedia") -> dict[str, Any]:
    raw_cleaned = str(raw_text).replace("\ufeff", "").replace("\u3000", " ")
    repaired_text, repaired = repair_mojibake(raw_cleaned)
    cleaned = normalize_zh_text(repaired_text)
    fallback = FALLBACK_KNOWLEDGE.get(topic, _generic_fallback(topic))

    paragraphs = _paragraphs(cleaned)
    overview = _clip(" ".join(paragraphs[:2]) if paragraphs else fallback["overview"], 520)
    if len(overview) < 80:
        overview = fallback["overview"]

    sections = _sections_from_text(paragraphs, fallback["sections"])
    key_points = _sentences_to_items(cleaned, minimum=5, fallback=fallback["key_points"])
    glossary = _glossary_for_topic(topic, fallback["glossary"])
    examples = _merge_items(_sentences_to_items(cleaned, minimum=2, fallback=[])[-2:], fallback["examples"], 2)
    review_questions = fallback["review_questions"]
    encoding_status = "repaired" if repaired else ("suspicious" if looks_mojibake(cleaned) else "ok")

    return {
        "id": _card_id(topic),
        "topic": topic,
        "title": topic,
        "source": source_url or source,
        "source_name": source,
        "source_url": source_url,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "offline": offline,
        "encoding_status": encoding_status,
        "overview": overview,
        "summary": overview,
        "sections": sections,
        "key_points": key_points,
        "glossary": glossary,
        "examples": examples,
        "review_questions": review_questions,
        "raw_excerpt": _clip(cleaned, 1200),
    }


def migrate_legacy_record(record: dict[str, Any]) -> dict[str, Any]:
    if "overview" in record and "key_points" in record:
        migrated = dict(record)
        migrated.setdefault("summary", migrated.get("overview", ""))
        migrated.setdefault("source_url", migrated.get("source", ""))
        migrated.setdefault("source_name", "Wikipedia" if not migrated.get("offline") else "offline")
        migrated.setdefault("encoding_status", "suspicious" if looks_mojibake(str(migrated.get("overview", ""))) else "ok")
        return migrated

    topic = normalize_zh_text(str(record.get("topic") or "知识点"))
    summary = str(record.get("summary") or "")
    source = str(record.get("source") or "")
    offline = bool(record.get("offline", True))
    return build_knowledge_card(topic, summary, source, offline=offline, source="offline" if offline else "Wikipedia")


def compact_card_for_context(card: dict[str, Any]) -> str:
    migrated = migrate_legacy_record(card)
    points = "；".join(str(point) for point in migrated.get("key_points", [])[:4])
    return f"{migrated.get('topic')}：{migrated.get('overview')}\n要点：{points}"


def knowledge_context(limit: int = 6) -> str:
    records = load_knowledge()
    if not records:
        records = [_fallback_card(topic) for topic in DEFAULT_COMPUTER_TOPICS]
    lines = [compact_card_for_context(record) for record in records[:limit]]
    return "计算机知识参考：\n" + "\n".join(lines)


def format_knowledge(records: list[dict[str, Any]] | None = None, limit: int = 12) -> str:
    records = records if records is not None else load_knowledge()
    if not records:
        return "知识库暂时为空。"

    blocks: list[str] = []
    for raw_record in records[:limit]:
        record = migrate_legacy_record(raw_record)
        status = "离线备用" if record.get("offline") else str(record.get("source_name") or "Wikipedia")
        lines = [f"{record.get('topic')} [{status}]", str(record.get("overview", ""))]

        key_points = record.get("key_points") or []
        if key_points:
            lines.append("关键点：")
            lines.extend(f"- {point}" for point in key_points[:6])

        sections = record.get("sections") or []
        if sections:
            lines.append("知识结构：")
            for section in sections[:4]:
                heading = section.get("heading", "小节") if isinstance(section, dict) else "小节"
                content = section.get("content", "") if isinstance(section, dict) else str(section)
                lines.append(f"- {heading}：{content}")

        glossary = record.get("glossary") or []
        if glossary:
            terms = []
            for item in glossary[:5]:
                if isinstance(item, dict):
                    terms.append(f"{item.get('term')}={item.get('explanation')}")
            if terms:
                lines.append("术语：" + "；".join(terms))

        examples = record.get("examples") or []
        if examples:
            lines.append("例子：" + "；".join(str(example) for example in examples[:2]))

        questions = record.get("review_questions") or []
        if questions:
            lines.append("复习问题：" + "；".join(str(question) for question in questions[:3]))

        source_url = record.get("source_url") or record.get("source")
        if source_url:
            lines.append(f"来源：{source_url}")
        blocks.append("\n".join(line for line in lines if line))
    return "\n\n".join(blocks)


def _fetch_mediawiki_extract(title: str) -> dict[str, str]:
    params = {
        "action": "query",
        "prop": "extracts|info",
        "explaintext": "1",
        "redirects": "1",
        "format": "json",
        "formatversion": "2",
        "utf8": "1",
        "variant": "zh-hans",
        "inprop": "url",
        "titles": title,
    }
    payload = _get_json(WIKIPEDIA_API + "?" + urllib.parse.urlencode(params))
    pages = ((payload.get("query") or {}).get("pages") or [])
    if not pages:
        raise KeyError("pages")
    page = pages[0]
    extract = normalize_zh_text(str(page.get("extract") or ""))
    if not extract:
        raise KeyError("extract")
    return {"extract": extract, "url": str(page.get("fullurl") or "")}


def _fetch_rest_summary(title: str) -> dict[str, str]:
    payload = _get_json(WIKIPEDIA_REST_SUMMARY + urllib.parse.quote(title))
    extract = normalize_zh_text(str(payload.get("extract") or ""))
    if not extract:
        raise KeyError("extract")
    page_url = ((payload.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    return {"extract": extract, "url": str(page_url)}


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json; charset=utf-8",
            "Accept-Language": "zh-Hans-CN, zh-CN, zh, en",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def _fallback_card(topic: str) -> dict[str, Any]:
    fallback = FALLBACK_KNOWLEDGE.get(topic, _generic_fallback(topic))
    card = build_knowledge_card(topic, fallback["overview"], "", offline=True, source="offline")
    card["sections"] = fallback["sections"]
    card["key_points"] = fallback["key_points"]
    card["glossary"] = fallback["glossary"]
    card["examples"] = fallback["examples"]
    card["review_questions"] = fallback["review_questions"]
    card["source"] = "offline"
    card["source_name"] = "offline"
    return card


def _generic_fallback(topic: str) -> dict[str, Any]:
    return {
        "overview": f"{topic} 是计算机基础知识的一部分。建议从核心概念、常见问题、实践例子和复习问题四个角度整理学习。",
        "sections": [
            {"heading": "核心概念", "content": f"先明确 {topic} 的定义、适用场景和边界。"},
            {"heading": "实践关联", "content": "把概念和项目、调试、面试题或课程作业联系起来。"},
            {"heading": "复习方式", "content": "用关键点、术语和自测问题定期复盘。"},
        ],
        "key_points": [f"理解 {topic} 的基本定义", "整理常见术语", "结合例子练习", "记录易错点", "定期复习巩固"],
        "glossary": [
            {"term": "概念", "explanation": "该主题的基础定义。"},
            {"term": "场景", "explanation": "该知识适用的问题类型。"},
            {"term": "约束", "explanation": "使用该知识时需要注意的限制。"},
            {"term": "实践", "explanation": "把知识用于真实任务。"},
            {"term": "复盘", "explanation": "学习后回顾掌握情况。"},
        ],
        "examples": [f"把 {topic} 整理成一张知识卡片。", "用一道面试题验证自己是否真正理解。"],
        "review_questions": [f"{topic} 的核心定义是什么？", f"{topic} 能解决什么问题？", f"学习 {topic} 时最容易混淆什么？"],
    }


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n+|(?<=。)", text) if part.strip()]


def _sections_from_text(paragraphs: list[str], fallback: list[dict[str, str]]) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for index, paragraph in enumerate(paragraphs[2:8], start=1):
        content = _clip(paragraph, 220)
        if len(content) >= 25:
            sections.append({"heading": f"知识片段 {index}", "content": content})
        if len(sections) >= 3:
            break
    return sections or fallback


def _sentences_to_items(text: str, minimum: int, fallback: list[str]) -> list[str]:
    sentences = [_clip(item.strip(), 120) for item in re.split(r"[。！？!?]\s*", text) if len(item.strip()) >= 12]
    return _merge_items(sentences, fallback, minimum)


def _merge_items(primary: list[str], fallback: list[str], minimum: int) -> list[str]:
    merged: list[str] = []
    for item in primary + fallback:
        cleaned = normalize_zh_text(str(item))
        if cleaned and cleaned not in merged:
            merged.append(cleaned)
        if len(merged) >= max(minimum, 5 if fallback else minimum):
            break
    return merged[: max(minimum, len(merged))]


def _glossary_for_topic(topic: str, fallback: list[dict[str, str]]) -> list[dict[str, str]]:
    return fallback if len(fallback) >= 5 else _generic_fallback(topic)["glossary"]


def _clip(text: str, limit: int) -> str:
    compact = " ".join(normalize_zh_text(text).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _card_id(topic: str) -> str:
    slug = urllib.parse.quote(topic, safe="")
    return f"wiki-{slug}"
