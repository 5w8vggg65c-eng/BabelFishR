"""The shared recording transport, tested with short and long WAVs and a
scripted audio backend. Native QtMultimedia checks run when available."""

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
    from PySide6 import QtCore, QtGui

    assert button.isVisibleTo(button.window()), f"{button.text()!r} is hidden"
    assert button.isEnabled(), f"{button.text()!r} is disabled"
    center = button.rect().center()
    point = QtCore.QPointF(center)
    screen = QtCore.QPointF(button.mapToGlobal(center))
    left = QtCore.Qt.LeftButton
    for event_type, held in ((QtCore.QEvent.MouseButtonPress, left),
                             (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.NoButton)):
        event = QtGui.QMouseEvent(event_type, point, screen, left, held, QtCore.Qt.NoModifier)
        qt_app.sendEvent(button, event)
    pump(qt_app)


def menu_play(qt_app, bubble):
    """Invoke the original-recording shortcut in the message menu."""
    assert bubble.play_action.isEnabled()
    bubble.play_action.trigger()
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


@pytest.fixture
def console(qt_app, config, store):
    """The real window, one saved session and a deterministic audio backend."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.models import Session
    from babelfishr.ui.main_window import MainWindow
    from babelfishr.ui.playback import PlaybackController

    app = BabelFishRApp(config=config, store=store)
    store.save_session(Session(id="s", conversation_id=store.default_conversation().id))
    window = MainWindow(app)
    window.show()

    def open_recordings(*records, controllable=True):
        backend = make_scripted_backend(controllable=controllable)
        for record in records:
            store.save_transmission(record)
            if record.audio_path:
                backend.durations[record.audio_path] = round(record.duration * 1000)
        controller = PlaybackController(backend, parent=window)
        window.timeline.set_playback(controller)
        window._reload_timeline()
        pump(qt_app)
        return window, controller, backend

    yield open_recordings
    window.close()
    app.close()


@pytest.mark.parametrize("kind,seconds", [("short", 2.0), ("exact", 5.0), ("long", 9.0)])
def test_all_lengths_get_the_same_fixed_pause_and_stop(qt_app, console, wavs, kind, seconds):
    window, controller, backend = console(tx("a", seconds, wavs[kind]))
    bar = window.playback_controls
    bubble = window.timeline.bubble("a")
    assert not bar.isVisible() and window.selected_panel.isAncestorOf(bar)
    assert not bubble.isAncestorOf(bar)

    window.timeline.select("a")
    pump(qt_app)
    assert bar.isVisible() and window.selected_play_button.isEnabled()
    assert backend.calls == []
    click(qt_app, window.selected_play_button)
    assert controller.owner == "a" and backend.loaded == wavs[kind]
    assert window.playback_pause_button.isEnabled() and window.playback_stop_button.isEnabled()
    backend.advance(400)
    click(qt_app, window.playback_pause_button)
    assert controller.state_for("a") == "paused" and backend.position() == 400
    assert window.playback_pause_button.text() == "Resume"
    click(qt_app, window.playback_stop_button)
    assert controller.owner is None and ("stop",) in backend.calls
    assert window.selected_play_button.isEnabled() and bar.isVisible()


def test_pause_preserves_position_and_play_resumes_the_same_recording(qt_app, console, wavs):
    window, controller, backend = console(tx("long", 9.0, wavs["long"]))
    window.timeline.select("long")
    click(qt_app, window.selected_play_button)
    backend.advance(3000)
    assert "0:03 / 0:09" in window.playback_position_label.text()
    click(qt_app, window.playback_pause_button)
    assert controller.state_for("long") == "paused" and backend.position() == 3000
    mark = len(backend.calls)
    click(qt_app, window.playback_pause_button)
    assert backend.calls[mark:] == [("play",)]
    assert backend.state() == "playing" and backend.position() == 3000
    assert window.playback_pause_button.text() == "Pause"


def test_rewind_and_fast_forward_skip_five_seconds_within_bounds(qt_app, console, wavs):
    window, controller, backend = console(tx("long", 9.0, wavs["long"]))
    window.timeline.select("long")
    click(qt_app, window.selected_play_button)
    backend.advance(2000)
    click(qt_app, window.playback_forward_button)
    assert backend.position() == 7000
    # The controller clamps a skip to this file. The sequence's separate
    # boundary action may advance to a later recording when one exists.
    controller.forward()
    assert backend.position() == 9000
    click(qt_app, window.playback_rewind_button)
    assert backend.position() == 4000
    click(qt_app, window.playback_rewind_button)
    assert backend.position() == 0 and "0:00 / 0:09" in window.playback_position_label.text()


def test_natural_completion_leaves_the_selected_recording_playable(qt_app, console, wavs):
    window, controller, backend = console(tx("short", 2.0, wavs["short"]))
    window.timeline.select("short")
    click(qt_app, window.selected_play_button)
    backend.advance(2500)
    pump(qt_app)
    assert controller.owner is None and window.timeline.selected_id == "short"
    assert not window.playback_pause_button.isEnabled() and window.playback_controls.isVisible()
    click(qt_app, window.selected_play_button)
    assert controller.owner == "short" and backend.position() == 0


def test_starting_another_recording_retires_only_the_previous_owner(qt_app, console, wavs):
    window, controller, backend = console(tx("a", 9.0, wavs["long"]), tx("b", 2.0, wavs["short"]))
    first, second = window.timeline.bubble("a"), window.timeline.bubble("b")
    window.timeline.select("a")
    click(qt_app, window.selected_play_button)
    backend.advance(2000)
    assert first.play_action.text() == "Pause" and second.play_action.text() == "Play original recording"
    bar = window.playback_controls
    window.timeline.select("b")
    click(qt_app, window.selected_play_button)
    assert controller.owner == "b" and backend.loaded == wavs["short"] and backend.position() == 0
    assert first.play_action.text() == "Play original recording" and second.play_action.text() == "Pause"
    assert window.playback_controls is bar and bar.isVisible()


def test_a_transcript_update_keeps_the_fixed_bar_and_playback_position(qt_app, console, wavs):
    window, controller, backend = console(tx("long", 9.0, wavs["long"]))
    window.timeline.select("long")
    click(qt_app, window.selected_play_button)
    backend.advance(4000)
    bar = window.playback_controls
    bubble = window.timeline.bubble("long")
    before = list(backend.calls)
    window.timeline.update(tx("long", 9.0, wavs["long"], transcript="corrected words",
                              translation="translated"))
    pump(qt_app)
    assert backend.calls == before and controller.position_for("long") == (4000, 9000)
    assert window.playback_controls is bar and bar.isVisible()
    assert "corrected words" in bubble.original_label.text()


def test_switching_threads_stops_playback(qt_app, wavs):
    backend = make_scripted_backend()
    view, controller = make_view(backend)
    bubble = view.add(tx("long", 9.0, wavs["long"]))
    assert controller.play(bubble.tx.id, bubble.tx.audio_path)
    view.set_transmissions([tx("elsewhere", 1.0, wavs["short"])])
    pump(qt_app)
    assert controller.owner is None and ("stop",) in backend.calls
    view.close()


def test_a_missing_recording_disables_play_until_its_original_returns(qt_app, console, tmp_path):
    path = tmp_path / "missing.wav"
    window, controller, backend = console(tx("gone", 9.0, str(path)))
    window.timeline.select("gone")
    bubble = window.timeline.bubble("gone")
    assert not window.playback_controls.isVisible() and not bubble.play_action.isEnabled()
    assert bubble.pairing_label.text() == "Audio missing"
    window._play_selected()
    assert backend.loaded is None and controller.owner is None
    path.write_bytes(b"RIFF synthetic")
    window.timeline.select(None)
    window.timeline.select("gone")
    assert window.playback_controls.isVisible() and window.selected_play_button.isEnabled()


def test_a_backend_error_is_shown_and_retry_clears_it(qt_app, console, wavs):
    window, controller, backend = console(tx("long", 9.0, wavs["long"]))
    window.timeline.select("long")
    click(qt_app, window.selected_play_button)
    backend.advance(1000)
    backend.fail("decoder gave up")
    pump(qt_app)
    assert controller.owner is None
    assert "decoder gave up" in window.timeline.bubble("long").status_label.text()
    click(qt_app, window.selected_play_button)
    assert controller.owner == "long" and "long" not in controller.last_error


def test_external_playback_has_no_in_app_pause_stop_or_seek(qt_app, console, wavs):
    window, controller, backend = console(tx("long", 9.0, wavs["long"]), controllable=False)
    window.timeline.select("long")
    click(qt_app, window.selected_play_button)
    assert ("play",) in backend.calls and not controller.controllable
    assert not window.playback_pause_button.isEnabled()
    assert not window.playback_stop_button.isEnabled()
    assert not window.playback_seek.isEnabled()
    assert not window.playback_rewind_button.isEnabled() and not window.playback_forward_button.isEnabled()


def test_the_system_backend_states_its_limitation(monkeypatch):
    from babelfishr.ui import playback

    launched = []
    monkeypatch.setattr(playback.subprocess, "Popen", lambda args, **kw: launched.append(args))
    controller = playback.PlaybackController(playback.SystemOpenBackend())
    assert not controller.controllable
    assert controller.play("x", __file__) and launched[0][-1] == __file__
    assert controller.owner is None


def test_a_message_without_a_recording_offers_no_play(qt_app, console):
    window, controller, backend = console(tx("silent", 9.0, None))
    window.timeline.select("silent")
    assert not window.playback_controls.isVisible() and not window.selected_play_button.isEnabled()
    assert not window.timeline.bubble("silent").play_action.isEnabled()
    assert controller.owner is None and backend.calls == []


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
