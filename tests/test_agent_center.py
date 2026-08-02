from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

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
        assert all("（只读）" in checkbox.text() for checkbox in dialog.resource_checks.values())
        capability_buttons = [
            button for button in dialog.findChildren(QPushButton) if button.text() == "测试 DeepSeek Agent 能力"
        ]
        assert len(capability_buttons) == 1
        assert dialog.topology_evaluation_button.text() == "对比单/多 Agent 质量"
        assert not dialog.topology_evaluation_button.isEnabled()
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


def test_agent_center_shows_capability_result_and_recovers_from_failure(tmp_path: Path):
    _app()
    runtime = AgentRuntime(store=AgentStore(tmp_path / "agent.db"), backend=FakeBackend())
    try:
        dialog = AgentCenterDialog(runtime)
        result = {
            "base_url": "https://api.deepseek.test",
            "model": "chat-test",
            "chat_completion": True,
            "function_tool": True,
            "json_arguments": True,
            "argument_validation": True,
            "multi_agent_capable": True,
            "request_count": 1,
            "tested_at": "2026-08-02T12:00:00",
        }

        dialog.capability_button.setEnabled(False)
        dialog._capability_ready(result)
        assert dialog.capability_button.isEnabled()
        assert dialog.topology_evaluation_button.isEnabled()
        assert "Chat Completion：通过" in dialog.capability_result.toPlainText()
        assert "chat-test" in dialog.capability_result.toPlainText()
        assert dialog.status.text() == "能力测试通过，可运行单/多 Agent 质量对比"

        evaluation = {
            "base_url": "https://api.deepseek.test",
            "model": "chat-test",
            "single_score": 82,
            "multi_score": 94,
            "routing_passed": True,
            "passed": True,
            "reason": "多 Agent 达到质量门槛且严格优于单 Agent。",
            "request_limit": 12,
            "evaluated_at": "2026-08-02T15:00:00",
            "cases": [],
        }
        dialog.topology_evaluation_button.setEnabled(False)
        dialog.send_button.setEnabled(False)
        dialog.stop_button.setEnabled(True)
        dialog._topology_evaluation_ready(evaluation)
        assert dialog.topology_evaluation_button.isEnabled()
        assert dialog.send_button.isEnabled()
        assert not dialog.stop_button.isEnabled()
        assert "单 Agent：82 分" in dialog.topology_evaluation_result.toPlainText()
        assert "专家协作：已启用" in dialog.topology_evaluation_result.toPlainText()
        assert dialog.status.text() == "多 Agent 评测胜出，已启用专家协作"

        dialog.capability_button.setEnabled(False)
        dialog._capability_failed("DeepSeek API 连接超时；本次不会自动重试。")
        assert dialog.capability_button.isEnabled()
        assert not dialog.topology_evaluation_button.isEnabled()
        assert "结果：失败" in dialog.capability_result.toPlainText()
        assert "连接超时" in dialog.status.text()
    finally:
        runtime.shutdown()
