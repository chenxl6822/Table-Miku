from __future__ import annotations

import random
import re
import sys
import threading
import textwrap
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Q_ARG, QEasingCurve, QMetaObject, QPoint, QPropertyAnimation, QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QIcon
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGraphicsOpacityEffect,
    QInputDialog,
    QLabel,
    QMenu,
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
from .goal_parser import ParsedGoalInput, parse_goal_input
from .knowledge_base import format_knowledge, load_knowledge, refresh_computer_knowledge
from .paths import PROJECT_ROOT, asset_path
from .pomodoro import pomodoro_status, start_pomodoro, stop_pomodoro
from .planner import add_goal, ensure_goal_plans, today_tasks
from .reminders import ReminderManager
from .sprites import export_menu_icon, export_runtime_sprite_assets
from .startup import is_startup_enabled, set_startup_enabled
from .storage import load_goals, load_settings, save_settings
from .system_monitor import SystemMonitor
from .weather import get_weather


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

        qml_path = Path(__file__).parent / "qml" / "PetScene.qml"
        self.setSource(QUrl.fromLocalFile(str(qml_path)))
        self._root = self.rootObject()
        if self._root is None:
            raise RuntimeError("PetScene.qml failed to load")
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


class TaskDialog(QDialog):
    def __init__(self, tasks: list[str], schedule: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("今日任务")
        self.setWindowIcon(QIcon(str(export_menu_icon())))
        self.resize(460, 360)
        self.setStyleSheet(DIALOG_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("今日学习计划")
        title.setObjectName("dialogTitle")
        title.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.Bold))
        title.setStyleSheet("color: #27385f;")
        layout.addWidget(title)

        content = QTextEdit(self)
        content.setReadOnly(True)
        content.setFont(QFont("Microsoft YaHei UI", 10))
        content.setPlainText(self._format(tasks, schedule))
        layout.addWidget(content)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @staticmethod
    def _format(tasks: list[str], schedule: list[str]) -> str:
        blocks: list[str] = []
        if tasks:
            blocks.append("今日任务\n" + "\n".join(f"{index}. {task}" for index, task in enumerate(tasks, 1)))
        if schedule:
            blocks.append("定时提醒\n" + "\n".join(f"- {item}" for item in schedule))
        return "\n\n".join(blocks) if blocks else "今天还没有任务。右键导入一个目标吧。"


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

        self.bubble = QLabel(self)
        self.bubble.setWordWrap(True)
        self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble.setGeometry(10, 8, 300, 86)
        self.bubble.setFont(QFont("Microsoft YaHei UI", 9))
        self.bubble.setStyleSheet(
            """
            QLabel {
                background: rgba(255, 255, 255, 246);
                border: 1px solid rgba(80, 101, 138, 210);
                border-radius: 22px;
                color: #263553;
                padding: 11px 14px;
                line-height: 150%;
            }
            """
        )
        self.bubble_effect = QGraphicsOpacityEffect(self.bubble)
        self.bubble_effect.setOpacity(0.0)
        self.bubble.setGraphicsEffect(self.bubble_effect)
        self.bubble_animation = QPropertyAnimation(self.bubble_effect, b"opacity", self)
        self.bubble_animation.setDuration(180)
        self.bubble_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.bubble_animation.finished.connect(self._finish_bubble_animation)
        self._bubble_hiding = False
        self.bubble.hide()
        self.bubble.raise_()

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._hide_bubble)

        self.reminders = ReminderManager(self)
        self.reminders.reminder.connect(self.say)
        self.reminders.start()

        self.system_monitor = SystemMonitor(self)
        self.system_monitor.notice.connect(self._handle_system_notice)
        self.system_monitor.start()

        self.assistant = PersonalAssistant(lambda: self.system_monitor.latest_snapshot, PROJECT_ROOT, self)
        self.assistant.notice.connect(self._handle_system_notice)
        self.assistant.start()

        self.tray_icon = self._setup_tray_icon()
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
        brief_action = QAction("生成助手简报", self)
        pomodoro_action = QAction("开始番茄钟", self)
        quit_action = QAction("关闭 Miku", self)
        show_action.triggered.connect(self.toggle_visible)
        brief_action.triggered.connect(self.show_assistant_brief)
        pomodoro_action.triggered.connect(self.toggle_pomodoro)
        quit_action.triggered.connect(QApplication.instance().quit)
        for action in (show_action, brief_action, pomodoro_action):
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
        seconds = int(self.settings.get("bubble_seconds", 7))
        bubble_text = self._bubble_text(text)
        self.bubble.hide()
        self.pet.show_bubble(bubble_text, max(seconds, 10 if len(bubble_text) > 96 else 3))

    def _hide_bubble(self) -> None:
        self._bubble_hiding = True
        self.bubble_animation.stop()
        self.bubble_animation.setStartValue(self.bubble_effect.opacity())
        self.bubble_animation.setEndValue(0.0)
        self.bubble_animation.start()

    def _finish_bubble_animation(self) -> None:
        if self._bubble_hiding:
            self.bubble.hide()

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
        weather_report_action = QAction("立即天气汇报", self)
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
        weather_report_action.triggered.connect(self.show_weather_report)
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

        for action in (today_action, add_goal_action, schedule_action, weather_action, city_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(system_status_action)
        menu.addAction(brief_action)
        menu.addAction(weather_report_action)
        menu.addAction(watch_command_action)
        menu.addAction(ai_plan_action)
        menu.addAction(toggle_ai_action)
        menu.addSeparator()
        for action in (
            pomodoro_action,
            view_timetable_action,
            timetable_pdf_action,
            course_time_action,
            application_action,
            interview_action,
            records_action,
            knowledge_action,
            view_knowledge_action,
        ):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(startup_action)
        menu.addAction(toggle_monitor_action)
        menu.addSeparator()
        menu.addAction(toggle_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.exec(self.mapToGlobal(position))

    def show_today_tasks(self) -> None:
        self.pet.set_expression("happy")
        tasks = today_tasks(load_goals())
        schedule = self._schedule_lines() + self._today_course_lines()
        TaskDialog(tasks, schedule, self).exec()
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
        self.settings = load_settings()
        city = self.settings.get("city", "auto")
        self.pet.set_expression("focus")
        self.say("我正在定位并查询天气，稍等一下。")
        QApplication.processEvents()
        try:
            self.pet.set_expression("smile")
            self.say(get_weather(city))
        except Exception:
            self.pet.set_expression("surprised")
            self.say("天气查询暂时失败了。请检查网络，或右键把城市设置为 auto / 具体城市名。")

    def show_system_status(self) -> None:
        self.pet.set_expression("focus")
        self.say("我正在检测 CPU、内存和网络：百度、Google 都会试一下。")
        self.system_monitor.check_now()

    def show_assistant_brief(self) -> None:
        self.pet.set_expression("focus")
        self.say("我在整理今日任务、电脑状态和最近事件。")
        self.assistant.brief_now()

    def show_weather_report(self) -> None:
        self.pet.set_expression("focus")
        self.say("我正在做天气汇报，网络慢的话会多等一会。")
        self.assistant.weather_now()

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
        enabled = not assistant.get("ai_agent_enabled", False)
        assistant["ai_agent_enabled"] = enabled
        save_settings(self.settings)
        self.pet.set_expression("thinking" if enabled else "sleepy")
        if enabled:
            self.say("AI 助理已开启。它会结合目标、课程表、投递和面试复盘给你更个性化的提醒。")
            self.assistant.ai_plan_now()
        else:
            self.say("AI 助理已关闭，本地提醒和番茄钟仍会继续运行。")

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
        self.say("我开始连接 Wikipedia 更新计算机知识库。网络不通时会使用本地备用摘要。")
        threading.Thread(target=self._refresh_knowledge_worker, daemon=True).start()

    def _refresh_knowledge_worker(self) -> None:
        records = refresh_computer_knowledge()
        online = sum(1 for record in records if not record.get("offline"))
        self.async_notice.emit("happy" if online else "focus", f"计算机知识库已更新：{online}/{len(records)} 条来自 Wikipedia。")

    def show_knowledge(self) -> None:
        dialog = TextInputDialog("计算机知识库", "这些知识会进入 Miku 的规划上下文。", format_knowledge(load_knowledge()), self)
        dialog.editor.setReadOnly(True)
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
