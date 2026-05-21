from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent


def bundled_root() -> Path:
    """Return the root that contains bundled read-only assets."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return PROJECT_ROOT


def asset_path(name: str) -> Path:
    return bundled_root() / "assets" / name


def qml_path(name: str) -> Path:
    return bundled_root() / "table_miku" / "qml" / name


def user_data_dir() -> Path:
    """Store editable data beside source in dev, and in AppData when packaged."""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home()))
        path = base / "TableMiku"
    else:
        path = PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path
