# Table-Miku → 个人助理进化计划

> 基于当前代码深度分析（~3,920 行 Python + 483 行 QML，19 模块）
> 从"桌宠+学习工具"进化为"桌面面试助手 + 知识管家"

---

## 目录

- [一、当前能力评估](#一当前能力评估)
- [二、进化路线图（7 阶段）](#二进化路线图7-阶段)
- [三、阶段详解](#三阶段详解)
- [四、架构建议](#四架构建议)
- [五、技术风险](#五技术风险)

---

## 一、当前能力评估

### 已有能力 ✅

| 能力域 | 当前状态 | 模块 |
|--------|---------|------|
| **学习目标管理** | 60 天循环计划，自然语言导入，定时提醒 | `planner.py`, `goal_parser.py`, `reminders.py` |
| **系统监测** | CPU/内存/网络实时检测，异常告警 | `system_monitor.py` |
| **天气查询** | Open-Meteo + IP 定位/手动城市 | `weather.py` |
| **番茄钟** | 25/5 切换，多轮计数 | `pomodoro.py` |
| **AI Agent** | DeepSeek/OpenAI 可选，简报+规划+命令监视 | `assistant_core.py`, `agent_adapter.py` |
| **课程表** | PDF 导入 + 手动编辑 + 提前提醒 | `assistant_data.py` |
| **投递/面试记录** | 结构化录入，AI 上下文 | `assistant_data.py` |
| **知识库** | **仅 Wikipedia 拉取，6 个硬编码主题，摘要 ≤180 字** | `knowledge_base.py` |
| **精灵图动画** | 5 表情 + 对话气泡 + 拖拽 | `sprites.py`, `PetScene.qml` |
| **托盘常驻** | 后台运行，开机自启 | `app.py` |

### 缺失能力 ❌（按优先级）

| 能力域 | 缺失原因 | 优先级 |
|--------|---------|--------|
| **多源知识抓取** | 只用了 Wikipedia，缺博客/论坛/技术网站 | **P0** |
| **面试题库** | 无公司真题/面经/刷题记录 | **P0** |
| **知识梳理 & 复习** | 无关联图谱/艾宾浩斯复习/知识卡片 | **P0** |
| **语音交互** | 无麦克风输入 / TTS 输出 | **P1** |
| **持续对话** | 气泡一次性，无对话树/上下文记忆 | **P1** |
| **多模态交互** | 只能看文字气泡，无法用自然语言指挥 | **P1** |
| **主动智能** | AI 仅早晚简报触发，不能随时问 | **P1** |
| **日历/邮件** | 未接入任何外部 API | **P2** |
| **网络搜索** | 无内置搜索能力 | **P2** |
| **窗口感知** | 不知道用户当前在哪个应用 | **P2** |
| **跨设备同步** | 无手机端/Web 端 | **P3** |
| **插件系统** | 模块虽清晰但无动态加载机制 | **P3** |

---

## 二、进化路线图（7 阶段）

```
P0 ───── P1 ───── P2 ───── P3
  │        │         │        │
  ▼        ▼         ▼        ▼
阶段1   阶段2      阶段3     阶段4    阶段5    阶段6    阶段7
基础     知识引擎   面试题库   语音    对话智能   生态    插件化
稳定     +复习      +面经    交互     +搜索     连接
(2h)    (10h)      (8h)     (4h)    (6h)      (6h)    (4h)
                    ─────────────────
                    ▲ 核心竞争力 ▲
```

| 阶段 | 名称 | 工时 | 目标 |
|------|------|------|------|
| **1** | **基础稳定 & 自动开启 AI** | ~2h | 提交当前更改、完善错误处理、开启 AI 默认、补充最小测试 |
| **2** | **知识引擎 & 复习系统** | ~10h | 多源知识爬取、结构化梳理、艾宾浩斯复习、知识图谱 |
| **3** | **面试题库 & 面经管理** | ~8h | 公司面试题、面经、刷题记录、错题本、面试模拟 |
| **4** | **语音交互 (TTS + STT)** | ~4h | 说给 Miku 听，Miku 说给你听 |
| **5** | **对话式个人助理 + 搜索** | ~6h | 自然语言指挥 Miku，持续对话，上下文记忆，网络搜索 |
| **6** | **生态连接** | ~6h | 邮件/日历/手机通知 |
| **7** | **插件化 & 开发者 API** | ~4h | 可扩展架构，第三方插件 |

---

## 三、阶段详解

### 阶段 1：基础改造（~2h）

**目标：** 稳住当前代码，开启 AI 默认能力，为后续打基础。

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| 1.1 | **提交当前 30 个改动** | 全部 | `git add -A && git commit -m "chore: stage pending changes"` |
| 1.2 | **GitHub 创建 release v0.1.0** | — | 打 tag，push |
| 1.3 | **全局错误处理加固** | `app.py`, `assistant_core.py`, `weather.py`, `storage.py` | 所有网络/文件操作用 try-catch 包裹，异常时气泡提示不崩溃 |
| 1.4 | **AI 助理默认开启** | `app.py` run() | 如果检测到 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`，自动开启 AI Agent |
| 1.5 | **补最小测试** | 新建 `tests/` | 对 `goal_parser.py`, `calorie_calc`(if exists), `pomodoro.py` 加 unit test |
| 1.6 | **README 更新** | `README.md` | 更新功能清单，加贡献指南 |

**关键代码改动：**

```python
# app.py run() — 自动检测并启用 AI
def run():
    app = QApplication(sys.argv)
    # ... 现有代码 ...
    window = TableMiku()
    
    # 自动检测 API key 并开启 AI
    if _deepseek_key_exists() or os.environ.get("OPENAI_API_KEY"):
        settings = load_settings()
        assistant = settings.setdefault("assistant", {})
        if not assistant.get("ai_agent_enabled", False):
            assistant["ai_agent_enabled"] = True
            save_settings(settings)
```

---

### 阶段 2：知识引擎 & 复习系统（~10h）⭐ 核心

**目标：** Miku 能自动从多个可靠互联网来源抓取计算机知识，做结构化梳理，并按科学的复习计划推送给你。

**为什么这是核心：** 这是面试准备的基础——没有牢固的知识体系，刷再多面经也是空中楼阁。

#### 2.1 知识源管理器

**新建 `table_miku/knowledge_sources.py`：**

```python
# 多源知识获取的调度中心
# 每个源实现一个标准接口

class KnowledgeSource(ABC):
    """知识源基类"""
    name: str           # 源名称，如 "Wikipedia"
    base_url: str       # 基础 URL
    priority: int       # 优先级（低 = 优先使用）
    
    @abstractmethod
    def fetch(self, topic: str) -> SourceResult:
        """获取某个主题的知识"""
        ...

class SourceResult:
    content: str        # 正文（Markdown）
    url: str            # 原文链接
    source: str         # 源名称
    quality_score: int  # 质量评分 1-5
    fetched_at: datetime
```

**内置知识源方案：**

| 源 | 获取方式 | 质量 | 覆盖范围 |
|------|---------|------|---------|
| **Wikipedia** ✅ 已有 | REST API | ⭐⭐⭐⭐ | 通用概念 |
| **博客园 (cnblogs)** | RSS + 搜索 | ⭐⭐⭐⭐ | 中文技术文章 |
| **知乎专栏** | 搜索 API | ⭐⭐⭐ | 实践心得 |
| **MDN Web Docs** | REST API (已开放) | ⭐⭐⭐⭐⭐ | Web 技术 |
| **GitHub Trending / awesome-list** | GitHub API | ⭐⭐⭐⭐ | 开源学习资源 |
| **菜鸟教程 / runoob** | 页面爬取 | ⭐⭐⭐ | 入门教程 |
| **GeeksforGeeks** | 页面爬取 | ⭐⭐⭐⭐ | 英文算法 |
| **本地缓存 / 离线备用** | JSON 文件 | ⭐⭐ | 断网回退 |

**核心改动：**
- 替代现有的 `knowledge_base.py`（仅 Wikipedia + 6 主题 → 多源智能聚合）
- 新建 `source_wikipedia.py`（复用现有逻辑）
- 新建 `source_cnblogs.py`（博客园 RSS）
- 新建 `source_zhihu.py`（知乎搜索）
- 新建 `source_github.py`（GitHub topics/trending）

```python
# 自动根据主题选择最优知识源
class KnowledgeOrchestrator:
    def fetch(self, topic: str) -> KnowledgeResult:
        """按优先级依次尝试知识源，取最优结果"""
        sources = self._sorted_sources()
        for source in sources:
            try:
                result = source.fetch(topic)
                if result and result.quality_score >= 3:
                    return result
            except (NetworkError, TimeoutError):
                continue
        # 全失败 → 返回离线缓存
        return self._offline_fallback(topic)
    
    def fetch_multi(self, topic: str) -> list[KnowledgeResult]:
        """从多个源获取同一个主题，交叉验证 + 去重"""
        ...
```

#### 2.2 知识结构化引擎

**新建 `table_miku/knowledge_organizer.py`：**

原始内容（大段文本）→ 结构化知识：

```python
# 提取：标题 → 核心概念 → 子主题 → 关键点 → 代码示例 → 相关链接

class KnowledgeNode:
    topic: str
    summary: str              # 一句话总结
    key_concepts: list[str]   # 核心概念列表
    sub_topics: list[KnowledgeNode]  # 子主题
    code_snippets: list[str]  # 代码示例
    references: list[str]     # 引用/来源
    difficulty: int           # 难度 1-5
    tags: list[str]           # 标签
    related_topics: list[str] # 关联知识点
    last_reviewed: datetime
    next_review: datetime     # 下次复习时间
```

**核心改动：**
- 使用 AI Agent（已有）辅助结构化：把抓取的原始文本发给 DeepSeek，提取关键点
- 或使用规则 + 正则提取代码块、标题、列表
- 知识图谱：`topic → related_topics` 的关联关系

```python
# AI 辅助结构化
def ai_organize(raw_text: str, topic: str) -> KnowledgeNode:
    prompt = f"""将以下关于"{topic}"的技术文章结构化为知识点：
    - 一句话总结
    - 3-5 个核心概念
    - 代码示例（如果有）
    - 难度评估
    
    原文：
    {raw_text[:3000]}
    """
    response = call_deepseek(prompt)
    return parse_ai_response(response)
```

#### 2.3 艾宾浩斯复习系统

**新建 `table_miku/review_scheduler.py`：**

基于艾宾浩斯遗忘曲线设置复习间隔：

```
第 1 次复习: 学习后 1 小时
第 2 次复习: 1 天
第 3 次复习: 7 天
第 4 次复习: 14 天
第 5 次复习: 30 天
第 6 次复习: 90 天
```

```python
class ReviewScheduler:
    INTERVALS = [1/24, 1, 7, 14, 30, 90]  # 单位：天
    
    def due_reviews(self) -> list[KnowledgeNode]:
        """返回今天需要复习的知识点"""
        ...
    
    def review_done(self, node: KnowledgeNode, score: int):
        """复习完成，根据掌握度调整下次间隔"""
        if score >= 4:  # 掌握良好
            node.next_review = now + self.INTERVALS[min(node.review_count, 5)]
        else:           # 掌握一般，缩短间隔
            node.next_review = now + timedelta(hours=12)
```

**每日简报整合：**
```
今日简报：
📚 今天要复习的知识：3 个
  · TCP 三次握手 (#2 复习)
  · HashMap 扩容机制 (#1 复习)
  · MySQL 索引类型 (新学)
  
💡 推荐新知识：B+ 树原理（关联：MySQL 索引）
```

**UI 改动：**
- 右键菜单新增「知识库」「复习计划」
- Miku 气泡在复习时间到了主动提醒

#### 2.4 知识看板与搜索

**新建 `table_miku/knowledge_dashboard.py`：**

```python
class KnowledgeDashboard(QDialog):
    """知识库总览窗口"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Miku 知识库")
        self.resize(900, 600)
        
        # 左侧：主题树形浏览
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["主题", "掌握度", "下次复习"])
        
        # 右侧：内容详情
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        
        # 搜索框
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索知识点...")
        self.search.textChanged.connect(self._search)
```

**搜索能力：**
- 全文搜索所有已拉取的知识点
- 按标签/难度/来源过滤
- 结果按相关度排序

---

### 阶段 3：面试题库 & 面经管理（~8h）⭐ 核心

**目标：** Miku 成为你的面试备考伙伴——收集公司面试题、管理面经、刷题记录、模拟面试。

#### 3.1 面试题采集

**新建 `table_miku/interview_crawler.py`：**

| 来源 | 方式 | 内容 |
|------|------|------|
| **牛客网** | 搜索 + 页面爬取 | 真实面经、公司真题 |
| **力扣 (LeetCode)** | 本地刷题记录导入 | 算法题解 |
| **公司官方技术博客** | RSS/Atom | 技术栈、面试方向 |
| **Github awesome-interview** | GitHub API | 汇总面经仓库 |
| **看准网 / 脉脉** | 搜索 | 公司评价、面试经验 |

```python
class InterviewCrawler:
    def fetch_company_questions(self, company: str) -> list[InterviewQuestion]:
        """从多个来源爬取某公司的面试题"""
        ...
    
    def fetch_recent_interviews(self, company: str = None) -> list[InterviewExperience]:
        """获取最近面经"""
        ...
```

**面试题模型：**

```python
@dataclass
class InterviewQuestion:
    company: str          # 公司
    position: str         # 岗位
    category: str         # 分类：算法/操作系统/网络/数据库/...
    question: str         # 题目
    answer: str           # 参考答案
    difficulty: int       # 难度 1-5
    source: str           # 来源 URL
    tags: list[str]       # 标签
    collected_at: datetime
```

**面经模型：**

```python
@dataclass
class InterviewExperience:
    company: str
    position: str
    rounds: list[InterviewRound]  # 一面/二面/三面
    overall_rating: int           # 整体难度
    result: str                   # offer/挂/进行中
    source: str
    date: datetime
```

#### 3.2 刷题记录 & 错题本

**新建 `table_miku/interview_tracker.py`：**

```python
class InterviewTracker:
    def add_attempt(self, question: InterviewQuestion, my_answer: str, score: int):
        """记录一次作答"""
        ...
    
    def wrong_questions(self) -> list[Attempt]:
        """错题本：得分 < 3 的题目"""
        ...
    
    def stats(self) -> dict:
        """统计：总题数、正确率、各公司分布、各分类分布"""
        ...
```

**统计看板：**
```
📊 面试准备进度
━━━━━━━━━━━━━━━━━━
已刷题目：47 道
正确率：  68%
掌握较弱：操作系统（47%）、网络（55%）
按公司：  字节 12 道、腾讯 8 道、阿里 7 道
错题待复习：5 道
```

#### 3.3 面试模拟

**新建 `table_miku/interview_simulator.py`：**

```python
class InterviewSimulator:
    def start_session(self, company: str = None, position: str = None):
        """开始模拟面试"""
        self.mode = "simulating"
        self.score = 0
        self.question_count = 0
        
    def next_question(self) -> InterviewQuestion:
        """从题库选一道（按难度递增）"""
        ...
    
    def evaluate_answer(self, my_answer: str, question: InterviewQuestion) -> str:
        """AI 评估你的答案，给出反馈"""
        prompt = f"""你是一个面试官。题目：{question.question}
        参考答案：{question.answer}
        面试者回答：{my_answer}
        
        请评价：
        1. 正确性（0-5）
        2. 完整性（0-5）
        3. 改进建议
        4. 参考答案要点
        """
        return call_deepseek(prompt)
```

**UI 流程：**
1. 右键 → 「模拟面试」
2. 选择公司/岗位（或随机）
3. Miku 出一道题 → 你打字回答 → Miku AI 评估
4. 连续 5-10 题后出成绩单
5. 错题自动加入复习队列

#### 3.4 面经管理（增强现有）

现有 `assistant_data.py` 已支持面经记录，增强：
- 面经自动归类（按公司/岗位/时间）
- 高频考点提取（所有面经中出现最多的知识点）
- 面经对比（同公司不同时期的面经变化）

```python
def extract_hot_topics(experiences: list[InterviewExperience]) -> list[TopicHeat]:
    """从所有面经中提取高频知识点"""
    all_questions = []
    for exp in experiences:
        for round in exp.rounds:
            all_questions.extend(round.questions)
    
    # 统计每个知识点出现次数
    topic_count = Counter()
    for q in all_questions:
        for tag in q.tags:
            topic_count[tag] += 1
    
    return [TopicHeat(topic, count, count / len(all_questions))
            for topic, count in topic_count.most_common(20)]
```

---

### 阶段 4：语音交互（~4h）

**目标：** Miku 能听懂你说的话，能用声音回应你。

#### 4.1 TTS 语音输出

**方案选择：**

| 方案 | 优势 | 劣势 |
|------|------|------|
| **edge-tts** (推荐) | 免费，中文自然，多音色 | 异步，需联网 |
| `pywin32` + SAPI | 离线，Windows 原生 | 中文 TTS 质量一般 |
| **ElevenLabs API** | 高质量 | 收费 |

**推荐方案：** `edge-tts`（免费+中文好）+ 回退到 `pywin32 SAPI`

**改动：**
- 新建 `table_miku/tts.py` — `say_async(text)` 异步调用 edge-tts
- `app.py` 气泡显示时同时调用 `tts.speak()`（可右键静音）
- 设置新增 `voice_enabled` 选项

```python
# tts.py
import asyncio
import edge_tts

VOICE = "zh-CN-XiaoxiaoNeural"  # 晓晓，也可选 Yunxi/Yunyang

async def speak(text: str) -> None:
    short = text[:120]
    communicate = edge_tts.Communicate(short, VOICE)
    await communicate.save("_tts_temp.mp3")
```

#### 4.2 语音输入

**方案选择：**

| 方案 | 优势 | 劣势 |
|------|------|------|
| **Vosk** (推荐) | 离线，中文模型 50MB | 首次下载模型 |
| `SpeechRecognition` + Google | 简单 | 需联网 |
| **Whisper** | 最准 | GPU 要求高 |

**推荐方案：** `Vosk` 离线 + 回退到 `SpeechRecognition` Google API

**改动：**
- 新建 `table_miku/stt.py` — 监听麦克风，返回文字
- 右键菜单新增「聆听模式」

---

### 阶段 5：对话式个人助理 + 搜索（~6h）

**目标：** 你可以直接用自然语言指挥 Miku，不必通过右键菜单。

#### 5.1 对话引擎

当前链路：
```
右键 → 对应 action → QDialog → parse → 执行
```

改为：
```
Floating chat input → parse intent → dispatch → Miku 回复(气泡+TTS+表情)
```

**新建 `table_miku/chat_engine.py`：**

```python
class ChatEngine:
    def __init__(self, miku: 'TableMiku'):
        self._miku = miku
        self._intents = IntentRegistry()
        self._register_default_intents()
    
    def _register_default_intents(self):
        self._intents.register("天气", self._handle_weather)
        self._intents.register("任务", self._handle_tasks)
        self._intents.register("搜索", self._handle_search)
        self._intents.register("复习", self._handle_review)
        self._intents.register("面试", self._handle_interview)
        self._intents.register("知识", self._handle_knowledge)
        self._intents.register("番茄", self._handle_pomodoro)
        # ... 所有功能都可以通过自然语言触发
    
    def process(self, text: str):
        """NLU → intent → execute → response"""
        intent, params = self._classify(text)
        if intent:
            result = intent.handler(params)
            self._miku.say(result)
        else:
            # 回退到 AI Agent 自由对话
            ...
```

#### 5.2 浮动输入框

**新建 `table_miku/chat_input.py`：**

```python
class ChatInput(QWidget):
    """浮动在 Miku 下方的输入框"""
    submitted = Signal(str)
    
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        )
        self.input = QLineEdit(self)
        self.input.returnPressed.connect(self._submit)
```

#### 5.3 网络搜索能力

**新建 `table_miku/web_search.py`：**

```python
class WebSearch:
    """内置搜索：DuckDuckGo / Brave Search API"""
    
    def search(self, query: str) -> list[SearchResult]:
        """搜索网页，返回摘要"""
        ...
    
    def fetch_page(self, url: str) -> str:
        """获取页面内容（用于阅读模式）"""
        ...
```

**对话示例：**
```
你：帮我搜一下 Java 并发编程最佳实践
Miku：🔍 搜索到以下结果：
1. Java并发编程实战 - 博客园 ⭐⭐⭐⭐
2. 并发编程的三大特性 - 知乎
3. Oracle官方并发教程
要打开哪一个？
```

---

### 阶段 6：生态连接（~6h）

**目标：** Miku 能读你的邮件、查日历、搜网页，甚至发通知到手机。

#### 6.1 邮件聚合

- 用 `imaplib` 读 Gmail / QQ 邮箱（需应用专用密码）
- 每天早上简报中包含：今日未读邮件数 / 重要邮件摘要

#### 6.2 手机通知推送

| 方案 | 优势 | 劣势 |
|------|------|------|
| **pushplus** / **Server酱** | 微信推送，极简 | 依赖第三方 |
| **Telegram Bot** | 免费可靠 | 需 TG 账号 |

推荐先用 pushplus。

#### 6.3 日历同步

- Google Calendar API / Outlook 日历
- 读取今日日程，合并到简报中

---

### 阶段 7：插件化 & 开发者 API（~4h）

**目标：** 第三方可以写插件扩展 Miku 的能力，不修改核心代码。

#### 7.1 插件系统设计

```
table_miku/plugins/
├── __init__.py
├── base.py           # Plugin 基类
├── registry.py       # 插件管理器
├── builtin/          # 内置插件
│   ├── weather.py
│   ├── pomodoro.py
│   └── ...
└── external/         # 用户安装的外部插件
```

```python
class MikuPlugin:
    name: str
    version: str
    description: str
    
    def on_load(self, miku: 'TableMiku'):
        """插件加载时调用，可注册意图/菜单项/定时任务"""
        pass
```

#### 7.2 开发者 API

```
GET  /api/v1/status       — 桌面状态快照
POST /api/v1/say          — 让 Miku 说话
POST /api/v1/event        — 推送事件到时间线
GET  /api/v1/knowledge    — 知识库查询
GET  /api/v1/interview    — 面试题/面经查询
```

---

## 四、架构建议

### 目标架构（阶段 5+）

```
app.py (QWidget, 仅窗口管理 + 事件路由)
  ├── QmlMiku (动画渲染)
  │
  ├── services/
  │   ├── reminder_service.py
  │   ├── system_monitor_service.py
  │   ├── calendar_service.py
  │   ├── email_service.py
  │   └── weather_service.py
  │
  ├── knowledge/             ← 知识学习核心
  │   ├── orchestrator.py    (多源调度)
  │   ├── sources/           (知识源适配器)
  │   │   ├── wikipedia.py
  │   │   ├── cnblogs.py
  │   │   ├── zhihu.py
  │   │   ├── github.py
  │   │   └── mdn.py
  │   ├── organizer.py       (结构化)
  │   ├── review_scheduler.py(复习调度)
  │   └── dashboard.py       (知识看板)
  │
  ├── interview/             ← 面试准备核心
  │   ├── crawler.py         (面试题采集)
  │   ├── tracker.py         (刷题记录)
  │   ├── simulator.py       (模拟面试)
  │   └── hot_topics.py      (高频考点)
  │
  ├── intelligence/          ← 智能层
  │   ├── chat_engine.py     (意图分类 + 调度)
  │   ├── conversation.py    (对话历史)
  │   └── agent_adapter.py   (AI 调用)
  │
  ├── interaction/           ← 交互层
  │   ├── chat_input.py      (浮动输入框)
  │   ├── tts.py             (语音输出)
  │   └── stt.py             (语音输入)
  │
  └── plugins/
      ├── base.py
      ├── registry.py
      └── builtin/
```

---

## 五、技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **知识源网站反爬** | 抓取失败 | 尊重 robots.txt；加 User-Agent + 延时；RSS 优先于页面爬取 |
| **内容质量参差不齐** | 知识库含错误信息 | 多源交叉验证；用户可评分/纠错；Wikipedia + MDN 作为权威锚点 |
| **爬取频率过高被限** | IP 被封 | 缓存 TTL（24h）；仅增量更新；用户可手动触发刷新 |
| **Vosk 模型 50MB 太大** | 分发包过大 | 首次运行时下载；`SpeechRecognition` Google API 回退 |
| **AI 调用费用** | DeepSeek API 有成本 | 仅结构化/评估走 AI；搜索和基础整理走规则；可配置是否使用 AI |
| **6 阶段总工时 40h** | 战线长 | 阶段 2-3 可并行开发；每阶段独立可用 |
| **窗口监测隐私** | 用户顾虑 | 数据全本地，不上传；可开关 |

---

## 六、推荐优先级矩阵

```
       高价值                   中等价值
       ┌──────────────────────────────────────┐
  低   │ 阶段1: 基础稳定 (2h)                │ 阶段7: 插件化 (4h)
  工   │ 阶段3: 面试题库 (8h) ⭐              │
  时   │ 阶段2: 知识引擎 (10h) ⭐              │
       ├──────────────────────────────────────┤
  高   │ 阶段4: 语音交互 (4h)                 │ 阶段6: 生态连接 (6h)
  工   │ 阶段5: 对话助理+搜索 (6h)            │
  时   │                                      │
       └──────────────────────────────────────┘
```

**推荐执行顺序：**

```
Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6 → Phase 7
（基础）   （知识）   （面试）   （语音）   （对话）   （生态）   （插件）
   2h       10h       8h        4h        6h        6h        4h
                              ─── 总计 40h ───
```

其中 **阶段 2（知识引擎）+ 阶段 3（面试题库）** 是核心竞争力，建议优先投入。

---

## 七、即刻可执行的下一步

当前 `knowledge_base.py` 仅支持 Wikipedia（6 主题），第一步改造：

1. **改 `knowledge_base.py` → `sources/wikipedia.py`**：保留现有 Wikipedia 逻辑
2. **新建 `sources/` 包**：先加博客园 RSS 源
3. **新建 `knowlege_organizer.py`**：用 DeepSeek 把抓取内容结构化
4. **新建 `review_scheduler.py`**：艾宾浩斯复习

---

*文档版本 v2.0 — 2026-05-28*
