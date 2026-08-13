# Table-Miku Knowledge Assistant 2.0 实施与运维手册

> 定位：企业知识库与任务处理 Agent 的可验证纵向切片
> 适用代码：`table_miku/knowledge_assistant/`
> 默认存储：SQLite `knowledge_assistant_2.db`，与桌面端旧知识库和 Agent 数据隔离

## 1. 目标、非目标与证据口径

本模块不是“多个 Agent 互相聊天”的演示。它把 AI 系统拆成可独立审查的知识摄取、检索、权限、任务、审批、幂等和可观测链路，并让每条关键安全边界都能由测试复现。

当前已实现的纵向能力：

| 目标 | 实现 | 可验证证据 |
|---|---|---|
| 文档上传、解析、切分、向量化 | JSON Base64 上传；TXT/Markdown/RST/JSON/PDF 解析；重叠切分；384 维本地哈希向量 | `test_knowledge_assistant_documents.py` |
| RAG、引用、拒答 | 多租户/集合过滤；向量和词项联合排序；阈值拒答；`[S1]` 结构化引用 | `test_knowledge_assistant_rag.py` |
| 工具调用与任务状态 | `query_knowledge`、`ingest_text`、`archive_document`；状态和错误持久化 | `test_knowledge_assistant_tasks.py` |
| 写审批、权限、幂等 | RBAC；请求人与审批人分离；10 分钟审批期限；一次性收据；幂等键冲突返回 409 | 同上 |
| 离线评测 | 固定语料、固定问答、召回/首引/引用覆盖/拒答门禁 | `evals/run_knowledge_assistant_evals.py` |
| Trace、延迟、Token | Trace + 嵌套 Span；端到端延迟；输入/输出 Token 估算；租户级聚合 | `observability.py` 与 API/测试 |
| 自动化测试与 Docker | pytest、Ruff、CI 离线门禁、非 root 容器、健康检查 | `.github/workflows/ci.yml`、`Dockerfile` |

当前明确不是以下能力：

- 不是大规模向量数据库；一次查询最多扫描当前租户范围内最近的 20,000 个 chunk。
- 不是语义模型效果证明；`local-hash-v1-384` 是离线、确定性的词项哈希向量。
- 不是生成式答案评测；当前回答是可审计的证据摘录组合，不调用 LLM。
- 不是 OCR；只有文本层的 PDF 能被索引。
- 不是完整身份提供商；服务验证 Bearer Token 并消费网关提供的租户/用户/角色头，生产部署必须由可信网关签发和清洗这些头。
- 不是分布式任务队列或多节点数据库；任务在当前进程同步执行，状态和失败会持久化。

## 2. 总体架构

```mermaid
flowchart LR
    U["用户或上游 Agent"] --> G["可信 API 网关\n认证并注入身份"]
    G --> API["Knowledge Assistant API"]

    API --> AUTH["RBAC + 租户/集合范围"]
    AUTH --> INGEST["文档摄取"]
    AUTH --> RAG["RAG 查询"]
    AUTH --> TASK["工具任务"]

    INGEST --> PARSE["格式校验与解析"]
    PARSE --> CHUNK["重叠切分"]
    CHUNK --> EMBED["本地哈希向量"]
    EMBED --> DB[("SQLite\n原文/文档/chunk/向量")]

    RAG --> RETRIEVE["租户/集合过滤\n向量 + 词项排序"]
    RETRIEVE --> GROUND["证据阈值\n引用或拒答"]
    RETRIEVE --> DB

    TASK --> READ["读工具\n直接执行"]
    TASK --> APPROVAL["写工具\nawaiting_approval"]
    APPROVAL --> HUMAN["独立人工审批"]
    HUMAN --> ONCE["一次执行 + 收据"]
    ONCE --> DB

    INGEST --> TRACE["Trace / Span"]
    RAG --> TRACE
    TASK --> TRACE
    TRACE --> DB
```

设计取舍：

1. 新模块使用独立数据库，避免改变旧桌面知识库 schema 或污染用户已有数据。
2. 原始文件保存在 `document_blobs`，解析后的 chunk 和向量在同一数据库中原子提交，便于重新索引和审计。
3. 直接上传是用户主动写操作，需要 `editor`；由 Agent 发起的 `ingest_text`/归档必须进入人工审批。
4. RAG 在证据不足时返回结构化拒答，不让模型凭参数知识补齐。
5. Trace 属性只保存标量元数据，主动丢弃包含 `content`、`prompt`、`password`、`token`、`secret`、`key` 的字段。

## 3. 数据与状态模型

### 3.1 核心表

| 表 | 用途 | 关键约束 |
|---|---|---|
| `documents` | 文档元数据和处理状态 | `tenant_id + collection_id + checksum` 活跃文档唯一 |
| `document_blobs` | 原始上传内容 | 与文档一对一，文档归档时保留以便恢复 |
| `chunks` | 切分文本和向量 | 文档内 ordinal 唯一；携带租户和集合范围 |
| `tasks` | 工具任务及状态 | `tenant_id + idempotency_key` 唯一 |
| `task_payloads` | 待审批写任务的临时正文 | 完成、拒绝、过期或失败后删除 |
| `approvals` | 人工决策 | 每个写任务只有一个审批对象 |
| `operation_receipts` | 已完成写操作的不可重复收据 | `operation_id` 和 `task_id` 均唯一 |
| `idempotency_records` | 直接上传的幂等响应 | 同 key 不同请求哈希拒绝 |
| `traces` / `spans` | 链路、延迟、Token 和错误 | 读取时强制按 tenant 过滤 |

SQLite 连接启用：

- `foreign_keys = ON`
- `journal_mode = WAL`
- `busy_timeout = 5000`
- 上下文退出时显式关闭连接，避免 Windows 数据库文件句柄泄漏

### 3.2 文档状态

```mermaid
stateDiagram-v2
    [*] --> processing: 预留文档与原文
    processing --> indexed: 解析/切分/向量/事务提交成功
    processing --> failed: 解析或持久化失败
    failed --> processing: 操作者用新幂等键显式重试同一内容
    indexed --> archived: 经批准的软归档
```

文档失败会保存有限错误摘要；不会静默退回旧 JSON，也不会把部分 chunk 标记为成功。

### 3.3 任务状态

```mermaid
stateDiagram-v2
    [*] --> queued: 读工具
    queued --> running: 原子认领
    running --> succeeded: 结果持久化
    running --> failed: 错误持久化，不自动重试

    [*] --> awaiting_approval: 写工具
    awaiting_approval --> queued: 独立审批人批准
    awaiting_approval --> rejected: 审批人拒绝
    awaiting_approval --> cancelled: 审批过期
```

安全不变量：

- 请求人不能批准或拒绝自己的写任务，即使同时具有 `editor` 和 `approver` 角色。
- 审批人必须先读取专用 Action Preview，再把该预览的 `preview_hash` 原样提交给批准端点；v2 哈希是由服务端专用密钥签发的 HMAC，绑定完整动作、目标、正文、到期时间和当前审批人，客户端不能从普通任务字段自行合成或转交给另一审批人使用。
- 任务读取、预览、批准、拒绝和最终执行都继承 Principal 的集合 allowlist；执行 Principal 只获得该动作的单一目标集合。
- 审批默认 10 分钟过期；过期任务被取消并删除暂存正文。
- 批准只把任务从 `awaiting_approval` 原子推进到 `queued`；同一 `preview_hash` 的并发重复批准返回当前/最终状态，并发执行只有一个认领者和一份收据。
- 写成功后生成以 `task_id` 为 `operation_id` 的唯一收据。
- 写失败没有收据，也不会自动重试；操作者审查失败后必须以新幂等键创建新任务。
- 参数完整性在执行前再次失败时，任务仍持久化为 `failed`；不受集合限制的审计角色可读取脱敏失败状态，集合受限角色在无法证明原集合时失败关闭，任务列表不会被一条损坏记录整体阻断。
- 进程重启时遗留的 `queued`/`running` 任务会以 `interrupted` 失败收口并删除暂存正文；必须先审查可能的局部副作用，再创建新任务。
- 归档是软删除；没有实现危险的物理删除工具。

## 4. 文档摄取

### 4.1 支持范围

| 类型 | 后缀 | 行为 |
|---|---|---|
| 纯文本 | `.txt`、`.rst` | 要求 UTF-8/UTF-8 BOM |
| Markdown | `.md`、`.markdown` | 保留 ATX 标题作为 chunk 元数据 |
| JSON | `.json` | 先严格解析，再格式化后切分 |
| PDF | `.pdf` | 使用 `pypdf` 提取文本并保留页码 |

限制：单文件最大 10 MiB，PDF 最多 500 页，文件名必须是不含目录的纯文件名。加密 PDF、无文本层的扫描 PDF、非 UTF-8 文本和不支持的后缀会明确失败。

### 4.2 切分与向量

- 默认 chunk 最大 900 字符、重叠 120 字符。
- 优先在换行、中文句号、英文句号或空格处切分。
- `local-hash-v1-384` 对英文/数字 token 和中文单字/双字组做带符号 feature hashing，再进行 L2 归一化。
- 向量以 little-endian float32 BLOB 存储，同时保存模型名和维度；模型不匹配的旧向量不会被错误参与查询。
- 相同租户、集合、内容 SHA-256 的活跃文档会去重。

本地哈希向量的优势是离线、无外发、确定性、无额外原生依赖；缺点是不能替代真实语义 embedding。若接入语义模型，应保留现有 provider 接口和模型版本字段，重建向量后再以固定评测集比较，而不是原地混用。

## 5. RAG、引用与拒答

查询流程：

1. 先用 `tenant_id`、授权集合、文档 `indexed` 和 `archived = 0` 做数据库过滤。
2. 对候选 chunk 计算余弦相似度、查询词覆盖率和显著英文/数字锚点覆盖率。
3. 查询包含 `Aurora`、`Redis` 等显著锚点但 chunk 不含任何锚点时，该 chunk 分数归零，防止仅因“内部代号”“租户”等公共词误命中。
4. 默认最高返回 5 条、上限 8 条；低于 0.24 的证据不进入回答。
5. 没有合格证据时返回 `refused = true`、`reason = insufficient_evidence` 和空 citations。
6. 有证据时只拼接截断后的来源摘录，并在每条后添加 `[S1]`；不调用 LLM，不补充来源外事实。

引用对象包括：

```json
{
  "id": "S1",
  "document_id": "doc-...",
  "chunk_id": "chunk-...",
  "filename": "spring.md",
  "collection_id": "engineering",
  "heading": "Spring IoC",
  "page_number": null,
  "excerpt": "Spring IoC 容器通过依赖注入...",
  "score": 0.51,
  "vector_score": 0.43,
  "lexical_score": 0.62
}
```

这证明了“检索与引用契约”，不等于开放域问答质量。接入生成模型时还应加入：引用忠实度判分、提示注入隔离、答案句子到证据的逐句映射、模型版本评测和真实 Token 用量回写。

## 6. 权限模型

### 6.1 角色

| 角色 | 权限 |
|---|---|
| `viewer` | 读知识、读任务、读 Trace |
| `editor` | viewer 能力 + 直接上传 + 创建 Agent 任务 |
| `approver` | 读知识/任务/Trace + 决策他人写任务 |
| `admin` | 全部权限，但仍不能自审批 |

每个 `Principal` 必须携带：

- `tenant_id`：所有文档、chunk、任务、审批和 Trace 的第一过滤键；
- `user_id`：请求、审批和审计主体；
- `roles`：RBAC；
- 可选 `collection_ids`：进一步缩小可读写集合。

服务不会把“查不到”和“另一个租户存在”区分给调用方；跨租户资源统一返回 404。集合越界返回 403。

### 6.2 身份信任边界

API 支持 `KNOWLEDGE_ASSISTANT_API_TOKEN` 的 Bearer Token 比较，并读取：

- `X-Tenant-ID`
- `X-User-ID`
- `X-Roles`，逗号分隔
- 可选 `X-Collection-IDs`，逗号分隔

这些身份头本身没有签名。生产部署必须：

1. 外部请求先经过真正的 OIDC/OAuth2/mTLS 网关；
2. 网关删除客户端自带的上述头；
3. 网关根据已验证身份重新注入 tenant/user/role/collection；
4. 服务只暴露在内网，Bearer Token 作为网关到服务的第二道共享凭据；
5. 定期轮换 Token，不写入镜像、Git、日志或 Trace。

直接把容器端口暴露到公网并信任客户端自报角色是不安全的。

## 7. HTTP API

### 7.1 桌面日常可信工作台（2.2）

运行桌面应用：

```powershell
.\.venv\Scripts\python.exe main.py
```

右键 Miku，进入“系统工具”并选择“企业知识助手管理台”。窗口包含：

- **文档**：列出当前租户/集合的文档、查看安全元数据、直接上传并索引，以及为现有文档创建归档审批任务；
- **RAG 查询**：默认打开；显示 grounded/refused 状态、安全 Markdown 答案、检索统计、结构化引用、纯文本证据详情和本次 `trace_id`；下一次拒答或查询失败会清空上一次答案与引用，避免来源错配；
- **任务与审批**：任务主视图显示人类可读的动作、目标、状态进度和操作收据，原始 JSON 默认折叠；审批人可主动加载专用 Action Preview、批准/拒绝/暂缓；
- **观测**：显示租户级 Trace/error、平均/P95/最大延迟、Token 估算、operation 聚合，并可按 RAG 返回的 ID 查看 Trace/Span。

首次使用按“上传资料 → 提问 → 核查引用”完成。管理台默认以 Viewer 打开并进入 RAG 查询页；没有资料时前往“文档”，在本地验收环境中展开身份设置并选择 Editor 后上传。Editor 创建写任务后，任务卡片会明确提示等待另一位审批人；必须改用不同用户名的 Approver，加载绑定当前审批人的精确预览后才能批准。身份设置默认折叠，当前已生效角色及能力持续可见；编辑但未应用的身份草稿会冻结业务区并清空受保护视图。

普通 RAG 答案使用受限 GitHub Markdown 阅读器，只展示标题、段落、强调、列表、表格、引用和代码等静态排版。HTML 不执行，Markdown 图片统一替换为“图片已禁用”，本地文件、网络、`data:` 与 `qrc:` 等资源均不加载，链接目标被移除且不能导航。引用详情始终按纯文本显示，并说明相关度只用于排序。此渲染器不用于审批正文：Action Preview 的不可信正文继续逐字、纯文本显示，防止 Markdown 隐藏 URL、伪造后果或改变“所见即所批”的内容。

默认情况下，桌面应用会在操作系统分配的随机 loopback 端口托管一个私有 HTTP API，并生成只保存在进程内存中的随机 Bearer Token。管理台仍经过真实的 JSON、Bearer、身份头、错误码和审批端点契约，但用户不需要另开 PowerShell 窗口。应用退出时会关闭该私有服务。

管理台不会自动复用任何已存在的 `127.0.0.1:8080` 实例。未显式配置外部连接时，如果启动前探测到该端口已有健康的 Knowledge Assistant，管理台会失败关闭并提示用户：先关闭外部服务，或显式提供外部 URL 和与该实例匹配的 Token。该探测只会降低与已知 8080 外部实例错连或共用数据库的风险，不能阻止两个使用随机端口的 Table Miku 进程同时打开同一数据目录。

连接外部实例必须同时显式设置以下两个变量；只设置 URL 或只设置 Token 都不构成有效外部连接配置：

```powershell
$env:KNOWLEDGE_ASSISTANT_DESKTOP_URL = "http://127.0.0.1:8080"
$env:KNOWLEDGE_ASSISTANT_API_TOKEN = "与服务端一致的 Token"
.\.venv\Scripts\python.exe main.py
```

非 loopback 连接必须使用 HTTPS，URL 不允许内嵌用户名、密码、路径、查询或 fragment。桌面客户端禁用环境 HTTP/HTTPS 代理，所有请求均直接连接显式目标，避免 loopback Token 和身份头被代理截获；需要企业代理或网关时，应把可信反向代理作为显式 HTTPS 目标。Token 不写入 `settings.json`、数据库、日志或界面。

审批交互遵循以下安全约束：

1. 管理台默认以 `viewer` 身份打开，不会自动执行上传、创建任务、批准或拒绝。
2. Agent 写任务必须由不同用户的 `approver` 主动加载专用预览；批准按钮在此之前保持禁用。
3. 审批页分成两个只读区域：可信动作契约显示准确目标、来源、`unverified` 标记、SHA-256、字节数、后果和恢复限制；不可信 Agent 原文与契约隔离，并通过 `QPlainTextEdit.setPlainText()` 仅按纯文本显示，不解释 HTML/Markdown，也不被当作界面指令。
4. “暂不处理，保留待审批”是默认焦点；Escape 退出预览并保留 pending，不把关闭误当成终态拒绝。
5. 最终批准还会弹出带目标、后果和可恢复性的二次确认，取消是默认选项；拒绝是另一个明确动作，不需要预览哈希且不会执行知识库写入。
6. 身份字段一旦变化，当前预览立即标记为陈旧并清除内存中的 `preview_hash`，批准/拒绝保持禁用，直到应用身份并重新刷新；选择其他任务、切换身份、暂缓、关闭预览或关闭管理台也会清除审批动作状态。
7. 批准成功后展示不含 ingest 正文的操作收据；拒绝会留下明确终态但不执行知识库写入。普通任务列表和收据区域不会重新显示不可信正文。

界面中的租户、用户、角色和集合输入是为了本地 UAT 复现 RBAC 与职责分离，不是登录系统。生产管理台必须从可信 OIDC/OAuth2/mTLS 网关获得不可编辑身份。当前 Trace/metrics 是租户级语义，无法安全映射到集合级授权：服务端对设置了 `collection_ids` 的身份访问 `GET /v1/metrics` 或 `GET /v1/traces/{trace_id}` 一律返回 `permission_denied`（HTTP 403）。管理台提前禁用观测面板只是改善交互，不是权限边界。

直接上传、创建 ingest 任务和创建归档任务都会保留本次输入及幂等键。若超时、连接中断、HTTP 408、服务端 5xx，或写请求返回 2xx 却带有畸形/超限响应体而使结果未知，管理台会在内存中保留一个绑定原 Principal 的未决请求胶囊：上传保存第一次读取的原始字节，ingest 保存原正文，归档保存原目标，重试界面的请求字段全部只读。关闭管理台、切换身份或取消重试不会静默删除胶囊；只有用户在风险提示中明确选择“放弃”才会生成新意图。除 408 外的明确 HTTP 4xx 或本地文件读取失败不会被误报为结果未知。若任务已经明确进入 `failed` 终态，则仍应先按第 4 节审查可能的局部副作用，再用新幂等键创建新任务。

当前可视化边界：

- API 没有 Trace 列表端点，只能查看本窗口查询返回或人工输入的单个 `trace_id`；
- HTTP 调用目前在 Qt UI 线程中同步完成；忙碌光标不代表后台执行或可取消，大文档解析期间窗口可能短暂失去响应。异步 worker/job、取消和进度展示是下一阶段工作；
- 5 秒 external health timeout 是 socket 空闲超时，不是抵御持续慢速响应的总时限；真正的 wall-clock deadline 与取消也属于下一阶段 worker/job；
- 私有服务仍是 SQLite 单节点原型。需要连接外部服务时必须显式配置桌面 URL 与匹配的 Token；检测到未显式配置的 `127.0.0.1:8080` 实例时失败关闭。当前没有跨进程单实例锁，不要让两个 Table Miku 进程同时使用同一数据目录；
- 未决请求胶囊只在当前桌面进程内存中保存；关闭管理台会保留，退出整个应用会丢失。生产版本需要持久化、加密且可审计的 outbox；
- 集合受限 Trace 的服务端数据模型尚未实现，因此集合受限身份访问 metrics/Trace 会由服务端返回 403；当前版本不宣称支持集合级观测。

### 7.2 本地启动

完整桌面开发环境：

```powershell
.\.venv\Scripts\python.exe -m table_miku.knowledge_assistant.api --host 127.0.0.1 --port 8080
```

只运行 2.0 服务：

```powershell
python -m venv .venv-ka2
.\.venv-ka2\Scripts\python.exe -m pip install -r requirements-ka2.txt
$env:TABLE_MIKU_DATA_DIR = Join-Path $env:LOCALAPPDATA "TableMiku-KA2"
$env:KNOWLEDGE_ASSISTANT_API_TOKEN = "使用本地生成的长随机值"
.\.venv-ka2\Scripts\python.exe -m table_miku.knowledge_assistant.api
```

健康检查不需要身份：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

### 7.3 请求头

以下示例假设：

```powershell
$headers = @{
  Authorization = "Bearer $env:KNOWLEDGE_ASSISTANT_API_TOKEN"
  "X-Tenant-ID" = "tenant-demo"
  "X-User-ID" = "alice"
  "X-Roles" = "editor"
}
```

### 7.4 上传文档

`POST /v1/documents` 要求 `editor` 和至少 8 字符的 `Idempotency-Key`：

```powershell
$bytes = [System.IO.File]::ReadAllBytes(".\docs\sample.md")
$body = @{
  filename = "sample.md"
  collection_id = "engineering"
  content_base64 = [Convert]::ToBase64String($bytes)
} | ConvertTo-Json
$uploadHeaders = $headers.Clone()
$uploadHeaders["Idempotency-Key"] = "upload-sample-0001"
Invoke-RestMethod http://127.0.0.1:8080/v1/documents `
  -Method Post -Headers $uploadHeaders -ContentType "application/json" -Body $body
```

同一个租户中，同一幂等键和同一请求返回原响应并标记 `idempotent_replay = true`；同 key 不同请求返回 409。

### 7.5 查询

`POST /v1/query` 要求 `knowledge:read`：

```powershell
$queryHeaders = $headers.Clone()
$queryHeaders["X-Roles"] = "viewer"
$body = @{
  query = "Spring IoC 的依赖注入解决什么问题？"
  collection_ids = @("engineering")
  top_k = 5
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8080/v1/query `
  -Method Post -Headers $queryHeaders -ContentType "application/json" -Body $body
```

响应始终含 `answer`、`refused`、`reason`、`citations`、`retrieval` 和 `trace_id`。

### 7.6 创建并审批写工具任务

Agent 以 `editor` 身份创建写任务：

```powershell
$taskHeaders = $headers.Clone()
$taskHeaders["X-User-ID"] = "agent-runtime"
$taskHeaders["Idempotency-Key"] = "agent-write-0001"
$body = @{
  tool_name = "ingest_text"
  arguments = @{
    filename = "agent-note.md"
    collection_id = "engineering"
    content = "# 审批后的知识`n这段内容只有在人工批准后才会进入知识库。"
  }
} | ConvertTo-Json -Depth 5
$task = Invoke-RestMethod http://127.0.0.1:8080/v1/tasks `
  -Method Post -Headers $taskHeaders -ContentType "application/json" -Body $body
```

返回的 `arguments` 只包含文件名、集合、大小和 SHA-256，不回显正文。正文暂存在 `task_payloads`。

另一个用户以 `approver` 身份先读取专用 Action Preview。该端点才会返回待写入的完整 UTF-8 正文，客户端必须将 `action.parameters.content` 当作纯文本显示，不能解释为 HTML/Markdown：

```powershell
$approvalHeaders = $headers.Clone()
$approvalHeaders["X-User-ID"] = "human-reviewer"
$approvalHeaders["X-Roles"] = "approver"
$preview = Invoke-RestMethod `
  "http://127.0.0.1:8080/v1/tasks/$($task.id)/approval-preview" `
  -Method Get -Headers $approvalHeaders

$approvalBody = @{
  preview_hash = $preview.preview_hash
} | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:8080/v1/tasks/$($task.id)/approve" `
  -Method Post -Headers $approvalHeaders -ContentType "application/json" -Body $approvalBody
```

预览包含准确目标、来源、后果、恢复语义、原文字节数与 SHA-256。v2 `preview_hash` 使用 HMAC-SHA256 绑定任务、租户、审批对象、隐藏请求哈希、完整 Action Preview、到期时间和 `decision.bound_approver`；普通任务字段不足以伪造它，另一审批人也不能复用。服务端在批准事务和实际执行前都会重验参数、暂存正文及归档目标。普通任务创建、读取、列表、Trace 与错误响应仍不回显正文。拒绝继续使用 `POST /v1/tasks/{id}/reject`，可提交 `{"reason":"证据不足"}`，不要求预览哈希，保持“拒绝即安全”的默认行为。

签名密钥以 32 字节 sidecar 文件保存在数据库旁：`knowledge_assistant_2.db.approval-hmac-key`。文件以独占创建方式生成，长度异常时服务拒绝启动；Docker 中它随 `/data` 卷持久化，因此未过期预览可跨容器重建继续使用。不得把该文件提交到 Git、写入日志或单独公开。多实例部署必须让所有实例安全共享同一密钥；当前单节点原型尚未提供 Secret Manager 集成和在线轮换。

成功收据会在 `operation_receipts` 中持久化 `preview_version`、`approved_preview_hash`、执行结果和不含正文的审批安全参数；旧版归档任务的富化目标也进入安全参数。升级前产生且没有预览契约的旧收据仍可读取，并返回 `approved_preview_hash = null`。SQLite 记录不能抵抗数据库管理员篡改，生产环境仍应把批准契约复制到追加写审计存储并签名或发送到 WORM 日志。HMAC 证明该身份从服务端获得了绑定预览令牌，但不能证明人实际阅读了屏幕内容。

### 7.7 其他端点

| 方法与路径 | 作用 |
|---|---|
| `GET /v1/documents` | 列出当前租户和集合范围内的活跃文档 |
| `GET /v1/documents/{id}` | 获取文档状态和 chunk 数 |
| `GET /v1/tasks` | 列出任务 |
| `GET /v1/tasks/{id}` | 获取任务、审批与收据 |
| `GET /v1/tasks/{id}/approval-preview` | 仅供独立、同租户且同集合审批人读取精确动作预览 |
| `POST /v1/tasks/{id}/approve` | 提交 `preview_hash` 批准预览中的精确动作 |
| `POST /v1/tasks/{id}/reject` | 拒绝任务且不产生写副作用 |
| `GET /v1/metrics` | 聚合 Trace 数、错误数、延迟、Token |
| `GET /v1/traces/{id}` | 获取单条 Trace 和嵌套 Span |

所有响应设置 `Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`。未处理异常只返回通用 500，详细堆栈留在服务端日志。

## 8. 可观测性

当前 operation：

- `document.upload`
- `rag.query`
- `task.execute`

典型 span：

- 上传：`document.reserve` → `document.parse` → `document.chunk` → `document.embed` → `document.persist`
- 查询：`rag.retrieve` → `rag.grounding`
- 任务：`tool.query_knowledge` / `tool.ingest_text` / `tool.archive_document`

`GET /v1/metrics` 返回：

- Trace 总数和错误数；
- 平均、P95、最大端到端延迟；
- 输入、输出和总 Token；
- 按 operation 的调用数和错误数。

P95 使用 nearest-rank 定义：对升序样本取第 `ceil(0.95 × N)` 个值；没有已完成 Trace 时返回 `0.0`。当前聚合范围是该租户最近最多 1000 条已完成 Trace。

注意：当前 Token 是基于本地英文 token 与中文 uni/bi-gram 的估算，用于相对成本和趋势，不是任何模型供应商的账单 Token。真实 LLM 接入后必须优先使用供应商响应中的 usage，并同时保留估算值用于异常检测。

SQLite 不是长期指标仓库。生产化应把脱敏 Trace 导出到 OpenTelemetry Collector，把延迟/错误率导入 Prometheus 或同类系统，并为：

- RAG 拒答率突变；
- P95 延迟超预算；
- 文档解析失败率；
- 审批积压/过期；
- 同一幂等键冲突；
- 任务失败率；

配置告警。

## 9. 离线评测

运行：

```powershell
.\.venv\Scripts\python.exe evals\run_knowledge_assistant_evals.py
```

固定输入：

- `evals/knowledge_assistant_corpus.jsonl`
- `evals/knowledge_assistant_cases.jsonl`

门槛：

| 指标 | 门槛 | 含义 |
|---|---:|---|
| `retrieval_recall` | ≥ 0.95 | 可回答问题的 citations 中包含预期文档 |
| `first_citation_accuracy` | ≥ 0.85 | 首条 citation 是预期文档 |
| `citation_coverage` | 1.00 | 所有可回答问题均有 citation |
| `refusal_accuracy` | 1.00 | 可答不拒、不可答必拒 |

结果写入被 Git 忽略的 `evals/results/knowledge_assistant_latest.json`。当前固定集包含 8 个样例，覆盖 Spring IoC、Redis、审批、幂等、Trace、PDF/OCR 边界和两类无答案问题；它是防回归门禁，不是统计显著的业务金标集。

扩充评测时应增加：

- 同义改写、简称、拼写错误和多语言问题；
- 多文档冲突、旧版本与归档文档；
- 跨租户/跨集合的对抗样例；
- 提示注入文档；
- 无答案但词面高度重叠的问题；
- 长文档和 PDF 页码引用；
- 专家人工标注的引用忠实度。

不得为了让评测通过而只修改阈值或把失败样例删除；应先判断是语料、检索、评分还是金标错误，并保留失败证据。

## 10. Docker 部署

### 10.1 构建与启动

生成本地 Token（示例只输出到当前进程环境，勿提交）：

```powershell
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$env:KNOWLEDGE_ASSISTANT_API_TOKEN = [Convert]::ToBase64String($bytes)
docker compose build
docker compose up -d
Invoke-RestMethod http://127.0.0.1:8080/health
```

Compose 默认：

- 只绑定 `127.0.0.1:8080`；
- 强制设置 API Token；
- 使用命名卷 `knowledge-assistant-data`；
- 容器内使用 UID/GID 10001；
- 只读根文件系统，只有 `/data` 和 64 MiB `/tmp` 可写；
- 删除全部 Linux capabilities；
- 启用 `no-new-privileges`；
- 包含健康检查。

查看状态和脱敏日志：

```powershell
docker compose ps
docker compose logs --tail 200 knowledge-assistant
```

不要把真实 Token 放进 `.env.example`、Compose 文件、镜像层或故障截图。

### 10.2 数据备份

数据库位于卷中的 `/data/knowledge_assistant_2.db`，审批签名密钥位于同卷的 `/data/knowledge_assistant_2.db.approval-hmac-key`。SQLite WAL 模式下不要只复制主 `.db` 文件而忽略未 checkpoint 的 WAL；也不能遗漏签名密钥，否则未完成的预览令牌会全部失效。推荐在维护窗口停止服务后备份整个卷，或使用 SQLite Online Backup API 生成数据库一致性副本并把 sidecar 密钥作为受控秘密单独备份。

恢复属于有状态外部操作，应先：

1. 明确目标环境和目标卷；
2. 验证备份 SHA-256、时间点和可读性；
3. 停止服务并保留当前卷快照；
4. 恢复到新卷做验证，不直接覆盖唯一副本；
5. 检查 `/health`、文档数、固定 RAG 查询和审批任务后再切换。

本手册不授权自动删除卷、覆盖数据库或执行生产恢复。

### 10.3 容量和健康

上线前至少建立：

- 文档数、chunk 数、数据库/WAL 大小；
- 上传大小和解析耗时分布；
- 查询候选数与 P95 延迟；
- 审批等待时间；
- 备份成功率和恢复演练记录。

当前 20,000 chunk 进程内扫描上限是保护措施，不是性能 SLA。超过该规模应迁移到带租户过滤、索引构建和备份策略的向量存储，并用同一离线集和真实压测比较。

## 11. 威胁模型

### 11.1 资产

- 企业文档原文、chunk 和引用；
- 租户、用户、角色和集合授权；
- 待审批 Agent 写正文；
- 任务、审批和操作收据；
- Trace、错误和使用量；
- API Token 与上游身份凭据。

### 11.2 已实现控制与剩余风险

| 威胁 | 当前控制 | 剩余风险/生产要求 |
|---|---|---|
| 跨租户读取 | 所有资源查询先过滤 `tenant_id`；跨租户返回 404 | 必须测试每个新增 SQL；生产建议数据库 RLS/独立 schema |
| 集合越权 | Principal 集合 allowlist；摄取、查询、任务读取、预览、决策和执行均检查，执行仅获目标单一集合 | 身份头必须由可信网关注入 |
| Agent 未经同意写入 | 写工具默认 `awaiting_approval`；自审批禁止；精确 Action Preview 与 `preview_hash` 绑定；成功后返回批准预览哈希 | 需要企业审批策略、通知与人员离职回收 |
| 重复写入 | 请求哈希 + 幂等键；唯一收据；原子状态认领 | 多节点时需数据库级锁/队列语义 |
| 失败后重复副作用 | 失败无收据、无自动重试 | 外部非事务工具需补 compensating action 和远端幂等键 |
| 路径穿越/任意文件读取 | 上传只接受纯文件名；内容由请求体提供 | 未来 multipart/对象存储需重新审计路径和 MIME |
| 文件炸弹/资源耗尽 | 10 MiB、500 页、请求体 16 MiB、chunk/候选上限 | 仍需 CPU/内存/并发限流和 PDF 沙箱 |
| 提示注入 | 当前无 LLM；回答只摘录来源 | 接入 LLM 后必须将文档标为不可信数据并做工具隔离 |
| 敏感信息进 Trace | 属性 allowlist 思路；敏感键丢弃；不记录正文 | 错误文本仍需 DLP/集中日志访问控制 |
| API 角色伪造 | 可选 Bearer Token、容器只绑 localhost | 必须用认证网关清洗并重建身份头 |
| 数据不可恢复 | 软归档、保留原文、SQLite 一致性备份可行 | 需要加密、异地备份、恢复演练和保留策略 |
| 供应链 | 最小服务依赖仅 `pypdf`，CI 跑依赖审计 | 镜像基座和 action 仍应 pin digest、生成 SBOM、签名 |

### 11.3 安全发布检查

1. 运行 Ruff、完整 pytest、覆盖率、离线评测和 `pip-audit`。
2. 构建容器并确认以非 root 运行，根文件系统只读。
3. 检查镜像不含 `.env`、数据库、用户数据、评测结果和开发缓存。
4. 使用两个租户、两个集合和两个用户复测读隔离与审批职责分离。
5. 验证无答案问题返回空 citations，不能通过调低阈值掩盖失败。
6. 检查日志、Trace 和任务参数不包含正文、Token 或密码。
7. 记录容量、延迟、错误预算、备份和回滚责任人。

## 12. 测试与开发命令

2.0 定向门禁：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:TABLE_MIKU_DATA_DIR = Join-Path $env:TEMP "TableMiku-ka2-test-data"
.\.venv\Scripts\python.exe -m ruff check table_miku\knowledge_assistant evals\run_knowledge_assistant_evals.py tests\test_knowledge_assistant_*.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_knowledge_assistant_*.py
.\.venv\Scripts\python.exe evals\run_knowledge_assistant_evals.py
```

全项目门禁：

```powershell
.\.venv\Scripts\python.exe -m ruff check main.py table_miku tests
.\.venv\Scripts\python.exe -m pytest --cov=table_miku --cov-branch --cov-report=term-missing
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
```

Docker：

```powershell
docker build --tag table-miku-knowledge-assistant:test .
```

单元测试使用独立临时数据库和租户，不读取项目 `data/`、用户 AppData、真实 Vault 或真实 API Key。

### 12.1 2026-08-07 本地验证记录

以下是本次实现后的实际命令结果，不代表未来 checkout 或生产环境状态：

| 检查 | 结果 |
|---|---|
| 2.0 定向 pytest | 38/38 通过 |
| 全项目 pytest | 285/285 通过 |
| 分支覆盖率 | 57.34%，高于 40% 门槛 |
| Ruff | `main.py table_miku tests evals/run_knowledge_assistant_evals.py` 通过 |
| 离线 RAG 门禁 | 8/8 通过，`quality_gate=True`，未调用真实模型 API |
| `pip check` | 无依赖冲突 |
| `pip-audit` | `requirements.txt` 与 `requirements-ka2.txt` 均未发现已知漏洞 |
| Compose | `docker compose config --quiet` 通过 |
| Linux 镜像 | 构建成功；无网络、只读根目录、UID 10001、临时 `/data` 条件下初始化成功 |
| Windows 打包 | 隔离临时目录构建成功，生成 15,338,552 字节的 `TableMiku.exe` |

Windows 构建仍输出两条非阻塞环境警告：当前 PyInstaller/PySide6 hook 对一个缺失的
`qmlassetdownloaderprivateplugin.dll` 记录警告时自身出现 logging format error，以及可选 hidden import
`tzdata` 未找到。产物生成成功，但本次没有启动 GUI 做人工桌面验收，也没有验证该可选 QML 插件对应的功能。

本次没有调用真实 DeepSeek/OpenAI 模型、没有启动 Compose 服务、没有连接生产数据、没有推送、发布或部署。

## 13. 从纵向切片到生产系统

建议按证据推进，而不是一次性替换：

面向用户的增量顺序是：

1. **2.2 日常可信工作台（当前）**：首次路径、角色说明、安全 Markdown、证据详情、任务进度和操作原因；目标是让用户不读 JSON 也能完成查询、核查和职责分离审批。
2. **2.3 可恢复摄取**：后台 job、进度、取消、批量导入、失败中心和加密持久化 outbox；目标是长任务不冻结窗口且中断后可继续。
3. **2.4 检索质量闭环**：真实语义 embedding、reranker、逐句证据、反馈和知识缺口；是否上线由业务金标集决定。
4. **2.5 可用任务工具**：审批收件箱、通知、到期提示、真实外部工具、远端幂等和补偿；目标是安全完成具体业务动作，而非展示 Agent 对话。
5. **3.0 企业试点**：真实登录与 ACL、连接器、文档版本、审计导出、SLO 和试点用户指标；达到目标环境的重复证据后再扩大部署。

对应的技术生产化工作仍包括：

1. **真实语料评测**：建立企业金标集、无答案集、冲突文档和引用忠实度标注。
2. **语义 embedding**：引入可版本化 provider；离线重建；A/B 比较召回、拒答和成本。
3. **异步摄取**：对象存储 + 恶意文件扫描 + 沙箱解析 + 队列 + 可取消 job。
4. **身份与策略**：OIDC、SCIM、组到集合 ACL、策略决策点、审计导出。
5. **向量存储**：在真实规模下选择 pgvector/专用向量库，保留 tenant filter 和备份演练。
6. **生成层**：证据限定 prompt、逐句引用、注入测试、模型故障降级和真实 usage。
7. **外部写工具**：每个工具定义副作用、审批预览、远端幂等键、补偿动作和超时。
8. **SRE**：OpenTelemetry、SLO、错误预算、负载/故障注入、灾备与密钥轮换。

只有当这些能力在目标环境中有可重复证据，才应把“可运行纵向切片”表述为“可承担生产 SLA 的企业知识助手”。
