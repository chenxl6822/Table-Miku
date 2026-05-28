from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal


@dataclass(frozen=True)
class CommandSpec:
    command: str
    cwd: Path


class WatchedCommand(QObject):
    finished_notice = Signal(str, str)

    def __init__(self, spec: CommandSpec, max_output_chars: int = 420, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.max_output_chars = max(max_output_chars, 120)
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(spec.cwd))
        self.process.setProgram("powershell.exe")
        self.process.setArguments(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", spec.command])
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._errored)

    def start(self) -> None:
        self.process.start()

    def _finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        output = self._combined_output()
        if exit_code == 0:
            level = "happy"
            message = f"命令跑完了：{self._short_command()}。退出码 0。"
        else:
            level = "surprised"
            message = f"命令结束但失败了：{self._short_command()}。退出码 {exit_code}。"
        if output:
            message = f"{message}\n{output}"
        self.finished_notice.emit(level, message)

    def _errored(self, _error: QProcess.ProcessError) -> None:
        self.finished_notice.emit("surprised", f"命令启动失败：{self._short_command()}。请检查路径或命令。")

    def _combined_output(self) -> str:
        chunks = [
            bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace").strip(),
            bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace").strip(),
        ]
        text = "\n".join(chunk for chunk in chunks if chunk)
        compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if len(compact) > self.max_output_chars:
            return compact[: self.max_output_chars - 3] + "..."
        return compact

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
