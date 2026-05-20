from __future__ import annotations

import random
import sys
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QWidget,
)

from .paths import asset_path
from .planner import add_goal, ensure_goal_plans, today_tasks
from .reminders import ReminderManager
from .storage import load_goals, load_settings, save_settings
from .weather import get_weather


CHAT_LINES = [
    "欸嘿，今天也要向实习 offer 前进一点点。",
    "先做 25 分钟就好，启动比完美更重要。",
    "你的项目 README 也需要被宠爱一下。",
    "算法题不会欺负你太久的，我帮你盯着时间。",
    "喝水、坐直、打开编辑器。小小仪式感，启动！",
]


class TableMiku(QWidget):
    def __init__(self) -> None:
        super().__init__()
        ensure_goal_plans()
        self.settings = load_settings()
        self.drag_position: QPoint | None = None
        self.was_dragged = False

        self.setWindowTitle("Table Miku")
        self.setFixedSize(280, 310)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

        self.bubble = QLabel(self)
        self.bubble.setWordWrap(True)
        self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble.setGeometry(8, 8, 264, 96)
        self.bubble.setStyleSheet(
            """
            QLabel {
                background: rgba(255, 255, 255, 226);
                border: 2px solid #27385f;
                border-radius: 16px;
                color: #27385f;
                font-size: 14px;
                padding: 10px;
            }
            """
        )
        self.bubble.hide()

        self.pet = QLabel(self)
        self.pet.setGeometry(38, 88, 210, 210)
        self.pet.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_pet_image()

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.bubble.hide)

        self.reminders = ReminderManager(self)
        self.reminders.reminder.connect(self.say)
        self.reminders.start()

        self.say("Table Miku 已就位。右键我可以设置目标、看任务和天气。")

    def _load_pet_image(self) -> None:
        candidates = [
            asset_path("miku.png"),
            asset_path("miku.jpg"),
            asset_path("miku.svg"),
        ]
        pixmap = QPixmap()
        for candidate in candidates:
            if candidate.exists() and pixmap.load(str(candidate)):
                break
        if pixmap.isNull():
            pixmap = self._fallback_pixmap()
        self.pet.setPixmap(
            pixmap.scaled(
                self.pet.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    @staticmethod
    def _fallback_pixmap() -> QPixmap:
        pixmap = QPixmap(210, 210)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.cyan)
        painter.drawEllipse(28, 18, 154, 142)
        painter.setBrush(Qt.GlobalColor.white)
        painter.drawEllipse(66, 72, 30, 34)
        painter.drawEllipse(116, 72, 30, 34)
        painter.setBrush(Qt.GlobalColor.darkCyan)
        painter.drawEllipse(75, 82, 12, 14)
        painter.drawEllipse(125, 82, 12, 14)
        painter.setBrush(Qt.GlobalColor.magenta)
        painter.drawEllipse(93, 124, 24, 14)
        painter.end()
        return pixmap

    def say(self, text: str) -> None:
        self.bubble.setText(text)
        self.bubble.show()
        seconds = int(self.settings.get("bubble_seconds", 7))
        self.hide_timer.start(max(seconds, 2) * 1000)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.was_dragged = False
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.buttons() & Qt.MouseButton.LeftButton and self.drag_position is not None:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            self.was_dragged = True
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = None
            if not self.was_dragged:
                self.say(random.choice(CHAT_LINES))
            event.accept()

    def show_context_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        today_action = QAction("查看今日任务", self)
        add_goal_action = QAction("导入学习目标", self)
        weather_action = QAction("提醒当前城市天气", self)
        toggle_action = QAction(
            "暂停提醒" if self.settings.get("reminders_enabled", True) else "开启提醒",
            self,
        )
        quit_action = QAction("关闭 Miku", self)

        today_action.triggered.connect(self.show_today_tasks)
        add_goal_action.triggered.connect(self.import_goal)
        weather_action.triggered.connect(self.show_weather)
        toggle_action.triggered.connect(self.toggle_reminders)
        quit_action.triggered.connect(QApplication.instance().quit)

        menu.addAction(today_action)
        menu.addAction(add_goal_action)
        menu.addAction(weather_action)
        menu.addSeparator()
        menu.addAction(toggle_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.exec(self.mapToGlobal(position))

    def show_today_tasks(self) -> None:
        tasks = today_tasks(load_goals())
        self.say("今天建议：\n" + "\n".join(tasks[:3]))

    def import_goal(self) -> None:
        title, ok = QInputDialog.getText(self, "导入学习目标", "你想实现什么目标？")
        if not ok or not title.strip():
            return
        minutes, ok = QInputDialog.getInt(self, "每日学习时间", "每天计划学习多少分钟？", 60, 15, 360, 15)
        if not ok:
            minutes = 60
        goal = add_goal(title, daily_minutes=minutes)
        self.say(f"目标已收下：{goal['title']}\n我会按每天 {minutes} 分钟帮你提醒。")

    def show_weather(self) -> None:
        city = self.settings.get("city", "Shanghai")
        self.say(f"我去看看 {city} 的天气，稍等一下。")
        QApplication.processEvents()
        try:
            self.say(get_weather(city))
        except Exception:
            self.say("天气查询暂时失败了，可能是网络不可用。学习计划不受影响，我们继续前进。")

    def toggle_reminders(self) -> None:
        self.settings = load_settings()
        enabled = not self.settings.get("reminders_enabled", True)
        self.settings["reminders_enabled"] = enabled
        save_settings(self.settings)
        self.say("提醒已开启，我会继续轻轻戳你。" if enabled else "提醒已暂停，需要时右键再叫醒我。")


def run() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Table Miku")
    icon = QIcon(str(asset_path("miku.svg")))
    if not icon.isNull():
        app.setWindowIcon(icon)
    window = TableMiku()
    desktop = app.primaryScreen().availableGeometry()
    window.move(desktop.right() - window.width() - 32, desktop.bottom() - window.height() - 32)
    window.show()
    sys.exit(app.exec())
