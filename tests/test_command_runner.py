from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from table_miku.command_runner import CommandSpec, WatchedCommand, parse_command_spec


def _app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def test_watched_command_uses_standard_powershell_policy(tmp_path):
    _app()
    command = WatchedCommand(
        CommandSpec(command="Write-Output ok", cwd=tmp_path),
        timeout_seconds=42,
    )

    arguments = command.process.arguments()
    assert "-NonInteractive" in arguments
    assert "-ExecutionPolicy" not in arguments
    assert "Bypass" not in arguments
    assert command._timeout_timer.interval() == 42_000


def test_watched_command_keeps_a_bounded_output_tail(tmp_path):
    _app()
    command = WatchedCommand(CommandSpec(command="noop", cwd=tmp_path), max_output_chars=120)

    command._append_output("old-" + "a" * 600)
    command._append_output("-latest")

    output = command._combined_output()
    assert len(output) == 120
    assert output.startswith("...")
    assert output.endswith("-latest")


def test_parse_command_spec_supports_relative_working_directory(tmp_path):
    spec = parse_command_spec("cwd=project\nWrite-Output ok", tmp_path)

    assert spec is not None
    assert spec.cwd == tmp_path / "project"
    assert spec.command == "Write-Output ok"
