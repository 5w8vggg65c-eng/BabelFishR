"""A failed playback request for one recording leaves another's playback
exactly as the backend has it.

Two real bubbles, their original-recording menu shortcuts, and the scripted backend
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
make_scripted_backend, make_view, menu_play, pump, tx = (
    _helpers.make_scripted_backend, _helpers.make_view, _helpers.menu_play,
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


def a_is_untouched(backend, controller, a, *, state, position):
    assert controller.owner == "a"
    assert controller.state_for("a") == state
    assert backend.state() == state
    assert controller.position_for("a") == (position, 9000)
    assert backend.position() == position
    expected = "Pause" if state == PLAYING else "Play original recording"
    assert a.play_action.isEnabled() and a.play_action.text() == expected


def attempt_missing_b(qt_app, b, controller):
    """B's original is missing: its Play is disabled and its card says so
    (the operator cannot start it); a request that reaches the controller
    anyway (a stale route) is still refused without touching A."""
    assert not b.play_action.isEnabled()
    assert b.pairing_label.text() == "Audio missing"
    assert not controller.play("b", b.tx.audio_path)
    pump(qt_app)


def b_reports_its_error(b, controller):
    assert "missing" in controller.last_error["b"]
    assert "Could not play" in b.status_label.text() and "missing" in b.status_label.text()
    assert not b.play_action.isEnabled()


def test_bs_missing_file_leaves_a_playing_a_long_recording(qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    menu_play(qt_app, a)
    backend.advance(700)
    pump(qt_app)
    a_is_untouched(backend, controller, a, state=PLAYING, position=700)
    assert a.play_action.text() == "Pause"
    mark = len(backend.calls)

    attempt_missing_b(qt_app, b, controller)
    a_is_untouched(backend, controller, a, state=PLAYING, position=700)
    assert a.play_action.text() == "Pause"
    assert stops_since(backend, mark) == 0, "the backend was stopped for B's failure"
    b_reports_its_error(b, controller)
    # A is still in charge: its own Stop still works, exactly once.
    controller.stop()
    assert controller.owner is None and backend.state() == STOPPED
    assert stops_since(backend, mark) == 1


def test_bs_missing_file_leaves_a_playing_a_short_recording(qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path,
                                                  a_seconds=2.0, a_path=wavs["short"])
    menu_play(qt_app, a)
    backend.advance(500)
    pump(qt_app)
    assert controller.state_for("a") == PLAYING
    assert a.play_action.isEnabled() and a.play_action.text() == "Pause"
    mark = len(backend.calls)

    attempt_missing_b(qt_app, b, controller)
    assert controller.owner == "a" and controller.state_for("a") == PLAYING
    assert backend.state() == PLAYING and controller.position_for("a") == (500, 2000)
    assert a.play_action.isEnabled() and a.play_action.text() == "Pause"
    assert stops_since(backend, mark) == 0
    b_reports_its_error(b, controller)
    backend.advance(1500)                                  # plays through to its end
    pump(qt_app)
    assert controller.owner is None and a.play_action.isEnabled()


def test_bs_missing_file_leaves_a_paused(qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    menu_play(qt_app, a)
    backend.advance(700)
    pump(qt_app)
    menu_play(qt_app, a)
    a_is_untouched(backend, controller, a, state=PAUSED, position=700)
    assert a.play_action.text() == "Play original recording"
    mark = len(backend.calls)

    attempt_missing_b(qt_app, b, controller)
    a_is_untouched(backend, controller, a, state=PAUSED, position=700)
    assert a.play_action.text() == "Play original recording"
    assert stops_since(backend, mark) == 0
    b_reports_its_error(b, controller)
    menu_play(qt_app, a)                          # resumes from 700 ms
    assert controller.state_for("a") == PLAYING and backend.position() == 700


def test_the_owner_failing_itself_natural_end_stop_and_takeover_still_behave(
        qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    c = view.add(tx("c", 9.0, wavs["long"]))
    pump(qt_app)
    # The owner itself fails (its file is missing now).
    menu_play(qt_app, a)
    pathlib.Path(wavs["long"]).rename(tmp_path / "moved.wav")
    try:
        mark = len(backend.calls)
        controller.play("a", a.tx.audio_path)              # a fresh request: file gone
        assert controller.owner is None and backend.state() == STOPPED
        assert stops_since(backend, mark) == 1 and "missing" in controller.last_error["a"]
        assert not a.play_action.isEnabled()
        assert a.pairing_label.text() == "Audio missing"
    finally:
        (tmp_path / "moved.wav").rename(wavs["long"])
    a.refresh_pairing()                      # restored: re-checked on the next look
    # Natural end.
    menu_play(qt_app, a)
    backend.advance(9000)
    pump(qt_app)
    assert controller.owner is None and a.play_action.isEnabled()
    # Stop pressed.
    menu_play(qt_app, a)
    mark = len(backend.calls)
    controller.stop()
    assert controller.owner is None and stops_since(backend, mark) == 1
    # A valid C takes over from A.
    menu_play(qt_app, a)
    mark = len(backend.calls)
    menu_play(qt_app, c)
    assert controller.owner == "c" and backend.loaded == wavs["long"]
    assert stops_since(backend, mark) == 1
    assert a.play_action.isEnabled() and a.play_action.text() == "Play original recording"
    assert c.play_action.text() == "Pause"


def test_b_can_be_retried_once_its_recording_exists(qt_app, wavs, tmp_path):
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    menu_play(qt_app, a)
    attempt_missing_b(qt_app, b, controller)
    b_reports_its_error(b, controller)
    assert controller.owner == "a"
    shutil.copy(wavs["short"], b.tx.audio_path)            # the file appears
    b.refresh_pairing()                                    # seen on the next look
    assert b.pairing_label.text().startswith("Audio + transcript")
    backend.durations[b.tx.audio_path] = 2000
    mark = len(backend.calls)
    menu_play(qt_app, b)
    assert controller.owner == "b" and controller.state_for("b") == PLAYING
    assert backend.loaded == b.tx.audio_path and stops_since(backend, mark) == 1
    assert "b" not in controller.last_error
    assert "Could not play" not in b.status_label.text()
    assert a.play_action.isEnabled() and a.play_action.text() == "Play original recording"


def test_a_backend_that_rejects_the_file_on_load_gives_no_false_start(
        qt_app, wavs, tmp_path):
    """Scripted: the backend emits its error inside load(), synchronously.
    Whether a given QtMultimedia build does that is not established here."""
    backend, view, controller, a, b = two_bubbles(qt_app, wavs, tmp_path)
    shutil.copy(wavs["short"], b.tx.audio_path)            # exists, but undecodable
    b.refresh_pairing()
    real_load = backend.load

    def load(path):
        if path == b.tx.audio_path:
            backend.calls.append(("load", path))
            backend.errorOccurred.emit("the recording could not be decoded")
            backend._set(STOPPED)
            return
        real_load(path)

    backend.load = load
    menu_play(qt_app, a)
    mark = len(backend.calls)
    menu_play(qt_app, b)
    after = backend.calls[mark:]
    assert ("play",) not in after, f"play() was pressed after a failed load: {after}"
    assert controller.owner is None and controller.state_for("b") == STOPPED
    assert backend.state() == STOPPED
    assert "decoded" in controller.last_error["b"]
    assert "Could not play" in b.status_label.text()
    assert b.play_action.isEnabled()   # the file is there
    assert a.play_action.isEnabled() and a.play_action.text() == "Play original recording"   # A was stopped by the takeover
    assert controller.play("b", b.tx.audio_path) is False
