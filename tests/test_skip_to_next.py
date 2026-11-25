"""+5 s near the end of a clip moves continuous playback to the next one.

With 5 s or less left, the playing card's +5 s advances once to the START of
the next playable original in the same Session - by the same rules as a
natural end (missing files skipped, waiting while the Session is live, the
end of the Session). With more left it seeks as before. Pressing twice, a
Stop, a Pause, retargeting and stale end reports from the player never
advance twice, restart a clip or cross Sessions; the waveform and the
scrolled card follow. The real card button is clicked; a scripted backend
stands in for the player (no sound): once issuing no "finished" at all for
the skipped clip, once issuing it late.
"""

from __future__ import annotations

import os

import pytest
from PySide6 import QtCore
from PySide6.QtTest import QTest

from test_alpha5_playback import pump
from test_continuous_playback_window import Rig


@pytest.fixture(scope="module")
def qt_app():
    from PySide6 import QtWidgets
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class LongRig(Rig):
    """Nine-second recordings, so each card has its transport bar."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, tx in self.rows.items():
            tx.duration = 9.0
            self.store.save_transmission(tx)
            self.backend.durations[self.path(name)] = 9000
        self.window._reload_timeline()
        pump(self.qt, 10)

    def play(self, name):
        self.window.timeline.bubble(name).play_button.click()
        pump(self.qt)

    def plus_five(self, pump_after=True):
        bubble = self.window.timeline.bubble(self.controller.owner)
        assert bubble.forward_button.isVisible(), "the card's +5 s is not offered"
        QTest.mouseClick(bubble.forward_button, QtCore.Qt.LeftButton)
        if pump_after:
            pump(self.qt, 20)

    def position(self):
        return self.backend.position()


@pytest.fixture
def rig(qt_app, config, store, tmp_path):
    made = LongRig(qt_app, config, store, tmp_path)
    yield made
    made.close()


def test_more_than_five_seconds_left_seeks_as_before(rig):
    rig.play("t0")
    rig.backend.advance(2000)                          # 7 s left
    rig.plus_five()
    assert rig.controller.owner == "t0" and rig.position() == 7000
    assert rig.loads() == [rig.path("t0")]


def test_five_seconds_or_less_left_starts_the_next_clip_from_its_start(rig):
    w = rig.window
    rig.play("t0")
    rig.backend.advance(4500)                          # 4.5 s left
    rig.plus_five()                                    # the player reports no "finished"
    assert rig.controller.owner == "t3"                # hidden and missing skipped
    assert rig.loads() == [rig.path("t0"), rig.path("t3")]
    assert rig.position() == 0, "the next clip did not start from its start"
    assert w.waveform.current_tx_id == "t3"
    assert w.timeline.now_playing == "t3"
    assert w.sequence.active and w.sequence.current_id == "t3"
    assert rig.controller.state_for("t0") == "stopped"


def test_a_late_end_report_from_the_skipped_clip_does_not_advance_again(rig):
    rig.play("t0")
    rig.backend.advance(5000)
    rig.plus_five()
    assert rig.controller.owner == "t3"
    rig.backend.finished.emit()                        # late, from t0
    pump(rig.qt, 20)
    assert rig.controller.owner == "t3"
    assert rig.loads() == [rig.path("t0"), rig.path("t3")]
    rig.finish()                                       # t3's own natural end still advances
    assert rig.controller.owner == "t4"


def test_pressing_twice_advances_once(rig):
    rig.play("t0")
    rig.backend.advance(6000)
    rig.plus_five(pump_after=False)
    bubble = rig.window.timeline.bubble("t0")
    QTest.mouseClick(bubble.forward_button, QtCore.Qt.LeftButton)   # before the advance runs
    pump(rig.qt, 20)
    assert rig.loads() == [rig.path("t0"), rig.path("t3")], "advanced twice"


def test_at_the_newest_clip_it_ends_without_wrapping(rig):
    rig.play("t4")
    rig.backend.advance(6000)
    rig.plus_five()
    assert rig.controller.owner is None and not rig.window.sequence.active
    assert rig.loads() == [rig.path("t4")]
    assert not rig.window.playback_stop_button.isEnabled()


def test_while_the_session_is_live_it_waits_for_the_next_recording(rig):
    w = rig.window
    rig.app.capture = object()
    rig.app._capture_conversation_id = rig.a.id
    try:
        rig.play("t4")
        rig.backend.advance(6000)
        rig.plus_five()
        assert w.sequence.waiting and w.playback_stop_button.isEnabled()
        new = rig.add("t7", "a-run2", 70)
        rig.store.save_transmission(new)
        rig.app.events.publish("transmission", new)
        w._drain_events()
        pump(rig.qt, 20)
        assert rig.loads()[-1] == rig.path("t7")
    finally:
        rig.app.capture = None
        rig.app._capture_conversation_id = ""


def test_stop_right_after_the_press_starts_nothing(rig):
    w = rig.window
    rig.play("t0")
    rig.backend.advance(6000)
    rig.plus_five(pump_after=False)
    w.playback_stop_button.click()                     # before the advance runs
    pump(rig.qt, 20)
    assert rig.controller.owner is None and not w.sequence.active
    assert rig.loads() == [rig.path("t0")]


def test_paused_near_the_end_advances_once_and_plays_the_next(rig):
    w = rig.window
    rig.play("t0")
    rig.backend.advance(6000)
    w.playback_pause_button.click()
    pump(rig.qt)
    rig.plus_five()
    assert rig.controller.owner == "t3" and rig.controller.state_for("t3") == "playing"
    assert rig.loads() == [rig.path("t0"), rig.path("t3")]


def test_a_retarget_then_plus_five_follows_the_new_clip(rig):
    rig.play("t0")
    rig.play("t3")                                     # the operator retargets
    rig.backend.advance(6000)
    rig.plus_five()
    assert rig.controller.owner == "t4"
    assert rig.path("other") not in rig.loads(), "crossed into another Session"
