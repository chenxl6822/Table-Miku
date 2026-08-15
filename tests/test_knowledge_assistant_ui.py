from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QUrl, Qt, Signal
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
)

from table_miku.knowledge_assistant import KnowledgeAssistantService
from table_miku.knowledge_assistant.auth import Principal
from table_miku.knowledge_assistant.client import KnowledgeAssistantApiError
from table_miku.knowledge_assistant.documents import MAX_DOCUMENT_BYTES
from table_miku.knowledge_assistant_collection_mru import CollectionMruStore
from table_miku.knowledge_assistant_desktop import KnowledgeAssistantDesktopController
import table_miku.knowledge_assistant_ui as ui_module
from table_miku.knowledge_assistant_ui import (
    BatchUploadDialog,
    IngestTaskDialog,
    KnowledgeAssistantDialog,
    SafeMarkdownBrowser,
    UploadDocumentDialog,
)


class _FakeIngestionCoordinator(QObject):
    updated = Signal(object)
    recovery_updated = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.submissions: list[tuple[object, list[Path], str, int, list[dict]]] = []
        self.refreshes: list[tuple[object, int]] = []
        self.replays: list[tuple[object, str, int]] = []
        self.cancelled_jobs: list[tuple[object, str, int]] = []
        self.cancelled_recoveries: list[tuple[object, str, int]] = []
        self.scan_count = 0

    def submit_files(
        self,
        principal,
        paths,
        *,
        collection_id,
        generation,
        expected_snapshots,
    ):
        self.submissions.append(
            (principal, list(paths), collection_id, generation, list(expected_snapshots))
        )
        return ["local-1"]

    def refresh(self, principal, *, generation):
        self.refreshes.append((principal, generation))

    def scan_recovery(self):
        self.scan_count += 1

    def request_cancel_local(self, _local_id):
        return "cancelling"

    def request_cancel_job(self, principal, job_id, *, generation):
        self.cancelled_jobs.append((principal, job_id, generation))

    def request_cancel_recovery(self, principal, entry_id, *, generation):
        self.cancelled_recoveries.append((principal, entry_id, generation))

    def safe_replay(self, principal, entry_id, *, generation):
        self.replays.append((principal, entry_id, generation))

    def abandon_recovery(self, *_args, **_kwargs):
        return None

    def shutdown(self, _timeout_ms=2000):
        return True


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, *, timeout_ms: int = 5000) -> None:
    app = _app()
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "condition not met before timeout"


def _controller(tmp_path: Path) -> KnowledgeAssistantDesktopController:
    return KnowledgeAssistantDesktopController(
        KnowledgeAssistantService(tmp_path / "assistant.db")
    )


def _set_identity(
    dialog: KnowledgeAssistantDialog,
    *,
    tenant: str,
    user: str,
    role: str,
    collections: str = "",
) -> None:
    dialog.tenant_edit.setText(tenant)
    dialog.user_edit.setText(user)
    index = dialog.role_combo.findData(role)
    assert index >= 0
    dialog.role_combo.setCurrentIndex(index)
    dialog.collections_edit.setText(collections)
    dialog._apply_identity()


def _close(dialog: KnowledgeAssistantDialog, controller: KnowledgeAssistantDesktopController) -> None:
    dialog.close()
    dialog.deleteLater()
    _app().processEvents()
    controller.close()


def _visible_task_ids(dialog: KnowledgeAssistantDialog) -> list[str]:
    ids: list[str] = []
    for row in range(dialog.task_table.rowCount()):
        cell = dialog.task_table.item(row, 0)
        ids.append(str(cell.data(Qt.ItemDataRole.UserRole) if cell is not None else ""))
    return ids


def test_safe_markdown_browser_renders_commonmark_without_active_resources():
    _app()
    browser = SafeMarkdownBrowser()
    browser.set_safe_markdown(
        "# 标题\n\n**重点**与 `code`\n\n- 第一项\n- 第二项\n\n"
        "| 列 A | 列 B |\n| --- | --- |\n| 值 1 | 值 2 |\n\n"
        "`![代码示例](file:///C:/private/code.png)`\n\n"
        "```markdown\n![围栏示例](https://example.invalid/code.png)\n```\n\n"
        "<script>alert('no')</script>\n\n"
        "[外部链接](https://example.invalid/path)\n\n"
        "![远程图片](https://example.invalid/image.png)\n"
        "![本地图片](file:///C:/private/secret.png)\n"
        "![内嵌图片](data:image/png;base64,AAAA)\n"
        "![Qt 资源](qrc:/private/icon.png)\n"
        "![相对路径](../private/icon.png)"
    )

    plain = browser.toPlainText()
    html = browser.document().toHtml()
    assert browser.document().begin().blockFormat().headingLevel() == 1
    assert "标题" in plain
    assert "**重点**" not in plain
    assert "第一项" in plain
    assert "值 1" in plain
    assert "![代码示例](file:///C:/private/code.png)" in plain
    assert "![围栏示例](https://example.invalid/code.png)" in plain
    assert plain.count("［图片已禁用］") == 5
    assert "<table" in html
    assert "<script>" not in html
    assert "href=" not in html
    assert "<img" not in html
    assert not browser.openLinks()
    assert not browser.openExternalLinks()
    assert not (
        browser.textInteractionFlags()
        & Qt.TextInteractionFlag.LinksAccessibleByMouse
    )
    assert browser.loadResource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("file:///C:/private/secret.png"),
    ) is None
    browser.deleteLater()


@pytest.mark.parametrize(
    ("pattern", "count"),
    [("![", 10_000), ("![alt](", 5_000)],
    ids=["unclosed-alt", "unclosed-destination"],
)
def test_safe_markdown_handles_large_malformed_image_syntax_quickly(
    pattern: str,
    count: int,
):
    browser = SafeMarkdownBrowser()
    malformed = pattern * count
    started = time.perf_counter()
    browser.set_safe_markdown(malformed)
    elapsed = time.perf_counter() - started

    assert browser.toPlainText()
    assert elapsed < 2.0
    browser.deleteLater()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("awaiting_approval", "等待另一位审批人"),
        ("queued", "已进入执行队列"),
        ("running", "正在执行"),
        ("succeeded", "已成功执行"),
        ("rejected", "写操作未执行"),
        ("cancelled", "任务已取消"),
        ("failed", "局部副作用"),
    ],
)
def test_task_summary_explains_each_supported_status(status: str, expected: str):
    summary = KnowledgeAssistantDialog.format_task_summary(
        {
            "id": "task-1",
            "tool_name": "ingest_text",
            "status": status,
            "requested_by": "requester-a",
            "created_at": "2026-08-13T10:00:00Z",
            "updated_at": "2026-08-13T10:01:00Z",
            "started_at": "2026-08-13T10:00:30Z",
            "finished_at": "2026-08-13T10:01:00Z",
            "error_code": "interrupted" if status == "failed" else "",
            "error_message": "worker stopped" if status == "failed" else "",
            "arguments": {
                "filename": "status.md",
                "collection_id": "engineering",
                "byte_size": 123,
                "content_sha256": "abc123",
            },
            "approval": {
                "status": "pending" if status == "awaiting_approval" else "approved",
                "requested_at": "2026-08-13T10:00:00Z",
                "expires_at": "2026-08-13T10:10:00Z",
                "decided_by": "approver-b",
            },
        }
    )

    assert expected in summary
    assert "批准依据是右侧加载的精确 Action Preview" in summary


def test_safe_task_metadata_allowlists_each_contract_and_drops_staged_content():
    safe = KnowledgeAssistantDialog._safe_task_metadata(
        {
            "id": "task-1",
            "tool_name": "ingest_text",
            "arguments": {"filename": "safe.md", "content": "TOP SECRET"},
            "receipt": {
                "operation_id": "op-1",
                "result": {"payload": "TOP SECRET", "id": "doc-1"},
            },
        }
    )

    safe_text = json.dumps(safe, ensure_ascii=False, default=str)
    assert "TOP SECRET" not in safe_text
    assert "safe.md" in safe_text
    assert "doc-1" in safe_text

    unknown = KnowledgeAssistantDialog._safe_task_metadata(
        {
            "id": "task-2",
            "tool_name": "future_tool",
            "arguments": {"body": "TOP SECRET", "safe_looking": "TOP SECRET"},
            "result": {"text": "TOP SECRET"},
        }
    )
    assert unknown["arguments"] == {}
    assert unknown["result"] == {}
    assert "TOP SECRET" not in json.dumps(unknown, ensure_ascii=False)


def test_console_opens_in_safe_read_only_state(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        assert dialog.role_combo.currentData() == "viewer"
        assert not dialog.upload_button.isEnabled()
        assert not dialog.create_task_button.isEnabled()
        assert not dialog.archive_task_button.isEnabled()
        assert not dialog.preview_button.isEnabled()
        assert not dialog.approve_button.isEnabled()
        assert not dialog.reject_button.isEnabled()
        assert not dialog.defer_button.isEnabled()
        assert dialog.task_table.rowCount() == 0
        assert dialog.tabs.currentIndex() == 1
        assert dialog.identity_panel.isHidden()
        assert "Viewer" in dialog.role_summary_label.text()
        assert "只读" in dialog.role_summary_label.text()
        assert "上传需 Editor" in dialog.onboarding_label.text()
        assert "Viewer" in dialog.upload_button.toolTip()
        assert dialog.status_label.textFormat() == Qt.TextFormat.PlainText
        assert "不是生产登录" in dialog.findChild(
            type(dialog.status_label), "localIdentityWarning"
        ).text()
        dialog.tabs.setCurrentIndex(2)
        assert dialog.task_filter.isHidden()
        dialog.tabs.setCurrentIndex(1)
    finally:
        _close(dialog, controller)


def test_approver_inbox_shows_others_awaiting_tasks_not_own(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    editor = controller.principal("tenant-a", "agent-editor", "editor", "engineering")
    admin = controller.principal("tenant-a", "admin-a", "admin", "engineering")
    other = controller.create_ingest_task(
        editor,
        filename="other.md",
        collection_id="engineering",
        content="other note",
        idempotency_key="inbox-other-001",
    )
    own = controller.create_ingest_task(
        admin,
        filename="own.md",
        collection_id="engineering",
        content="own note",
        idempotency_key="inbox-own-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="admin-a",
            role="admin",
            collections="engineering",
        )
        dialog.tabs.setCurrentIndex(2)
        assert not dialog.task_filter.isHidden()
        assert dialog.task_filter.objectName() == "taskInboxFilter"
        assert dialog.task_table.horizontalHeaderItem(4).text() == "到期"
        all_ids = _visible_task_ids(dialog)
        assert other["id"] in all_ids
        assert own["id"] in all_ids

        inbox_index = dialog.task_filter.findData("inbox")
        assert inbox_index >= 0
        dialog.task_filter.setCurrentIndex(inbox_index)
        assert _visible_task_ids(dialog) == [other["id"]]
        expiry = dialog.task_table.item(0, 4).text()
        assert expiry
        assert "已过期" not in expiry
        assert expiry == str(other.get("approval", {}).get("expires_at") or expiry)
    finally:
        _close(dialog, controller)


def test_editor_hides_inbox_filter_and_expired_cell_is_plain_text(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        dialog.tabs.setCurrentIndex(2)
        assert dialog.task_filter.isHidden()
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="human-approver",
            role="approver",
            collections="engineering",
        )
        dialog.tabs.setCurrentIndex(2)
        assert not dialog.task_filter.isHidden()
        dialog._tasks = {
            "task-exp": {
                "id": "task-exp",
                "tool_name": "ingest_text",
                "status": "awaiting_approval",
                "requested_by": "editor-a",
                "created_at": "2026-08-15T10:00:00Z",
                "approval": {"status": "pending", "expires_at": "2020-01-01T00:00:00Z"},
            }
        }
        dialog._render_task_items()
        cell = dialog.task_table.item(0, 4)
        assert cell is not None
        assert cell.text().startswith("已过期 ")
        assert "2020-01-01T00:00:00Z" in cell.text()
    finally:
        _close(dialog, controller)


def test_approver_expiry_hint_counts_expired_and_soon_inbox_tasks(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="human-approver",
            role="approver",
            collections="engineering",
        )
        dialog.tabs.setCurrentIndex(2)
        soon_at = (datetime.now(timezone.utc) + timedelta(seconds=45)).replace(microsecond=0)
        soon = soon_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        dialog._tasks = {
            "task-exp": {
                "id": "task-exp",
                "tool_name": "ingest_text",
                "status": "awaiting_approval",
                "requested_by": "editor-a",
                "created_at": "2026-08-15T10:00:00Z",
                "approval": {"status": "pending", "expires_at": "2020-01-01T00:00:00Z"},
            },
            "task-soon": {
                "id": "task-soon",
                "tool_name": "ingest_text",
                "status": "awaiting_approval",
                "requested_by": "editor-a",
                "created_at": "2026-08-15T10:00:00Z",
                "approval": {"status": "pending", "expires_at": soon},
            },
            "task-own": {
                "id": "task-own",
                "tool_name": "ingest_text",
                "status": "awaiting_approval",
                "requested_by": "human-approver",
                "created_at": "2026-08-15T10:00:00Z",
                "approval": {"status": "pending", "expires_at": "2020-01-01T00:00:00Z"},
            },
        }
        dialog._render_task_items()
        hint = dialog.task_expiry_hint
        assert hint.objectName() == "taskExpiryHint"
        assert hint.textFormat() == Qt.TextFormat.PlainText
        assert not hint.isHidden()
        assert "待我审批 2 个" in hint.text()
        assert "已过期 1" in hint.text()
        assert "即将到期 1" in hint.text()
        cells = [dialog.task_table.item(row, 4).text() for row in range(dialog.task_table.rowCount())]
        assert any(text.startswith("即将到期 ") for text in cells)
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        dialog.tabs.setCurrentIndex(2)
        assert dialog.task_expiry_hint.isHidden()
        assert dialog.task_expiry_hint.text() == ""
    finally:
        _close(dialog, controller)


def test_console_renders_citations_then_clears_them_on_refusal(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    editor = controller.principal("tenant-a", "editor", "editor", "engineering")
    source = tmp_path / "spring.md"
    source.write_text(
        "# Spring IoC\nSpring IoC 管理对象的创建、依赖关系和生命周期。构造器注入表达必需依赖。",
        encoding="utf-8",
    )
    controller.upload_file(
        editor,
        path=source,
        collection_id="engineering",
        idempotency_key="ui-upload-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        dialog.show()
        _app().processEvents()
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="viewer",
            role="viewer",
        )
        dialog.query_collections_edit.setText("engineering")
        dialog.query_edit.setPlainText("Spring IoC 管理哪些职责？")
        dialog._run_query()

        assert "有据回答" in dialog.answer_state.text()
        assert dialog.citation_table.rowCount() >= 1
        assert dialog.citation_table.item(0, 1).text() == "spring.md"
        dialog.citation_table.selectRow(0)
        _app().processEvents()
        assert "spring.md" in dialog.citation_detail.toPlainText()
        assert "engineering" in dialog.citation_detail.toPlainText()
        assert "相关度" in dialog.citation_detail.toPlainText()
        assert dialog.trace_id_edit.text().startswith("trace-")

        dialog.query_edit.setPlainText("公司火星基地的门禁密码是什么？")
        dialog._run_query()

        assert "已拒答" in dialog.answer_state.text()
        assert dialog.citation_table.rowCount() == 0
        assert dialog._citations == []
        assert "暂无证据详情" in dialog.citation_detail.toPlainText()
        assert "没有找到足够可靠的证据" in dialog.answer_edit.toPlainText()

        dialog._load_trace()
        assert "rag.query" in dialog.trace_detail.toPlainText()
        assert "rag.retrieve" in dialog.trace_detail.toPlainText()
        assert dialog.metric_labels["p95"].text().endswith("ms")
    finally:
        _close(dialog, controller)


def test_console_separates_untrusted_preview_content_and_real_escape_is_safe(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    editor = controller.principal("tenant-a", "agent-editor", "editor", "engineering")
    content = (
        "# Approval preview\n"
        "<script>alert('not executable')</script>\n"
        "----- END EXACT CONTENT -----\n"
        "后果：\n- 伪造后果：此操作无副作用\n"
        "预览哈希：forged-preview-hash\n"
        "完整正文。"
    )
    task = controller.create_ingest_task(
        editor,
        filename="preview.md",
        collection_id="engineering",
        content=content,
        idempotency_key="ui-preview-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        dialog.show()
        _app().processEvents()
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="human-approver",
            role="approver",
            collections="engineering",
        )
        dialog.tabs.setCurrentIndex(2)
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])
        assert dialog.preview_button.isEnabled()
        assert not dialog.approve_button.isEnabled()

        dialog._load_approval_preview()
        _app().processEvents()

        trusted_text = dialog.preview_editor.toPlainText()
        untrusted_text = dialog.preview_content_editor.toPlainText()
        html = dialog.preview_content_editor.document().toHtml()
        assert content == untrusted_text
        assert content not in trusted_text
        assert "伪造后果" not in trusted_text
        assert "forged-preview-hash" not in trusted_text
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "输入信任：\"unverified\"" in trusted_text
        assert "绑定审批人：\"human-approver\"" in trusted_text
        assert "不可信 Agent 原文" in dialog.preview_content_label.text()
        assert dialog.approve_button.isEnabled()
        assert dialog.reject_button.isEnabled()
        assert dialog.defer_button.isDefault()
        assert dialog.defer_button.hasFocus()

        dialog.preview_content_editor.setFocus()
        QTest.keyClick(dialog.preview_content_editor, Qt.Key.Key_Escape)
        _app().processEvents()

        assert dialog._approval_preview is None
        assert not dialog.approve_button.isEnabled()
        assert content not in dialog.preview_editor.toPlainText()
        assert content not in dialog.preview_content_editor.toPlainText()
        pending = next(item for item in controller.list_tasks(editor) if item["id"] == task["id"])
        assert pending["status"] == "awaiting_approval"
        assert "仍保持待审批" in dialog.status_label.text()
    finally:
        _close(dialog, controller)


def test_console_approval_executes_once_and_shows_receipt_without_content(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    editor = controller.principal("tenant-a", "agent-editor", "editor", "engineering")
    content = "# Approved note\n这段正文不应进入普通任务或收据。"
    task = controller.create_ingest_task(
        editor,
        filename="approved.md",
        collection_id="engineering",
        content=content,
        idempotency_key="ui-approve-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="human-approver",
            role="approver",
            collections="engineering",
        )
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])
        dialog._load_approval_preview()
        preview_hash = str(dialog._approval_preview["preview_hash"])
        dialog._confirm_approval = lambda _preview: True

        dialog._approve_selected()

        completed = next(
            item for item in controller.list_tasks(
                controller.principal("tenant-a", "viewer", "viewer", "engineering")
            )
            if item["id"] == task["id"]
        )
        assert completed["status"] == "succeeded"
        assert completed["receipt"]["approved_preview_hash"] == preview_hash
        assert content not in str(completed["receipt"])
        assert content not in dialog.receipt_detail.toPlainText()
        assert "operation_id" in dialog.receipt_detail.toPlainText()
        assert not dialog.task_detail.toPlainText().lstrip().startswith("{")
        assert "已成功执行" in dialog.task_detail.toPlainText()
        assert "批准依据是右侧加载的精确 Action Preview" in dialog.task_detail.toPlainText()
        assert dialog.task_technical_detail.isHidden()
        assert dialog.task_technical_detail.toPlainText().lstrip().startswith("{")
        assert content not in dialog.task_technical_detail.toPlainText()
        assert len(controller.list_documents(editor)) == 1
    finally:
        _close(dialog, controller)


@pytest.mark.parametrize("size", [(1180, 790), (980, 680)])
def test_console_keeps_guidance_and_task_actions_visible_at_supported_sizes(
    tmp_path: Path,
    size: tuple[int, int],
):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        dialog.resize(*size)
        dialog.tabs.setCurrentIndex(2)
        dialog.show()
        _app().processEvents()

        assert dialog.size().width() == size[0]
        assert dialog.size().height() == size[1]
        for widget in (
            dialog.title_label,
            dialog.findChild(type(dialog.status_label), "localIdentityWarning"),
            dialog.role_summary_label,
            dialog.identity_toggle,
            dialog.tabs,
            dialog.task_table,
            dialog.task_detail,
            dialog.receipt_detail,
            dialog.approval_hint_label,
            dialog.preview_button,
            dialog.approve_button,
            dialog.reject_button,
            dialog.defer_button,
            dialog.status_label,
        ):
            assert widget is not None
            assert widget.isVisibleTo(dialog)
            top_left = widget.mapTo(dialog, widget.rect().topLeft())
            bottom_right = widget.mapTo(dialog, widget.rect().bottomRight())
            assert top_left.x() >= 0
            assert top_left.y() >= 0
            assert bottom_right.x() < dialog.width()
            assert bottom_right.y() < dialog.height()
        assert dialog.task_table.height() >= 90
        assert dialog.task_detail.height() >= 70
        assert dialog.receipt_detail.height() >= 70
        for button in (
            dialog.preview_button,
            dialog.approve_button,
            dialog.reject_button,
            dialog.defer_button,
        ):
            assert button.width() >= button.sizeHint().width()
    finally:
        _close(dialog, controller)


def test_query_failure_clears_previous_answer_and_evidence(tmp_path: Path, monkeypatch):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        dialog.answer_edit.set_safe_markdown("# Previous answer\nSensitive evidence")
        dialog._citations = [{"id": "S1", "filename": "old.md"}]
        dialog.citation_table.setRowCount(1)
        dialog.citation_table.setItem(0, 0, QTableWidgetItem("S1"))
        dialog.citation_detail.setPlainText("old.md\nSensitive evidence")
        dialog.query_edit.setPlainText("new question")
        def fail_query(*_args, **_kwargs):
            raise ConnectionError("lost")

        monkeypatch.setattr(controller, "query", fail_query)
        monkeypatch.setattr(dialog, "_show_error", lambda *_args, **_kwargs: None)

        dialog._run_query()

        assert "Previous answer" not in dialog.answer_edit.toPlainText()
        assert "Sensitive evidence" not in dialog.answer_edit.toPlainText()
        assert dialog.citation_table.rowCount() == 0
        assert dialog._citations == []
        assert "暂无证据详情" in dialog.citation_detail.toPlainText()
        assert "旧答案与引用已清除" in dialog.answer_state.text()
    finally:
        _close(dialog, controller)


def test_rag_state_forces_untrusted_retrieval_metadata_to_plain_text(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    malicious = '<img src="data:image/png;base64,AAAA">'
    try:
        monkeypatch.setattr(
            controller,
            "query",
            lambda *_args, **_kwargs: {
                "answer": "Grounded answer",
                "refused": False,
                "citations": [],
                "retrieval": {"accepted_count": malicious, "top_score": malicious},
                "trace_id": "trace-safe-state",
            },
        )
        monkeypatch.setattr(dialog, "_refresh_metrics", lambda *args, **kwargs: None)
        dialog.query_edit.setPlainText("question")

        dialog._run_query()

        assert dialog.answer_state.textFormat() == Qt.TextFormat.PlainText
        assert malicious in dialog.answer_state.text()
    finally:
        _close(dialog, controller)


def test_task_card_explains_self_approval_and_preview_requirements(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    requester = controller.principal("tenant-a", "requester-a", "editor", "engineering")
    content = "# Pending\nOnly the exact preview may authorize this content."
    task = controller.create_ingest_task(
        requester,
        filename="pending.md",
        collection_id="engineering",
        content=content,
        idempotency_key="ui-task-card-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="requester-a",
            role="approver",
            collections="engineering",
        )
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])

        summary = dialog.task_detail.toPlainText()
        assert "写入并索引文档" in summary
        assert "pending.md" in summary
        assert "等待另一位审批人" in summary
        assert content not in summary
        assert not dialog.preview_button.isEnabled()
        assert not dialog.reject_button.isEnabled()
        assert "请求人不能审批自己的任务" in dialog.approval_hint_label.text()
        assert "请求人不能审批自己的任务" in dialog.preview_button.toolTip()

        _set_identity(
            dialog,
            tenant="tenant-a",
            user="approver-b",
            role="approver",
            collections="engineering",
        )
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])
        assert dialog.preview_button.isEnabled()
        assert dialog.reject_button.isEnabled()
        assert not dialog.approve_button.isEnabled()
        assert "批准前必须加载" in dialog.approval_hint_label.text()
        assert "结束任务" in dialog.reject_button.toolTip()
        assert "删除暂存正文" in dialog.reject_button.toolTip()

        dialog._load_approval_preview()
        assert dialog.approve_button.isEnabled()
        assert dialog.defer_button.isEnabled()
        assert "精确预览已加载" in dialog.approval_hint_label.text()
    finally:
        _close(dialog, controller)


def test_preview_and_task_refresh_failures_clear_stale_action_state(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    requester = controller.principal("tenant-a", "requester-a", "editor", "engineering")
    task = controller.create_ingest_task(
        requester,
        filename="failure.md",
        collection_id="engineering",
        content="staged content must not remain visible",
        idempotency_key="ui-failure-state-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="approver-b",
            role="approver",
            collections="engineering",
        )
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])
        dialog._load_approval_preview()
        assert dialog.defer_button.isEnabled()

        def fail_preview(*_args, **_kwargs):
            raise ConnectionError("preview unavailable")

        monkeypatch.setattr(controller, "approval_preview", fail_preview)
        monkeypatch.setattr(dialog, "_show_error", lambda *_args, **_kwargs: None)
        dialog._load_approval_preview()

        assert dialog._approval_preview is None
        assert not dialog.approve_button.isEnabled()
        assert not dialog.defer_button.isEnabled()
        assert "精确预览已加载" not in dialog.approval_hint_label.text()
        assert "批准前必须加载" in dialog.approval_hint_label.text()

        def fail_list(*_args, **_kwargs):
            raise ConnectionError("task list unavailable")

        monkeypatch.setattr(controller, "list_tasks", fail_list)
        dialog._refresh_tasks()

        assert dialog.task_table.rowCount() == 0
        assert dialog._tasks == {}
        assert "任务列表不可用" in dialog.task_detail.toPlainText()
        assert "没有可核查的操作收据" in dialog.receipt_detail.toPlainText()
        assert dialog.task_technical_detail.toPlainText() == ""
        assert not dialog.task_technical_toggle.isEnabled()
        assert not dialog.preview_button.isEnabled()
        assert not dialog.reject_button.isEnabled()
        assert not dialog.approve_button.isEnabled()
        assert not dialog.defer_button.isEnabled()
        assert "请先选择一个任务" in dialog.approval_hint_label.text()
    finally:
        _close(dialog, controller)


def test_console_hides_tenant_metrics_from_collection_scoped_identity(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="scoped-viewer",
            role="viewer",
            collections="engineering",
        )

        assert dialog.metric_labels["trace_count"].text() == "—"
        assert dialog.metric_labels["p95"].text() == "—"
        assert dialog.operation_table.rowCount() == 0
    finally:
        _close(dialog, controller)


def test_identity_edit_clears_scoped_state_freezes_actions_and_apply_switches_identity(
    tmp_path: Path,
):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        dialog.show()
        _app().processEvents()
        dialog.answer_edit.setPlainText("tenant-local answer")
        dialog._citations = [{"id": "S1", "filename": "tenant-local.md"}]
        dialog.citation_table.setRowCount(1)
        dialog.citation_detail.setPlainText("tenant-local evidence")
        dialog.trace_id_edit.setText("trace-tenant-local")
        dialog.trace_detail.setPlainText("tenant-local trace")
        dialog.document_table.setRowCount(1)
        dialog.task_table.setRowCount(1)
        dialog._approval_preview = {"preview_hash": "old-preview"}
        dialog._approval_task_id = "task-old"
        dialog.preview_content_editor.setPlainText("tenant-local staged content")

        dialog.tenant_edit.setFocus()
        dialog.tenant_edit.selectAll()
        QTest.keyClicks(dialog.tenant_edit, "tenant-b")
        _app().processEvents()

        assert dialog._identity_dirty is True
        assert not dialog.tabs.isEnabled()
        assert "尚未应用" in dialog.role_summary_label.text()
        assert dialog._approval_preview is None
        assert dialog.document_table.rowCount() == 0
        assert dialog.task_table.rowCount() == 0
        assert dialog.answer_edit.toPlainText() == ""
        assert dialog._citations == []
        assert dialog.citation_table.rowCount() == 0
        assert "tenant-local evidence" not in dialog.citation_detail.toPlainText()
        assert dialog.trace_id_edit.text() == ""
        assert dialog.trace_detail.toPlainText() == ""
        assert "staged content" not in dialog.preview_content_editor.toPlainText()
        assert dialog._principal().tenant_id == "tenant-local"

        dialog.user_edit.setText("viewer-b")
        dialog.collections_edit.setText("engineering")
        dialog._apply_identity()
        _app().processEvents()

        applied = dialog._principal()
        assert dialog._identity_dirty is False
        assert dialog.tabs.isEnabled()
        assert applied.tenant_id == "tenant-b"
        assert applied.user_id == "viewer-b"
        assert applied.roles == frozenset({"viewer"})
        assert applied.collection_ids == frozenset({"engineering"})
        assert "Viewer" in dialog.role_summary_label.text()
    finally:
        _close(dialog, controller)


def test_refreshing_an_externally_rejected_task_clears_loaded_preview(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    editor = controller.principal("tenant-a", "agent-editor", "editor", "engineering")
    task = controller.create_ingest_task(
        editor,
        filename="external-reject.md",
        collection_id="engineering",
        content="external terminal transition content",
        idempotency_key="ui-external-reject-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="human-approver",
            role="approver",
            collections="engineering",
        )
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])
        dialog._load_approval_preview()
        assert dialog._approval_preview is not None

        external_approver = controller.principal(
            "tenant-a",
            "other-approver",
            "approver",
            "engineering",
        )
        controller.reject_task(external_approver, task["id"], "handled outside this console")
        dialog._refresh_tasks()

        selected = dialog._selected_task()
        assert selected is not None
        assert selected["status"] == "rejected"
        assert dialog._approval_preview is None
        assert not dialog.approve_button.isEnabled()
        assert not dialog.defer_button.isEnabled()
        assert "external terminal transition content" not in dialog.preview_content_editor.toPlainText()
    finally:
        _close(dialog, controller)


def test_close_and_reopen_clears_sensitive_views(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    editor = controller.principal("tenant-a", "agent-editor", "editor", "engineering")
    content = "sensitive staged content must not survive window close"
    task = controller.create_ingest_task(
        editor,
        filename="close-preview.md",
        collection_id="engineering",
        content=content,
        idempotency_key="ui-close-preview-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        dialog.show()
        _app().processEvents()
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="human-approver",
            role="approver",
            collections="engineering",
        )
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])
        dialog._load_approval_preview()
        dialog.answer_edit.setPlainText("sensitive answer")
        dialog._citations = [{"id": "S1", "filename": "sensitive.md"}]
        dialog.citation_table.setRowCount(1)
        dialog.citation_detail.setPlainText("sensitive evidence")
        dialog.trace_detail.setPlainText("sensitive trace")
        retry_capsule = {
            "principal_signature": dialog._principal_signature(dialog._principal()),
            "path": "hidden-retry.md",
            "filename": "hidden-retry.md",
            "content": b"frozen retry bytes",
            "collection_id": "engineering",
            "idempotency_key": "hidden-retry-key",
        }
        dialog._upload_draft = retry_capsule
        dialog._set_status("uploaded sensitive-filename.md")
        assert content in dialog.preview_content_editor.toPlainText()

        dialog.close()
        _app().processEvents()

        assert dialog._approval_preview is None
        assert content not in dialog.preview_content_editor.toPlainText()
        assert dialog.answer_edit.toPlainText() == ""
        assert dialog._citations == []
        assert dialog.citation_table.rowCount() == 0
        assert "sensitive evidence" not in dialog.citation_detail.toPlainText()
        assert dialog.trace_detail.toPlainText() == ""
        assert dialog._upload_draft is retry_capsule
        assert "sensitive-filename.md" not in dialog.status_label.text()

        dialog.show()
        _app().processEvents()

        assert dialog._approval_preview is None
        assert content not in dialog.preview_content_editor.toPlainText()
        assert content not in dialog.task_detail.toPlainText()
        assert content not in dialog.receipt_detail.toPlainText()
        assert dialog._upload_draft is retry_capsule
        assert "sensitive-filename.md" not in dialog.status_label.text()
    finally:
        _close(dialog, controller)


def test_reject_requires_confirmation_and_never_executes_the_write(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    editor = controller.principal("tenant-a", "agent-editor", "editor", "engineering")
    content = "rejected content must never be indexed"
    task = controller.create_ingest_task(
        editor,
        filename="reject-me.md",
        collection_id="engineering",
        content=content,
        idempotency_key="ui-reject-confirm-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="human-approver",
            role="approver",
            collections="engineering",
        )
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])
        confirmation_ids: list[str] = []

        def decline(selected: dict) -> bool:
            confirmation_ids.append(str(selected["id"]))
            return False

        dialog._confirm_rejection = decline
        dialog._reject_selected()

        pending = next(item for item in controller.list_tasks(editor) if item["id"] == task["id"])
        assert confirmation_ids == [task["id"]]
        assert pending["status"] == "awaiting_approval"
        assert controller.list_documents(editor) == []

        dialog._confirm_rejection = lambda selected: str(selected["id"]) == task["id"]
        dialog._reject_selected()

        rejected = next(item for item in controller.list_tasks(editor) if item["id"] == task["id"])
        assert rejected["status"] == "rejected"
        assert rejected["receipt"] is None
        assert content not in str(rejected)
        assert controller.list_documents(editor) == []
        assert dialog._approval_preview is None
    finally:
        _close(dialog, controller)


def test_uncertain_rejection_clears_stale_preview_and_refreshes_task(tmp_path: Path, monkeypatch):
    _app()
    controller = _controller(tmp_path)
    editor = controller.principal("tenant-a", "agent-editor", "editor", "engineering")
    content = "rejection response may have been lost"
    task = controller.create_ingest_task(
        editor,
        filename="reject-uncertain.md",
        collection_id="engineering",
        content=content,
        idempotency_key="ui-reject-uncertain-001",
    )
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="human-approver",
            role="approver",
            collections="engineering",
        )
        KnowledgeAssistantDialog._select_row(dialog.task_table, task["id"])
        dialog._load_approval_preview()
        assert content in dialog.preview_content_editor.toPlainText()
        monkeypatch.setattr(dialog, "_confirm_rejection", lambda _task: True)
        monkeypatch.setattr(
            controller,
            "reject_task",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ConnectionError("response lost after rejection")
            ),
        )
        monkeypatch.setattr(dialog, "_show_error", lambda *_args, **_kwargs: None)

        dialog._reject_selected()

        assert dialog._approval_preview is None
        assert content not in dialog.preview_content_editor.toPlainText()
        assert not dialog.approve_button.isEnabled()
        assert dialog._selected_task()["status"] == "awaiting_approval"
    finally:
        _close(dialog, controller)


def test_write_dialogs_default_to_cancel_and_restore_uncertain_drafts(tmp_path: Path):
    _app()
    controller = _controller(tmp_path)
    upload_draft = {
        "path": str(tmp_path / "retry.md"),
        "collection_id": "engineering",
        "idempotency_key": "desktop-upload-retry",
    }
    task_draft = {
        "filename": "retry.md",
        "collection_id": "engineering",
        "content": "exact retry content",
        "idempotency_key": "desktop-task-retry",
    }
    upload = UploadDocumentDialog(controller, draft=upload_draft)
    task = IngestTaskDialog(controller, draft=task_draft)
    try:
        upload_buttons = upload.findChild(QDialogButtonBox)
        task_buttons = task.findChild(QDialogButtonBox)
        assert upload_buttons is not None
        assert task_buttons is not None
        assert upload_buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault()
        assert task_buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault()
        assert upload.path_edit.text() == upload_draft["path"]
        assert upload.collection_edit.text() == upload_draft["collection_id"]
        assert upload.idempotency_edit.text() == upload_draft["idempotency_key"]
        assert upload.path_edit.isReadOnly()
        assert upload.collection_edit.isReadOnly()
        assert upload.idempotency_edit.isReadOnly()
        assert task.filename_edit.text() == task_draft["filename"]
        assert task.collection_edit.text() == task_draft["collection_id"]
        assert task.content_edit.toPlainText() == task_draft["content"]
        assert task.idempotency_edit.text() == task_draft["idempotency_key"]
        assert task.filename_edit.isReadOnly()
        assert task.collection_edit.isReadOnly()
        assert task.idempotency_edit.isReadOnly()
        assert task.content_edit.isReadOnly()
    finally:
        upload.close()
        task.close()
        controller.close()


def test_explicit_abandon_discards_capsule_and_next_operation_gets_a_new_key(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    original_key = "desktop-upload-abandoned"
    draft = {
        "path": str(tmp_path / "abandoned.md"),
        "filename": "abandoned.md",
        "content": b"abandoned frozen bytes",
        "collection_id": "engineering",
        "idempotency_key": original_key,
        "principal_signature": dialog._principal_signature(dialog._principal()),
    }
    dialog._upload_draft = draft
    monkeypatch.setattr(dialog, "_confirm_abandon_uncertain", lambda *_args: True)
    try:
        dialog._handle_uncertain_cancel("上传", "_upload_draft", draft)

        assert dialog._upload_draft is None
        next_dialog = UploadDocumentDialog(controller)
        try:
            assert next_dialog.idempotency_edit.text() != original_key
            assert next_dialog.idempotency_edit.text().startswith("desktop-upload-")
        finally:
            next_dialog.close()
    finally:
        _close(dialog, controller)


def test_uncertain_upload_and_ingest_retries_reuse_the_exact_intent_and_key(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    _set_identity(
        dialog,
        tenant="tenant-a",
        user="editor-a",
        role="editor",
        collections="engineering",
    )
    upload_attempts: list[dict[str, object]] = []
    task_attempts: list[dict[str, str]] = []
    upload_seen_drafts: list[dict[str, object] | None] = []
    task_seen_drafts: list[dict[str, object] | None] = []
    prepared_paths: list[Path] = []

    class FakeUploadDialog:
        def __init__(self, _controller, _parent, *, draft=None, **_kwargs):
            upload_seen_drafts.append(dict(draft) if draft is not None else None)
            values = draft or {
                "path": str(tmp_path / "uncertain.md"),
                "collection_id": "engineering",
                "idempotency_key": "desktop-upload-uncertain",
            }
            self.path_edit = SimpleNamespace(text=lambda: values["path"])
            self.collection_edit = SimpleNamespace(text=lambda: values["collection_id"])
            self.idempotency_edit = SimpleNamespace(text=lambda: values["idempotency_key"])

        results = iter(
            (
                QDialog.DialogCode.Accepted,
                QDialog.DialogCode.Rejected,
                QDialog.DialogCode.Accepted,
            )
        )

        def exec(self):
            del self
            return next(FakeUploadDialog.results)

    class FakeIngestDialog:
        def __init__(self, _controller, _parent, *, draft=None):
            task_seen_drafts.append(dict(draft) if draft is not None else None)
            values = draft or {
                "filename": "uncertain.md",
                "collection_id": "engineering",
                "content": "exact uncertain content",
                "idempotency_key": "desktop-task-uncertain",
            }
            self.filename_edit = SimpleNamespace(text=lambda: values["filename"])
            self.collection_edit = SimpleNamespace(text=lambda: values["collection_id"])
            self.content_edit = SimpleNamespace(toPlainText=lambda: values["content"])
            self.idempotency_edit = SimpleNamespace(text=lambda: values["idempotency_key"])

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    def prepare_upload(path: Path):
        prepared_paths.append(path)
        return "uncertain.md", b"exact frozen upload bytes"

    def upload_content(_principal, *, filename, content, collection_id, idempotency_key):
        upload_attempts.append(
            {
                "filename": filename,
                "content": content,
                "collection_id": collection_id,
                "idempotency_key": idempotency_key,
            }
        )
        if len(upload_attempts) == 1:
            raise ConnectionError("response lost after commit")
        return {"id": "doc-replayed", "filename": "uncertain.md", "chunk_count": 1}

    def create_ingest_task(
        _principal,
        *,
        filename,
        collection_id,
        content,
        idempotency_key,
    ):
        task_attempts.append(
            {
                "filename": filename,
                "collection_id": collection_id,
                "content": content,
                "idempotency_key": idempotency_key,
            }
        )
        if len(task_attempts) == 1:
            raise ConnectionError("response lost after commit")
        return {"id": "task-replayed", "status": "awaiting_approval"}

    monkeypatch.setattr(ui_module, "UploadDocumentDialog", FakeUploadDialog)
    monkeypatch.setattr(ui_module, "IngestTaskDialog", FakeIngestDialog)
    monkeypatch.setattr(controller, "prepare_upload", prepare_upload)
    monkeypatch.setattr(controller, "upload_content", upload_content)
    monkeypatch.setattr(controller, "create_ingest_task", create_ingest_task)
    monkeypatch.setattr(dialog, "_show_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dialog, "_refresh_documents", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_refresh_tasks", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_refresh_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_confirm_abandon_uncertain", lambda *_args: False)
    try:
        dialog._upload_document()
        assert dialog._upload_draft is not None
        frozen_upload_draft = dialog._upload_draft
        dialog.show()
        _app().processEvents()
        dialog.close()
        dialog.show()
        _app().processEvents()
        assert dialog._upload_draft is frozen_upload_draft

        _set_identity(
            dialog,
            tenant="tenant-a",
            user="other-editor",
            role="editor",
            collections="engineering",
        )
        dialog._upload_document()
        assert dialog._upload_draft is frozen_upload_draft
        assert len(upload_attempts) == 1
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )

        dialog._upload_document()
        assert dialog._upload_draft is not None
        assert len(upload_attempts) == 1
        dialog._upload_document()
        assert dialog._upload_draft is None
        assert upload_seen_drafts[0] is None
        assert upload_seen_drafts[1] == upload_seen_drafts[2]
        assert upload_attempts[0] == upload_attempts[1]
        assert upload_attempts[0]["content"] == b"exact frozen upload bytes"
        assert prepared_paths == [tmp_path / "uncertain.md"]

        dialog._create_ingest_task()
        assert dialog._ingest_task_draft is not None
        dialog._create_ingest_task()
        assert dialog._ingest_task_draft is None
        assert task_seen_drafts[0] is None
        assert task_seen_drafts[1] is not None
        assert {
            key: task_seen_drafts[1][key]
            for key in ("filename", "collection_id", "content", "idempotency_key")
        } == task_attempts[0]
        assert task_attempts[0] == task_attempts[1]
    finally:
        _close(dialog, controller)


def test_definite_write_error_discards_retry_capsule(tmp_path: Path, monkeypatch):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)

    class FakeUploadDialog:
        def __init__(self, _controller, _parent, *, draft=None, **_kwargs):
            assert draft is None
            self.path_edit = SimpleNamespace(text=lambda: str(tmp_path / "definite.md"))
            self.collection_edit = SimpleNamespace(text=lambda: "engineering")
            self.idempotency_edit = SimpleNamespace(text=lambda: "definite-upload-key")

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    errors: list[str] = []
    monkeypatch.setattr(ui_module, "UploadDocumentDialog", FakeUploadDialog)
    monkeypatch.setattr(
        controller,
        "prepare_upload",
        lambda _path: ("definite.md", b"definite content"),
    )

    def reject_upload(*_args, **_kwargs):
        raise KnowledgeAssistantApiError(409, "conflict", "definite conflict")

    monkeypatch.setattr(controller, "upload_content", reject_upload)
    monkeypatch.setattr(dialog, "_show_error", lambda title, _error: errors.append(title))
    try:
        dialog._upload_document()

        assert dialog._upload_draft is None
        assert errors == ["上传失败；服务已返回确定结果"]

        def invalid_success_response(*_args, **_kwargs):
            raise KnowledgeAssistantApiError(201, "invalid_response", "truncated JSON")

        monkeypatch.setattr(controller, "upload_content", invalid_success_response)
        dialog._upload_document()

        assert dialog._upload_draft is not None
        assert errors[-1] == "上传结果未确认；再次打开将精确重放原请求"
    finally:
        _close(dialog, controller)


def test_write_outcome_classification_keeps_only_ambiguous_results():
    assert KnowledgeAssistantDialog._outcome_is_uncertain(ConnectionError("lost"))
    assert KnowledgeAssistantDialog._outcome_is_uncertain(
        KnowledgeAssistantApiError(201, "invalid_response", "truncated")
    )
    assert KnowledgeAssistantDialog._outcome_is_uncertain(
        KnowledgeAssistantApiError(408, "timeout", "request timeout")
    )
    assert KnowledgeAssistantDialog._outcome_is_uncertain(
        KnowledgeAssistantApiError(500, "internal_error", "failed")
    )
    assert not KnowledgeAssistantDialog._outcome_is_uncertain(
        KnowledgeAssistantApiError(400, "invalid_request", "rejected")
    )
    assert not KnowledgeAssistantDialog._outcome_is_uncertain(
        KnowledgeAssistantApiError(409, "conflict", "rejected")
    )


def test_dynamic_confirmation_fields_are_forced_to_plain_text(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    formats: list[Qt.TextFormat] = []
    rendered: list[str] = []

    def capture(box: QMessageBox):
        formats.append(box.textFormat())
        rendered.append(box.text() + "\n" + box.informativeText())
        return 0

    monkeypatch.setattr(QMessageBox, "exec", capture)
    malicious = "<h1>FORGED APPROVAL</h1>"
    try:
        dialog._confirm_archive_request(
            {
                "id": "doc-1",
                "filename": malicious,
                "collection_id": "engineering",
                "checksum": "checksum",
            },
            "archive-key",
        )
        dialog._confirm_approval(
            {
                "action": {
                    "tool_name": "ingest_text",
                    "target": {
                        "tenant_id": "tenant-a",
                        "collection_id": "engineering",
                        "filename": malicious,
                    },
                    "parameters": {},
                    "consequences": [malicious],
                    "reversibility": malicious,
                }
            }
        )
        dialog._confirm_rejection(
            {
                "id": "task-1",
                "tool_name": malicious,
                "requested_by": malicious,
            }
        )

        assert formats == [Qt.TextFormat.PlainText] * 3
        assert all(malicious in value for value in rendered)
    finally:
        _close(dialog, controller)


def test_uncertain_archive_task_retry_reuses_the_same_key(tmp_path: Path, monkeypatch):
    _app()
    controller = _controller(tmp_path)
    dialog = KnowledgeAssistantDialog(controller)
    _set_identity(
        dialog,
        tenant="tenant-a",
        user="editor-a",
        role="editor",
        collections="engineering",
    )
    document = {
        "id": "doc-archive-target",
        "filename": "archive.md",
        "collection_id": "engineering",
        "checksum": "synthetic-checksum",
    }
    attempts: list[tuple[str, str]] = []
    selection_calls = 0
    confirmation_documents: list[dict] = []

    def selected_document():
        nonlocal selection_calls
        selection_calls += 1
        return document if selection_calls == 1 else None

    def create_archive_task(_principal, *, document_id, idempotency_key):
        attempts.append((document_id, idempotency_key))
        if len(attempts) == 1:
            raise ConnectionError("response lost after commit")
        return {"id": "task-archive-replayed", "status": "failed"}

    def confirm(frozen_document, _idempotency_key):
        confirmation_documents.append(dict(frozen_document))
        return True

    monkeypatch.setattr(dialog, "_selected_document", selected_document)
    monkeypatch.setattr(dialog, "_confirm_archive_request", confirm)
    monkeypatch.setattr(dialog, "_show_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dialog, "_refresh_documents", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialog, "_refresh_tasks", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "create_archive_task", create_archive_task)
    try:
        dialog._create_archive_task()
        assert dialog._archive_task_draft is not None
        dialog._update_action_permissions()
        assert dialog.archive_task_button.isEnabled()
        assert "未确认" in dialog.archive_task_button.text()
        dialog._create_archive_task()

        assert attempts[0] == attempts[1]
        assert attempts[0][0] == document["id"]
        assert confirmation_documents == [document, document]
        assert selection_calls == 2
        assert dialog._archive_task_draft is None
        assert "不能据此断言" in dialog.status_label.text()
        assert "尚未归档" not in dialog.status_label.text()
    finally:
        _close(dialog, controller)


def test_batch_upload_dialog_defaults_to_cancel_and_limits_the_batch(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    dialog = BatchUploadDialog()
    try:
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        assert buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault()
        paths = [tmp_path / f"doc-{index}.md" for index in range(20)]
        for path in paths:
            path.write_text("content", encoding="utf-8")
        monkeypatch.setattr(
            QFileDialog,
            "getOpenFileNames",
            lambda *_args, **_kwargs: ([str(path) for path in paths], ""),
        )
        dialog._choose()
        _wait_until(lambda: not dialog._precheck_busy and dialog.submit_button.isEnabled())
        dialog.collection_edit.setText("engineering")
        dialog.accept()
        _wait_until(lambda: dialog.result() == QDialog.DialogCode.Accepted)
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_batch_upload_drop_files_enters_precheck(tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QMimeData, QPoint, QPointF, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent

    _app()
    source = tmp_path / "dropped.md"
    source.write_text("dropped content", encoding="utf-8")
    dialog = BatchUploadDialog()
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        enter = QDragEnterEvent(
            QPoint(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        dialog.dragEnterEvent(enter)
        assert enter.isAccepted()
        drop = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        dialog.dropEvent(drop)
        _wait_until(lambda: not dialog._precheck_busy and len(dialog.file_snapshots) == 1)
        assert dialog.paths[0].resolve() == source.resolve()
        assert dialog.submit_button.isEnabled()
        assert "拖放" in dialog.intro_label.text()
    finally:
        dialog.close()


def test_batch_upload_drop_directory_enters_precheck(tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDropEvent

    _app()
    folder = tmp_path / "folder"
    nested = folder / "policy"
    nested.mkdir(parents=True)
    source = nested / "dropped.md"
    source.write_text("dropped content", encoding="utf-8")
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)
    dialog = BatchUploadDialog()
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(folder))])
        drop = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        dialog.dropEvent(drop)
        _wait_until(lambda: not dialog._precheck_busy and len(dialog.file_snapshots) == 1)
        assert dialog.paths[0].resolve() == source.resolve()
        assert dialog.submit_button.isEnabled()
        assert dialog.findChild(QPushButton, "chooseIngestionDirectory") is not None
    finally:
        dialog.close()


def test_batch_upload_drop_over_quota_directory_fails_closed(tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDropEvent

    from table_miku.knowledge_assistant_desktop import MAX_BATCH_FILES

    _app()
    folder = tmp_path / "many"
    folder.mkdir()
    for index in range(MAX_BATCH_FILES + 1):
        (folder / f"doc-{index:02d}.md").write_text("x", encoding="utf-8")
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = BatchUploadDialog()
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(folder))])
        drop = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        dialog.dropEvent(drop)
        assert warnings and warnings[-1][0] == "文件过多"
        assert dialog.paths == []
        assert dialog.file_snapshots == []
        assert dialog.submit_button.isEnabled() is False
    finally:
        dialog.close()


def test_batch_upload_choose_directory_enters_precheck(tmp_path: Path, monkeypatch):
    _app()
    folder = tmp_path / "chosen"
    folder.mkdir()
    source = folder / "notes.txt"
    source.write_text("notes", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(folder),
    )
    dialog = BatchUploadDialog()
    try:
        dialog._choose_directory()
        _wait_until(lambda: not dialog._precheck_busy and len(dialog.file_snapshots) == 1)
        assert dialog.paths[0].resolve() == source.resolve()
        assert dialog.submit_button.isEnabled()
    finally:
        dialog.close()


def test_batch_upload_duplicate_hint_after_precheck(tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDropEvent

    _app()
    source = tmp_path / "dropped.md"
    source.write_text("dropped content", encoding="utf-8")
    calls: list[tuple[str, list[str]]] = []

    def lookup(collection_id: str, checksums: list[str]) -> list[dict[str, str]]:
        calls.append((collection_id, list(checksums)))
        return [
            {
                "id": "doc-existing",
                "filename": "existing.md",
                "collection_id": collection_id,
                "checksum": checksums[0],
            }
        ]

    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)
    dialog = BatchUploadDialog(duplicate_lookup=lookup)
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        drop = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        dialog.dropEvent(drop)
        _wait_until(lambda: not dialog._precheck_busy and len(dialog.file_snapshots) == 1)
        assert "dropped.md" in dialog.duplicate_label.text()
        assert "existing.md" in dialog.duplicate_label.text()
        assert dialog.submit_button.isEnabled()
        assert calls and calls[0][0] == "default"
        dialog.collection_edit.setText("engineering")
        assert calls[-1][0] == "engineering"
    finally:
        dialog.close()


def test_batch_upload_restricted_collection_combo_lists_allowlist(tmp_path: Path):
    _app()
    principal = Principal(
        "tenant-a",
        "editor-1",
        frozenset({"editor"}),
        frozenset({"ops", "legal"}),
    )
    store = CollectionMruStore(tmp_path / "collection_mru.json")
    store.remember(principal, "legal")
    dialog = BatchUploadDialog(principal=principal, collection_mru=store)
    try:
        items = [dialog.collection_edit.itemText(index) for index in range(dialog.collection_edit.count())]
        assert items[0] == "legal"
        assert set(items) == {"legal", "ops"}
        assert dialog.collection_edit.isEditable() is False
        assert dialog.collection_edit.objectName() == "batchUploadCollection"
    finally:
        dialog.close()


def test_batch_upload_deny_all_blocks_accept(tmp_path: Path, monkeypatch):
    _app()
    source = tmp_path / "notes.md"
    source.write_text("notes", encoding="utf-8")
    principal = Principal("tenant-a", "editor-1", frozenset({"editor"}), frozenset())
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = BatchUploadDialog(principal=principal)
    try:
        dialog._paths = [source]
        dialog.accept()
        assert warnings and warnings[-1][0] == "没有可用集合"
        assert dialog.result() != QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_batch_upload_preview_is_specific_and_fails_closed_if_a_file_changes(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "same.md"
    second = second_dir / "same.md"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(first), str(second)], ""),
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = BatchUploadDialog()
    try:
        dialog._choose()
        _wait_until(lambda: not dialog._precheck_busy and len(dialog.file_snapshots) == 2)

        paths = [dialog.file_table.item(row, 0).text() for row in range(2)]
        assert paths == [str(first.resolve()), str(second.resolve())]
        assert len(dialog.file_snapshots) == 2
        assert set(dialog.file_snapshots[0]) == {
            "canonical_path",
            "size",
            "mtime_ns",
            "device",
            "inode",
            "sha256",
        }
        assert dialog.file_snapshots[0]["sha256"] == hashlib.sha256(b"first").hexdigest()
        assert dialog.file_table.horizontalHeaderItem(3).text() == "SHA-256 摘要"
        expected_digest = hashlib.sha256(b"first").hexdigest()
        assert dialog.file_table.item(0, 3).text() == f"{expected_digest[:16]}…"
        assert dialog.file_table.item(0, 3).toolTip() == expected_digest
        assert dialog.submit_button.text() == "加入摄取队列（2）"
        assert "直接写入" in dialog.intro_label.text()
        assert "无需审批" in dialog.intro_label.text()
        assert "部分成功" in dialog.intro_label.text()
        assert "不支持 OCR" in dialog.intro_label.text()

        first.write_text("changed after preview", encoding="utf-8")
        dialog.collection_edit.setText("engineering")
        dialog.accept()
        _wait_until(lambda: warnings and warnings[-1][0] == "文件已变化")

        assert dialog.result() == QDialog.DialogCode.Rejected
        assert warnings and warnings[-1][0] == "文件已变化"
    finally:
        dialog.close()


def test_batch_upload_rejects_same_size_rewrite_even_if_mtime_is_restored(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    source = tmp_path / "stable.md"
    source.write_bytes(b"first")
    original = source.stat()
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(source)], ""),
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = BatchUploadDialog()
    try:
        dialog._choose()
        _wait_until(lambda: not dialog._precheck_busy and len(dialog.file_snapshots) == 1)
        accepted_hash = dialog.file_snapshots[0]["sha256"]
        source.write_bytes(b"other")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert source.stat().st_size == original.st_size
        assert source.stat().st_mtime_ns == original.st_mtime_ns

        dialog.accept()
        _wait_until(lambda: warnings and warnings[-1][0] == "文件已变化")

        assert dialog.result() == QDialog.DialogCode.Rejected
        assert warnings and warnings[-1][0] == "文件已变化"
        assert hashlib.sha256(source.read_bytes()).hexdigest() != accepted_hash
    finally:
        dialog.close()


def test_batch_upload_rejects_oversized_file_during_selection(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    oversized = tmp_path / "oversized.pdf"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_DOCUMENT_BYTES + 1)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(oversized)], ""),
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = BatchUploadDialog()
    try:
        dialog._choose()
        _wait_until(lambda: not dialog._precheck_busy)

        assert dialog.paths == []
        assert dialog.file_snapshots == []
        assert warnings and warnings[-1][0] == "文件不可读取"
        assert str(MAX_DOCUMENT_BYTES) in warnings[-1][1]
    finally:
        dialog.close()


def test_ingestion_surfaces_remain_visible_at_minimum_supported_size(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        dialog.resize(980, 680)
        dialog.tabs.setCurrentIndex(4)
        dialog.show()
        _app().processEvents()

        for widget in (
            dialog.ingestion_table,
            dialog.ingestion_progress,
            dialog.ingestion_detail,
            dialog.ingestion_cancel_button,
            dialog.ingestion_retry_button,
            dialog.ingestion_abandon_button,
        ):
            assert widget.isVisibleTo(dialog)
            top_left = widget.mapTo(dialog, widget.rect().topLeft())
            bottom_right = widget.mapTo(dialog, widget.rect().bottomRight())
            assert top_left.x() >= 0
            assert top_left.y() >= 0
            assert bottom_right.x() < dialog.width()
            assert bottom_right.y() < dialog.height()
    finally:
        _close(dialog, controller)


def test_batch_upload_enters_background_queue_without_blocking_ui(tmp_path: Path, monkeypatch):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    source = tmp_path / "guide.md"
    source.write_text("guide", encoding="utf-8")

    class AcceptedBatchDialog:
        paths = [source]
        file_snapshots = [
            {
                "canonical_path": str(source.resolve()),
                "size": source.stat().st_size,
                "mtime_ns": source.stat().st_mtime_ns,
                "device": source.stat().st_dev,
                "inode": source.stat().st_ino,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ]
        collection_edit = SimpleNamespace(text=lambda: "engineering")

        def __init__(self, _parent=None, **_kwargs):
            pass

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui_module, "BatchUploadDialog", AcceptedBatchDialog)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        dialog._choose_batch_upload()

        assert len(coordinator.submissions) == 1
        principal, paths, collection, generation, snapshots = coordinator.submissions[0]
        assert principal.user_id == "editor-a"
        assert paths == [source]
        assert collection == "engineering"
        assert generation == dialog._ingestion_generation
        assert snapshots == AcceptedBatchDialog.file_snapshots
        assert dialog.tabs.currentIndex() == 4
        assert dialog._collection_mru.suggestions(dialog._principal())[0] == "engineering"
        other = Principal("tenant-a", "editor-b", frozenset({"editor"}))
        assert dialog._collection_mru.suggestions(other) == ["default"]
    finally:
        _close(dialog, controller)


def test_ingestion_tab_shows_last_batch_summary_with_failed_filename(
    tmp_path: Path, monkeypatch
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    source = tmp_path / "guide.md"
    source.write_text("guide", encoding="utf-8")

    class AcceptedBatchDialog:
        paths = [source]
        file_snapshots = [
            {
                "canonical_path": str(source.resolve()),
                "size": source.stat().st_size,
                "mtime_ns": source.stat().st_mtime_ns,
                "device": source.stat().st_dev,
                "inode": source.stat().st_ino,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ]
        collection_edit = SimpleNamespace(text=lambda: "engineering")

        def __init__(self, _parent=None, **_kwargs):
            pass

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui_module, "BatchUploadDialog", AcceptedBatchDialog)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        dialog._choose_batch_upload()

        summary = dialog.ingestion_batch_summary
        assert summary.objectName() == "ingestionBatchSummary"
        assert summary.textFormat() == Qt.TextFormat.PlainText
        assert "本批 1 个" in summary.text()
        assert "进行中 1" in summary.text()

        dialog._on_ingestion_update(
            {
                "local_id": "local-1",
                "filename": r"C:\vault\secret\guide.md",
                "collection_id": "engineering",
                "status": "failed",
                "error_message": "Document validation failed.",
                "generation": dialog._ingestion_generation,
                "principal_signature": dialog._principal_signature(dialog._principal()),
            }
        )
        text = summary.text()
        assert "成功 0" in text
        assert "失败 1" in text
        assert "guide.md：Document validation failed." in text
        assert r"C:\vault" not in text
    finally:
        _close(dialog, controller)


def test_ingestion_batch_summary_follows_job_snapshot_and_clears_on_identity_change(
    tmp_path: Path, monkeypatch
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    source = tmp_path / "guide.md"
    source.write_text("guide", encoding="utf-8")

    class AcceptedBatchDialog:
        paths = [source]
        file_snapshots = [
            {
                "canonical_path": str(source.resolve()),
                "size": source.stat().st_size,
                "mtime_ns": source.stat().st_mtime_ns,
                "device": source.stat().st_dev,
                "inode": source.stat().st_ino,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ]
        collection_edit = SimpleNamespace(text=lambda: "engineering")

        def __init__(self, _parent=None, **_kwargs):
            pass

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui_module, "BatchUploadDialog", AcceptedBatchDialog)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        dialog._choose_batch_upload()
        generation = dialog._ingestion_generation
        signature = dialog._principal_signature(dialog._principal())
        dialog._on_ingestion_update(
            {
                "local_id": "local-1",
                "job_id": "job-9",
                "filename": "guide.md",
                "collection_id": "engineering",
                "status": "running",
                "generation": generation,
                "principal_signature": signature,
            }
        )
        dialog._on_ingestion_update(
            {
                "status": "snapshot",
                "jobs": [
                    {
                        "id": "job-9",
                        "requested_by": "editor-a",
                        "filename": "guide.md",
                        "collection_id": "engineering",
                        "status": "succeeded",
                    }
                ],
                "generation": generation,
                "principal_signature": signature,
            }
        )
        text = dialog.ingestion_batch_summary.text()
        assert "本批 1 个" in text
        assert "成功 1" in text
        assert "失败 0" in text
        assert "job:job-9" in dialog._ingestion_items
        assert "local-1" not in dialog._ingestion_items

        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-b",
            role="editor",
            collections="engineering",
        )
        assert dialog.ingestion_batch_summary.text() == ""
    finally:
        _close(dialog, controller)


def test_ingestion_snapshot_keeps_safe_job_details_and_recovery_across_refresh(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        generation = dialog._ingestion_generation
        signature = dialog._principal_signature(dialog._principal())
        principal = {
            "tenant_id": "tenant-a",
            "user_id": "editor-a",
            "roles": ["editor"],
            "collection_ids": ["engineering"],
        }
        recovery_id = "outbox-" + "a" * 32
        tracking_id = "outbox-" + "c" * 32
        dialog._on_recovery_update(
            [
                {
                    "entry_id": recovery_id,
                    "status": "pending",
                    "principal": principal,
                    "filename": "pending.md",
                    "collection_id": "engineering",
                },
                {
                    "entry_id": tracking_id,
                    "status": "tracking",
                    "principal": principal,
                    "filename": "guide.md",
                    "collection_id": "engineering",
                    "job_id": "job-1",
                },
            ]
        )
        dialog._on_ingestion_update(
            {
                "status": "snapshot",
                "jobs": [
                    {
                        "id": "job-1",
                        "requested_by": "editor-a",
                        "filename": "guide.md",
                        "collection_id": "engineering",
                        "status": "failed",
                        "progress": {"phase": "embedding", "current": 2, "total": 4},
                        "error_code": "invalid_document",
                        "error_message": "Document validation failed.",
                        "attempt_count": 2,
                        "max_attempts": 3,
                        "trace_id": "trace-1",
                        "document_id": None,
                        "cancel_outcome": "already_terminal",
                    }
                ],
                "generation": generation,
                "principal_signature": signature,
            }
        )

        assert recovery_id in dialog._ingestion_items
        job = dialog._ingestion_items["job:job-1"]
        assert job["entry_id"] == tracking_id
        assert job["requested_by"] == "editor-a"
        assert job["progress"] == {"phase": "embedding", "current": 2, "total": 4}
        assert job["error_code"] == "invalid_document"
        assert job["error_message"] == "Document validation failed."
        assert job["attempt_count"] == 2
        assert job["max_attempts"] == 3
        assert job["trace_id"] == "trace-1"
        assert "document_id" in job
        assert job["cancel_outcome"] == "already_terminal"

        dialog._select_row(dialog.ingestion_table, "job:job-1")
        dialog._ingestion_selected()
        assert "embedding" in dialog.ingestion_progress.format()
        assert "2/4" in dialog.ingestion_progress.format()
        assert "Document validation failed." in dialog.ingestion_detail.toPlainText()
    finally:
        _close(dialog, controller)


def test_ingestion_cancel_respects_role_and_requester_and_previews_exact_action(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    captured: list[QMessageBox] = []

    def keep_default(box: QMessageBox):
        captured.append(box)
        return 0

    monkeypatch.setattr(QMessageBox, "exec", keep_default)
    try:
        generation = dialog._ingestion_generation
        signature = dialog._principal_signature(dialog._principal())
        dialog._on_ingestion_update(
            {
                "status": "snapshot",
                "jobs": [
                    {
                        "id": "job-1",
                        "requested_by": "editor-a",
                        "filename": "guide.md",
                        "collection_id": "engineering",
                        "status": "queued",
                    }
                ],
                "generation": generation,
                "principal_signature": signature,
            }
        )
        dialog.ingestion_table.selectRow(0)
        dialog._ingestion_selected()
        assert not dialog.ingestion_cancel_button.isEnabled()
        assert "写入权限" in dialog.ingestion_cancel_button.toolTip()

        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-b",
            role="editor",
            collections="engineering",
        )
        generation = dialog._ingestion_generation
        signature = dialog._principal_signature(dialog._principal())
        dialog._on_ingestion_update(
            {
                "status": "snapshot",
                "jobs": [
                    {
                        "id": "job-1",
                        "requested_by": "editor-a",
                        "filename": "guide.md",
                        "collection_id": "engineering",
                        "status": "queued",
                    }
                ],
                "generation": generation,
                "principal_signature": signature,
            }
        )
        dialog.ingestion_table.selectRow(0)
        dialog._ingestion_selected()
        assert not dialog.ingestion_cancel_button.isEnabled()
        assert "发起人 editor-a" in dialog.ingestion_cancel_button.toolTip()

        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        generation = dialog._ingestion_generation
        signature = dialog._principal_signature(dialog._principal())
        dialog._on_ingestion_update(
            {
                "status": "snapshot",
                "jobs": [
                    {
                        "id": "job-1",
                        "requested_by": "editor-a",
                        "filename": "guide.md",
                        "collection_id": "engineering",
                        "status": "queued",
                    }
                ],
                "generation": generation,
                "principal_signature": signature,
            }
        )
        dialog.ingestion_table.selectRow(0)
        dialog._cancel_selected_ingestion()

        assert coordinator.cancelled_jobs == []
        box = captured[-1]
        assert box.textFormat() == Qt.TextFormat.PlainText
        rendered = box.text() + "\n" + box.informativeText()
        assert "job-1" in rendered
        assert "guide.md" in rendered
        assert "engineering" in rendered
        assert "editor-a" in rendered
        assert box.defaultButton() is box.escapeButton()
        assert box.defaultButton().text() == "保留任务"
    finally:
        _close(dialog, controller)


def test_uncertain_recovery_cancel_uses_durable_cancel_intent_and_too_late_is_a_receipt(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    monkeypatch.setattr(dialog, "_confirm_ingestion_cancel", lambda *_args: True)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        entry_id = "outbox-" + "b" * 32
        dialog._ingestion_items[entry_id] = {
            "entry_id": entry_id,
            "filename": "uncertain.md",
            "collection_id": "engineering",
            "status": "outcome_unknown",
            "requested_by": "editor-a",
        }
        dialog._render_ingestion_items()
        dialog.ingestion_table.selectRow(0)
        dialog._cancel_selected_ingestion()

        assert coordinator.cancelled_recoveries == [
            (dialog._principal(), entry_id, dialog._ingestion_generation)
        ]
        assert "取消意图已保留" in dialog.ingestion_detail.toPlainText()

        signature = dialog._principal_signature(dialog._principal())
        dialog._on_ingestion_update(
            {
                "status": "snapshot",
                "jobs": [
                    {
                        "id": "job-late",
                        "requested_by": "editor-a",
                        "filename": "late.md",
                        "collection_id": "engineering",
                        "status": "succeeded",
                        "document_id": "doc-late",
                        "cancel_outcome": "too_late",
                    }
                ],
                "generation": dialog._ingestion_generation,
                "principal_signature": signature,
            }
        )
        dialog._select_row(dialog.ingestion_table, "job:job-late")
        dialog._ingestion_selected()
        assert "取消过晚，文档已写入" in dialog.ingestion_detail.toPlainText()
    finally:
        _close(dialog, controller)


def test_rejected_cancel_recovery_is_visible_and_only_retries_after_user_consent(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    monkeypatch.setattr(dialog, "_confirm_ingestion_cancel", lambda *_args: True)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        principal = {
            "tenant_id": "tenant-a",
            "user_id": "editor-a",
            "roles": ["editor"],
            "collection_ids": ["engineering"],
        }
        entry_id = "outbox-" + "d" * 32
        dialog._on_recovery_update(
            [
                {
                    "entry_id": entry_id,
                    "status": "tracking",
                    "principal": principal,
                    "requested_by": "editor-a",
                    "filename": "guide.md",
                    "collection_id": "engineering",
                    "job_id": "job-rejected",
                    "cancel_delivery_state": "rejected",
                }
            ]
        )
        assert dialog._ingestion_items[entry_id]["status"] == "cancel_rejected"

        dialog._on_ingestion_update(
            {
                "status": "snapshot",
                "jobs": [
                    {
                        "id": "job-rejected",
                        "requested_by": "editor-a",
                        "filename": "guide.md",
                        "collection_id": "engineering",
                        "status": "running",
                    }
                ],
                "generation": dialog._ingestion_generation,
                "principal_signature": dialog._principal_signature(dialog._principal()),
            }
        )

        assert dialog.ingestion_table.rowCount() == 1
        dialog._select_row(dialog.ingestion_table, "job:job-rejected")
        dialog._ingestion_selected()
        assert "服务端明确拒绝" in dialog.ingestion_detail.toPlainText()
        assert dialog.ingestion_cancel_button.isEnabled()
        assert "上次取消被明确拒绝" in dialog.ingestion_cancel_button.toolTip()

        dialog._cancel_selected_ingestion()
        assert coordinator.cancelled_jobs == [
            (dialog._principal(), "job-rejected", dialog._ingestion_generation)
        ]
    finally:
        _close(dialog, controller)


def test_reconciliation_required_survives_server_snapshot_as_attention(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        entry_id = "outbox-" + "e" * 32
        dialog._on_recovery_update(
            [
                {
                    "entry_id": entry_id,
                    "status": "tracking",
                    "principal": {
                        "tenant_id": "tenant-a",
                        "user_id": "editor-a",
                        "roles": ["editor"],
                        "collection_ids": ["engineering"],
                    },
                    "requested_by": "editor-a",
                    "filename": "missing.md",
                    "collection_id": "engineering",
                    "job_id": "job-missing",
                    "cancel_delivery_state": "none",
                }
            ]
        )
        common = {
            "generation": dialog._ingestion_generation,
            "principal_signature": dialog._principal_signature(dialog._principal()),
        }
        dialog._on_ingestion_update(
            {
                **common,
                "entry_id": entry_id,
                "job_id": "job-missing",
                "status": "reconciliation_required",
                "message": "服务端未返回该跟踪任务；本地记录已保留，请人工核查。",
            }
        )
        dialog._on_ingestion_update({**common, "status": "snapshot", "jobs": []})

        assert dialog._ingestion_items[entry_id]["status"] == "reconciliation_required"
        attention_index = dialog.ingestion_filter.findData("attention")
        assert attention_index >= 0
        dialog.ingestion_filter.setCurrentIndex(attention_index)
        dialog._render_ingestion_items()
        assert dialog.ingestion_table.rowCount() == 1
        assert dialog.ingestion_table.item(0, 0).text() == "需人工对账"
        dialog.ingestion_table.selectRow(0)
        dialog._ingestion_selected()
        assert not dialog.ingestion_cancel_button.isEnabled()
        assert "本地记录已保留" in dialog.ingestion_detail.toPlainText()
    finally:
        _close(dialog, controller)


def test_unknown_tracking_cancel_never_offers_replay_of_the_original_upload(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        entry_id = "outbox-" + "f" * 32
        dialog._on_recovery_update(
            [
                {
                    "entry_id": entry_id,
                    "status": "tracking",
                    "principal": {
                        "tenant_id": "tenant-a",
                        "user_id": "editor-a",
                        "roles": ["editor"],
                        "collection_ids": ["engineering"],
                    },
                    "requested_by": "editor-a",
                    "filename": "guide.md",
                    "collection_id": "engineering",
                    "job_id": "job-unknown-cancel",
                    "cancel_delivery_state": "unknown",
                }
            ]
        )

        dialog.ingestion_table.selectRow(0)
        dialog._ingestion_selected()
        assert dialog._ingestion_items[entry_id]["status"] == "outcome_unknown"
        assert not dialog.ingestion_retry_button.isEnabled()
        assert "不能重传原请求" in dialog.ingestion_retry_button.toolTip()
        assert dialog.ingestion_cancel_button.isEnabled()
        assert not dialog.ingestion_abandon_button.isEnabled()
        assert "已绑定服务端任务" in dialog.ingestion_abandon_button.toolTip()
    finally:
        _close(dialog, controller)


def test_ingestion_ui_distinguishes_cancelling_and_ignores_stale_identity_results(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        _set_identity(
            dialog,
            tenant="tenant-a",
            user="editor-a",
            role="editor",
            collections="engineering",
        )
        generation = dialog._ingestion_generation
        signature = dialog._principal_signature(dialog._principal())
        dialog._on_ingestion_update(
            {
                "local_id": "local-1",
                "filename": "guide.md",
                "collection_id": "engineering",
                "status": "cancelling",
                "message": "取消请求中；尚不能断言任务已取消。",
                "generation": generation,
                "principal_signature": signature,
            }
        )

        assert dialog.ingestion_table.item(0, 0).text() == "取消请求中"
        assert "尚不能断言" in dialog.ingestion_table.item(0, 3).text()
        dialog._on_ingestion_update(
            {
                "local_id": "local-1",
                "status": "succeeded",
                "generation": generation - 1,
                "principal_signature": signature,
            }
        )
        assert dialog.ingestion_table.item(0, 0).text() == "取消请求中"

        dialog._identity_draft_changed()
        assert dialog.ingestion_table.rowCount() == 0
    finally:
        _close(dialog, controller)


def test_close_clears_ingestion_view_without_replaying_or_stopping_coordinator(
    tmp_path: Path,
    monkeypatch,
):
    _app()
    controller = _controller(tmp_path)
    coordinator = _FakeIngestionCoordinator()
    monkeypatch.setattr(controller, "create_ingestion_coordinator", lambda: coordinator)
    dialog = KnowledgeAssistantDialog(controller)
    try:
        dialog._ingestion_items["local-1"] = {
            "status": "outcome_unknown",
            "filename": "private.md",
            "collection_id": "engineering",
            "entry_id": "outbox-" + "a" * 32,
            "message": "结果待确认",
        }
        dialog._render_ingestion_items()
        assert dialog.ingestion_table.rowCount() == 1

        dialog.close()
        _app().processEvents()

        assert dialog.ingestion_table.rowCount() == 0
        assert coordinator.replays == []
        assert coordinator.scan_count >= 1
    finally:
        _close(dialog, controller)
