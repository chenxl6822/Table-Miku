from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from table_miku.agent_center import AgentCenterDialog
from table_miku.agent_runtime import AgentRuntime
from table_miku.agent_store import AgentStore

from .test_agent_core import FakeBackend


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_agent_center_defaults_to_knowledge_and_review_only(tmp_path: Path):
    _app()
    runtime = AgentRuntime(store=AgentStore(tmp_path / "agent.db"), backend=FakeBackend())
    try:
        dialog = AgentCenterDialog(runtime)
        checked = {resource.value: checkbox.isChecked() for resource, checkbox in dialog.resource_checks.items()}
        assert checked == {
            "knowledge": True,
            "review": True,
            "goals": False,
            "timetable": False,
            "interviews": False,
        }
        assert dialog.current_session_id
    finally:
        runtime.shutdown()


def test_agent_center_renders_message_markdown(tmp_path: Path):
    _app()
    store = AgentStore(tmp_path / "agent.db")
    session_id = store.create_session()
    store.add_message(session_id, "assistant", "## 复习计划\n\n- Spring IoC\n- MySQL 索引")
    runtime = AgentRuntime(store=store, backend=FakeBackend())
    try:
        dialog = AgentCenterDialog(runtime)
        dialog.current_session_id = session_id
        dialog._render_messages()

        plain_text = dialog.chat.toPlainText()
        assert "## 复习计划" not in plain_text
        assert "复习计划" in plain_text
        assert "Spring IoC" in plain_text
        assert "<h2" in dialog.chat.toHtml().lower()
    finally:
        runtime.shutdown()
