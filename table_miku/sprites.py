from __future__ import annotations

import urllib.request
from pathlib import Path

from PySide6.QtGui import QPixmap

from .paths import PROJECT_ROOT, user_data_dir


SPRITE_DIR = PROJECT_ROOT / "assets" / "sprites"
SPRITE_FILENAMES = {
    "idle": "miku_idle.png",
    "happy": "miku_happy.png",
    "focus": "miku_focus.png",
    "surprised": "miku_surprised.png",
    "sleepy": "miku_sleepy.png",
}

# Transparent chibi-style references found through web search. Most public PNG
# aggregation sites mark these as personal/non-commercial use, so local files
# in assets/sprites are preferred for redistribution.
REMOTE_SPRITE_URLS = [
    "https://www.pngkey.com/png/detail/573-5732891_hatsune-miku-chibi-png.png",
]

_download_attempted = False


def load_sprite(expression: str = "idle") -> QPixmap:
    expression = expression if expression in SPRITE_FILENAMES else "idle"
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
    return "请把透明 PNG 放到 assets/sprites/miku_idle.png，或保持联网让程序尝试下载参考精灵图。"


def _load_pixmap(path: Path) -> QPixmap:
    if not path.exists():
        return QPixmap()
    pixmap = QPixmap(str(path))
    return pixmap


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
