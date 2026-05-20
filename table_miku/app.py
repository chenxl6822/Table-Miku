from __future__ import annotations

import math
import random
import sys

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
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
from .sprites import load_sprite, sprite_source_hint
from .storage import load_goals, load_settings, save_settings
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


class TypingMiku(QWidget):
    """Compact desktop-pet sprite with a realistic typing keyboard overlay."""

    EXPRESSIONS = ["focus", "smile", "happy", "surprised", "sleepy"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.frame = 0
        self.expression = "focus"
        self.sprites: dict[str, QPixmap] = {}

        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(95)
        self.animation_timer.timeout.connect(self._next_frame)
        self.animation_timer.start()

        self.expression_timer = QTimer(self)
        self.expression_timer.setInterval(4_500)
        self.expression_timer.timeout.connect(self.random_expression)
        self.expression_timer.start()

    def set_expression(self, expression: str) -> None:
        self.expression = expression if expression in self.EXPRESSIONS else "focus"
        self.update()

    def random_expression(self) -> None:
        self.set_expression(random.choice(self.EXPRESSIONS))

    def _next_frame(self) -> None:
        self.frame = (self.frame + 1) % 240
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bounce = math.sin(self.frame / 8) * 2.2
        left_tap = -5 if self.frame % 14 < 7 else 2
        right_tap = 3 if self.frame % 18 < 9 else -5
        blink = self.expression != "surprised" and self.frame % 72 in {0, 1, 2}

        self._draw_shadow(painter)
        painter.translate(0, bounce)
        self._draw_sprite(painter, blink)
        self._draw_keyboard(painter, left_tap, right_tap)
        painter.end()

    def _draw_shadow(self, painter: QPainter) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(22, 31, 54, 55))
        painter.drawEllipse(QRectF(43, 232, 214, 26))

    def _draw_sprite(self, painter: QPainter, blink: bool) -> None:
        pixmap = self._sprite_for_current_expression()
        if pixmap.isNull():
            self._draw_missing_sprite_hint(painter)
            return

        width = 190 + (2 if self.expression == "surprised" else 0)
        height = 172
        x = (self.width() - width) / 2
        y = 16 + math.sin(self.frame / 11) * 1.5
        painter.drawPixmap(
            QRectF(x, y, width, height).toRect(),
            pixmap.scaled(
                int(width),
                int(height),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ),
        )
        self._draw_expression_effects(painter, blink)

    def _sprite_for_current_expression(self) -> QPixmap:
        key = self.expression if self.expression in {"happy", "focus", "surprised", "sleepy"} else "idle"
        if key not in self.sprites:
            self.sprites[key] = load_sprite(key)
        if self.sprites[key].isNull() and "idle" not in self.sprites:
            self.sprites["idle"] = load_sprite("idle")
        return self.sprites.get(key, QPixmap()) if not self.sprites.get(key, QPixmap()).isNull() else self.sprites.get("idle", QPixmap())

    def _draw_expression_effects(self, painter: QPainter, blink: bool) -> None:
        if blink:
            painter.setPen(QPen(QColor("#243653"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(112, 94, 132, 94)
            painter.drawLine(169, 94, 189, 94)
        if self.expression == "happy":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#ff8cad"))
            painter.drawEllipse(QRectF(226, 48, 18, 18))
            painter.drawEllipse(QRectF(241, 48, 18, 18))
            heart = QPainterPath()
            heart.moveTo(243, 73)
            heart.lineTo(225, 55)
            heart.lineTo(260, 55)
            heart.closeSubpath()
            painter.drawPath(heart)
        elif self.expression == "surprised":
            painter.setPen(QPen(QColor("#243653"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(223, 48, 235, 34)
            painter.drawLine(237, 55, 253, 47)
        elif self.expression == "sleepy":
            painter.setPen(QPen(QColor("#6d80a7"), 2))
            painter.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
            painter.drawText(QRectF(216, 36, 58, 28), Qt.AlignmentFlag.AlignCenter, "Zzz")

    def _draw_missing_sprite_hint(self, painter: QPainter) -> None:
        painter.setPen(QPen(QColor("#243653"), 2))
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.drawRoundedRect(QRectF(42, 32, 216, 132), 18, 18)
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(
            QRectF(54, 48, 192, 96),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            sprite_source_hint(),
        )

    def _draw_keyboard(self, painter: QPainter, left_tap: int, right_tap: int) -> None:
        painter.setPen(QPen(QColor("#25304a"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        base = QPainterPath()
        base.moveTo(56, 204)
        base.lineTo(244, 204)
        base.lineTo(272, 248)
        base.lineTo(32, 248)
        base.closeSubpath()
        painter.setBrush(QColor("#f5f7fb"))
        painter.drawPath(base)

        painter.setPen(Qt.PenStyle.NoPen)
        gloss = QPainterPath()
        gloss.moveTo(63, 208)
        gloss.lineTo(237, 208)
        gloss.lineTo(251, 222)
        gloss.lineTo(52, 222)
        gloss.closeSubpath()
        painter.setBrush(QColor(255, 255, 255, 115))
        painter.drawPath(gloss)

        painter.setPen(QPen(QColor("#8ea0c4"), 1.3))
        cable = QPainterPath()
        cable.moveTo(150, 204)
        cable.cubicTo(151, 189, 175, 186, 190, 178)
        painter.drawPath(cable)

        labels = [
            ["Esc", "Q", "W", "E", "R", "T", "Y", "Del"],
            ["Tab", "A", "S", "D", "F", "G", "H"],
            ["Shift", "Z", "X", "C", "V", "B", "Enter"],
            ["Ctrl", "Alt", "Space", "Alt", "Fn"],
        ]
        y_positions = [211, 222, 233, 243]
        x_offsets = [65, 72, 59, 76]
        active_index = (self.frame // 3) % 16
        key_counter = 0
        for row, keys in enumerate(labels):
            x = x_offsets[row]
            for key in keys:
                width = 19
                if key in {"Shift", "Enter"}:
                    width = 34
                if key == "Space":
                    width = 59
                if key in {"Tab", "Ctrl", "Alt", "Del"}:
                    width = 26
                pressed = key_counter == active_index or key_counter == (active_index + 5) % 16
                self._draw_key(painter, QRectF(x, y_positions[row] + (2 if pressed else 0), width, 8), key, pressed)
                x += width + 5
                key_counter += 1

        painter.setPen(QPen(QColor("#243653"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(QColor("#f8fbff"))
        painter.drawEllipse(QRectF(84 + left_tap, 178, 42, 29))
        painter.drawEllipse(QRectF(174 + right_tap, 178, 42, 29))
        painter.setPen(QPen(QColor("#ff8cad"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(101 + left_tap, 204, 120 + left_tap, 212)
        painter.drawLine(197 + right_tap, 204, 180 + right_tap, 212)

    def _draw_key(self, painter: QPainter, rect: QRectF, label: str, pressed: bool) -> None:
        painter.setPen(QPen(QColor("#8997b7"), 1.0))
        painter.setBrush(QColor("#d8fbff") if pressed else QColor("#ffffff"))
        painter.drawRoundedRect(rect, 2.2, 2.2)
        painter.setPen(QPen(QColor("#48617f"), 0.9))
        font = QFont("Segoe UI", 4 if len(label) <= 2 else 3)
        font.setBold(label in {"Space", "Enter"})
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)


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


class TaskDialog(QDialog):
    def __init__(self, tasks: list[str], schedule: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("今日任务")
        self.resize(460, 360)

        layout = QVBoxLayout(self)
        title = QLabel("今日学习计划")
        title.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.Bold))
        title.setStyleSheet("color: #27385f;")
        layout.addWidget(title)

        content = QTextEdit(self)
        content.setReadOnly(True)
        content.setFont(QFont("Microsoft YaHei UI", 10))
        content.setStyleSheet(
            """
            QTextEdit {
                background: #ffffff;
                border: 2px solid #d5def3;
                border-radius: 14px;
                padding: 12px;
                color: #27385f;
            }
            """
        )
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
    def __init__(self) -> None:
        super().__init__()
        ensure_goal_plans()
        self.settings = load_settings()
        self.drag_position: QPoint | None = None
        self.was_dragged = False

        self.setWindowTitle("Table Miku")
        self.setFixedSize(300, 340)
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
        self.bubble.setGeometry(10, 6, 280, 92)
        self.bubble.setFont(QFont("Microsoft YaHei UI", 9))
        self.bubble.setStyleSheet(
            """
            QLabel {
                background: rgba(255, 255, 255, 238);
                border: 2px solid #27385f;
                border-radius: 18px;
                color: #27385f;
                padding: 10px;
                line-height: 150%;
            }
            """
        )
        self.bubble.hide()

        self.pet = TypingMiku(self)
        self.pet.setGeometry(0, 82, 300, 260)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.bubble.hide)

        self.reminders = ReminderManager(self)
        self.reminders.reminder.connect(self.say)
        self.reminders.start()

        self.say("Table Miku 已就位。我会边敲键盘边按时间提醒你。")

    def say(self, text: str) -> None:
        self.settings = load_settings()
        self.bubble.setText(self._bubble_text(text))
        self.bubble.show()
        seconds = int(self.settings.get("bubble_seconds", 7))
        self.hide_timer.start(max(seconds, 3) * 1000)

    @staticmethod
    def _bubble_text(text: str) -> str:
        compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
        return compact if len(compact) <= 72 else compact[:69] + "..."

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
                self.pet.random_expression()
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
        self.pet.set_expression("happy")
        tasks = today_tasks(load_goals())
        schedule = self._schedule_lines()
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
    app.setFont(QFont("Microsoft YaHei UI", 9))
    icon = QIcon(str(asset_path("miku.svg")))
    if not icon.isNull():
        app.setWindowIcon(icon)
    window = TableMiku()
    desktop = app.primaryScreen().availableGeometry()
    window.move(desktop.right() - window.width() - 32, desktop.bottom() - window.height() - 32)
    window.show()
    sys.exit(app.exec())
