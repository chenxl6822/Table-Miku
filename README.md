# Table Miku

Table Miku 是一个 Windows 桌面 Miku 桌宠：可以透明置顶、自由拖动、点击互动、提醒学习计划，也可以查询当前城市天气。

这个项目的 v1 目标是先做成一个稳定、能本地运行、能打包转发的桌宠。规划能力暂时使用本地规则模板，不需要配置 AI API Key。

## 功能

- 透明无边框桌宠窗口，默认在屏幕右下角显示。
- 左键按住拖动，左键轻点触发随机对话。
- Miku 是自绘动态形象，会眨眼、轻微浮动并敲键盘，不再依赖单张贴图。
- 右键菜单：
  - 查看今日任务
  - 导入学习目标/时间表
  - 编辑定时提醒
  - 提醒当前城市天气
  - 设置/自动定位城市
  - 暂停/开启学习提醒
  - 关闭 Miku
- 内置“大二学生准备进入公司实习”学习路线。
- 支持导入自定义目标，并生成每日学习提醒。
- 支持具体时间提醒，例如 `08:30 复习基础`、`20:30 整理项目 README`。
- 天气默认使用 `auto` 通过 IP 粗略定位，也可以手动设置城市名。
- 支持打包为 `.exe` 后转发给他人使用。

## 项目结构

```text
Table-Miku/
├─ main.py
├─ table_miku/
│  ├─ app.py
│  ├─ paths.py
│  ├─ planner.py
│  ├─ reminders.py
│  ├─ storage.py
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

安装依赖后执行：

```powershell
pyinstaller --noconsole --name TableMiku --add-data "assets;assets" main.py
```

打包成功后，可执行文件会出现在：

```text
dist/TableMiku/TableMiku.exe
```

可以把整个 `dist/TableMiku/` 文件夹压缩后转发给他人。

## 配置说明

配置文件在源码运行时位于 `data/` 目录：

- `data/settings.json`：城市、提醒开关、提醒间隔、免打扰时间。
- `data/goals.json`：学习目标和学习计划。

打包成 `.exe` 后，用户数据会保存到：

```text
%APPDATA%/TableMiku/
```

### 修改城市

打开 `data/settings.json`，修改：

```json
{
  "city": "auto"
}
```

`auto` 表示使用 IP 自动定位。也可以改成 `Beijing`、`Guangzhou`、`Shenzhen` 等城市名。

### 修改定时提醒

打开 `data/settings.json`，修改：

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

当前版本默认使用程序自绘的动态敲键盘 Miku，`assets/miku.svg` 仅作为窗口图标和备用资源。

如果你想使用自己的图片，把图片放到：

```text
assets/miku.png
```

程序会优先读取 `miku.png`，其次读取 `miku.jpg`，最后读取内置的 `miku.svg`。

建议使用透明背景或接近正方形的图片，这样桌宠显示效果更好。

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

检查 `data/settings.json`：

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
pyinstaller --noconsole --name TableMiku --add-data "assets;assets" main.py
```

如果仍然失败，可以先用源码运行方式使用项目。

## 后续可拓展

- 增加更多动作帧和动画。
- 增加托盘图标和开机自启。
- 接入 AI API，让目标规划更个性化。
- 支持语音提醒。
- 支持番茄钟、课程表、投递记录和面试复盘。
