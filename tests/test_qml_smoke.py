from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtQuickWidgets import QQuickWidget

from table_miku.paths import qml_path


def test_pet_scene_qml_loads_without_errors():
    app = QApplication.instance() or QApplication([])
    widget = QQuickWidget()

    widget.setSource(QUrl.fromLocalFile(str(qml_path("PetScene.qml"))))

    errors = "\n".join(error.toString() for error in widget.errors())
    assert widget.status() == QQuickWidget.Status.Ready, errors
    assert widget.rootObject() is not None
    widget.deleteLater()
    app.processEvents()
