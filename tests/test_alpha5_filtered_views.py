"""A Search or Review view admits only what its own query admits - live.

Search and Review are reached through the real View menu (two mouse clicks,
with the Search text dialog substituted to type the word); records are saved
to the production Store and delivered through the production event queue,
drained by the window's own timer. Membership follows the Store's queries in
both directions: a record that comes to qualify appears where its time puts
it, one that stops qualifying leaves, and the count in the status line
follows. Nothing here uses audio hardware or a real engine. Real Qt
(offscreen) tests, not physical Mac tests.
"""

from __future__ import annotations

import datetime as dt
import os
import time

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState, Session, Transmission
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)

T0 = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds: int = 20) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.004)


def deliver(qt_app, app, store, kind, tx) -> None:
    """Persist, publish on the production queue, let the window's timer drain."""
    store.save_transmission(tx)
    app.events.publish(kind, tx)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not app.events._queue.empty():
        pump(qt_app, 5)
    pump(qt_app, 10)


def record(i: int, session_id: str, text: str, *, conf: float = 0.9,
           state=ProcessingState.COMPLETE, **overrides) -> Transmission:
    fields = dict(id=f"tx{i}", session_id=session_id, transcript=text,
                  transcript_confidence=conf, started_at=T0 + dt.timedelta(seconds=i),
                  state=state, target_language="en", source_language="en")
    fields.update(overrides)
    return Transmission(**fields)


def menu_titled(window, title):
    from PySide6 import QtWidgets

    for menu in window.menuBar().findChildren(QtWidgets.QMenu):
        if menu.title() == title:
            return menu
    raise AssertionError(f"no menu {title!r}")


def click_menu_item(qt_app, window, menu_title, action) -> None:
    """Two real clicks: the menu title in the bar, then the item in its popup."""
    from PySide6 import QtCore
    from PySide6.QtTest import QTest

    bar = window.menuBar()
    menu = menu_titled(window, menu_title)
    QTest.mouseClick(bar, QtCore.Qt.LeftButton, pos=bar.actionGeometry(menu.menuAction()).center())
    pump(qt_app, 5)
    assert menu.isVisible(), f"the {menu_title} menu did not open"
    QTest.mouseClick(menu, QtCore.Qt.LeftButton, pos=menu.actionGeometry(action).center())
    pump(qt_app, 10)
    if menu.isVisible():
        menu.close()


def search_via_menu(qt_app, window, monkeypatch, text, accept=True):
    from PySide6 import QtWidgets

    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: (text, accept)))
    click_menu_item(qt_app, window, "&View", window.search_action)


def two_sessions_window(qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    general = app.conversation_id
    ops = app.create_conversation("Ops")
    store.save_session(Session(id="sg", conversation_id=general))
    store.save_session(Session(id="so", conversation_id=ops.id))
    store.save_transmission(record(0, "sg", "an old giraffe sighting"))
    store.save_transmission(record(1, "sg", "purple giraffe seventeen"))
    store.save_transmission(record(2, "sg", "ordinary words", conf=0.2))
    window._refresh_session_tabs()
    window._reload_timeline()
    pump(qt_app)
    return app, window, general, ops.id


def ids(txs):
    return [t.id for t in txs]


# ---- Search -------------------------------------------------------------------


def test_search_view_admits_only_what_the_search_admits(qt_app, config, store,
                                                        monkeypatch):
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    assert window.timeline.order() == ["tx1", "tx0"]
    assert "2 match(es) for 'giraffe'" in window.status.currentMessage()

    # A non-matching arrival in this Session, and a matching one in another.
    deliver(qt_app, app, store, "transmission", record(3, "sg", "unrelated arrival"))
    deliver(qt_app, app, store, "transmission", record(4, "so", "giraffe in Ops"))
    assert window.timeline.order() == ["tx1", "tx0"], "the view widened with what arrived"
    assert "2 match(es)" in window.status.currentMessage()

    # A newer match arrives: at the top, counted.
    deliver(qt_app, app, store, "transmission", record(5, "sg", "another giraffe now"))
    assert window.timeline.order() == ["tx5", "tx1", "tx0"]
    assert window.timeline.order() == ids(app.search("giraffe"))
    assert "3 match(es)" in window.status.currentMessage()

    # An older record comes to match through editing: placed by its time,
    # not on top.
    edited = store.get_transmission("tx3")
    edited.transcript = "the giraffe was mentioned after all"
    deliver(qt_app, app, store, "updated", edited)
    assert window.timeline.order() == ["tx5", "tx3", "tx1", "tx0"]
    assert window.timeline.order() == ids(app.search("giraffe"))
    assert "4 match(es)" in window.status.currentMessage()

    # A shown record stops matching: it leaves.
    edited = store.get_transmission("tx1")
    edited.transcript = "no animals here"
    deliver(qt_app, app, store, "updated", edited)
    assert window.timeline.order() == ["tx5", "tx3", "tx0"]
    assert "3 match(es)" in window.status.currentMessage()

    # Removed-from-view records are not search results, whatever arrives.
    deliver(qt_app, app, store, "transmission",
            record(6, "sg", "giraffe removed from view", hidden=True))
    assert "tx6" not in window.timeline.order()

    # A late event for a deleted record cannot bring it back.
    stale = store.get_transmission("tx3")
    assert app.delete_permanently("tx3") is not None
    window.timeline.remove("tx3")
    app.events.publish("updated", stale)
    pump(qt_app, 40)
    assert "tx3" not in window.timeline.order()
    assert window.timeline.order() == ["tx5", "tx0"]

    # Show all restores the thread, and live traffic lands on top again.
    click_menu_item(qt_app, window, "&View", window.show_all_action)
    assert window.timeline.order() == ids(app.recent_transmissions(newest_first=True))
    assert "tx1" in window.timeline.order() and "tx6" not in window.timeline.order()
    deliver(qt_app, app, store, "transmission", record(7, "sg", "plain thread traffic"))
    assert window.timeline.order()[0] == "tx7"
    window.close()
    pump(qt_app, 40)


def test_a_cancelled_search_changes_nothing(qt_app, config, store, monkeypatch):
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    before = window.timeline.order()
    search_via_menu(qt_app, window, monkeypatch, "ordinary", accept=False)
    assert window.timeline.order() == before
    assert (window._view_kind, window._view_query) == ("search", "giraffe")
    # ...and the live view is still the giraffe search.
    deliver(qt_app, app, store, "transmission", record(8, "sg", "ordinary again"))
    assert window.timeline.order() == before
    window.close()
    pump(qt_app, 40)


def test_switching_session_leaves_the_search_for_that_sessions_thread(
        qt_app, config, store, monkeypatch):
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    store.save_transmission(record(4, "so", "giraffe in Ops"))
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    assert window.timeline.order() == ["tx1", "tx0"]
    for index in range(window.session_tabs.count()):
        if window.session_tabs.tabData(index) == ops:
            window.session_tabs.setCurrentIndex(index)
    pump(qt_app, 20)
    assert window.timeline.order() == ["tx4"]            # the Ops thread, not a search
    deliver(qt_app, app, store, "transmission", record(9, "so", "plain Ops traffic"))
    assert window.timeline.order() == ["tx9", "tx4"]
    window.close()
    pump(qt_app, 40)


# ---- Review -------------------------------------------------------------------


def test_review_view_admits_only_what_the_review_queue_admits(qt_app, config, store):
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    click_menu_item(qt_app, window, "&View", window.review_action)
    assert window.timeline.order() == ["tx2"] == ids(app.review_queue())
    assert "1 transmission(s) need review" in window.status.currentMessage()

    deliver(qt_app, app, store, "updated", record(10, "sg", "confident update", conf=0.95))
    assert window.timeline.order() == ["tx2"], "a confident record joined the review queue"

    deliver(qt_app, app, store, "transmission", record(11, "sg", "mumbled", conf=0.3))
    assert window.timeline.order() == ["tx11", "tx2"] == ids(app.review_queue())
    assert "2 transmission(s) need review" in window.status.currentMessage()

    failed = record(12, "sg", "", conf=None, state=ProcessingState.FAILED)
    deliver(qt_app, app, store, "updated", failed)
    assert window.timeline.order() == ["tx12", "tx11", "tx2"] == ids(app.review_queue())

    # Reviewed: it leaves the queue - as the Store defines the queue.
    done = store.get_transmission("tx2")
    done.reviewed = True
    deliver(qt_app, app, store, "updated", done)
    assert window.timeline.order() == ["tx12", "tx11"] == ids(app.review_queue())
    assert "2 transmission(s) need review" in window.status.currentMessage()

    # Low confidence in another Session is not this Session's to review.
    deliver(qt_app, app, store, "transmission", record(13, "so", "mumbled in Ops", conf=0.3))
    assert window.timeline.order() == ["tx12", "tx11"]
    window.close()
    pump(qt_app, 40)


# ---- the reading position ------------------------------------------------------


def test_filtered_arrivals_above_the_viewport_keep_the_reading_position(
        qt_app, config, store, monkeypatch):
    from PySide6 import QtCore

    app, window, general, ops = two_sessions_window(qt_app, config, store)
    for i in range(20, 60):
        store.save_transmission(record(i, "sg", f"giraffe report number {i} " * 3))
    window.resize(900, 500)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    view = window.timeline
    assert view.count() == 42
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    pump(qt_app, 10)
    anchor = view._anchor()
    assert anchor is not None
    anchor_id = anchor[0]
    anchor_bubble = view._bubbles[anchor_id]

    def viewport_y(bubble) -> int:
        return bubble.mapTo(view.viewport(), QtCore.QPoint(0, 0)).y()

    before = viewport_y(anchor_bubble)

    # A newer match arrives above everything, and a bubble above the
    # viewport grows through an update. The text under the eyes stays put.
    deliver(qt_app, app, store, "transmission", record(70, "sg", "giraffe, brand new " * 6))
    top_id = view.order()[1]                              # just below the arrival
    grown = store.get_transmission(top_id)
    grown.transcript = grown.transcript + " and much more was said about the giraffe " * 4
    deliver(qt_app, app, store, "updated", grown)
    pump(qt_app, 20)
    assert view.order()[0] == "tx70"
    assert view._bubbles[anchor_id] is anchor_bubble
    assert abs(viewport_y(anchor_bubble) - before) <= 1, (
        f"the anchored bubble moved from y={before} to y={viewport_y(anchor_bubble)}")
    window.close()
    pump(qt_app, 40)
