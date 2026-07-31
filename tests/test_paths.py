from __future__ import annotations

from pathlib import Path

import pytest

from table_miku import paths


def test_user_data_dir_honors_explicit_override(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("TABLE_MIKU_DATA_DIR", str(runtime_dir))

    assert paths.user_data_dir() == runtime_dir.resolve()
    assert runtime_dir.is_dir()


def test_runtime_path_copies_legacy_file_only_once(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    legacy_file = project_root / "data" / "settings.json"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text('{"city": "legacy"}', encoding="utf-8")

    appdata = tmp_path / "appdata"
    monkeypatch.delenv("TABLE_MIKU_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(paths, "PROJECT_ROOT", project_root)

    target = paths.runtime_path("settings.json")
    assert target == appdata / "TableMiku" / "settings.json"
    assert target.read_text(encoding="utf-8") == '{"city": "legacy"}'

    legacy_file.write_text('{"city": "changed"}', encoding="utf-8")
    assert paths.runtime_path("settings.json").read_text(encoding="utf-8") == '{"city": "legacy"}'


@pytest.mark.parametrize("filename", ["../settings.json", "nested/../../settings.json"])
def test_runtime_path_rejects_parent_traversal(filename, tmp_path, monkeypatch):
    monkeypatch.setenv("TABLE_MIKU_DATA_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="Invalid runtime filename"):
        paths.runtime_path(filename)
