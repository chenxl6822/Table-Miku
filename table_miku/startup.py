from __future__ import annotations

import os
import sys
from pathlib import Path

from .paths import PROJECT_ROOT


STARTUP_SCRIPT = "TableMiku.cmd"


def startup_script_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_SCRIPT


def is_startup_enabled() -> bool:
    return startup_script_path().exists()


def set_startup_enabled(enabled: bool) -> Path:
    path = startup_script_path()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_startup_script(), encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return path


def _startup_script() -> str:
    if getattr(sys, "frozen", False):
        command = f'start "" "{sys.executable}"'
    else:
        command = f'start "" "{sys.executable}" "{PROJECT_ROOT / "main.py"}"'
    return "@echo off\n" f"cd /d \"{PROJECT_ROOT}\"\n" f"{command}\n"
