from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .agent_models import CoachResponse, ReadResource
from .agent_runtime import AgentRuntime


RESOURCE_LABELS = {
    ReadResource.KNOWLEDGE: "知识库",
    ReadResource.REVIEW: "复习与错题",
    ReadResource.GOALS: "学习目标",
    ReadResource.TIMETABLE: "课程表",
    ReadResource.INTERVIEWS: "投递/面试记录",
}


class AgentCenterDialog(QDialog):
    def __init__(self, runtime: AgentRuntime, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.current_session_id = ""
        self.setWindowTitle("Table Miku · Agent 中心")
        self.resize(980, 680)
        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        root.addWidget(splitter, 1)

        session_panel = QWidget(splitter)
        session_layout = QVBoxLayout(session_panel)
        session_layout.addWidget(QLabel("会话"))
        self.session_list = QListWidget(session_panel)
        self.session_list.currentItemChanged.connect(self._session_changed)
        session_layout.addWidget(self.session_list, 1)
        session_buttons = QHBoxLayout()
        new_button = QPushButton("新会话")
        delete_button = QPushButton("删除")
        new_button.clicked.connect(self._new_session)
        delete_button.clicked.connect(self._delete_session)
        session_buttons.addWidget(new_button)
        session_buttons.addWidget(delete_button)
        session_layout.addLayout(session_buttons)

        chat_panel = QWidget(splitter)
        chat_layout = QVBoxLayout(chat_panel)
        self.status = QLabel("就绪")
        chat_layout.addWidget(self.status)
        self.chat = QTextEdit(chat_panel)
        self.chat.setReadOnly(True)
        chat_layout.addWidget(self.chat, 1)
        self.input = QTextEdit(chat_panel)
        self.input.setPlaceholderText("询问 Java 后端知识、开始一道练习或制定复习计划…")
        self.input.setMaximumHeight(110)
        chat_layout.addWidget(self.input)
        action_row = QHBoxLayout()
        self.send_button = QPushButton("发送")
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.send_button.clicked.connect(self._send)
        self.stop_button.clicked.connect(self._stop)
        action_row.addStretch(1)
        action_row.addWidget(self.stop_button)
        action_row.addWidget(self.send_button)
        chat_layout.addLayout(action_row)
        self.approval_panel = QWidget(chat_panel)
        approval_layout = QVBoxLayout(self.approval_panel)
        self.approval_title = QLabel("待审批操作")
        self.approval_preview = QTextEdit(self.approval_panel)
        self.approval_preview.setReadOnly(True)
        self.approval_preview.setMaximumHeight(150)
        approval_layout.addWidget(self.approval_title)
        approval_layout.addWidget(self.approval_preview)
        approval_buttons = QHBoxLayout()
        self.approve_button = QPushButton("执行写入")
        self.reject_button = QPushButton("拒绝")
        self.reject_button.setDefault(True)
        self.reject_button.setAutoDefault(True)
        self.approve_button.clicked.connect(self._approve)
        self.reject_button.clicked.connect(self._reject)
        approval_buttons.addWidget(self.approve_button)
        approval_buttons.addStretch(1)
        approval_buttons.addWidget(self.reject_button)
        approval_layout.addLayout(approval_buttons)
        self.approval_panel.hide()
        self._pending_operation_id = ""
        chat_layout.addWidget(self.approval_panel)

        source_panel = QWidget(splitter)
        source_layout = QVBoxLayout(source_panel)
        source_layout.addWidget(QLabel("来源"))
        self.sources = QListWidget(source_panel)
        source_layout.addWidget(self.sources, 1)
        source_layout.addWidget(QLabel("只读资源授权"))
        self.resource_checks: dict[ReadResource, QCheckBox] = {}
        grants = self.runtime.store.resource_grants()
        for resource, label in RESOURCE_LABELS.items():
            checkbox = QCheckBox(label, source_panel)
            checkbox.setChecked(grants.get(resource.value, False))
            checkbox.toggled.connect(
                lambda checked, resource=resource: self.runtime.store.set_resource_grant(resource.value, checked)
            )
            self.resource_checks[resource] = checkbox
            source_layout.addWidget(checkbox)
        self.capability_button = QPushButton("测试 DeepSeek Agent 能力")
        self.capability_button.clicked.connect(self._test_capabilities)
        source_layout.addWidget(self.capability_button)
        self.capability_button = QPushButton("测试 DeepSeek Agent 能力")
        self.capability_button.clicked.connect(self._test_capabilities)
        source_layout.addWidget(self.capability_button)

        splitter.setSizes([190, 560, 230])
        self.runtime.progress.connect(self._progress)
        self.runtime.response_ready.connect(self._response)
        self.runtime.failed.connect(self._failed)
        self.runtime.sessions_changed.connect(self.reload_sessions)
        self.runtime.capability_ready.connect(self._capability_ready)
        self.runtime.capability_ready.connect(self._capability_ready)
        self.reload_sessions()

    def reload_sessions(self) -> None:
        selected = self.current_session_id
        self.session_list.clear()
        sessions = self.runtime.store.list_sessions()
        if not sessions:
            selected = self.runtime.new_session()
            sessions = self.runtime.store.list_sessions()
        target_row = 0
        for index, session in enumerate(sessions):
            item = QListWidgetItem(str(session.get("title") or "新会话"))
            item.setData(Qt.ItemDataRole.UserRole, session["id"])
            self.session_list.addItem(item)
            if session["id"] == selected:
                target_row = index
        self.session_list.setCurrentRow(target_row)

    def _session_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        self.current_session_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        self._render_messages()

    def _render_messages(self) -> None:
        blocks = []
        for message in self.runtime.store.list_messages(self.current_session_id):
            speaker = "你" if message["role"] == "user" else "Miku"
            blocks.append(f"### {speaker}\n\n{message['content']}")
        self.chat.setMarkdown("\n\n---\n\n".join(blocks))

    def _new_session(self) -> None:
        self.current_session_id = self.runtime.new_session()
        self.reload_sessions()

    def _delete_session(self) -> None:
        if self.current_session_id:
            self.runtime.delete_session(self.current_session_id)
            self.current_session_id = ""
            self.reload_sessions()

    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text or not self.current_session_id:
            return
        self.input.clear()
        self.send_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        if self.runtime.submit(self.current_session_id, text):
            self._render_messages()

    def _stop(self) -> None:
        if self.runtime.cancel():
            self.status.setText("正在停止…")

    def _progress(self, session_id: str, text: str) -> None:
        if session_id == self.current_session_id:
            self.status.setText(text)

    def _response(self, session_id: str, response: object) -> None:
        self.send_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if session_id != self.current_session_id:
            return
        coach = response if isinstance(response, CoachResponse) else CoachResponse(body=str(response))
        if coach.approval_request is not None:
            request = coach.approval_request
            self._pending_operation_id = request.operation_id
            self.approval_title.setText(f"{request.title} · 目标：{request.target}")
            self.approval_preview.setPlainText(
                json.dumps(
                    {
                        "tool": request.tool_name,
                        "operation_id": request.operation_id,
                        "fields": request.fields,
                        "reversible": request.reversible,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self.approve_button.setText(request.approve_label)
            self.approval_panel.show()
            self.reject_button.setFocus()
            self.status.setText("等待你的逐次审批")
        else:
            self._pending_operation_id = ""
            self.approval_panel.hide()
            self.status.setText("完成")
        self.sources.clear()
        for source in coach.sources:
            item = QListWidgetItem(source.title)
            item.setToolTip(source.location or source.excerpt)
            self.sources.addItem(item)
        self._render_messages()

    def _failed(self, session_id: str, message: str) -> None:
        self.send_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if session_id == self.current_session_id:
            self.status.setText(message)
            self._render_messages()

    def _test_capabilities(self) -> None:
        answer = QMessageBox.question(
            self,
            "测试 DeepSeek Agent 能力",
            "将发送 1 次不含用户数据的合成 API 请求，验证 function tool 与 JSON 参数。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.runtime.test_capabilities():
            self.capability_button.setEnabled(False)
            self.status.setText("正在进行合成能力测试…")

    def _capability_ready(self, result: object) -> None:
        self.capability_button.setEnabled(True)
        compatible = bool(isinstance(result, dict) and result.get("multi_agent_enabled"))
        self.status.setText("能力测试通过，已启用专家协作" if compatible else "能力测试未通过，多 Agent 保持禁用")

    def _test_capabilities(self) -> None:
        answer = QMessageBox.question(
            self,
            "测试 DeepSeek Agent 能力",
            "将发送 1 次不含用户数据的合成 API 请求，验证 function tool 与 JSON 参数。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.runtime.test_capabilities():
            self.capability_button.setEnabled(False)
            self.status.setText("正在进行合成能力测试…")

    def _capability_ready(self, result: object) -> None:
        self.capability_button.setEnabled(True)
        compatible = bool(isinstance(result, dict) and result.get("multi_agent_enabled"))
        self.status.setText("能力测试通过，已启用专家协作" if compatible else "能力测试未通过，多 Agent 保持禁用")

    def _approve(self) -> None:
        if self._pending_operation_id and self.runtime.approve(self._pending_operation_id):
            self.approval_panel.hide()
            self.send_button.setEnabled(False)
            self.stop_button.setEnabled(True)

    def _reject(self) -> None:
        if self._pending_operation_id and self.runtime.reject(self._pending_operation_id):
            self.approval_panel.hide()
            self._pending_operation_id = ""

    def reject_pending_action(self) -> None:
        self._reject()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._pending_operation_id:
                self._reject()
                return
            if self.runtime.cancel():
                self.status.setText("运行已取消")
                return
        super().keyPressEvent(event)
