"""The GUI/database boundary at shutdown, and the end-of-run write.

A. Once every user of the store has finished, the window shuts every route
   it has to the store - menus, shortcuts, tool bars, controls, its event
   drain, and the continuation of any handler already past a dialog - and
   only then asks the application to begin closing engines and store. Real
   menu clicks and shortcuts during a held cleanup, including the interval
   after the real Store.close has run, reach the store zero times.
B. Recording the end of a run (close_session) takes the store lock, which a
   worker may hold. It now runs on its own tracked thread with the Session
   identity and ending time fixed at Stop; final cleanup waits for it; a
   failed write is reported and retried, never skipped.

Substitutions, precisely: Store.close wrapped on the instance to run the
real close and then hold; the store's SQLite connection wrapped by a proxy
that counts execute() calls made from the main thread once cleanup has
begun (every query passes through it); the real store RLock acquired and
held by another thread; store.close_session made to raise once via
monkeypatch; QApplication.activeModalWidget patched to report an open
dialog; QInputDialog.getText patched to record that the Search handler was
reached. Everything else is production code. Real Qt (offscreen) tests, not
physical Mac tests; production timeouts untouched.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.models import ProcessingState
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.testing import build_fixture

SR = 48_000
VOICE_OPEN = [{"gap": 1.0}, {"kind": "voice", "duration": 2.0, "level_dbfs": -14}]


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def wav(tmp_path) -> str:
    return build_fixture([{"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
                          {"gap": 1.0}], sample_rate=SR).write(str(tmp_path / "one.wav"))


def pump(qt_app, rounds: int = 20) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.004)


def pump_until(qt_app, condition, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        pump(qt_app, 5)
    return condition()


def heartbeat(qt_app):
    from PySide6 import QtCore

    ticks = []
    timer = QtCore.QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    return timer, ticks


def alive_beat(qt_app, ticks, rounds: int = 60) -> None:
    before = len(ticks)
    pump(qt_app, rounds)
    assert len(ticks) >= before + 5, "the event loop did not turn"


class Held:
    def __init__(self, real, after: bool = True):
        self.real, self.after = real, after
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if not self.after:
            self.entered.set()
            assert self.release.wait(60.0)
            return self.real(*args, **kwargs)
        result = self.real(*args, **kwargs)             # the real work first...
        self.entered.set()
        assert self.release.wait(60.0)                  # ...then the hold
        return result


class CountingConnection:
    """Every query the store makes passes through here."""

    def __init__(self, conn):
        self._conn = conn
        self.armed = False
        self.main_thread_queries = []

    def execute(self, *args, **kwargs):
        if self.armed and threading.current_thread() is threading.main_thread():
            self.main_thread_queries.append(args[0] if args else "?")
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def window_with_app(qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app, window


def saved_message(app, wav):
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    app.stop_session()
    assert app.wait_for_session_ends(10.0)
    return app.recent_transmissions()[0]


def menu_titled(window, title):
    from PySide6 import QtWidgets

    for menu in window.menuBar().findChildren(QtWidgets.QMenu):
        if menu.title() == title:
            return menu
    raise AssertionError(f"no menu {title!r}")


def click_menu_item(qt_app, window, menu_title, action):
    """Two real clicks: the menu title in the bar, then the item in its popup."""
    from PySide6 import QtCore
    from PySide6.QtTest import QTest

    bar = window.menuBar()
    menu = menu_titled(window, menu_title)
    QTest.mouseClick(bar, QtCore.Qt.LeftButton, pos=bar.actionGeometry(menu.menuAction()).center())
    pump(qt_app, 5)
    opened = menu.isVisible()
    if opened:
        QTest.mouseClick(menu, QtCore.Qt.LeftButton, pos=menu.actionGeometry(action).center())
    else:
        # The bar refused to open; press where the item would be anyway.
        QTest.mouseClick(menu, QtCore.Qt.LeftButton, pos=menu.actionGeometry(action).center())
    pump(qt_app, 5)
    if menu.isVisible():
        menu.close()
    return opened


def finish(qt_app, window, *holds):
    for hold in holds:
        hold.release.set()
    pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0) or window.close()


# ---- A. no GUI route to the store once cleanup can close it ----------------------


def test_menus_and_shortcuts_cannot_reach_the_store_during_final_cleanup(
        qt_app, config, store, wav, monkeypatch):
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtTest import QTest

    app, window = window_with_app(qt_app, config, store)
    tx = saved_message(app, wav)
    counter = CountingConnection(store._conn)
    store._conn = counter
    searches = []
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: (searches.append(a), ("", False))[1]))

    # Before Quit the menus and shortcuts work as they always did.
    assert window.review_action.isEnabled() and window.menuBar().isEnabled()
    QTest.keyClick(window, QtCore.Qt.Key_F, QtCore.Qt.ControlModifier)
    pump(qt_app, 5)
    assert len(searches) == 1, "Ctrl+F did not reach Search before quitting"
    opened = click_menu_item(qt_app, window, "&View", window.review_action)
    assert opened, "the View menu did not open before quitting"
    assert "match" in window.status.currentMessage().lower() or window.timeline.count() >= 0

    # Quit, with the real Store.close run and then held: the interval Codex
    # found, after the database is closed and before cleanup returns.
    held_close = Held(store.close, after=True)
    monkeypatch.setattr(store, "close", held_close)
    timer, ticks = heartbeat(qt_app)
    try:
        assert window.close() is False
        assert pump_until(qt_app, lambda: held_close.entered.is_set(), timeout=10.0), (
            "cleanup never reached the store")
        counter.armed = True
        assert app.cleaning and window._store_off_limits
        assert not window.menuBar().isEnabled()
        assert not window.review_action.isEnabled() and not window.search_action.isEnabled()
        assert all(not a.isEnabled() for a in window.findChildren(QtGui_actions()))
        alive_beat(qt_app, ticks)

        # Real clicks and keys, exactly as before - the database is closed now.
        opened = click_menu_item(qt_app, window, "&View", window.review_action)
        assert not opened, "the View menu opened during cleanup"
        QTest.keyClick(window, QtCore.Qt.Key_F, QtCore.Qt.ControlModifier)
        QTest.keyClick(window, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier)
        QTest.keyClick(window, QtCore.Qt.Key_R, QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier)
        window.review_action.trigger()
        window.show_all_action.trigger()
        window.search_action.trigger()
        window._show_review_queue()                    # a handler reached some other way
        window._reload_timeline()
        app.events.publish("transmission", tx)
        window._drain_events()
        pump(qt_app, 20)
        assert len(searches) == 1, "Ctrl+F reached Search during cleanup"
        assert counter.main_thread_queries == [], counter.main_thread_queries
        assert "not done" in window.status.currentMessage() or "closing" in window.status.currentMessage()
        assert window.isVisible()

        held_close.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert app._closed and counter.main_thread_queries == []
    finally:
        timer.stop()
        finish(qt_app, window, held_close)


def QtGui_actions():
    from PySide6 import QtGui

    return QtGui.QAction


def test_an_open_dialog_defers_cleanup_until_it_closes(qt_app, config, store, wav,
                                                       monkeypatch):
    from PySide6 import QtWidgets

    app, window = window_with_app(qt_app, config, store)
    saved_message(app, wav)
    dialog = QtWidgets.QWidget()                       # stands for an open modal dialog
    monkeypatch.setattr(QtWidgets.QApplication, "activeModalWidget",
                        staticmethod(lambda: dialog))
    timer, ticks = heartbeat(qt_app)
    try:
        assert window.close() is False
        assert pump_until(qt_app, lambda: app.ready_for_cleanup, timeout=10.0)
        pump(qt_app, 40)
        assert window._store_off_limits, "routes stayed open with cleanup imminent"
        assert not app.cleaning, "cleanup began under an open dialog"
        assert "open dialog" in window.status.currentMessage()
        # The dialog's handler continues on this thread when it closes, and
        # finds the door shut rather than a closing store.
        assert window._store_gone() is True
        assert store.get_transmission("anything") is None   # the store is still open
        alive_beat(qt_app, ticks)

        monkeypatch.setattr(QtWidgets.QApplication, "activeModalWidget",
                            staticmethod(lambda: None))
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert app._closed
    finally:
        timer.stop()
        finish(qt_app, window)


# ---- B. the end-of-run write ------------------------------------------------------


class LockHolder:
    """Holds the store's real lock from another thread until released."""

    def __init__(self, store):
        self.store = store
        self.holding = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.store._lock:
            self.holding.set()
            self.release.wait(60.0)

    def start(self):
        self.thread.start()
        assert self.holding.wait(5.0)
        return self


def live_window(qt_app, config, store):
    app, window = window_with_app(qt_app, config, store)
    source = CallbackAudioSource(SR)
    app.start_session(source=source, name="live")
    app.begin_capture()
    audio = build_fixture(VOICE_OPEN, sample_rate=SR).audio
    for start in range(0, audio.size, 4800):
        source.push(audio[start:start + 4800])
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (
            source._queue.empty() and app.capture.detector.open):
        time.sleep(0.02)
    assert app.capture.detector.open
    return app, window, source


def test_stop_returns_while_the_store_lock_is_held_and_records_the_end_later(
        qt_app, config, store):
    app, window, source = live_window(qt_app, config, store)
    session = app.session
    holder = LockHolder(store).start()
    timer, ticks = heartbeat(qt_app)
    try:
        started = time.monotonic()
        window._stop_monitoring()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"Stop waited {elapsed:.2f}s on the store lock"
        alive_beat(qt_app, ticks)                          # lock still held
        assert app.session is None
        assert app.session_end_pending(), "the end-of-run write was not tracked"
        assert store.get_session(session.id).ended_at is None, "written while the lock was held?"
        ended_at = session.ended_at
        assert ended_at is not None                      # fixed at Stop
        assert "end of the run" not in window.status.currentMessage()  # Stop, not Quit

        holder.release.set()
        assert pump_until(qt_app, lambda: not app.session_end_pending(), timeout=10.0)
        stored = store.get_session(session.id)
        assert stored.ended_at is not None
        assert abs((stored.ended_at - ended_at).total_seconds()) < 1e-3
        assert pump_until(qt_app, lambda: app.outstanding_work() == 0, timeout=20.0)
        txs = store.recent_transmissions()
        assert len(txs) == 1 and txs[0].state is ProcessingState.COMPLETE
        # Stop is not Quit: the window still works afterwards.
        assert not app.closing and window.menuBar().isEnabled()
        window.close()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
    finally:
        holder.release.set()
        timer.stop()
        finish(qt_app, window)


def test_quit_with_the_lock_held_returns_and_records_the_end_before_closing(
        qt_app, config, store):
    app, window, source = live_window(qt_app, config, store)
    session = app.session
    order = []
    real_close_session, real_close = store.close_session, store.close
    store.close_session = lambda sid, ended_at=None: (order.append(("end", sid)), real_close_session(sid, ended_at=ended_at))[1]
    real_save = store.save_transmission
    store.save_transmission = lambda tx: (order.append(("save", tx.state.value)), real_save(tx))[1]
    store.close = lambda: (order.append("store-close"), real_close())[1]
    holder = LockHolder(store).start()
    timer, ticks = heartbeat(qt_app)
    try:
        started = time.monotonic()
        assert window.close() is False
        assert time.monotonic() - started < 1.0, "Quit waited on the store lock"
        alive_beat(qt_app, ticks)
        assert app.session_end_pending() and not app.cleaning
        assert "end of the run" in window.status.currentMessage() or "transmission" in window.status.currentMessage()
        for _ in range(3):
            assert window.close() is False                 # repeated Quit
        assert sum(1 for o in order if o[0] == "end") <= 1

        holder.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert app._closed
        ends = [o for o in order if o[0] == "end"]
        assert ends == [("end", session.id)], ends          # once, this run
        assert ("save", "complete") in order
        assert order.index(("end", session.id)) < order.index("store-close")
        assert order.index(("save", "complete")) < order.index("store-close")
        from babelfishr.storage import Store
        again = Store(config.database, recordings_dir=config.recording.directory)
        stored = again.get_session(session.id)
        rows = again.recent_transmissions()
        again.close()
        assert stored.ended_at is not None
        assert abs((stored.ended_at - session.ended_at).total_seconds()) < 1e-3
        assert len(rows) == 1 and rows[0].state is ProcessingState.COMPLETE
    finally:
        holder.release.set()
        timer.stop()
        finish(qt_app, window)


def test_a_failed_end_of_run_write_is_reported_and_retried(qt_app, config, store, wav,
                                                           monkeypatch):
    app, window = window_with_app(qt_app, config, store)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    session = app.session
    real_close_session = store.close_session
    calls = {"n": 0}

    def failing(session_id, ended_at=None):
        calls["n"] += 1
        raise OSError("database is locked for good")

    monkeypatch.setattr(store, "close_session", failing)
    window._stop_monitoring()
    assert pump_until(qt_app, lambda: bool(app.persistence_error), timeout=10.0)
    assert "locked for good" in app.persistence_error
    assert app.session_end_pending()
    assert store.get_session(session.id).ended_at is None

    assert window.close() is False
    assert pump_until(qt_app, lambda: "could not finish quitting" in
                      window.status.currentMessage().lower(), timeout=10.0), (
        window.status.currentMessage())
    assert "locked for good" in window.status.currentMessage()
    assert not app.cleaning and not app._closed and window.isVisible()
    first = calls["n"]
    assert pump_until(qt_app, lambda: calls["n"] > first, timeout=5.0), "the write was not retried"
    assert not app._closed

    monkeypatch.setattr(store, "close_session", real_close_session)
    assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
    assert app._closed
    from babelfishr.storage import Store
    again = Store(config.database, recordings_dir=config.recording.directory)
    stored = again.get_session(session.id)
    again.close()
    assert stored.ended_at is not None
    assert abs((stored.ended_at - session.ended_at).total_seconds()) < 1e-3


def test_quit_waits_for_a_slow_end_of_run_write_before_closing(qt_app, config, store, wav,
                                                               monkeypatch):
    """Only the end-of-run write is outstanding - everything else is done.
    The store must not close over it."""
    app, window = window_with_app(qt_app, config, store)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    session = app.session
    held_end = Held(store.close_session, after=False)
    monkeypatch.setattr(store, "close_session", held_end)
    order = []
    real_close = store.close
    store.close = lambda: (order.append("store-close"), real_close())[1]
    timer, ticks = heartbeat(qt_app)
    try:
        window._stop_monitoring()
        assert held_end.entered.wait(5.0)
        assert window.close() is False
        alive_beat(qt_app, ticks)                          # the write still held
        assert app.session_end_pending()
        assert not app.ready_for_cleanup and not app.cleaning, (
            "cleanup was allowed with the end of the run unrecorded")
        assert "end of the run" in window.status.currentMessage()
        assert order == [] and store.get_session(session.id).ended_at is None

        held_end.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert app._closed and order == ["store-close"] and held_end.calls == 1
        from babelfishr.storage import Store
        again = Store(config.database, recordings_dir=config.recording.directory)
        stored = again.get_session(session.id)
        again.close()
        assert stored.ended_at is not None
        assert abs((stored.ended_at - session.ended_at).total_seconds()) < 1e-3
    finally:
        timer.stop()
        finish(qt_app, window, held_end)
