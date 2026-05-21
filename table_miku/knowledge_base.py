from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from .storage import read_json, write_json


DEFAULT_COMPUTER_TOPICS = [
    "计算机网络",
    "计算机组成原理",
    "数据结构",
    "操作系统",
    "编译原理",
    "数据库原理",
]

FALLBACK_SUMMARIES = {
    "计算机网络": "计算机网络关注分层模型、协议、寻址、路由、可靠传输、拥塞控制、DNS、HTTP 与网络安全。",
    "计算机组成原理": "计算机组成原理关注 CPU、存储层次、指令系统、流水线、缓存、总线、输入输出与性能评价。",
    "数据结构": "数据结构关注数组、链表、栈、队列、树、图、散列表、堆，以及它们对应的复杂度和典型算法。",
    "操作系统": "操作系统关注进程线程、调度、同步互斥、内存管理、文件系统、I/O、虚拟化与系统调用。",
    "编译原理": "编译原理关注词法分析、语法分析、语义分析、中间表示、优化、代码生成与运行时系统。",
    "数据库原理": "数据库原理关注关系模型、SQL、索引、事务、并发控制、恢复、查询优化和数据库设计。",
}


def load_knowledge() -> list[dict[str, Any]]:
    payload = read_json("knowledge_base.json", [])
    return payload if isinstance(payload, list) else []


def refresh_computer_knowledge(topics: list[str] | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for topic in topics or DEFAULT_COMPUTER_TOPICS:
        records.append(fetch_wikipedia_summary(topic))
    write_json("knowledge_base.json", records)
    return records


def fetch_wikipedia_summary(topic: str) -> dict[str, Any]:
    title = topic.strip()
    url = "https://zh.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Table-Miku/0.6"})
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
        extract = str(payload.get("extract") or "").strip()
        page_url = ((payload.get("content_urls") or {}).get("desktop") or {}).get("page", "")
        return {
            "topic": title,
            "summary": _compact_summary(extract or FALLBACK_SUMMARIES.get(title, "")),
            "source": page_url or url,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "offline": False,
        }
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError):
        return {
            "topic": title,
            "summary": FALLBACK_SUMMARIES.get(title, f"{title} 是计算机基础知识的一部分，建议结合课程目标拆成概念、实践和复盘。"),
            "source": url,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "offline": True,
        }


def knowledge_context(limit: int = 6) -> str:
    records = load_knowledge()
    if not records:
        records = [{"topic": topic, "summary": summary, "offline": True} for topic, summary in FALLBACK_SUMMARIES.items()]
    lines = []
    for record in records[:limit]:
        lines.append(f"{record.get('topic')}：{record.get('summary')}")
    return "计算机知识参考：\n" + "\n".join(lines)


def format_knowledge(records: list[dict[str, Any]] | None = None, limit: int = 12) -> str:
    records = records if records is not None else load_knowledge()
    if not records:
        return "知识库暂时为空。"
    lines = []
    for record in records[:limit]:
        status = "离线备用" if record.get("offline") else "Wikipedia"
        lines.append(f"{record.get('topic')} [{status}]\n{record.get('summary')}")
    return "\n\n".join(lines)


def _compact_summary(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."
