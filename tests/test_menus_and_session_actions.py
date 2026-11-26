"""Menus without duplicates; Removed filter; Session actions; file cleanup.

- The menu bar has no Message menu and View no longer repeats Review queue,
  All messages or Removed - the filter buttons do that; Search, Refresh and
  Show Times In remain, and the shortcuts work from the window.
- Removed is a filter over the current Session's removed-from-view
  messages, with a plain empty state and a visible Restore.
- A visible Session actions button opens the same menu as right-clicking a
  tab: Hide Session acts at once (undoable), Delete Session... asks ONE
  named question - Hide Session, Delete, or Cancel (the default).
- Retry file cleanup... appears only while a confirmed deletion still has
  files on disk, also after a restart; Cancel changes nothing.

Disposable data in temporary folders only.
"""

from __future__ import annotations

import pathlib

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtTest import QTest

from test_alpha5_session_removal import (answers, menu_action, pump, qt_app,  # noqa: F401
                                         silence_boxes, tab_index_for,
                                         two_sessions_window, wav)


def _texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def _menu(window, title):
    return next(a.menu() for a in window.menuBar().actions()
                if a.text().replace("&", "") == title)


def test_menus_carry_no_duplicates_and_shortcuts_still_work(qt_app, config, store, wav,
                                                            monkeypatch):
    app, window, general, ops, _ = two_sessions_window(qt_app, config, store, wav)
    titles = [a.text().replace("&", "") for a in window.menuBar().actions()]
    assert "Message" not in titles
    view = _texts(_menu(window, "View"))
    for gone in ("Review queue", "Show all transmissions", "Show removed messages"):
        assert gone not in view
    for kept in ("Search transmissions...", "Refresh", "Show Times In"):
        assert kept in view
    assert not any("deletion" in t.lower() or "cleanup" in t.lower()
                   for t in _texts(_menu(window, "Tools")))
    # The thread's own filters are the visible route; Ctrl+Shift+A still works.
    window._run_search("nothing-matches")
    assert window._view_kind == "search"
    window.activateWindow()
    QTest.keySequence(window, QtGui.QKeySequence("Ctrl+Shift+A"))
    pump(qt_app)
    assert window._view_kind == "thread"
    window.close()


def test_the_removed_filter_is_scoped_with_an_empty_state_and_restore(qt_app, config,
                                                                     store, wav):
    app, window, general, ops, snapshot = two_sessions_window(qt_app, config, store, wav)
    _, general_txs, _ = snapshot(general)
    _, ops_txs, _ = snapshot(ops)
    store.hide_transmission(ops_txs[0], hidden=True)      # another Session's
    app.select_conversation(general)
    window._refresh_session_tabs()
    window._reload_timeline()
    QTest.mouseClick(window.filter_removed, QtCore.Qt.LeftButton)
    pump(qt_app)
    assert window._view_kind == "removed" and window.filter_removed.isChecked()
    assert window.timeline.order() == []
    assert "No removed messages in this Session" in window.timeline.empty_label.text()
    assert window.timeline.empty_label.isVisibleTo(window)

    store.hide_transmission(general_txs[0], hidden=True)
    window._refresh_current_view()
    assert window.timeline.order() == [general_txs[0]]
    bubble = window.timeline.bubble(general_txs[0])
    assert bubble.restore_button.isVisibleTo(bubble)
    bubble.restore_button.click()
    pump(qt_app)
    assert store.get_transmission(general_txs[0]).hidden is False
    assert window.timeline.order() == [], "a restored message stayed in Removed"
    QTest.mouseClick(window.filter_all, QtCore.Qt.LeftButton)
    pump(qt_app)
    assert general_txs[0] in window.timeline.order()
    assert "No removed messages" not in window.timeline.empty_label.text()
    window.close()


def test_session_context_menu_by_keyboard_hide_and_one_delete_question(
        qt_app, config, store, wav, monkeypatch):
    """No separate Actions dropdown: the Session's own context menu, also
    from the keyboard (Shift+F10 / Menu key on the Sessions list)."""
    app, window, general, ops, snapshot = two_sessions_window(qt_app, config, store, wav)
    before = snapshot(ops)
    assert not hasattr(window, "session_actions_button")
    assert not [b for b in window.findChildren(QtWidgets.QAbstractButton)
                if b.text().startswith("Actions")]
    shown_menus = []

    def read_and_close_popup():
        popup = QtWidgets.QApplication.activePopupWidget()
        if popup is not None:
            shown_menus.append(_texts(popup))
            popup.close()

    window.session_tabs.setCurrentIndex(tab_index_for(window, ops))
    window.activateWindow()
    window.session_tabs.setFocus()
    pump(qt_app)
    QtCore.QTimer.singleShot(50, read_and_close_popup)
    QTest.keyClick(window.session_tabs, QtCore.Qt.Key_F10, QtCore.Qt.ShiftModifier)
    pump(qt_app)
    assert shown_menus and "Hide Session" in shown_menus[0]
    QtCore.QTimer.singleShot(50, read_and_close_popup)
    QTest.keyClick(window.session_tabs, QtCore.Qt.Key_Menu)
    pump(qt_app)
    assert len(shown_menus) == 2 and shown_menus[1] == shown_menus[0]
    assert "Delete Session…" in shown_menus[0]
    assert "Clear Session History…" in shown_menus[0]

    # Delete Session...: one question; the real dialog's default is Cancel.
    defaults = []

    def box_exec(self):
        # macOS sheets show the question in the body and omit the window title.
        defaults.append((self.text(), self.defaultButton().text().replace("&", ""),
                         [b.text().replace("&", "") for b in self.buttons()]))
        return 0                                            # closed with Cancel

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", box_exec)
    menu_action(window, tab_index_for(window, ops), "Delete Session").trigger()
    pump(qt_app)
    assert len(defaults) == 1
    question, default, labels = defaults[0]
    assert "Delete the Session “Ops” permanently?" == question
    assert default == "Cancel"
    assert {"Hide Session", "Delete", "Cancel"} <= set(labels)
    assert snapshot(ops) == before and not store.get_conversation(ops).hidden

    # Hide Session: at once, everything kept, restorable.
    menu_action(window, tab_index_for(window, ops), "Hide Session").trigger()
    pump(qt_app)
    assert store.get_conversation(ops).hidden and snapshot(ops) == before
    window.show_hidden_sessions_action.setChecked(True)
    pump(qt_app)
    menu_action(window, tab_index_for(window, ops), "Restore Session").trigger()
    assert not store.get_conversation(ops).hidden
    window.close()


def test_cleanup_is_offered_only_while_files_are_left_also_after_restart(
        qt_app, config, store, wav, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app, window, general, ops, snapshot = two_sessions_window(qt_app, config, store, wav)
    assert not window.cleanup_button.isVisibleTo(window)
    _, txs, files = snapshot(ops)
    stuck = pathlib.Path(files[0])
    real_unlink = pathlib.Path.unlink

    def refuse(self, *a, **k):
        if self == stuck:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", refuse)
    shown = silence_boxes(monkeypatch)
    answers(monkeypatch, window, window.DELETE_CHOICE)
    menu_action(window, tab_index_for(window, ops), "Delete Session").trigger()
    pump(qt_app)
    assert stuck.is_file() and store.leftover_deletions()
    assert window.cleanup_button.isVisibleTo(window)
    window.close()                                       # closes the app and its store

    # After a restart it is still offered (read from the database).
    from babelfishr.storage import Store
    from test_alpha5_session_removal import mock_app

    store = Store(config.database, recordings_dir=config.recording.directory)
    app = mock_app(config, store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    assert window.cleanup_button.isVisibleTo(window)
    # Cancel changes nothing.
    answers(monkeypatch, window, "")
    window.cleanup_button.click()
    pump(qt_app)
    assert stuck.is_file() and store.leftover_deletions()
    # Still failing: reported, and still offered.
    answers(monkeypatch, window, window.RETRY_CLEANUP)
    window.cleanup_button.click()
    pump(qt_app)
    assert shown["warning"] and "Still could not remove" in shown["warning"][-1][2]
    assert window.cleanup_button.isVisibleTo(window)
    # Fixed: done, and the offer goes away.
    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    answers(monkeypatch, window, window.RETRY_CLEANUP)
    window.cleanup_button.click()
    pump(qt_app)
    assert not stuck.exists() and not store.leftover_deletions()
    assert not window.cleanup_button.isVisibleTo(window)
    window.close()
    store.close()
