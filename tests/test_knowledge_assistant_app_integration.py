from __future__ import annotations

from types import SimpleNamespace

import table_miku.app as app_module


class _FakePet:
    def __init__(self) -> None:
        self.expressions: list[str] = []

    def set_expression(self, expression: str) -> None:
        self.expressions.append(expression)


def _host():
    messages: list[str] = []
    return SimpleNamespace(
        _knowledge_assistant_dialog=None,
        pet=_FakePet(),
        say=messages.append,
        messages=messages,
    )


def test_app_reuses_console_and_closes_endpoint_on_shutdown(monkeypatch):
    controllers = []
    dialogs = []

    class FakeController:
        def __init__(self) -> None:
            self.close_count = 0
            controllers.append(self)

        def close(self) -> None:
            self.close_count += 1

    class FakeDialog:
        def __init__(self, controller, parent) -> None:
            self.controller = controller
            self.parent = parent
            self.show_count = 0
            self.raise_count = 0
            self.activate_count = 0
            self.close_count = 0
            dialogs.append(self)

        def show(self) -> None:
            self.show_count += 1

        def raise_(self) -> None:
            self.raise_count += 1

        def activateWindow(self) -> None:
            self.activate_count += 1

        def close(self) -> None:
            self.close_count += 1

    monkeypatch.setattr(app_module, "KnowledgeAssistantDesktopController", FakeController)
    monkeypatch.setattr(app_module, "KnowledgeAssistantDialog", FakeDialog)
    host = _host()

    app_module.TableMiku.show_knowledge_assistant(host)
    app_module.TableMiku.show_knowledge_assistant(host)

    assert len(controllers) == 1
    assert len(dialogs) == 1
    assert dialogs[0].show_count == 2
    assert dialogs[0].raise_count == 2
    assert dialogs[0].activate_count == 2

    app_module.TableMiku._shutdown_knowledge_assistant(host)

    assert controllers[0].close_count == 1
    assert dialogs[0].close_count == 1
    assert host._knowledge_assistant_dialog is None


def test_app_cleans_controller_when_console_construction_fails(monkeypatch):
    controllers = []

    class FakeController:
        def __init__(self) -> None:
            self.close_count = 0
            controllers.append(self)

        def close(self) -> None:
            self.close_count += 1

    class FailingDialog:
        def __init__(self, _controller, _parent) -> None:
            raise RuntimeError("synthetic dialog failure")

    monkeypatch.setattr(app_module, "KnowledgeAssistantDesktopController", FakeController)
    monkeypatch.setattr(app_module, "KnowledgeAssistantDialog", FailingDialog)
    host = _host()

    app_module.TableMiku.show_knowledge_assistant(host)

    assert len(controllers) == 1
    assert controllers[0].close_count == 1
    assert host._knowledge_assistant_dialog is None
    assert host.pet.expressions == ["surprised"]
    assert host.messages == ["企业知识助手管理台启动失败：synthetic dialog failure"]


def test_app_keeps_console_alive_when_ingestion_worker_cannot_stop_safely():
    host = _host()

    class Controller:
        close_count = 0

        def close(self) -> bool:
            self.close_count += 1
            return False

    class Dialog:
        def __init__(self) -> None:
            self.controller = Controller()
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    dialog = Dialog()
    host._knowledge_assistant_dialog = dialog

    result = app_module.TableMiku._shutdown_knowledge_assistant(host)

    assert result is False
    assert dialog.controller.close_count == 1
    assert dialog.close_count == 0
    assert host._knowledge_assistant_dialog is dialog
    assert host.pet.expressions == ["surprised"]
    assert "后台摄取仍在结束" in host.messages[-1]


def test_app_keeps_console_alive_when_shutdown_raises():
    host = _host()

    class Controller:
        @staticmethod
        def close() -> bool:
            raise RuntimeError("synthetic shutdown failure")

    class Dialog:
        controller = Controller()
        close_count = 0

        def close(self) -> None:
            self.close_count += 1

    dialog = Dialog()
    host._knowledge_assistant_dialog = dialog

    result = app_module.TableMiku._shutdown_knowledge_assistant(host)

    assert result is False
    assert dialog.close_count == 0
    assert host._knowledge_assistant_dialog is dialog
    assert host.pet.expressions == ["surprised"]
    assert "synthetic shutdown failure" in host.messages[-1]


def test_quit_is_not_requested_until_knowledge_assistant_stops(monkeypatch):
    host = _host()
    quit_calls: list[str] = []
    fake_app = SimpleNamespace(quit=lambda: quit_calls.append("quit"))
    monkeypatch.setattr(app_module.QApplication, "instance", lambda: fake_app)
    host._shutdown_knowledge_assistant = lambda: False

    assert app_module.TableMiku._request_quit(host) is False
    assert quit_calls == []

    host._shutdown_knowledge_assistant = lambda: True
    assert app_module.TableMiku._request_quit(host) is True
    assert quit_calls == ["quit"]


class _FakeTray:
    def __init__(self, *, supports_messages: bool = True) -> None:
        self.supports_messages = supports_messages
        self.messages: list[tuple[str, str]] = []

    def supportsMessages(self) -> bool:
        return self.supports_messages

    def showMessage(self, title: str, message: str, *args, **kwargs) -> None:
        if not self.supports_messages:
            raise AssertionError("tray messages are unsupported")
        self.messages.append((title, message))


def test_app_shows_tray_balloon_and_opens_inbox_on_click():
    host = _host()
    tray = _FakeTray()
    host.tray_icon = tray
    opened: list[str] = []
    inbox: list[str] = []

    class Dialog:
        def open_approval_inbox(self) -> None:
            inbox.append("inbox")

    host._knowledge_assistant_dialog = Dialog()
    host.show_knowledge_assistant = lambda: opened.append("show")

    app_module.TableMiku._show_approval_tray_message(host, "企业知识助手", "待我审批 1 个。")
    assert tray.messages == [("企业知识助手", "待我审批 1 个。")]
    assert host._approval_tray_awaiting_click is True

    app_module.TableMiku._on_approval_tray_clicked(host)
    assert opened == ["show"]
    assert inbox == ["inbox"]
    assert host._approval_tray_awaiting_click is False

    app_module.TableMiku._on_approval_tray_clicked(host)
    assert opened == ["show"]
    assert inbox == ["inbox"]


def test_app_skips_tray_when_messages_unsupported():
    host = _host()
    host.tray_icon = _FakeTray(supports_messages=False)
    app_module.TableMiku._show_approval_tray_message(host, "企业知识助手", "待我审批 1 个。")
    assert getattr(host, "_approval_tray_awaiting_click", False) is False


def test_app_binds_approval_tray_when_opening_console(monkeypatch):
    class FakeSignal:
        def __init__(self) -> None:
            self.slots: list[object] = []

        def connect(self, slot) -> None:
            self.slots.append(slot)

    class FakeController:
        def close(self) -> None:
            return None

    class FakeDialog:
        def __init__(self, controller, parent) -> None:
            self.controller = controller
            self.parent = parent
            self.approval_tray_requested = FakeSignal()
            self.show_count = 0

        def show(self) -> None:
            self.show_count += 1

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(app_module, "KnowledgeAssistantDesktopController", FakeController)
    monkeypatch.setattr(app_module, "KnowledgeAssistantDialog", FakeDialog)
    host = _host()
    host.tray_icon = _FakeTray()
    app_module.TableMiku.show_knowledge_assistant(host)
    dialog = host._knowledge_assistant_dialog
    assert len(dialog.approval_tray_requested.slots) == 1
    dialog.approval_tray_requested.slots[0]("企业知识助手", "待我审批 1 个。")
    assert host.tray_icon.messages == [("企业知识助手", "待我审批 1 个。")]
    assert host._approval_tray_awaiting_click is True
