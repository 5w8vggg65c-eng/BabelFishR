"""Playback notifications reach the bubbles they concern, and history is
inserted where it belongs (F7, first pass).

Before: every bubble listened to the controller itself AND the view redrew
every bubble on every change, so one change redrew a thread of N bubbles
twice; every position tick reached all N bubbles; and each record of history
was inserted at the top, removed and re-inserted at the bottom. Here the
controller names the recordings a change concerns, the view redraws only
their bubbles (inside the anchoring), position reports go to the owner's
bubble alone, and history goes straight into its final place.

Real Qt widgets (offscreen), real QTest clicks, the scripted playback backend
from test_alpha5_playback (records every call, reports state like a player).
Work is counted by wrapping the bubble's render and position slots at class
level - callbacks as well as renders - so an all-bubble dispatch hidden behind
an early return would still be counted. No sound device; not a Mac test.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib

import pytest
from PySide6 import QtCore

from babelfishr.models import ProcessingState, Transmission
from babelfishr.ui import timeline as tl
from babelfishr.ui.playback import PAUSED, PLAYING, STOPPED, PlaybackController

_spec = importlib.util.spec_from_file_location(
    "playback_helpers", pathlib.Path(__file__).with_name("test_alpha5_playback.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
make_scripted_backend, make_view, click, pump, tx = (
    _helpers.make_scripted_backend, _helpers.make_view, _helpers.click,
    _helpers.pump, _helpers.tx)
qt_app = _helpers.qt_app
wavs = _helpers.wavs

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def settle(qt_app, rounds: int = 10) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


@pytest.fixture
def work(monkeypatch):
    """Per-bubble counts of render and position callbacks."""
    renders, positions = {}, {}
    real_render = tl.TransmissionBubble._render_playback
    real_position = tl.TransmissionBubble._on_playback_position

    def render(self):
        renders[self.tx.id] = renders.get(self.tx.id, 0) + 1
        return real_render(self)

    def position(self, *args):
        positions[self.tx.id] = positions.get(self.tx.id, 0) + 1
        return real_position(self, *args)

    monkeypatch.setattr(tl.TransmissionBubble, "_render_playback", render)
    monkeypatch.setattr(tl.TransmissionBubble, "_on_playback_position", position)

    class Work:
        def reset(self):
            renders.clear()
            positions.clear()

        @property
        def renders(self):
            return dict(renders)

        @property
        def positions(self):
            return dict(positions)

        def only(self, *ids):
            """Rendered exactly this set of bubbles (any positive counts)."""
            assert set(renders) == set(ids), f"rendered {sorted(renders)}, expected {sorted(ids)}"

    return Work()


def thread(qt_app, wavs, n, long_seconds=9.0):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    backend.durations[wavs["short"]] = 2000
    view, controller = make_view(backend)
    view.resize(900, 600)
    view.show()
    records = [tx(f"m{i:04d}", long_seconds, wavs["long"],
                  started_at=T0 + dt.timedelta(seconds=i), transcript=f"message {i} " * 4)
               for i in range(n)]
    view.set_transmissions(records)
    settle(qt_app)
    return backend, view, controller


def controls_agree(view, controller, backend, owner, state):
    """Controller, backend and visible controls tell the same story."""
    assert controller.owner == owner
    assert backend.state() == state
    for tx_id, bubble in view._bubbles.items():
        mine = tx_id == owner
        assert controller.state_for(tx_id) == (state if mine else STOPPED)
        if mine and state in (PLAYING, PAUSED):
            assert bubble.playback_bar.isVisible() and bubble.play_button.isHidden()
            assert bubble.pause_button.text().startswith("⏸" if state == PLAYING else "▶")
        else:
            assert bubble.playback_bar.isHidden()
            assert bubble.play_button.isVisible() and bubble.play_button.isEnabled()


# ---- routing ------------------------------------------------------------------------

def test_play_pause_resume_stop_touch_only_the_owner(qt_app, wavs, work):
    backend, view, controller = thread(qt_app, wavs, 40)
    a = view._bubbles["m0020"]
    work.reset()
    click(qt_app, a.play_button)
    controls_agree(view, controller, backend, "m0020", PLAYING)
    work.only("m0020")
    # Loading reports the duration, which the controller passes on as one
    # position report - to the owner alone.
    assert work.positions == {"m0020": 1}
    backend.advance(700)
    assert work.positions == {"m0020": 2} and a.position_label.text().startswith("0:00")
    work.reset()
    click(qt_app, a.pause_button)
    controls_agree(view, controller, backend, "m0020", PAUSED)
    work.only("m0020")
    work.reset()
    click(qt_app, a.pause_button)                              # resume
    controls_agree(view, controller, backend, "m0020", PLAYING)
    assert controller.position_for("m0020") == (700, 9000)
    work.only("m0020")
    work.reset()
    click(qt_app, a.stop_button)
    controls_agree(view, controller, backend, None, STOPPED)
    work.only("m0020")


def test_a_takeover_redraws_the_previous_and_the_new_owner_only(qt_app, wavs, work):
    backend, view, controller = thread(qt_app, wavs, 40)
    a, b = view._bubbles["m0010"], view._bubbles["m0030"]
    click(qt_app, a.play_button)
    backend.advance(500)
    work.reset()
    click(qt_app, b.play_button)
    controls_agree(view, controller, backend, "m0030", PLAYING)
    assert backend.position() == 0
    work.only("m0010", "m0030")
    assert work.positions == {"m0030": 1}                     # the duration report, to B only
    # Two things happened to A (its backend stop, then the takeover naming
    # it): a bounded, legitimate handful of redraws, not one per bubble.
    assert work.renders["m0010"] <= 2 and work.renders["m0030"] <= 2


def test_bs_missing_file_touches_b_only_and_a_retry_clears_bs_error(qt_app, wavs, work, tmp_path):
    backend, view, controller = thread(qt_app, wavs, 40)
    missing = tmp_path / "missing-b.wav"
    view.update(tx("m0030", 9.0, str(missing), started_at=T0 + dt.timedelta(seconds=30)))
    a, b = view._bubbles["m0010"], view._bubbles["m0030"]
    click(qt_app, a.play_button)
    backend.advance(700)
    work.reset()
    click(qt_app, b.play_button)
    controls_agree(view, controller, backend, "m0010", PLAYING)
    assert controller.position_for("m0010") == (700, 9000)
    assert "Could not play" in b.status_label.text() and "missing" in b.status_label.text()
    work.only("m0030")
    # The file appears; the retry succeeds and B's error line goes away.
    import shutil
    shutil.copy(wavs["long"], missing)
    work.reset()
    click(qt_app, b.play_button)
    controls_agree(view, controller, backend, "m0030", PLAYING)
    assert "Could not play" not in b.status_label.text()
    assert "m0030" not in controller.last_error
    work.only("m0010", "m0030")


def test_completion_duration_and_position_reach_the_owner_only(qt_app, wavs, work):
    backend, view, controller = thread(qt_app, wavs, 40)
    a = view._bubbles["m0005"]
    click(qt_app, a.play_button)
    work.reset()
    backend.durations[wavs["long"]] = 9000
    backend.durationChanged.emit(12000)                      # a late duration report
    assert work.positions == {"m0005": 1} and "0:12" in a.position_label.text()
    for _ in range(5):
        backend.advance(1000)
    assert work.positions == {"m0005": 6}
    assert a.position_label.text().startswith("0:05")
    assert work.renders == {}
    work.reset()
    backend.advance(20000)                                    # runs to the end
    controls_agree(view, controller, backend, None, STOPPED)
    work.only("m0005")


def test_the_work_does_not_grow_with_the_thread(qt_app, wavs, work):
    def workload(n):
        backend, view, controller = thread(qt_app, wavs, n)
        a, b = view._bubbles["m0001"], view._bubbles["m0002"]
        work.reset()
        click(qt_app, a.play_button)
        backend.advance(300)
        click(qt_app, a.pause_button)
        click(qt_app, a.pause_button)
        click(qt_app, b.play_button)                          # takeover
        backend.advance(300)
        click(qt_app, b.stop_button)
        totals = (sum(work.renders.values()), sum(work.positions.values()), set(work.renders))
        view.close()
        settle(qt_app)
        return totals

    small = workload(6)
    large = workload(240)
    assert small == large, f"work grew with the thread: {small} -> {large}"
    assert small[2] == {"m0001", "m0002"}


# ---- lifecycle ----------------------------------------------------------------------

def test_cleared_or_removed_bubbles_receive_nothing(qt_app, wavs, work):
    backend, view, controller = thread(qt_app, wavs, 12)
    gone = view._bubbles["m0003"]
    view.remove("m0003")
    view.set_transmissions([tx("n1", 9.0, wavs["long"]), tx("n2", 9.0, wavs["long"])])
    settle(qt_app)
    work.reset()
    controller.changed.emit(frozenset({"m0003", "m0005", "n1"}))
    controller.positionChanged.emit("m0005", 100, 9000)
    controller.positionChanged.emit("n1", 100, 9000)
    assert work.renders == {"n1": 1}
    assert work.positions == {"n1": 1}
    # Real traffic after a switch of threads still lands in the right place.
    click(qt_app, view._bubbles["n2"].play_button)
    controls_agree(view, controller, backend, "n2", PLAYING)
    view.close()
    settle(qt_app)


def test_a_standalone_bubble_still_follows_its_controller(qt_app, wavs, work):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    controller = PlaybackController(backend)
    alone = tl.TransmissionBubble(tx("solo", 9.0, wavs["long"]), controller)
    other = tl.TransmissionBubble(tx("other", 9.0, wavs["long"]), controller)
    alone.show()
    other.show()
    pump(qt_app)
    work.reset()
    click(qt_app, alone.play_button)
    assert controller.owner == "solo" and controller.state_for("solo") == PLAYING
    assert alone.playback_bar.isVisible() and other.playback_bar.isHidden()
    assert work.renders == {"solo": 1}, work.renders
    backend.advance(400)
    # The duration report and the tick: each standalone bubble listens for
    # itself, so both hear both - the cost of standing alone.
    assert work.positions == {"solo": 2, "other": 2}
    assert alone.position_label.text().startswith("0:00") and other.position_label.text() == ""
    click(qt_app, alone.stop_button)
    assert controller.owner is None and alone.playback_bar.isHidden()


def test_a_transcript_update_during_playback_keeps_position_and_controls(qt_app, wavs, work):
    backend, view, controller = thread(qt_app, wavs, 12)
    a = view._bubbles["m0004"]
    click(qt_app, a.play_button)
    backend.advance(2500)
    label_before = a.position_label.text()
    updated = tx("m0004", 9.0, wavs["long"], started_at=T0 + dt.timedelta(seconds=4),
                 transcript="a much longer transcript arriving mid-playback " * 5,
                 translation="and its translation " * 5)
    view.update(updated)
    pump(qt_app)
    controls_agree(view, controller, backend, "m0004", PLAYING)
    assert backend.position() == 2500 and controller.position_for("m0004") == (2500, 9000)
    assert a.position_label.text() == label_before
    assert "much longer" in a.transcript_label.text() if hasattr(a, "transcript_label") else True


# ---- history insertion -----------------------------------------------------------------

def test_history_is_inserted_once_in_its_final_place(qt_app, wavs):
    backend, view, controller = thread(qt_app, wavs, 0)
    inserts, removes = [], []
    real_insert, real_remove = view._layout.insertWidget, view._layout.removeWidget
    view._layout.insertWidget = lambda index, w, *a, **k: (inserts.append(index), real_insert(index, w, *a, **k))[1]
    view._layout.removeWidget = lambda w: (removes.append(w), real_remove(w))
    records = [tx(f"h{i:03d}", 3.0, wavs["long"], started_at=T0 + dt.timedelta(seconds=i)) for i in range(50)]
    view.set_transmissions(records)
    assert len(inserts) == 50 and removes == [], (len(inserts), len(removes))
    assert inserts == list(range(50)), "each record went straight to the bottom"
    assert view.order() == [f"h{i:03d}" for i in reversed(range(50))]
    assert set(view._bubbles) == {r.id for r in records}
    # Layout membership: bubbles contiguous from the top, then the empty
    # state label (hidden), then the stretch.
    widgets = [view._layout.itemAt(i).widget() for i in range(view._layout.count())]
    assert widgets[:50] == [view._bubbles[i] for i in view.order()]
    assert widgets[50] is view.empty_label and widgets[51] is None
    assert view.empty_label.isHidden()
    # Ties on started_at fall back to the id; an existing record is updated, not duplicated.
    view.set_transmissions([tx("z", 3.0, wavs["long"], started_at=T0), tx("a", 3.0, wavs["long"], started_at=T0)])
    assert view.order() == ["z", "a"]
    view.append_older(tx("a", 3.0, wavs["long"], started_at=T0, transcript="updated"))
    assert view.order() == ["z", "a"] and view.count() == 2
    view.clear()
    assert view.empty_label.isVisible() and view.count() == 0


def viewport_y(view, bubble):
    return bubble.mapTo(view.viewport(), QtCore.QPoint(0, 0)).y()


def test_reading_position_holds_through_insertion_growth_and_playback_above(qt_app, wavs):
    backend, view, controller = thread(qt_app, wavs, 40)
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    settle(qt_app)
    anchor_id = view._anchor()[0]
    anchor = view._bubbles[anchor_id]
    before = viewport_y(view, anchor)
    above = view.order()[2]                                  # near the top, above the viewport
    # A record placed above by time.
    view.place(tx("newer", 9.0, wavs["long"], started_at=T0 + dt.timedelta(seconds=100)))
    settle(qt_app)
    assert abs(viewport_y(view, anchor) - before) <= 1
    # A bubble above grows.
    grown = view._bubbles[above].tx
    view.update(tx(above, 9.0, wavs["long"], started_at=grown.started_at,
                   transcript="and much more was said " * 12))
    settle(qt_app)
    assert abs(viewport_y(view, anchor) - before) <= 1
    # Playback controls expand on a bubble above ... and collapse again.
    click(qt_app, view._bubbles[above].play_button)
    settle(qt_app)
    assert view._bubbles[above].playback_bar.isVisible()
    assert abs(viewport_y(view, anchor) - before) <= 1, "expanding controls above moved the text"
    click(qt_app, view._bubbles[above].stop_button)
    settle(qt_app)
    assert view._bubbles[above].playback_bar.isHidden()
    assert abs(viewport_y(view, anchor) - before) <= 1, "collapsing controls above moved the text"
    assert view._bubbles[anchor_id] is anchor


def test_a_takeover_collapses_the_previous_owner_before_the_backend_reports_its_stop(qt_app, wavs,
                                                                                    work):
    """QtMultimedia reports state changes asynchronously; the scripted
    backend reports them at once. With a backend that says nothing until
    later, the previous owner's controls must still collapse the moment
    ownership moves - the change names the previous owner itself, rather
    than relying on the backend's stop report to do it."""
    backend, view, controller = thread(qt_app, wavs, 12)
    a, b = view._bubbles["m0003"], view._bubbles["m0007"]
    click(qt_app, a.play_button)
    assert a.playback_bar.isVisible()
    real_stop = backend.stop

    def silent_stop():
        backend.calls.append(("stop",))              # the player stops ...
        backend._position = 0
        backend._state = STOPPED                     # ... and will report it later

    backend.stop = silent_stop
    work.reset()
    click(qt_app, b.play_button)
    assert controller.owner == "m0007" and controller.state_for("m0003") == STOPPED
    assert a.playback_bar.isHidden() and a.play_button.isVisible(), "A's controls stayed open"
    assert b.playback_bar.isVisible()
    work.only("m0003", "m0007")
    backend.stop = real_stop
    # (What a stop report arriving *after* the takeover would do to the new
    # owner is a separate question about the controller and QtMultimedia's
    # ordering, recorded on the ledger; not this test's subject.)
