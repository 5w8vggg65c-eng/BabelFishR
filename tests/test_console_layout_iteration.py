"""The console layout from Eric's field test.

- The strip with the Sidebar button and "Transcript and translation" is
  gone; the transcript pane starts with the waveform.
- An arrow-only control sits at the top of the sidebar's vertical scroll
  bar, pinned while the contents scroll: left arrow hides the sidebar; the
  right arrow is then at the same left boundary and brings it back (mouse
  and keyboard).
- Sessions, Input and Local Engines each have a disclosure arrow on the
  right of their header; collapsing and reopening keeps what was selected
  or typed.
- The transcript pane reads: waveform, the local-processing notice, then
  the filters, then Time | Transcription | Review.
"""

from __future__ import annotations

import time

import pytest
from PySide6 import QtCore, QtWidgets
from PySide6.QtTest import QTest

from babelfishr.app import BabelFishRApp
from test_offline_indicator import Jobs


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds=15):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.003)


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    Jobs(monkeypatch)
    win = MainWindow(BabelFishRApp(config=config, store=store))
    win.resize(1100, 720)
    win.show()
    win.activateWindow()
    pump(qt_app)
    yield win
    win.close()


def y_in(window, widget):
    return widget.mapTo(window, QtCore.QPoint(0, 0)).y()


def test_the_header_strip_is_gone_and_the_waveform_comes_first(qt_app, window):
    texts = ([w.text() for w in window.findChildren(QtWidgets.QLabel)]
             + [w.text() for w in window.findChildren(QtWidgets.QAbstractButton)])
    assert "Transcript and translation" not in texts and "Sidebar" not in texts
    right = window.waveform_stack.parentWidget()
    assert y_in(window, window.waveform_stack) - y_in(window, right) <= 10, \
        "space above the waveform was not reclaimed"
    window.privacy_banner.show()
    pump(qt_app)
    header = window.findChild(QtWidgets.QFrame, "columnHeader")
    assert (y_in(window, window.waveform_stack) < y_in(window, window.privacy_banner)
            < y_in(window, window.filter_all) < y_in(window, header)
            < y_in(window, window.timeline))


def test_the_sidebar_arrow_is_pinned_on_the_scroll_bar(qt_app, window):
    toggle = window.sidebar_toggle
    assert toggle in window.sidebar.scrollBarWidgets(QtCore.Qt.AlignTop)
    assert toggle.text() == "" and toggle.arrowType() == QtCore.Qt.LeftArrow
    assert toggle.isVisibleTo(window)
    before = toggle.mapTo(window, QtCore.QPoint(0, 0))
    window.resize(1100, 420)                              # make the sidebar scroll
    pump(qt_app)
    before = toggle.mapTo(window, QtCore.QPoint(0, 0))
    bar = window.sidebar.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    pump(qt_app)
    assert toggle.mapTo(window, QtCore.QPoint(0, 0)) == before, "the arrow scrolled away"


def test_left_arrow_hides_right_arrow_restores_by_mouse_and_keyboard(qt_app, window):
    sidebar_right = window.sidebar.mapTo(window, QtCore.QPoint(window.sidebar.width(), 0)).x()
    QTest.mouseClick(window.sidebar_toggle, QtCore.Qt.LeftButton)
    pump(qt_app)
    assert not window.sidebar.isVisible()
    restore = window.sidebar_restore
    assert restore.isVisible() and restore.arrowType() == QtCore.Qt.RightArrow
    assert restore.mapTo(window, QtCore.QPoint(0, 0)).x() <= 12, \
        "the way back is not at the left boundary"
    QTest.mouseClick(restore, QtCore.Qt.LeftButton)
    pump(qt_app)
    assert window.sidebar.isVisible() and not restore.isVisible()
    # Keyboard: Space on the arrow, and focus follows to its counterpart.
    window.sidebar_toggle.setFocus()
    pump(qt_app)
    QTest.keyClick(window.sidebar_toggle, QtCore.Qt.Key_Space)
    pump(qt_app)
    assert not window.sidebar.isVisible()
    assert QtWidgets.QApplication.focusWidget() is restore
    QTest.keyClick(restore, QtCore.Qt.Key_Space)
    pump(qt_app)
    assert window.sidebar.isVisible()
    assert abs(window.sidebar.mapTo(window, QtCore.QPoint(window.sidebar.width(), 0)).x()
               - sidebar_right) <= 4, "the sidebar came back at another width"


def test_each_section_collapses_and_keeps_its_state(qt_app, window):
    assert set(window.section_toggles) == {"sessions", "input", "engines"}
    ops = window.app.create_conversation("Ops")
    window._refresh_session_tabs()
    index = next(i for i in range(window.session_tabs.count())
                 if window.session_tabs.tabData(i) == ops.id)
    window.session_tabs.setCurrentIndex(index)
    window.source_mode_box.setCurrentIndex(window.source_mode_box.findData("specified"))
    window.source_language_box.setCurrentIndex(window.source_language_box.findData("de"))
    window.input_panel.gate_box.setValue(-33.0)            # work in progress
    for key, inside in (("sessions", window.session_tabs), ("input", window.input_panel),
                        ("engines", window.source_language_box)):
        toggle = window.section_toggles[key]
        header = toggle.parentWidget()
        # The arrow is on the right of its header.
        assert toggle.geometry().right() >= header.width() - 6
        QTest.mouseClick(toggle, QtCore.Qt.LeftButton)
        pump(qt_app)
        assert not inside.isVisibleTo(window)
        assert toggle.arrowType() == QtCore.Qt.RightArrow
        assert toggle.accessibleName().startswith("Expand")
        QTest.mouseClick(toggle, QtCore.Qt.LeftButton)
        pump(qt_app)
        assert inside.isVisibleTo(window) and toggle.arrowType() == QtCore.Qt.DownArrow
    assert window.session_tabs.tabData(window.session_tabs.currentIndex()) == ops.id
    assert window.source_language_box.currentData() == "de"
    assert window.input_panel.gate_box.value() == -33.0


def test_choosing_an_input_opens_a_collapsed_input_section(qt_app, window, monkeypatch):
    window.section_toggles["input"].setChecked(False)
    window.sidebar_toggle.setChecked(False)
    monkeypatch.setattr(QtWidgets.QComboBox, "showPopup", lambda self: None)
    window._expand_section("input")
    assert window.sidebar.isVisible() and window.section_toggles["input"].isChecked()
