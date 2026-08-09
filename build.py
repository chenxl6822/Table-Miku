from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.__main__ import run as run_pyinstaller


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    os.chdir(PROJECT_ROOT)
    run_pyinstaller(
        [
            "--noconfirm",
            "--noconsole",
            "--name",
            "TableMiku",
            "--add-data",
            "assets;assets",
            "--add-data",
            "table_miku/qml;table_miku/qml",
            "main.py",
        ]
    )


if __name__ == "__main__":
    main()
