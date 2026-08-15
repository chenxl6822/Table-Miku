from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor, QTextDocument
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
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .knowledge_assistant.auth import Principal
from .knowledge_assistant.client import KnowledgeAssistantApiError
from .knowledge_assistant_desktop import KnowledgeAssistantDesktopController
from .knowledge_assistant_desktop import MAX_BATCH_FILES
from .knowledge_assistant_file_precheck import FilePrecheckController, PrecheckBatchResult


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
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QTableWidget {
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
QPlainTextEdit, QTextBrowser {
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


class SafeMarkdownBrowser(QTextBrowser):
    """Render answer Markdown without navigation or external resource loading."""

    _MARKDOWN_FEATURES = (
        QTextDocument.MarkdownFeature.MarkdownNoHTML
        | QTextDocument.MarkdownFeature.MarkdownDialectGitHub
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.document().setDocumentMargin(12)
        self.document().setDefaultStyleSheet(
            "h1 { font-size: 20px; margin: 8px 0 5px 0; }"
            "h2 { font-size: 17px; margin: 7px 0 4px 0; }"
            "h3 { font-size: 15px; margin: 6px 0 3px 0; }"
            "p { margin: 4px 0; }"
            "pre { background-color: #f2f5f9; color: #203247; padding: 8px; }"
            "code { font-family: Consolas, 'Courier New', monospace; background-color: #f2f5f9; }"
            "blockquote { color: #526176; margin-left: 12px; }"
        )

    def loadResource(self, _resource_type, _name):  # noqa: N802 - Qt virtual method
        return None

    def set_safe_markdown(self, markdown: str) -> None:
        self.document().setMarkdown(str(markdown or ""), self._MARKDOWN_FEATURES)
        self._sanitize_document(self.document())

    @classmethod
    def markdown_to_plain_text(cls, markdown: str) -> str:
        document = QTextDocument()
        document.setMarkdown(str(markdown or ""), cls._MARKDOWN_FEATURES)
        cls._sanitize_document(document)
        return document.toPlainText().strip()

    @staticmethod
    def _sanitize_document(document: QTextDocument) -> None:
        anchors: list[tuple[int, int, QTextCharFormat]] = []
        images: list[tuple[int, int]] = []
        block = document.begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    char_format = fragment.charFormat()
                    if char_format.isImageFormat():
                        images.append((fragment.position(), fragment.length()))
                    elif char_format.isAnchor():
                        anchors.append(
                            (fragment.position(), fragment.length(), char_format)
                        )
                iterator += 1
            block = block.next()
        for position, length, char_format in anchors:
            clean_format = QTextCharFormat(char_format)
            clean_format.setAnchor(False)
            clean_format.setAnchorHref("")
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(clean_format)
        for position, length in reversed(images):
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText("［图片已禁用］")


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


class BatchUploadDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("批量添加知识资料")
        self.resize(760, 520)
        self.setStyleSheet(CONSOLE_STYLE)
        self._paths: list[Path] = []
        self._file_snapshots: list[dict[str, int | str]] = []
        self._failed_prechecks: list[dict[str, str]] = []
        self._pending_paths: list[Path] = []
        self._precheck_generation = 0
        self._precheck_busy = False
        self._confirming = False
        self._precheck = FilePrecheckController(self)
        self._precheck.progress.connect(self._on_precheck_progress)
        self._precheck.finished.connect(self._on_precheck_finished)
        layout = QVBoxLayout(self)
        self.intro_label = QLabel(
            f"这是当前 Editor 身份的直接写入，一次最多选择 {MAX_BATCH_FILES} 个文件，"
            "无需审批。每个文件独立摄取，部分成功不会回滚；PDF 仅支持文本层，不支持 OCR。"
            "选择后会在后台校验 SHA-256，可取消尚未确认的预检。",
            self,
        )
        self.intro_label.setWordWrap(True)
        self.intro_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.intro_label)
        choose_row = QHBoxLayout()
        choose = QPushButton("选择文件…", self)
        choose.setObjectName("chooseIngestionFiles")
        choose.clicked.connect(self._choose)
        choose_row.addWidget(choose)
        self.cancel_precheck_button = QPushButton("取消预检", self)
        self.cancel_precheck_button.setObjectName("cancelFilePrecheck")
        self.cancel_precheck_button.setEnabled(False)
        self.cancel_precheck_button.clicked.connect(self._cancel_precheck)
        choose_row.addWidget(self.cancel_precheck_button)
        self.exclude_failed_button = QPushButton("排除失败项", self)
        self.exclude_failed_button.setObjectName("excludeFailedPrechecks")
        self.exclude_failed_button.setEnabled(False)
        self.exclude_failed_button.clicked.connect(self._exclude_failed_prechecks)
        choose_row.addWidget(self.exclude_failed_button)
        choose_row.addStretch(1)
        layout.addLayout(choose_row)
        self.file_table = QTableWidget(0, 4, self)
        self.file_table.setObjectName("batchUploadFiles")
        self.file_table.setHorizontalHeaderLabels(
            ["规范路径", "大小", "修改时间快照", "SHA-256 摘要"]
        )
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.file_table, 1)
        self.count_label = QLabel("尚未选择文件。", self)
        self.count_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.count_label)
        self.precheck_progress = QProgressBar(self)
        self.precheck_progress.setObjectName("batchPrecheckProgress")
        self.precheck_progress.setRange(0, 1)
        self.precheck_progress.setValue(0)
        self.precheck_progress.setVisible(False)
        layout.addWidget(self.precheck_progress)
        form = QFormLayout()
        self.collection_edit = QLineEdit("default", self)
        self.collection_edit.setObjectName("batchUploadCollection")
        form.addRow("目标集合", self.collection_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.submit_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.submit_button.setText("加入摄取队列（0）")
        self.submit_button.setEnabled(False)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        cancel.setFocus()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def paths(self) -> list[Path]:
        return list(self._paths)

    @property
    def file_snapshots(self) -> list[dict[str, int | str]]:
        return [dict(item) for item in self._file_snapshots]

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._cancel_precheck()
        if not self._precheck.shutdown(2000):
            event.ignore()
            QMessageBox.warning(
                self,
                "预检仍在停止",
                "后台校验尚未安全停止，窗口不能关闭。请稍后再试。",
            )
            return
        super().closeEvent(event)

    def reject(self) -> None:
        self._cancel_precheck()
        if not self._precheck.shutdown(2000):
            QMessageBox.warning(
                self,
                "预检仍在停止",
                "后台校验尚未安全停止，窗口不能关闭。请稍后再试。",
            )
            return
        super().reject()

    def _choose(self) -> None:
        filenames, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "选择知识资料",
            "",
            "支持的文档 (*.txt *.md *.markdown *.rst *.json *.pdf);;所有文件 (*)",
        )
        if not filenames:
            return
        unique: list[Path] = []
        seen: set[str] = set()
        for filename in filenames:
            try:
                resolved = Path(filename).resolve(strict=True)
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    "文件不可读取",
                    f"无法为所选文件建立安全快照，本次没有加入队列：{exc}",
                )
                return
            key = str(resolved).casefold()
            if key not in seen:
                seen.add(key)
                unique.append(resolved)
        if len(unique) > MAX_BATCH_FILES:
            QMessageBox.warning(
                self,
                "文件过多",
                f"一次最多选择 {MAX_BATCH_FILES} 个文件；本次没有加入队列。",
            )
            return
        self._start_precheck(unique, confirming=False)

    def _start_precheck(self, paths: list[Path], *, confirming: bool) -> None:
        self._cancel_precheck()
        self._precheck_generation += 1
        generation = self._precheck_generation
        self._confirming = confirming
        self._precheck_busy = True
        self._pending_paths = list(paths)
        self._failed_prechecks = []
        if not confirming:
            self._paths = []
            self._file_snapshots = []
            self.file_table.setRowCount(len(paths))
            for row, path in enumerate(paths):
                self.file_table.setItem(row, 0, QTableWidgetItem(str(path)))
                self.file_table.setItem(row, 1, QTableWidgetItem("…"))
                self.file_table.setItem(row, 2, QTableWidgetItem("校验中"))
                self.file_table.setItem(row, 3, QTableWidgetItem("…"))
        self.submit_button.setEnabled(False)
        self.exclude_failed_button.setEnabled(False)
        self.cancel_precheck_button.setEnabled(True)
        self.precheck_progress.setVisible(True)
        self.precheck_progress.setRange(0, max(len(paths), 1))
        self.precheck_progress.setValue(0)
        verb = "确认" if confirming else "校验"
        self.count_label.setText(f"正在{verb}第 0/{len(paths)} 个文件…")
        self._precheck.start(paths, generation=generation)

    def _cancel_precheck(self) -> None:
        if self._precheck_busy:
            self._precheck.cancel()

    def _on_precheck_progress(self, payload: dict[str, Any]) -> None:
        self._apply_precheck_progress(payload)

    def _apply_precheck_progress(self, payload: dict[str, Any]) -> None:
        if int(payload.get("generation", -1)) != self._precheck_generation:
            return
        index = int(payload.get("index", 0))
        total = int(payload.get("total", 0))
        phase = str(payload.get("phase") or "")
        path = str(payload.get("path") or "")
        bytes_processed = int(payload.get("bytes_processed") or 0)
        verb = "确认" if self._confirming else "校验"
        if phase == "reading":
            self.count_label.setText(
                f"正在{verb}第 {index}/{total} 个文件：{Path(path).name}（已处理 {bytes_processed:,} 字节）"
            )
            self.precheck_progress.setValue(max(index - 1, 0))
        elif phase == "ready":
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, dict) and not self._confirming:
                self._render_ready_row(index - 1, Path(path), snapshot)
            self.precheck_progress.setValue(index)
        elif phase == "failed" and not self._confirming:
            error = str(payload.get("error") or "未知错误")
            row = index - 1
            if 0 <= row < self.file_table.rowCount():
                self.file_table.setItem(row, 1, QTableWidgetItem("失败"))
                self.file_table.setItem(row, 2, QTableWidgetItem(error))
                self.file_table.setItem(row, 3, QTableWidgetItem("—"))
            self.precheck_progress.setValue(index)
        elif phase == "cancelled":
            self.count_label.setText("预检已取消。未创建摄取队列，也未发送网络请求。")

    def _on_precheck_finished(self, result: PrecheckBatchResult) -> None:
        if int(result.generation) != self._precheck_generation:
            return
        self._precheck_busy = False
        self.cancel_precheck_button.setEnabled(False)
        self.precheck_progress.setVisible(False)
        if self._confirming:
            self._finish_confirm(result)
            return
        if result.cancelled:
            self._paths = []
            self._file_snapshots = []
            self._failed_prechecks = []
            self._pending_paths = []
            self.file_table.setRowCount(0)
            self.submit_button.setText("加入摄取队列（0）")
            self.submit_button.setEnabled(False)
            self.exclude_failed_button.setEnabled(False)
            self.count_label.setText("预检已取消。尚未选择可提交的文件。")
            return
        self._paths = list(result.ready_paths)
        self._file_snapshots = [dict(item) for item in result.ready_snapshots]
        self._failed_prechecks = [dict(item) for item in result.failed]
        if not self._paths and self._failed_prechecks:
            first_error = self._failed_prechecks[0].get("error") or "未知错误"
            QMessageBox.warning(
                self,
                "文件不可读取",
                f"无法为所选文件建立安全快照，本次没有加入队列：{first_error}",
            )
            self.count_label.setText("预检失败。请重新选择文件。")
            self.submit_button.setEnabled(False)
            self.exclude_failed_button.setEnabled(False)
            return
        total = sum(int(item["size"]) for item in self._file_snapshots)
        if self._failed_prechecks:
            self.count_label.setText(
                f"预检完成：{len(self._paths)} 个成功，{len(self._failed_prechecks)} 个失败，"
                f"共 {total:,} 字节。请排除失败项后再确认。"
            )
            self.submit_button.setEnabled(False)
            self.exclude_failed_button.setEnabled(True)
        else:
            self.count_label.setText(f"已选择 {len(self._paths)} 个文件，共 {total:,} 字节。")
            self.submit_button.setEnabled(bool(self._paths))
            self.exclude_failed_button.setEnabled(False)
        self.submit_button.setText(f"加入摄取队列（{len(self._paths)}）")

    def _finish_confirm(self, result: PrecheckBatchResult) -> None:
        self._confirming = False
        can_retry = bool(self._paths) and not self._failed_prechecks
        if result.cancelled:
            self.count_label.setText("确认已取消。文件尚未加入摄取队列。")
            self.submit_button.setEnabled(can_retry)
            return
        if result.failed:
            QMessageBox.warning(
                self,
                "文件已变化",
                "文件无法按确认快照读取。本次没有加入队列，可重试或重新选择。",
            )
            self.count_label.setText("确认失败，可重试或重新选择。")
            self.submit_button.setEnabled(can_retry)
            return
        current = [dict(item) for item in result.ready_snapshots]
        if current != self._file_snapshots:
            QMessageBox.warning(
                self,
                "文件已变化",
                "至少一个文件在预览后发生变化。本次没有加入队列，可重试或重新选择。",
            )
            self.count_label.setText("确认失败，可重试或重新选择。")
            self.submit_button.setEnabled(can_retry)
            return
        super().accept()

    def _exclude_failed_prechecks(self) -> None:
        if self._precheck_busy or not self._failed_prechecks:
            return
        failed_keys = {str(Path(item["path"]).resolve()).casefold() for item in self._failed_prechecks}
        kept_rows: list[tuple[Path, dict[str, int | str]]] = []
        for path, snapshot in zip(self._paths, self._file_snapshots, strict=True):
            if str(path).casefold() in failed_keys:
                continue
            kept_rows.append((path, snapshot))
        self._paths = [path for path, _snapshot in kept_rows]
        self._file_snapshots = [snapshot for _path, snapshot in kept_rows]
        self._failed_prechecks = []
        self.file_table.setRowCount(len(kept_rows))
        total = 0
        for row, (path, snapshot) in enumerate(kept_rows):
            total += int(snapshot["size"])
            self._render_ready_row(row, path, snapshot)
        self.count_label.setText(f"已选择 {len(self._paths)} 个文件，共 {total:,} 字节。")
        self.submit_button.setText(f"加入摄取队列（{len(self._paths)}）")
        self.submit_button.setEnabled(bool(self._paths))
        self.exclude_failed_button.setEnabled(False)

    def _render_ready_row(
        self,
        row: int,
        path: Path,
        snapshot: dict[str, int | str],
    ) -> None:
        if row < 0:
            return
        if row >= self.file_table.rowCount():
            self.file_table.setRowCount(row + 1)
        size = int(snapshot["size"])
        self.file_table.setItem(row, 0, QTableWidgetItem(str(path)))
        self.file_table.setItem(row, 1, QTableWidgetItem(f"{size:,} B"))
        modified = datetime.fromtimestamp(int(snapshot["mtime_ns"]) / 1_000_000_000).astimezone()
        self.file_table.setItem(
            row,
            2,
            QTableWidgetItem(f"{modified.isoformat(timespec='seconds')} (ns={snapshot['mtime_ns']})"),
        )
        digest = str(snapshot["sha256"])
        digest_item = QTableWidgetItem(f"{digest[:16]}…")
        digest_item.setToolTip(digest)
        self.file_table.setItem(row, 3, digest_item)

    def accept(self) -> None:
        if self._precheck_busy:
            QMessageBox.warning(self, "预检进行中", "请等待文件校验完成，或取消预检后重试。")
            return
        if not self._paths:
            QMessageBox.warning(self, "尚未选择文件", "请先选择至少一个知识资料文件。")
            return
        if self._failed_prechecks:
            QMessageBox.warning(
                self,
                "仍有失败项",
                "请先排除失败项，或重新选择文件。失败项不会被提交。",
            )
            return
        if not self.collection_edit.text().strip():
            QMessageBox.warning(self, "缺少集合", "请填写目标集合。")
            return
        self._start_precheck(list(self._paths), confirming=True)


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

    _TOOL_LABELS = {
        "ingest_text": "写入并索引文档",
        "archive_document": "归档文档",
        "query_knowledge": "知识检索（只读）",
    }
    _TASK_STATUS_LABELS = {
        "awaiting_approval": "待另一位审批人",
        "queued": "已批准，待执行",
        "running": "执行中",
        "succeeded": "执行成功",
        "rejected": "已拒绝",
        "cancelled": "已取消",
        "failed": "失败，需核查",
    }
    _APPROVAL_STATUS_LABELS = {
        "pending": "待决定",
        "approved": "已批准",
        "rejected": "已拒绝",
        "expired": "已过期",
    }
    _INGESTION_STATUS_LABELS = {
        "queued": "等待处理",
        "reading": "读取文件",
        "persisted": "已安全保存",
        "sending": "正在提交",
        "running": "正在处理",
        "cancelling": "取消请求中",
        "succeeded": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "outcome_unknown": "结果待确认",
        "cancel_rejected": "取消被拒，需核查",
        "reconciliation_required": "需人工对账",
        "unavailable": "恢复记录不可用",
        "pending": "待安全重试",
        "tracking": "等待状态确认",
        "abandoned": "已明确放弃",
    }

    def __init__(
        self,
        controller: KnowledgeAssistantDesktopController | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller or KnowledgeAssistantDesktopController()
        self._documents: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._citations: list[dict[str, Any]] = []
        self._approval_preview: dict[str, Any] | None = None
        self._approval_task_id = ""
        self._last_trace_id = ""
        self._upload_draft: dict[str, Any] | None = None
        self._ingest_task_draft: dict[str, Any] | None = None
        self._archive_task_draft: dict[str, Any] | None = None
        self._needs_refresh_on_show = False
        self._ingestion_generation = 0
        self._ingestion_items: dict[str, dict[str, Any]] = {}
        self._ingestion_recovery: dict[str, dict[str, Any]] = {}
        self._ingestion_coordinator = None
        self._ingestion_unavailable_reason = ""
        self._ingestion_views_active = True
        try:
            self._ingestion_coordinator = self.controller.create_ingestion_coordinator()
        except Exception:
            self._ingestion_unavailable_reason = (
                "可恢复摄取暂不可用：需要 Windows DPAPI 和稳定的服务实例标识。"
            )
        self.setObjectName("knowledgeAssistantDialog")
        self.setWindowTitle("Table Miku · Knowledge Assistant 管理台")
        self.resize(1180, 790)
        self.setMinimumSize(980, 680)
        self.setStyleSheet(CONSOLE_STYLE)

        root = QVBoxLayout(self)
        self.title_label = QLabel("Knowledge Assistant 2.3 · 可恢复知识摄取工作台", self)
        self.title_label.setObjectName("knowledgeAssistantTitle")
        self.title_label.setFont(QFont("Microsoft YaHei UI", 16, QFont.Weight.Bold))
        root.addWidget(self.title_label)
        notice = QLabel(
            f"连接：{self.controller.connection_label}。默认由桌面应用托管私有 loopback API，不需要启动 PowerShell。"
            "身份选择仅用于本地验收 RBAC，不是生产登录；生产仍必须使用可信身份网关。"
        )
        notice.setObjectName("localIdentityWarning")
        notice.setWordWrap(True)
        notice.setStyleSheet("background:#fff5d9;border:1px solid #e4c978;border-radius:6px;padding:8px;")
        root.addWidget(notice)
        self.identity_panel = self._build_identity_panel()
        self._active_principal = self.controller.principal(
            self.tenant_edit.text(),
            self.user_edit.text(),
            str(self.role_combo.currentData() or "viewer"),
            self.collections_edit.text(),
        )
        self._identity_dirty = False
        self.role_summary_label = QLabel(self)
        self.role_summary_label.setObjectName("activeRoleSummary")
        self.role_summary_label.setWordWrap(True)
        self.role_summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.role_summary_label.setStyleSheet(
            "background:#eaf7f9;border:1px solid #9dd4dd;border-radius:6px;padding:7px;"
        )
        root.addWidget(self.role_summary_label)
        self.identity_toggle = QPushButton("展开本地验收身份设置", self)
        self.identity_toggle.setObjectName("toggleLocalIdentity")
        self.identity_toggle.setCheckable(True)
        self.identity_toggle.toggled.connect(self._toggle_identity_panel)
        root.addWidget(self.identity_toggle)
        self.identity_panel.setVisible(False)
        root.addWidget(self.identity_panel)
        self._update_role_summary()

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("knowledgeAssistantTabs")
        self.tabs.addTab(self._build_documents_tab(), "文档")
        self.tabs.addTab(self._build_query_tab(), "RAG 查询")
        self.tabs.addTab(self._build_tasks_tab(), "任务与审批")
        self.tabs.addTab(self._build_observability_tab(), "观测")
        self.tabs.addTab(self._build_ingestion_tab(), "摄取中心")
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.setCurrentIndex(1)
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
        self._ingestion_timer = QTimer(self)
        self._ingestion_timer.setInterval(2000)
        self._ingestion_timer.timeout.connect(self._refresh_ingestion_jobs)
        if self._ingestion_coordinator is not None:
            self._ingestion_coordinator.updated.connect(self._on_ingestion_update)
            self._ingestion_coordinator.recovery_updated.connect(self._on_recovery_update)
            self._ingestion_coordinator.scan_recovery()
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
        self.upload_button = QPushButton("批量添加资料…", tab)
        self.upload_button.setObjectName("uploadDocument")
        self.upload_button.clicked.connect(self._choose_batch_upload)
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

    def _build_ingestion_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        intro = QLabel(
            "这里展示后台摄取和失败恢复。进度只显示真实阶段；“取消请求中”不等于已经取消。",
            tab,
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(intro)
        actions = QHBoxLayout()
        refresh = QPushButton("刷新", tab)
        refresh.clicked.connect(self._refresh_ingestion_jobs)
        self.ingestion_cancel_button = QPushButton("取消所选任务", tab)
        self.ingestion_cancel_button.setObjectName("cancelIngestion")
        self.ingestion_cancel_button.clicked.connect(self._cancel_selected_ingestion)
        self.ingestion_retry_button = QPushButton("安全重试原请求", tab)
        self.ingestion_retry_button.setObjectName("retryIngestion")
        self.ingestion_retry_button.clicked.connect(self._retry_selected_ingestion)
        self.ingestion_abandon_button = QPushButton("放弃恢复记录", tab)
        self.ingestion_abandon_button.setObjectName("abandonIngestionRecovery")
        self.ingestion_abandon_button.clicked.connect(self._abandon_selected_recovery)
        self.ingestion_filter = QComboBox(tab)
        self.ingestion_filter.addItem("全部", "all")
        self.ingestion_filter.addItem("进行中", "active")
        self.ingestion_filter.addItem("失败与待确认", "attention")
        self.ingestion_filter.currentIndexChanged.connect(self._render_ingestion_items)
        actions.addWidget(refresh)
        actions.addWidget(self.ingestion_cancel_button)
        actions.addWidget(self.ingestion_retry_button)
        actions.addWidget(self.ingestion_abandon_button)
        actions.addStretch(1)
        actions.addWidget(QLabel("筛选", tab))
        actions.addWidget(self.ingestion_filter)
        layout.addLayout(actions)
        self.ingestion_table = self._table(
            ["状态", "文件", "集合", "真实阶段/说明", "任务 ID"], tab
        )
        self.ingestion_table.setObjectName("ingestionTable")
        self.ingestion_table.itemSelectionChanged.connect(self._ingestion_selected)
        layout.addWidget(self.ingestion_table, 1)
        self.ingestion_progress = QProgressBar(tab)
        self.ingestion_progress.setObjectName("ingestionProgress")
        self.ingestion_progress.setRange(0, 1)
        self.ingestion_progress.setValue(0)
        self.ingestion_progress.setFormat("没有进行中的摄取")
        layout.addWidget(self.ingestion_progress)
        self.ingestion_detail = QPlainTextEdit(tab)
        self.ingestion_detail.setObjectName("ingestionDetail")
        self.ingestion_detail.setReadOnly(True)
        self.ingestion_detail.setMaximumHeight(150)
        self.ingestion_detail.setPlainText(
            self._ingestion_unavailable_reason or "选择一个摄取任务查看安全详情。"
        )
        layout.addWidget(self.ingestion_detail)
        return tab

    def _build_query_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        onboarding = QWidget(tab)
        onboarding.setObjectName("ragOnboarding")
        onboarding.setStyleSheet(
            "#ragOnboarding { background:#eef8ff;border:1px solid #b8d8ee;border-radius:7px; }"
        )
        onboarding_layout = QHBoxLayout(onboarding)
        self.onboarding_label = QLabel(
            "首次使用：1. 上传并索引资料（上传需 Editor）  "
            "2. 用 Viewer 提问  3. 选择引用核查证据",
            onboarding,
        )
        self.onboarding_label.setObjectName("ragOnboardingText")
        self.onboarding_label.setWordWrap(True)
        self.onboarding_label.setTextFormat(Qt.TextFormat.PlainText)
        onboarding_layout.addWidget(self.onboarding_label, 1)
        documents_button = QPushButton("前往文档", onboarding)
        documents_button.setObjectName("goToDocuments")
        documents_button.clicked.connect(self._go_to_documents)
        onboarding_layout.addWidget(documents_button)
        layout.addWidget(onboarding)
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
        self.answer_state.setTextFormat(Qt.TextFormat.PlainText)
        self.answer_state.setStyleSheet("font-weight:600;padding:5px;")
        layout.addWidget(self.answer_state)
        splitter = QSplitter(Qt.Orientation.Vertical, tab)
        self.answer_edit = SafeMarkdownBrowser(splitter)
        self.answer_edit.setObjectName("ragAnswer")
        self.answer_edit.setMinimumHeight(130)
        evidence_panel = QWidget(splitter)
        evidence_layout = QVBoxLayout(evidence_panel)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        evidence_hint = QLabel(
            "引用可核查：选择一行查看索引位置与纯文本证据；相关度仅用于排序，不代表事实正确率。",
            evidence_panel,
        )
        evidence_hint.setWordWrap(True)
        evidence_hint.setTextFormat(Qt.TextFormat.PlainText)
        evidence_layout.addWidget(evidence_hint)
        self.citation_table = self._table(
            ["引用", "文件", "集合", "位置", "得分", "证据摘录"], parent=evidence_panel
        )
        self.citation_table.setObjectName("citationTable")
        self.citation_table.setMinimumHeight(95)
        self.citation_table.itemSelectionChanged.connect(self._citation_selected)
        evidence_layout.addWidget(self.citation_table, 1)
        self.citation_detail = QPlainTextEdit(evidence_panel)
        self.citation_detail.setObjectName("citationDetail")
        self.citation_detail.setReadOnly(True)
        self.citation_detail.setMaximumHeight(100)
        self.citation_detail.setPlainText("暂无证据详情。完成查询后选择一条引用。")
        evidence_layout.addWidget(self.citation_detail)
        splitter.setSizes([250, 310])
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
        self.task_table.setMinimumHeight(135)
        self.task_table.itemSelectionChanged.connect(self._task_selected)
        left_layout.addWidget(self.task_table, 1)
        left_layout.addWidget(QLabel("任务概览与状态进度（非审批依据）"))
        self.task_detail = QPlainTextEdit(left)
        self.task_detail.setObjectName("taskSummary")
        self.task_detail.setReadOnly(True)
        self.task_detail.setMaximumHeight(195)
        left_layout.addWidget(self.task_detail)
        left_layout.addWidget(QLabel("操作收据"))
        self.receipt_detail = QPlainTextEdit(left)
        self.receipt_detail.setObjectName("operationReceipt")
        self.receipt_detail.setReadOnly(True)
        self.receipt_detail.setMaximumHeight(145)
        left_layout.addWidget(self.receipt_detail)
        self.task_technical_toggle = QPushButton("显示技术详情（JSON）", left)
        self.task_technical_toggle.setObjectName("toggleTaskTechnicalDetail")
        self.task_technical_toggle.setCheckable(True)
        self.task_technical_toggle.toggled.connect(self._toggle_task_technical_detail)
        left_layout.addWidget(self.task_technical_toggle)
        self.task_technical_detail = QPlainTextEdit(left)
        self.task_technical_detail.setObjectName("taskTechnicalDetail")
        self.task_technical_detail.setReadOnly(True)
        self.task_technical_detail.setMaximumHeight(165)
        self.task_technical_detail.setVisible(False)
        left_layout.addWidget(self.task_technical_detail)

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
        self.approval_hint_label = QLabel("请先选择一个任务。", approval)
        self.approval_hint_label.setObjectName("approvalActionHint")
        self.approval_hint_label.setWordWrap(True)
        self.approval_hint_label.setTextFormat(Qt.TextFormat.PlainText)
        self.approval_hint_label.setStyleSheet(
            "background:#eef3f8;border:1px solid #cbd6e4;border-radius:6px;padding:6px;"
        )
        approval_layout.addWidget(self.approval_hint_label)
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
        table.verticalHeader().setDefaultSectionSize(30)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _toggle_identity_panel(self, expanded: bool) -> None:
        self.identity_panel.setVisible(expanded)
        self.identity_toggle.setText(
            "收起本地验收身份设置" if expanded else "展开本地验收身份设置"
        )

    def _update_role_summary(self) -> None:
        if self._identity_dirty:
            self.role_summary_label.setText(
                "身份草稿尚未应用：受保护视图和业务操作已冻结。应用后才会切换权限。"
            )
            return
        principal = self._principal()
        if "admin" in principal.roles:
            role_name = "Admin"
            capability = "可读、上传、创建任务和审批；仍严格禁止审批自己请求的任务。"
        elif "approver" in principal.roles:
            role_name = "Approver"
            capability = "可读取并处理其他用户的待审批任务；不能审批自己的任务。"
        elif "editor" in principal.roles:
            role_name = "Editor"
            capability = "可上传资料和创建写任务；自己创建的任务必须由另一位 Approver 审批。"
        else:
            role_name = "Viewer"
            capability = "只读：可查询知识和核查引用，不能上传、创建写任务或审批。"
        collections = (
            "全部集合"
            if principal.collection_ids is None
            else "、".join(sorted(principal.collection_ids))
        )
        self.role_summary_label.setText(
            f"当前身份：{principal.user_id} · {role_name} · {collections}。{capability}"
        )

    def _go_to_documents(self) -> None:
        self.tabs.setCurrentIndex(0)
        if not self.upload_button.isEnabled():
            self._set_status(
                "当前身份没有上传权限。本地验收可展开身份设置，选择 Editor 后应用；"
                "生产环境应由可信登录身份授权。"
            )

    def _toggle_task_technical_detail(self, visible: bool) -> None:
        self.task_technical_detail.setVisible(visible)
        self.task_technical_toggle.setText(
            "隐藏技术详情（JSON）" if visible else "显示技术详情（JSON）"
        )

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
        self._ingestion_generation += 1
        self._clear_identity_scoped_views()
        self.tabs.setEnabled(False)
        self._update_role_summary()
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
        self._ingestion_generation += 1
        self._identity_dirty = False
        self.tabs.setEnabled(True)
        self._clear_identity_scoped_views()
        self._update_role_summary()
        self._set_status(
            f"已应用身份：tenant={principal.tenant_id}，user={principal.user_id}，"
            f"roles={','.join(sorted(principal.roles))}。"
        )
        self._update_action_permissions()
        self.refresh_all()
        if self._ingestion_coordinator is not None:
            self._ingestion_coordinator.scan_recovery()

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

    def _choose_batch_upload(self) -> None:
        coordinator = self._ingestion_coordinator
        if coordinator is None:
            self._show_error(
                "可恢复摄取不可用",
                OSError(self._ingestion_unavailable_reason or "本机安全存储不可用"),
            )
            return
        dialog = BatchUploadDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            local_ids = coordinator.submit_files(
                self._principal(),
                dialog.paths,
                collection_id=dialog.collection_edit.text().strip(),
                generation=self._ingestion_generation,
                expected_snapshots=dialog.file_snapshots,
            )
        except Exception as exc:
            self._show_error("无法加入摄取队列", exc)
            return
        self.tabs.setCurrentIndex(4)
        self._set_status(
            f"已将 {len(local_ids)} 个文件加入后台摄取队列；关闭管理台后任务仍会继续。"
        )

    def _refresh_ingestion_jobs(self, _checked: bool = False) -> None:
        del _checked
        if self._identity_dirty or self._ingestion_coordinator is None:
            return
        self._ingestion_coordinator.refresh(
            self._principal(), generation=self._ingestion_generation
        )

    def _on_ingestion_update(self, update: object) -> None:
        if not self._ingestion_views_active:
            return
        if not isinstance(update, dict):
            return
        if int(update.get("generation", -1)) != self._ingestion_generation:
            return
        if update.get("principal_signature") != self._principal_signature(self._principal()):
            return
        status = str(update.get("status") or "")
        if status == "snapshot":
            jobs = update.get("jobs") if isinstance(update.get("jobs"), list) else []
            server_items = {
                f"job:{job_id}": self._safe_ingestion_server_item(item, job_id)
                for item in jobs
                if isinstance(item, dict)
                and (job_id := str(item.get("id") or item.get("job_id") or "").strip())
            }
            server_job_ids = {
                str(value.get("job_id") or "") for value in server_items.values()
            }
            recovery_items: dict[str, dict[str, Any]] = {}
            for entry_id, record in self._ingestion_recovery.items():
                recovery_job_id = str(record.get("job_id") or "")
                server_key = f"job:{recovery_job_id}" if recovery_job_id else ""
                if server_key and server_key in server_items:
                    server_items[server_key]["entry_id"] = entry_id
                    server_items[server_key]["cancel_delivery_state"] = str(
                        record.get("cancel_delivery_state") or "none"
                    )
                else:
                    recovery_items[entry_id] = dict(record)
            local_attention = {
                key: value
                for key, value in self._ingestion_items.items()
                if (
                    (
                        str(value.get("status"))
                        in {
                            "outcome_unknown",
                            "cancel_rejected",
                            "reconciliation_required",
                            "unavailable",
                            "pending",
                            "tracking",
                        }
                        and str(value.get("job_id") or "") not in server_job_ids
                    )
                    or (
                        key.startswith("local-")
                        and str(value.get("job_id") or "") not in server_job_ids
                    )
                )
            }
            self._ingestion_items = {
                **recovery_items,
                **local_attention,
                **server_items,
            }
        elif status == "poll_failed":
            self._set_status(str(update.get("message") or "摄取任务刷新失败"), error=True)
            return
        else:
            key_value = update.get("local_id") or update.get("entry_id")
            if not key_value and update.get("job_id"):
                key_value = f"job:{update['job_id']}"
            if not key_value:
                return
            key = str(key_value)
            existing = self._ingestion_items.get(key, {})
            merged = {**existing, **update}
            if merged.get("job_id") and not merged.get("requested_by"):
                merged["requested_by"] = self._principal().user_id
            if status == "abandoned":
                self._ingestion_items.pop(key, None)
                entry_id = str(update.get("entry_id") or "")
                self._ingestion_recovery.pop(entry_id, None)
            else:
                self._ingestion_items[key] = merged
                if key in self._ingestion_recovery or key.startswith("outbox-"):
                    self._ingestion_recovery[key] = dict(merged)
        self._render_ingestion_items()

    @classmethod
    def _safe_ingestion_server_item(
        cls,
        item: dict[str, Any],
        job_id: str,
    ) -> dict[str, Any]:
        progress_value = item.get("progress")
        progress = dict(progress_value) if isinstance(progress_value, dict) else {}
        safe = {
            "job_id": job_id,
            "requested_by": str(item.get("requested_by") or ""),
            "filename": str(item.get("filename") or ""),
            "collection_id": str(item.get("collection_id") or ""),
            "status": str(item.get("status") or "queued"),
            "progress": {
                "phase": str(progress.get("phase") or ""),
                "current": max(0, int(progress.get("current") or 0)),
                "total": max(0, int(progress.get("total") or 0)),
            },
            "error_code": str(item.get("error_code") or ""),
            "error_message": str(item.get("error_message") or ""),
            "attempt_count": max(0, int(item.get("attempt_count") or 0)),
            "max_attempts": max(0, int(item.get("max_attempts") or 0)),
            "trace_id": str(item.get("trace_id") or ""),
            "document_id": str(item.get("document_id") or ""),
            "cancel_outcome": str(item.get("cancel_outcome") or ""),
            "cancel_requested_at": str(item.get("cancel_requested_at") or ""),
        }
        safe["message"] = cls._ingestion_server_message(safe)
        return safe

    @staticmethod
    def _ingestion_server_message(item: dict[str, Any]) -> str:
        status = str(item.get("status") or "")
        if status == "succeeded" and item.get("cancel_outcome") == "too_late":
            return "取消过晚，文档已写入。"
        error_message = str(item.get("error_message") or "").strip()
        if status == "failed" and error_message:
            return f"摄取失败：{error_message}"
        return {
            "queued": "服务端已接收，等待处理。",
            "running": "服务端正在处理。",
            "cancelling": "取消请求中；尚不能断言任务已取消。",
            "succeeded": "摄取完成。",
            "failed": "摄取失败；核查原因后请重新选择文件创建新任务。",
            "cancelled": "服务端已确认取消。",
            "cancel_rejected": "服务端已明确拒绝取消；请核查权限与任务状态。",
            "reconciliation_required": "服务端未返回该跟踪任务；本地记录已保留。",
        }.get(status, "任务状态已更新。")

    def _on_recovery_update(self, records: object) -> None:
        if not self._ingestion_views_active:
            return
        if not isinstance(records, list):
            return
        active = self._principal_signature(self._principal())
        filtered: dict[str, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            entry_id = str(record.get("entry_id") or "")
            if not entry_id:
                continue
            if record.get("status") == "unavailable":
                filtered[entry_id] = record
                continue
            principal = record.get("principal")
            if not isinstance(principal, dict):
                continue
            signature = (
                str(principal.get("tenant_id") or ""),
                str(principal.get("user_id") or ""),
                tuple(sorted(principal.get("roles") or [])),
                (
                    tuple(sorted(principal.get("collection_ids") or []))
                    if principal.get("collection_ids") is not None
                    else None
                ),
            )
            if signature == active:
                safe_record = dict(record)
                safe_record["outbox_state"] = str(record.get("status") or "")
                delivery_state = str(record.get("cancel_delivery_state") or "none")
                if delivery_state == "rejected":
                    safe_record["status"] = "cancel_rejected"
                    safe_record["message"] = (
                        "上次取消请求被明确拒绝；记录已保留，请核查后再决定。"
                    )
                elif delivery_state == "unknown":
                    safe_record["status"] = "outcome_unknown"
                    safe_record["message"] = "取消结果待确认；不会自动重新发送写操作。"
                filtered[entry_id] = safe_record
        self._ingestion_recovery = filtered
        for entry_id in tuple(self._ingestion_items):
            if entry_id.startswith("outbox-"):
                self._ingestion_items.pop(entry_id, None)
        for entry_id, record in filtered.items():
            self._ingestion_items[entry_id] = dict(record)
        self._render_ingestion_items()

    def _render_ingestion_items(self, *_args) -> None:
        if not hasattr(self, "ingestion_table"):
            return
        filter_name = str(self.ingestion_filter.currentData() or "all")
        terminal = {"succeeded", "failed", "cancelled", "abandoned"}
        attention = {
            "failed",
            "outcome_unknown",
            "cancel_rejected",
            "reconciliation_required",
            "unavailable",
            "pending",
            "tracking",
        }
        visible: list[tuple[str, dict[str, Any]]] = []
        for key, item in self._ingestion_items.items():
            status = str(item.get("status") or "")
            if filter_name == "active" and status in terminal | attention:
                continue
            if filter_name == "attention" and status not in attention:
                continue
            visible.append((key, item))
        selected = self._selected_ingestion()
        selected_key = selected[0] if selected is not None else ""
        self.ingestion_table.setRowCount(len(visible))
        for row, (key, item) in enumerate(visible):
            status = str(item.get("status") or "")
            values = (
                self._INGESTION_STATUS_LABELS.get(status, status),
                item.get("filename", ""),
                item.get("collection_id", ""),
                item.get("message", self._ingestion_server_message(item)),
                item.get("job_id", ""),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, key)
                self.ingestion_table.setItem(row, column, cell)
        self._restore_selection(self.ingestion_table, selected_key)
        self._ingestion_selected()

    def _selected_ingestion(self) -> tuple[str, dict[str, Any]] | None:
        row = self.ingestion_table.currentRow()
        if row < 0:
            return None
        cell = self.ingestion_table.item(row, 0)
        key = str(cell.data(Qt.ItemDataRole.UserRole) or "") if cell is not None else ""
        item = self._ingestion_items.get(key)
        return (key, item) if item is not None else None

    def _ingestion_selected(self) -> None:
        selected = self._selected_ingestion()
        if selected is None:
            self.ingestion_detail.setPlainText("选择一个摄取任务查看安全详情。")
            self.ingestion_progress.setRange(0, 1)
            self.ingestion_progress.setValue(0)
            self.ingestion_progress.setFormat("选择任务查看真实阶段")
            for button in (
                self.ingestion_cancel_button,
                self.ingestion_retry_button,
                self.ingestion_abandon_button,
            ):
                button.setEnabled(False)
            return
        key, item = selected
        status = str(item.get("status") or "")
        progress_value = item.get("progress")
        progress = progress_value if isinstance(progress_value, dict) else {}
        phase = str(progress.get("phase") or status or "unknown")
        current = max(0, int(progress.get("current") or 0))
        total = max(0, int(progress.get("total") or 0))
        if total:
            self.ingestion_progress.setRange(0, total)
            self.ingestion_progress.setValue(min(current, total))
            self.ingestion_progress.setFormat(f"当前任务：{phase} · {current}/{total}")
        else:
            self.ingestion_progress.setRange(0, 1)
            self.ingestion_progress.setValue(1 if status in {"succeeded", "failed", "cancelled"} else 0)
            self.ingestion_progress.setFormat(f"当前任务阶段：{phase}")
        cancel_receipt = (
            "取消过晚，文档已写入。"
            if status == "succeeded" and item.get("cancel_outcome") == "too_late"
            else str(item.get("cancel_outcome") or "—")
        )
        delivery_label = {
            "none": "未请求",
            "requested": "已持久化，等待投递",
            "delivered": "已投递，等待服务端终态",
            "unknown": "投递结果待确认",
            "rejected": "服务端明确拒绝",
        }.get(str(item.get("cancel_delivery_state") or "none"), "未知")
        self.ingestion_detail.setPlainText(
            "\n".join(
                (
                    f"状态：{self._INGESTION_STATUS_LABELS.get(status, status)}",
                    f"文件：{item.get('filename') or '—'}",
                    f"集合：{item.get('collection_id') or '—'}",
                    f"请求人：{item.get('requested_by') or '—'}",
                    f"任务 ID：{item.get('job_id') or '—'}",
                    f"恢复记录：{item.get('entry_id') or '—'}",
                    f"阶段：{phase}",
                    f"进度：{current}/{total}" if total else "进度：未提供计数",
                    (
                        f"尝试：{item.get('attempt_count') or 0}/"
                        f"{item.get('max_attempts') or 0}"
                    ),
                    f"错误代码：{item.get('error_code') or '—'}",
                    f"错误说明：{item.get('error_message') or '—'}",
                    f"Trace：{item.get('trace_id') or '—'}",
                    f"文档 ID：{item.get('document_id') or '—'}",
                    f"取消结果：{cancel_receipt}",
                    f"取消投递：{delivery_label}",
                    f"说明：{item.get('message') or self._ingestion_server_message(item)}",
                )
            )
        )
        principal = self._principal()
        can_write = "knowledge:write" in principal.permissions
        job_id = str(item.get("job_id") or "")
        requester = str(item.get("requested_by") or "")
        local_or_recovery = not job_id and (
            key.startswith("local-") or key.startswith("outbox-")
        )
        active = status in {
            "queued",
            "reading",
            "persisted",
            "sending",
            "running",
            "outcome_unknown",
            "pending",
            "cancel_rejected",
        }
        owns_request = local_or_recovery or requester == principal.user_id
        can_cancel = can_write and active and owns_request and (local_or_recovery or bool(requester))
        self.ingestion_cancel_button.setEnabled(can_cancel)
        if not can_write:
            cancel_reason = "当前身份没有 knowledge:write 写入权限，不能取消摄取。"
        elif job_id and not requester:
            cancel_reason = "任务没有可核验的发起人信息，已安全禁用取消。"
        elif job_id and requester != principal.user_id:
            cancel_reason = f"只有任务发起人 {requester} 可以取消该摄取任务。"
        elif str(item.get("cancel_delivery_state") or "") == "rejected":
            cancel_reason = "上次取消被明确拒绝；核查后可再次明确请求一次取消。"
        elif not active:
            cancel_reason = "任务不再处于可请求取消的状态。"
        else:
            cancel_reason = "查看精确取消预览后，可请求取消当前任务。"
        self.ingestion_cancel_button.setToolTip(cancel_reason)
        is_pending_request = str(item.get("outbox_state") or "pending") == "pending"
        can_recover = (
            can_write
            and status in {"outcome_unknown", "pending"}
            and is_pending_request
            and not job_id
        )
        self.ingestion_retry_button.setEnabled(can_recover)
        if str(item.get("outbox_state") or "") == "tracking":
            retry_reason = "该记录已绑定服务端任务；请刷新状态或明确再次请求取消，不能重传原请求。"
        elif can_recover:
            retry_reason = "使用同一冻结内容和同一幂等键重试；不会创建新的请求意图。"
        else:
            retry_reason = "当前记录不允许重试原请求。"
        self.ingestion_retry_button.setToolTip(retry_reason)
        can_abandon = can_recover and bool(item.get("entry_id") or key)
        self.ingestion_abandon_button.setEnabled(can_abandon)
        if not can_write:
            abandon_reason = "当前身份没有 knowledge:write 写入权限，不能放弃恢复记录。"
        elif str(item.get("outbox_state") or "") == "tracking":
            abandon_reason = "该记录已绑定服务端任务；请刷新状态或再次请求取消，不能当作未提交请求放弃。"
        elif can_abandon:
            abandon_reason = "明确删除本地恢复记录；不会自动重试，也不会撤销服务端可能已完成的写入。"
        else:
            abandon_reason = "只有尚未绑定服务端任务的待确认恢复记录可以明确放弃。"
        self.ingestion_abandon_button.setToolTip(abandon_reason)

    def _cancel_selected_ingestion(self) -> None:
        selected = self._selected_ingestion()
        if selected is None or self._ingestion_coordinator is None:
            return
        key, item = selected
        if not self._confirm_ingestion_cancel(item):
            return
        job_id = str(item.get("job_id") or "")
        if job_id:
            item["status"] = "cancelling"
            item["message"] = "取消请求中；尚不能断言任务已取消。"
            self._ingestion_coordinator.request_cancel_job(
                self._principal(), job_id, generation=self._ingestion_generation
            )
        elif str(item.get("status") or "") in {"outcome_unknown", "pending"}:
            entry_id = str(item.get("entry_id") or (key if key.startswith("outbox-") else ""))
            if not entry_id:
                return
            item["status"] = "cancelling"
            item["message"] = "取消意图已保留，结果仍待确认。"
            self._ingestion_coordinator.request_cancel_recovery(
                self._principal(), entry_id, generation=self._ingestion_generation
            )
        else:
            status = self._ingestion_coordinator.request_cancel_local(key)
            item["status"] = status
            item["message"] = (
                "已在发送前取消。"
                if status == "cancelled"
                else "取消请求中；尚不能断言任务已取消。"
            )
        self._render_ingestion_items()

    def _confirm_ingestion_cancel(self, item: dict[str, Any]) -> bool:
        principal = self._principal()
        job_id = str(item.get("job_id") or "")
        status = str(item.get("status") or "")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle("核对取消摄取请求")
        box.setText(
            "\n".join(
                (
                    f"任务：{job_id or item.get('entry_id') or '本地待发送任务'}",
                    f"文件：{item.get('filename') or '—'}",
                    f"集合：{item.get('collection_id') or '—'}",
                    f"当前身份：{principal.user_id}",
                    f"当前状态：{self._INGESTION_STATUS_LABELS.get(status, status)}",
                )
            )
        )
        if status in {"outcome_unknown", "pending"} and not job_id:
            consequence = (
                "将持久化取消意图并继续核对原请求；不会宣称任务已经取消，也不会创建新请求。"
            )
        elif job_id:
            consequence = (
                "将请求服务端停止任务。任务可能在请求到达前完成；只有服务端确认后才算取消成功。"
            )
        else:
            consequence = (
                "若请求尚未发送，将停止并清理本地记录；若已经发送，只能请求服务端取消。"
            )
        box.setInformativeText(consequence)
        cancel_action = box.addButton("请求取消", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("保留任务", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is cancel_action

    def _retry_selected_ingestion(self) -> None:
        selected = self._selected_ingestion()
        if selected is None or self._ingestion_coordinator is None:
            return
        key, item = selected
        entry_id = str(item.get("entry_id") or (key if key.startswith("outbox-") else ""))
        box = QMessageBox(self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle("确认安全重试")
        if not entry_id:
            return
        box.setText("使用已加密保留的原内容和原幂等键重试？")
        box.setInformativeText("不会重新读取原文件，也不会创建新意图。")
        retry = box.addButton("确认重试", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("暂不处理", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is not retry:
            return
        self._ingestion_coordinator.safe_replay(
            self._principal(), entry_id, generation=self._ingestion_generation
        )

    def _abandon_selected_recovery(self) -> None:
        selected = self._selected_ingestion()
        if selected is None or self._ingestion_coordinator is None:
            return
        key, item = selected
        entry_id = str(item.get("entry_id") or (key if key.startswith("outbox-") else ""))
        if not entry_id:
            return
        box = QMessageBox(self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle("保留还是放弃恢复记录")
        box.setText("明确放弃该结果待确认的请求？")
        box.setInformativeText(
            "放弃不会撤回可能已经发生的服务端写入；以后使用新请求可能产生重复结果。"
        )
        abandon = box.addButton("明确放弃", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("保留待处理", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        if box.clickedButton() is abandon:
            self._ingestion_coordinator.abandon_recovery(
                self._principal(), entry_id, generation=self._ingestion_generation
            )

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

    def _clear_citations(self) -> None:
        self._citations = []
        self.citation_table.clearSelection()
        self.citation_table.setRowCount(0)
        self.citation_detail.setPlainText("暂无证据详情。完成查询后选择一条引用。")

    def _clear_query_result(self, *, state: str, message: str = "") -> None:
        self.answer_state.setText(state)
        if message:
            self.answer_edit.setPlainText(message)
        else:
            self.answer_edit.clear()
        self._clear_citations()
        self._last_trace_id = ""
        self.trace_id_edit.clear()
        self.trace_detail.clear()

    def _citation_selected(self) -> None:
        row = self.citation_table.currentRow()
        if row < 0 or row >= len(self._citations):
            self.citation_detail.setPlainText("暂无证据详情。完成查询后选择一条引用。")
            return
        citation = self._citations[row]
        page = citation.get("page_number")
        heading = self._summary_value(citation.get("heading"))
        position = heading
        if page is not None:
            page_text = f"第 {self._summary_value(page)} 页"
            position = f"{heading} / {page_text}" if heading != "—" else page_text
        excerpt = SafeMarkdownBrowser.markdown_to_plain_text(
            str(citation.get("excerpt") or "")
        )
        self.citation_detail.setPlainText(
            "证据详情（纯文本，不作为操作指令）\n"
            f"引用：{self._summary_value(citation.get('id'))}\n"
            f"文件：{self._summary_value(citation.get('filename'))}\n"
            f"集合：{self._summary_value(citation.get('collection_id'))}\n"
            f"位置：{position}\n"
            f"相关度：{self._summary_value(citation.get('score'))}（仅用于排序）\n"
            f"文档 ID：{self._summary_value(citation.get('document_id'))}\n"
            f"Chunk ID：{self._summary_value(citation.get('chunk_id'))}\n\n"
            f"索引原文摘录：\n{excerpt or '—'}"
        )

    def _run_query(self) -> None:
        query = self.query_edit.toPlainText().strip()
        if not query:
            self._show_error("查询为空", ValueError("请输入至少 2 个字符的问题"))
            return
        self._clear_query_result(state="正在检索当前知识库…")
        try:
            with self._busy():
                result = self.controller.query(
                    self._principal(),
                    query=query,
                    collection_ids=self.query_collections_edit.text(),
                    top_k=self.top_k_spin.value(),
                )
        except Exception as exc:
            self._clear_query_result(
                state="查询失败：旧答案与引用已清除。",
                message="本次查询未完成。为避免混淆，上一次答案与证据已清除。",
            )
            self._show_error("RAG 查询失败", exc)
            return
        self.answer_edit.set_safe_markdown(str(result.get("answer") or ""))
        raw_citations = result.get("citations") if isinstance(result.get("citations"), list) else []
        citations = [dict(item) for item in raw_citations if isinstance(item, dict)]
        self._citations = citations
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
                SafeMarkdownBrowser.markdown_to_plain_text(
                    str(citation.get("excerpt") or "")
                ),
            )
            for column, value in enumerate(values):
                self.citation_table.setItem(row, column, QTableWidgetItem(str(value)))
        if citations:
            self.citation_table.selectRow(0)
            self.citation_table.setCurrentCell(0, 0)
        else:
            self._citation_selected()
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
            self._clear_task_views(
                "任务列表不可用。旧任务概览已清除，请检查连接后重试。",
                "没有可核查的操作收据。",
            )
            self._update_action_permissions()
            if show_error:
                self._show_error("无法读取任务", exc)
            else:
                self._set_status(f"任务读取失败：{exc}", error=True)
            return
        self._tasks = {str(item["id"]): item for item in tasks}
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            approval = task.get("approval") if isinstance(task.get("approval"), dict) else {}
            task_status = str(task.get("status") or "")
            tool_name = str(task.get("tool_name") or "")
            approval_status = str(approval.get("status") or "")
            values = (
                self._TASK_STATUS_LABELS.get(task_status, task_status),
                self._TOOL_LABELS.get(tool_name, tool_name),
                task.get("requested_by", ""),
                self._APPROVAL_STATUS_LABELS.get(approval_status, approval_status or "—"),
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

    @staticmethod
    def _summary_value(value: object) -> str:
        if value is None or value == "":
            return "—"
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        else:
            text = str(value)
        text = " ".join(text.replace("\x00", "").splitlines()).strip()
        return text[:500] + ("…" if len(text) > 500 else "") if text else "—"

    @classmethod
    def _safe_task_metadata(cls, task: dict[str, Any]) -> dict[str, Any]:
        safe_fields = (
            "id",
            "tenant_id",
            "requested_by",
            "tool_name",
            "idempotency_key",
            "status",
            "error_code",
            "error_message",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
            "arguments_integrity",
        )
        safe: dict[str, Any] = {
            key: task.get(key) for key in safe_fields if key in task
        }
        tool_name = str(task.get("tool_name") or "")
        arguments = task.get("arguments") if isinstance(task.get("arguments"), dict) else {}
        allowed_arguments = {
            "ingest_text": ("filename", "collection_id", "content_sha256", "byte_size"),
            "archive_document": (
                "document_id",
                "filename",
                "collection_id",
                "checksum",
            ),
            "query_knowledge": ("query", "collection_ids", "top_k"),
        }.get(tool_name, ())
        safe["arguments"] = {
            key: arguments.get(key) for key in allowed_arguments if key in arguments
        }
        approval = task.get("approval") if isinstance(task.get("approval"), dict) else None
        if approval is not None:
            allowed_approval = (
                "id",
                "status",
                "requested_by",
                "decided_by",
                "requested_at",
                "decided_at",
                "expires_at",
                "reason",
            )
            safe["approval"] = {
                key: approval.get(key) for key in allowed_approval if key in approval
            }
        result = task.get("result") if isinstance(task.get("result"), dict) else None
        if result is not None:
            safe["result"] = cls._allowlisted_result(tool_name, result)
        receipt = task.get("receipt") if isinstance(task.get("receipt"), dict) else None
        if receipt is not None:
            safe["receipt"] = cls._safe_receipt_metadata(tool_name, receipt)
        return safe

    @staticmethod
    def _allowlisted_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        allowed_results = {
            "ingest_text": (
                "id",
                "document_id",
                "filename",
                "collection_id",
                "status",
                "chunk_count",
                "deduplicated",
            ),
            "archive_document": (
                "id",
                "document_id",
                "filename",
                "collection_id",
                "status",
                "archived",
            ),
            "query_knowledge": ("refused", "trace_id"),
        }.get(tool_name, ())
        return {key: result.get(key) for key in allowed_results if key in result}

    @classmethod
    def _safe_receipt_metadata(
        cls, tool_name: str, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        allowed_receipt = (
            "operation_id",
            "tool_name",
            "approved_by",
            "completed_at",
            "approved_preview_hash",
        )
        safe = {key: receipt.get(key) for key in allowed_receipt if key in receipt}
        arguments = receipt.get("arguments") if isinstance(receipt.get("arguments"), dict) else {}
        allowed_arguments = {
            "ingest_text": ("filename", "collection_id", "content_sha256", "byte_size"),
            "archive_document": (
                "document_id",
                "filename",
                "collection_id",
                "checksum",
            ),
            "query_knowledge": ("query", "collection_ids", "top_k"),
        }.get(tool_name, ())
        safe["arguments"] = {
            key: arguments.get(key) for key in allowed_arguments if key in arguments
        }
        result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
        safe["result"] = cls._allowlisted_result(tool_name, result)
        return safe

    @classmethod
    def _task_target_lines(cls, task: dict[str, Any]) -> list[str]:
        raw_arguments = task.get("arguments")
        tool_name = str(task.get("tool_name") or "")
        arguments = cls._safe_task_metadata(task).get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        if tool_name == "ingest_text":
            return [
                f"- 文件：{cls._summary_value(arguments.get('filename'))}",
                f"- 集合：{cls._summary_value(arguments.get('collection_id'))}",
                f"- 正文大小：{cls._summary_value(arguments.get('byte_size'))} 字节",
                f"- 内容 SHA-256：{cls._summary_value(arguments.get('content_sha256'))}",
            ]
        if tool_name == "archive_document":
            return [
                f"- 文件：{cls._summary_value(arguments.get('filename'))}",
                f"- 集合：{cls._summary_value(arguments.get('collection_id'))}",
                f"- 文档 ID：{cls._summary_value(arguments.get('document_id'))}",
                f"- 校验值：{cls._summary_value(arguments.get('checksum'))}",
            ]
        if tool_name == "query_knowledge":
            return [
                f"- 查询：{cls._summary_value(arguments.get('query'))}",
                f"- 集合：{cls._summary_value(arguments.get('collection_ids'))}",
                f"- Top K：{cls._summary_value(arguments.get('top_k'))}",
            ]
        if not arguments:
            return ["- 当前任务没有可显示的安全参数。"]
        return [
            f"- {cls._summary_value(key)}：{cls._summary_value(item)}"
            for key, item in sorted(arguments.items())
        ]

    @classmethod
    def format_task_summary(cls, task: dict[str, Any]) -> str:
        tool_name = str(task.get("tool_name") or "")
        status = str(task.get("status") or "unknown")
        approval = task.get("approval") if isinstance(task.get("approval"), dict) else {}
        requested_at = approval.get("requested_at") or task.get("created_at")
        lines = [
            f"动作：{cls._TOOL_LABELS.get(tool_name, cls._summary_value(tool_name))}",
            f"状态：{cls._TASK_STATUS_LABELS.get(status, cls._summary_value(status))}",
            f"请求人：{cls._summary_value(task.get('requested_by'))}",
            f"创建时间：{cls._summary_value(task.get('created_at'))}",
            f"最近更新：{cls._summary_value(task.get('updated_at'))}",
            "",
            "目标与安全参数：",
            *cls._task_target_lines(task),
            "",
            "状态进度：",
            f"[完成] 请求已提交：{cls._summary_value(requested_at)}",
        ]
        if status == "awaiting_approval":
            lines.extend(
                (
                    "[当前] 等待另一位审批人核对精确 Action Preview 后决定。",
                    f"[期限] {cls._summary_value(approval.get('expires_at'))}",
                    "[未开始] 写操作尚未执行。",
                )
            )
        elif status == "queued":
            if approval:
                lines.append(
                    f"[完成] 审批人 {cls._summary_value(approval.get('decided_by'))} 已批准。"
                )
            else:
                lines.append("[完成] 只读任务，无需审批。")
            lines.append("[当前] 已进入执行队列。")
        elif status == "running":
            if approval:
                lines.append(
                    f"[完成] 审批人 {cls._summary_value(approval.get('decided_by'))} 已批准。"
                )
            else:
                lines.append("[完成] 只读任务，无需审批。")
            lines.append(f"[当前] 正在执行：{cls._summary_value(task.get('started_at'))}")
        elif status == "succeeded":
            if approval:
                lines.append(
                    f"[完成] 审批人：{cls._summary_value(approval.get('decided_by'))}"
                )
            else:
                lines.append("[完成] 只读任务，无需审批。")
            lines.append(f"[完成] 已成功执行：{cls._summary_value(task.get('finished_at'))}")
        elif status == "rejected":
            lines.extend(
                (
                    f"[终止] 审批人 {cls._summary_value(approval.get('decided_by'))} 已拒绝。",
                    f"[原因] {cls._summary_value(approval.get('reason'))}",
                    "[安全结果] 写操作未执行。",
                )
            )
        elif status == "cancelled":
            lines.extend(
                (
                    f"[终止] 任务已取消：{cls._summary_value(task.get('error_message'))}",
                    "[提示] 请刷新任务并核对取消原因后再决定是否创建新请求。",
                )
            )
        elif status == "failed":
            lines.extend(
                (
                    f"[失败] {cls._summary_value(task.get('error_code'))}："
                    f"{cls._summary_value(task.get('error_message'))}",
                    "[警告] 失败可能伴随局部副作用；请核查文档、收据与 Trace 后再重试。",
                )
            )
        else:
            lines.append("[提示] 当前状态不能确定执行结果，请核查技术详情与 Trace。")
        lines.extend(
            (
                "",
                "审批提示：此摘要仅用于浏览；批准依据是右侧加载的精确 Action Preview。",
            )
        )
        return "\n".join(lines)

    @classmethod
    def format_receipt_summary(cls, task: dict[str, Any]) -> str:
        receipt = task.get("receipt") if isinstance(task.get("receipt"), dict) else None
        status = str(task.get("status") or "")
        if receipt is None:
            if status == "awaiting_approval":
                return "尚未执行，因此还没有操作收据。请等待另一位审批人处理。"
            if status == "rejected":
                return "任务已拒绝；写操作未执行，因此没有执行收据。"
            if status == "failed":
                return "没有成功操作收据。失败可能存在局部副作用，请核查文档状态和 Trace。"
            if status == "cancelled":
                return "任务已取消或审批已过期，没有成功操作收据。"
            return "尚未产生操作收据。"
        safe_receipt = cls._safe_receipt_metadata(
            str(task.get("tool_name") or ""), receipt
        )
        lines = [
            "成功操作收据",
            f"operation_id（操作 ID）：{cls._summary_value(safe_receipt.get('operation_id'))}",
            f"动作：{cls._summary_value(safe_receipt.get('tool_name'))}",
            f"审批人：{cls._summary_value(safe_receipt.get('approved_by'))}",
            f"完成时间：{cls._summary_value(safe_receipt.get('completed_at'))}",
            f"绑定预览哈希：{cls._summary_value(safe_receipt.get('approved_preview_hash'))}",
        ]
        for label, key in (("安全参数", "arguments"), ("执行结果", "result")):
            value = safe_receipt.get(key)
            if isinstance(value, dict):
                lines.append(f"{label}：")
                lines.extend(
                    f"- {cls._summary_value(item_key)}：{cls._summary_value(item_value)}"
                    for item_key, item_value in sorted(value.items())
                )
        if str(task.get("tool_name") or "") == "archive_document":
            lines.append("恢复说明：当前没有自助恢复接口；如需恢复，请由管理员核查并处理。")
        else:
            lines.append("恢复说明：如需降低新文档的检索可见性，可另建归档审批任务。")
        return "\n".join(lines)

    def _task_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            self.task_detail.setPlainText("请选择一个任务。")
            self.receipt_detail.setPlainText("没有收据。")
            self.task_technical_detail.clear()
            self.task_technical_toggle.setChecked(False)
            self.task_technical_toggle.setEnabled(False)
            self._clear_approval_preview()
            self._update_action_permissions()
            return
        safe_task = self._safe_task_metadata(task)
        self.task_detail.setPlainText(self.format_task_summary(task))
        self.receipt_detail.setPlainText(self.format_receipt_summary(task))
        self.task_technical_detail.setPlainText(_json_text(safe_task))
        self.task_technical_toggle.setEnabled(True)
        self.task_technical_toggle.setChecked(False)
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
            self._update_action_permissions()
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

    def _clear_task_views(self, task_message: str, receipt_message: str) -> None:
        self._clear_approval_preview()
        self._tasks = {}
        self.task_table.clearSelection()
        self.task_table.setRowCount(0)
        self.task_detail.setPlainText(task_message)
        self.receipt_detail.setPlainText(receipt_message)
        self.task_technical_detail.clear()
        self.task_technical_toggle.setChecked(False)
        self.task_technical_toggle.setEnabled(False)

    def _clear_identity_scoped_views(self) -> None:
        self._clear_approval_preview()
        self._documents = {}
        self.document_table.setRowCount(0)
        self.document_detail.clear()
        self._clear_task_views("", "")
        self.query_edit.clear()
        self.query_collections_edit.clear()
        self.answer_state.setText("尚未查询")
        self.answer_edit.clear()
        self._clear_citations()
        self._last_trace_id = ""
        self.trace_id_edit.clear()
        self.trace_detail.clear()
        self.reject_reason_edit.clear()
        self._clear_metrics("身份已改变，等待刷新")
        self._ingestion_items = {}
        self._ingestion_recovery = {}
        if hasattr(self, "ingestion_table"):
            self.ingestion_table.setRowCount(0)
            self.ingestion_detail.clear()
        self._set_status("受保护视图已清空。")

    def _tab_changed(self, index: int) -> None:
        if index != 2 and self._approval_preview is not None:
            self._defer_preview()
        if hasattr(self, "_ingestion_timer"):
            if index == 4 and self.isVisible() and not self._identity_dirty:
                self._ingestion_timer.start()
                self._refresh_ingestion_jobs()
                if self._ingestion_coordinator is not None:
                    self._ingestion_coordinator.scan_recovery()
            else:
                self._ingestion_timer.stop()

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
            if hasattr(self, "approval_hint_label"):
                reason = "身份草稿尚未应用；所有审批操作已冻结。"
                self.approval_hint_label.setText(reason)
                for button in (
                    self.preview_button,
                    self.approve_button,
                    self.reject_button,
                    self.defer_button,
                ):
                    button.setToolTip(reason)
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
        ingestion_available = self._ingestion_coordinator is not None
        self.upload_button.setEnabled(can_write and ingestion_available)
        self.upload_button.setToolTip(
            "选择最多 20 个文件，加入可恢复的后台摄取队列。"
            if can_write and ingestion_available
            else (
                self._ingestion_unavailable_reason
                if can_write
                else "当前 Viewer 没有上传权限；请使用 Editor 身份。"
            )
        )
        self.archive_task_button.setText(
            "重试/处理未确认归档任务"
            if has_archive_retry
            else "为所选文档创建归档审批任务"
        )
        has_selected_document = self._selected_document() is not None
        self.archive_task_button.setEnabled(
            can_create_task and (has_selected_document or has_archive_retry)
        )
        if not can_create_task:
            self.archive_task_button.setToolTip("当前身份不能创建归档任务；请使用 Editor。")
        elif not has_selected_document and not has_archive_retry:
            self.archive_task_button.setToolTip("请先选择需要申请归档的文档。")
        else:
            self.archive_task_button.setToolTip("创建待另一位审批人处理的归档任务。")
        self.create_task_button.setEnabled(can_create_task)
        self.create_task_button.setToolTip(
            "创建待另一位审批人处理的写入任务。"
            if can_create_task
            else "当前身份不能创建写入任务；请使用 Editor。"
        )
        task = self._selected_task()
        awaiting = bool(task and task.get("status") == "awaiting_approval")
        approval = task.get("approval") if isinstance(task and task.get("approval"), dict) else {}
        approval_pending = bool(approval.get("status") == "pending")
        independent = bool(task and task.get("requested_by") != principal.user_id)
        eligible = can_approve and awaiting and approval_pending and independent
        preview_current = bool(
            eligible
            and self._approval_preview is not None
            and self._approval_task_id == self._selected_task_id()
        )
        self.preview_button.setEnabled(eligible)
        self.reject_button.setEnabled(eligible)
        self.defer_button.setEnabled(preview_current)
        self.approve_button.setEnabled(preview_current)
        if task is None:
            reason = "请先选择一个任务。"
        elif not can_approve:
            reason = "当前身份没有审批权限；请使用独立的 Approver。"
        elif not independent:
            reason = "请求人不能审批自己的任务，请由另一位 Approver 处理。"
        elif not awaiting or not approval_pending:
            reason = "该任务已决定、已失效或不再处于待审批状态，不能再次处理。"
        elif not preview_current:
            reason = (
                "可拒绝并结束任务（删除暂存正文，不执行写操作）；"
                "批准前必须加载绑定当前审批人的精确预览。"
            )
        else:
            reason = "精确预览已加载。请核对目标、参数、后果和恢复限制后再决定。"
        self.approval_hint_label.setText(reason)
        self.preview_button.setToolTip(
            "加载与当前审批人绑定的精确 Action Preview。" if eligible else reason
        )
        self.approve_button.setToolTip(
            "批准并只执行当前精确预览中的动作。" if preview_current else reason
        )
        self.reject_button.setToolTip(
            "拒绝会结束任务并删除暂存正文，但不会执行写操作。" if eligible else reason
        )
        self.defer_button.setToolTip(
            "关闭当前预览，任务保持待审批。" if preview_current else reason
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
        self._ingestion_views_active = False
        if hasattr(self, "_ingestion_timer"):
            self._ingestion_timer.stop()
        self._clear_identity_scoped_views()
        self._needs_refresh_on_show = True
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        self._ingestion_views_active = True
        super().showEvent(event)
        if self._needs_refresh_on_show:
            self._needs_refresh_on_show = False
            if self._identity_dirty:
                self._set_status("身份字段尚未应用；受保护视图保持清空，请先应用身份。")
            else:
                self.refresh_all()
        if (
            hasattr(self, "_ingestion_timer")
            and self.tabs.currentIndex() == 4
            and not self._identity_dirty
        ):
            self._ingestion_timer.start()
            self._refresh_ingestion_jobs()
            if self._ingestion_coordinator is not None:
                self._ingestion_coordinator.scan_recovery()

    def shutdown_ingestion(self, timeout_ms: int = 2000) -> bool:
        """Stop desktop ingestion workers before closing the owned API endpoint."""
        if hasattr(self, "_ingestion_timer"):
            self._ingestion_timer.stop()
        coordinator = self._ingestion_coordinator
        return True if coordinator is None else coordinator.shutdown(timeout_ms)
