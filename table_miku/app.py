from __future__ import annotations

import random
import sys

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPolygon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLabel,
    QMenu,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .goal_parser import ParsedGoalInput, parse_goal_input
from .paths import asset_path
from .planner import add_goal, ensure_goal_plans, today_tasks
from .reminders import ReminderManager
from .storage import load_goals, load_settings, save_settings
from .weather import get_weather


CHAT_LINES = [
    "我正在敲键盘陪你冲刺，今天先把最小任务启动起来。",
    "给我 25 分钟专注时间，我们把知识点一颗一颗敲进去。",
    "实习准备不是玄学：项目、算法、简历，每天推进一小块。",
    "别急着追求完美，先让代码跑起来，再让 README 说人话。",
    "今天的你只需要赢过昨天一点点，我负责在旁边噼里啪啦加油。",
    "如果卡住了，就写下问题。能被描述的问题，已经解决了一半。",
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


class TypingMiku(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.frame = 0
        self.timer = QTimer(self)
        self.timer.setInterval(110)
        self.timer.timeout.connect(self._next_frame)
        self.timer.start()

    def _next_frame(self) -> None:
        self.frame = (self.frame + 1) % 120
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.frame
        bounce = -4 if 8 <= t % 24 <= 14 else 0
        hand_shift = 5 if t % 12 < 6 else -5
        blink = t % 70 in {0, 1, 2}

        self._draw_shadow(painter)
        painter.translate(0, bounce)
        self._draw_twin_tails(painter)
        self._draw_body(painter)
        self._draw_head(painter, blink)
        self._draw_keyboard(painter, hand_shift)
        painter.end()

    def _draw_shadow(self, painter: QPainter) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(22, 31, 54, 55))
        painter.drawEllipse(QRectF(65, 300, 230, 34))

    def _draw_twin_tails(self, painter: QPainter) -> None:
        pen = QPen(QColor("#25365e"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QColor("#62dce1"))
        left = QPainterPath()
        left.moveTo(103, 92)
        left.cubicTo(42, 96, 38, 190, 53, 262)
        left.cubicTo(85, 231, 93, 149, 122, 116)
        left.closeSubpath()
        right = QPainterPath()
        right.moveTo(237, 92)
        right.cubicTo(298, 96, 302, 190, 287, 262)
        right.cubicTo(255, 231, 247, 149, 218, 116)
        right.closeSubpath()
        painter.drawPath(left)
        painter.drawPath(right)

    def _draw_body(self, painter: QPainter) -> None:
        painter.setPen(QPen(QColor("#25365e"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QColor("#f8fbff"))
        painter.drawRoundedRect(QRectF(118, 216, 104, 78), 18, 18)
        painter.setBrush(QColor("#ff92ad"))
        painter.drawPolygon(
            QPolygon(
                [
                    QPoint(138, 222),
                    QPoint(170, 260),
                    QPoint(202, 222),
                    QPoint(184, 222),
                    QPoint(170, 239),
                    QPoint(156, 222),
                ]
            )
        )

    def _draw_head(self, painter: QPainter, blink: bool) -> None:
        outline = QPen(QColor("#25365e"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(outline)
        painter.setBrush(QColor("#78e8eb"))
        painter.drawEllipse(QRectF(72, 42, 196, 178))

        painter.setBrush(QColor("#8ff4f0"))
        hair = QPainterPath()
        hair.moveTo(82, 88)
        hair.cubicTo(105, 40, 145, 31, 170, 38)
        hair.cubicTo(198, 31, 235, 45, 260, 89)
        hair.lineTo(210, 80)
        hair.lineTo(170, 132)
        hair.lineTo(128, 80)
        hair.lineTo(82, 88)
        hair.closeSubpath()
        painter.drawPath(hair)

        painter.setBrush(QColor("#25365e"))
        painter.drawRoundedRect(QRectF(91, 38, 31, 47), 8, 8)
        painter.drawRoundedRect(QRectF(218, 38, 31, 47), 8, 8)
        painter.setBrush(QColor("#ff8cad"))
        painter.drawRoundedRect(QRectF(101, 47, 9, 29), 4, 4)
        painter.drawRoundedRect(QRectF(230, 47, 9, 29), 4, 4)

        painter.setPen(QPen(QColor("#25365e"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if blink:
            painter.drawLine(112, 142, 144, 142)
            painter.drawLine(196, 142, 228, 142)
        else:
            self._draw_eye(painter, QRectF(106, 119, 42, 44))
            self._draw_eye(painter, QRectF(192, 119, 42, 44))

        painter.setPen(QPen(QColor("#25365e"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(QRectF(154, 151, 32, 24), 200 * 16, 140 * 16)
        painter.setPen(QPen(QColor("#ff8cad"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(QRectF(92, 155, 42, 22), 20 * 16, 120 * 16)
        painter.drawArc(QRectF(207, 155, 42, 22), 40 * 16, 120 * 16)

    def _draw_eye(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(QColor("#25365e"), 5))
        painter.setBrush(QColor("#eaffff"))
        painter.drawEllipse(rect)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#4fd5df"))
        painter.drawEllipse(rect.adjusted(9, 10, -8, -7))
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(rect.adjusted(21, 9, -12, -25))

    def _draw_keyboard(self, painter: QPainter, hand_shift: int) -> None:
        painter.setPen(QPen(QColor("#25365e"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(QRectF(74, 276, 192, 46), 14, 14)

        painter.setPen(Qt.PenStyle.NoPen)
        key_colors = [QColor("#d9fbff"), QColor("#ffccd8"), QColor("#e7f4ff")]
        for row in range(2):
            for col in range(7):
                color = key_colors[(col + row + self.frame // 4) % len(key_colors)]
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(92 + col * 22, 286 + row * 15, 15, 8), 3, 3)

        painter.setPen(QPen(QColor("#25365e"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(QColor("#f8fbff"))
        painter.drawEllipse(QRectF(97 + hand_shift, 253, 45, 33))
        painter.drawEllipse(QRectF(198 - hand_shift, 253, 45, 33))
        painter.setPen(QPen(QColor("#ff8cad"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(114 + hand_shift, 280, 135 + hand_shift, 286)
        painter.drawLine(222 - hand_shift, 280, 202 - hand_shift, 286)


class TextInputDialog(QDialog):
    def __init__(self, title: str, intro: str, example: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 430)
        layout = QVBoxLayout(self)

        label = QLabel(intro)
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


class TableMiku(QWidget):
    def __init__(self) -> None:
        super().__init__()
        ensure_goal_plans()
        self.settings = load_settings()
        self.drag_position: QPoint | None = None
        self.was_dragged = False

        self.setWindowTitle("Table Miku")
        self.setFixedSize(360, 410)
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
        self.bubble.setGeometry(16, 8, 328, 112)
        self.bubble.setFont(QFont("Microsoft YaHei UI", 10))
        self.bubble.setStyleSheet(
            """
            QLabel {
                background: rgba(255, 255, 255, 235);
                border: 2px solid #27385f;
                border-radius: 18px;
                color: #27385f;
                padding: 12px;
                line-height: 150%;
            }
            """
        )
        self.bubble.hide()

        self.pet = TypingMiku(self)
        self.pet.setGeometry(10, 82, 340, 330)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.bubble.hide)

        self.reminders = ReminderManager(self)
        self.reminders.reminder.connect(self.say)
        self.reminders.start()

        self.say("Table Miku 已就位。我会一边敲键盘，一边按时间提醒你学习。")

    def say(self, text: str) -> None:
        self.settings = load_settings()
        self.bubble.setText(text)
        self.bubble.show()
        seconds = int(self.settings.get("bubble_seconds", 7))
        self.hide_timer.start(max(seconds, 3) * 1000)

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
        self.settings = load_settings()
        menu = QMenu(self)
        menu.setFont(QFont("Microsoft YaHei UI", 9))

        today_action = QAction("查看今日任务", self)
        add_goal_action = QAction("导入学习目标/时间表", self)
        schedule_action = QAction("编辑定时提醒", self)
        weather_action = QAction("提醒当前城市天气", self)
        city_action = QAction("设置/自动定位城市", self)
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
        toggle_action.triggered.connect(self.toggle_reminders)
        quit_action.triggered.connect(QApplication.instance().quit)

        for action in (today_action, add_goal_action, schedule_action, weather_action, city_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(toggle_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.exec(self.mapToGlobal(position))

    def show_today_tasks(self) -> None:
        tasks = today_tasks(load_goals())
        schedule = self._schedule_lines()
        parts = []
        if tasks:
            parts.append("今日学习：\n" + "\n".join(tasks[:2]))
        if schedule:
            parts.append("定时提醒：\n" + "\n".join(schedule[:4]))
        self.say("\n\n".join(parts) if parts else "今天还没有任务。右键导入一个目标吧。")

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
            self.say("我没有识别到具体时间。请按“08:30 任务内容”的格式写。")
            return
        self.settings = load_settings()
        self.settings["scheduled_reminders"] = parsed.reminders
        self.settings["fired_reminders"] = {}
        save_settings(self.settings)
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
            self.say(f"导入完成：{added_goals} 个目标，{len(parsed.reminders)} 条定时提醒。")
        else:
            self.say("我没识别到目标或时间表。可以按示例再试一次。")

    def _schedule_lines(self) -> list[str]:
        self.settings = load_settings()
        return [
            f"{item.get('time')} {item.get('task')}"
            for item in self.settings.get("scheduled_reminders", [])
            if item.get("time") and item.get("task")
        ]

    def set_city(self) -> None:
        self.settings = load_settings()
        city, ok = QInputDialog.getText(
            self,
            "设置城市",
            "输入城市名，或输入 auto 使用 IP 自动定位：",
            text=str(self.settings.get("city", "auto")),
        )
        if not ok:
            return
        self.settings["city"] = city.strip() or "auto"
        save_settings(self.settings)
        self.say(f"定位设置已更新为：{self.settings['city']}")

    def show_weather(self) -> None:
        self.settings = load_settings()
        city = self.settings.get("city", "auto")
        self.say("我正在定位并查询天气，稍等一下。")
        QApplication.processEvents()
        try:
            self.say(get_weather(city))
        except Exception:
            self.say("天气查询暂时失败了。请检查网络，或右键把城市设置为 auto / 具体城市名。")

    def toggle_reminders(self) -> None:
        self.settings = load_settings()
        enabled = not self.settings.get("reminders_enabled", True)
        self.settings["reminders_enabled"] = enabled
        save_settings(self.settings)
        self.say("提醒已开启，我会按时间表叫你学习。" if enabled else "提醒已暂停，需要时右键再叫醒我。")


def run() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Table Miku")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    icon = QIcon(str(asset_path("miku.svg")))
    if not icon.isNull():
        app.setWindowIcon(icon)
    window = TableMiku()
    desktop = app.primaryScreen().availableGeometry()
    window.move(desktop.right() - window.width() - 32, desktop.bottom() - window.height() - 32)
    window.show()
    sys.exit(app.exec())
