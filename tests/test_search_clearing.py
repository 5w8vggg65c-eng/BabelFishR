"""Clearing the search shows the whole thread again.

Field report (installed run-30/32 builds): clearing the search left the
filtered results on screen under an empty search field. The field ran a
search only on Return; its clear button (and deleting the text) emitted
nothing the window listened to. View > Show all transmissions worked.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtTest  # noqa: E402

from test_ui_v5_console import console, pump, qt_app  # noqa: E402,F401


def shown(window):
    return len(window.timeline.order())


def search(window, qt_app, text):
    window.search_field.setText(text)
    QtTest.QTest.keyClick(window.search_field, QtCore.Qt.Key_Return)
    pump(qt_app)


def test_clearing_the_search_field_restores_the_thread(qt_app, console):
    window, app, made = console
    full = shown(window)
    search(window, qt_app, "Bonjour")
    assert shown(window) < full and window._view_kind == "search"
    window.search_field.clear()                  # what the field's clear button does
    pump(qt_app)
    assert shown(window) == full, "the filtered results stayed on screen"
    assert window._view_kind == "thread" and window.filter_all.isChecked()


def test_deleting_the_search_text_restores_the_thread(qt_app, console):
    window, app, made = console
    full = shown(window)
    search(window, qt_app, "Bonjour")
    window.search_field.selectAll()
    QtTest.QTest.keyClick(window.search_field, QtCore.Qt.Key_Backspace)
    pump(qt_app)
    assert window.search_field.text() == "" and shown(window) == full


@pytest.mark.parametrize("start", ["search", "review", "bookmarked"])
def test_all_messages_restores_the_thread_from_every_view(qt_app, console, start):
    window, app, made = console
    full = shown(window)
    if start == "search":
        search(window, qt_app, "Bonjour")
    else:
        QtTest.QTest.mouseClick({"review": window.filter_review,
                                 "bookmarked": window.filter_bookmarked}[start],
                                QtCore.Qt.LeftButton)
        pump(qt_app)
    assert shown(window) < full
    QtTest.QTest.mouseClick(window.filter_all, QtCore.Qt.LeftButton)
    pump(qt_app)
    assert shown(window) == full and window._view_kind == "thread"
    assert window.search_field.text() == ""


def test_typing_into_the_field_does_not_search_until_return(qt_app, console):
    """Unchanged: a search runs on Return, not on every keystroke."""
    window, app, made = console
    full = shown(window)
    window.search_field.setText("Bonj")
    pump(qt_app)
    assert shown(window) == full and window._view_kind == "thread"
