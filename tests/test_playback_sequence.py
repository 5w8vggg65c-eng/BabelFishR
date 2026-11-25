"""Continuous playback through a session: play one, the later ones follow.

The scripted backend from test_alpha5_playback stands in for the player: it
makes no sound, records every call, and reports state, completion and errors
when the test says so. "Recordings" are empty temp files - the controller
only checks that they exist.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import time

import pytest

pytest.importorskip("PySide6")

from babelfishr.ui.playback import PLAYING, STOPPED, PlaybackController  # noqa: E402
from babelfishr.ui.playback_sequence import (  # noqa: E402
    IDLE, MAX_CONSECUTIVE_ERRORS, WAITING, SequencePlayer)

_spec = importlib.util.spec_from_file_location(
    "playback_helpers", pathlib.Path(__file__).with_name("test_alpha5_playback.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
make_scripted_backend = _helpers.make_scripted_backend
qt_app = _helpers.qt_app


def pump(qt_app, seconds: float = 0.03) -> None:
    deadline = time.monotonic() + seconds
    while True:
        qt_app.processEvents()
        if time.monotonic() >= deadline:
            break
        time.sleep(0.002)


class Session:
    """A named session's recordings in chronological order - the provider.

    Two monitoring runs are just more rows: ordering across them is the
    provider's job, and the player only follows ``next_after``.
    """

    def __init__(self, tmp_path, ids, missing=()):
        self.order = []
        self.paths = {}
        self.live = False
        self.tmp_path = tmp_path
        for tx_id in ids:
            self.add(tx_id, exists=tx_id not in missing)

    def add(self, tx_id, exists=True):
        path = str(self.tmp_path / f"{tx_id}.wav")
        if exists:
            pathlib.Path(path).write_bytes(b"")
        self.order.append(tx_id)
        self.paths[tx_id] = path
        return path

    def next_after(self, tx_id):
        index = self.order.index(tx_id)
        if index + 1 < len(self.order):
            nxt = self.order[index + 1]
            return nxt, self.paths[nxt]
        return None

    def is_live(self):
        return self.live


def rig(qt_app, tmp_path, ids=("a", "b", "c"), backend=None, **kwargs):
    backend = backend or make_scripted_backend()
    controller = PlaybackController(backend)
    session = Session(tmp_path, ids, **kwargs)
    player = SequencePlayer(controller, session.next_after, session.is_live)
    player.settle_ms = 5
    seen = {"current": [], "state": []}
    player.currentChanged.connect(seen["current"].append)
    player.stateChanged.connect(seen["state"].append)
    return backend, controller, session, player, seen


def loads(backend):
    return [os.path.basename(c[1])[:-4] for c in backend.calls if c[0] == "load"]


def finish(qt_app, backend):
    """Play the current clip to its end, then let the player advance."""
    backend.advance(10 ** 9)
    pump(qt_app)


# ---- advancing ---------------------------------------------------------------


def test_natural_finish_advances_in_order_and_ends_at_the_newest(qt_app, tmp_path):
    backend, controller, session, player, seen = rig(
        qt_app, tmp_path, ids=("run1-a", "run1-b", "run2-a", "run2-b"))
    assert player.start("run1-a", session.paths["run1-a"])
    assert player.active and player.current_id == "run1-a"
    for expected in ("run1-b", "run2-a", "run2-b"):
        finish(qt_app, backend)
        assert controller.owner == expected and player.current_id == expected
    finish(qt_app, backend)
    assert not player.active and player.state == IDLE
    assert controller.owner is None
    assert loads(backend) == ["run1-a", "run1-b", "run2-a", "run2-b"], "wrapped"
    assert seen["current"] == ["run1-a", "run1-b", "run2-a", "run2-b", None]
    assert seen["state"] == [PLAYING, IDLE]


def test_starting_in_the_middle_only_plays_newer(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("b", session.paths["b"])
    finish(qt_app, backend)
    finish(qt_app, backend)
    assert loads(backend) == ["b", "c"] and not player.active


def test_finish_reported_before_the_stop_also_advances(qt_app, tmp_path):
    """Some players report completion first and the stop after it."""
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    backend._position = backend._duration          # at its end, as a real player is
    backend.finished.emit()
    backend._set(STOPPED)
    pump(qt_app)
    assert controller.owner == "b" and player.current_id == "b"


def test_a_clip_that_finishes_inside_play_still_advances(qt_app, tmp_path):
    backend = make_scripted_backend()
    scripted_play = backend.play

    def play():
        scripted_play()
        backend.advance(10 ** 9)           # zero-length: over at once

    backend.play = play
    _, controller, session, player, seen = rig(qt_app, tmp_path, backend=backend)
    assert player.start("a", session.paths["a"])
    pump(qt_app, 0.1)
    assert loads(backend) == ["a", "b", "c"]
    assert not player.active and seen["state"][-1] == IDLE


# ---- live sessions -----------------------------------------------------------


def test_waits_when_live_and_continues_on_a_new_recording(qt_app, tmp_path):
    backend, controller, session, player, seen = rig(qt_app, tmp_path, ids=("a",))
    session.live = True
    player.start("a", session.paths["a"])
    finish(qt_app, backend)
    assert player.waiting and player.active and player.current_id is None
    assert seen["state"] == [PLAYING, WAITING]
    player.notify_new_recording()          # nothing new yet: still waiting
    pump(qt_app)
    assert player.waiting and controller.owner is None

    session.add("b")
    player.notify_new_recording()
    pump(qt_app)
    assert controller.owner == "b" and player.current_id == "b"
    assert seen["state"] == [PLAYING, WAITING, PLAYING]


def test_waiting_ends_when_monitoring_ends(qt_app, tmp_path):
    backend, controller, session, player, seen = rig(qt_app, tmp_path, ids=("a",))
    session.live = True
    player.start("a", session.paths["a"])
    finish(qt_app, backend)
    assert player.waiting
    session.live = False
    player.notify_live_ended()
    assert not player.active and seen["state"][-1] == IDLE
    session.add("b")
    player.notify_new_recording()
    pump(qt_app)
    assert controller.owner is None, "restarted after the sequence ended"


def test_stop_while_waiting_ends_cleanly(qt_app, tmp_path):
    backend, controller, session, player, seen = rig(qt_app, tmp_path, ids=("a",))
    session.live = True
    player.start("a", session.paths["a"])
    finish(qt_app, backend)
    stops = backend.calls.count(("stop",))
    player.stop()
    assert not player.active and seen["state"][-1] == IDLE
    assert backend.calls.count(("stop",)) == stops
    session.add("b")
    player.notify_new_recording()
    pump(qt_app)
    assert controller.owner is None


def test_a_manual_play_while_waiting_ends_the_wait(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path, ids=("a", "x"))
    session.order.remove("x")              # not part of the session's order
    session.live = True
    player.start("a", session.paths["a"])
    finish(qt_app, backend)
    controller.play("x", session.paths["x"])
    assert not player.active
    finish(qt_app, backend)
    assert loads(backend) == ["a", "x"]


# ---- stopping, replacing, cancelling -----------------------------------------


def test_manual_stop_ends_and_a_late_finish_is_ignored(qt_app, tmp_path):
    backend, controller, session, player, seen = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    backend.advance(500)
    player.stop()
    assert not player.active and controller.owner is None
    backend.finished.emit()                # late, from the stopped clip
    pump(qt_app)
    assert loads(backend) == ["a"] and controller.owner is None
    assert seen["state"] == [PLAYING, IDLE]


def test_the_controllers_own_stop_ends_the_sequence(qt_app, tmp_path):
    """The bubble's Stop button goes straight to the controller."""
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    controller.stop()
    pump(qt_app)
    assert not player.active
    backend.finished.emit()
    pump(qt_app)
    assert loads(backend) == ["a"]


def test_a_different_manual_play_replaces_the_sequence(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    controller.play("c", session.paths["c"])
    assert not player.active and controller.owner == "c"
    finish(qt_app, backend)
    assert loads(backend) == ["a", "c"], "the sequence advanced from c"


def test_cancel_ignores_late_events_and_leaves_the_controller_alone(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    backend.advance(100)
    backend.advance(10 ** 9)               # finished queued an advance...
    player.cancel()                        # ...the session switched first
    pump(qt_app)
    assert loads(backend) == ["a"] and not player.active
    session.live = True
    player.notify_new_recording()
    pump(qt_app)
    assert loads(backend) == ["a"]


def test_cancel_while_playing_lets_the_clip_play_out_alone(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    player.cancel()
    assert controller.owner == "a"
    finish(qt_app, backend)
    assert loads(backend) == ["a"]


def test_start_replaces_a_running_sequence(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    player.start("b", session.paths["b"])
    assert controller.owner == "b" and player.current_id == "b"
    finish(qt_app, backend)
    finish(qt_app, backend)
    assert loads(backend) == ["a", "b", "c"] and not player.active


def test_pause_resume_and_seek_keep_the_sequence(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    backend.advance(1000)
    controller.pause()
    pump(qt_app, 0.05)
    assert player.active and player.current_id == "a"
    assert controller.play("a", session.paths["a"])           # resume
    controller.forward()
    controller.rewind()
    pump(qt_app, 0.05)
    assert player.current_id == "a"
    finish(qt_app, backend)
    assert controller.owner == "b" and player.current_id == "b"


# ---- skipping and failures ---------------------------------------------------


def test_a_missing_file_is_skipped(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(
        qt_app, tmp_path, ids=("a", "b", "c"), missing=("b",))
    player.start("a", session.paths["a"])
    finish(qt_app, backend)
    assert controller.owner == "c"
    assert "b" not in controller.last_error, "tried to play the missing file"


def test_a_provider_cycle_does_not_loop(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    calls = []

    def cyclic(tx_id):
        calls.append(tx_id)
        nxt = {"a": "b", "b": "a"}[tx_id]
        return nxt, session.paths[nxt]

    player._next_after = cyclic
    player.start("a", session.paths["a"])
    finish(qt_app, backend)
    finish(qt_app, backend)
    assert loads(backend) == ["a", "b"] and not player.active
    assert len(calls) == 2


def test_a_provider_cycle_of_missing_files_does_not_loop(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(
        qt_app, tmp_path, ids=("a", "m1", "m2"), missing=("m1", "m2"))
    player._next_after = lambda tx_id: (
        {"a": "m1", "m1": "m2", "m2": "m1"}[tx_id],
        session.paths[{"a": "m1", "m1": "m2", "m2": "m1"}[tx_id]])
    player.start("a", session.paths["a"])
    finish(qt_app, backend)
    assert not player.active and loads(backend) == ["a"]


def test_a_refused_first_play_starts_nothing(qt_app, tmp_path):
    backend, controller, session, player, seen = rig(qt_app, tmp_path,
                                                     missing=("a",))
    assert not player.start("a", session.paths["a"])
    assert not player.active and seen["state"] == []
    pump(qt_app)
    assert loads(backend) == []


def test_a_refused_later_play_ends_cleanly(qt_app, tmp_path):
    backend, controller, session, player, seen = rig(qt_app, tmp_path)
    original_play = controller.play

    def refuse_b(tx_id, path):
        if tx_id == "b":
            controller.last_error[tx_id] = "refused"
            return False
        return original_play(tx_id, path)

    controller.play = refuse_b
    player.start("a", session.paths["a"])
    finish(qt_app, backend)
    pump(qt_app)
    assert not player.active and seen["state"] == [PLAYING, IDLE]
    assert loads(backend) == ["a"]


def test_an_error_mid_clip_skips_to_the_next(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    backend.fail("decoder gave up")
    pump(qt_app)
    assert controller.owner == "b" and player.current_id == "b"
    assert "decoder" in controller.last_error["a"]


def test_consecutive_errors_are_capped(qt_app, tmp_path):
    ids = [f"t{i}" for i in range(MAX_CONSECUTIVE_ERRORS + 3)]
    backend, controller, session, player, _ = rig(qt_app, tmp_path, ids=ids)
    player.start(ids[0], session.paths[ids[0]])
    for _ in range(MAX_CONSECUTIVE_ERRORS + 2):
        if controller.owner is not None:
            backend.fail("bad")
        pump(qt_app)
    assert not player.active
    assert loads(backend) == ids[:MAX_CONSECUTIVE_ERRORS]


def test_a_success_resets_the_error_count(qt_app, tmp_path):
    ids = [f"t{i}" for i in range(2 * MAX_CONSECUTIVE_ERRORS)]
    backend, controller, session, player, _ = rig(qt_app, tmp_path, ids=ids)
    player.start(ids[0], session.paths[ids[0]])
    for _ in range(MAX_CONSECUTIVE_ERRORS - 1):
        backend.fail("bad")
        pump(qt_app)
    finish(qt_app, backend)                # one plays to its end
    backend.fail("bad")
    pump(qt_app)
    assert player.active, "the earlier errors still counted"


def test_a_quiet_stop_with_no_reason_ends_the_sequence(qt_app, tmp_path):
    backend, controller, session, player, _ = rig(qt_app, tmp_path)
    player.start("a", session.paths["a"])
    backend._set(STOPPED)                  # no finished, no error
    pump(qt_app, 0.1)
    assert not player.active and loads(backend) == ["a"]
    backend.finished.emit()                # very late; ignored
    pump(qt_app)
    assert loads(backend) == ["a"]


# ---- the system player -------------------------------------------------------


def test_an_uncontrollable_backend_plays_one_clip_and_never_cascades(qt_app, tmp_path):
    backend = make_scripted_backend(controllable=False)
    scripted_play = backend.play

    def play():                            # like SystemOpenBackend
        scripted_play()
        backend._set(STOPPED)
        backend.finished.emit()

    backend.play = play
    _, controller, session, player, seen = rig(qt_app, tmp_path, backend=backend)
    session.live = True
    assert player.start("a", session.paths["a"])
    pump(qt_app, 0.1)
    assert loads(backend) == ["a"]
    assert not player.active and not player.waiting and seen["state"] == []
    player.notify_new_recording()
    pump(qt_app)
    assert loads(backend) == ["a"]


def test_nothing_starts_without_start(qt_app, tmp_path):
    backend, controller, session, player, seen = rig(qt_app, tmp_path)
    session.live = True
    player.notify_new_recording()
    controller.play("a", session.paths["a"])
    finish(qt_app, backend)
    assert loads(backend) == ["a"] and not player.active and seen["state"] == []


# ---- the controller's ending signals -----------------------------------------


def test_the_controller_tells_finish_stop_and_error_apart(qt_app, tmp_path):
    backend = make_scripted_backend()
    controller = PlaybackController(backend)
    events = []
    controller.clipFinished.connect(lambda t: events.append(("finished", t)))
    controller.clipStopped.connect(lambda t: events.append(("stopped", t)))
    controller.clipFailed.connect(lambda t: events.append(("failed", t)))
    paths = {}
    for name in "abcd":
        paths[name] = str(tmp_path / f"{name}.wav")
        pathlib.Path(paths[name]).write_bytes(b"")
    controller.play("a", paths["a"])
    backend.advance(10 ** 9)
    controller.play("b", paths["b"])
    controller.stop()
    controller.play("c", paths["c"])
    backend.fail("x")
    controller.play("d", paths["d"])
    controller.play("a", paths["a"])       # replacing d is none of the three
    controller.pause()
    controller.forward()
    assert events == [("finished", "a"), ("stopped", "b"), ("failed", "c")]
