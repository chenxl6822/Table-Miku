from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .knowledge_assistant.auth import Principal
from .knowledge_assistant.client import KnowledgeAssistantApiError
from .knowledge_assistant_desktop import KnowledgeAssistantDesktopController


CONSOLE_STYLE = """
QDialog {
    background: #f4f7fb;
    color: #1e2a3b;
}
QGroupBox {
    border: 1px solid #cbd6e4;
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QTableWidget {
    background: #ffffff;
    border: 1px solid #c7d2e2;
    border-radius: 6px;
    selection-background-color: #bcecf6;
    selection-color: #13253b;
}
QLineEdit, QComboBox, QSpinBox {
    min-height: 28px;
    padding: 2px 7px;
}
QPlainTextEdit {
    padding: 7px;
}
QPushButton {
    min-height: 30px;
    border: 1px solid #b7c6d9;
    border-radius: 6px;
    padding: 4px 12px;
    background: #ffffff;
    font-weight: 600;
}
QPushButton:hover:enabled {
    background: #e9f8fb;
    border-color: #64c7d9;
}
QPushButton:disabled {
    color: #8b97a8;
    background: #edf1f6;
}
QTabWidget::pane {
    border: 1px solid #cbd6e4;
    background: #ffffff;
    border-radius: 7px;
}
QTabBar::tab {
    min-width: 112px;
    padding: 8px 12px;
    background: #e8edf5;
    border: 1px solid #cbd6e4;
}
QTabBar::tab:selected {
    background: #ffffff;
    border-bottom-color: #ffffff;
}
QHeaderView::section {
    background: #eaf0f7;
    border: 0;
    border-right: 1px solid #d2dbe8;
    border-bottom: 1px solid #d2dbe8;
    padding: 6px;
    font-weight: 600;
}
"""


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class UploadDocumentDialog(QDialog):
    def __init__(
        self,
        controller: KnowledgeAssistantDesktopController,
        parent: QWidget | None = None,
        *,
        draft: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("上传并索引文档")
        self.resize(650, 240)
        self.setStyleSheet(CONSOLE_STYLE)
        layout = QVBoxLayout(self)
        intro_text = "这是人工直接写入。文件会在本机解析、切分、向量化并进入所选集合。"
        if draft is not None:
            intro_text += " 正在重试结果未知的原请求；路径、集合、原始字节与幂等键已冻结，不可修改。"
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self)
        self.path_edit.setObjectName("uploadPath")
        self.path_edit.setPlaceholderText("选择 TXT、Markdown、RST、JSON 或文本层 PDF")
        self.path_edit.setText((draft or {}).get("path", ""))
        browse = QPushButton("选择文件…", self)
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        form.addRow("文档文件", path_row)
        self.collection_edit = QLineEdit((draft or {}).get("collection_id", "default"), self)
        self.collection_edit.setObjectName("uploadCollection")
        form.addRow("目标集合", self.collection_edit)
        self.idempotency_edit = QLineEdit(
            (draft or {}).get("idempotency_key")
            or controller.new_idempotency_key("desktop-upload"),
            self,
        )
        self.idempotency_edit.setObjectName("uploadIdempotencyKey")
        self.idempotency_edit.setToolTip("重复提交相同键与相同内容会返回原结果；相同键不同内容会被拒绝。")
        form.addRow("幂等键", self.idempotency_edit)
        if draft is not None:
            self.path_edit.setReadOnly(True)
            self.collection_edit.setReadOnly(True)
            self.idempotency_edit.setReadOnly(True)
            browse.setEnabled(False)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_button.setText("上传并索引")
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setDefault(True)
        cancel_button.setFocus()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择知识文档",
            "",
            "支持的文档 (*.txt *.md *.markdown *.rst *.json *.pdf);;所有文件 (*)",
        )
        if filename:
            self.path_edit.setText(filename)

    def accept(self) -> None:
        if not self.path_edit.text().strip():
            QMessageBox.warning(self, "缺少文件", "请先选择需要索引的文档。")
            return
        if not self.collection_edit.text().strip():
            QMessageBox.warning(self, "缺少集合", "请填写目标集合。")
            return
        if not self.idempotency_edit.text().strip():
            QMessageBox.warning(self, "缺少幂等键", "请填写幂等键。")
            return
        super().accept()


class IngestTaskDialog(QDialog):
    def __init__(
        self,
        controller: KnowledgeAssistantDesktopController,
        parent: QWidget | None = None,
        *,
        draft: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("创建 Agent 写入审批任务")
        self.resize(680, 520)
        self.setStyleSheet(CONSOLE_STYLE)
        layout = QVBoxLayout(self)
        intro_text = "这里只创建 awaiting_approval 任务，不会立即写入。必须由另一位审批人读取精确预览后决定。"
        if draft is not None:
            intro_text += " 正在重试结果未知的原请求；目标、正文与幂等键已冻结，不可修改。"
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.filename_edit = QLineEdit((draft or {}).get("filename", "agent-note.md"), self)
        self.filename_edit.setObjectName("taskFilename")
        self.collection_edit = QLineEdit((draft or {}).get("collection_id", "default"), self)
        self.collection_edit.setObjectName("taskCollection")
        self.idempotency_edit = QLineEdit(
            (draft or {}).get("idempotency_key")
            or controller.new_idempotency_key("desktop-task"),
            self,
        )
        self.idempotency_edit.setObjectName("taskIdempotencyKey")
        form.addRow("目标文件名", self.filename_edit)
        form.addRow("目标集合", self.collection_edit)
        form.addRow("幂等键", self.idempotency_edit)
        layout.addLayout(form)
        layout.addWidget(QLabel("待审批原文（仅在审批专用预览中回显）"))
        self.content_edit = QPlainTextEdit(self)
        self.content_edit.setObjectName("taskContent")
        self.content_edit.setPlaceholderText("输入等待审批的 UTF-8 文本…")
        self.content_edit.setPlainText((draft or {}).get("content", ""))
        if draft is not None:
            self.filename_edit.setReadOnly(True)
            self.collection_edit.setReadOnly(True)
            self.idempotency_edit.setReadOnly(True)
            self.content_edit.setReadOnly(True)
        layout.addWidget(self.content_edit, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_button.setText("提交审批任务")
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setDefault(True)
        cancel_button.setFocus()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        required = (
            (self.filename_edit.text(), "请填写目标文件名。"),
            (self.collection_edit.text(), "请填写目标集合。"),
            (self.idempotency_edit.text(), "请填写幂等键。"),
            (self.content_edit.toPlainText(), "请填写待审批原文。"),
        )
        for value, message in required:
            if not value.strip():
                QMessageBox.warning(self, "输入不完整", message)
                return
        super().accept()


class KnowledgeAssistantDialog(QDialog):
    """Local visual console for the enterprise Knowledge Assistant vertical slice."""

    def __init__(
        self,
        controller: KnowledgeAssistantDesktopController | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller or KnowledgeAssistantDesktopController()
        self._documents: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._approval_preview: dict[str, Any] | None = None
        self._approval_task_id = ""
        self._last_trace_id = ""
        self._upload_draft: dict[str, Any] | None = None
        self._ingest_task_draft: dict[str, Any] | None = None
        self._archive_task_draft: dict[str, Any] | None = None
        self._needs_refresh_on_show = False
        self.setObjectName("knowledgeAssistantDialog")
        self.setWindowTitle("Table Miku · Knowledge Assistant 管理台")
        self.resize(1180, 790)
        self.setMinimumSize(980, 680)
        self.setStyleSheet(CONSOLE_STYLE)

        root = QVBoxLayout(self)
        title = QLabel("Knowledge Assistant 2.1 · 本地可视化管理台", self)
        title.setFont(QFont("Microsoft YaHei UI", 16, QFont.Weight.Bold))
        root.addWidget(title)
        notice = QLabel(
            f"连接：{self.controller.connection_label}。默认由桌面应用托管私有 loopback API，不需要启动 PowerShell。"
            "身份选择仅用于本地验收 RBAC，不是生产登录；生产仍必须使用可信身份网关。"
        )
        notice.setObjectName("localIdentityWarning")
        notice.setWordWrap(True)
        notice.setStyleSheet("background:#fff5d9;border:1px solid #e4c978;border-radius:6px;padding:8px;")
        root.addWidget(notice)
        root.addWidget(self._build_identity_panel())
        self._active_principal = self.controller.principal(
            self.tenant_edit.text(),
            self.user_edit.text(),
            str(self.role_combo.currentData() or "viewer"),
            self.collections_edit.text(),
        )
        self._identity_dirty = False

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("knowledgeAssistantTabs")
        self.tabs.addTab(self._build_documents_tab(), "文档")
        self.tabs.addTab(self._build_query_tab(), "RAG 查询")
        self.tabs.addTab(self._build_tasks_tab(), "任务与审批")
        self.tabs.addTab(self._build_observability_tab(), "观测")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("就绪", self)
        self.status_label.setObjectName("consoleStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        footer.addWidget(self.status_label, 1)
        close_button = QPushButton("关闭管理台", self)
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        root.addLayout(footer)

        self._update_action_permissions()
        self.refresh_all()

    def _build_identity_panel(self) -> QGroupBox:
        group = QGroupBox("当前本地验收身份", self)
        layout = QGridLayout(group)
        self.tenant_edit = QLineEdit("tenant-local", group)
        self.tenant_edit.setObjectName("identityTenant")
        self.user_edit = QLineEdit("local-viewer", group)
        self.user_edit.setObjectName("identityUser")
        self.role_combo = QComboBox(group)
        self.role_combo.setObjectName("identityRole")
        self.role_combo.addItem("Viewer（只读）", "viewer")
        self.role_combo.addItem("Editor（上传/创建任务）", "editor")
        self.role_combo.addItem("Approver（审批他人任务）", "approver")
        self.role_combo.addItem("Admin（全部权限，仍禁止自审批）", "admin")
        self.collections_edit = QLineEdit(group)
        self.collections_edit.setObjectName("identityCollections")
        self.collections_edit.setPlaceholderText("可选，逗号分隔；留空表示全部集合")
        apply_button = QPushButton("应用身份并刷新", group)
        apply_button.setObjectName("applyIdentity")
        apply_button.clicked.connect(self._apply_identity)
        layout.addWidget(QLabel("租户"), 0, 0)
        layout.addWidget(self.tenant_edit, 0, 1)
        layout.addWidget(QLabel("用户"), 0, 2)
        layout.addWidget(self.user_edit, 0, 3)
        layout.addWidget(QLabel("角色"), 0, 4)
        layout.addWidget(self.role_combo, 0, 5)
        layout.addWidget(QLabel("集合范围"), 1, 0)
        layout.addWidget(self.collections_edit, 1, 1, 1, 4)
        layout.addWidget(apply_button, 1, 5)
        layout.setColumnStretch(1, 2)
        layout.setColumnStretch(3, 2)
        layout.setColumnStretch(5, 2)
        self.tenant_edit.textEdited.connect(self._identity_draft_changed)
        self.user_edit.textEdited.connect(self._identity_draft_changed)
        self.collections_edit.textEdited.connect(self._identity_draft_changed)
        self.role_combo.currentIndexChanged.connect(self._identity_draft_changed)
        return group

    def _build_documents_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        actions = QHBoxLayout()
        refresh = QPushButton("刷新文档", tab)
        refresh.clicked.connect(self._refresh_documents)
        self.upload_button = QPushButton("上传并索引…", tab)
        self.upload_button.setObjectName("uploadDocument")
        self.upload_button.clicked.connect(self._upload_document)
        self.archive_task_button = QPushButton("为所选文档创建归档审批任务", tab)
        self.archive_task_button.setObjectName("createArchiveTask")
        self.archive_task_button.clicked.connect(self._create_archive_task)
        actions.addWidget(refresh)
        actions.addWidget(self.upload_button)
        actions.addWidget(self.archive_task_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.document_table = self._table(
            ["文件名", "集合", "状态", "Chunks", "字节", "创建者", "创建时间", "文档 ID"]
        )
        self.document_table.setObjectName("documentTable")
        self.document_table.itemSelectionChanged.connect(self._document_selected)
        layout.addWidget(self.document_table, 1)
        layout.addWidget(QLabel("文档元数据"))
        self.document_detail = QPlainTextEdit(tab)
        self.document_detail.setObjectName("documentDetail")
        self.document_detail.setReadOnly(True)
        self.document_detail.setMaximumHeight(145)
        layout.addWidget(self.document_detail)
        return tab

    def _build_query_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        form = QGridLayout()
        self.query_edit = QPlainTextEdit(tab)
        self.query_edit.setObjectName("ragQuery")
        self.query_edit.setMaximumHeight(95)
        self.query_edit.setPlaceholderText("输入必须由当前知识库证据回答的问题…")
        self.query_collections_edit = QLineEdit(tab)
        self.query_collections_edit.setObjectName("ragCollections")
        self.query_collections_edit.setPlaceholderText("可选，逗号分隔")
        self.top_k_spin = QSpinBox(tab)
        self.top_k_spin.setRange(1, 8)
        self.top_k_spin.setValue(5)
        self.query_button = QPushButton("检索并生成有据回答", tab)
        self.query_button.setObjectName("runRagQuery")
        self.query_button.clicked.connect(self._run_query)
        form.addWidget(QLabel("问题"), 0, 0)
        form.addWidget(self.query_edit, 0, 1, 1, 5)
        form.addWidget(QLabel("限定集合"), 1, 0)
        form.addWidget(self.query_collections_edit, 1, 1, 1, 2)
        form.addWidget(QLabel("Top K"), 1, 3)
        form.addWidget(self.top_k_spin, 1, 4)
        form.addWidget(self.query_button, 1, 5)
        form.setColumnStretch(2, 1)
        layout.addLayout(form)
        self.answer_state = QLabel("尚未查询", tab)
        self.answer_state.setObjectName("ragAnswerState")
        self.answer_state.setWordWrap(True)
        self.answer_state.setStyleSheet("font-weight:600;padding:5px;")
        layout.addWidget(self.answer_state)
        splitter = QSplitter(Qt.Orientation.Vertical, tab)
        self.answer_edit = QPlainTextEdit(splitter)
        self.answer_edit.setObjectName("ragAnswer")
        self.answer_edit.setReadOnly(True)
        self.citation_table = self._table(
            ["引用", "文件", "集合", "位置", "得分", "证据摘录"], parent=splitter
        )
        self.citation_table.setObjectName("citationTable")
        splitter.setSizes([230, 260])
        layout.addWidget(splitter, 1)
        return tab

    def _build_tasks_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        splitter = QSplitter(Qt.Orientation.Horizontal, tab)
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        task_actions = QHBoxLayout()
        refresh = QPushButton("刷新任务", left)
        refresh.clicked.connect(self._refresh_tasks)
        self.create_task_button = QPushButton("创建写入审批任务…", left)
        self.create_task_button.setObjectName("createIngestTask")
        self.create_task_button.clicked.connect(self._create_ingest_task)
        task_actions.addWidget(refresh)
        task_actions.addWidget(self.create_task_button)
        task_actions.addStretch(1)
        left_layout.addLayout(task_actions)
        self.task_table = self._table(
            ["状态", "工具", "请求人", "审批", "创建时间", "任务 ID"], parent=left
        )
        self.task_table.setObjectName("taskTable")
        self.task_table.itemSelectionChanged.connect(self._task_selected)
        left_layout.addWidget(self.task_table, 1)
        left_layout.addWidget(QLabel("任务安全元数据"))
        self.task_detail = QPlainTextEdit(left)
        self.task_detail.setReadOnly(True)
        self.task_detail.setMaximumHeight(155)
        left_layout.addWidget(self.task_detail)
        left_layout.addWidget(QLabel("操作收据"))
        self.receipt_detail = QPlainTextEdit(left)
        self.receipt_detail.setObjectName("operationReceipt")
        self.receipt_detail.setReadOnly(True)
        self.receipt_detail.setMaximumHeight(145)
        left_layout.addWidget(self.receipt_detail)

        approval = QGroupBox("人工 Action Preview", splitter)
        approval_layout = QVBoxLayout(approval)
        preview_notice = QLabel(
            "审批预览来自不可信 Agent 输入。正文仅按纯文本显示；批准只执行与当前审批人及预览哈希绑定的精确动作。"
        )
        preview_notice.setWordWrap(True)
        approval_layout.addWidget(preview_notice)
        approval_layout.addWidget(QLabel("可信执行契约与后果（结构化字段）"))
        self.preview_editor = QPlainTextEdit(approval)
        self.preview_editor.setObjectName("approvalPreview")
        self.preview_editor.setReadOnly(True)
        self.preview_editor.setPlainText("先选择 awaiting_approval 任务，再加载专用审批预览。")
        approval_layout.addWidget(self.preview_editor, 1)
        self.preview_content_label = QLabel("不可信 Agent 原文（与契约区域隔离，仅按纯文本显示）", approval)
        approval_layout.addWidget(self.preview_content_label)
        self.preview_content_editor = QPlainTextEdit(approval)
        self.preview_content_editor.setObjectName("approvalUntrustedContent")
        self.preview_content_editor.setReadOnly(True)
        self.preview_content_editor.setMaximumHeight(190)
        self.preview_content_editor.setPlainText("当前动作没有已加载的正文。")
        approval_layout.addWidget(self.preview_content_editor)
        self.reject_reason_edit = QLineEdit(approval)
        self.reject_reason_edit.setObjectName("rejectReason")
        self.reject_reason_edit.setPlaceholderText("拒绝原因（可选，不执行写操作）")
        approval_layout.addWidget(self.reject_reason_edit)
        approval_actions = QHBoxLayout()
        self.preview_button = QPushButton("加载精确预览", approval)
        self.preview_button.setObjectName("loadApprovalPreview")
        self.preview_button.clicked.connect(self._load_approval_preview)
        self.approve_button = QPushButton("批准并执行此精确操作", approval)
        self.approve_button.setObjectName("approveExactAction")
        self.approve_button.clicked.connect(self._approve_selected)
        self.reject_button = QPushButton("拒绝，不执行写操作", approval)
        self.reject_button.setObjectName("rejectWithoutSideEffects")
        self.reject_button.clicked.connect(self._reject_selected)
        self.defer_button = QPushButton("暂不处理，保留待审批", approval)
        self.defer_button.setObjectName("deferApproval")
        self.defer_button.setDefault(True)
        self.defer_button.setAutoDefault(True)
        self.defer_button.clicked.connect(self._defer_preview)
        approval_actions.addWidget(self.preview_button)
        approval_actions.addWidget(self.approve_button)
        approval_actions.addStretch(1)
        approval_actions.addWidget(self.reject_button)
        approval_actions.addWidget(self.defer_button)
        approval_layout.addLayout(approval_actions)
        splitter.setSizes([650, 500])
        layout.addWidget(splitter, 1)
        return tab

    def _build_observability_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        actions = QHBoxLayout()
        refresh = QPushButton("刷新聚合指标", tab)
        refresh.clicked.connect(self._refresh_metrics)
        actions.addWidget(refresh)
        actions.addStretch(1)
        layout.addLayout(actions)
        metrics = QGroupBox("当前租户最近 Trace 聚合", tab)
        grid = QGridLayout(metrics)
        metric_names = (
            ("trace_count", "Trace 数"),
            ("error_count", "错误数"),
            ("average", "平均延迟"),
            ("p95", "P95 延迟"),
            ("max", "最大延迟"),
            ("input", "输入 Token（估算）"),
            ("output", "输出 Token（估算）"),
            ("total", "总 Token（估算）"),
        )
        self.metric_labels: dict[str, QLabel] = {}
        for index, (key, label) in enumerate(metric_names):
            row, column = divmod(index, 4)
            box = QWidget(metrics)
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(8, 4, 8, 4)
            name_label = QLabel(label, box)
            name_label.setStyleSheet("color:#526176;")
            value_label = QLabel("0", box)
            value_label.setObjectName(f"metric_{key}")
            value_label.setFont(QFont("Microsoft YaHei UI", 13, QFont.Weight.Bold))
            box_layout.addWidget(name_label)
            box_layout.addWidget(value_label)
            grid.addWidget(box, row, column)
            self.metric_labels[key] = value_label
        layout.addWidget(metrics)
        layout.addWidget(QLabel("按 operation 聚合"))
        self.operation_table = self._table(["Operation", "调用数", "错误数"], parent=tab)
        self.operation_table.setObjectName("operationMetrics")
        self.operation_table.setMaximumHeight(180)
        layout.addWidget(self.operation_table)
        trace_group = QGroupBox("Trace 明细查询", tab)
        trace_layout = QVBoxLayout(trace_group)
        trace_actions = QHBoxLayout()
        self.trace_id_edit = QLineEdit(trace_group)
        self.trace_id_edit.setObjectName("traceId")
        self.trace_id_edit.setPlaceholderText("输入 RAG 响应中的 trace_id")
        use_last = QPushButton("使用本窗口最近查询", trace_group)
        use_last.clicked.connect(self._use_last_trace)
        load_trace = QPushButton("加载 Trace 与 Span", trace_group)
        load_trace.clicked.connect(self._load_trace)
        trace_actions.addWidget(self.trace_id_edit, 1)
        trace_actions.addWidget(use_last)
        trace_actions.addWidget(load_trace)
        trace_layout.addLayout(trace_actions)
        self.trace_detail = QPlainTextEdit(trace_group)
        self.trace_detail.setObjectName("traceDetail")
        self.trace_detail.setReadOnly(True)
        trace_layout.addWidget(self.trace_detail, 1)
        layout.addWidget(trace_group, 1)
        return tab

    @staticmethod
    def _table(headers: list[str], parent: QWidget | None = None) -> QTableWidget:
        table = QTableWidget(0, len(headers), parent)
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _principal(self) -> Principal:
        return self._active_principal

    @staticmethod
    def _principal_signature(principal: Principal) -> tuple[object, ...]:
        collections = (
            tuple(sorted(principal.collection_ids))
            if principal.collection_ids is not None
            else None
        )
        return (
            principal.tenant_id,
            principal.user_id,
            tuple(sorted(principal.roles)),
            collections,
        )

    def _draft_belongs_to_active_identity(self, draft: dict[str, Any]) -> bool:
        return draft.get("principal_signature") == self._principal_signature(self._principal())

    @staticmethod
    def _outcome_is_uncertain(error: Exception) -> bool:
        return isinstance(error, ConnectionError) or (
            isinstance(error, KnowledgeAssistantApiError)
            and not (400 <= error.status_code < 500 and error.status_code != 408)
        )

    def _identity_draft_changed(self, *_args) -> None:
        if not hasattr(self, "_active_principal"):
            return
        self._identity_dirty = True
        self._clear_identity_scoped_views()
        self.tabs.setEnabled(False)
        self._set_status("身份字段已修改。请先点击“应用身份并刷新”，所有业务操作已暂时冻结。")

    def _apply_identity(self) -> None:
        try:
            principal = self.controller.principal(
                self.tenant_edit.text(),
                self.user_edit.text(),
                str(self.role_combo.currentData() or ""),
                self.collections_edit.text(),
            )
        except (ValueError, TypeError) as exc:
            self._show_error("身份无效", exc)
            return
        self._active_principal = principal
        self._identity_dirty = False
        self.tabs.setEnabled(True)
        self._clear_identity_scoped_views()
        self._set_status(
            f"已应用身份：tenant={principal.tenant_id}，user={principal.user_id}，"
            f"roles={','.join(sorted(principal.roles))}。"
        )
        self._update_action_permissions()
        self.refresh_all()

    def refresh_all(self) -> None:
        self._refresh_documents(show_error=False)
        self._refresh_tasks(show_error=False)
        self._refresh_metrics(show_error=False)
        self._update_action_permissions()

    def _refresh_documents(self, _checked: bool = False, *, show_error: bool = True) -> None:
        selected_id = self._selected_document_id()
        try:
            with self._busy():
                documents = self.controller.list_documents(self._principal())
        except Exception as exc:
            self._documents = {}
            self.document_table.setRowCount(0)
            if show_error:
                self._show_error("无法读取文档", exc)
            else:
                self._set_status(f"文档读取失败：{exc}", error=True)
            return
        self._documents = {str(item["id"]): item for item in documents}
        self.document_table.setRowCount(len(documents))
        for row, document in enumerate(documents):
            values = (
                document.get("filename", ""),
                document.get("collection_id", ""),
                document.get("status", ""),
                document.get("chunk_count", ""),
                document.get("byte_size", ""),
                document.get("created_by", ""),
                document.get("created_at", ""),
                document.get("id", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(document["id"]))
                self.document_table.setItem(row, column, item)
        self._restore_selection(self.document_table, selected_id)
        self._document_selected()

    def _upload_document(self) -> None:
        principal = self._principal()
        draft = self._upload_draft
        if draft is not None and not self._draft_belongs_to_active_identity(draft):
            self._show_error(
                "存在其他身份的未确认上传",
                PermissionError("请切回发起该请求的身份后重试或明确放弃"),
            )
            return
        dialog = UploadDocumentDialog(self.controller, self, draft=draft)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            if draft is not None:
                self._handle_uncertain_cancel("上传", "_upload_draft", draft)
            return
        if draft is None:
            path = Path(dialog.path_edit.text().strip())
            try:
                filename, content = self.controller.prepare_upload(path)
            except Exception as exc:
                self._show_error("文件读取失败；请求尚未发送", exc)
                return
            draft = {
                "path": str(path),
                "filename": filename,
                "content": content,
                "collection_id": dialog.collection_edit.text().strip(),
                "idempotency_key": dialog.idempotency_edit.text().strip(),
                "principal_signature": self._principal_signature(principal),
            }
            self._upload_draft = draft
        try:
            with self._busy():
                result = self.controller.upload_content(
                    principal,
                    filename=str(draft["filename"]),
                    content=bytes(draft["content"]),
                    collection_id=str(draft["collection_id"]),
                    idempotency_key=str(draft["idempotency_key"]),
                )
        except Exception as exc:
            if self._outcome_is_uncertain(exc):
                self._show_error("上传结果未确认；再次打开将精确重放原请求", exc)
            else:
                self._upload_draft = None
                self._show_error("上传失败；服务已返回确定结果", exc)
            return
        self._upload_draft = None
        replay = "；命中幂等重放" if result.get("idempotent_replay") else ""
        deduplicated = "；复用同集合已有内容" if result.get("deduplicated") else ""
        self._set_status(f"文档已索引：{result.get('filename')}，chunks={result.get('chunk_count')}{replay}{deduplicated}")
        self._refresh_documents(show_error=False)
        self._refresh_metrics(show_error=False)
        self._select_row(self.document_table, str(result.get("id") or ""))

    def _document_selected(self) -> None:
        document = self._selected_document()
        self.document_detail.setPlainText(_json_text(document) if document else "请选择一个文档。")
        self._update_action_permissions()

    def _selected_document_id(self) -> str:
        row = self.document_table.currentRow()
        if row < 0:
            return ""
        item = self.document_table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def _selected_document(self) -> dict[str, Any] | None:
        return self._documents.get(self._selected_document_id())

    def _run_query(self) -> None:
        query = self.query_edit.toPlainText().strip()
        if not query:
            self._show_error("查询为空", ValueError("请输入至少 2 个字符的问题"))
            return
        try:
            with self._busy():
                result = self.controller.query(
                    self._principal(),
                    query=query,
                    collection_ids=self.query_collections_edit.text(),
                    top_k=self.top_k_spin.value(),
                )
        except Exception as exc:
            self._show_error("RAG 查询失败", exc)
            return
        self.answer_edit.setPlainText(str(result.get("answer") or ""))
        citations = result.get("citations") if isinstance(result.get("citations"), list) else []
        retrieval = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
        if result.get("refused"):
            self.answer_state.setText(
                "已拒答：当前知识库没有足够可靠的证据；引用为空，未使用参数知识补答。"
            )
        else:
            self.answer_state.setText(
                f"有据回答：采用 {retrieval.get('accepted_count', len(citations))} 条证据；"
                f"最高分 {retrieval.get('top_score', 0)}。"
            )
        self.citation_table.setRowCount(len(citations))
        for row, citation in enumerate(citations):
            page = citation.get("page_number")
            location = str(citation.get("heading") or "")
            if page is not None:
                location = f"{location} / 第 {page} 页" if location else f"第 {page} 页"
            values = (
                citation.get("id", ""),
                citation.get("filename", ""),
                citation.get("collection_id", ""),
                location,
                citation.get("score", ""),
                citation.get("excerpt", ""),
            )
            for column, value in enumerate(values):
                self.citation_table.setItem(row, column, QTableWidgetItem(str(value)))
        self._last_trace_id = str(result.get("trace_id") or "")
        if self._last_trace_id:
            self.trace_id_edit.setText(self._last_trace_id)
        self._set_status(
            "RAG 查询完成：拒答且无引用。" if result.get("refused") else f"RAG 查询完成：{len(citations)} 条引用。"
        )
        self._refresh_metrics(show_error=False)

    def _refresh_tasks(self, _checked: bool = False, *, show_error: bool = True) -> None:
        self._clear_approval_preview()
        selected_id = self._selected_task_id()
        try:
            with self._busy():
                tasks = self.controller.list_tasks(self._principal())
        except Exception as exc:
            self._tasks = {}
            self.task_table.setRowCount(0)
            if show_error:
                self._show_error("无法读取任务", exc)
            else:
                self._set_status(f"任务读取失败：{exc}", error=True)
            return
        self._tasks = {str(item["id"]): item for item in tasks}
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            approval = task.get("approval") if isinstance(task.get("approval"), dict) else {}
            values = (
                task.get("status", ""),
                task.get("tool_name", ""),
                task.get("requested_by", ""),
                approval.get("status", "—"),
                task.get("created_at", ""),
                task.get("id", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(task["id"]))
                self.task_table.setItem(row, column, item)
        self._restore_selection(self.task_table, selected_id)
        self._task_selected()

    def _create_ingest_task(self) -> None:
        principal = self._principal()
        draft = self._ingest_task_draft
        if draft is not None and not self._draft_belongs_to_active_identity(draft):
            self._show_error(
                "存在其他身份的未确认任务",
                PermissionError("请切回发起该请求的身份后重试或明确放弃"),
            )
            return
        dialog = IngestTaskDialog(self.controller, self, draft=draft)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            if draft is not None:
                self._handle_uncertain_cancel("Agent 写入任务", "_ingest_task_draft", draft)
            return
        if draft is None:
            draft = {
                "filename": dialog.filename_edit.text().strip(),
                "collection_id": dialog.collection_edit.text().strip(),
                "content": dialog.content_edit.toPlainText(),
                "idempotency_key": dialog.idempotency_edit.text().strip(),
                "principal_signature": self._principal_signature(principal),
            }
            self._ingest_task_draft = draft
        try:
            with self._busy():
                task = self.controller.create_ingest_task(
                    principal,
                    filename=str(draft["filename"]),
                    collection_id=str(draft["collection_id"]),
                    content=str(draft["content"]),
                    idempotency_key=str(draft["idempotency_key"]),
                )
        except Exception as exc:
            if self._outcome_is_uncertain(exc):
                self._show_error("任务创建结果未确认；再次打开将精确重放原请求", exc)
            else:
                self._ingest_task_draft = None
                self._show_error("任务创建失败；服务已返回确定结果", exc)
            return
        self._ingest_task_draft = None
        replay = "；命中幂等重放" if task.get("idempotent_replay") else ""
        self._set_status(f"审批任务已创建：{task.get('id')}，当前状态 {task.get('status')}{replay}")
        self._refresh_tasks(show_error=False)
        self._select_row(self.task_table, str(task.get("id") or ""))
        self.tabs.setCurrentIndex(2)

    def _create_archive_task(self) -> None:
        principal = self._principal()
        draft = self._archive_task_draft
        if draft is not None and not self._draft_belongs_to_active_identity(draft):
            self._show_error(
                "存在其他身份的未确认归档任务",
                PermissionError("请切回发起该请求的身份后重试或明确放弃"),
            )
            return
        if draft is None:
            document = self._selected_document()
            if document is None:
                self._show_error("未选择文档", ValueError("请先选择需要申请归档的文档"))
                return
            document_id = str(document["id"])
            draft = {
                "document_id": document_id,
                "idempotency_key": self.controller.new_idempotency_key("desktop-archive"),
                "document": dict(document),
                "principal_signature": self._principal_signature(principal),
            }
        else:
            frozen = draft.get("document")
            if not isinstance(frozen, dict):
                self._show_error("未确认归档任务损坏", ValueError("冻结的目标元数据不可用"))
                return
            document = frozen
            document_id = str(draft["document_id"])
        idempotency_key = str(draft["idempotency_key"])
        if not self._confirm_archive_request(document, idempotency_key):
            if self._archive_task_draft is not None:
                self._handle_uncertain_cancel("归档任务", "_archive_task_draft", draft)
            return
        self._archive_task_draft = draft
        try:
            with self._busy():
                task = self.controller.create_archive_task(
                    principal,
                    document_id=document_id,
                    idempotency_key=idempotency_key,
                )
        except Exception as exc:
            if self._outcome_is_uncertain(exc):
                self._show_error("归档任务结果未确认；重试将精确复用原请求", exc)
            else:
                self._archive_task_draft = None
                self._show_error("归档任务创建失败；服务已返回确定结果", exc)
            return
        self._archive_task_draft = None
        task_status = str(task.get("status") or "unknown")
        if task_status == "succeeded":
            self._set_status(f"幂等重放确认 {document.get('filename')} 的归档任务已经执行。")
        elif task_status == "awaiting_approval":
            self._set_status(f"已为 {document.get('filename')} 创建归档审批任务；文档尚未归档。")
        else:
            self._set_status(
                f"归档任务返回状态 {task_status}；不能据此断言有无局部副作用，"
                "已刷新文档与任务，请核查 Trace 和实际文档状态。",
                error=True,
            )
        self._refresh_documents(show_error=False)
        self._refresh_tasks(show_error=False)
        self._select_row(self.task_table, str(task.get("id") or ""))
        self.tabs.setCurrentIndex(2)

    def _confirm_archive_request(self, document: dict[str, Any], idempotency_key: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle("创建归档审批任务")
        box.setText(f"为文档「{document.get('filename')}」创建归档审批任务？")
        box.setInformativeText("此时不会归档。另一位审批人仍需查看精确目标、后果和恢复限制后决定。")
        box.setDetailedText(
            f"document_id={document.get('id')}\ncollection={document.get('collection_id')}\n"
            f"checksum={document.get('checksum')}\nidempotency_key={idempotency_key}"
        )
        create = box.addButton("创建审批任务", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is create

    def _task_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            self.task_detail.setPlainText("请选择一个任务。")
            self.receipt_detail.setPlainText("没有收据。")
            self._clear_approval_preview()
            self._update_action_permissions()
            return
        safe_task = {
            key: value
            for key, value in task.items()
            if key not in {"receipt", "result"}
        }
        safe_task["result"] = task.get("result")
        self.task_detail.setPlainText(_json_text(safe_task))
        receipt = task.get("receipt")
        self.receipt_detail.setPlainText(_json_text(receipt) if receipt else "尚未产生操作收据。")
        approval = task.get("approval") if isinstance(task.get("approval"), dict) else {}
        preview_still_pending = (
            str(task.get("id")) == self._approval_task_id
            and task.get("status") == "awaiting_approval"
            and approval.get("status") == "pending"
        )
        if not preview_still_pending:
            self._clear_approval_preview()
        self._update_action_permissions()

    def _selected_task_id(self) -> str:
        row = self.task_table.currentRow()
        if row < 0:
            return ""
        item = self.task_table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def _selected_task(self) -> dict[str, Any] | None:
        return self._tasks.get(self._selected_task_id())

    def _load_approval_preview(self) -> None:
        task = self._selected_task()
        if task is None:
            self._show_error("未选择任务", ValueError("请先选择 awaiting_approval 任务"))
            return
        try:
            with self._busy():
                principal = self._principal()
                preview = self.controller.approval_preview(principal, str(task["id"]))
            decision = preview.get("decision") if isinstance(preview.get("decision"), dict) else {}
            action = preview.get("action") if isinstance(preview.get("action"), dict) else {}
            target = action.get("target") if isinstance(action.get("target"), dict) else {}
            if str(preview.get("task_id") or "") != str(task["id"]):
                raise ValueError("approval preview returned a different task")
            if str(decision.get("bound_approver") or "") != principal.user_id:
                raise ValueError("approval preview is not bound to the applied identity")
            if str(target.get("tenant_id") or "") != principal.tenant_id:
                raise ValueError("approval preview returned a different tenant")
        except Exception as exc:
            self._clear_approval_preview()
            self._show_error("审批预览加载失败", exc)
            return
        self._approval_preview = preview
        self._approval_task_id = str(task["id"])
        self.preview_editor.setPlainText(self.format_approval_preview(preview))
        self.preview_content_editor.setPlainText(self.approval_preview_content(preview))
        self._update_action_permissions()
        self.defer_button.setFocus()
        self._set_status(
            f"已加载绑定审批人 {preview.get('decision', {}).get('bound_approver')} 的精确预览；"
            "请核对后批准或拒绝。"
        )

    @staticmethod
    def format_approval_preview(preview: dict[str, Any]) -> str:
        action = preview.get("action") if isinstance(preview.get("action"), dict) else {}
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        parameters = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
        provenance = preview.get("provenance") if isinstance(preview.get("provenance"), dict) else {}
        decision = preview.get("decision") if isinstance(preview.get("decision"), dict) else {}
        lines = [
            "ACTION PREVIEW（精确执行契约）",
            f"预览版本：{KnowledgeAssistantDialog._field(preview.get('preview_version'))}",
            f"任务 ID：{KnowledgeAssistantDialog._field(preview.get('task_id'))}",
            f"审批 ID：{KnowledgeAssistantDialog._field(preview.get('approval_id'))}",
            f"到期时间：{KnowledgeAssistantDialog._field(preview.get('expires_at'))}",
            f"绑定审批人：{KnowledgeAssistantDialog._field(decision.get('bound_approver'))}",
            f"请求人：{KnowledgeAssistantDialog._field(provenance.get('requested_by'))}",
            f"来源：{KnowledgeAssistantDialog._field(provenance.get('origin'))}",
            f"输入信任：{KnowledgeAssistantDialog._field(provenance.get('input_trust'))}",
            "",
            f"工具：{KnowledgeAssistantDialog._field(action.get('tool_name'))}",
            f"意图：{KnowledgeAssistantDialog._field(action.get('intent'))}",
            "目标（逐字段）：",
        ]
        lines.extend(
            f"- {key} = {KnowledgeAssistantDialog._field(value)}"
            for key, value in target.items()
        )
        lines.extend(("", "参数（逐字段）："))
        for key, value in parameters.items():
            if key != "content":
                lines.append(f"- {key} = {KnowledgeAssistantDialog._field(value)}")
        lines.extend(("", "后果："))
        consequences = action.get("consequences")
        if isinstance(consequences, list):
            lines.extend(f"- {KnowledgeAssistantDialog._field(item)}" for item in consequences)
        lines.extend(
            (
                "",
                f"可恢复性：{KnowledgeAssistantDialog._field(action.get('reversibility'))}",
                f"预览哈希：{KnowledgeAssistantDialog._field(preview.get('preview_hash'))}",
            )
        )
        return "\n".join(lines)

    @staticmethod
    def approval_preview_content(preview: dict[str, Any]) -> str:
        action = preview.get("action") if isinstance(preview.get("action"), dict) else {}
        parameters = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
        if "content" not in parameters:
            return "当前动作不包含正文。请仅核对上方精确目标和后果。"
        return str(parameters["content"])

    @staticmethod
    def _field(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def _approve_selected(self) -> None:
        task = self._selected_task()
        preview = self._approval_preview
        if task is None or preview is None or str(task.get("id")) != self._approval_task_id:
            self._show_error("缺少有效预览", ValueError("请先为当前任务加载精确审批预览"))
            return
        if not self._confirm_approval(preview):
            self.defer_button.setFocus()
            return
        task_id = str(task["id"])
        try:
            with self._busy():
                result = self.controller.approve_task(
                    self._principal(),
                    task_id,
                    str(preview.get("preview_hash") or ""),
                )
        except Exception as exc:
            self._clear_approval_preview()
            self._update_action_permissions()
            self._show_error("批准执行失败", exc)
            return
        receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
        self._set_status(
            f"任务已执行：status={result.get('status')}；operation_id={receipt.get('operation_id', '未产生')}。"
        )
        self._clear_approval_preview()
        self._refresh_documents(show_error=False)
        self._refresh_tasks(show_error=False)
        self._refresh_metrics(show_error=False)
        self._select_row(self.task_table, task_id)

    def _confirm_approval(self, preview: dict[str, Any]) -> bool:
        action = preview.get("action") if isinstance(preview.get("action"), dict) else {}
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        label = target.get("filename") or target.get("document_id") or "所列目标"
        consequences = action.get("consequences") if isinstance(action.get("consequences"), list) else []
        parameters = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle("确认批准精确动作")
        box.setText(
            f"批准并执行 {action.get('tool_name')}？\n"
            f"租户：{target.get('tenant_id')}\n集合：{target.get('collection_id')}\n目标：{label}"
        )
        box.setInformativeText(
            "\n".join(str(item) for item in consequences)
            + f"\n可恢复性：{action.get('reversibility')}\n"
            f"内容 SHA-256：{parameters.get('content_sha256') or target.get('checksum') or '—'}\n"
            f"字节数：{parameters.get('byte_size', '—')}"
        )
        approve = box.addButton("批准并执行", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("返回继续检查", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is approve

    def _reject_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            self._show_error("未选择任务", ValueError("请先选择 awaiting_approval 任务"))
            return
        if not self._confirm_rejection(task):
            return
        task_id = str(task["id"])
        try:
            with self._busy():
                result = self.controller.reject_task(
                    self._principal(),
                    task_id,
                    self.reject_reason_edit.text(),
                )
        except Exception as exc:
            self._clear_approval_preview()
            self._update_action_permissions()
            self._refresh_tasks(show_error=False)
            self._show_error("拒绝任务失败", exc)
            return
        self._set_status(f"任务已拒绝：{result.get('id')}；未执行知识库写操作。")
        self._clear_approval_preview()
        self._refresh_tasks(show_error=False)
        self._select_row(self.task_table, task_id)

    def _confirm_rejection(self, task: dict[str, Any]) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle("确认拒绝任务")
        box.setText(f"拒绝 {task.get('tool_name')} 任务？")
        box.setInformativeText(
            f"任务 ID：{task.get('id')}\n请求人：{task.get('requested_by')}\n"
            "拒绝会结束该任务、删除暂存正文，并且不会执行知识库写操作。"
        )
        reject = box.addButton("拒绝任务", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("暂不处理", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is reject

    def _handle_uncertain_cancel(
        self,
        operation: str,
        attribute: str,
        draft: dict[str, Any],
    ) -> None:
        if self._confirm_abandon_uncertain(
            operation,
            str(draft.get("idempotency_key") or ""),
        ):
            setattr(self, attribute, None)
            self._set_status(
                f"已明确放弃未确认的{operation}请求；再次操作会创建新幂等键。",
                error=True,
            )
        else:
            self._set_status(f"未确认的{operation}请求已保留；下次将精确重放原请求。")
        self._update_action_permissions()

    def _confirm_abandon_uncertain(self, operation: str, idempotency_key: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle("保留还是放弃未确认请求")
        box.setText(f"{operation}的服务端结果尚未确认。")
        box.setInformativeText(
            "保留可继续使用同一内容和同一幂等键重试。放弃后无法确认先前请求是否已提交，"
            "以后使用新键重试可能产生重复结果。"
        )
        box.setDetailedText(f"idempotency_key={idempotency_key}")
        abandon = box.addButton("明确放弃", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("保留待重试", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is abandon

    def _clear_approval_preview(self) -> None:
        self._approval_preview = None
        self._approval_task_id = ""
        if hasattr(self, "preview_editor"):
            self.preview_editor.setPlainText("先选择 awaiting_approval 任务，再加载专用审批预览。")
            self.preview_content_editor.setPlainText("当前动作没有已加载的正文。")
        if hasattr(self, "approve_button"):
            self.approve_button.setEnabled(False)

    def _clear_identity_scoped_views(self) -> None:
        self._clear_approval_preview()
        self._documents = {}
        self.document_table.setRowCount(0)
        self.document_detail.clear()
        self._tasks = {}
        self.task_table.setRowCount(0)
        self.task_detail.clear()
        self.receipt_detail.clear()
        self.query_edit.clear()
        self.query_collections_edit.clear()
        self.answer_state.setText("尚未查询")
        self.answer_edit.clear()
        self.citation_table.setRowCount(0)
        self._last_trace_id = ""
        self.trace_id_edit.clear()
        self.trace_detail.clear()
        self.reject_reason_edit.clear()
        self._clear_metrics("身份已改变，等待刷新")
        self._set_status("受保护视图已清空。")

    def _tab_changed(self, index: int) -> None:
        if index != 2 and self._approval_preview is not None:
            self._defer_preview()

    def _defer_preview(self) -> None:
        if self._approval_preview is None:
            return
        task_id = self._approval_task_id
        self._clear_approval_preview()
        self._update_action_permissions()
        self._set_status(f"已退出任务 {task_id} 的审批预览；任务仍保持待审批，未执行也未拒绝。")

    def _refresh_metrics(self, _checked: bool = False, *, show_error: bool = True) -> None:
        principal = self._principal()
        if principal.collection_ids is not None:
            self._clear_metrics("集合受限身份不可查看租户级指标")
            if show_error:
                self._set_status(
                    "观测面板已禁用：当前服务的 metrics 是租户级聚合，不能安全映射到集合范围。",
                    error=True,
                )
            return
        try:
            with self._busy():
                metrics = self.controller.metrics(principal)
        except Exception as exc:
            if show_error:
                self._show_error("指标读取失败", exc)
            else:
                self._set_status(f"指标读取失败：{exc}", error=True)
            return
        latency = metrics.get("latency_ms") if isinstance(metrics.get("latency_ms"), dict) else {}
        tokens = metrics.get("tokens") if isinstance(metrics.get("tokens"), dict) else {}
        values = {
            "trace_count": metrics.get("trace_count", 0),
            "error_count": metrics.get("error_count", 0),
            "average": f"{latency.get('average', 0)} ms",
            "p95": f"{latency.get('p95', 0)} ms",
            "max": f"{latency.get('max', 0)} ms",
            "input": tokens.get("input", 0),
            "output": tokens.get("output", 0),
            "total": tokens.get("total", 0),
        }
        for key, value in values.items():
            self.metric_labels[key].setText(str(value))
        operations = metrics.get("operations") if isinstance(metrics.get("operations"), dict) else {}
        self.operation_table.setRowCount(len(operations))
        for row, (operation, bucket) in enumerate(sorted(operations.items())):
            counts = bucket if isinstance(bucket, dict) else {}
            for column, value in enumerate((operation, counts.get("count", 0), counts.get("errors", 0))):
                self.operation_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _clear_metrics(self, placeholder: str) -> None:
        for label in self.metric_labels.values():
            label.setText("—")
            label.setToolTip(placeholder)
        self.operation_table.setRowCount(0)

    def _use_last_trace(self) -> None:
        if not self._last_trace_id:
            self._show_error("没有最近 Trace", ValueError("请先在本窗口执行一次 RAG 查询"))
            return
        self.trace_id_edit.setText(self._last_trace_id)
        self._load_trace()

    def _load_trace(self) -> None:
        trace_id = self.trace_id_edit.text().strip()
        if not trace_id:
            self._show_error("缺少 Trace ID", ValueError("请输入 trace_id"))
            return
        principal = self._principal()
        if principal.collection_ids is not None:
            self._show_error(
                "Trace 面板已禁用",
                PermissionError("当前 Trace 仅支持租户级授权，集合受限身份不能安全查看"),
            )
            return
        try:
            with self._busy():
                trace = self.controller.get_trace(principal, trace_id)
        except Exception as exc:
            self._show_error("Trace 读取失败", exc)
            return
        self.trace_detail.setPlainText(_json_text(trace))
        self._set_status(f"已加载 Trace：{trace_id}，Span 数={len(trace.get('spans') or [])}。")

    def _update_action_permissions(self) -> None:
        try:
            principal = self._principal()
        except (ValueError, TypeError):
            return
        if self._identity_dirty:
            return
        permissions = principal.permissions
        can_write = "knowledge:write" in permissions
        can_create_task = "task:create" in permissions and can_write
        can_approve = "task:approve" in permissions
        archive_retry = self._archive_task_draft
        has_archive_retry = bool(
            archive_retry is not None
            and archive_retry.get("principal_signature")
            == self._principal_signature(principal)
        )
        self.upload_button.setEnabled(can_write)
        self.archive_task_button.setText(
            "重试/处理未确认归档任务"
            if has_archive_retry
            else "为所选文档创建归档审批任务"
        )
        self.archive_task_button.setEnabled(
            can_create_task and (self._selected_document() is not None or has_archive_retry)
        )
        self.create_task_button.setEnabled(can_create_task)
        task = self._selected_task()
        awaiting = bool(task and task.get("status") == "awaiting_approval")
        independent = bool(task and task.get("requested_by") != principal.user_id)
        self.preview_button.setEnabled(can_approve and awaiting and independent)
        self.reject_button.setEnabled(can_approve and awaiting and independent)
        self.defer_button.setEnabled(self._approval_preview is not None)
        self.approve_button.setEnabled(
            can_approve
            and awaiting
            and independent
            and self._approval_preview is not None
            and self._approval_task_id == self._selected_task_id()
        )

    @staticmethod
    def _restore_selection(table: QTableWidget, item_id: str) -> None:
        if not item_id:
            if table.rowCount() > 0:
                table.selectRow(0)
            return
        KnowledgeAssistantDialog._select_row(table, item_id)

    @staticmethod
    def _select_row(table: QTableWidget, item_id: str) -> None:
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and str(item.data(Qt.ItemDataRole.UserRole) or "") == item_id:
                table.selectRow(row)
                table.setCurrentCell(row, 0)
                return

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color:#9d1c1c;" if error else "color:#29445f;")

    def _show_error(self, title: str, error: Exception) -> None:
        message = str(error).strip() or type(error).__name__
        self._set_status(f"{title}：{message}", error=True)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    @contextmanager
    def _busy(self) -> Iterator[None]:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            yield
        finally:
            QApplication.restoreOverrideCursor()

    def keyPressEvent(self, event) -> None:
        if (
            event.key() == Qt.Key.Key_Escape
            and self.tabs.currentIndex() == 2
            and self._approval_preview is not None
        ):
            self._defer_preview()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._clear_identity_scoped_views()
        self._needs_refresh_on_show = True
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._needs_refresh_on_show:
            self._needs_refresh_on_show = False
            if self._identity_dirty:
                self._set_status("身份字段尚未应用；受保护视图保持清空，请先应用身份。")
            else:
                self.refresh_all()
