from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

MAX_DIRECTORY_VISITS = 200
MAX_DIRECTORY_DEPTH = 3


@dataclass(frozen=True)
class BatchPathExpansion:
    files: tuple[Path, ...]
    skipped_unsupported: tuple[str, ...]
    error: str | None


class _ExpansionLimit(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def expand_batch_upload_paths(
    raw_paths: list[Path],
    *,
    suffixes: frozenset[str],
    max_files: int,
    max_visits: int = MAX_DIRECTORY_VISITS,
    max_depth: int = MAX_DIRECTORY_DEPTH,
) -> BatchPathExpansion:
    files: list[Path] = []
    seen: set[str] = set()
    skipped_unsupported: list[str] = []
    visits = 0

    def visit() -> None:
        nonlocal visits
        visits += 1
        if visits > max_visits:
            raise _ExpansionLimit("too_many_visits")

    def add_file(path: Path) -> None:
        if path.suffix.casefold() not in suffixes:
            skipped_unsupported.append(path.name)
            return
        key = str(path).casefold()
        if key in seen:
            return
        if len(files) >= max_files:
            raise _ExpansionLimit("too_many_files")
        seen.add(key)
        files.append(path)

    def walk_dir(directory: Path, depth: int) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise _ExpansionLimit("unreadable") from exc
        for child in children:
            visit()
            if child.name.startswith(".") or _is_link(child):
                continue
            if child.is_dir():
                child_depth = depth + 1
                if child_depth > max_depth:
                    raise _ExpansionLimit("directory_too_deep")
                walk_dir(child, child_depth)
            elif child.is_file():
                try:
                    add_file(child.resolve(strict=True))
                except OSError as exc:
                    raise _ExpansionLimit("unreadable") from exc

    try:
        for raw in raw_paths:
            visit()
            given = Path(raw)
            if given.name.startswith(".") or _is_link(given):
                continue
            try:
                resolved = given.resolve(strict=True)
            except OSError as orig:
                raise _ExpansionLimit("unreadable") from orig
            if resolved.is_dir():
                walk_dir(resolved, 0)
            elif resolved.is_file():
                add_file(resolved)
            else:
                raise _ExpansionLimit("unreadable")
    except _ExpansionLimit as exc:
        return BatchPathExpansion(files=(), skipped_unsupported=(), error=exc.code)
    return BatchPathExpansion(
        files=tuple(files),
        skipped_unsupported=tuple(skipped_unsupported),
        error=None,
    )
