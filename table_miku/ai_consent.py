from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget


class AIConsentChoice(str, Enum):
    ONCE = "once"
    STANDING = "standing"


class AIConsentDialog(QDialog):
    """Ask for explicit, time-bounded permission before sending AI context."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        endpoint: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.choice: AIConsentChoice | None = None
        self.setWindowTitle("启用 AI 助理")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(590, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel("确认发送范围与授权时长", self)
        title.setFont(QFont("Microsoft YaHei UI", 14, QFont.Weight.Bold))
        layout.addWidget(title)

        intro = QLabel(
            "AI 助理会把下列本地摘要发送给外部模型服务。取消不会影响本地提醒、天气、番茄钟或知识复习。",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        details = QTextEdit(self)
        details.setReadOnly(True)
        details.setPlainText(
            "\n".join(
                [
                    f"接收方：{provider}",
                    f"模型：{model}",
                    f"接口：{endpoint}",
                    "",
                    "每次发送的内容：",
                    "- 今日任务摘要（最多 2 项）",
                    "- CPU、内存和网络状态摘要",
                    "- 最近事件标题（最多 6 条）",
                    "- 最近投递、面试复盘与课程表摘要",
                    "- 本地知识卡片摘要（最多 4 张）",
                    "",
                    "不会作为对话文本发送：API Key、本地文件全文、命令输出全文。",
                    "持续启用后，启动简报和每日简报可能自动调用；可随时在右键菜单中关闭。",
                ]
            )
        )
        layout.addWidget(details)

        hint = QLabel("“仅运行这一次”不会保存长期授权；“持续启用”会保存设置。", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        once_button = QPushButton("仅运行这一次", self)
        standing_button = QPushButton("持续启用", self)
        cancel_button = QPushButton("暂不启用", self)
        cancel_button.setDefault(True)
        cancel_button.setAutoDefault(True)
        cancel_button.setFocus()
        once_button.clicked.connect(lambda: self._accept(AIConsentChoice.ONCE))
        standing_button.clicked.connect(lambda: self._accept(AIConsentChoice.STANDING))
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(once_button)
        buttons.addWidget(standing_button)
        buttons.addStretch(1)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

    def _accept(self, choice: AIConsentChoice) -> None:
        self.choice = choice
        self.accept()


def request_ai_consent(
    *,
    provider: str,
    model: str,
    endpoint: str,
    parent: QWidget | None = None,
) -> AIConsentChoice | None:
    dialog = AIConsentDialog(provider=provider, model=model, endpoint=endpoint, parent=parent)
    return dialog.choice if dialog.exec() == QDialog.DialogCode.Accepted else None
