"""Continuous playback in the window, and the playback bar.

The operator: pressing Play on a recording should carry on upwards - each
newer transmission of the same Session in turn - until Stop. And: a
permanent playback bar with Play/Pause and Stop, separate from Stop
monitoring; nothing plays without a deliberate target; choosing a message
alone stays silent.

A scripted backend stands in for the player: nothing reaches the speakers.
Synthetic rows and files only.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState, Session, Transmission

from test_alpha5_playback import make_scripted_backend, pump

T0 = dt.datetime(2026, 4, 1, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class Rig:
    """Two Sessions: A with two monitoring runs, B with one."""

    def __init__(self, qt_app, config, store, tmp_path, controllable=True):
        from babelfishr.ui.main_window import MainWindow
        from babelfishr.ui.playback import PlaybackController

        self.qt, self.store, self.tmp = qt_app, store, tmp_path
        self.app = BabelFishRApp(config=config, store=store)
        self.a = store.create_conversation("A")
        self.b = store.create_conversation("B")
        for sid, conv in (("a-run1", self.a), ("a-run2", self.a), ("b-run", self.b)):
            store.save_session(Session(id=sid, conversation_id=conv.id))
        self.rows = {}
        self.add("t0", "a-run1", 0)
        self.add("t1-hidden", "a-run1", 10, hidden=True)
        self.add("other", "b-run", 15)
        self.add("t2-missing", "a-run2", 20, file=False)
        self.add("t3", "a-run2", 30)
        self.add("t4", "a-run2", 30)                       # same instant as t3, saved later
        self.app.select_conversation(self.a.id)
        self.window = MainWindow(self.app)
        self.backend = make_scripted_backend(controllable=controllable)
        self.controller = PlaybackController(self.backend, parent=self.window)
        self.window.timeline.set_playback(self.controller)
        self.window._reload_timeline()
        self.window.show()
        pump(qt_app)

    def add(self, name, run, seconds, file=True, hidden=False):
        path = self.tmp / f"{name}.wav"
        if file:
            path.write_bytes(b"RIFF synthetic")
        tx = Transmission(id=name, session_id=run, duration=1.0, audio_path=str(path),
                          started_at=T0 + dt.timedelta(seconds=seconds),
                          state=ProcessingState.COMPLETE, transcript=f"words {name}",
                          hidden=hidden)
        self.store.save_transmission(tx)
        self.rows[name] = tx
        return tx

    def path(self, name):
        return str(self.tmp / f"{name}.wav")

    def finish(self):
        """The clip plays to its own end, then queued work runs."""
        self.backend.advance(10_000)
        pump(self.qt, 20)

    def loads(self):
        return [c[1] for c in self.backend.calls if c[0] == "load"]

    def close(self):
        self.window.close()
        self.app.close()


@pytest.fixture
def rig(qt_app, config, store, tmp_path):
    made = Rig(qt_app, config, store, tmp_path)
    yield made
    made.close()


# --- no implicit target -------------------------------------------------------------

def test_nothing_plays_without_a_deliberate_target(rig):
    w = rig.window
    assert w.selected_hint.text() == "Select a transmission to play."
    assert not w.selected_play_button.isEnabled()
    assert not w.playback_stop_button.isEnabled()
    w._play_selected()                                   # Play with nothing chosen
    pump(rig.qt, 20)
    w.timeline.select("t0")                              # then choose a message
    pump(rig.qt, 20)
    assert rig.loads() == [], "choosing a message alone must stay silent"
    assert w.selected_play_button.isEnabled()
    w.selected_play_button.click()                       # the explicit Play
    pump(rig.qt)
    assert rig.loads() == [rig.path("t0")]
    assert w.playback_stop_button.isEnabled()


def test_no_autoplay_on_reload_new_arrivals_or_a_fresh_tab(rig):
    w = rig.window
    w._reload_timeline()
    new = rig.add("t9", "a-run2", 90)
    rig.app.events.publish("transmission", new)
    w._drain_events()
    tabs = w.session_tabs
    for i in range(tabs.count()):
        tabs.setCurrentIndex(i)
    pump(rig.qt, 20)
    assert rig.loads() == []


def test_a_missing_original_cannot_be_targeted(rig):
    w = rig.window
    w.timeline.select("t2-missing")
    assert not w.selected_play_button.isEnabled()
    w._play_selected()
    assert not w.timeline.bubble("t2-missing").play_button.isEnabled()
    pump(rig.qt)
    assert rig.loads() == []


# --- the sequence -------------------------------------------------------------------

def test_play_continues_upwards_through_the_session_and_ends_at_the_newest(rig):
    w = rig.window
    w.timeline.bubble("t0").play_button.click()          # inline Play: target + play
    pump(rig.qt)
    assert rig.loads() == [rig.path("t0")]
    assert "continues with newer" in w.playback_line.text()
    rig.finish()
    # Skipped: the removed-from-view one, the missing file and Session B.
    assert rig.loads()[-1] == rig.path("t3")
    rig.finish()
    assert rig.loads()[-1] == rig.path("t4"), "a tie plays in saved order"
    rig.finish()
    assert rig.loads() == [rig.path(n) for n in ("t0", "t3", "t4")], "no wrap-around"
    assert rig.controller.owner is None
    assert not w.sequence.active and not w.playback_stop_button.isEnabled()


def test_stop_playback_ends_it_and_late_events_restart_nothing(rig):
    w = rig.window
    w.timeline.bubble("t0").play_button.click()
    pump(rig.qt)
    assert w.playback_stop_button.isEnabled()
    w.playback_stop_button.click()
    pump(rig.qt, 20)
    assert rig.controller.owner is None and not w.sequence.active
    rig.backend.finished.emit()                          # a late report
    w.sequence.notify_new_recording()
    pump(rig.qt, 20)
    assert rig.loads() == [rig.path("t0")]


def test_pause_and_resume_keep_the_sequence(rig):
    w = rig.window
    w.timeline.bubble("t0").play_button.click()
    pump(rig.qt)
    w.playback_pause_button.click()
    pump(rig.qt)
    assert w.playback_pause_button.text() == "Resume"
    assert w.playback_line.text().startswith("Paused")
    w.playback_pause_button.click()
    pump(rig.qt)
    rig.finish()
    assert rig.loads()[-1] == rig.path("t3")


def test_short_clips_continue_and_stop_is_offered_throughout(rig):
    for name in ("t0", "t3", "t4"):
        rig.backend.durations[rig.path(name)] = 300
    w = rig.window
    w.timeline.bubble("t0").play_button.click()
    pump(rig.qt)
    assert w.playback_stop_button.isEnabled()
    rig.backend.advance(300)
    pump(rig.qt, 20)
    assert rig.loads()[-1] == rig.path("t3")
    assert w.playback_stop_button.isEnabled()


def test_another_manual_play_replaces_the_sequence(rig):
    w = rig.window
    w.timeline.bubble("t0").play_button.click()
    pump(rig.qt)
    rig.controller.play("other-clip", rig.path("t4"))   # e.g. a single-clip preview
    pump(rig.qt, 20)
    assert not w.sequence.active
    rig.finish()
    assert rig.loads() == [rig.path("t0"), rig.path("t4")], "nothing followed the preview"


def test_switching_session_stops_and_never_resumes(rig):
    w = rig.window
    w.timeline.bubble("t0").play_button.click()
    pump(rig.qt)
    tabs = w.session_tabs
    other = next(i for i in range(tabs.count()) if tabs.tabData(i) == rig.b.id)
    tabs.setCurrentIndex(other)
    pump(rig.qt, 20)
    assert rig.controller.owner is None and not w.sequence.active
    rig.finish()
    assert rig.loads() == [rig.path("t0")]


def test_while_the_session_is_monitored_it_waits_and_follows_new_recordings(rig):
    w = rig.window
    rig.app.capture = object()                           # monitoring into A
    rig.app._capture_conversation_id = rig.a.id
    try:
        w.timeline.bubble("t4").play_button.click()
        pump(rig.qt)
        rig.finish()
        assert w.sequence.waiting
        assert w.playback_stop_button.isEnabled()
        assert w.playback_line.text().startswith("Waiting")
        new = rig.add("t5", "a-run2", 50)                 # a new original is saved
        rig.app.events.publish("transmission", new)
        w._drain_events()
        pump(rig.qt, 20)
        assert rig.loads()[-1] == rig.path("t5")
        rig.finish()
        assert w.sequence.waiting
    finally:
        rig.app.capture = None
        rig.app._capture_conversation_id = ""
    w._drain_events()                                    # monitoring has ended
    pump(rig.qt, 20)
    assert not w.sequence.active and not w.playback_stop_button.isEnabled()


def test_stop_while_waiting(rig):
    w = rig.window
    rig.app.capture = object()
    rig.app._capture_conversation_id = rig.a.id
    try:
        w.timeline.bubble("t4").play_button.click()
        pump(rig.qt)
        rig.finish()
        assert w.sequence.waiting
        w.timeline.select(None)                           # no message selected
        w.playback_stop_button.click()
        pump(rig.qt)
        assert not w.sequence.active
        new = rig.add("t6", "a-run2", 60)
        rig.app.events.publish("transmission", new)
        w._drain_events()
        pump(rig.qt, 20)
        assert rig.path("t6") not in rig.loads()
    finally:
        rig.app.capture = None
        rig.app._capture_conversation_id = ""


def test_browsing_does_not_interrupt_the_sequence(rig):
    w = rig.window
    w.timeline.bubble("t0").play_button.click()
    pump(rig.qt)
    w.timeline.select("t4")
    w.timeline.select("t0")
    pump(rig.qt)
    assert rig.controller.owner == "t0"
    rig.finish()
    assert rig.loads()[-1] == rig.path("t3")


def test_an_external_player_never_cascades(qt_app, config, store, tmp_path):
    """A system-open backend reports "finished" as soon as it has launched
    the player: nothing may follow it."""
    made = Rig(qt_app, config, store, tmp_path, controllable=False)
    try:
        made.window.timeline.bubble("t0").play_button.click()
        pump(qt_app)
        made.finish()
        assert made.loads() == [made.path("t0")]
        assert not made.window.sequence.active
    finally:
        made.close()


# --- the fixed bar: AUDIO PLAYBACK, one action per situation -------------------------

def test_the_bar_is_named_and_offers_one_pause_and_a_startable_new_row(rig):
    from PySide6 import QtWidgets

    w = rig.window
    assert w.selected_panel.accessibleName() == "Audio playback"
    titles = [l.text() for l in w.selected_panel.findChildren(QtWidgets.QLabel)]
    assert "AUDIO PLAYBACK" in titles and "SELECTED AUDIO" not in titles
    w.timeline.select("t0")
    w.selected_play_button.click()
    pump(rig.qt)
    # The current recording is selected: Play is not a second Pause.
    assert w.selected_play_button.text() == "Now playing"
    assert not w.selected_play_button.isEnabled()
    pauses = [b for b in w.selected_panel.findChildren(QtWidgets.QPushButton)
              if b.isEnabled() and b.text() in ("Pause", "Resume")]
    assert pauses == [w.playback_pause_button]
    w.playback_pause_button.click()
    pump(rig.qt)
    assert w.selected_play_button.text() == "Paused"
    assert w.playback_pause_button.text() == "Resume"
    # Another playable row is still startable from the bar.
    w.timeline.select("t3")
    assert w.selected_play_button.text() == "Play selected"
    assert w.selected_play_button.isEnabled()
    w.selected_play_button.click()
    pump(rig.qt)
    assert rig.loads()[-1] == rig.path("t3") and rig.controller.owner == "t3"
    # Stop ends the Session's playback whatever is selected.
    w.timeline.select(None)
    assert w.playback_stop_button.isEnabled()
    w.playback_stop_button.click()
    pump(rig.qt, 20)
    assert rig.controller.owner is None and not w.sequence.active
