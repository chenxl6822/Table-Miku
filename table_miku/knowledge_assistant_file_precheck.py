from __future__ import annotations

import hashlib
import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .knowledge_assistant.documents import MAX_DOCUMENT_BYTES

ReadChunk = Callable[[Any, int], bytes]
CancelCheck = Callable[[], bool]
BytesCallback = Callable[[int, int], None]


def _default_read_chunk(handle: Any, size: int) -> bytes:
    return handle.read(size)


@dataclass
class PrecheckBatchResult:
    generation: int
    ready_paths: list[Path] = field(default_factory=list)
    ready_snapshots: list[dict[str, int | str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    cancelled: bool = False


def build_file_snapshot(
    path: Path,
    *,
    max_bytes: int = MAX_DOCUMENT_BYTES,
    chunk_size: int = 1024 * 1024,
    should_cancel: CancelCheck | None = None,
    on_bytes: BytesCallback | None = None,
    read_chunk: ReadChunk | None = None,
) -> tuple[Path, dict[str, int | str]]:
    reader = read_chunk or _default_read_chunk
    resolved = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("请选择普通文件")
        if before.st_size > max_bytes:
            raise ValueError(f"文件超过 {max_bytes} 字节上限")
        bytes_read = 0
        while True:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("precheck cancelled")
            chunk = reader(handle, chunk_size)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                raise ValueError(f"文件超过 {max_bytes} 字节上限")
            digest.update(chunk)
            if on_bytes is not None:
                on_bytes(bytes_read, int(before.st_size))
        after = os.fstat(handle.fileno())
    before_identity = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
    )
    after_identity = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
    )
    if before_identity != after_identity or bytes_read != int(after.st_size):
        raise ValueError("文件在建立快照时发生变化")
    return resolved, {
        "canonical_path": str(resolved),
        "size": int(after.st_size),
        "mtime_ns": int(after.st_mtime_ns),
        "device": int(after.st_dev),
        "inode": int(after.st_ino),
        "sha256": digest.hexdigest(),
    }


class _FilePrecheckWorker(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, *, read_chunk: ReadChunk | None = None) -> None:
        super().__init__()
        self._read_chunk = read_chunk
        self._cancel = threading.Event()
        self._active_generation = 0

    def request_cancel(self) -> None:
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def _cancelled(self) -> bool:
        return self._cancel.is_set() or QThread.currentThread().isInterruptionRequested()

    @Slot(object)
    def run_batch(self, command: dict[str, Any]) -> None:
        generation = int(command.get("generation", 0))
        paths = [Path(item) for item in command.get("paths", [])]
        self._active_generation = generation
        self._cancel.clear()
        result = PrecheckBatchResult(generation=generation)
        try:
            total = len(paths)
            for index, path in enumerate(paths, start=1):
                if self._cancelled():
                    result.cancelled = True
                    self.progress.emit(
                        {
                            "generation": generation,
                            "phase": "cancelled",
                            "index": index,
                            "total": total,
                            "path": str(path),
                            "bytes_processed": 0,
                            "error": None,
                            "snapshot": None,
                        }
                    )
                    break
                self.progress.emit(
                    {
                        "generation": generation,
                        "phase": "reading",
                        "index": index,
                        "total": total,
                        "path": str(path),
                        "bytes_processed": 0,
                        "error": None,
                        "snapshot": None,
                    }
                )

                def on_bytes(processed: int, _size: int, *, _index: int = index) -> None:
                    self.progress.emit(
                        {
                            "generation": generation,
                            "phase": "reading",
                            "index": _index,
                            "total": total,
                            "path": str(path),
                            "bytes_processed": processed,
                            "error": None,
                            "snapshot": None,
                        }
                    )

                try:
                    resolved, snapshot = build_file_snapshot(
                        path,
                        should_cancel=self._cancelled,
                        on_bytes=on_bytes,
                        read_chunk=self._read_chunk,
                    )
                except InterruptedError:
                    result.cancelled = True
                    self.progress.emit(
                        {
                            "generation": generation,
                            "phase": "cancelled",
                            "index": index,
                            "total": total,
                            "path": str(path),
                            "bytes_processed": 0,
                            "error": None,
                            "snapshot": None,
                        }
                    )
                    break
                except (OSError, ValueError) as exc:
                    failure = {"path": str(path), "error": str(exc)}
                    result.failed.append(failure)
                    self.progress.emit(
                        {
                            "generation": generation,
                            "phase": "failed",
                            "index": index,
                            "total": total,
                            "path": str(path),
                            "bytes_processed": 0,
                            "error": str(exc),
                            "snapshot": None,
                        }
                    )
                    continue
                result.ready_paths.append(resolved)
                result.ready_snapshots.append(snapshot)
                self.progress.emit(
                    {
                        "generation": generation,
                        "phase": "ready",
                        "index": index,
                        "total": total,
                        "path": str(resolved),
                        "bytes_processed": int(snapshot["size"]),
                        "error": None,
                        "snapshot": dict(snapshot),
                    }
                )
        except Exception:
            result.failed.append({"path": "", "error": "预检失败"})
            self.progress.emit(
                {
                    "generation": generation,
                    "phase": "failed",
                    "index": 0,
                    "total": len(paths),
                    "path": "",
                    "bytes_processed": 0,
                    "error": "预检失败",
                    "snapshot": None,
                }
            )
        finally:
            if self._cancelled():
                result.cancelled = True
            self.finished.emit(result)


class FilePrecheckController(QObject):
    progress = Signal(object)
    finished = Signal(object)
    _run = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        read_chunk: ReadChunk | None = None,
    ) -> None:
        super().__init__(parent)
        self.result = PrecheckBatchResult(generation=0)
        self._busy = False
        self._shutting_down = False
        self._pending: dict[str, Any] | None = None
        self._thread = QThread(self)
        self._worker = _FilePrecheckWorker(read_chunk=read_chunk)
        self._worker.moveToThread(self._thread)
        self._run.connect(self._worker.run_batch)
        self._worker.progress.connect(self.progress.emit)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self, paths: list[Path], *, generation: int) -> None:
        if self._shutting_down:
            raise RuntimeError("file precheck controller is shutting down")
        command = {
            "generation": int(generation),
            "paths": [str(path) for path in paths],
        }
        if self._busy:
            self._pending = command
            self.cancel()
            return
        self._begin(command)

    def _begin(self, command: dict[str, Any]) -> None:
        self.result = PrecheckBatchResult(generation=int(command["generation"]))
        self._busy = True
        self._worker.reset_cancel()
        self._run.emit(command)

    def cancel(self) -> None:
        self._worker.request_cancel()

    @Slot(object)
    def _on_finished(self, result: PrecheckBatchResult) -> None:
        self.result = result
        pending = self._pending
        self._pending = None
        if pending is not None and not self._shutting_down:
            self.finished.emit(result)
            self._begin(pending)
            return
        self._busy = False
        self.finished.emit(result)

    def shutdown(self, timeout_ms: int) -> bool:
        if not self._thread.isRunning():
            self._busy = False
            return True
        previous_shutting_down = self._shutting_down
        self._shutting_down = True
        self._pending = None
        self._worker.request_cancel()
        self._thread.requestInterruption()
        self._thread.quit()
        finished = self._thread.wait(max(0, int(timeout_ms)))
        if not finished:
            # Keep the controller usable and the dialog open; destroying a live
            # QThread is fail-open and can crash the process.
            self._shutting_down = previous_shutting_down
            return False
        self._busy = False
        return True
