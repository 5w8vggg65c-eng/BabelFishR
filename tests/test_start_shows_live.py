"""Starting monitoring shows the live thread at its newest message.

From a search, a filter (Removed, Review, Bookmarked), a list scrolled
down, or another Session's tab chosen just before Start: a successful
Start shows the Session being recorded, unfiltered, scrolled to the top
(newest first), so new transcriptions appear in view. Scrolling afterwards
stays the operator's. A Start that fails leaves the view and position as
they were. Replay files and temporary data only.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from PySide6 import QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState, Session, Transmission
from test_offline_indicator import Jobs

T0 = dt.datetime(2026, 4, 1, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds=15):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.003)


@pytest.fixture
def wav(tmp_path):
    from babelfishr.testing import build_fixture

    return build_fixture([{"gap": 0.5}, {"kind": "voice", "duration": 1.0, "level_dbfs": -14},
                          {"gap": 0.5}], sample_rate=16_000).write(str(tmp_path / "one.wav"))


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    Jobs(monkeypatch)
    app = BabelFishRApp(config=config, store=store)
    ops = store.create_conversation("Ops")
    store.save_session(Session(id="ops-run", conversation_id=ops.id))
    for n in range(40):                        # a long thread, to scroll
        store.save_transmission(Transmission(
            id=f"m{n:02d}", session_id="ops-run", duration=1.0,
            started_at=T0 + dt.timedelta(minutes=n), state=ProcessingState.COMPLETE,
            transcript=f"message number {n} " + "words " * 12, hidden=(n == 3)))
    app.select_conversation(ops.id)
    win = MainWindow(app)
    win.resize(900, 420)
    win.show()
    pump(qt_app)
    win.ops = ops.id
    yield win
    if win.app.session is not None:
        win._stop_monitoring()
    win.app.wait_for_session_ends(10)
    deadline = time.monotonic() + 15
    while win.app.outstanding_work() and time.monotonic() < deadline:
        pump(qt_app)
    win.close()


def _scroll_down(window, qt_app):
    pump(qt_app, 40)                           # the rebuilt list's final size
    bar = window.timeline.verticalScrollBar()
    bar.setValue(bar.maximum())
    pump(qt_app)
    assert bar.value() > bar.minimum(), "the thread is not long enough to scroll"


def _at_top(window):
    bar = window.timeline.verticalScrollBar()
    return bar.value() == bar.minimum()


def test_from_a_search_scrolled_down_start_shows_the_live_top(qt_app, window, wav):
    window.search_field.setText("number 7")
    window._run_search("number 7")
    assert window._view_kind == "search"
    window._reload_timeline()
    window._run_search("message")
    _scroll_down(window, qt_app)
    window._start_monitoring(replay_path=wav)
    pump(qt_app)
    assert window.app.session is not None
    assert window._view_kind == "thread" and window.search_field.text() == ""
    assert window.filter_all.isChecked()
    assert _at_top(window)
    assert window.timeline.order()[0] == "m39", "the newest is not at the top"


@pytest.mark.parametrize("show", ["_show_removed", "_show_review_queue", "_show_bookmarked"])
def test_from_a_filter_start_shows_the_live_thread(qt_app, window, wav, show):
    getattr(window, show)()
    assert window._view_kind != "thread"
    window._start_monitoring(replay_path=wav)
    pump(qt_app)
    assert window._view_kind == "thread" and _at_top(window)
    assert "m03" not in window.timeline.order(), "removed messages shown in the live thread"


def test_from_another_session_start_records_and_shows_that_session(qt_app, window, wav):
    general = window.app.store.default_conversation().id
    index = next(i for i in range(window.session_tabs.count())
                 if window.session_tabs.tabData(i) == general)
    window.session_tabs.setCurrentIndex(index)       # the operator switched tabs
    pump(qt_app)
    window._start_monitoring(replay_path=wav)
    pump(qt_app)
    assert window.app._capture_conversation_id == general
    assert window.session_tabs.tabData(window.session_tabs.currentIndex()) == general
    assert window._view_kind == "thread" and _at_top(window)


def test_scrolling_afterwards_is_kept(qt_app, window, wav):
    window._start_monitoring(replay_path=wav)
    pump(qt_app)
    _scroll_down(window, qt_app)
    held = window.timeline.verticalScrollBar().value()
    pump(qt_app, 30)
    window._drain_events()
    pump(qt_app)
    assert window.timeline.verticalScrollBar().value() >= held - 5, \
        "the view jumped back to the top after the operator scrolled"


def test_a_failed_start_leaves_the_view_alone(qt_app, window, monkeypatch):
    window._run_search("message")
    _scroll_down(window, qt_app)
    held = window.timeline.verticalScrollBar().value()
    shown = window.timeline.order()

    def refuse(*a, **k):
        raise RuntimeError("the input could not be opened")

    monkeypatch.setattr(window.app, "start_session", refuse)
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    window._start_monitoring(replay_path="/nonexistent.wav")
    pump(qt_app)
    assert window.app.session is None
    assert window._view_kind == "search" and window.timeline.order() == shown
    assert window.timeline.verticalScrollBar().value() == held
