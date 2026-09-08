"""A failed playback request for one recording leaves another's playback
exactly as the backend has it.

Two real bubbles, real Play/Pause/Stop clicks, the scripted playback backend
from test_alpha5_playback (it records every call and reports state like a
player). No sound device, no QtMultimedia: a scripted signal order shows the
controller's behaviour, not what any particular QtMultimedia backend emits.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil

import pytest

from babelfishr.ui.playback import PAUSED, PLAYING, STOPPED

_spec = importlib.util.spec_from_file_location(
    "playback_helpers", pathlib.Path(__file__).with_name("test_alpha5_playback.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
make_scripted_backend, make_view, click, pump, tx = (
    _helpers.make_scripted_backend, _helpers.make_view, _helpers.click,
    _helpers.pump, _helpers.tx)
qt_app = _helpers.qt_app
wavs = _helpers.wavs


def two_bubbles(qt_app, wavs, tmp_path, a_seconds=9.0, a_path=None):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    backend.durations[wavs["short"]] = 2000
    view, controller = make_view(backend)
    view.show()
    a = view.add(tx("a", a_seconds, a_path or wavs["long"]))
    b = view.add(tx("b", 4.0, str(tmp_path / "missing-b.wav")))
    pump(qt_app)
    return backend, view, controller, a, b


def stops_since(backend, mark: int) -> int:
    return sum(1 for c in backend.calls[mark:] if c == ("stop",))


def a_is_untouched(backend, controller, a, *, state, position, bar_visible):
    assert controller.owner == "a"
    assert controller.state_for("a") == state
    assert backend.state() == state
    assert controller.position_for("a") == (position, 9000)
    assert backend.position() == position
    assert a.playback_bar.isVisible() is bar_visible


def b_reports_its_error(b, controller):
    assert "missing" in controller.last_error["b"]
    assert "Could not play" in b.status_label.text() and "missing" in b.status_label.text()
    assert b.play_button.isEnabled() and b.playback_bar.isHidden()


def test_bs_missing_file_leaves_a_playing_a_long_recording(qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    click(qt_app, a.play_button)
    backend.advance(700)
    pump(qt_app)
    a_is_untouched(backend, controller, a, state=PLAYING, position=700, bar_visible=True)
    assert a.pause_button.text().startswith("⏸")
    mark = len(backend.calls)

    click(qt_app, b.play_button)
    a_is_untouched(backend, controller, a, state=PLAYING, position=700, bar_visible=True)
    assert a.play_button.isHidden()
    assert a.pause_button.text().startswith("⏸")
    assert stops_since(backend, mark) == 0, "the backend was stopped for B's failure"
    b_reports_its_error(b, controller)
    # A is still in charge: its own Stop still works, exactly once.
    click(qt_app, a.stop_button)
    assert controller.owner is None and backend.state() == STOPPED
    assert stops_since(backend, mark) == 1


def test_bs_missing_file_leaves_a_playing_a_short_recording(qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path,
                                                  a_seconds=2.0, a_path=wavs["short"])
    click(qt_app, a.play_button)
    backend.advance(500)
    pump(qt_app)
    assert controller.state_for("a") == PLAYING and a.playback_bar.isHidden()
    assert not a.play_button.isEnabled() and "Playing" in a.play_button.text()
    mark = len(backend.calls)

    click(qt_app, b.play_button)
    assert controller.owner == "a" and controller.state_for("a") == PLAYING
    assert backend.state() == PLAYING and controller.position_for("a") == (500, 2000)
    assert not a.play_button.isEnabled() and "Playing" in a.play_button.text()
    assert stops_since(backend, mark) == 0
    b_reports_its_error(b, controller)
    backend.advance(1500)                                  # plays through to its end
    pump(qt_app)
    assert controller.owner is None and a.play_button.isEnabled()


def test_bs_missing_file_leaves_a_paused(qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    click(qt_app, a.play_button)
    backend.advance(700)
    pump(qt_app)
    click(qt_app, a.pause_button)
    a_is_untouched(backend, controller, a, state=PAUSED, position=700, bar_visible=True)
    assert a.pause_button.text().startswith("▶")
    mark = len(backend.calls)

    click(qt_app, b.play_button)
    a_is_untouched(backend, controller, a, state=PAUSED, position=700, bar_visible=True)
    assert a.pause_button.text().startswith("▶")
    assert stops_since(backend, mark) == 0
    b_reports_its_error(b, controller)
    click(qt_app, a.pause_button)                          # resumes from 700 ms
    assert controller.state_for("a") == PLAYING and backend.position() == 700


def test_the_owner_failing_itself_natural_end_stop_and_takeover_still_behave(
        qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    c = view.add(tx("c", 9.0, wavs["long"]))
    pump(qt_app)
    # The owner itself fails (its file is missing now).
    click(qt_app, a.play_button)
    pathlib.Path(wavs["long"]).rename(tmp_path / "moved.wav")
    try:
        mark = len(backend.calls)
        controller.play("a", a.tx.audio_path)              # a fresh request: file gone
        assert controller.owner is None and backend.state() == STOPPED
        assert stops_since(backend, mark) == 1 and "missing" in controller.last_error["a"]
        assert a.playback_bar.isHidden() and a.play_button.isEnabled()
    finally:
        (tmp_path / "moved.wav").rename(wavs["long"])
    # Natural end.
    click(qt_app, a.play_button)
    backend.advance(9000)
    pump(qt_app)
    assert controller.owner is None and a.play_button.isEnabled() and a.playback_bar.isHidden()
    # Stop pressed.
    click(qt_app, a.play_button)
    mark = len(backend.calls)
    click(qt_app, a.stop_button)
    assert controller.owner is None and stops_since(backend, mark) == 1
    # A valid C takes over from A.
    click(qt_app, a.play_button)
    mark = len(backend.calls)
    click(qt_app, c.play_button)
    assert controller.owner == "c" and backend.loaded == wavs["long"]
    assert stops_since(backend, mark) == 1
    assert a.playback_bar.isHidden() and a.play_button.isEnabled()
    assert c.playback_bar.isVisible()


def test_b_can_be_retried_once_its_recording_exists(qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    click(qt_app, a.play_button)
    click(qt_app, b.play_button)
    b_reports_its_error(b, controller)
    assert controller.owner == "a"
    shutil.copy(wavs["short"], b.tx.audio_path)            # the file appears
    backend.durations[b.tx.audio_path] = 2000
    mark = len(backend.calls)
    click(qt_app, b.play_button)
    assert controller.owner == "b" and controller.state_for("b") == PLAYING
    assert backend.loaded == b.tx.audio_path and stops_since(backend, mark) == 1
    assert "b" not in controller.last_error
    assert "Could not play" not in b.status_label.text()
    assert a.playback_bar.isHidden() and a.play_button.isEnabled()


def test_a_backend_that_rejects_the_file_on_load_gives_no_false_start(
        qt_app, wavs, tmp_path):
    """Scripted: the backend emits its error inside load(), synchronously.
    Whether a given QtMultimedia build does that is not established here."""
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    shutil.copy(wavs["short"], b.tx.audio_path)            # exists, but undecodable
    real_load = backend.load

    def load(path):
        if path == b.tx.audio_path:
            backend.calls.append(("load", path))
            backend.errorOccurred.emit("the recording could not be decoded")
            backend._set(STOPPED)
            return
        real_load(path)

    backend.load = load
    click(qt_app, a.play_button)
    mark = len(backend.calls)
    click(qt_app, b.play_button)
    after = backend.calls[mark:]
    assert ("play",) not in after, f"play() was pressed after a failed load: {after}"
    assert controller.owner is None and controller.state_for("b") == STOPPED
    assert backend.state() == STOPPED
    assert "decoded" in controller.last_error["b"]
    assert "Could not play" in b.status_label.text()
    assert b.play_button.isEnabled() and b.playback_bar.isHidden()
    assert a.playback_bar.isHidden() and a.play_button.isEnabled()   # A was stopped by the takeover
    assert controller.play("b", b.tx.audio_path) is False
