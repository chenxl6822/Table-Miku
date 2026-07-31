# Table Miku

Table Miku 是一个 Windows 桌面 Miku 桌宠：可以透明置顶、自由拖动、点击互动、提醒学习计划，也可以查询当前城市天气。

这个项目的 v1 目标是先做成一个稳定、能本地运行、能打包转发的桌宠。默认使用本地规则模板；配置 API Key 后，仍需在应用内明确选择单次或持续授权，才会调用 AI 规划和汇报。

## 功能

- 透明无边框桌宠窗口，默认在屏幕右下角显示。
- 左键按住拖动，左键轻点触发随机对话。
- Miku 本体优先使用透明 PNG 精灵图，键盘由程序绘制并带按键动画。
- 默认读取 `assets/sprites/miku_idle.png`；也支持 `miku_happy.png`、`miku_focus.png`、`miku_surprised.png`、`miku_sleepy.png` 做表情切换。
- 右键菜单：
  - 查看今日任务
  - 导入学习目标/时间表
  - 编辑定时提醒
  - 提醒当前城市天气
  - 设置/自动定位城市
  - 立即检测电脑/网络
  - 生成助手简报
  - 立即天气汇报
  - 运行并监视命令
  - AI 规划/汇报（可选）
  - 暂停/开启系统监测
  - 暂停/开启学习提醒
  - 关闭 Miku
- 内置“大二学生准备进入公司实习”学习路线。
- 支持导入自定义目标，并生成每日学习提醒。
- 支持具体时间提醒，例如 `08:30 复习基础`、`20:30 整理项目 README`。
- 天气默认使用 `auto` 通过 IP 粗略定位，也可以手动设置城市名。
- 支持 CPU、内存告警和网络连通性提示：默认检测百度与 Google，适合判断普通上网和 VPN/代理是否正常。
- 支持助手简报、每日天气汇报、命令完成提醒；适合让桌宠盯着测试、打包、长时间脚本是否跑完。
- 支持打包为 `.exe` 后转发给他人使用。

## 项目结构

```text
Table-Miku/
├─ main.py
├─ table_miku/
│  ├─ app.py
│  ├─ agent_adapter.py
│  ├─ assistant_core.py
│  ├─ assistant_log.py
│  ├─ command_runner.py
│  ├─ paths.py
│  ├─ planner.py
│  ├─ reminders.py
│  ├─ storage.py
│  ├─ system_monitor.py
│  └─ weather.py
├─ assets/
│  └─ miku.svg
├─ data/
│  ├─ goals.json
│  └─ settings.json
├─ requirements.txt
└─ README.md
```

## 环境要求

- Windows 10/11
- Python 3.12 或更新版本
- Git

## 从 GitHub 拉取运行

```powershell
git clone https://github.com/chenxl6822/Table-Miku.git
cd Table-Miku
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

如果 PowerShell 禁止运行虚拟环境激活脚本，可以改用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## 打包为 exe

安装依赖后执行项目构建脚本；该入口不依赖 PowerShell 脚本执行策略：

```powershell
.\.venv\Scripts\python.exe build.py
```

打包成功后，可执行文件会出现在：

```text
dist/TableMiku/TableMiku.exe
```

可以把整个 `dist/TableMiku/` 文件夹压缩后转发给他人。

## 配置说明

配置和运行数据统一保存在：

```text
%APPDATA%/TableMiku/
```

首次使用新版源码运行时，程序会把项目旧 `data/` 目录中的同名运行文件复制到上述目录，旧文件不会被删除。主要文件包括：

- `settings.json`：城市、提醒开关、提醒间隔、免打扰时间、系统监测配置。
- `goals.json`：学习目标和学习计划。

### 修改城市

打开 `%APPDATA%\TableMiku\settings.json`，修改：

```json
{
  "city": "雨湖区,湘潭,湖南"
}
```

建议填写 `区县,城市,省份`，例如 `雨湖区,湘潭,湖南`。`auto` 表示使用 IP 自动定位，但 IP 定位会受 VPN、代理、运营商出口影响，不保证等于真实所在地，也通常无法精确到区县。

天气位置解析会优先使用 OpenStreetMap Nominatim 地理库做省/市/区县消歧，再回退到 Open-Meteo 地理编码；天气数据使用 Open-Meteo 当前天气接口。

### 修改定时提醒

打开 `%APPDATA%\TableMiku\settings.json`，修改：

```json
{
  "scheduled_reminders": [
    {
      "time": "08:30",
      "task": "复习编程基础，整理今天要攻克的知识点。"
    },
    {
      "time": "20:30",
      "task": "整理简历/项目 README，复盘今天的学习成果。"
    }
  ]
}
```

也可以在桌宠右键菜单中选择“编辑定时提醒”，每行按 `HH:MM 任务内容` 输入。

### 修改系统监测

系统监测默认开启：每 30 秒采样一次 CPU 和内存；CPU 连续 3 次超过 85% 会告警，内存连续 2 次超过 88% 会告警，恢复后也会提示。网络默认每 2 分钟检测一次百度和 Google，启动时和右键“立即检测电脑/网络”会立刻汇报。

打开 `%APPDATA%\TableMiku\settings.json`，可以调整：

```json
{
  "system_monitor": {
    "enabled": true,
    "cpu_warning_percent": 85,
    "memory_warning_percent": 88,
    "network_check_interval_minutes": 2,
    "network_targets": [
      {"name": "百度", "url": "https://www.baidu.com/"},
      {"name": "Google", "url": "https://www.google.com/generate_204"}
    ]
  }
}
```

百度能连、Google 不能连，通常表示国内网络可用但 Google/VPN/代理出口有问题；两者都不能连，通常表示断网、DNS 或代理配置异常。

网络检测会分层记录 DNS、TCP、TLS、HTTP 状态和延迟；非手动检测时需要连续异常才会告警，避免一次网络抖动就误判。内存检测同时参考已用百分比和可用 MB，默认可用内存低于 `1024 MB` 也会进入压力判断。

### 天气定位与预警

天气查询支持三种定位方式：

- 推荐：填写 `区县,城市,省份`，例如 `雨湖区,湘潭,湖南`。
- 精确：填写坐标 `纬度,经度`，例如 `27.8563,112.9000`。
- 兜底：填写 `auto` 使用 IP 定位；该结果会标记为低置信度，可能受 VPN、代理和运营商出口影响。

地理编码结果会缓存在本地用户数据目录，减少重复联网解析。主动天气监测会读取 `weather_alerts.lead_minutes`，结合 Open-Meteo 当前天气和未来小时级预报提醒雷暴、降雨、降雪、冻雨、大风、高温和低温。

### 可信知识源

知识库现在优先走本地 SQLite Repository，并保留旧 `knowledge_base.json` 迁移/兜底。可信来源优先级为：

```text
官方文档 / RFC / 标准 / 论文元数据 > Obsidian 只读笔记 > Wikipedia > 离线种子
```

如需接入本地 Obsidian 知识库，可在 `%APPDATA%\TableMiku\settings.json` 中配置：

```json
{
  "knowledge": {
    "trusted_sources": {
      "enabled": true,
      "obsidian_vault": "D:\\AIWorkspace\\projects\\Obsidian Vault\\计算机知识"
    }
  }
}
```

Table-Miku 只读取该目录下 Markdown 摘要并写入自己的 `knowledge.db`，不会修改、删除、移动或格式化 Obsidian Vault 文件；包含 `.env`、token、secret、password、key、密钥、凭据等敏感命名的路径会被跳过。

### 修改个人助手

个人助手默认开启：启动后生成一次简报；每天 `08:10` 做天气汇报，`08:20` 做今日简报；右键“运行并监视命令”可以让 Miku 运行 PowerShell 命令，命令结束后自动提示退出码和简短输出。

```json
{
  "assistant": {
    "enabled": true,
    "daily_brief_time": "08:20",
    "weather_report_time": "08:10",
    "startup_brief": true,
    "command_max_output_chars": 420,
    "command_timeout_seconds": 600,
    "ai_agent_enabled": false,
    "ai_model": "gpt-5-nano"
  }
}
```

命令监视输入示例：

```powershell
cwd=D:\code\Table Pet
.\.venv\Scripts\python.exe -m compileall main.py table_miku
```

命令使用当前系统 PowerShell 执行策略，不会启用 `ExecutionPolicy Bypass`；默认 600 秒超时，可从右键菜单取消。界面只显示有限的末尾输出，事件日志不保存命令或输出全文。

`AI 规划/汇报（可选）` 默认不会调用云端模型。配置 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY` 后，还需要在右键菜单选择“开启 AI 助理”，查看将发送的数据范围，并选择“仅运行这一次”或“持续启用”。持续授权可随时从同一菜单撤销。未安装 `openai-agents` 时，OpenAI 提供商会使用 Responses API。

## 导入学习目标格式

右键选择“导入学习目标/时间表”，可以粘贴自然语言、Markdown 列表或 JSON。

```text
目标：大二暑假前拿到后端开发实习
每天 90 分钟
08:30 复习 Java/Python 基础
14:30 刷 2 道算法题
20:30 整理项目 README 和简历
```

也支持 JSON：

```json
{
  "goals": [
    {
      "title": "准备软件开发实习",
      "daily_minutes": 90
    }
  ],
  "schedule": [
    {
      "time": "08:30",
      "task": "复习基础"
    }
  ]
}
```

## 使用自己的 Miku 图片

当前版本默认优先使用真实 PNG 精灵图，`assets/miku.svg` 仅作为窗口图标和备用资源。仓库已内置一张 AI 生成的五表情横向精灵图：

```text
assets/sprites/miku_sprite_sheet.png
```

表情顺序为 `idle`、`focus`、`happy`、`surprised`、`sleepy`。程序会自动裁剪并尽量去除棋盘格假透明背景。

仓库也内置了 AI 生成的键盘素材：

```text
assets/sprites/keyboard.png
```

程序会优先使用这张键盘图片，以匹配 Miku 的半立体可爱画风；图片缺失时才回退到代码绘制键盘。

推荐把风格接近参考图的透明 PNG 放到：

```text
assets/sprites/miku_idle.png
```

如需表情变化，可以继续放入：

```text
assets/sprites/miku_happy.png
assets/sprites/miku_focus.png
assets/sprites/miku_surprised.png
assets/sprites/miku_sleepy.png
```

如果没有本地精灵图，程序会尝试从代码中的参考 URL 缓存下载一张 chibi Miku 透明图。网上 PNG 聚合站的素材通常只适合个人学习或非商业使用；如果要转发或发布，请替换为你确认授权可用的素材。

建议图片使用透明背景，主体接近正方形，角色朝向正面或略微俯视，这样和键盘叠加效果更自然。

## 个人助理增强

- 右键菜单新增番茄钟、课程表 PDF 导入、投递记录、面试复盘、助理记录查看、AI 助理开关和开机自启。
- AI 助理只有在用户查看发送范围并选择单次或持续授权后才会调用外部模型；持续授权可随时撤销。
- AI 调用会读取环境变量或项目根目录 `.env.local` / `.env` 中的 API Key；事件日志只保留脱敏后的 provider、model、response id、usage 和授权类型等元数据。
- 课程表 PDF 导入需要 `pypdf` 依赖；执行 `pip install -r requirements.txt` 后可在右键菜单选择 PDF。
- 投递记录保存到 `applications.json`，面试复盘保存到 `interviews.json`，课程表保存到 `timetable.json`；打包后这些文件位于 `%APPDATA%/TableMiku/`。
- 开机自启通过 Windows 启动文件夹中的 `TableMiku.cmd` 实现，可在右键菜单开启或关闭。

## 常见问题

### 运行时报 `No module named PySide6`

说明依赖还没安装：

```powershell
pip install -r requirements.txt
```

### PowerShell 不能执行 npm 或虚拟环境脚本

本项目运行不依赖 npm。虚拟环境如果无法激活，可以直接使用 `.venv` 里的 Python：

```powershell
.\.venv\Scripts\python.exe main.py
```

### 天气查询失败

天气功能使用 Open-Meteo 免费接口，需要联网。网络不可用时桌宠不会崩溃，只会提示查询失败。

### 提醒没有弹出

检查 `%APPDATA%\TableMiku\settings.json`：

- `reminders_enabled` 是否为 `true`
- `reminder_interval_minutes` 是否太长
- 当前时间是否处于 `quiet_hours` 免打扰时间

### 打包失败

先确认 PyInstaller 已安装：

```powershell
pip install pyinstaller
```

然后重新执行打包命令：

```powershell
.\.venv\Scripts\python.exe build.py
```

如果仍然失败，可以先用源码运行方式使用项目。

## 后续可拓展

- 增加更多动作帧和动画。
- 增加托盘图标和开机自启。
- 接入 AI API，让目标规划更个性化。
- 支持语音提醒。
- 支持番茄钟、课程表、投递记录和面试复盘。

## 开发

### 环境
- Python 3.12+
- PySide6 >= 6.7

### 安装
```powershell
git clone https://github.com/chenxl6822/Table-Miku.git
cd Table-Miku
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

如果 PowerShell 禁止运行虚拟环境激活脚本，可以改用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### 运行
```powershell
.\.venv\Scripts\python.exe main.py
```

### 测试
```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:TABLE_MIKU_DATA_DIR = Join-Path $env:TEMP "TableMiku-test-data"
.\.venv\Scripts\python.exe -m ruff check main.py table_miku tests
.\.venv\Scripts\python.exe -m pytest --cov=table_miku --cov-branch --cov-report=term-missing
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
```

### 打包
```powershell
.\.venv\Scripts\python.exe build.py
```

完整维护与发布检查见 [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md)。

## 许可证

Table Miku 源代码采用 [MIT License](LICENSE) 授权。

角色图片、精灵图、图标、音频及其他媒体资源不属于 MIT 授权范围。除非资源附有单独的许可证或署名说明，否则其权利由相应权利人保留；复制、修改、分发或商用前请自行确认授权。详见 [ASSET_LICENSE.md](ASSET_LICENSE.md)。
