from __future__ import annotations

import urllib.request
from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPixmap

from .paths import PROJECT_ROOT, user_data_dir


SPRITE_DIR = PROJECT_ROOT / "assets" / "sprites"
SPRITE_FILENAMES = {
    "idle": "miku_idle.png",
    "happy": "miku_happy.png",
    "focus": "miku_focus.png",
    "surprised": "miku_surprised.png",
    "sleepy": "miku_sleepy.png",
}
SPRITE_SHEET = "miku_sprite_sheet.png"
SPRITE_ORDER = ["idle", "focus", "happy", "surprised", "sleepy"]

# Transparent chibi-style references found through web search. Most public PNG
# aggregation sites mark these as personal/non-commercial use, so local files
# in assets/sprites are preferred for redistribution.
REMOTE_SPRITE_URLS = [
    "https://www.pngkey.com/png/detail/573-5732891_hatsune-miku-chibi-png.png",
]

_download_attempted = False


def load_sprite(expression: str = "idle") -> QPixmap:
    expression = expression if expression in SPRITE_FILENAMES else "idle"
    sheet_pixmap = _load_from_sprite_sheet(expression)
    if not sheet_pixmap.isNull():
        return sheet_pixmap

    candidates = [
        SPRITE_DIR / SPRITE_FILENAMES[expression],
        SPRITE_DIR / SPRITE_FILENAMES["idle"],
        user_data_dir() / "sprites" / SPRITE_FILENAMES[expression],
        user_data_dir() / "sprites" / SPRITE_FILENAMES["idle"],
    ]

    for path in candidates:
        pixmap = _load_pixmap(path)
        if not pixmap.isNull():
            return pixmap

    downloaded = _download_first_available()
    if downloaded is not None:
        pixmap = _load_pixmap(downloaded)
        if not pixmap.isNull():
            return pixmap

    return QPixmap()


def sprite_source_hint() -> str:
    return "请把 AI 生成的五表情横向图放到 assets/sprites/miku_sprite_sheet.png，或放入 miku_idle.png。"


def _load_pixmap(path: Path) -> QPixmap:
    if not path.exists():
        return QPixmap()
    pixmap = QPixmap(str(path))
    return pixmap


def _load_from_sprite_sheet(expression: str) -> QPixmap:
    sheet_path = SPRITE_DIR / SPRITE_SHEET
    if not sheet_path.exists():
        return QPixmap()
    sheet = QPixmap(str(sheet_path))
    if sheet.isNull():
        return QPixmap()

    index = SPRITE_ORDER.index(expression) if expression in SPRITE_ORDER else 0
    frame_width = sheet.width() // len(SPRITE_ORDER)
    rect = QRect(index * frame_width, 0, frame_width, sheet.height())
    frame = sheet.copy(rect)
    return _trim_transparentized(frame)


def _trim_transparentized(pixmap: QPixmap) -> QPixmap:
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    image = _remove_checkerboard_background(image)
    image = _remove_edge_fragments(image)

    left = image.width()
    top = image.height()
    right = 0
    bottom = 0
    for y in range(image.height()):
        for x in range(image.width()):
            if QColor(image.pixelColor(x, y)).alpha() > 8:
                left = min(left, x)
                top = min(top, y)
                right = max(right, x)
                bottom = max(bottom, y)

    if right <= left or bottom <= top:
        return QPixmap.fromImage(image)
    return QPixmap.fromImage(image.copy(QRect(left, top, right - left + 1, bottom - top + 1)))


def _remove_edge_fragments(image: QImage) -> QImage:
    width = image.width()
    height = image.height()
    visited: set[tuple[int, int]] = set()
    components: list[tuple[int, bool, list[tuple[int, int]]]] = []

    for y in range(height):
        for x in range(width):
            if (x, y) in visited or QColor(image.pixelColor(x, y)).alpha() <= 8:
                continue
            pixels: list[tuple[int, int]] = []
            touches_edge = False
            queue = [(x, y)]
            while queue:
                px, py = queue.pop()
                if (px, py) in visited or px < 0 or py < 0 or px >= width or py >= height:
                    continue
                visited.add((px, py))
                if QColor(image.pixelColor(px, py)).alpha() <= 8:
                    continue
                pixels.append((px, py))
                touches_edge = touches_edge or px <= 2 or px >= width - 3
                queue.extend([(px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)])
            if pixels:
                components.append((len(pixels), touches_edge, pixels))

    if not components:
        return image

    largest = max(size for size, _, _ in components)
    for size, touches_edge, pixels in components:
        if touches_edge and size < largest * 0.45:
            for x, y in pixels:
                color = QColor(image.pixelColor(x, y))
                color.setAlpha(0)
                image.setPixelColor(x, y, color)

    return image


def _remove_checkerboard_background(image: QImage) -> QImage:
    """Make generated checkerboard pseudo-transparency transparent near edges."""
    width = image.width()
    height = image.height()
    visited = set()
    queue: list[tuple[int, int]] = []

    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))

    def is_background(color: QColor) -> bool:
        r, g, b = color.red(), color.green(), color.blue()
        return abs(r - g) <= 8 and abs(g - b) <= 8 and 214 <= r <= 255

    while queue:
        x, y = queue.pop()
        if (x, y) in visited or x < 0 or y < 0 or x >= width or y >= height:
            continue
        visited.add((x, y))
        color = QColor(image.pixelColor(x, y))
        if not is_background(color):
            continue
        color.setAlpha(0)
        image.setPixelColor(x, y, color)
        queue.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])

    return image


def _download_first_available() -> Path | None:
    global _download_attempted
    cache_dir = user_data_dir() / "sprites"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / SPRITE_FILENAMES["idle"]
    if target.exists():
        return target
    if _download_attempted:
        return None
    _download_attempted = True

    for url in REMOTE_SPRITE_URLS:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Table-Miku/0.3"})
            with urllib.request.urlopen(request, timeout=8) as response:
                data = response.read()
            if data:
                target.write_bytes(data)
                return target
        except OSError:
            continue
    return None
