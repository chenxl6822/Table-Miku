from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal


@dataclass(frozen=True)
class CommandSpec:
    command: str
    cwd: Path


class WatchedCommand(QObject):
    finished_notice = Signal(str, str)

    def __init__(
        self,
        spec: CommandSpec,
        max_output_chars: int = 420,
        timeout_seconds: int = 600,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self.max_output_chars = max(max_output_chars, 120)
        self.audit_id = uuid.uuid4().hex[:12]
        self._output_tail = ""
        self._cancelled = False
        self._timed_out = False
        self._notified = False
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(spec.cwd))
        self.process.setProgram("powershell.exe")
        self.process.setArguments(["-NoProfile", "-NonInteractive", "-Command", spec.command])
        self.process.readyReadStandardOutput.connect(self._capture_output)
        self.process.readyReadStandardError.connect(self._capture_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._errored)
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(max(int(timeout_seconds), 1) * 1000)
        self._timeout_timer.timeout.connect(self._timeout)

    def start(self) -> None:
        self.process.start()
        self._timeout_timer.start()

    def cancel(self) -> bool:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return False
        self._cancelled = True
        self._stop_process()
        return True

    def _finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._timeout_timer.stop()
        output = self._combined_output()
        if self._timed_out:
            level = "surprised"
            message = f"命令超时并已停止：{self._short_command()}。"
        elif self._cancelled:
            level = "focus"
            message = f"命令已取消：{self._short_command()}。"
        elif exit_code == 0:
            level = "happy"
            message = f"命令跑完了：{self._short_command()}。退出码 0。"
        else:
            level = "surprised"
            message = f"命令结束但失败了：{self._short_command()}。退出码 {exit_code}。"
        if output:
            message = f"{message}\n{output}"
        self._emit_once(level, message)

    def _errored(self, _error: QProcess.ProcessError) -> None:
        if self._cancelled or self._timed_out:
            return
        self._timeout_timer.stop()
        self._emit_once("surprised", f"命令启动失败：{self._short_command()}。请检查路径或命令。")

    def _capture_output(self) -> None:
        chunks = [
            bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace"),
            bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace"),
        ]
        for chunk in chunks:
            if chunk:
                self._append_output(chunk)

    def _append_output(self, text: str) -> None:
        self._output_tail = (self._output_tail + text)[-self.max_output_chars * 4 :]

    def _combined_output(self) -> str:
        self._capture_output()
        compact = " ".join(line.strip() for line in self._output_tail.splitlines() if line.strip())
        if len(compact) > self.max_output_chars:
            return "..." + compact[-self.max_output_chars + 3 :]
        return compact

    def _timeout(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._timed_out = True
        self._stop_process()

    def _stop_process(self) -> None:
        self._timeout_timer.stop()
        self.process.terminate()
        QTimer.singleShot(3000, self._kill_if_running)

    def _kill_if_running(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()

    def _emit_once(self, level: str, message: str) -> None:
        if self._notified:
            return
        self._notified = True
        self.finished_notice.emit(level, message)

    def _short_command(self) -> str:
        command = " ".join(self.spec.command.split())
        return command if len(command) <= 34 else command[:31] + "..."


def parse_command_spec(raw: str, default_cwd: Path) -> CommandSpec | None:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None

    cwd = default_cwd
    if lines[0].lower().startswith("cwd="):
        maybe_cwd = Path(lines.pop(0)[4:].strip().strip('"'))
        cwd = maybe_cwd if maybe_cwd.is_absolute() else default_cwd / maybe_cwd
    command = "\n".join(lines).strip()
    if not command:
        return None
    return CommandSpec(command=command, cwd=cwd)
