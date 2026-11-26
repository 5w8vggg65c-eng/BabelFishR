"""Continuous playback keeps the operator oriented.

Mac Build 40: sequential audio played correctly, but the waveform and the
view stayed on the first transmission. Now, while an original plays:

* the waveform shows the recording being heard, with its own moving
  playhead, and changes with each clip;
* the playing card is highlighted and brought into view at each new clip,
  even after the operator scrolled away;
* in Needs review, Bookmarked or Search the sequence still traverses the
  whole Session: a clip outside the filter is shown while it plays (and the
  bar says so), then taken away again - the filter stays, the clip is never
  hidden, playback is never reset, and other Sessions never appear;
* choosing a message only targets it (no autoplay), and Stop or a Session
  change makes any pending scroll stale.

Real synthetic WAVs, a scripted backend (no sound), real widget geometry.
"""

from __future__ import annotations

import datetime as dt

import pytest
from PySide6 import QtCore

from babelfishr.audio.wavefile import write_wav
from babelfishr.models import ProcessingState, Transmission
from babelfishr.testing import tone

from test_alpha5_playback import pump
from test_continuous_playback_window import T0, Rig

SR = 8000


@pytest.fixture(scope="module")
def qt_app():
    import os

    from PySide6 import QtWidgets
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FollowRig(Rig):
    """The Rig's Sessions, real WAVs, and enough older messages that the
    thread scrolls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("t0", "t3", "t4", "other"):
            write_wav(self.path(name), tone(1.0, 440.0, SR), SR)
        for i in range(25):                          # older filler, all playable
            self.add(f"old{i:02d}", "a-run1", -600 + i)
            write_wav(self.path(f"old{i:02d}"), tone(0.5, 300.0, SR), SR)
        self.window.resize(1000, 640)
        self.window._reload_timeline()
        pump(self.qt, 10)

    def visible(self, tx_id):
        """The card is inside the list's viewport."""
        view = self.window.timeline
        bubble = view.bubble(tx_id)
        if bubble is None or not bubble.isVisible():
            return False
        top = bubble.mapTo(view.viewport(), QtCore.QPoint(0, 0)).y()
        return top + bubble.height() > 0 and top < view.viewport().height()


@pytest.fixture
def rig(qt_app, config, store, tmp_path):
    made = FollowRig(qt_app, config, store, tmp_path)
    yield made
    made.close()


def scroll_to_bottom(rig):
    bar = rig.window.timeline.verticalScrollBar()
    bar.setValue(bar.maximum())
    pump(rig.qt, 10)


def test_the_waveform_follows_each_clip_with_its_own_playhead(rig):
    w = rig.window
    w.timeline.select("t0")
    w.selected_play_button.click()
    pump(rig.qt)
    assert (w.waveform.current_tx_id, w.waveform.current_path) == ("t0", rig.path("t0"))
    rig.backend.advance(400)
    pump(rig.qt)
    assert w.waveform.marker_visible and w.waveform.marker_ms == 400
    rig.finish()                                         # on to t3
    assert rig.controller.owner == "t3"
    assert (w.waveform.current_tx_id, w.waveform.current_path) == ("t3", rig.path("t3"))
    rig.backend.advance(250)
    pump(rig.qt)
    assert w.waveform.marker_visible and w.waveform.marker_ms == 250, \
        "the playhead belongs to the clip being heard"
    assert w.timeline.selected_id == "t3", "the teal selection follows the recording"
    assert w.timeline.now_playing == "t3"
    assert w.timeline.bubble("t3").property("nowPlaying") is True
    assert w.timeline.bubble("t0").property("nowPlaying") is False


def test_each_new_clip_is_brought_into_view_after_scrolling_away(rig):
    w = rig.window
    rig.play("t0")
    pump(rig.qt, 10)
    scroll_to_bottom(rig)                                 # the operator reads old traffic
    assert not rig.visible("t3")
    rig.finish()
    assert rig.controller.owner == "t3"
    assert rig.visible("t3"), "the new clip's card was not brought into view"
    scroll_to_bottom(rig)
    assert not rig.visible("t4")
    rig.finish()
    assert rig.visible("t4")


def test_a_clip_outside_the_filter_is_shown_while_it_plays_and_the_filter_stays(rig):
    w = rig.window
    rig.app.bookmark("t0", True)
    w.filter_bookmarked.click()
    pump(rig.qt)
    assert w.timeline.order() == ["t0"]
    rig.play("t0")
    pump(rig.qt)
    rig.finish()                                          # t3: not bookmarked
    assert rig.controller.owner == "t3"
    assert "t3" in w.timeline.order(), "the playing clip was hidden by the filter"
    assert w.filter_bookmarked.isChecked() and w._view_kind == "bookmarked"
    assert "not in this view" in w.playback_line.text()
    assert rig.visible("t3") and w.waveform.current_tx_id == "t3"
    assert "other" not in w.timeline.order(), "another Session leaked in"
    rig.finish()                                          # t4 replaces t3
    assert w.timeline.order().count("t4") == 1 and "t3" not in w.timeline.order()
    rig.finish()                                          # the end: the filter is whole again
    assert w.timeline.order() == ["t0"]
    assert w.filter_bookmarked.isChecked()


def test_editing_a_playing_clip_outside_the_filter_keeps_the_editor_open(rig):
    w = rig.window
    rig.app.bookmark("t0", True)
    w.filter_bookmarked.click()
    rig.play("t0")
    rig.finish()
    bubble = w.timeline.bubble("t3")
    bubble.edit_button.click()
    bubble.original_label.setPlainText("draft stays here")
    rig.finish()
    assert rig.controller.owner is None and w.timeline.editing_id == "t3"
    assert w.timeline.bubble("t3") is bubble
    assert bubble.original_label.toPlainText() == "draft stays here"
    bubble.cancel_edit_button.click()
    pump(rig.qt)
    assert w.timeline.order() == ["t0"]


def test_changing_the_filter_mid_clip_keeps_playing_and_shows_the_clip(rig):
    w = rig.window
    rig.play("t3")
    pump(rig.qt)
    w.filter_review.click()                               # nothing needs review
    pump(rig.qt)
    assert rig.controller.owner == "t3", "changing the filter reset playback"
    assert w.timeline.order() == ["t3"]
    w.filter_all.click()
    pump(rig.qt)
    assert rig.controller.owner == "t3"
    assert w.timeline.bubble("t3").property("nowPlaying") is True


def test_choosing_another_message_while_playing_only_targets_it(rig):
    w = rig.window
    rig.play("t0")
    pump(rig.qt)
    w.timeline.select("old05")
    pump(rig.qt)
    assert w.waveform.current_tx_id == "t0", "the waveform left the clip being heard"
    assert rig.controller.owner == "t0"
    assert w.selected_play_button.text() == "Play selected"
    w.playback_stop_button.click()
    pump(rig.qt)
    assert w.waveform.current_tx_id == "old05", "after Stop it shows the selection again"
    assert w.timeline.now_playing is None


def test_stop_makes_a_pending_scroll_stale(rig):
    w = rig.window
    rig.play("t0")
    pump(rig.qt, 10)
    scroll_to_bottom(rig)
    before = w.timeline.verticalScrollBar().value()
    rig.backend.advance(10_000)                          # t0 ends ...
    rig.qt.processEvents()                               # ... t3 starts, its scroll is queued
    w.playback_stop_button.click()                       # Stop before it runs
    pump(rig.qt, 20)
    assert rig.controller.owner is None and not w.sequence.active
    assert w.timeline.verticalScrollBar().value() == before, \
        "a scroll queued before Stop moved the view afterwards"


def test_a_session_change_ends_following(rig):
    w = rig.window
    rig.play("t0")
    pump(rig.qt)
    tabs = w.session_tabs
    tabs.setCurrentIndex(next(i for i in range(tabs.count())
                              if tabs.tabData(i) == rig.b.id))
    pump(rig.qt, 20)
    rig.finish()
    assert rig.controller.owner is None
    assert w.waveform.current_tx_id != "t0"
    assert w.timeline.now_playing is None
    assert "t0" not in w.timeline.order()
