# Table-Miku → 个人助理进化计划

> 基于当前代码深度分析（~3,920 行 Python + 483 行 QML，19 模块）
> 从"桌宠+学习工具"进化为"桌面个人助理"

---

## 目录

- [一、当前能力评估](#一当前能力评估)
- [二、进化路线图（6 阶段）](#二进化路线图6-阶段)
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
| **知识库** | Wikipedia 拉取 + 离线备用 | `knowledge_base.py` |
| **精灵图动画** | 5 表情 + 对话气泡 + 拖拽 | `sprites.py`, `PetScene.qml` |
| **托盘常驻** | 后台运行，开机自启 | `app.py` |

### 缺失能力 ❌

| 能力域 | 缺失原因 | 优先级 |
|--------|---------|--------|
| **语音交互** | 无麦克风输入 / TTS 输出 | **P0** |
| **持续对话** | 气泡一次性，无对话树/上下文记忆 | **P0** |
| **多模态交互** | 只能看文字气泡，无法用自然语言指挥 | **P1** |
| **主动智能** | AI 仅早晚简报触发，不能随时问 | **P1** |
| **日历/邮件** | 未接入任何外部 API | **P1** |
| **网络搜索** | 无内置搜索能力 | **P1** |
| **窗口感知** | 不知道用户当前在哪个应用 | **P2** |
| **手势/快捷键** | 无自定义全局快捷键 | **P2** |
| **跨设备同步** | 无手机端/Web 端 | **P3** |
| **插件系统** | 模块虽清晰但无动态加载机制 | **P3** |

---

## 二、进化路线图（6 阶段）

```
P0 ─── P1 ─── P2 ─── P3
  │       │       │       │
  ▼       ▼       ▼       ▼
阶段1   阶段2   阶段3   阶段4   阶段5   阶段6
基础改造  语音觉醒  对话智能  窗口感知  生态连接  插件化
(2h)    (4h)    (6h)    (4h)    (6h)    (4h)
```

| 阶段 | 名称 | 工时 | 目标 |
|------|------|------|------|
| **1** | **基础改造 & 当前稳定性** | ~2h | 提交当前更改、完善错误处理、开启 AI 默认、补充最小测试 |
| **2** | **语音交互 (TTS + STT)** | ~4h | 说给 Miku 听，Miku 说给你听 |
| **3** | **对话式个人助理** | ~6h | 自然语言指挥 Miku，持续对话，上下文记忆 |
| **4** | **窗口感知 & 主动智能** | ~4h | Miku 知道你在做什么，主动建议 |
| **5** | **生态连接** | ~6h | 邮件/日历/搜索/手机通知 |
| **6** | **插件化 & 开发者 API** | ~4h | 可扩展架构，第三方插件 |

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

### 阶段 2：语音交互（~4h）

**目标：** Miku 能听懂你说的话，能用声音回应你。

#### 2.1 TTS 语音输出

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
    # 只读前 120 字符，避免读过长
    short = text[:120]
    communicate = edge_tts.Communicate(short, VOICE)
    await communicate.save("_tts_temp.mp3")
    # 播放（用 playsound 或 winsound）
```

#### 2.2 语音输入

**方案选择：**

| 方案 | 优势 | 劣势 |
|------|------|------|
| **Vosk** (推荐) | 离线，中文模型 50MB | 首次下载模型 |
| `SpeechRecognition` + Google | 简单 | 需联网 |
| **Whisper** | 最准 | GPU 要求高 |

**推荐方案：** `Vosk` 离线 + 回退到 `SpeechRecognition` Google API

**改动：**
- 新建 `table_miku/stt.py` — 监听麦克风，返回文字
- 右键菜单新增「聆听模式」— 点击后 Miku 进入 listening 状态，10 秒内识别你的语音
- 识别后的文字传给 AI Agent / 命令执行器

```python
# stt.py
from vosk import Model, KaldiRecognizer
import json, pyaudio

class VoiceListener:
    def __init__(self, model_path="models/vosk-small"):
        self.model = Model(model_path)
        self.recognizer = KaldiRecognizer(self.model, 16000)
    
    def listen_once(self, timeout=10) -> str:
        # 打开麦克风，流式识别
        # 返回识别到的文字
```

**UI 改动：**
- Miku 表情新增 `listening` 和 `speaking`
- QML 增加麦克风/喇叭的视觉指示
- 快捷键 `Ctrl+Space` 激活聆听（全局热键）

---

### 阶段 3：对话式个人助理（~6h）

**目标：** 你可以直接用自然语言指挥 Miku，不必通过右键菜单。

#### 3.1 对话引擎

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
        self._intents.register("课程", self._handle_timetable)
        self._intents.register("番茄", self._handle_pomodoro)
        self._intents.register("命令", self._handle_command)
        self._intents.register("提醒", self._handle_reminder)
        self._intents.register("投递", self._handle_application)
        # ... 所有右键功能都可以通过自然语言触发
    
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

**意图分类方案：**

| 方案 | 优势 | 劣势 |
|------|------|------|
| **规则匹配** (推荐初期) | 零依赖，快速 | 覆盖有限 |
| **小模型分类** (后期) | 泛化好 | 需加载模型 |

先用规则匹配（关键词 + 正则），后续可升级到 AI 分类。

#### 3.2 浮动输入框

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
        # 半透明背景，圆角，一个输入框 + 发送按钮
        self.input = QLineEdit(self)
        self.input.returnPressed.connect(self._submit)
    
    def toggle(self):
        self.setVisible(not self.isVisible())
        if self.isVisible():
            self.input.setFocus()
```

**热键：** `Ctrl+Shift+M` 切换输入框，回车发送，Esc 关闭。

#### 3.3 对话历史与上下文

**新建 `table_miku/conversation.py`：**

```python
class Conversation:
    def __init__(self, max_turns=20):
        self.history: list[dict] = []
    
    def add(self, role: str, text: str):
        self.history.append({"role": role, "text": text, "time": datetime.now()})
        if len(self.history) > self.max_turns * 2:
            self.history = self.history[-self.max_turns * 2:]
    
    def context(self) -> str:
        # 格式化为 AI 上下文
        lines = []
        for msg in self.history[-6:]:  # 最近 6 轮
            prefix = "我" if msg['role'] == 'user' else "Miku"
            lines.append(f"{prefix}：{msg['text']}")
        return "\n".join(lines)
```

#### 3.4 输入框绑定到 TableMiku

```python
class TableMiku(QWidget):
    def __init__(self):
        # ... 现有初始化 ...
        self.chat_input = ChatInput(self)
        self.chat_engine = ChatEngine(self)
        self.conversation = Conversation()
        
        # 全局快捷键绑定
        self._setup_global_hotkey()
    
    def _on_chat_submitted(self, text: str):
        self.conversation.add("user", text)
        response = self.chat_engine.process(text)
        self.conversation.add("assistant", response)
        self.say(response)
```

---

### 阶段 4：窗口感知 & 主动智能（~4h）

**目标：** Miku 知道你在做什么，在合适的时机主动给出建议。

#### 4.1 活跃窗口监测

**新建 `table_miku/window_watcher.py`：**

```python
import ctypes
import ctypes.wintypes

class WindowWatcher:
    """Windows 活跃窗口监测，知道你在用哪个应用/页面"""
    
    @staticmethod
    def active_window_title() -> str:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd) + 1
        buffer = ctypes.create_unicode_buffer(length)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length)
        return buffer.value
    
    @staticmethod
    def active_process_name() -> str:
        # 获取进程名：chrome.exe, Code.exe, terminal 等
        ...
```

#### 4.2 上下文感知建议

基于窗口标题和当前时间，Miku 可以：

| 窗口标题包含 | Miku 建议 |
|-------------|----------|
| `leetcode` / `牛客` | "刷题中？加油，记得记录错题。" |
| `VS Code` + 文件名含 `test` | "写测试了？覆盖率目标 80% 以上哦。" |
| `Google Chrome` + 工作时间 | "浏览网页超过 20 分钟了，切回任务？" |
| `WeChat` / `QQ` | "工作时间聊微信小心分心~" |
| 长时间无操作 (空闲监测) | "需要提神吗？开始一个番茄钟？" |

#### 4.3 智能时间线

Miku 记录一个"你的一天"时间线：

```
08:10 天气汇报：晴 22°C
08:20 简报：今日第12天：数组与字符串
08:30 你在 VS Code 中写代码
09:00 番茄钟专注开始
09:25 休息提醒
10:15 你在看 LeetCode
12:00 锁屏/长时间离开
14:30 你在用 Chrome —— 20分钟了，切回任务？
```

**新建 `table_miku/timeline.py`：**

```python
class Timeline:
    def __init__(self):
        self.events: list[dict] = []
    
    def add(self, category: str, detail: str):
        self.events.append({
            "time": datetime.now(),
            "category": category,
            "detail": detail,
        })
    
    def daily_summary(self) -> str:
        """每晚生成一天的总结"""
        ...
    
    def context(self) -> str:
        """最近 1 小时的事件，供 AI Agent 使用"""
        recent = [e for e in self.events if (datetime.now() - e['time']).seconds < 3600]
        return "\n".join(f"{e['time'].strftime('%H:%M')} {e['detail']}" for e in recent)
```

---

### 阶段 5：生态连接（~6h）

**目标：** Miku 能读你的邮件、查日历、搜网页，甚至发通知到手机。

#### 5.1 邮件聚合

- 用 `imaplib` 读 Gmail / QQ 邮箱（需应用专用密码）
- 每天早上简报中包含：今日未读邮件数 / 重要邮件摘要
- 可配置要检查的邮箱账号

#### 5.2 搜索能力

- 新增 Web Search 意图：`"帮我搜一下：Python async/await 最佳实践"`
- 用 Brave Search / DuckDuckGo API（免费）
- 结果摘要气泡显示，可点开详情

#### 5.3 手机通知推送

| 方案 | 优势 | 劣势 |
|------|------|------|
| **pushplus** / **Server酱** | 微信推送，极简 | 依赖第三方 |
| **Telegram Bot** | 免费可靠 | 需 TG 账号 |
| **自建 WebSocket** | 可控 | 需服务器 |

推荐先用 pushplus（免费），后续可自定义。

#### 5.4 日历同步

- Google Calendar API / Outlook 日历
- 读取今日日程，合并到简报中
- 上课/会议前 10 分钟提醒

---

### 阶段 6：插件化 & 开发者 API（~4h）

**目标：** 第三方可以写插件扩展 Miku 的能力，不修改核心代码。

#### 6.1 插件系统设计

```
table_miku/plugins/
├── __init__.py
├── base.py           # Plugin 基类
├── registry.py       # 插件管理器
├── builtin/          # 内置插件（随主程序发布）
│   ├── weather.py
│   ├── pomodoro.py
│   └── ...
└── external/         # 用户安装的外部插件
    └── ...
```

**Plugin 基类：**

```python
class MikuPlugin:
    name: str
    version: str
    description: str
    author: str = ""
    
    def on_load(self, miku: 'TableMiku'):
        """插件加载时调用，可注册意图/菜单项/定时任务"""
        pass
    
    def on_unload(self):
        """插件卸载时清理"""
        pass
```

**注册模式：**

```python
@plugin("笔记")
class NotePlugin(MikuPlugin):
    def on_load(self, miku):
        miku.intents.register("记笔记", self.handle_note)
        miku.menus.add("查看笔记", self.show_notes)
    
    def handle_note(self, text: str) -> str:
        # 把文字追加到今天的笔记文件中
        ...
```

#### 6.2 开发者 API

Miku 暴露的 API（通过本地 HTTP / 命名管道）：

```
GET  /api/v1/status       — 桌面状态快照
POST /api/v1/say          — 让 Miku 说话
POST /api/v1/event        — 推送事件到时间线
GET  /api/v1/tasks        — 今日任务
```

便于其他程序（如脚本、AutoHotkey、VS Code 插件）和 Miku 交互。

---

## 四、架构建议

### 当前架构（阶段 0）

```
app.py (QWidget, 全部 UI + 事件)
  ├── QmlMiku (动画渲染)
  ├── ReminderManager (15s 定时)
  ├── SystemMonitor (系统检测)
  ├── PersonalAssistant (60s 定时 + AI)
  └── TextInputDialog / TaskDialog (弹窗)
```

### 目标架构（阶段 3+）

```
app.py (QWidget, 仅窗口管理 + 事件路由)
  ├── QmlMiku (动画渲染)
  │
  ├── services/              ← 各类独立服务
  │   ├── reminder_service.py
  │   ├── system_monitor_service.py
  │   ├── calendar_service.py
  │   ├── email_service.py
  │   └── weather_service.py
  │
  ├── intelligence/          ← 智能层
  │   ├── chat_engine.py     (意图分类 + 调度)
  │   ├── conversation.py    (对话历史)
  │   ├── timeline.py        (时间线)
  │   └── agent_adapter.py   (AI 调用)
  │
  ├── interaction/           ← 交互层
  │   ├── chat_input.py      (浮动输入框)
  │   ├── tts.py             (语音输出)
  │   └── stt.py             (语音输入)
  │
  └── plugins/               ← 插件系统
      ├── base.py
      ├── registry.py
      └── builtin/
```

### 不拆分建议

**当前 `app.py` 961 行 → 不需要大拆分。** 只在阶段 3 时把 `ChatEngine` 和 `ChatInput` 抽成独立文件。app.py 仍保留为"主控制器"，只将服务逻辑外移。

---

## 五、技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **Vosk 模型 50MB 太大** | 分发包过大 | 用 `SpeechRecognition` Google API 回退；模型首次运行时下载 |
| **edge-tts 播放延迟** | 影响体验 | 短文本(<80字)直接气泡，长文本 TTS；预加载语音引擎 |
| **全局热键冲突** | 和其他软件冲突 | 可配置热键；默认选用 Ctrl+Shift+字母(较少冲突) |
| **6 阶段总工时 26h** | 战线长 | 分阶段交付，每阶段可独立使用；按优先级减速 |
| **窗口监测隐私** | 用户顾虑 | 数据全本地，不上传；可开关；显示隐私说明 |
| **插件安全** | 恶意插件 | 插件沙箱(限制文件/网络权限)、签名机制(后期) |

---

## 六、推荐优先级矩阵

```
       高价值                  低价值
       ┌─────────────────────────────┐
  低   │ 阶段1: 基础改造 (2h)        │ 阶段6: 插件化 (4h)
  工   │ 阶段2: 语音交互 (4h)        │
  时   │                              │
       ├─────────────────────────────┤
  高   │ 阶段3: 对话助理 (6h)        │ 阶段4: 窗口感知 (4h)
  工   │ ─────────────────           │ ─────────────────
  时   │ 这是核心增值，建议优先做    │ 可做但优先级低
       │                              │ 阶段5: 生态连接 (6h)
       └─────────────────────────────┘
```

**推荐执行顺序：** 阶段 1 → 阶段 2 → 阶段 3 → 阶段 5 → 阶段 4 → 阶段 6

---

*文档版本 v1.0 — 2026-05-28*
