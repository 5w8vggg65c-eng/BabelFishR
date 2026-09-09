"""Resuming a paused recording reports what happened, like a fresh start.

The scripted backend from test_alpha5_playback: a scripted signal order
shows the controller's contract, not what any QtMultimedia build emits.
"""

from __future__ import annotations

import importlib.util
import pathlib

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


def paused_at_700(qt_app, wavs, tmp_path):
    backend = make_scripted_backend()
    backend.durations[wavs["long"]] = 9000
    view, controller = make_view(backend)
    view.show()
    a = view.add(tx("a", 9.0, wavs["long"]))
    b = view.add(tx("b", 9.0, wavs["short"]))
    pump(qt_app)
    click(qt_app, a.play_button)
    backend.advance(700)
    click(qt_app, a.pause_button)
    pump(qt_app)
    assert controller.state_for("a") == PAUSED and backend.position() == 700
    return backend, view, controller, a, b


def test_a_resume_the_backend_rejects_returns_false_and_shows_the_error(qt_app, wavs,
                                                                         tmp_path):
    backend, view, controller, a, b = paused_at_700(qt_app, wavs, tmp_path)

    def rejecting_play():
        backend.calls.append(("play",))
        backend.fail("resume rejected")             # errorOccurred, then STOPPED

    backend.play = rejecting_play
    assert controller.play("a", wavs["long"]) is False
    assert controller.owner is None
    assert controller.state_for("a") == STOPPED and backend.state() == STOPPED
    assert controller.last_error["a"] == "resume rejected"
    pump(qt_app)
    assert "Could not play: resume rejected" in a.status_label.text()
    assert a.play_button.isEnabled() and a.playback_bar.isHidden()
    # Nothing of B's was touched by A's failure.
    assert "b" not in controller.last_error and b.status_label.text() != a.status_label.text()


def test_a_resume_that_works_returns_true_and_continues_from_the_pause(qt_app, wavs,
                                                                       tmp_path):
    backend, view, controller, a, b = paused_at_700(qt_app, wavs, tmp_path)
    mark = len(backend.calls)
    assert controller.play("a", wavs["long"]) is True
    assert backend.calls[mark:] == [("play",)], "resume reloaded or stopped the recording"
    assert controller.owner == "a" and controller.state_for("a") == PLAYING
    assert controller.position_for("a") == (700, 9000)
    backend.advance(300)
    assert controller.position_for("a") == (1000, 9000)
    assert "a" not in controller.last_error


def test_a_paused_recording_still_survives_bs_failure_and_yields_to_a_valid_b(
        qt_app, wavs, tmp_path):
    backend, view, controller, a, b = paused_at_700(qt_app, wavs, tmp_path)
    # B's file missing: A stays paused at 700, exactly as before.
    assert controller.play("b", str(tmp_path / "missing.wav")) is False
    assert controller.owner == "a" and controller.state_for("a") == PAUSED
    assert backend.position() == 700 and "missing" in controller.last_error["b"]
    # A valid B takes over; A is retired and B plays from the start.
    mark = len(backend.calls)
    assert controller.play("b", wavs["short"]) is True
    assert controller.owner == "b" and controller.state_for("a") == STOPPED
    assert ("stop",) in backend.calls[mark:] and backend.state() == PLAYING
    assert backend.position() == 0
