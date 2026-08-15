from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from table_miku import knowledge_assistant_file_precheck as precheck_module
from table_miku.knowledge_assistant_file_precheck import FilePrecheckController
from table_miku.knowledge_assistant_ui import BatchUploadDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, *, timeout_ms: int = 5000) -> None:
    app = _app()
    loop = QEventLoop(app)
    timer = QTimer()
    timer.setInterval(20)
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    def tick() -> None:
        if predicate() or time.monotonic() >= deadline:
            loop.quit()

    timer.timeout.connect(tick)
    timer.start()
    loop.exec()
    timer.stop()
    assert predicate(), "condition not met before timeout"


def test_precheck_keeps_qt_event_loop_alive_with_slow_reader(tmp_path: Path):
    app = _app()
    source = tmp_path / "slow.md"
    source.write_bytes(b"x" * (512 * 1024))
    gate = threading.Event()
    ticks = {"count": 0}
    events: list[dict] = []

    original_read = precheck_module._default_read_chunk

    def slow_read(handle, size: int) -> bytes:
        gate.wait(timeout=2.0)
        time.sleep(0.05)
        return original_read(handle, size)

    controller = FilePrecheckController(read_chunk=slow_read)
    controller.progress.connect(lambda payload: events.append(dict(payload)))
    heartbeat = QTimer()
    heartbeat.setInterval(15)
    heartbeat.timeout.connect(lambda: ticks.__setitem__("count", ticks["count"] + 1))
    finished = QEventLoop(app)
    controller.finished.connect(lambda _payload: finished.quit())
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(finished.quit)
    try:
        heartbeat.start()
        controller.start([source], generation=7)
        assert ticks["count"] == 0
        # Prove the GUI thread keeps pumping while the worker is blocked.
        _wait_until(lambda: ticks["count"] >= 3, timeout_ms=2000)
        gate.set()
        timeout.start(8000)
        finished.exec()
        assert ticks["count"] >= 3, ticks
        assert any(item.get("phase") == "reading" for item in events)
        assert any(item.get("phase") == "ready" for item in events)
        assert controller.result.ready_snapshots[0]["sha256"] == hashlib.sha256(
            source.read_bytes()
        ).hexdigest()
        assert controller.result.generation == 7
    finally:
        heartbeat.stop()
        timeout.stop()
        controller.shutdown(2000)


def test_precheck_cancel_while_reading_creates_no_outbox_and_no_http(tmp_path: Path):
    app = _app()
    source = tmp_path / "cancel-me.md"
    source.write_bytes(b"y" * (256 * 1024))
    started = threading.Event()
    release = threading.Event()

    original_read = precheck_module._default_read_chunk

    def blocking_read(handle, size: int) -> bytes:
        started.set()
        release.wait(timeout=2.0)
        return original_read(handle, size)

    controller = FilePrecheckController(read_chunk=blocking_read)
    events: list[dict] = []
    controller.progress.connect(lambda payload: events.append(dict(payload)))
    finished = QEventLoop(app)
    controller.finished.connect(lambda _payload: finished.quit())
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(finished.quit)
    try:
        controller.start([source], generation=3)
        assert started.wait(timeout=2.0)
        controller.cancel()
        release.set()
        timeout.start(5000)
        finished.exec()
        assert any(item.get("phase") == "cancelled" for item in events)
        assert controller.result.cancelled is True
        assert controller.result.ready_snapshots == []
        assert controller.result.ready_paths == []
    finally:
        timeout.stop()
        controller.shutdown(2000)


def test_precheck_late_progress_from_old_generation_is_ignored(tmp_path: Path):
    app = _app()
    source = tmp_path / "late.md"
    source.write_text("late", encoding="utf-8")
    dialog = BatchUploadDialog()
    applied: list[int] = []

    def record(payload: dict) -> None:
        if int(payload.get("generation", -1)) == dialog._precheck_generation:
            applied.append(int(payload["generation"]))

    dialog._apply_precheck_progress = record  # type: ignore[method-assign]
    try:
        dialog._precheck_generation = 5
        dialog._on_precheck_progress(
            {
                "generation": 4,
                "phase": "ready",
                "index": 1,
                "total": 1,
                "path": str(source),
                "bytes_processed": 4,
                "error": None,
                "snapshot": None,
            }
        )
        dialog._on_precheck_progress(
            {
                "generation": 5,
                "phase": "reading",
                "index": 1,
                "total": 1,
                "path": str(source),
                "bytes_processed": 1,
                "error": None,
                "snapshot": None,
            }
        )
        assert applied == [5]
    finally:
        dialog.close()


def test_batch_dialog_precheck_is_async_and_shows_progress(tmp_path: Path, monkeypatch):
    app = _app()
    source = tmp_path / "guide.md"
    source.write_bytes(b"z" * (128 * 1024))
    gate = threading.Event()
    original_read = precheck_module._default_read_chunk

    def slow_read(handle, size: int) -> bytes:
        gate.wait(timeout=2.0)
        time.sleep(0.02)
        return original_read(handle, size)

    monkeypatch.setattr(precheck_module, "_default_read_chunk", slow_read)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(source)], ""),
    )
    dialog = BatchUploadDialog()
    ticks = {"count": 0}
    heartbeat = QTimer()
    heartbeat.setInterval(15)
    heartbeat.timeout.connect(lambda: ticks.__setitem__("count", ticks["count"] + 1))
    try:
        heartbeat.start()
        dialog._choose()
        assert dialog.submit_button.isEnabled() is False
        assert "正在校验" in dialog.count_label.text()
        # Hold the reader until the GUI heartbeat has clearly advanced.
        _wait_until(lambda: ticks["count"] >= 3, timeout_ms=2000)
        gate.set()
        _wait_until(lambda: dialog.submit_button.isEnabled() and len(dialog.file_snapshots) == 1)
        assert ticks["count"] >= 3, ticks
        assert dialog.file_snapshots[0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert dialog.submit_button.text() == "加入摄取队列（1）"
    finally:
        heartbeat.stop()
        dialog.close()


def test_batch_dialog_cancel_precheck_leaves_empty_selection(tmp_path: Path, monkeypatch):
    app = _app()
    source = tmp_path / "hold.md"
    source.write_bytes(b"h" * (256 * 1024))
    started = threading.Event()
    release = threading.Event()
    original_read = precheck_module._default_read_chunk

    def blocking_read(handle, size: int) -> bytes:
        started.set()
        release.wait(timeout=2.0)
        return original_read(handle, size)

    monkeypatch.setattr(precheck_module, "_default_read_chunk", blocking_read)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(source)], ""),
    )
    dialog = BatchUploadDialog()
    try:
        dialog._choose()
        assert started.wait(timeout=2.0)
        dialog._cancel_precheck()
        release.set()
        _wait_until(lambda: not dialog._precheck_busy)
        assert dialog.paths == []
        assert dialog.file_snapshots == []
        assert dialog.file_table.rowCount() == 0
        assert dialog.submit_button.isEnabled() is False
    finally:
        dialog.close()


def test_batch_dialog_excludes_failed_items_before_confirm(tmp_path: Path, monkeypatch):
    app = _app()
    good = tmp_path / "good.md"
    bad = tmp_path / "bad.md"
    good.write_text("ok", encoding="utf-8")
    bad.write_text("nope", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(good), str(bad)], ""),
    )

    original_build = precheck_module.build_file_snapshot

    def flaky_build(path: Path, **kwargs):
        if path.resolve() == bad.resolve():
            raise OSError("permission denied")
        return original_build(path, **kwargs)

    monkeypatch.setattr(precheck_module, "build_file_snapshot", flaky_build)
    dialog = BatchUploadDialog()
    try:
        dialog._choose()
        _wait_until(lambda: not dialog._precheck_busy and dialog.file_table.rowCount() >= 1)
        assert any("失败" in (dialog.count_label.text() or "") for _ in [0])
        assert dialog.submit_button.isEnabled() is False
        dialog._exclude_failed_prechecks()
        assert len(dialog.paths) == 1
        assert dialog.paths[0].resolve() == good.resolve()
        assert len(dialog.file_snapshots) == 1
        dialog.collection_edit.setText("engineering")
        dialog.accept()
        _wait_until(lambda: dialog.result() == QDialog.DialogCode.Accepted)
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_batch_dialog_accept_rejects_same_size_rewrite_without_gui_thread_hash(
    tmp_path: Path,
    monkeypatch,
):
    app = _app()
    source = tmp_path / "stable.md"
    source.write_bytes(b"first")
    original = source.stat()
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(source)], ""),
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = BatchUploadDialog()
    gui_thread_hashes = {"count": 0}
    real_sha = hashlib.sha256

    class CountingHash:
        def __init__(self, *args, **kwargs):
            self._inner = real_sha(*args, **kwargs)
            if (
                QApplication.instance() is not None
                and threading.current_thread() is threading.main_thread()
            ):
                gui_thread_hashes["count"] += 1

        def update(self, data: bytes) -> None:
            self._inner.update(data)

        def hexdigest(self) -> str:
            return self._inner.hexdigest()

    try:
        dialog._choose()
        _wait_until(lambda: len(dialog.file_snapshots) == 1)
        baseline = gui_thread_hashes["count"]
        monkeypatch.setattr(hashlib, "sha256", CountingHash)
        monkeypatch.setattr(precheck_module.hashlib, "sha256", CountingHash)
        source.write_bytes(b"other")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        dialog.accept()
        _wait_until(lambda: warnings and warnings[-1][0] == "文件已变化")
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert gui_thread_hashes["count"] == baseline
        assert dialog.submit_button.isEnabled() is True
    finally:
        dialog.close()


def test_precheck_unexpected_error_still_emits_finished(tmp_path: Path, monkeypatch):
    app = _app()
    source = tmp_path / "boom.md"
    source.write_text("boom", encoding="utf-8")

    def exploding_build(_path: Path, **_kwargs):
        raise RuntimeError("injected unexpected failure")

    monkeypatch.setattr(precheck_module, "build_file_snapshot", exploding_build)
    controller = FilePrecheckController()
    finished_payloads: list[object] = []
    finished = QEventLoop(app)
    controller.finished.connect(lambda payload: (finished_payloads.append(payload), finished.quit()))
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(finished.quit)
    try:
        controller.start([source], generation=9)
        timeout.start(5000)
        finished.exec()
        assert finished_payloads
        result = finished_payloads[-1]
        assert result.generation == 9
        assert result.failed
        assert result.failed[0]["error"] == "预检失败"
        assert controller.busy is False
    finally:
        timeout.stop()
        controller.shutdown(2000)


def test_batch_dialog_close_ignored_when_precheck_shutdown_fails(monkeypatch):
    _app()
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    dialog = BatchUploadDialog()
    monkeypatch.setattr(dialog._precheck, "shutdown", lambda _timeout_ms: False)
    try:
        from PySide6.QtGui import QCloseEvent

        event = QCloseEvent()
        dialog.closeEvent(event)
        assert event.isAccepted() is False
        assert warnings and warnings[-1][0] == "预检仍在停止"
    finally:
        # Instance monkeypatch must not leave a live QThread for GC.
        assert FilePrecheckController.shutdown(dialog._precheck, 2000)
        dialog.close()
