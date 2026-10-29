"""Targeted UI state regressions. No network requests, C1 changes or saved credentials."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
pytest.importorskip('PySide6')
pytest.importorskip('openai')

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from c1ai import ai_window


def test_new_request_drops_old_preview_and_key_change_drops_capabilities(monkeypatch):
    app = QApplication.instance() or QApplication([])
    settings = {'base_url': 'https://api.example.com/v1', 'key': 'test-only',
                'model': 'reported-model', 'effort': '', 'remember': False}
    monkeypatch.setattr(ai_window, 'load_settings', lambda: dict(settings))
    class Worker(QObject):
        event = Signal(str, object)
        finished = Signal()
        def __init__(self, *args): super().__init__()
        def isRunning(self): return False
        def start(self): pass  # Do not connect to C1 or the network.
    monkeypatch.setattr(ai_window, 'Worker', Worker)
    window = ai_window.AIWindow()
    window.picture = QPixmap(20, 20)
    window.picture.fill()
    window.show_picture()
    window.prompt.setPlainText('分析当前照片')
    window.start()
    assert window.picture.isNull()
    assert window.preview.pixmap().isNull()
    assert '读取' in window.preview.text()
    window.finished()
    dialog = ai_window.SettingsDialog(settings, window)
    dialog.models = {'reported-model': {'reasoning_efforts': ['previous-account-level']}}
    dialog.effort.addItem('previous-account-level')
    QTest.keyClicks(dialog.key, 'changed')
    assert dialog.models == {}
    assert dialog.model.currentText() == '' and dialog.effort.currentText() == ''
    dialog.close()
    window.close()
    app.processEvents()
