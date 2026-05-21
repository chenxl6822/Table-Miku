from __future__ import annotations

import math
import random
import sys

from PySide6.QtCore import QEasingCurve, QPoint, QRectF, Qt, QPropertyAnimation, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGraphicsOpacityEffect,
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
from .sprites import load_keyboard, load_sprite, sprite_source_hint
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


class TypingMiku(QWidget):
    """Compact desktop-pet sprite with a realistic typing keyboard overlay."""

    EXPRESSIONS = ["focus", "smile", "happy", "surprised", "sleepy"]
    EXPRESSION_TRAITS = {
        "focus": {"tempo": 1.05, "bounce": 2.0, "lift": 0.0, "scale": 1.0, "accent": "#43d9f5"},
        "smile": {"tempo": 0.9, "bounce": 2.4, "lift": -1.0, "scale": 1.01, "accent": "#4fd6c6"},
        "happy": {"tempo": 1.18, "bounce": 3.2, "lift": -2.5, "scale": 1.035, "accent": "#ff8cad"},
        "surprised": {"tempo": 1.32, "bounce": 2.8, "lift": -4.0, "scale": 1.045, "accent": "#ffd166"},
        "sleepy": {"tempo": 0.58, "bounce": 1.2, "lift": 1.5, "scale": 0.985, "accent": "#8ea0c4"},
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.frame = 0
        self.expression = "focus"
        self.previous_expression = "focus"
        self.transition_frame = 0
        self.transition_frames = 18
        self.click_pulse = 0.0
        self.particles: list[dict[str, object]] = []
        self.sprites: dict[str, QPixmap] = {}
        self.keyboard = load_keyboard()

        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(33)
        self.animation_timer.timeout.connect(self._next_frame)
        self.animation_timer.start()

        self.expression_timer = QTimer(self)
        self.expression_timer.setInterval(5_200)
        self.expression_timer.timeout.connect(self.random_expression)
        self.expression_timer.start()

    def set_expression(self, expression: str) -> None:
        next_expression = expression if expression in self.EXPRESSIONS else "focus"
        if next_expression == self.expression:
            return
        self.previous_expression = self.expression
        self.expression = next_expression
        self.transition_frame = self.transition_frames
        self._burst_for_expression(next_expression)
        self.update()

    def random_expression(self) -> None:
        choices = [expression for expression in self.EXPRESSIONS if expression != self.expression]
        self.set_expression(random.choice(choices))
        self.expression_timer.setInterval(random.randint(4_800, 7_200))

    def nudge(self) -> None:
        self.click_pulse = 1.0
        self.set_expression(random.choice(["happy", "smile", "surprised"]))
        self._burst_for_expression("happy", count=12)

    def _next_frame(self) -> None:
        self.frame = (self.frame + 1) % 10_000
        if self.transition_frame:
            self.transition_frame -= 1
        self.click_pulse = max(0.0, self.click_pulse - 0.055)
        self._update_particles()
        if self.expression in {"focus", "smile"} and self.frame % 9 == 0:
            self._spawn_particle("key", 92 + random.random() * 116, 202 + random.random() * 18)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        trait = self.EXPRESSION_TRAITS[self.expression]
        progress = self._transition_progress()
        pop = math.sin(progress * math.pi) * 0.018 if self.transition_frame else 0.0
        click_pop = math.sin(self.click_pulse * math.pi) * 0.025 if self.click_pulse else 0.0
        scale = float(trait["scale"]) + pop + click_pop
        bounce = math.sin(self.frame / (8.0 / float(trait["tempo"]))) * float(trait["bounce"]) + float(trait["lift"])
        left_tap = math.sin(self.frame / 3.2) * 4.4
        right_tap = math.sin(self.frame / 3.7 + 1.6) * 4.7
        blink = self.expression != "surprised" and self.frame % 145 in {0, 1, 2, 3}

        self._draw_shadow(painter, scale)
        self._draw_aura(painter, QColor(str(trait["accent"])))
        self._draw_particles(painter, behind=True)
        painter.save()
        painter.translate(0, bounce)
        painter.translate(self.width() / 2, 138)
        painter.scale(scale, scale)
        painter.translate(-self.width() / 2, -138)
        self._draw_sprite(painter, blink)
        self._draw_keyboard(painter, left_tap, right_tap)
        painter.restore()
        self._draw_particles(painter, behind=False)
        painter.end()

    def _draw_shadow(self, painter: QPainter, scale: float) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(22, 31, 54, 44))
        width = 214 * (1.02 - (scale - 1.0) * 1.8)
        painter.drawEllipse(QRectF((self.width() - width) / 2, 232, width, 26))

    def _draw_aura(self, painter: QPainter, color: QColor) -> None:
        painter.save()
        glow = QRadialGradient(self.width() / 2, 128, 114)
        color.setAlpha(46 if self.expression != "sleepy" else 28)
        glow.setColorAt(0.0, color)
        glow.setColorAt(0.55, QColor(color.red(), color.green(), color.blue(), 12))
        glow.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(34, 22, 232, 218))
        painter.restore()

    def _draw_sprite(self, painter: QPainter, blink: bool) -> None:
        pixmap = self._sprite_for_expression(self.expression)
        if pixmap.isNull():
            self._draw_missing_sprite_hint(painter)
            return

        width = 190 + (2 if self.expression == "surprised" else 0)
        height = 172
        x = (self.width() - width) / 2
        y = 16 + math.sin(self.frame / 11) * 1.5
        rect = QRectF(x, y, width, height).toRect()
        progress = self._transition_progress()
        old_pixmap = self._sprite_for_expression(self.previous_expression)
        if self.transition_frame and not old_pixmap.isNull():
            painter.save()
            painter.setOpacity(1.0 - progress)
            painter.drawPixmap(
                rect,
                old_pixmap.scaled(
                    int(width),
                    int(height),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ),
            )
            painter.restore()
        painter.save()
        painter.setOpacity(progress if self.transition_frame else 1.0)
        painter.drawPixmap(
            rect,
            pixmap.scaled(
                int(width),
                int(height),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ),
        )
        painter.restore()
        self._draw_expression_effects(painter, blink)

    def _sprite_for_expression(self, expression: str) -> QPixmap:
        key = expression if expression in {"happy", "focus", "surprised", "sleepy"} else "idle"
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
            float_y = math.sin(self.frame / 8) * 2
            painter.drawEllipse(QRectF(226, 48 + float_y, 18, 18))
            painter.drawEllipse(QRectF(241, 48 + float_y, 18, 18))
            heart = QPainterPath()
            heart.moveTo(243, 73 + float_y)
            heart.lineTo(225, 55 + float_y)
            heart.lineTo(260, 55 + float_y)
            heart.closeSubpath()
            painter.drawPath(heart)
        elif self.expression == "surprised":
            painter.setPen(QPen(QColor("#243653"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(223, 48, 235, 34)
            painter.drawLine(237, 55, 253, 47)
        elif self.expression == "sleepy":
            painter.setPen(QPen(QColor("#6d80a7"), 2))
            painter.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
            painter.drawText(QRectF(216, 36 + math.sin(self.frame / 12) * 3, 58, 28), Qt.AlignmentFlag.AlignCenter, "Zzz")
        elif self.expression == "focus":
            painter.setPen(QPen(QColor(67, 217, 245, 145), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(QRectF(57, 33, 186, 154), 22 * 16, 22 * 16)
            painter.drawArc(QRectF(57, 33, 186, 154), 136 * 16, 18 * 16)

    def _transition_progress(self) -> float:
        if not self.transition_frame:
            return 1.0
        linear = 1.0 - self.transition_frame / self.transition_frames
        return 1.0 - pow(1.0 - linear, 3)

    def _burst_for_expression(self, expression: str, count: int = 8) -> None:
        kinds = {
            "happy": "heart",
            "smile": "spark",
            "surprised": "spark",
            "sleepy": "bubble",
            "focus": "key",
        }
        kind = kinds.get(expression, "spark")
        for _ in range(count):
            self._spawn_particle(kind, 96 + random.random() * 112, 70 + random.random() * 54)

    def _spawn_particle(self, kind: str, x: float, y: float) -> None:
        palette = {
            "heart": QColor("#ff8cad"),
            "spark": QColor("#ffd166"),
            "bubble": QColor("#9fb1d8"),
            "key": QColor("#43d9f5"),
        }
        self.particles.append(
            {
                "kind": kind,
                "x": x,
                "y": y,
                "vx": random.uniform(-0.34, 0.34),
                "vy": random.uniform(-1.12, -0.28),
                "life": random.randint(24, 48),
                "max_life": 48,
                "size": random.uniform(3.4, 7.2),
                "color": palette.get(kind, QColor("#43d9f5")),
                "behind": kind == "key",
            }
        )
        if len(self.particles) > 42:
            self.particles = self.particles[-42:]

    def _update_particles(self) -> None:
        alive: list[dict[str, object]] = []
        for particle in self.particles:
            particle["x"] = float(particle["x"]) + float(particle["vx"])
            particle["y"] = float(particle["y"]) + float(particle["vy"])
            particle["vy"] = float(particle["vy"]) + 0.018
            particle["life"] = int(particle["life"]) - 1
            if int(particle["life"]) > 0:
                alive.append(particle)
        self.particles = alive

    def _draw_particles(self, painter: QPainter, behind: bool) -> None:
        painter.save()
        for particle in self.particles:
            if bool(particle.get("behind")) != behind:
                continue
            life = int(particle["life"])
            max_life = int(particle["max_life"])
            opacity = max(0.0, min(1.0, life / max_life))
            color = QColor(particle["color"])  # type: ignore[arg-type]
            color.setAlpha(int(color.alpha() * opacity))
            x = float(particle["x"])
            y = float(particle["y"])
            size = float(particle["size"]) * (0.72 + opacity * 0.34)
            painter.setOpacity(opacity)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            kind = str(particle["kind"])
            if kind == "heart":
                heart = QPainterPath()
                heart.moveTo(x, y + size)
                heart.cubicTo(x - size * 1.4, y - size * 0.2, x - size, y - size, x, y - size * 0.25)
                heart.cubicTo(x + size, y - size, x + size * 1.4, y - size * 0.2, x, y + size)
                painter.drawPath(heart)
            elif kind == "key":
                painter.drawRoundedRect(QRectF(x, y, size * 2.4, size), 2, 2)
            else:
                painter.drawEllipse(QRectF(x, y, size, size))
        painter.restore()

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

    def _draw_keyboard(self, painter: QPainter, left_tap: float, right_tap: float) -> None:
        if not self.keyboard.isNull():
            y = 168 + math.sin(self.frame / 9) * 0.8
            painter.save()
            painter.setOpacity(0.96)
            painter.drawPixmap(
                QRectF(24, y, 252, 88).toRect(),
                self.keyboard.scaled(
                    252,
                    88,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ),
            )
            painter.restore()
            painter.setPen(Qt.PenStyle.NoPen)
            for offset in (0, 5):
                glow_x = 68 + ((self.frame // 3 + offset) % 10) * 16
                painter.setBrush(QColor(130, 245, 255, 58 if offset else 96))
                painter.drawRoundedRect(QRectF(glow_x, y + 30 + offset * 0.8, 14, 9), 3, 3)
            return

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
                background: rgba(255, 255, 255, 246);
                border: 1px solid rgba(73, 99, 139, 210);
                border-radius: 20px;
                color: #27385f;
                padding: 11px 13px;
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

        self.pet = TypingMiku(self)
        self.pet.setGeometry(0, 82, 300, 260)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._hide_bubble)

        self.reminders = ReminderManager(self)
        self.reminders.reminder.connect(self.say)
        self.reminders.start()

        self.system_monitor = SystemMonitor(self)
        self.system_monitor.notice.connect(self._handle_system_notice)
        self.system_monitor.start()

        self.say("Table Miku 已就位。我会边敲键盘边按时间提醒你。")

    def say(self, text: str) -> None:
        self.settings = load_settings()
        self.bubble.setText(self._bubble_text(text))
        self.bubble.show()
        self._bubble_hiding = False
        self.bubble_animation.stop()
        self.bubble_animation.setStartValue(self.bubble_effect.opacity())
        self.bubble_animation.setEndValue(1.0)
        self.bubble_animation.start()
        seconds = int(self.settings.get("bubble_seconds", 7))
        self.hide_timer.start(max(seconds, 3) * 1000)

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
                self.pet.nudge()
                self.say(random.choice(CHAT_LINES))
            event.accept()

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
        toggle_monitor_action.triggered.connect(self.toggle_system_monitor)
        toggle_action.triggered.connect(self.toggle_reminders)
        quit_action.triggered.connect(QApplication.instance().quit)

        for action in (today_action, add_goal_action, schedule_action, weather_action, city_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(system_status_action)
        menu.addAction(toggle_monitor_action)
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
        self.say("我正在检测 CPU 和网络：百度、Google 都会试一下。")
        self.system_monitor.check_now()

    def toggle_system_monitor(self) -> None:
        self.settings = load_settings()
        monitor = self.settings.setdefault("system_monitor", {})
        enabled = not monitor.get("enabled", True)
        monitor["enabled"] = enabled
        save_settings(self.settings)
        self.pet.set_expression("smile" if enabled else "sleepy")
        if enabled:
            self.say("系统监测已开启。我会留意 CPU 和网络状态，发现异常会提醒你。")
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
    app.setFont(QFont("Microsoft YaHei UI", 9))
    icon = QIcon(str(asset_path("miku.svg")))
    if not icon.isNull():
        app.setWindowIcon(icon)
    window = TableMiku()
    desktop = app.primaryScreen().availableGeometry()
    window.move(desktop.right() - window.width() - 32, desktop.bottom() - window.height() - 32)
    window.show()
    sys.exit(app.exec())
