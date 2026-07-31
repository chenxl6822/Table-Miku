from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent
_runtime_migration_lock = threading.Lock()


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
    """Return the writable runtime-data directory.

    Runtime state is always kept outside the source/build tree. Tests and
    portable diagnostics may override it with ``TABLE_MIKU_DATA_DIR``.
    """
    override = os.environ.get("TABLE_MIKU_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    else:
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        path = base / "TableMiku"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_path(filename: str, *, migrate_legacy: bool = True) -> Path:
    """Return a runtime file path and copy a legacy dev file once if needed."""
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Invalid runtime filename: {filename}")

    target = user_data_dir() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if (
        not migrate_legacy
        or target.exists()
        or os.environ.get("TABLE_MIKU_DATA_DIR", "").strip()
        or getattr(sys, "frozen", False)
    ):
        return target

    legacy = PROJECT_ROOT / "data" / relative
    if not legacy.is_file():
        return target

    with _runtime_migration_lock:
        if target.exists():
            return target
        temporary = target.with_name(target.name + ".migration.tmp")
        shutil.copy2(legacy, temporary)
        temporary.replace(target)
    return target
