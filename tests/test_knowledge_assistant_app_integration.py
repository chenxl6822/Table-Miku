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
