from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from table_miku.ai_consent import AIConsentChoice, AIConsentDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _dialog() -> AIConsentDialog:
    _app()
    return AIConsentDialog(
        provider="OpenAI",
        model="gpt-test",
        endpoint="https://api.example.test/v1/responses",
    )


def test_consent_dialog_defaults_to_safe_cancel():
    dialog = _dialog()
    cancel = next(button for button in dialog.findChildren(QPushButton) if button.text() == "暂不启用")

    assert cancel.isDefault()
    assert dialog.choice is None
    dialog.reject()
    assert dialog.choice is None


def test_single_use_choice_is_not_standing_authority():
    dialog = _dialog()

    dialog._accept(AIConsentChoice.ONCE)

    assert dialog.choice == AIConsentChoice.ONCE
