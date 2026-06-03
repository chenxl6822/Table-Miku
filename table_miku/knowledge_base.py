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
    "软件工程",
    "算法设计与分析",
    "计算机安全",
    "分布式系统",
]

# 中文主题 → 英文 Wikipedia 页面名映射（当中文 Wikipedia 失败时回退）
TOPIC_ALIASES: dict[str, str] = {
    "计算机组成原理": "Computer_architecture",
    "编译原理": "Compiler",
    "数据库原理": "Database",
    "数据结构": "Data_structure",
    "操作系统": "Operating_system",
    "计算机网络": "Computer_network",
    "软件工程": "Software_engineering",
    "算法设计与分析": "Analysis_of_algorithms",
    "计算机安全": "Computer_security",
    "分布式系统": "Distributed_computing",
}

# 主题分解：当主主题获取失败时，拆成子主题分别查询
TOPIC_DECOMPOSITION: dict[str, list[str]] = {
    "编译原理": ["编译器", "编译过程", "词法分析", "语法分析", "中间代码生成", "代码优化", "目标代码生成"],
    "计算机组成原理": ["CPU", "存储器层次结构", "指令系统", "总线", "输入输出系统", "计算机体系结构"],
    "数据库原理": ["关系数据库", "SQL", "事务", "索引", "数据库范式", "查询优化"],
    "操作系统": ["进程管理", "内存管理", "文件系统", "死锁", "调度算法", "虚拟内存"],
    "计算机网络": ["TCP/IP", "HTTP协议", "DNS", "路由算法", "网络分层", "拥塞控制"],
    "数据结构": ["数组", "链表", "栈", "队列", "树", "图", "哈希表", "排序算法"],
    "软件工程": ["软件生命周期", "设计模式", "敏捷开发", "测试驱动开发", "版本控制"],
    "算法设计与分析": ["动态规划", "贪心算法", "分治法", "图算法", "NP完全性", "近似算法"],
    "计算机安全": ["加密算法", "网络安全", "认证与授权", "漏洞与攻击", "安全协议"],
    "分布式系统": ["CAP定理", "一致性算法", "分布式事务", "微服务", "消息队列"],
}

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
    "软件工程": {
        "overview": "软件工程是用工程化方法开发、运行和维护软件系统的学科，涵盖需求分析、设计、编码、测试、部署和维护全生命周期。核心目标是在成本、时间和质量之间取得平衡。",
        "sections": [
            {"heading": "软件生命周期", "content": "典型阶段包括需求分析、系统设计、编码实现、测试验证、部署上线和运维迭代，各阶段有对应的文档和评审。"},
            {"heading": "设计模式", "content": "常用设计模式如单例、工厂、观察者、策略等，提供可复用的设计经验，解决特定上下文中的常见问题。"},
            {"heading": "开发方法", "content": "瀑布模型强调阶段顺序推进，敏捷开发强调迭代交付和快速反馈，Scrum 和 Kanban 是常见敏捷框架。"},
        ],
        "key_points": ["软件生命周期覆盖从需求到运维全过程", "设计模式是可复用的设计经验", "敏捷开发注重快速迭代和持续反馈", "测试驱动开发以测试为先导编写代码", "版本控制是团队协作的基础"],
        "glossary": [
            {"term": "敏捷开发", "explanation": "以迭代为核心、快速响应变化的软件开发方法。"},
            {"term": "设计模式", "explanation": "针对特定问题的可复用设计方案。"},
            {"term": "CI/CD", "explanation": "持续集成与持续部署，自动化构建、测试与发布。"},
            {"term": "重构", "explanation": "在不改变外部行为的前提下改善代码内部结构。"},
            {"term": "技术债务", "explanation": "为快速交付而做出的妥协，后续需要偿还的代码质量代价。"},
        ],
        "examples": ["用户故事描述功能需求，驱动迭代开发计划。", "Git 分支策略如 GitFlow 规范团队协作流程。"],
        "review_questions": ["瀑布模型和敏捷开发的核心区别是什么？", "设计模式解决了什么问题？", "持续集成为什么能降低集成风险？"],
    },
    "算法设计与分析": {
        "overview": "算法设计与分析研究如何设计高效算法并分析其时间与空间复杂度。核心内容包括分治法、动态规划、贪心算法、图算法、NP 完全性理论和近似算法。",
        "sections": [
            {"heading": "算法设计范式", "content": "分治法将问题拆分为子问题递归求解；动态规划利用子问题重叠和最优子结构；贪心算法在每步做局部最优选择。"},
            {"heading": "复杂度分析", "content": "用大 O 记号描述最坏情况增长趋势；P 类问题可在多项式时间内求解，NP 类问题可在多项式时间内验证。"},
            {"heading": "经典算法", "content": "排序算法、最短路径（Dijkstra、Bellman-Ford）、最小生成树（Prim、Kruskal）、网络流等是面试和竞赛高频考点。"},
        ],
        "key_points": ["时间复杂度衡量算法随输入增长的成本", "分治法适合可分解且子问题独立的问题", "动态规划解决有重叠子问题的最优化问题", "贪心算法依赖贪心选择性质", "NP 完全问题目前没有多项式解法"],
        "glossary": [
            {"term": "大O记号", "explanation": "描述算法时间复杂度上界的数学记号。"},
            {"term": "动态规划", "explanation": "将原问题分解为重叠子问题并保存中间结果的算法范式。"},
            {"term": "贪心算法", "explanation": "每步做局部最优选择，期望得到全局最优的算法。"},
            {"term": "NP完全", "explanation": "一类既属于NP又NP难的问题，目前无多项式解法。"},
            {"term": "近似算法", "explanation": "对NP难问题在多项式时间内找到接近最优解的算法。"},
        ],
        "examples": ["背包问题的 0/1 版本需动态规划，分数版本可用贪心。", "Dijkstra 算法在有负权边时会失效，需改用 Bellman-Ford。"],
        "review_questions": ["动态规划和分治法的核心区别是什么？", "什么时候贪心算法能保证全局最优？", "P 和 NP 的区别是什么？"],
    },
    "计算机安全": {
        "overview": "计算机安全研究保护信息系统免受未授权访问、使用、泄露、破坏、修改或销毁的方法。核心领域包括密码学、网络安全、系统安全、应用安全和安全管理。",
        "sections": [
            {"heading": "密码学基础", "content": "对称加密（AES）使用相同密钥加解密；非对称加密（RSA）使用公钥加密私钥解密；哈希函数（SHA-256）用于完整性校验。"},
            {"heading": "网络安全", "content": "防火墙过滤流量，IDS/IPS 检测入侵，TLS 保护传输安全，VPN 建立加密隧道。"},
            {"heading": "应用安全", "content": "常见漏洞包括 SQL 注入、XSS、CSRF、缓冲区溢出，防御需要输入校验、参数化查询、输出编码等。"},
        ],
        "key_points": ["机密性、完整性、可用性是信息安全三要素", "对称加密速度快，非对称加密便于密钥分发", "TLS 为网络通信提供加密和身份认证", "输入校验是防御注入攻击的第一道防线", "最小权限原则限制攻击面"],
        "glossary": [
            {"term": "加密", "explanation": "将明文转换为密文的过程，需密钥才能解密。"},
            {"term": "哈希", "explanation": "将任意数据映射为固定长度的摘要，不可逆。"},
            {"term": "XSS", "explanation": "跨站脚本攻击，向网页注入恶意脚本窃取信息。"},
            {"term": "防火墙", "explanation": "按规则过滤进出网络流量的安全设备。"},
            {"term": "零信任", "explanation": "默认不信任任何网络内外实体，持续验证的安全模型。"},
        ],
        "examples": ["HTTPS 使用 TLS 加密 HTTP 流量，防止中间人攻击。", "SQL 注入可通过参数化查询有效防御。"],
        "review_questions": ["对称加密和非对称加密各有什么优缺点？", "如何防御 XSS 攻击？", "TLS 握手的基本过程是什么？"],
    },
    "分布式系统": {
        "overview": "分布式系统由多台独立计算机通过网络协作，对外表现为一个统一系统。核心挑战包括一致性、可用性、分区容错、共识算法和分布式事务。",
        "sections": [
            {"heading": "CAP 定理", "content": "在存在网络分区的情况下，分布式系统只能在一致性（Consistency）和可用性（Availability）之间二选一，分区容错（Partition tolerance）是必须的。"},
            {"heading": "一致性算法", "content": "Paxos 和 Raft 是经典共识算法，保证分布式节点就某个值达成一致；ZooKeeper 和 etcd 常用于分布式协调。"},
            {"heading": "分布式架构", "content": "微服务将应用拆分为独立可部署的服务；消息队列解耦生产者和消费者；负载均衡分发请求到多节点。"},
        ],
        "key_points": ["CAP 定理中分区容错是必需的", "Raft 比 Paxos 更易理解和实现", "微服务提升独立部署能力但增加运维复杂度", "幂等性确保重复请求不产生副作用", "最终一致性放松强一致性以提升性能"],
        "glossary": [
            {"term": "CAP", "explanation": "一致性、可用性、分区容错三者不可兼得的定理。"},
            {"term": "Raft", "explanation": "通过领导者选举和日志复制达成共识的算法。"},
            {"term": "微服务", "explanation": "将应用拆分为小型、独立部署的服务的架构风格。"},
            {"term": "消息队列", "explanation": "异步传递消息的中间件，解耦生产者和消费者。"},
            {"term": "幂等性", "explanation": "操作执行多次与执行一次效果相同。"},
        ],
        "examples": ["电商系统用消息队列异步处理订单，提升系统吞吐。", "Redis Sentinel 通过选举机制实现高可用。"],
        "review_questions": ["CAP 定理对分布式系统设计有什么指导意义？", "Raft 如何保证日志一致性？", "微服务和单体架构各有什么优缺点？"],
    },
}

FALLBACK_SUMMARIES = {topic: data["overview"] for topic, data in FALLBACK_KNOWLEDGE.items()}


# ── 备用数据源 ──

EN_WIKIPEDIA_REST = "https://en.wikipedia.org/api/rest_v1/page/summary/"


def _fetch_english_wikipedia(topic: str) -> dict[str, Any] | None:
    """Try fetching from English Wikipedia when Chinese fails."""
    alias = TOPIC_ALIASES.get(topic, topic.replace(" ", "_"))
    try:
        payload = _get_json(EN_WIKIPEDIA_REST + urllib.parse.quote(alias, safe=""))
        extract = str(payload.get("extract") or "")
        if not extract or len(extract) < 60:
            return None
        page_url = ((payload.get("content_urls") or {}).get("desktop") or {}).get("page", "")
        return {
            "extract": extract,
            "url": str(page_url),
            "title": str(payload.get("title", alias)),
        }
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError, KeyError):
        return None


# ── 内容验证 ──


def _verify_card_content(card: dict[str, Any]) -> bool:
    """Verify that a knowledge card contains valid, meaningful content.

    Returns True if content passes quality checks, False otherwise.
    """
    overview = str(card.get("overview", ""))
    key_points = card.get("key_points") or []

    # Overview must have minimum length (60 chars is ~2 substantial Chinese sentences)
    if len(overview) < 60:
        return False

    # Must have at least 3 non-empty key points
    valid_points = [p for p in key_points if isinstance(p, str) and len(p.strip()) >= 4]
    if len(valid_points) < 3:
        return False

    # Overview should contain the topic or related concepts
    topic = str(card.get("topic", ""))
    source_name = str(card.get("source_name", ""))
    is_english_source = "(en)" in source_name or "decomposed" in source_name
    if not is_english_source:
        # Basic relevance: check if topic characters appear in overview
        topic_chars = set(topic.replace(" ", ""))
        overview_chars = set(overview)
        overlap = topic_chars & overview_chars
        if len(overlap) < 2 and len(topic) >= 2:
            return False

    # For Chinese topics from Chinese sources, verify overview has Chinese content
    if topic and any('一' <= c <= '鿿' for c in topic):
        chinese_chars = sum(1 for c in overview if '一' <= c <= '鿿')
        # Skip Chinese check for English Wikipedia sources
        if chinese_chars < 20 and source_name == "Wikipedia":
            return False

    return True


def _cross_validate_card(card: dict[str, Any]) -> dict[str, Any]:
    """Cross-validate card content against fallback data.

    Returns the card with an added validation note if discrepancies found.
    """
    topic = str(card.get("topic", ""))
    fallback = FALLBACK_KNOWLEDGE.get(topic)
    if not fallback:
        return card

    fb_key_points = set(str(p).strip() for p in fallback.get("key_points", []))
    card_key_points = set(str(p).strip() for p in card.get("key_points", []))

    # Calculate overlap ratio
    if fb_key_points and card_key_points:
        overlap = sum(1 for cp in card_key_points if any(
            _text_overlap(cp, fp) > 0.3 for fp in fb_key_points
        ))
        overlap_ratio = overlap / max(len(card_key_points), 1)
        if overlap_ratio < 0.2:
            card["encoding_status"] = "low_confidence"
            card["validation_note"] = "来源内容与知识库参考数据重叠率低"

    return card


def _text_overlap(text_a: str, text_b: str) -> float:
    """Simple character-level overlap ratio between two strings."""
    if not text_a or not text_b:
        return 0.0
    chars_a = set(text_a)
    chars_b = set(text_b)
    intersection = chars_a & chars_b
    return len(intersection) / max(len(chars_a), 1)


# ── 分解查询 ──


def _assemble_from_parts(topic: str, sub_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble a knowledge card from sub-topic query results."""
    fallback = FALLBACK_KNOWLEDGE.get(topic, _generic_fallback(topic))

    # Collect overviews from successful sub-results
    overviews = []
    all_key_points: list[str] = []
    sections: list[dict[str, str]] = []
    examples: list[str] = []

    for sub in sub_results:
        ov = str(sub.get("overview", ""))
        if len(ov) >= 30:
            overviews.append(ov)
        for kp in sub.get("key_points") or []:
            if str(kp) not in all_key_points:
                all_key_points.append(str(kp))
        for sec in sub.get("sections") or []:
            if isinstance(sec, dict):
                sections.append(sec)

    # Assemble overview from sub-overviews
    if overviews:
        overview = f"{topic}涵盖多个核心领域。{'；'.join(overviews[:4])}"
        if len(overview) > 520:
            overview = overview[:517] + "..."
    else:
        overview = fallback["overview"]

    # Merge key points (prefer extracted, fill gaps with fallback)
    key_points = all_key_points[:8]
    if len(key_points) < 5:
        for kp in fallback.get("key_points", []):
            if kp not in key_points:
                key_points.append(kp)
            if len(key_points) >= 6:
                break

    # Build sections from sub-topics
    if not sections:
        sections = fallback.get("sections", [])

    # Use fallback for review questions and glossary
    glossary = fallback.get("glossary", [])
    review_questions = fallback.get("review_questions", [])
    examples_list = fallback.get("examples", [])

    source_url = sub_results[0].get("source_url", "") if sub_results else ""

    return {
        "id": _card_id(topic),
        "topic": topic,
        "title": topic,
        "source": source_url,
        "source_name": "Wikipedia (decomposed)",
        "source_url": source_url,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "offline": False,
        "encoding_status": "ok",
        "overview": _clip(overview, 520),
        "summary": _clip(overview, 520),
        "sections": sections[:6],
        "key_points": key_points[:8],
        "glossary": glossary,
        "examples": examples_list,
        "review_questions": review_questions,
        "raw_excerpt": overview[:1200],
    }


def _fetch_decomposed_topic(topic: str) -> dict[str, Any] | None:
    """Try to fetch a topic by decomposing it into sub-topics and assembling results."""
    sub_topics = TOPIC_DECOMPOSITION.get(topic, [])
    if not sub_topics:
        return None

    sub_results: list[dict[str, Any]] = []
    for sub_topic in sub_topics[:6]:  # Limit to 6 sub-queries
        try:
            card = fetch_wikipedia_summary(sub_topic)
            if not card.get("offline") and _verify_card_content(card):
                sub_results.append(card)
        except Exception:
            continue

    if not sub_results:
        return None

    assembled = _assemble_from_parts(topic, sub_results)
    if _verify_card_content(assembled):
        assembled = _cross_validate_card(assembled)
        return assembled

    return None


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
        card = build_knowledge_card(title, page["extract"], page["url"], offline=False, source="Wikipedia")
        if _verify_card_content(card):
            return _cross_validate_card(card)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        pass

    try:
        page = _fetch_rest_summary(title)
        card = build_knowledge_card(title, page["extract"], page["url"], offline=False, source="Wikipedia")
        if _verify_card_content(card):
            return _cross_validate_card(card)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        pass

    # 尝试英文 Wikipedia
    en_page = _fetch_english_wikipedia(topic)
    if en_page:
        card = build_knowledge_card(title, en_page["extract"], en_page["url"], offline=False, source="Wikipedia (en)")
        if _verify_card_content(card):
            return _cross_validate_card(card)

    # 分解查询
    decomposed = _fetch_decomposed_topic(topic)
    if decomposed:
        return decomposed

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
