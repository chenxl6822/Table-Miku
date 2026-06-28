# Table-Miku 当前阶段与下一阶段规划

> 更新日期：2026-06-04  
> 当前分支：`main`  
> 最新发布标签：`v0.1.2`  
> 当前阶段判断：阶段 3“知识复习闭环”已在工作区出现实现痕迹，但知识库还没有覆盖用户配置的 10 个主题；下一阶段建议合并复习闭环与知识引擎，直接面向 `v0.2.0：知识引擎整合版` 开发。

## 1. 当前状态摘要

Table-Miku 已经从桌宠提醒器推进到“桌面学习与求职助手”。目前知识板块已经具备结构化知识卡片、Wikipedia 中文修复、离线 fallback、复习调度和今日复习入口的雏形。下一阶段不要继续简单堆 JSON 文件，而应该把知识模块升级为可长期增长的数据系统。

| 项目 | 状态 |
|---|---|
| 桌宠基础 | 透明窗口、拖动、托盘、右键菜单已具备 |
| 交互体验 | 菜单已分组；长文本气泡与详情窗口已具备 |
| 学习提醒 | 目标解析、定时提醒、课程提醒、番茄钟已具备 |
| AI 助手 | DeepSeek/OpenAI 适配、每日简报、事件日志已具备 |
| 知识卡片 | Wikipedia/离线卡片、乱码修复、结构化字段已具备 |
| 复习闭环 | `review_scheduler`、`knowledge_review`、今日复习和测试已出现 |
| 当前短板 | 10 个主题没有全部入库和初始化复习；知识数据仍以 JSON 为主，不适合多源增长、全文搜索、去重和长期历史 |

## 2. 当前知识模块分析

### 2.1 已有优势

| 能力 | 价值 |
|---|---|
| 结构化知识卡片 | 已有 overview、sections、key_points、glossary、examples、review_questions，可直接用于复习和 AI 上下文 |
| 编码修复 | 对中文 mojibake 有检测和修复能力，适合继续接入中文来源 |
| 离线 fallback | 网络失败时仍可提供六类计算机基础知识 |
| 复习状态独立 | 复习数据没有混入卡片主体，方向正确 |
| 单元测试 | 知识卡片、编码、复习调度和复习服务已有测试基础 |

### 2.2 主要限制

| 限制 | 影响 |
|---|---|
| JSON 文件适合小规模配置，不适合长期知识库 | 数据多后加载、搜索、去重、迁移都会变重 |
| 只有粗粒度知识卡片，没有来源片段层 | 多源合并时难以追踪每条内容来自哪里 |
| 没有全文索引 | 用户无法快速查“TCP 握手”“索引失效”“进程线程”等关键词 |
| 没有多源任务表 | 不能管理拉取状态、失败重试、更新时间和来源质量 |
| 去重只靠卡片 ID/标题会不够 | Wikipedia、MDN、博客、面经可能讲同一主题但标题不同 |
| 复习历史长期增长后 JSON 会膨胀 | append history 写回整个文件，长期不划算 |

### 2.3 当前必须修正的主题覆盖缺口

用户当前希望知识库覆盖以下 10 个计算机主题，但实际知识库和复习板块没有完整出现这些主题。下一阶段必须把“主题配置 -> 知识卡片 -> 复习状态 -> 今日复习队列”打通，不能只停留在 settings 里的主题列表。

```json
[
  "计算机网络",
  "计算机组成原理",
  "数据结构",
  "操作系统",
  "编译原理",
  "数据库原理",
  "软件工程",
  "算法设计与分析",
  "计算机安全",
  "分布式系统"
]
```

验收标准：

| 验收项 | 标准 |
|---|---|
| 主题入库 | 首次刷新或迁移后，10 个主题都存在 `knowledge_cards` |
| 离线兜底 | 网络失败时，10 个主题都有 fallback card，不只覆盖原来的 6 个主题 |
| 复习初始化 | 每个主题都有 `review_states` 记录 |
| 今日复习 | 到期时 10 个主题能进入复习队列 |
| 搜索可见 | 搜索主题名或关键词能找到对应卡片 |
| 测试覆盖 | 对新增 4 个主题写 fallback/迁移/复习初始化测试 |

## 3. 下一阶段定位：v0.2.0 知识引擎整合版

下一阶段目标不是“爬更多数据”这么简单，而是建立一个能承载长期增长的本地知识引擎。版本策略上，不建议单独发布“v0.2.0 复习闭环稳定版”后再发布“v0.3.0 知识引擎版”；当前更适合继续开发，把复习闭环、10 主题入库、问答式复习和 SQLite 知识引擎合并为 `v0.2.0：知识引擎整合版` 一次发布。

目标能力：

1. 从多个来源拉取或导入知识数据。
2. 将知识条目统一结构化为本地卡片。
3. 用 SQLite 保存卡片、来源、复习状态和复习历史。
4. 支持全文搜索，能按关键词、主题、来源、掌握度过滤。
5. 支持多来源去重与合并，避免同一知识点重复提醒。
6. 复习历史采用追加式记录，长期增长不拖慢主流程。
7. 保持离线可用，网络只影响更新，不影响复习。
8. 复习答案展示为“问题 - 参考答案”一一对应，而不是只展示一段泛化答案。

## 4. 数据存储建议

建议采用本地 SQLite，而不是外部数据库服务。

| 方案 | 结论 | 理由 |
|---|---|---|
| 继续 JSON | 不建议作为长期方案 | 实现简单，但搜索、去重、历史增长和迁移会越来越难 |
| SQLite | 推荐 | Python 内置、单文件、适合桌面应用、可用 FTS5 做全文搜索 |
| 外部数据库 | 暂不建议 | 对桌宠过重，部署和隐私成本高 |
| 向量数据库 | 后置 | 等全文搜索和去重稳定后，再考虑语义检索 |

建议数据库文件：

```text
data/knowledge.db              # 开发态
%APPDATA%/TableMiku/knowledge.db  # 打包态
```

JSON 保留用途：

| 文件 | 保留方式 |
|---|---|
| `settings.json` | 继续保存配置 |
| `goals.json` | 继续保存学习目标 |
| `knowledge_base.json` | 作为迁移来源和备份导出格式 |
| `knowledge_reviews.json` | 迁移到 SQLite 后只做兼容导入 |

## 5. 推荐 SQLite Schema

### 5.1 知识卡片

```sql
CREATE TABLE knowledge_cards (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  topic TEXT NOT NULL,
  normalized_topic TEXT NOT NULL,
  overview TEXT NOT NULL DEFAULT '',
  difficulty TEXT NOT NULL DEFAULT 'normal',
  tags TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
);
```

### 5.2 来源与原始片段

```sql
CREATE TABLE knowledge_sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  license_note TEXT NOT NULL DEFAULT '',
  fetched_at TEXT,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE knowledge_chunks (
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  heading TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  quality_score REAL NOT NULL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  FOREIGN KEY(card_id) REFERENCES knowledge_cards(id),
  FOREIGN KEY(source_id) REFERENCES knowledge_sources(id)
);
```

### 5.3 搜索索引

```sql
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
  title,
  topic,
  overview,
  content,
  tokenize='unicode61'
);
```

如果目标环境的 SQLite 没有 FTS5，则降级为 `LIKE` 搜索，不阻塞应用运行。

### 5.4 复习状态与历史

```sql
CREATE TABLE review_states (
  card_id TEXT PRIMARY KEY,
  mastery REAL NOT NULL DEFAULT 0,
  review_stage INTEGER NOT NULL DEFAULT 0,
  next_review_at TEXT,
  last_reviewed_at TEXT,
  review_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(card_id) REFERENCES knowledge_cards(id)
);

CREATE TABLE review_history (
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  result TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  mastery_after REAL NOT NULL,
  stage_after INTEGER NOT NULL,
  FOREIGN KEY(card_id) REFERENCES knowledge_cards(id)
);
```

### 5.5 复习问答对

复习板块展示答案时，需要把问题和答案一一对应展示。建议新增结构化问答表，或者在卡片 JSON 字段中保留 `qa_pairs`。SQLite 方案建议使用独立表。

```sql
CREATE TABLE knowledge_qa_pairs (
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  source_chunk_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(card_id) REFERENCES knowledge_cards(id)
);
```

展示要求：

| UI 状态 | 内容 |
|---|---|
| 复习提问页 | 显示主题、概览、关键点和问题列表；可先隐藏答案 |
| 用户标记后 | 按 `问题 1 -> 参考答案 1`、`问题 2 -> 参考答案 2` 成对展示 |
| 无答案时 | 从 overview、sections、key_points 生成保底答案，不显示空答案 |
| 来源 | 如果答案来自 chunk，显示来源名或链接 |

卡片字段建议：

```json
{
  "qa_pairs": [
    {
      "question": "TCP 为什么需要三次握手？",
      "answer": "三次握手用于确认双方收发能力、同步初始序列号，并避免旧连接请求造成误连接。",
      "source": "Wikipedia/离线卡片"
    }
  ]
}
```

### 5.5 拉取任务与去重记录

```sql
CREATE TABLE ingest_jobs (
  id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  query TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE dedupe_links (
  id TEXT PRIMARY KEY,
  winner_card_id TEXT NOT NULL,
  duplicate_card_id TEXT NOT NULL,
  score REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

## 6. 多源拉取规划

按稳定性分批接入，不要一口气上所有来源。

| 批次 | 来源 | 类型 | 说明 |
|---|---|---|---|
| A | Wikipedia zh/en | 百科 | 继续沿用现有能力，补英文 fallback |
| A | 本地手工卡片 | 手动输入 | 用户自己添加知识点，最高可信 |
| B | MDN Web Docs | 官方文档 | 前端和 Web 基础高质量来源 |
| B | Python 官方文档 | 官方文档 | Python 基础、标准库、工程实践 |
| C | GitHub README/Topics | 项目资料 | 用于工程概念和工具链，不做大量爬取 |
| C | RSS/博客园等中文技术博客 | 博客 | 只取摘要和链接，注意质量和重复 |
| D | 面试复盘/投递薄弱点 | 本地数据 | 从用户自己的面试记录生成复习卡片 |

拉取流程：

```text
source adapter
  -> fetch raw
  -> decode/normalize
  -> extract chunks
  -> build candidate card
  -> dedupe
  -> merge or create card
  -> update FTS
  -> schedule review state
```

## 7. 去重策略

去重分三层，先简单可靠，再逐步增强。

| 层级 | 方法 | 说明 |
|---|---|---|
| L1 精确去重 | `source_url`、`content_hash` | 同一 URL 或完全相同正文直接合并 |
| L2 规范化标题 | normalized topic/title | 去除空格、标点、繁简差异、大小写 |
| L3 内容相似度 | token overlap / Jaccard | 相似度超过阈值时进入候选合并 |

暂不建议马上做向量相似度。先用标题规范化 + hash + Jaccard 足够覆盖大部分重复。

合并规则：

1. 保留用户复习历史最多的卡片作为 winner。
2. 多来源内容进入 `knowledge_chunks`，不要覆盖原卡片。
3. `overview` 可保留质量最高或用户编辑版本。
4. 被合并卡片写入 `dedupe_links`，便于回滚。

## 8. 全文搜索设计

搜索入口建议分两步做：

| 层级 | 能力 |
|---|---|
| v0.2.0 必做 | 搜索框：关键词 -> 卡片列表，显示标题、摘要、来源、掌握度 |
| v0.2.0 必做 | 从搜索结果一键加入今日复习/标记薄弱点 |
| v0.2.x 后续增强 | 过滤器：来源、标签、是否到期、掌握度区间 |

搜索结果排序：

1. 标题命中优先。
2. 到期复习卡片优先。
3. 用户低掌握度卡片优先。
4. 来源质量分高的卡片优先。
5. 最近更新内容适度加权。

## 9. 下一阶段任务拆分

| 任务 | 优先级 | 验收 |
|---|---|---|
| 设计 `knowledge_db.py` | P0 | 能创建 SQLite schema，重复初始化安全 |
| JSON -> SQLite 迁移 | P0 | 现有 `knowledge_base.json` 和 `knowledge_reviews.json` 可导入 |
| Repository API | P0 | 提供 create/update/search/get_due/record_review 等接口 |
| FTS 搜索 | P0 | 搜索“TCP”“索引”“进程”能返回相关卡片 |
| 10 主题种子数据 | P0 | 10 个主题全部入库、可搜索、可进入复习 |
| 问答对生成和展示 | P0 | 复习答案页按问题和答案一一对应展示 |
| 多源 adapter 接口 | P1 | Wikipedia 适配器先迁入统一接口 |
| 去重基础版 | P1 | URL/hash/normalized title 去重可用 |
| UI 搜索入口 | P1 | 右键知识库可搜索和打开卡片 |
| 长期复习历史 | P0 | review_history 追加保存，不覆盖旧记录 |
| 测试 | P0 | DB 初始化、迁移、搜索、去重、复习历史均有测试 |

## 10. 暂缓事项

| 暂缓 | 原因 |
|---|---|
| 大规模爬虫 | 容易触发反爬、版权和质量问题 |
| 向量数据库 | 当前先解决关键词搜索和结构化去重 |
| 自动采集知乎/牛客完整内容 | 合规和稳定性风险较高 |
| 云同步 | 本地数据模型稳定后再做 |
| 知识图谱可视化 | 数据还不够稳定，先做搜索和复习 |

## 11. Release 创建时机

当前不建议马上创建 release，也不建议单独发布“v0.2.0 复习闭环稳定版”。建议保留 `v0.1.2` 作为当前最新 release，等知识引擎整合完成后直接创建：

```text
v0.2.0 - 知识引擎整合版
```

创建 release 的最低条件：

| 条件 | 标准 |
|---|---|
| 代码状态 | 工作区干净，不包含个人 `data/*.json` 运行时变更 |
| 主题覆盖 | 10 个主题全部能入库、搜索、初始化复习 |
| 复习体验 | 今日复习可用，答案按问题一一对应展示 |
| 数据迁移 | JSON -> SQLite 迁移可重复执行且不丢数据 |
| 搜索 | 全文搜索或 fallback 搜索可用 |
| 去重 | URL/hash/标题规范化去重基础可用 |
| 测试 | `compileall` 和 `pytest` 全通过 |
| GUI 验收 | 启动、右键菜单、知识库、搜索、今日复习、退出均通过 |
| 文档 | README、计划文档、release note 更新 |

## 12. 开发前检查

下一阶段开发前先确认：

```powershell
git status --short --branch
.\.venv\Scripts\python.exe -m compileall main.py table_miku
.\.venv\Scripts\python.exe -m pytest tests\ -v
```

注意：PowerShell 下 `python -m py_compile main.py table_miku\*.py` 不会可靠展开通配符，建议用 `compileall`。

提交前不要包含：

| 不应提交 | 说明 |
|---|---|
| `data/settings.json` 运行时间字段 | 本地运行状态 |
| 用户复习历史测试脏数据 | 需要区分样例数据和个人数据 |
| `.env.local` | 可能包含 API Key |
| `.venv/`、`__pycache__/`、`.pytest_cache/` | 生成物 |

## 13. 2026-06-28 监测与知识库可信化升级

本轮升级把天气、系统监测和知识库可信来源推进到可验证状态：

| 模块 | 状态 |
|---|---|
| 天气定位 | 支持 `区县,城市,省份`、手动坐标和低置信度 IP 兜底；地理编码结果会写入本地缓存 |
| 天气预警 | Open-Meteo 请求使用 m/s 风速；主动监测会读取 `weather_alerts.lead_minutes` 并检查未来小时级预报 |
| 系统监测 | 内存告警同时参考百分比和可用 MB；网络探测拆分 DNS、TCP、TLS、HTTP 状态 |
| 误判防护 | 网络非手动检测需要连续异常才告警，恢复后单独提示 |
| 可信知识源 | 新增官方文档/RFC/论文元数据和 Obsidian 只读适配器；敏感路径会跳过 |
| 知识入口 | GUI、提醒、复习和 AI 简报优先走 SQLite Repository，旧 JSON 保留迁移/兜底 |
| 进度文档 | `docs/2026-06-28_monitoring_knowledge_upgrade_progress.md` 记录每阶段变更和测试 |

Obsidian Vault 约束：只允许读取配置的 Markdown 来源，不允许修改、删除、移动或格式化 Vault 中任何文件。Table-Miku 只会把摘要、来源元数据和片段写入自己的 `knowledge.db`。
