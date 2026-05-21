# Table Miku 多平台适配说明

Table Miku 当前主程序是 Windows/PySide6 桌面宠物。为了后续支持 Android 和 iOS，本轮把可复用能力尽量放在 UI 无关模块中：

- `table_miku/agent_adapter.py`：AI provider 适配，当前支持 DeepSeek OpenAI-compatible Chat Completions。
- `table_miku/assistant_data.py`：课程表、投递记录、面试复盘和课程时间表解析。
- `table_miku/knowledge_base.py`：计算机网络、计算机组成原理、数据结构、操作系统、编译原理、数据库原理知识缓存；优先从 Wikipedia 更新，断网时使用本地备用摘要。
- `table_miku/reminders.py`：番茄钟、课程提醒和普通提醒的调度规则。

## Android / iOS 路线

1. 保留这些核心模块的数据结构：`settings.json`、`timetable.json`、`knowledge_base.json`、`applications.json`、`interviews.json`。
2. 移动端 UI 使用原生 Kotlin/Swift 或 Flutter 重做，读取同一套 JSON schema。
3. AI 调用继续走 DeepSeek 的 OpenAI-compatible API：`https://api.deepseek.com/chat/completions`，默认模型 `deepseek-v4-flash`，重规划可切换 `deepseek-v4-pro`。
4. 课程提醒在移动端应使用系统通知能力：Android WorkManager/AlarmManager，iOS UserNotifications。
5. 桌宠动画资源可复用 `assets/sprites/miku_sprite_sheet.png`，移动端按帧切图或使用 Lottie/原生 sprite 动画。

PySide6 本身不适合作为 Android/iOS 直接运行时，所以移动端建议复用核心数据与 AI/提醒规则，而不是直接移植窗口层代码。
