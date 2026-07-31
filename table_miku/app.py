from __future__ import annotations

import random
import re
import sys
import threading
import textwrap
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Q_ARG, QEasingCurve, QMetaObject, QPoint, QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QIcon
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .assistant_data import (
    add_application_record,
    add_interview_review,
    format_course_time_slots,
    format_application_summary,
    format_interview_summary,
    format_timetable,
    import_timetable_pdf,
    load_application_records,
    load_interview_reviews,
    load_timetable,
    parse_course_time_slots,
)
from .assistant_core import PersonalAssistant
from .ai_consent import AIConsentChoice, request_ai_consent
from .assistant_log import append_event
from .goal_parser import ParsedGoalInput, parse_goal_input
from .knowledge_base import migrate_legacy_record, qa_pairs_for_card
from .knowledge_service import (
    due_review_items,
    format_knowledge,
    load_knowledge_cards,
    record_review,
    refresh_knowledge_repository,
)
from .paths import PROJECT_ROOT, asset_path, qml_path
from .pomodoro import pomodoro_status, start_pomodoro, stop_pomodoro
from .planner import add_goal, ensure_goal_plans, today_tasks
from .reminders import ReminderManager
from .sprites import export_menu_icon, export_runtime_sprite_assets
from .startup import is_startup_enabled, set_startup_enabled
from .storage import load_goals, load_settings, save_settings
from .system_monitor import SystemMonitor
from .weather_monitor import WeatherMonitor


CHAT_LINES = [
    "键盘热身完毕，先做一个最小任务吧。",
    "给我 25 分钟专注时间，我负责加油。",
    "项目、算法、简历，今天推进一小块。",
    "先让代码跑起来，完美可以晚点来。",
    "卡住就写下问题，问题清楚就好办了。",
    "今天赢过昨天一点点，就算 Miku 盖章通过。",
]

IMPORT_EXAMPLE = """支持多种输入格式，例如：

目标：大二暑假前拿到后端开发实习
每天 90 分钟
08:30 复习 Java/Python 基础
14:30 刷 2 道算法题
20:30 整理项目 README 和简历

也支持 JSON：
{
  "goals": [{"title": "准备软件开发实习", "daily_minutes": 90}],
  "schedule": [{"time": "08:30", "task": "复习基础"}]
}
"""

COMMAND_EXAMPLE = """cwd=D:\\code\\Table Pet
.\\.venv\\Scripts\\python.exe -m compileall main.py table_miku
"""

APPLICATION_EXAMPLE = """公司：星河科技
岗位：后端开发实习
状态：已投递
渠道：Boss 直聘
下一步：5 月 24 日前补一版项目 README，若未回复就跟进一次
备注：JD 强调 Python、SQL、接口设计
"""

INTERVIEW_EXAMPLE = """公司：星河科技
轮次：一面
复盘：项目讲清楚了，但数据库索引回答不够具体。
下一步：整理 3 个索引失效案例，明晚用 STAR 结构重讲项目亮点。
"""

COURSE_TIME_EXAMPLE = """# 可粘贴默认、冬季或夏季时间表
default 1-2节 08:00-09:40
default 3-4节 10:00-11:40
default 5-6节 14:00-15:40
default 7-8节 16:00-17:40
default 9-10节 19:00-20:40

winter 1-2节 08:00-09:40
winter 3-4节 10:00-11:40
summer 1-2节 08:00-09:40
summer 3-4节 10:00-11:40
"""

DIALOG_STYLE = """
QDialog {
    background: #f7fbff;
    color: #263553;
}
QLabel#dialogTitle {
    color: #1f2d4a;
}
QLabel#dialogIntro {
    color: #52627f;
    line-height: 150%;
}
QTextEdit {
    background: rgba(255, 255, 255, 245);
    border: 1px solid #c9d6ec;
    border-radius: 12px;
    color: #263553;
    padding: 12px;
    selection-background-color: #b8f5ff;
}
QPushButton {
    min-width: 84px;
    min-height: 30px;
    border: 1px solid #bdd0ea;
    border-radius: 8px;
    padding: 6px 12px;
    background: #ffffff;
    color: #263553;
    font-weight: 600;
}
QPushButton:hover {
    background: #e9fbff;
    border-color: #7fd9e9;
}
QPushButton:pressed {
    background: #d8f6ff;
}
"""

MENU_STYLE = """
QMenu {
    background: rgba(255, 255, 255, 246);
    border: 1px solid #c8d5eb;
    border-radius: 12px;
    padding: 8px;
    color: #263553;
}
QMenu::item {
    min-width: 176px;
    padding: 8px 12px;
    border-radius: 8px;
}
QMenu::item:selected {
    background: #e7fbff;
    color: #1d405f;
}
QMenu::separator {
    height: 1px;
    background: #dce6f5;
    margin: 6px 4px;
}
"""


class QmlMiku(QQuickWidget):
    """Qt Quick renderer for the desktop pet animation surface."""

    EXPRESSIONS = ["focus", "smile", "happy", "surprised", "sleepy", "typing", "cheer", "thinking", "alarm", "rest"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.setClearColor(QColor(0, 0, 0, 0))
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        pet_scene_path = qml_path("PetScene.qml")
        self.engine().addImportPath(str(pet_scene_path.parent))
        self.setSource(QUrl.fromLocalFile(str(pet_scene_path)))
        self._root = self.rootObject()
        if self._root is None:
            errors = "; ".join(error.toString() for error in self.errors())
            detail = f": {errors}" if errors else f" at {pet_scene_path}"
            raise RuntimeError(f"PetScene.qml failed to load{detail}")
        self._load_sprite_assets()

        self.expression_timer = QTimer(self)
        self.expression_timer.setInterval(5_400)
        self.expression_timer.timeout.connect(self.random_expression)
        self.expression_timer.start()

    @property
    def scene(self):
        return self._root

    def set_expression(self, expression: str) -> None:
        next_expression = expression if expression in self.EXPRESSIONS else "focus"
        QMetaObject.invokeMethod(
            self._root,
            "setExpression",
            Q_ARG("QVariant", next_expression),
        )

    def random_expression(self) -> None:
        current = str(self._root.property("expression"))
        choices = [expression for expression in self.EXPRESSIONS if expression != current]
        self.set_expression(random.choice(choices))
        self.expression_timer.setInterval(random.randint(4_800, 7_400))

    def nudge(self) -> None:
        QMetaObject.invokeMethod(self._root, "nudge")
        self.set_expression(random.choice(["happy", "smile", "surprised"]))

    def show_bubble(self, text: str, seconds: int) -> None:
        QMetaObject.invokeMethod(
            self._root,
            "showBubble",
            Q_ARG("QVariant", text),
            Q_ARG("QVariant", seconds),
        )

    def _load_sprite_assets(self) -> None:
        assets = {
            key: QUrl.fromLocalFile(str(path)).toString()
            for key, path in export_runtime_sprite_assets().items()
        }
        focus_source = assets.get("focus") or assets.get("idle") or ""
        self._root.setProperty("spriteMap", assets)
        self._root.setProperty("keyboardSource", assets.get("keyboard", ""))
        self._root.setProperty("previousSource", focus_source)
        self._root.setProperty("currentSource", focus_source)


class TextInputDialog(QDialog):
    def __init__(self, title: str, intro: str, example: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowIcon(QIcon(str(export_menu_icon())))
        self.resize(560, 430)
        self.setStyleSheet(DIALOG_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        label = QLabel(intro)
        label.setObjectName("dialogIntro")
        label.setWordWrap(True)
        layout.addWidget(label)

        self.editor = QTextEdit(self)
        self.editor.setPlainText(example)
        self.editor.selectAll()
        layout.addWidget(self.editor)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def text(self) -> str:
        return self.editor.toPlainText()


class KnowledgeLibraryDialog(QDialog):
    """Structured knowledge browser with subject list, search, and readable details."""

    def __init__(self, records: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._records = [migrate_legacy_record(record) for record in records]
        self._visible_indexes: list[int] = []
        self.setWindowTitle("计算机知识库")
        self.setWindowIcon(QIcon(str(export_menu_icon())))
        self.resize(860, 620)
        self.setStyleSheet(DIALOG_STYLE + """
QLineEdit {
    background: #ffffff;
    border: 1px solid #c9d6ec;
    border-radius: 8px;
    color: #263553;
    padding: 8px 10px;
}
QListWidget {
    background: rgba(255, 255, 255, 245);
    border: 1px solid #c9d6ec;
    border-radius: 10px;
    color: #263553;
    padding: 6px;
}
QListWidget::item {
    padding: 9px 8px;
    border-radius: 7px;
}
QListWidget::item:selected {
    background: #dff8ff;
    color: #1d405f;
}
""")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("计算机知识库")
        title.setObjectName("dialogTitle")
        title.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.Bold))
        layout.addWidget(title)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜索学科、术语、关键点或问题")
        self._search.textChanged.connect(self._refresh_list)
        layout.addWidget(self._search)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._list = QListWidget(splitter)
        self._list.setMinimumWidth(220)
        self._list.currentItemChanged.connect(lambda current, _previous: self._show_selected(current))

        self._detail = QTextEdit(splitter)
        self._detail.setReadOnly(True)
        self._detail.setFont(QFont("Microsoft YaHei UI", 10))
        self._detail.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_list()

    def _refresh_list(self) -> None:
        query = self._search.text().strip().lower()
        self._list.clear()
        self._visible_indexes = []
        for index, record in enumerate(self._records):
            if query and query not in self._search_text(record):
                continue
            self._visible_indexes.append(index)
            topic = str(record.get("topic") or record.get("title") or "未命名主题")
            item = QListWidgetItem(f"{topic}  ·  {self._category(record)}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            self._list.addItem(item)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)
        else:
            self._detail.setPlainText("没有匹配的知识卡片。")

    def _show_selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(index, int) and 0 <= index < len(self._records):
            self._detail.setPlainText(self._format_detail(self._records[index]))

    @staticmethod
    def _category(record: dict) -> str:
        topic = str(record.get("topic") or record.get("title") or "")
        if topic in {"Java 后端基础", "Go 后端基础", "工程实践与架构"}:
            return "实习与工程"
        if topic in {"软件工程", "算法设计与分析", "计算机安全", "分布式系统"}:
            return "进阶基础"
        return "计算机基础"

    def _search_text(self, record: dict) -> str:
        fields: list[str] = [
            str(record.get("topic") or ""),
            str(record.get("title") or ""),
            str(record.get("overview") or record.get("summary") or ""),
            self._category(record),
        ]
        fields.extend(str(item) for item in record.get("key_points") or [])
        fields.extend(str(item) for item in record.get("review_questions") or [])
        for item in record.get("glossary") or []:
            if isinstance(item, dict):
                fields.append(str(item.get("term") or ""))
                fields.append(str(item.get("explanation") or ""))
        for pair in qa_pairs_for_card(record):
            fields.append(pair["question"])
            fields.append(pair["answer"])
        return " ".join(fields).lower()

    def _format_detail(self, record: dict) -> str:
        record = migrate_legacy_record(record)
        topic = str(record.get("topic") or record.get("title") or "未命名主题")
        parts = [
            topic,
            f"分类：{self._category(record)}",
            f"来源状态：{'离线种子' if record.get('offline') else record.get('source_name', '未知')}",
            "",
            "概览",
            str(record.get("overview") or record.get("summary") or "暂无概览。"),
        ]

        sections = record.get("sections") or []
        if sections:
            parts.extend(["", "知识结构"])
            for section in sections[:6]:
                if isinstance(section, dict):
                    heading = section.get("heading") or "小节"
                    content = section.get("content") or ""
                else:
                    heading = "小节"
                    content = str(section)
                parts.append(f"- {heading}：{content}")

        key_points = record.get("key_points") or []
        if key_points:
            parts.extend(["", "关键点"])
            parts.extend(f"- {point}" for point in key_points[:8])

        glossary = record.get("glossary") or []
        if glossary:
            parts.extend(["", "术语"])
            for item in glossary[:8]:
                if isinstance(item, dict):
                    parts.append(f"- {item.get('term')}：{item.get('explanation')}")

        examples = record.get("examples") or []
        if examples:
            parts.extend(["", "例子"])
            parts.extend(f"- {example}" for example in examples[:4])

        qa_pairs = qa_pairs_for_card(record)
        if qa_pairs:
            parts.extend(["", "问题与参考答案"])
            for pair in qa_pairs[:6]:
                parts.append(f"问：{pair['question']}")
                parts.append(f"答：{pair['answer']}")
                parts.append("")

        sources = record.get("sources") or []
        if sources:
            parts.append("来源")
            for source in sources[:6]:
                if isinstance(source, dict):
                    name = source.get("name") or "来源"
                    kind = source.get("kind") or ""
                    url = source.get("url") or ""
                    parts.append(f"- {name}{' / ' + kind if kind else ''}{' / ' + url if url else ''}")
        else:
            source_url = record.get("source_url") or record.get("source") or ""
            if source_url:
                parts.extend(["来源", f"- {source_url}"])
        return "\n".join(part for part in parts if part is not None)


class TaskDialog(QDialog):
    def __init__(self, report: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("今日任务")
        self.setWindowIcon(QIcon(str(export_menu_icon())))
        self.resize(500, 440)
        self.setStyleSheet(DIALOG_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("今日任务全景")
        title.setObjectName("dialogTitle")
        title.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.Bold))
        title.setStyleSheet("color: #27385f;")
        layout.addWidget(title)

        content = QTextEdit(self)
        content.setReadOnly(True)
        content.setFont(QFont("Microsoft YaHei UI", 10))
        content.setPlainText(report)
        layout.addWidget(content)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class BubbleDetailDialog(QDialog):
    """长文本详情查看窗口（非模态）"""

    def __init__(self, title: str, content: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowIcon(QIcon(str(export_menu_icon())))
        self.resize(480, 380)
        self.setStyleSheet(DIALOG_STYLE)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)

        self._content_area = QTextEdit(self)
        self._content_area.setReadOnly(True)
        self._content_area.setPlainText(content)
        self._content_area.setFont(QFont("Microsoft YaHei UI", 10))
        layout.addWidget(self._content_area)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def set_text(self, content: str) -> None:
        """更新显示的文本内容"""
        self._content_area.setPlainText(content)


class ReviewDialog(QDialog):
    """今日复习 — 显示到期知识卡片并提供掌握/模糊/不会反馈按钮"""

    def __init__(self, items: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items = items
        self._index = 0
        self._showing_answer = False
        self._last_result = ""
        self.setWindowTitle("今日复习")
        self.setWindowIcon(QIcon(str(export_menu_icon())))
        self.resize(520, 560)
        self.setStyleSheet(DIALOG_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        self._title_label = QLabel()
        self._title_label.setObjectName("dialogTitle")
        self._title_label.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.Bold))
        self._title_label.setStyleSheet("color: #27385f;")
        layout.addWidget(self._title_label)

        self._content_area = QTextEdit(self)
        self._content_area.setReadOnly(True)
        self._content_area.setFont(QFont("Microsoft YaHei UI", 10))
        layout.addWidget(self._content_area)

        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(6)

        row1 = QHBoxLayout()
        self._known_btn = QPushButton("✓ 掌握")
        self._fuzzy_btn = QPushButton("~ 模糊")
        self._forgotten_btn = QPushButton("✗ 不会")
        self._known_btn.setStyleSheet("QPushButton { color: #1b8c5e; font-weight: bold; } QPushButton:hover { background: #e0f7ec; }")
        self._fuzzy_btn.setStyleSheet("QPushButton { color: #c78a1c; font-weight: bold; } QPushButton:hover { background: #fff8e6; }")
        self._forgotten_btn.setStyleSheet("QPushButton { color: #c74242; font-weight: bold; } QPushButton:hover { background: #ffeaea; }")
        self._known_btn.clicked.connect(lambda: self._answer("known"))
        self._fuzzy_btn.clicked.connect(lambda: self._answer("fuzzy"))
        self._forgotten_btn.clicked.connect(lambda: self._answer("forgotten"))
        row1.addWidget(self._known_btn)
        row1.addWidget(self._fuzzy_btn)
        row1.addWidget(self._forgotten_btn)

        self._next_btn = QPushButton("下一张 →")
        self._next_btn.setStyleSheet("QPushButton { color: #27385f; font-weight: bold; min-width: 120px; } QPushButton:hover { background: #e9fbff; border-color: #7fd9e9; }")
        self._next_btn.clicked.connect(self._go_next)
        self._next_btn.hide()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row2 = QHBoxLayout()
        row2.addWidget(self._next_btn)
        row2.addStretch()
        row2.addWidget(close_btn)

        btn_layout.addLayout(row1)
        btn_layout.addLayout(row2)
        layout.addLayout(btn_layout)

        self._show_current()

    def _show_current(self) -> None:
        self._showing_answer = False
        self._known_btn.setEnabled(True)
        self._fuzzy_btn.setEnabled(True)
        self._forgotten_btn.setEnabled(True)
        self._next_btn.hide()

        if self._index >= len(self._items):
            self._title_label.setText("今日复习 ✓")
            self._content_area.setPlainText("今天到期的知识卡片已全部复习完毕！\n\nMiku 给你点赞~ 继续保持这个节奏。")
            self._known_btn.setEnabled(False)
            self._fuzzy_btn.setEnabled(False)
            self._forgotten_btn.setEnabled(False)
            return

        item = self._items[self._index]
        card = item["card"]
        state = item["state"]
        text = self._format_card(card, state)
        self._title_label.setText(f"今日复习 ({self._index + 1}/{len(self._items)})")
        self._content_area.setPlainText(text)

    @staticmethod
    def _format_card(card: dict, state: dict) -> str:
        parts = [
            f"主题：{card.get('topic', card.get('title', '未知'))}",
            f"掌握度：{state.get('mastery', 0):.0%}  |  复习次数：{state.get('review_count', 0)}",
            f"来源：{card.get('source_name', '未知')}",
            "",
            f"{card.get('overview', card.get('summary', ''))}",
        ]

        sections = card.get("sections") or []
        if sections:
            parts.append("")
            for section in sections[:4]:
                heading = section.get("heading", "小节") if isinstance(section, dict) else "小节"
                content = section.get("content", "") if isinstance(section, dict) else str(section)
                parts.append(f"■ {heading}：{content}")

        key_points = card.get("key_points") or []
        if key_points:
            parts.append("\n关键点：")
            parts.extend(f"  • {p}" for p in key_points[:8])

        glossary = card.get("glossary") or []
        if glossary:
            parts.append("\n术语：")
            for g in glossary[:5]:
                if isinstance(g, dict):
                    parts.append(f"  {g.get('term')}：{g.get('explanation')}")

        examples = card.get("examples") or []
        if examples:
            parts.append("\n例子：")
            for e in examples[:2]:
                parts.append(f"  • {e}")

        questions = card.get("review_questions") or []
        if questions:
            parts.append("\n复习问题：")
            for q in questions[:5]:
                parts.append(f"  ❓ {q}")

        return "\n".join(parts)

    def _answer(self, result: str) -> None:
        if self._index >= len(self._items):
            return
        item = self._items[self._index]
        card_id = item["card"].get("id", "")
        topic = item["card"].get("topic", "未知")
        labels = {"known": "掌握", "fuzzy": "模糊", "forgotten": "不会"}

        # Walk up to find TableMiku parent for say()
        miku_parent = self.parent()
        while miku_parent is not None and not hasattr(miku_parent, 'say'):
            miku_parent = miku_parent.parent()

        if card_id:
            updated = record_review(card_id, result)
            if updated and miku_parent and hasattr(miku_parent, 'say'):
                next_at = updated.get("next_review_at", "")
                try:
                    next_dt = datetime.fromisoformat(next_at)
                    next_str = next_dt.strftime("%m月%d日 %H:%M")
                except (TypeError, ValueError):
                    next_str = next_at
                miku_parent.say(f"{topic}：标记为「{labels[result]}」。下次复习：{next_str}。")

        # 先展示答案卡片，而不是直接跳到下一张
        self._showing_answer = True
        self._last_result = result
        self._show_answer_card(result)

    def _show_answer_card(self, result: str) -> None:
        """Show the answer/review card after user gives feedback."""
        if self._index >= len(self._items):
            return
        item = self._items[self._index]
        card = item["card"]
        labels = {"known": "掌握", "fuzzy": "模糊", "forgotten": "不会"}
        emoji = {"known": "✅", "fuzzy": "🔶", "forgotten": "❌"}

        parts = [f"你选择了：{emoji.get(result, '')} {labels.get(result, result)}", ""]

        qa_pairs = qa_pairs_for_card(card)
        if qa_pairs:
            parts.append("问题与参考答案")
            for pair in qa_pairs[:6]:
                parts.append(f"问：{pair['question']}")
                parts.append(f"答：{pair['answer'] or '暂无可靠答案'}")
                parts.append("")

        # 核心概念
        parts.append("📚 核心概念")
        parts.append(card.get("overview", card.get("summary", "")))
        parts.append("")

        key_points = card.get("key_points") or []
        if key_points:
            parts.append("关键要点：")
            parts.extend(f"  • {p}" for p in key_points[:6])
            parts.append("")

        # 应用场景
        parts.append("🔧 应用场景")
        sections = card.get("sections") or []
        if sections:
            for section in sections[:3]:
                heading = section.get("heading", "") if isinstance(section, dict) else ""
                content = section.get("content", "") if isinstance(section, dict) else str(section)
                if heading and content:
                    parts.append(f"  {heading}：{content}")

        examples = card.get("examples") or []
        if examples:
            parts.append("")
            for e in examples[:2]:
                parts.append(f"  例：{e}")
        parts.append("")

        # 来源
        parts.append("📖 来源")
        source_name = card.get("source_name", "未知")
        source_url = card.get("source_url") or card.get("source", "")
        parts.append(f"  {source_name}")
        if source_url:
            parts.append(f"  {source_url}")
        parts.append("")

        # 复习提示
        if result == "forgotten":
            parts.append("💡 提示：这张卡片标记为「不会」，1 小时后会再次出现，建议重点回顾。")
        elif result == "fuzzy":
            parts.append("💡 提示：这张卡片标记为「模糊」，会按当前间隔再次安排复习。")

        self._title_label.setText(f"今日复习 ({self._index + 1}/{len(self._items)}) — 答案")
        self._content_area.setPlainText("\n".join(parts))

        # 禁用反馈按钮，显示下一张按钮
        self._known_btn.setEnabled(False)
        self._fuzzy_btn.setEnabled(False)
        self._forgotten_btn.setEnabled(False)
        # 高亮用户选择
        selected_btn = {"known": self._known_btn, "fuzzy": self._fuzzy_btn, "forgotten": self._forgotten_btn}.get(result)
        if selected_btn:
            selected_btn.setStyleSheet(
                "QPushButton { color: #fff; font-weight: bold; background: #43d9f5; border: 2px solid #2ab8d4; border-radius: 8px; padding: 6px 12px; }"
            )
        self._next_btn.show()

    def _go_next(self) -> None:
        """Move to the next review card."""
        self._showing_answer = False
        # Reset selected button style
        for btn in (self._known_btn, self._fuzzy_btn, self._forgotten_btn):
            if "background: #43d9f5" in (btn.styleSheet() or ""):
                if btn is self._known_btn:
                    btn.setStyleSheet("QPushButton { color: #1b8c5e; font-weight: bold; } QPushButton:hover { background: #e0f7ec; }")
                elif btn is self._fuzzy_btn:
                    btn.setStyleSheet("QPushButton { color: #c78a1c; font-weight: bold; } QPushButton:hover { background: #fff8e6; }")
                else:
                    btn.setStyleSheet("QPushButton { color: #c74242; font-weight: bold; } QPushButton:hover { background: #ffeaea; }")
        self._index += 1
        self._show_current()


class TableMiku(QWidget):
    async_notice = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        ensure_goal_plans()
        self.settings = load_settings()
        self.drag_position: QPoint | None = None
        self.was_dragged = False

        self.setWindowTitle("Table Miku")
        self.setFixedSize(320, 380)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.async_notice.connect(self._handle_system_notice)

        self.pet = QmlMiku(self)
        self.pet.setGeometry(0, 0, self.width(), self.height())
        self.pet.scene.petPressed.connect(self._on_pet_pressed)
        self.pet.scene.petMoved.connect(self._on_pet_moved)
        self.pet.scene.petReleased.connect(self._on_pet_released)
        self.pet.scene.petRightClicked.connect(
            lambda x, y: self.show_context_menu(QPoint(int(x), int(y)))
        )

        self.reminders = ReminderManager(self)
        self.reminders.reminder.connect(self.say)
        self.reminders.start()

        self.system_monitor = SystemMonitor(self)
        self.system_monitor.notice.connect(self._handle_system_notice)
        self.system_monitor.start()

        # 天气预警监测
        self.weather_monitor = WeatherMonitor(self)
        self.weather_monitor.notice.connect(self._handle_system_notice)
        self.weather_monitor.start()

        self.assistant = PersonalAssistant(lambda: self.system_monitor.latest_snapshot, PROJECT_ROOT, self)
        self.assistant.notice.connect(self._handle_system_notice)
        self.assistant.start()

        self.tray_icon = self._setup_tray_icon()
        if (self.settings.get("assistant") or {}).get("ai_agent_enabled", False):
            self.say("Table Miku 已就位。AI 助理已自动开启，我会监测电脑、网络和今日计划。")
        else:
            self.say("Table Miku 已就位。我会监测电脑、网络、命令和今日计划。")

    def _setup_tray_icon(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        icon = QIcon(str(asset_path("miku.svg")))
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("Table Miku")

        menu = QMenu(self)
        menu.setFont(QFont("Microsoft YaHei UI", 9))
        menu.setStyleSheet(MENU_STYLE)
        show_action = QAction("显示/隐藏 Miku", self)
        review_action = QAction("今日复习", self)
        brief_action = QAction("生成助手简报", self)
        pomodoro_action = QAction("开始番茄钟", self)
        quit_action = QAction("关闭 Miku", self)
        show_action.triggered.connect(self.toggle_visible)
        review_action.triggered.connect(self.show_due_reviews)
        brief_action.triggered.connect(self.show_assistant_brief)
        pomodoro_action.triggered.connect(self.toggle_pomodoro)
        quit_action.triggered.connect(QApplication.instance().quit)
        for action in (show_action, review_action, brief_action, pomodoro_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        return tray

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggle_visible()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    def say(self, text: str) -> None:
        self.settings = load_settings()
        seconds = self._bubble_duration(text)
        bubble_text = self._bubble_text(text)

        # 显示 QML 气泡（自带淡入/淡出动画和点击暂停交互）
        self.pet.show_bubble(bubble_text, seconds)

        # 长文本：延迟弹出详情窗口
        if len(text) > 120:
            QTimer.singleShot(500, lambda: self._show_detail_dialog(text))

    def _show_detail_dialog(self, text: str) -> None:
        if not hasattr(self, '_detail_dialog') or self._detail_dialog is None:
            self._detail_dialog = BubbleDetailDialog("Miku 消息详情", text, self)
        self._detail_dialog.set_text(text)
        self._detail_dialog.show()
        self._detail_dialog.raise_()

    @staticmethod
    def _bubble_text(text: str) -> str:
        compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if len(compact) > 80:
            compact = TableMiku._summarize_long_text(compact)
        if len(compact) <= 42:
            return compact
        wrapped = textwrap.wrap(
            compact,
            width=24,
            max_lines=5,
            placeholder="...",
            break_long_words=True,
            replace_whitespace=False,
        )
        return "\n".join(wrapped)

    @staticmethod
    def _summarize_long_text(text: str) -> str:
        parts = [part.strip(" ，。；;") for part in re.split(r"[。；;]\s*|\n+", text) if part.strip()]
        if not parts:
            return text[:100]
        summary = "；".join(parts[:3])
        if len(summary) > 72:
            summary = summary[:69] + "..."
        return "摘要：" + summary

    @staticmethod
    def _bubble_duration(text: str) -> int:
        """弹性时长：max(8, 字数×0.3)，上限 80 秒"""
        raw_len = len(text)
        seconds = max(8, int(raw_len * 0.3))
        return min(seconds, 80)

    def _on_pet_pressed(self, x: float, y: float) -> None:
        del x, y
        self.drag_position = QCursor.pos() - self.frameGeometry().topLeft()
        self.was_dragged = False

    def _on_pet_moved(self, x: float, y: float) -> None:
        del x, y
        if self.drag_position is not None:
            self.move(QCursor.pos() - self.drag_position)
            self.was_dragged = True

    def _on_pet_released(self, x: float, y: float) -> None:
        del x, y
        self.drag_position = None
        if not self.was_dragged:
            self.pet.nudge()
            self.say(random.choice(CHAT_LINES))

    def show_context_menu(self, position: QPoint) -> None:
        self.settings = load_settings()
        menu = QMenu(self)
        menu.setFont(QFont("Microsoft YaHei UI", 9))
        menu.setStyleSheet(MENU_STYLE)

        today_action = QAction("查看今日任务", self)
        add_goal_action = QAction("导入学习目标/时间表", self)
        schedule_action = QAction("编辑定时提醒", self)
        weather_action = QAction("提醒当前城市天气", self)
        city_action = QAction("设置/自动定位城市", self)
        system_status_action = QAction("立即检测电脑/网络", self)
        brief_action = QAction("生成助手简报", self)
        watch_command_action = QAction("运行并监视命令", self)
        ai_plan_action = QAction("AI 规划/汇报（可选）", self)
        toggle_ai_action = QAction(
            "关闭 AI 助理" if (self.settings.get("assistant") or {}).get("ai_agent_enabled", False) else "开启 AI 助理",
            self,
        )
        pomodoro_action = QAction("番茄钟：开始/暂停", self)
        view_timetable_action = QAction("查看课程表", self)
        timetable_pdf_action = QAction("导入课程表 PDF", self)
        course_time_action = QAction("导入课程时间表", self)
        application_action = QAction("新增投递记录", self)
        interview_action = QAction("新增面试复盘", self)
        records_action = QAction("查看助理记录", self)
        knowledge_action = QAction("更新计算机知识库", self)
        view_knowledge_action = QAction("查看计算机知识库", self)
        startup_action = QAction("关闭开机自启" if is_startup_enabled() else "开启开机自启", self)
        menu_icon = QIcon(str(export_menu_icon()))
        if not menu_icon.isNull():
            timetable_pdf_action.setIcon(menu_icon)
        monitor_settings = self.settings.get("system_monitor") or {}
        toggle_monitor_action = QAction(
            "暂停系统监测" if monitor_settings.get("enabled", True) else "开启系统监测",
            self,
        )
        toggle_action = QAction(
            "暂停提醒" if self.settings.get("reminders_enabled", True) else "开启提醒",
            self,
        )
        quit_action = QAction("关闭 Miku", self)

        today_action.triggered.connect(self.show_today_tasks)
        add_goal_action.triggered.connect(self.import_goal)
        schedule_action.triggered.connect(self.edit_schedule)
        weather_action.triggered.connect(self.show_weather)
        city_action.triggered.connect(self.set_city)
        system_status_action.triggered.connect(self.show_system_status)
        brief_action.triggered.connect(self.show_assistant_brief)
        watch_command_action.triggered.connect(self.watch_command)
        ai_plan_action.triggered.connect(self.show_ai_plan)
        toggle_ai_action.triggered.connect(self.toggle_ai_agent)
        pomodoro_action.triggered.connect(self.toggle_pomodoro)
        view_timetable_action.triggered.connect(self.show_timetable)
        timetable_pdf_action.triggered.connect(self.import_timetable_pdf)
        course_time_action.triggered.connect(self.import_course_time_slots)
        application_action.triggered.connect(self.add_application)
        interview_action.triggered.connect(self.add_interview_review)
        records_action.triggered.connect(self.show_assistant_records)
        knowledge_action.triggered.connect(self.refresh_knowledge)
        view_knowledge_action.triggered.connect(self.show_knowledge)
        startup_action.triggered.connect(self.toggle_startup)
        toggle_monitor_action.triggered.connect(self.toggle_system_monitor)
        toggle_action.triggered.connect(self.toggle_reminders)
        quit_action.triggered.connect(QApplication.instance().quit)

        review_action = QAction("今日复习", self)
        review_action.triggered.connect(self.show_due_reviews)

        # ── 子菜单 1：📖 学习 ──
        study_menu = QMenu("📖 学习", menu)
        study_menu.setStyleSheet(MENU_STYLE)
        study_menu.addAction(today_action)
        study_menu.addAction(review_action)
        study_menu.addAction(add_goal_action)
        study_menu.addAction(schedule_action)
        study_menu.addAction(view_timetable_action)

        # ── 子菜单 2：💼 求职 ──
        job_menu = QMenu("💼 求职", menu)
        job_menu.setStyleSheet(MENU_STYLE)
        job_menu.addAction(application_action)
        job_menu.addAction(interview_action)
        job_menu.addAction(records_action)
        job_menu.addSeparator()
        job_menu.addAction(knowledge_action)
        job_menu.addAction(view_knowledge_action)

        # ── 子菜单 3：⚙️ 系统工具 ──
        tools_menu = QMenu("⚙️ 系统工具", menu)
        tools_menu.setStyleSheet(MENU_STYLE)
        tools_menu.addAction(weather_action)
        tools_menu.addAction(city_action)
        tools_menu.addAction(system_status_action)
        tools_menu.addSeparator()
        tools_menu.addAction(brief_action)
        tools_menu.addAction(ai_plan_action)
        tools_menu.addAction(watch_command_action)
        tools_menu.addSeparator()
        tools_menu.addAction(pomodoro_action)
        tools_menu.addAction(timetable_pdf_action)
        tools_menu.addAction(course_time_action)

        # ── 子菜单 4：🎨 设置 ──
        settings_menu = QMenu("🎨 设置", menu)
        settings_menu.setStyleSheet(MENU_STYLE)
        settings_menu.addAction(toggle_ai_action)
        settings_menu.addAction(toggle_monitor_action)
        settings_menu.addAction(toggle_action)
        settings_menu.addAction(startup_action)
        settings_menu.addSeparator()
        settings_menu.addAction(quit_action)

        # ── 主菜单 ──
        menu.addMenu(study_menu)
        menu.addMenu(job_menu)
        menu.addMenu(tools_menu)
        menu.addMenu(settings_menu)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.exec(self.mapToGlobal(position))

    def show_today_tasks(self) -> None:
        self.pet.set_expression("happy")
        settings = load_settings()
        blocks: list[str] = []

        tasks = today_tasks(load_goals())
        if tasks:
            lines = [f"{i}. {task}" for i, task in enumerate(tasks, 1)]
            blocks.append("学习目标\n" + "\n".join(lines))

        courses = self._today_course_lines()
        if courses:
            blocks.append("今日课程\n" + "\n".join(f"- {c}" for c in courses))

        schedule = self._schedule_lines()
        if schedule:
            blocks.append("定时提醒\n" + "\n".join(f"- {s}" for s in schedule))

        blocks.append(pomodoro_status(settings))

        apps = format_application_summary(load_application_records(), 5)
        if apps:
            blocks.append(apps)

        interviews = format_interview_summary(load_interview_reviews(), 5)
        if interviews:
            blocks.append(interviews)

        knowledge = format_knowledge(load_knowledge_cards(6), 6)
        if knowledge:
            blocks.append("计算机知识库\n" + knowledge)

        snapshot = self.system_monitor.latest_snapshot
        if snapshot:
            parts: list[str] = []
            if snapshot.cpu_percent is not None:
                parts.append(f"CPU {snapshot.cpu_percent:.0f}%")
            if snapshot.memory_percent is not None:
                parts.append(f"内存 {snapshot.memory_percent:.0f}%")
            if snapshot.network:
                statuses = [f"{n.name}: {'通' if n.ok else '断'}" for n in snapshot.network]
                parts.append("网络 " + "，".join(statuses))
            if parts:
                blocks.append("系统状态\n" + "；".join(parts))

        report = "\n\n".join(blocks) if blocks else "今天还没有任务。右键导入一个目标吧。"
        TaskDialog(report, self).exec()
        self.say("今日任务已打开。先挑最小的一项开始吧。")

    def import_goal(self) -> None:
        dialog = TextInputDialog(
            "导入学习目标/时间表",
            "可以粘贴自然语言、Markdown 列表或 JSON。我会自动识别目标和具体时间提醒。",
            IMPORT_EXAMPLE,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        parsed = parse_goal_input(dialog.text())
        self._apply_parsed_input(parsed)

    def edit_schedule(self) -> None:
        current = "\n".join(self._schedule_lines()) or "08:30 复习基础\n14:30 刷算法题\n20:30 复盘项目"
        dialog = TextInputDialog(
            "编辑定时提醒",
            "每行写一个时间和任务，例如：08:30 复习基础。保存后会覆盖当前定时提醒表。",
            current,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        parsed = parse_goal_input(dialog.text())
        if not parsed.reminders:
            self.pet.set_expression("surprised")
            self.say("我没有识别到具体时间。请按“08:30 任务内容”的格式写。")
            return
        self.settings = load_settings()
        self.settings["scheduled_reminders"] = parsed.reminders
        self.settings["fired_reminders"] = {}
        save_settings(self.settings)
        self.pet.set_expression("happy")
        self.say(f"定时提醒已更新，共 {len(parsed.reminders)} 条。")

    def _apply_parsed_input(self, parsed: ParsedGoalInput) -> None:
        added_goals = 0
        for goal in parsed.goals:
            add_goal(
                goal.get("title", "新的学习目标"),
                goal.get("description", ""),
                int(goal.get("daily_minutes", 60)),
            )
            added_goals += 1

        self.settings = load_settings()
        if parsed.reminders:
            self.settings["scheduled_reminders"] = parsed.reminders
            self.settings["fired_reminders"] = {}
            save_settings(self.settings)

        if added_goals or parsed.reminders:
            self.pet.set_expression("happy")
            self.say(f"导入完成：{added_goals} 个目标，{len(parsed.reminders)} 条定时提醒。")
        else:
            self.pet.set_expression("surprised")
            self.say("我没识别到目标或时间表。可以按示例再试一次。")

    def _schedule_lines(self) -> list[str]:
        self.settings = load_settings()
        return [
            f"{item.get('time')} {item.get('task')}"
            for item in self.settings.get("scheduled_reminders", [])
            if item.get("time") and item.get("task")
        ]

    def _today_course_lines(self) -> list[str]:
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.now().weekday()]
        lines: list[str] = []
        for entry in load_timetable():
            if entry.get("weekday") != weekday:
                continue
            when = str(entry.get("section") or f"{entry.get('start')}-{entry.get('end')}").strip("-")
            lines.append(f"{weekday} {when} {entry.get('course')}")
        return lines

    def set_city(self) -> None:
        self.settings = load_settings()
        city, ok = QInputDialog.getText(
            self,
            "设置城市",
            "建议输入“区县,城市,省份”，例如：雨湖区,湘潭,湖南。输入 auto 会使用 IP 定位，可能受 VPN 影响：",
            text=str(self.settings.get("city", "auto")),
        )
        if not ok:
            return
        self.settings["city"] = city.strip() or "auto"
        save_settings(self.settings)
        self.pet.set_expression("smile")
        self.say(f"定位设置已更新为：{self.settings['city']}")

    def show_weather(self) -> None:
        self.pet.set_expression("focus")
        self.say("我正在定位并查询天气，稍等一下。")
        self.assistant.weather_now()

    def show_system_status(self) -> None:
        self.pet.set_expression("focus")
        self.say("我正在检测 CPU、内存和网络：百度、Google 都会试一下。")
        self.system_monitor.check_now()

    def show_assistant_brief(self) -> None:
        self.pet.set_expression("focus")
        self.say("我在整理今日任务、电脑状态和最近事件。")
        self.assistant.brief_now()
        # 简报在后台线程生成，延迟弹出对话框显示完整内容
        QTimer.singleShot(2000, self._show_brief_dialog)

    def _show_brief_dialog(self) -> None:
        report = getattr(self.assistant, '_last_brief_report', '')
        if not report:
            report = self.assistant._last_brief_report if hasattr(self.assistant, '_last_brief_report') else ''
        if not report:
            self.say("简报还在生成中，请稍后再试。")
            return
        dialog = TaskDialog(report, self)
        dialog.setWindowTitle("助手简报")
        dialog.exec()

    def watch_command(self) -> None:
        dialog = TextInputDialog(
            "运行并监视命令",
            "第一行可以写 cwd=工作目录，后面写要运行的 PowerShell 命令。命令结束后我会自动提醒你。",
            COMMAND_EXAMPLE,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.pet.set_expression("focus")
        self.assistant.run_watched_command(dialog.text())

    def show_ai_plan(self) -> None:
        self.pet.set_expression("focus")
        self.assistant.ai_plan_now()

    def toggle_ai_agent(self) -> None:
        self.settings = load_settings()
        assistant = self.settings.setdefault("assistant", {})
        if assistant.get("ai_agent_enabled", False):
            assistant["ai_agent_enabled"] = False
            save_settings(self.settings)
            append_event("consent", "AI 助理长期授权已撤销")
            self.pet.set_expression("sleepy")
            self.say("AI 助理已关闭，本地提醒和番茄钟仍会继续运行。")
            return

        provider = str(assistant.get("ai_provider", "deepseek")).lower()
        model_key = "deepseek_model" if provider == "deepseek" else "ai_model"
        model = str(assistant.get(model_key, "deepseek-v4-flash" if provider == "deepseek" else "gpt-5-nano"))
        endpoint = (
            str(assistant.get("deepseek_base_url", "https://api.deepseek.com")).rstrip("/") + "/chat/completions"
            if provider == "deepseek"
            else "https://api.openai.com/v1/responses"
        )
        choice = request_ai_consent(
            provider="DeepSeek" if provider == "deepseek" else "OpenAI",
            model=model,
            endpoint=endpoint,
            parent=self,
        )
        if choice is None:
            self.pet.set_expression("smile")
            self.say("没有启用 AI；本地功能保持不变。")
            return

        self.pet.set_expression("thinking")
        if choice == AIConsentChoice.STANDING:
            assistant["ai_agent_enabled"] = True
            save_settings(self.settings)
            append_event("consent", "AI 助理长期授权已启用", payload={"provider": provider, "model": model})
            self.say("AI 助理已持续启用；右键菜单可随时关闭。")
            self.assistant.ai_plan_now(authority="standing")
            return

        append_event("consent", "AI 助理单次授权", payload={"provider": provider, "model": model})
        self.say("AI 助理仅运行这一次，不会保存长期授权。")
        self.assistant.ai_plan_now(force=True, authority="once")

    def show_due_reviews(self) -> None:
        """打开今日复习对话框，显示到期知识卡片"""
        items = due_review_items()
        if not items:
            self.pet.set_expression("smile")
            self.say("今天没有到期的知识卡片，休息一下也没关系~")
            return
        self.pet.set_expression("thinking")
        dialog = ReviewDialog(items, self)
        dialog.exec()

    def toggle_pomodoro(self) -> None:
        self.settings = load_settings()
        pomodoro = self.settings.setdefault("pomodoro", {})
        if pomodoro.get("running", False):
            stop_pomodoro(self.settings)
            save_settings(self.settings)
            self.pet.set_expression("rest")
            self.say("番茄钟已暂停。需要继续时右键再开始。")
            return

        start_pomodoro(self.settings)
        save_settings(self.settings)
        self.pet.set_expression("typing")
        work_minutes = int((self.settings.get("pomodoro") or {}).get("work_minutes", 25))
        self.say(f"{pomodoro_status(self.settings)} 先专注 {work_minutes} 分钟，我会到点提醒你。")

    def import_timetable_pdf(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "导入课程表 PDF", str(PROJECT_ROOT), "PDF 文件 (*.pdf)")
        if not file_path:
            return
        self.pet.set_expression("thinking")
        try:
            entries = import_timetable_pdf(Path(file_path))
        except Exception as exc:
            self.pet.set_expression("surprised")
            self.say(f"课程表导入失败：{exc}")
            return
        if entries:
            self.pet.set_expression("happy")
            self.say(f"课程表导入完成，共识别 {len(entries)} 节课。可右键查看课程表，我也会按课程时间提前提醒。")
        else:
            self.pet.set_expression("surprised")
            self.say("PDF 已读取，但没识别到“周几 + 时间段 + 课程”的行。可以换一版可复制文本的课表 PDF。")

    def show_timetable(self) -> None:
        text = format_timetable(load_timetable(), 80)
        time_slots = format_course_time_slots((load_settings().get("course_time_slots") or []), 30)
        if time_slots:
            text = (text or "暂时还没有课程表。") + "\n\n课程时间表：\n" + time_slots
        dialog = TextInputDialog("课程表", "课程提醒会使用这里的课表和课程时间表。", text or "暂时还没有课程表。", self)
        dialog.editor.setReadOnly(True)
        dialog.exec()
        self.pet.set_expression("smile")

    def import_course_time_slots(self) -> None:
        current = format_course_time_slots((load_settings().get("course_time_slots") or []), 80) or COURSE_TIME_EXAMPLE
        dialog = TextInputDialog("导入课程时间表", "支持文字或 JSON。冬夏时间表可用 winter/summer 标记；没有标记则作为默认时间表。", current, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        slots = parse_course_time_slots(dialog.text())
        if not slots:
            self.pet.set_expression("surprised")
            self.say("我没有识别到课程时间。请按“winter 1-2节 08:00-09:40”的格式输入。")
            return
        self.settings = load_settings()
        self.settings["course_time_slots"] = slots
        self.settings.setdefault("course_reminders", {})["enabled"] = True
        save_settings(self.settings)
        self.pet.set_expression("happy")
        self.say(f"课程时间表已导入，共 {len(slots)} 条。课程提醒已开启。")

    def add_application(self) -> None:
        dialog = TextInputDialog("新增投递记录", "可按示例填写，也支持 JSON。Miku 会把它纳入 AI 规划上下文。", APPLICATION_EXAMPLE, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        record = add_application_record(dialog.text())
        self.pet.set_expression("cheer")
        self.say(f"投递记录已保存：{record.get('company')} {record.get('position')}。")

    def add_interview_review(self) -> None:
        dialog = TextInputDialog("新增面试复盘", "记录问题、表现和下一步动作。之后 AI 规划会优先参考这些复盘。", INTERVIEW_EXAMPLE, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        record = add_interview_review(dialog.text())
        self.pet.set_expression("happy")
        self.say(f"面试复盘已保存：{record.get('company')} {record.get('round')}。")

    def show_assistant_records(self) -> None:
        blocks = [
            format_timetable(load_timetable(), 10),
            format_application_summary(load_application_records(), 10),
            format_interview_summary(load_interview_reviews(), 10),
        ]
        text = "\n\n".join(block for block in blocks if block) or "暂时还没有课程表、投递记录或面试复盘。"
        dialog = TextInputDialog("助理记录", "这些记录会进入 AI 规划上下文。", text, self)
        dialog.editor.setReadOnly(True)
        dialog.exec()
        self.pet.set_expression("smile")

    def refresh_knowledge(self) -> None:
        self.pet.set_expression("thinking")
        self.say("我开始更新计算机知识库：优先使用离线种子兜底，能联网时补充 Wikipedia，并保留官方文档等来源链接。")
        threading.Thread(target=self._refresh_knowledge_worker, daemon=True).start()

    def _refresh_knowledge_worker(self) -> None:
        topics = (load_settings().get("knowledge") or {}).get("topics") or None
        summary = refresh_knowledge_repository(list(topics) if isinstance(topics, list) else None)
        online = int(summary.get("online", 0))
        trusted = int(summary.get("trusted_sources", 0))
        chunks = int(summary.get("trusted_chunks", 0))
        self.async_notice.emit(
            "happy" if online or trusted else "focus",
            f"计算机知识库已更新：共 {summary.get('topics', 0)} 个主题，"
            f"{online} 个主题使用在线摘要，新增/更新 {trusted} 个可信来源、{chunks} 个来源片段。",
        )

    def show_knowledge(self) -> None:
        dialog = KnowledgeLibraryDialog(load_knowledge_cards(), self)
        dialog.exec()
        self.pet.set_expression("smile")

    def toggle_startup(self) -> None:
        enabled = not is_startup_enabled()
        try:
            path = set_startup_enabled(enabled)
        except OSError as exc:
            self.pet.set_expression("surprised")
            self.say(f"开机自启设置失败：{exc}")
            return
        self.settings = load_settings()
        self.settings.setdefault("startup", {})["enabled"] = enabled
        save_settings(self.settings)
        self.pet.set_expression("smile" if enabled else "sleepy")
        self.say(f"开机自启已{'开启' if enabled else '关闭'}：{path}")

    def toggle_system_monitor(self) -> None:
        self.settings = load_settings()
        monitor = self.settings.setdefault("system_monitor", {})
        enabled = not monitor.get("enabled", True)
        monitor["enabled"] = enabled
        save_settings(self.settings)
        self.pet.set_expression("smile" if enabled else "sleepy")
        if enabled:
            self.say("系统监测已开启。我会留意 CPU、内存和网络状态，发现异常会提醒你。")
            self.system_monitor.check_now()
        else:
            self.say("系统监测已暂停。需要时右键可以立即检测。")

    def _handle_system_notice(self, expression: str, message: str) -> None:
        self.pet.set_expression(expression)
        self.say(message)

    def toggle_reminders(self) -> None:
        self.settings = load_settings()
        enabled = not self.settings.get("reminders_enabled", True)
        self.settings["reminders_enabled"] = enabled
        save_settings(self.settings)
        self.pet.set_expression("smile" if enabled else "sleepy")
        self.say("提醒已开启，我会按时间表叫你学习。" if enabled else "提醒已暂停，需要时右键再叫醒我。")


def run() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Table Miku")
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    icon = QIcon(str(asset_path("miku.svg")))
    if not icon.isNull():
        app.setWindowIcon(icon)

    window = TableMiku()
    desktop = app.primaryScreen().availableGeometry()
    window.move(desktop.right() - window.width() - 32, desktop.bottom() - window.height() - 32)
    window.show()
    sys.exit(app.exec())
