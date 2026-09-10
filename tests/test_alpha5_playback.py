"""Per-bubble playback: a compact Play, and a control bar for longer recordings.

Two kinds of test, kept apart. Most drive the real bubble and view with real
mouse clicks against a *scripted* backend - the playback seam the controller
talks to - so pause, stop, seek, completion and errors are deterministic and
the controller's ownership logic is what is under test. A small final group
touches QtMultimedia itself and skips, with its reason, where that module is
not installed (this development environment has PySide6 Essentials only; the
packaged Mac app has the full module). Neither kind is a physical Mac
playing sound through a speaker.
"""

from __future__ import annotations

import datetime as dt
import os
import time

import pytest

from babelfishr.models import ProcessingState, Transmission
from babelfishr.testing import build_fixture

SR = 48_000


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds: int = 10) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.003)


def click(qt_app, button) -> None:
    from PySide6 import QtCore
    from PySide6.QtTest import QTest

    assert button.isVisibleTo(button.window()), f"{button.text()!r} is hidden"
    assert button.isEnabled(), f"{button.text()!r} is disabled"
    QTest.mouseClick(button, QtCore.Qt.LeftButton)
    pump(qt_app)


# ---- a scripted backend ----------------------------------------------------


def make_scripted_backend(controllable: bool = True):
    from babelfishr.ui.playback import (PAUSED, PLAYING, STOPPED,
                                        PlaybackBackend)

    class Scripted(PlaybackBackend):
        """Does exactly what it is told and reports it like a player would."""

        name = "scripted"

        def __init__(self):
            super().__init__()
            self.loaded = None
            self.calls = []
            self._state = STOPPED
            self._position = 0
            self._duration = 0
            self.durations = {}

        # -- the interface --
        def load(self, path):
            self.calls.append(("load", path))
            self.loaded = path
            self._position = 0
            self._duration = self.durations.get(path, 8000)
            self.durationChanged.emit(self._duration)

        def play(self):
            self.calls.append(("play",))
            self._set(PLAYING)

        def pause(self):
            self.calls.append(("pause",))
            if self._state == PLAYING:
                self._set(PAUSED)

        def stop(self):
            self.calls.append(("stop",))
            self._position = 0
            self._set(STOPPED)

        def seek(self, ms):
            self.calls.append(("seek", ms))
            self._position = max(0, min(int(ms), self._duration))
            self.positionChanged.emit(self._position)

        def position(self):
            return self._position

        def duration(self):
            return self._duration

        def state(self):
            return self._state

        # -- the script --
        def advance(self, ms):
            """Time passes while playing."""
            if self._state == PLAYING:
                self._position = min(self._position + ms, self._duration)
                self.positionChanged.emit(self._position)
                if self._position >= self._duration:
                    self._set(STOPPED)
                    self.finished.emit()

        def fail(self, message):
            self.errorOccurred.emit(message)
            self._set(STOPPED)

        def _set(self, state):
            if state != self._state:
                self._state = state
                self.stateChanged.emit(state)

    Scripted.controllable = controllable
    return Scripted()


def make_view(backend):
    from babelfishr.ui.playback import PlaybackController
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    # Swap the view's controller for one on the scripted backend, and rewire
    # the anchoring slot, so bubbles created from here on use it.
    controller = PlaybackController(backend, parent=view)
    view.set_playback(controller)      # the view rewires changed and positionChanged itself
    return view, controller


def tx(tx_id: str, seconds: float, path: str, **overrides) -> Transmission:
    fields = dict(id=tx_id, session_id="s", duration=seconds, audio_path=path,
                  started_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                  transcript=f"words for {tx_id}", state=ProcessingState.COMPLETE,
                  target_language="en", source_language="en")
    fields.update(overrides)
    return Transmission(**fields)


@pytest.fixture
def wavs(tmp_path):
    short = build_fixture([{"kind": "voice", "duration": 2.0, "level_dbfs": -14}],
                          sample_rate=SR).write(str(tmp_path / "short.wav"))
    long_ = build_fixture([{"kind": "voice", "duration": 9.0, "level_dbfs": -14}],
                          sample_rate=SR).write(str(tmp_path / "long.wav"))
    exact = build_fixture([{"kind": "voice", "duration": 5.0, "level_dbfs": -14}],
                          sample_rate=SR).write(str(tmp_path / "exact.wav"))
    return {"short": short, "long": long_, "exact": exact}


# ---- short recordings ------------------------------------------------------


def test_a_short_recording_plays_through_without_expanding(qt_app, wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["short"]] = 2000
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("short", 2.0, wavs["short"]))
    pump(qt_app)

    assert bubble.play_button.isVisibleTo(view) and bubble.playback_bar.isHidden()
    click(qt_app, bubble.play_button)
    assert backend.loaded == wavs["short"]
    assert controller.owner == "short"
    assert bubble.playback_bar.isHidden(), "a short recording expanded controls"
    assert not bubble.play_button.isEnabled()
    assert "Playing" in bubble.play_button.text()

    backend.advance(2500)                       # it finishes
    pump(qt_app)
    assert controller.owner is None
    assert bubble.play_button.isEnabled() and "Play" in bubble.play_button.text()
    assert bubble.playback_bar.isHidden()
    # And it can be played again.
    click(qt_app, bubble.play_button)
    assert controller.owner == "short"
    view.close()


def test_exactly_five_seconds_is_treated_as_short(qt_app, wavs):
    """The recommended boundary, pending the operator's confirmation."""
    backend = make_scripted_backend()
    backend.durations[wavs["exact"]] = 5000
    view, controller = make_view(backend)
    bubble = view.add(tx("exact", 5.0, wavs["exact"]))
    assert not bubble.is_long_recording
    click(qt_app, bubble.play_button)
    assert bubble.playback_bar.isHidden()
    view.close()


# ---- long recordings -------------------------------------------------------


def test_a_long_recording_expands_controls_and_stop_collapses_them(qt_app, wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    pump(qt_app)
    assert bubble.is_long_recording

    click(qt_app, bubble.play_button)
    assert bubble.playback_bar.isVisibleTo(view), "the control bar did not expand"
    assert bubble.play_button.isHidden(), "compact Play still shown beside the bar"
    labels = [b.text() for b in (bubble.rewind_button, bubble.pause_button,
                                 bubble.stop_button, bubble.forward_button)]
    assert any("Pause" in t for t in labels)
    assert any("Stop" in t for t in labels)
    assert any("5 s" in t for t in labels[:1]) and any("5 s" in t for t in labels[3:])
    # The bar lives at the bottom of *this* bubble.
    assert bubble.playback_bar.parent() is bubble
    assert bubble.playback_bar.y() > bubble.header.y()

    click(qt_app, bubble.stop_button)
    assert ("stop",) in backend.calls
    assert controller.owner is None
    assert bubble.playback_bar.isHidden(), "Stop did not collapse the bar"
    assert bubble.play_button.isVisibleTo(view) and bubble.play_button.isEnabled()
    view.close()


def test_pause_preserves_position_and_play_resumes_the_same_recording(qt_app,
                                                                       wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    pump(qt_app)
    click(qt_app, bubble.play_button)
    backend.advance(3000)
    pump(qt_app)
    assert "0:03 / 0:09" in bubble.position_label.text()

    click(qt_app, bubble.pause_button)
    assert backend.state() == "paused"
    assert backend.position() == 3000, "pausing lost the position"
    assert "Play" in bubble.pause_button.text()
    assert bubble.playback_bar.isVisibleTo(view), "pausing collapsed the bar"

    loads_before = [c for c in backend.calls if c[0] == "load"]
    click(qt_app, bubble.pause_button)            # resume
    assert backend.state() == "playing"
    assert backend.position() == 3000, "resume restarted from the beginning"
    assert [c for c in backend.calls if c[0] == "load"] == loads_before, (
        "resuming reloaded the file instead of continuing")
    assert "Pause" in bubble.pause_button.text()
    view.close()


def test_rewind_and_fast_forward_skip_five_seconds_within_bounds(qt_app, wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    pump(qt_app)
    click(qt_app, bubble.play_button)
    backend.advance(2000)
    click(qt_app, bubble.forward_button)
    assert backend.position() == 7000
    click(qt_app, bubble.forward_button)
    assert backend.position() == 9000, "fast-forward ran past the end"
    click(qt_app, bubble.rewind_button)
    assert backend.position() == 4000
    click(qt_app, bubble.rewind_button)
    assert backend.position() == 0, "rewind went before the start"
    assert "0:00 / 0:09" in bubble.position_label.text()
    view.close()


def test_natural_completion_collapses_the_bar_and_restores_play(qt_app, wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    pump(qt_app)
    click(qt_app, bubble.play_button)
    assert bubble.playback_bar.isVisibleTo(view)
    backend.advance(9500)
    pump(qt_app)
    assert controller.owner is None
    assert bubble.playback_bar.isHidden()
    assert bubble.play_button.isVisibleTo(view) and bubble.play_button.isEnabled()
    view.close()


# ---- ownership -------------------------------------------------------------


def test_an_unrelated_bubble_is_not_affected_by_another_recording(qt_app, wavs):
    """The defect the shared player invited: bubble B must not claim Pause
    because bubble A is playing."""
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    a = view.add(tx("a", 9.0, wavs["long"]))
    b = view.add(tx("b", 9.0, wavs["long"], transcript="other words"))
    pump(qt_app)
    click(qt_app, a.play_button)

    assert a.playback_bar.isVisibleTo(view)
    assert b.playback_bar.isHidden(), "B expanded for A's recording"
    assert b.play_button.isVisibleTo(view) and b.play_button.isEnabled()
    assert "Play" in b.play_button.text() and "Playing" not in b.play_button.text()
    assert b.play_action.text() == "Play original recording"
    assert a.play_action.text() == "Pause"
    view.close()


def test_starting_another_recording_retires_the_previous_bubble(qt_app, wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    a = view.add(tx("a", 9.0, wavs["long"]))
    b = view.add(tx("b", 9.0, wavs["long"], transcript="other words"))
    pump(qt_app)
    click(qt_app, a.play_button)
    backend.advance(2000)
    click(qt_app, b.play_button)

    assert controller.owner == "b"
    assert a.playback_bar.isHidden(), "A kept its controls after B took over"
    assert a.play_button.isVisibleTo(view) and a.play_button.isEnabled()
    assert b.playback_bar.isVisibleTo(view)
    assert backend.loaded == wavs["long"] and backend.position() == 0
    view.close()


def test_a_transcript_update_does_not_reset_or_rebuild_playback(qt_app, wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    pump(qt_app)
    click(qt_app, bubble.play_button)
    backend.advance(4000)
    bar_before = bubble.playback_bar
    calls_before = list(backend.calls)

    view.update(tx("long", 9.0, wavs["long"],
                   transcript="corrected words", translation="translated"))
    pump(qt_app)
    assert backend.calls == calls_before, "an update touched the player"
    assert backend.position() == 4000 and backend.state() == "playing"
    assert bubble.playback_bar is bar_before, "the bar was rebuilt"
    assert bubble.playback_bar.isVisibleTo(view)
    assert "corrected words" in bubble.original_label.text()
    view.close()


def test_switching_threads_stops_playback(qt_app, wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    click(qt_app, bubble.play_button)
    assert controller.owner == "long"
    view.set_transmissions([tx("elsewhere", 1.0, wavs["short"])])
    pump(qt_app)
    assert controller.owner is None
    assert ("stop",) in backend.calls
    view.close()


# ---- errors and honesty ----------------------------------------------------


def test_a_missing_recording_is_reported_and_leaves_play_usable(qt_app, tmp_path):
    backend = make_scripted_backend()
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("gone", 9.0, str(tmp_path / "does-not-exist.wav")))
    pump(qt_app)
    click(qt_app, bubble.play_button)
    assert backend.loaded is None, "a missing file reached the backend"
    assert controller.owner is None
    assert bubble.playback_bar.isHidden()
    assert bubble.play_button.isEnabled()
    assert "Could not play" in bubble.status_label.text()
    assert "missing" in bubble.status_label.text()


def test_a_backend_error_mid_playback_leaves_an_honest_state(qt_app, wavs):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    pump(qt_app)
    click(qt_app, bubble.play_button)
    backend.advance(1000)
    backend.fail("decoder gave up")
    pump(qt_app)
    assert controller.owner is None
    assert bubble.playback_bar.isHidden()
    assert bubble.play_button.isEnabled() and "Play" in bubble.play_button.text()
    assert "decoder gave up" in bubble.status_label.text()
    # It can be tried again, and the error note clears on a fresh start.
    click(qt_app, bubble.play_button)
    assert controller.owner == "long"
    assert "long" not in controller.last_error
    view.close()


def test_an_uncontrollable_backend_never_shows_pause_stop_or_seek(qt_app, wavs):
    """When only the system player is available, in-app controls would
    govern nothing. The bubble must not offer them."""
    backend = make_scripted_backend(controllable=False)
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    pump(qt_app)
    assert not controller.controllable
    click(qt_app, bubble.play_button)
    assert bubble.playback_bar.isHidden(), "controls offered for an external player"
    assert ("play",) in backend.calls
    view.close()


def test_the_system_backend_states_its_limitation(monkeypatch):
    """The real fallback: launches the OS player, cannot control it, says so."""
    from babelfishr.ui.playback import (PlaybackController, SystemOpenBackend)

    launched = []
    import babelfishr.ui.playback as playback

    monkeypatch.setattr(playback.subprocess, "Popen",
                        lambda args, **kw: launched.append(args))
    backend = SystemOpenBackend()
    assert backend.controllable is False
    controller = PlaybackController(backend)
    assert controller.play("x", __file__)      # any existing file
    assert launched and launched[0][-1] == __file__
    # Fire and forget: the controller is idle again immediately, so no bubble
    # can show a Pause for a player this process does not own.
    assert controller.owner is None


def test_a_bubble_without_a_recording_offers_no_play(qt_app):
    backend = make_scripted_backend()
    view, controller = make_view(backend)
    bubble = view.add(tx("silent", 9.0, None))
    assert bubble.play_button.isHidden()
    assert bubble.playback_bar.isHidden()
    assert not bubble.play_action.isEnabled()
    view.close()


# ---- the reading position ------------------------------------------------------


def test_expanding_and_collapsing_the_bar_keeps_the_reading_position(qt_app,
                                                                     wavs):
    """The operator is reading an older bubble far down the thread. A bar
    expanding on a bubble above it must not move what they are reading."""
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.resize(420, 260)
    view.show()
    pump(qt_app)
    ids = [f"t{i}" for i in range(14)]
    for i, tx_id in enumerate(ids):
        view.append_older(tx(tx_id, 9.0, wavs["long"],
                             transcript=f"line {i} " * 12))
    pump(qt_app, 20)
    reading = view._bubbles[ids[10]]
    bar = view.verticalScrollBar()
    bar.setValue(reading.y() - 8)
    pump(qt_app, 10)
    before = reading.y() - bar.value()
    assert not view.at_top(), "the test needs a scrolled-down reading position"

    click(qt_app, view._bubbles[ids[2]].play_button)     # expands above
    pump(qt_app, 20)
    assert view._bubbles[ids[2]].playback_bar.isVisibleTo(view)
    assert abs((reading.y() - bar.value()) - before) <= 1, (
        "expanding a bar above the reader moved what they were reading")

    click(qt_app, view._bubbles[ids[2]].stop_button)        # collapses above
    pump(qt_app, 20)
    assert abs((reading.y() - bar.value()) - before) <= 1, (
        "collapsing a bar above the reader moved what they were reading")
    view.close()


# ---- QtMultimedia itself, where it exists -------------------------------------


def test_the_qt_backend_reports_a_missing_file_as_an_error(qt_app, tmp_path):
    pytest.importorskip("PySide6.QtMultimedia",
                        reason="QtMultimedia is not installed here; the "
                               "packaged app has it")
    from babelfishr.ui.playback import PlaybackController, QtMultimediaBackend

    backend = QtMultimediaBackend()
    controller = PlaybackController(backend)
    assert controller.play("x", str(tmp_path / "missing.wav")) is False
    assert "missing" in controller.last_error["x"]
    assert controller.owner is None


def test_the_qt_backend_learns_a_real_wav_duration(qt_app, wavs):
    """Loads a 9-second WAV and reads its duration from QMediaPlayer's own
    signals. Skips if this host has no decoder for it. Not a speaker test."""
    QtMultimedia = pytest.importorskip(
        "PySide6.QtMultimedia",
        reason="QtMultimedia is not installed here; the packaged app has it")
    from babelfishr.ui.playback import QtMultimediaBackend

    backend = QtMultimediaBackend()
    seen = {"duration": 0, "error": ""}
    backend.durationChanged.connect(lambda ms: seen.__setitem__("duration", ms))
    backend.errorOccurred.connect(lambda m: seen.__setitem__("error", m))
    backend.load(wavs["long"])
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not seen["duration"] and not seen["error"]:
        pump(qt_app)
    if seen["error"]:
        pytest.skip(f"this host cannot decode the WAV: {seen['error']}")
    assert 8500 <= seen["duration"] <= 9500, seen
