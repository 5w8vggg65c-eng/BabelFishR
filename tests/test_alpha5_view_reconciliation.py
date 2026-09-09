"""A Search or Review view is reconciled to its own query, completely.

The earlier repair kept unrelated traffic out of a filtered view but judged
by the event's record alone: a record that had fallen out of a full result
(the Store's 200-row limit) could never come back to replace one that left,
and the bubble menu's Remove message updated the widgets directly, so a
removed message stayed in a Search that no longer held it. Here the Store's
own bounded answer is the membership - for live events, for the operator's
own removal and restoration, and for deletion - and the count follows.

Real Qt (offscreen); Search and Review through the real View menu (the
Search text dialog substituted to type the word); records persisted and
delivered through the production event queue drained by the window's timer;
the removal question answered through the existing substitution point only.
No audio hardware, no real engine. Not a physical Mac test.
"""

from __future__ import annotations

import importlib.util
import pathlib
import time

import pytest

_spec = importlib.util.spec_from_file_location(
    "filtered_view_helpers", pathlib.Path(__file__).with_name("test_alpha5_filtered_views.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
qt_app = _helpers.qt_app
pump, deliver, record, ids = _helpers.pump, _helpers.deliver, _helpers.record, _helpers.ids
click_menu_item, search_via_menu = _helpers.click_menu_item, _helpers.search_via_menu
two_sessions_window = _helpers.two_sessions_window

LIMIT = 200          # Store.search and Store.review_queue: the production limit


def drain(qt_app, app) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not app.events._queue.empty():
        pump(qt_app, 3)
    pump(qt_app, 6)


def full_session_window(qt_app, config, store, **fields):
    """A General Session holding one more qualifying record than the limit."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.models import Session
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    store.save_session(Session(id="sg", conversation_id=app.conversation_id))
    for i in range(LIMIT + 1):
        store.save_transmission(record(i, "sg", f"giraffe number {i}", **fields))
    window = MainWindow(app)
    window.resize(900, 500)
    window.show()
    pump(qt_app)
    return app, window


def viewport_y(view, bubble) -> int:
    from PySide6 import QtCore

    return bubble.mapTo(view.viewport(), QtCore.QPoint(0, 0)).y()


def in_view(window, results) -> None:
    """Membership equals the Store's answer, and the count agrees."""
    assert window.timeline.order() == ids(results)
    assert f"{len(results)} " in window.status.currentMessage()


def click_bubble_menu(qt_app, bubble, action) -> None:
    """Two real clicks: the bubble's ... button, then the item in its menu."""
    from PySide6 import QtCore
    from PySide6.QtTest import QTest

    menu = bubble.menu_button.menu()
    opened = []

    def click_item():
        opened.append(menu.isVisible())
        QTest.mouseClick(menu, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                         menu.actionGeometry(action).center())

    QtCore.QTimer.singleShot(50, click_item)
    QTest.mouseClick(bubble.menu_button, QtCore.Qt.LeftButton)
    pump(qt_app, 30)
    assert opened == [True], "the ... menu did not open"


def test_a_full_search_replaces_a_row_that_stops_qualifying(qt_app, config, store,
                                                            monkeypatch):
    app, window = full_session_window(qt_app, config, store)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    view = window.timeline
    shown = view.order()
    assert len(shown) == LIMIT and shown[0] == f"tx{LIMIT}" and "tx0" not in shown
    in_view(window, app.search("giraffe"))

    # Read somewhere in the middle; remember the bubble under the eyes.
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    pump(qt_app, 10)
    anchor_id = view._anchor()[0]
    anchor_bubble = view._bubbles[anchor_id]
    before = viewport_y(view, anchor_bubble)
    survivors = {tx_id: view._bubbles[tx_id] for tx_id in shown[1:]}

    # The newest shown row stops matching: it leaves, and the oldest match,
    # which the limit had kept out, comes in at the bottom.
    top = store.get_transmission(f"tx{LIMIT}")
    top.transcript = "nothing of interest"
    deliver(qt_app, app, store, "updated", top)
    results = app.search("giraffe")
    assert ids(results)[-1] == "tx0" and f"tx{LIMIT}" not in ids(results)
    in_view(window, results)
    assert view.count() == LIMIT
    assert all(view._bubbles[tx_id] is bubble for tx_id, bubble in survivors.items()), (
        "surviving bubbles were rebuilt")
    assert abs(viewport_y(view, anchor_bubble) - before) <= 1, (
        f"the anchored bubble moved from y={before} to y={viewport_y(view, anchor_bubble)}")

    # And the other way: a record it has just come to match, at the limit,
    # pushes the oldest match back out.
    top.transcript = "a giraffe after all"
    deliver(qt_app, app, store, "updated", top)
    results = app.search("giraffe")
    assert ids(results)[0] == f"tx{LIMIT}" and "tx0" not in ids(results)
    in_view(window, results)
    window.close()
    pump(qt_app, 40)


def test_a_full_review_queue_replaces_a_row_the_operator_reviews(qt_app, config, store):
    app, window = full_session_window(qt_app, config, store, conf=0.2)
    click_menu_item(qt_app, window, "&View", window.review_action)
    view = window.timeline
    assert view.count() == LIMIT and "tx0" not in view.order()
    in_view(window, app.review_queue())
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    pump(qt_app, 10)
    anchor_id = view._anchor()[0]
    anchor_bubble = view._bubbles[anchor_id]
    before = viewport_y(view, anchor_bubble)

    # Correcting a transcript marks it reviewed, through the production
    # path, which publishes the update itself.
    app.correct(f"tx{LIMIT}", transcript="checked and fine")
    drain(qt_app, app)
    results = app.review_queue()
    assert ids(results)[-1] == "tx0" and f"tx{LIMIT}" not in ids(results)
    in_view(window, results)
    assert view._bubbles[anchor_id] is anchor_bubble
    assert abs(viewport_y(view, anchor_bubble) - before) <= 1
    window.close()
    pump(qt_app, 40)


def test_removing_a_shown_message_leaves_the_search_even_while_removed_messages_are_shown(
        qt_app, config, store, monkeypatch):
    """Finding B: the bubble menu's Remove message, with removed messages
    shown, used to leave the now-hidden bubble in a Search that the Store no
    longer answered with it."""
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    click_menu_item(qt_app, window, "&View", window.show_removed_action)
    assert window.show_removed_action.isChecked()
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    assert window.timeline.order() == ["tx1", "tx0"]
    asked = []
    monkeypatch.setattr(window, "_choose",
                        lambda *a, **k: (asked.append(a), window.REMOVE_KEEP)[1])

    bubble = window.timeline._bubbles["tx1"]
    click_bubble_menu(qt_app, bubble, bubble.remove_action)
    assert asked, "Remove message did not ask the question"
    assert store.get_transmission("tx1").hidden is True
    assert app.search("giraffe") and ids(app.search("giraffe")) == ["tx0"]
    assert window.timeline.order() == ["tx0"], "the removed message stayed in the Search view"
    assert (window._view_kind, window._view_query) == ("search", "giraffe")
    message = window.status.currentMessage()
    assert "1 match(es)" in message and "removed from the thread" in message

    # Show all: the thread, which does show removed messages, has it.
    click_menu_item(qt_app, window, "&View", window.show_all_action)
    assert window.timeline.order() == ["tx2", "tx1", "tx0"]
    assert window.timeline._bubbles["tx1"].tx.hidden is True
    window.close()
    pump(qt_app, 40)


def test_removing_and_deleting_from_a_full_search_bring_in_replacements(
        qt_app, config, store, monkeypatch):
    app, window = full_session_window(qt_app, config, store)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    view = window.timeline
    assert view.count() == LIMIT and "tx0" not in view.order()
    monkeypatch.setattr(window, "_choose", lambda *a, **k: window.REMOVE_KEEP)
    bubble = view._bubbles[f"tx{LIMIT}"]
    click_bubble_menu(qt_app, bubble, bubble.remove_action)
    results = app.search("giraffe")
    assert ids(results)[-1] == "tx0" and view.count() == LIMIT
    in_view(window, results)

    # Delete permanently, through the two real questions; the next record
    # the limit had kept out comes in.
    store.save_transmission(record(-1, "sg", "giraffe number minus one"))   # older than tx0
    answers = iter([window.DELETE_FOREVER, window.CONFIRM_DELETE])
    monkeypatch.setattr(window, "_choose", lambda *a, **k: next(answers))
    bubble = view._bubbles[f"tx{LIMIT - 1}"]
    click_bubble_menu(qt_app, bubble, bubble.remove_action)
    assert store.get_transmission(f"tx{LIMIT - 1}") is None
    results = app.search("giraffe")
    assert ids(results)[-1] == "tx-1" and f"tx{LIMIT - 1}" not in ids(results)
    in_view(window, results)
    window.close()
    pump(qt_app, 40)


def test_a_shown_row_that_is_hidden_or_deleted_by_an_event_leaves(qt_app, config, store,
                                                                 monkeypatch):
    """The event path, distinct from the menu: a shown row's update says it
    is now hidden (or it was deleted and a worker's late result arrives).
    Before, _belongs_here() rejected such an update outright and the old
    widget stayed."""
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    assert window.timeline.order() == ["tx1", "tx0"]

    hidden = store.hide_transmission("tx1", hidden=True)
    app.events.publish("updated", hidden)
    drain(qt_app, app)
    assert window.timeline.order() == ["tx0"] == ids(app.search("giraffe"))
    assert "1 match(es)" in window.status.currentMessage()

    # Deleted while shown; the late event must not draw it back, and the
    # widget it had must go.
    doomed = store.get_transmission("tx0")
    app.delete_permanently("tx0")
    app.events.publish("updated", doomed)
    drain(qt_app, app)
    assert window.timeline.order() == [] == ids(app.search("giraffe"))
    assert "0 match(es)" in window.status.currentMessage()

    # The Session's own thread, removed messages not shown: the same.
    click_menu_item(qt_app, window, "&View", window.show_all_action)
    assert window.timeline.order() == ["tx2"]
    hidden = store.hide_transmission("tx2", hidden=True)
    app.events.publish("updated", hidden)
    drain(qt_app, app)
    assert window.timeline.order() == []
    window.close()
    pump(qt_app, 40)


def test_the_removed_messages_preference_returns_to_the_thread(qt_app, config, store,
                                                               monkeypatch):
    """Preserved: toggling View > Show removed messages reloads the thread,
    as Show all does; the Store's Search keeps its own rule about hidden
    rows either way (not decided here)."""
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    store.hide_transmission("tx0", hidden=True)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    assert window.timeline.order() == ["tx1"] == ids(app.search("giraffe"))
    click_menu_item(qt_app, window, "&View", window.show_removed_action)
    assert window._view_kind == "thread"
    assert window.timeline.order() == ["tx2", "tx1", "tx0"]
    window.close()
    pump(qt_app, 40)


def test_one_query_per_drained_batch_and_none_for_other_sessions(qt_app, config, store,
                                                                 monkeypatch):
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    queries = []
    real_search = app.search
    monkeypatch.setattr(app, "search",
                        lambda *a, **k: (queries.append(a), real_search(*a, **k))[1])

    for i in range(20, 25):
        tx = record(i, "sg", f"giraffe {i}")
        store.save_transmission(tx)
        app.events.publish("transmission", tx)
    drain(qt_app, app)
    assert window.timeline.order()[:5] == ["tx24", "tx23", "tx22", "tx21", "tx20"]
    assert len(queries) == 1, f"{len(queries)} queries for one drained batch"

    for i in range(30, 33):                        # Ops traffic: not this view's
        tx = record(i, "so", f"giraffe in ops {i}")
        store.save_transmission(tx)
        app.events.publish("transmission", tx)
    drain(qt_app, app)
    assert len(queries) == 1 and window.timeline.count() == 7
    window.close()
    pump(qt_app, 40)


def test_reconciliation_never_queries_a_closing_store(qt_app, config, store, monkeypatch):
    app, window, general, ops = two_sessions_window(qt_app, config, store)
    search_via_menu(qt_app, window, monkeypatch, "giraffe")
    queries = []
    monkeypatch.setattr(app, "search", lambda *a, **k: (queries.append(a), [])[1])
    window._shut_store_routes()                    # the production shutter, as Quit uses it
    assert window._store_gone()
    tx = record(40, "sg", "giraffe late")
    store.save_transmission(tx)
    app.events.publish("transmission", tx)
    window._drain_events()
    window._refresh_view({"tx40"})
    window._after_local_change(tx)
    pump(qt_app, 10)
    assert queries == [] and window.timeline.order() == ["tx1", "tx0"]
    window._store_off_limits = False               # let the window close normally
    window.close()
    pump(qt_app, 40)
