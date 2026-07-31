from __future__ import annotations

import importlib
import os
import pkgutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import table_miku


def test_all_production_modules_import_without_side_effect_errors():
    module_names = sorted(
        module_info.name
        for module_info in pkgutil.iter_modules(table_miku.__path__, f"{table_miku.__name__}.")
    )

    imported = [importlib.import_module(module_name).__name__ for module_name in module_names]

    assert imported == module_names
