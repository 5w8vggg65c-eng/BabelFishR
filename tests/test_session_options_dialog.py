"""Session Options opens as a dialog over the transcript.

The bottom button opens it centred on the transcript; the transcript,
waveform and meters keep their size and place underneath. Close, Escape and
the button close it; it resizes and stays on a small screen with scrolling
contents. The options themselves are the same controls with the same events.
"""

from __future__ import annotations

import os

import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtTest import QTest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, n=5):
    for _ in range(n):
        qt_app.processEvents()


@pytest.fixture
def window(qt_app, config, store, tmp_path):
    from babelfishr.ui.main_window import MainWindow

    config.source_path = str(tmp_path / "settings.toml")
    app = BabelFishRApp(config=config, store=store)
    app.transcription, app.translation = MockTranscriptionEngine(), MockTranslationEngine()
    win = MainWindow(app)
    win.resize(1100, 760)
    win.show()
    pump(qt_app)
    yield win
    win.session_options_dialog.close()
    win.close()
    app.close()


def _global_rect(widget):
    return QtCore.QRect(widget.mapToGlobal(QtCore.QPoint(0, 0)), widget.size())


def test_the_button_opens_a_dialog_over_the_transcript_without_moving_it(qt_app, window):
    timeline_before = _global_rect(window.timeline)
    waveform_before = _global_rect(window.waveform)
    window.session_options_button.click()
    pump(qt_app)
    dialog = window.session_options_dialog
    assert dialog.isVisible() and window.session_options_button.isChecked()
    assert dialog.windowTitle() == "Session Options"
    assert window.setup_box.isVisible() and window.setup_box.window() is dialog
    # Over the transcript (kept on screen; centring is checked below).
    assert dialog.frameGeometry().intersects(_global_rect(window.timeline))
    _assert_on_screen(dialog)
    # Roomy, and nothing underneath moved or shrank.
    assert dialog.width() >= 600 and dialog.height() >= 400
    assert _global_rect(window.timeline) == timeline_before
    assert _global_rect(window.waveform) == waveform_before
    assert window.waveform.isVisible()
    assert not dialog.isModal(), "monitoring and the transcript stay usable"


def _screen():
    return QtGui.QGuiApplication.primaryScreen().availableGeometry()


def _assert_on_screen(dialog, slack=6):
    frame, screen = dialog.frameGeometry(), _screen()
    assert frame.left() >= screen.left() - slack and frame.top() >= screen.top() - slack
    assert frame.right() <= screen.right() + slack and frame.bottom() <= screen.bottom() + slack


def test_it_is_centred_on_the_transcript_when_there_is_room(qt_app):
    from babelfishr.ui.session_options import SessionOptionsDialog

    host = QtWidgets.QWidget()
    host.setGeometry(100, 100, 600, 600)        # centre (400, 400) on an 800 px screen
    host.show()
    content = QtWidgets.QLabel("options")
    dialog = SessionOptionsDialog(content, host)
    try:
        dialog.resize(500, 400)
        dialog.open_over(host)
        pump(qt_app)
        centre = QtCore.QRect(host.mapToGlobal(QtCore.QPoint(0, 0)), host.size()).center()
        got = dialog.frameGeometry().center()
        assert abs(got.x() - centre.x()) <= 6 and abs(got.y() - centre.y()) <= 6, (got, centre)
        _assert_on_screen(dialog)
    finally:
        dialog.close()
        host.close()


@pytest.mark.parametrize("how", ["escape", "close", "button"])
def test_close_escape_and_the_button_close_it(qt_app, window, how):
    dialog = window.session_options_dialog
    window.session_options_button.click()
    pump(qt_app)
    assert dialog.isVisible()
    if how == "escape":
        QTest.keyClick(dialog, QtCore.Qt.Key_Escape)
    elif how == "close":
        dialog.close_button.click()
    else:
        window.session_options_button.click()
    pump(qt_app)
    assert not dialog.isVisible()
    assert not window.session_options_button.isChecked()
    window.session_options_button.click()          # and opens again
    pump(qt_app)
    assert dialog.isVisible()


def test_it_resizes_and_fits_a_small_screen_with_scrolling(qt_app, window):
    dialog = window.session_options_dialog
    assert dialog.isSizeGripEnabled()
    window.session_options_button.click()
    pump(qt_app)
    dialog.resize(900, 640)
    pump(qt_app)
    assert dialog.size() == QtCore.QSize(900, 640)       # the operator's size
    window.session_options_button.click()               # close and reopen
    window.session_options_button.click()
    pump(qt_app)
    # Reopened on this 800 px screen: fitted, every part (Close too) on it.
    assert dialog.width() <= _screen().width()
    _assert_on_screen(dialog)
    assert dialog.scroll.widgetResizable()
    assert dialog.scroll.widget() is window.setup_box
    dialog.resize(dialog.minimumSize())
    pump(qt_app)
    # Too small for the controls: they scroll rather than being cut off.
    content = window.setup_box.sizeHint()
    if content.height() > dialog.scroll.viewport().height():
        assert dialog.scroll.verticalScrollBar().maximum() > 0


def test_the_options_keep_their_controls_and_events(qt_app, window):
    window.session_options_button.click()
    pump(qt_app)
    dialog = window.session_options_dialog
    box = window.quiet_seconds_box
    assert box.window() is dialog
    box.setValue(1.0)                               # the 1 s quiet setting
    assert window.app.config.detector.hang_time == 1.0
    assert Config.load(window.app.config.source_path).detector.hang_time == 1.0
    combos = window.setup_box.findChildren(QtWidgets.QComboBox)
    assert combos and all(c.window() is dialog for c in combos)
    assert not any(c.view().isVisible() for c in combos)


def test_there_is_no_session_options_checkbox_anywhere(window):
    assert not window.setup_box.isCheckable()
    boxes = [b for b in window.findChildren(QtWidgets.QCheckBox)
             if "session options" in b.text().lower()]
    assert boxes == []
