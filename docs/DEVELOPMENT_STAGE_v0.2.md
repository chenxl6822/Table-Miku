# Table-Miku 开发阶段文档 v0.2

> 更新日期：2026-06-03
> 当前版本：`v0.2.0-dev`
> 当前 HEAD：`0b4ee14`
> 远程分支：`origin/main`

---

## 1. 当前阶段完成情况

### v0.1.0 → v0.1.2（基础稳定化 + 知识库热修复）

| 成果 | 状态 |
|------|------|
| 透明桌宠、拖动、右键菜单、托盘图标 | ✅ |
| 学习目标解析和今日任务 | ✅ |
| 定时提醒、课程提醒、番茄钟 | ✅ |
| 天气查询和主动恶劣天气监测 | ✅ |
| CPU/内存/网络系统监测 | ✅ |
| DeepSeek/OpenAI AI 助理 | ✅ |
| Wikipedia 中文结构化知识卡片 | ✅ |
| 乱码检测修复 | ✅ |
| 离线 fallback 知识库 | ✅ |

### v0.1.2 → v0.2.0-dev（本轮完成）

| 成果 | 说明 |
|------|------|
| ✅ 间隔重复复习调度器 | `review_scheduler.py` — 6 阶段间隔 (1h→30d)，已知/模糊/忘记反馈 |
| ✅ 复习持久化服务 | `knowledge_review.py` — knowledge_reviews.json，自动初始化 |
| ✅ 今日复习 Dialog | 逐张显示到期卡片，掌握/模糊/不会反馈按钮 |
| ✅ 复习答案卡片 | 选择后展示核心概念、应用场景、来源，点击"下一张"继续 |
| ✅ 简报复习集成 | 每日简报包含待复习数量 |
| ✅ 复习到期提醒 | 每日一次去重提醒 |
| ✅ 天气 WMO 码补全 | 新增 8 个缺失码（冻雨、雪粒、冰雹雷暴等） |
| ✅ 天气严重程度分级 | 轻度/中等/较强 |
| ✅ 每日天气预报 | 3 天趋势分析（降水变化、温度变化、雷暴预警） |
| ✅ 天气监测增强 | 新增雷暴、雾、冻雨警报 |
| ✅ 气泡优化 | 删除冗余 Python QLabel，仅 QML 气泡 + 点击暂停交互 |
| ✅ 简报弹窗 | 点击"生成助手简报"弹出完整内容对话框 |
| ✅ AI Agent 去重 | 5 分钟冷却 + 并发锁 + 启动智能跳过 |
| ✅ 知识获取增强 | 四级回退（zh.wiki → en.wiki → 分解查询 → fallback） |
| ✅ 知识内容验证 | 长度、关键点、中文检测、交叉验证 |
| ✅ 知识维度扩展 | 从 6 个扩展到 10 个（新增软件工程、算法、安全、分布式） |
| ✅ 65 个单元测试 | 零回退 |

---

## 2. 当前技术栈和架构

```
Python 3.12 + PySide6 + QML
│
├── table_miku/
│   ├── app.py                 # 主窗口、菜单、Dialog、服务集成（~1100行）
│   ├── qml/PetScene.qml       # QML 桌宠渲染、气泡动画、点击交互
│   ├── review_scheduler.py    # [新] 纯逻辑间隔重复调度器
│   ├── knowledge_review.py    # [新] 复习持久化服务
│   ├── knowledge_base.py      # 知识卡片获取、构建、验证（4级回退）
│   ├── assistant_core.py      # 简报、AI Agent、定时任务
│   ├── agent_adapter.py       # DeepSeek/OpenAI API 适配
│   ├── assistant_data.py      # 课程表、投递记录、面试复盘
│   ├── assistant_log.py       # 助手事件日志（JSONL）
│   ├── reminders.py           # 定时提醒、课程提醒、番茄钟、复习提醒
│   ├── pomodoro.py            # 番茄钟纯逻辑状态机
│   ├── planner.py             # 学习目标计划生成
│   ├── goal_parser.py         # 自然语言目标解析
│   ├── weather.py             # 天气查询、地理编码、趋势分析
│   ├── weather_monitor.py     # 主动天气监测服务
│   ├── system_monitor.py      # CPU/内存/网络监测
│   ├── storage.py             # JSON 原子化读写
│   ├── encoding_utils.py      # 乱码检测修复
│   ├── paths.py               # 路径策略
│   ├── sprites.py             # 精灵资源导出
│   └── startup.py             # 开机自启
│
├── tests/
│   ├── test_review_scheduler.py  # [新] 20 个调度器测试
│   ├── test_knowledge_review.py  # [新] 12 个复习服务测试
│   ├── test_knowledge_base.py    # 10 个知识获取测试
│   ├── test_goal_parser.py       # 11 个目标解析测试
│   ├── test_pomodoro.py          # 8 个番茄钟测试
│   └── test_encoding_utils.py    # 4 个编码测试
│
├── data/
│   ├── settings.json             # 用户配置
│   ├── goals.json                # 学习目标
│   ├── knowledge_base.json       # 知识卡片库
│   └── knowledge_reviews.json    # [新] 复习状态
│
└── docs/
```

---

## 3. 下一阶段任务（v0.3.0 — 求职闭环）

### 优先级 P0

| 任务 | 说明 | 建议文件 |
|------|------|---------|
| 投递状态机 | 准备中→已投递→笔试→面试→Offer→拒绝→归档 | `assistant_data.py` |
| 下一步提醒 | 每条投递有 `next_step` 和 `next_action_at` | `assistant_data.py` |
| 投递到期提醒 | 定时检查需要跟进的投递 | `reminders.py` |
| 面试问题结构化 | 问题、我的回答、标准答案、薄弱知识点 | `assistant_data.py` |
| 薄弱点→知识卡片 | 面试复盘中的薄弱点一键生成复习卡片 | `knowledge_review.py` |
| 简报求职集成 | 每日简报提示需要跟进的投递/面试 | `assistant_core.py` |

### 优先级 P1

| 任务 | 说明 | 建议文件 |
|------|------|---------|
| 知识库手动编辑 | 用户可编辑知识卡片内容，不被刷新覆盖 | `knowledge_base.py` |
| 复习统计面板 | 展示各主题掌握度、复习次数、学习曲线 | `app.py` 新 Dialog |
| 知识搜索 | 在知识库中搜索关键词 | `knowledge_base.py` |
| 投递/面试导入导出 | JSON/CSV 导入导出 | `assistant_data.py` |

### 优先级 P2

| 任务 | 说明 |
|------|------|
| 场景化对话窗口 | 多轮聊天、意图路由 |
| 语音 TTS/STT | 语音交互 |
| 多知识源爬虫 | 知乎、博客园、GitHub Trending |
| 打包发布 | PyInstaller 打包、首次使用引导 |

---

## 4. 如何再次进入 Claude Code 开发模式

### 前置条件

```powershell
# 1. 确保 Python 3.12+ 和虚拟环境
cd D:\AIWorkspace\projects\Table-Miku
.\.venv\Scripts\python.exe --version

# 2. 确保依赖完整
.\.venv\Scripts\pip.exe install -r requirements.txt

# 3. 运行测试确认环境正常
.\.venv\Scripts\python.exe -m pytest tests\ -v
```

### 启动 Claude Code

```powershell
# 在项目根目录启动 Claude Code
cd D:\AIWorkspace\projects\Table-Miku
claude
```

### 交给 Claude 的开发提示

在 Claude Code 对话中，直接给出任务描述即可。以下是一个模板：

```text
请阅读以下文件了解项目当前状态：
- docs/DEVELOPMENT_STAGE_v0.2.md（本文件）
- TableMiku_CLAUDEPROMPT.md（详细的下一阶段提示文档）
- table_miku/app.py（主入口）
- table_miku/knowledge_base.py（知识获取）
- table_miku/knowledge_review.py（复习服务）

然后按照以下要求进行开发：
[这里写你的具体需求]

完成后先跑测试：
.\.venv\Scripts\python.exe -m pytest tests\ -v
```

### 常用命令速查

```powershell
# 编译检查
.\.venv\Scripts\python.exe -m py_compile main.py table_miku\*.py

# 运行测试
.\.venv\Scripts\python.exe -m pytest tests\ -v

# 运行单个测试文件
.\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py -v

# 启动 GUI（手工验收）
.\.venv\Scripts\python.exe main.py

# 查看 git 状态
git status --short --branch
git log --oneline -10

# 打包 exe
pyinstaller --onefile --windowed --icon=assets/miku.ico main.py
```

### 开发约束（交给 Claude 时必须遵守）

1. **QML 只做动画和基础事件**，不把业务逻辑写进 `PetScene.qml`
2. **复习状态不与知识卡片混存**，独立保存为 `knowledge_reviews.json`
3. **持久化必须走** `storage.read_json()` / `storage.write_json()`
4. **测试不依赖**真实网络、Qt GUI 或 API Key
5. **不提交** `.env.local`、API Key、个人绝对路径、`.venv`、`__pycache__`
6. **不引入新第三方依赖**，除非现有能力无法完成
7. **尽量不大规模重构** `app.py`（已接近 1100 行，后续考虑拆分）

---

## 5. 版本路线图

| 版本 | 内容 | 状态 |
|------|------|------|
| `v0.1.0` | 基础桌宠 | ✅ 已发布 |
| `v0.1.1` | 天气、系统监测、AI 助理 | ✅ 已发布 |
| `v0.1.2` | 结构化知识库、乱码修复 | ✅ 已发布 |
| `v0.2.0` | 知识复习闭环、天气强化、气泡优化 | 🔄 开发完成，待打标签 |
| `v0.3.0` | 求职投递和面试复盘闭环 | 📋 计划中 |
| `v0.4.0` | 场景化对话窗口和意图路由 | 📋 计划中 |
| `v0.5.0` | 打包、发布、首次使用引导 | 📋 计划中 |
| `v1.0.0` | 稳定可转发版本 | 🎯 目标 |

---

## 6. 运行截图的验收清单

在打 `v0.2.0` 标签前，建议完成以下手工验收：

| 编号 | 操作 | 预期 |
|------|------|------|
| G1 | `python main.py` 启动 | Miku 出现在右下角，无崩溃，无重复 AI 简报 |
| G2 | 右键 → 学习 → 今日复习 | 打开 ReviewDialog，显示到期卡片 |
| G3 | 点击掌握/模糊/不会 | 先显示答案卡片（概念+场景+来源），再点"下一张" |
| G4 | 右键 → 生成助手简报 | 弹出对话框显示完整简报内容 |
| G5 | 气泡出现 | 仅 QML 气泡，无双层重叠；点击可暂停/继续 |
| G6 | 右键 → 系统工具 → 提醒当前城市天气 | 显示温度范围 + 趋势分析 |
| G7 | 右键 → 更新计算机知识库 | 10 个主题全部成功（网络正常时） |
| G8 | 等待定时提醒触发 | 复习到期提醒每天只出现一次 |
| G9 | 托盘菜单 → 今日复习 | 可正常打开 |
| G10 | 重启应用 | 复习状态不丢失，设置保留 |
