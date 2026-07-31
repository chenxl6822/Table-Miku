# Table Miku 维护指南

## 本地质量门槛

使用 Python 3.12 和项目虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:QT_QPA_PLATFORM = "offscreen"
$env:TABLE_MIKU_DATA_DIR = Join-Path $env:TEMP "TableMiku-test-data"
.\.venv\Scripts\python.exe -m ruff check main.py table_miku tests
.\.venv\Scripts\python.exe -m pytest --cov=table_miku --cov-branch --cov-report=term-missing
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
```

覆盖率门槛从当前全项目基线 40% 起步。新增或修改逻辑应优先补回归测试，不应通过降低门槛来让 CI 通过。

## 数据与迁移

- 可写运行数据位于 `%APPDATA%\TableMiku`，测试可用 `TABLE_MIKU_DATA_DIR` 定向到临时目录。
- 源码旧 `data/` 中的同名运行文件只复制一次，旧文件不自动删除。
- SQLite schema 由 `table_miku/knowledge_db.py` 的版本迁移维护；迁移必须幂等，并为已有数据增加回归测试。
- 打包产物不得包含 `data/`、`.env`、日志、数据库、缓存或个人记录。

## 安全边界

- AI 只有在用户选择单次或持续授权后才可发送界面列出的摘要；取消是安全默认值，持续授权可在菜单撤销。
- 命令监视不得使用 `ExecutionPolicy Bypass`，必须保留超时、取消和有限输出。
- 事件日志必须经过脱敏并保持轮换；不要记录 API Key、Token、密码、命令全文或输出全文。
- SQLite 写入失败时不得静默写入旧 JSON，避免形成两个相互矛盾的数据源。

## 发布检查

1. 从干净环境安装 `requirements-dev.txt`，运行 lint、完整测试、覆盖率和依赖审计。
2. 运行 `.\.venv\Scripts\python.exe build.py`，确认 `dist\TableMiku\TableMiku.exe` 存在。
3. 检查打包目录不含运行数据、凭据和个人记录。
4. 在真实 Windows 桌面手动验证启动、托盘、QML 动画、AI 授权、天气查询和命令取消。
5. 由维护者选择并加入明确的 `LICENSE` 后，才能把许可证信息写入发布说明。
