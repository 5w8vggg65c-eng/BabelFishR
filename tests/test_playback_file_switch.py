"""One message, two files: switching between its original and its decoded
audio must hand the player over cleanly.

QMediaPlayer reports StoppedState synchronously when a new source is loaded.
Before the fix, PlaybackController.play() set the new owner and then loaded
the file; that stop arrived as "the recording ended" and retired the owner
it had just set. The audio kept playing with no owner, so nothing could pause
or stop it. The backend here behaves like QMediaPlayer in that respect.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtWidgets  # noqa: E402

from babelfishr.audio.wavefile import write_wav  # noqa: E402
from babelfishr.testing import speech_like, tone  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def qt_like_backend():
    """The scripted backend, loading the way QMediaPlayer does."""
    from test_alpha5_playback import make_scripted_backend

    from babelfishr.ui.playback import STOPPED

    backend = make_scripted_backend()
    scripted_load = backend.load

    def load(path):
        if backend.state() != STOPPED:
            backend._set(STOPPED)          # setSource stops, synchronously
        scripted_load(path)

    backend.load = load
    return backend


@pytest.fixture
def files(tmp_path):
    return {
        "original": write_wav(str(tmp_path / "original.wav"), speech_like(4.0, 48_000), 48_000),
        "decoded": write_wav(str(tmp_path / "decoded.wav"), tone(4.0, 700.0, 8000), 8000),
    }


@pytest.mark.parametrize("first,second", [("original", "decoded"),
                                          ("decoded", "original")])
@pytest.mark.parametrize("paused", [False, True])
def test_switching_files_of_one_message_keeps_control(qt_app, files, first, second,
                                                      paused):
    from babelfishr.ui.playback import PlaybackController

    backend = qt_like_backend()
    controller = PlaybackController(backend)
    assert controller.play("T", files[first])
    backend.advance(700)
    if paused:
        controller.pause()
        assert controller.state_for("T") == "paused"

    assert controller.play("T", files[second])
    assert controller.owner == "T"
    assert controller.path == files[second]
    assert controller.state_for("T") == "playing"
    assert backend.loaded == files[second] and backend.state() == "playing"
    assert ("stop",) in backend.calls, "the other file was not stopped first"

    controller.pause()
    assert controller.state_for("T") == "paused" and backend.state() == "paused"
    controller.stop()
    assert controller.owner is None and backend.state() == "stopped", (
        "the new file could not be stopped")


def test_a_paused_file_still_resumes_where_it_was(qt_app, files):
    from babelfishr.ui.playback import PlaybackController

    backend = qt_like_backend()
    controller = PlaybackController(backend)
    controller.play("T", files["original"])
    backend.advance(1500)
    controller.pause()
    loads = sum(1 for call in backend.calls if call[0] == "load")
    assert controller.play("T", files["original"])
    assert sum(1 for call in backend.calls if call[0] == "load") == loads, "reloaded"
    assert ("stop",) not in backend.calls
    assert backend.position() == 1500 and controller.state_for("T") == "playing"


def test_another_message_still_takes_over_and_missing_files_still_refuse(
        qt_app, files, tmp_path):
    from babelfishr.ui.playback import PlaybackController

    backend = qt_like_backend()
    controller = PlaybackController(backend)
    controller.play("A", files["original"])
    assert controller.play("B", files["decoded"])
    assert controller.owner == "B" and controller.state_for("A") == "stopped"
    assert controller.state_for("B") == "playing"

    # The existing failure rules, unchanged: a missing file for ANOTHER
    # message leaves the one playing alone...
    missing = str(tmp_path / "gone.wav")
    assert not controller.play("C", missing)
    assert "missing" in controller.last_error["C"]
    assert controller.owner == "B" and controller.path == files["decoded"]
    assert controller.state_for("B") == "playing"
    # ...and one for the message that is playing retires it (playback.py _fail).
    assert not controller.play("B", missing)
    assert controller.owner is None and backend.state() == "stopped"


def test_starting_the_same_file_again_while_it_plays_keeps_control(qt_app, files):
    from babelfishr.ui.playback import PlaybackController

    backend = qt_like_backend()
    controller = PlaybackController(backend)
    controller.play("T", files["decoded"])
    backend.advance(900)
    assert controller.play("T", files["decoded"])          # e.g. Play decoded again
    assert controller.owner == "T" and controller.state_for("T") == "playing"
    assert backend.position() == 0, "a fresh start begins at the beginning"
    controller.stop()
    assert controller.owner is None and backend.state() == "stopped"
