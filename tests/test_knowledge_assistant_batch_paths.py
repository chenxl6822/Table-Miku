from __future__ import annotations

from pathlib import Path

from table_miku.knowledge_assistant_batch_paths import expand_batch_upload_paths
from table_miku.knowledge_assistant_desktop import MAX_BATCH_FILES
from table_miku.knowledge_assistant_ui import BATCH_UPLOAD_SUFFIXES


def _expand(paths: list[Path], **kwargs):
    return expand_batch_upload_paths(
        paths,
        suffixes=BATCH_UPLOAD_SUFFIXES,
        max_files=kwargs.pop("max_files", MAX_BATCH_FILES),
        **kwargs,
    )


def test_expand_nested_supported_files_and_skips_hidden_and_unsupported(tmp_path: Path):
    root = tmp_path / "docs"
    nested = root / "policy"
    nested.mkdir(parents=True)
    keep = nested / "leave.md"
    keep.write_text("keep", encoding="utf-8")
    (root / "notes.txt").write_text("notes", encoding="utf-8")
    (root / "skip.bin").write_text("nope", encoding="utf-8")
    (root / ".secret.md").write_text("hidden", encoding="utf-8")
    hidden_dir = root / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "also.md").write_text("hidden dir", encoding="utf-8")

    result = _expand([root])

    assert result.error is None
    names = {path.name for path in result.files}
    assert names == {"leave.md", "notes.txt"}
    assert "skip.bin" in result.skipped_unsupported


def test_expand_mixed_files_and_directory_dedupes(tmp_path: Path):
    folder = tmp_path / "folder"
    folder.mkdir()
    inside = folder / "a.md"
    inside.write_text("a", encoding="utf-8")
    outside = tmp_path / "b.md"
    outside.write_text("b", encoding="utf-8")

    result = _expand([folder, inside, outside])

    assert result.error is None
    assert {path.name for path in result.files} == {"a.md", "b.md"}


def test_expand_too_many_files_fails_closed(tmp_path: Path):
    folder = tmp_path / "many"
    folder.mkdir()
    for index in range(MAX_BATCH_FILES + 1):
        (folder / f"doc-{index:02d}.md").write_text("x", encoding="utf-8")

    result = _expand([folder])

    assert result.error == "too_many_files"
    assert result.files == ()


def test_expand_visit_limit_fails_closed(tmp_path: Path):
    folder = tmp_path / "busy"
    folder.mkdir()
    for index in range(5):
        (folder / f"doc-{index}.md").write_text("x", encoding="utf-8")

    result = _expand([folder], max_visits=3)

    assert result.error == "too_many_visits"
    assert result.files == ()


def test_expand_directory_too_deep_fails_closed(tmp_path: Path):
    current = tmp_path / "root"
    current.mkdir()
    for level in range(4):
        current = current / f"l{level}"
        current.mkdir()
    (current / "deep.md").write_text("deep", encoding="utf-8")

    result = _expand([tmp_path / "root"], max_depth=3)

    assert result.error == "directory_too_deep"
    assert result.files == ()


def test_expand_skips_symlink_without_following(tmp_path: Path, monkeypatch):
    folder = tmp_path / "folder"
    folder.mkdir()
    real = folder / "real.md"
    real.write_text("real", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = folder / "link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        link.write_text("should-not-import", encoding="utf-8")
        original = Path.is_symlink

        def fake_is_symlink(self: Path) -> bool:
            if self == link or self.resolve() == link.resolve():
                return True
            return original(self)

        monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    result = _expand([folder])

    assert result.error is None
    assert [path.name for path in result.files] == ["real.md"]
    assert outside.name not in {path.name for path in result.files}


def test_expand_unreadable_path_fails_closed(tmp_path: Path):
    missing = tmp_path / "missing.md"

    result = _expand([missing])

    assert result.error == "unreadable"
    assert result.files == ()
