from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QTableWidgetItem,
)

from table_miku.knowledge_assistant import KnowledgeAssistantService
from table_miku.knowledge_assistant.client import KnowledgeAssistantApiError
from table_miku.knowledge_assistant_desktop import KnowledgeAssistantDesktopController
import table_miku.knowledge_assistant_ui as ui_module
from table_miku.knowledge_assistant_ui import (
    IngestTaskDialog,
    KnowledgeAssistantDialog,
    SafeMarkdownBrowser,
    UploadDocumentDialog,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


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
        def __init__(self, _controller, _parent, *, draft=None):
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
        def __init__(self, _controller, _parent, *, draft=None):
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
