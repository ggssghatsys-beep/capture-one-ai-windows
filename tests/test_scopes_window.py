"""Window lifecycle only: no Capture One calls, photos or network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from c1ai.scopes_window import ScopeApplication, ScopeWindow


class FinishedRead:
    def deleteLater(self):
        pass


def window_at(tmp_path):
    app = QApplication.instance() or ScopeApplication([])
    settings = QSettings(str(tmp_path / 'scope.ini'), QSettings.Format.IniFormat)
    window = ScopeWindow(settings=settings)
    window.timer.stop()
    return app, window


@pytest.mark.parametrize('change', ['changed', 'toggle_pause'])
def test_old_queued_request_is_dropped_after_settings_change_or_pause(tmp_path, monkeypatch, change):
    app, window = window_at(tmp_path)
    window.worker = FinishedRead()
    window.send({'action': 'get', 'analysis_id': 'old-frame'})
    sent = []
    monkeypatch.setattr(window, 'send', sent.append)
    getattr(window, change)()
    window.finished()
    assert sent == [] and window.pending is None
    if change == 'toggle_pause':
        assert window.paused and not window.timer.isActive()
    window.close()


def test_resident_hide_stops_reads_and_reopen_preserves_pause(tmp_path, monkeypatch):
    app, window = window_at(tmp_path)
    class Tray:
        hidden = False
        def hide(self): self.hidden = True
    window.tray = Tray()
    window.show(); window.timer.stop()
    window.worker = FinishedRead()
    previous = window.generation
    window.close()
    assert not window.isVisible() and window.suspended
    assert window.generation > previous and not window.timer.isActive()
    window.finished()
    assert window.worker is None and not window.timer.isActive()
    window.paused = True
    window.open_request({})
    assert window.isVisible() and not window.suspended and not window.timer.isActive()
    window.toggle_pause()
    assert window.timer.isActive()
    quit_calls = []
    monkeypatch.setattr(app, 'exit_ready', False, raising=False)
    monkeypatch.setattr(app, 'quit', lambda: quit_calls.append(True))
    window.worker = FinishedRead()
    window.quit_viewer()
    assert window.exiting and not quit_calls and not window.timer.isActive()
    window.finished()
    assert quit_calls == [True] and window.tray.hidden and app.exit_ready
    window.tray = None
    window.close()
