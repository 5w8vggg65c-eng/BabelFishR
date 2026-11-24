"""The selected message's actions are reachable from the menu bar.

Field report (installed run-32 build, macOS under assistive/automated
control): a message's ⋯ pop-up menu never opened - not by an
accessibility press, Space or screen-coordinate clicks - so "Retry as
speech in..." (the way to give a message its real language) was out of
reach. Message in the menu bar runs the same handlers for the selected
message, with keyboard shortcuts.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from test_ui_v5_console import console, pump, qt_app  # noqa: E402,F401


def menu_texts(window):
    return [a.text() for a in window.message_menu.actions() if not a.isSeparator()]


def test_the_menu_bar_has_a_message_menu_with_the_rows_actions(qt_app, console):
    window, app, made = console
    titles = [a.text().replace("&", "") for a in window.menuBar().actions()]
    assert "Message" in titles
    texts = menu_texts(window)
    for wanted in ("Retry as speech in...", "Retry processing",
                   "Edit transcript and translation...", "Add or edit note...",
                   "Bookmark", "Export audio...", "Play original recording"):
        assert wanted in texts
    assert window.message_retry_speech_action.shortcut() == QtGui.QKeySequence("Ctrl+L")


def test_nothing_selected_disables_the_actions(qt_app, console):
    window, app, made = console
    window.timeline.select(None)
    window._refresh_message_menu()
    assert not window.message_retry_speech_action.isEnabled()
    assert not window.message_edit_action.isEnabled()


def test_retry_as_speech_from_the_menu_bar_uses_the_selected_message(
        qt_app, console, monkeypatch):
    window, app, made = console
    target = made[1]
    window.timeline.select(target.id)
    pump(qt_app)
    window._refresh_message_menu()
    assert window.message_retry_speech_action.isEnabled()
    seen = []
    monkeypatch.setattr(app, "retry", lambda tx_id, source_language=None:
                        seen.append((tx_id, source_language)) or True)
    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem",
                        staticmethod(lambda *a, **k: ("English (en)", True)))
    window.message_retry_speech_action.trigger()
    assert seen == [(target.id, "en")]


def test_the_keyboard_shortcut_reaches_it_too(qt_app, console, monkeypatch):
    window, app, made = console
    window.timeline.select(made[0].id)
    pump(qt_app)
    seen = []
    monkeypatch.setattr(window, "_on_retry_as_speech", lambda tx_id: seen.append(tx_id))
    window.activateWindow()
    QtTest.QTest.keySequence(window, QtGui.QKeySequence("Ctrl+L"))
    pump(qt_app)
    assert seen == [made[0].id]


def test_bookmark_and_retry_run_the_same_handlers_as_the_row(qt_app, console,
                                                             monkeypatch):
    window, app, made = console
    tx = made[1]
    window.timeline.select(tx.id)
    pump(qt_app)
    window.message_bookmark_action.trigger()
    pump(qt_app)
    assert app.store.get_transmission(tx.id).bookmarked
    retried = []
    monkeypatch.setattr(window, "_on_retry", lambda tx_id: retried.append(tx_id))
    window.message_retry_action.trigger()
    assert retried == [tx.id]
