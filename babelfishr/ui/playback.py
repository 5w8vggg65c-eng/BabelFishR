"""Recording playback for the message thread.

One :class:`PlaybackController` per thread owns *which* recording is playing
and *what state it is in*; every bubble renders itself from that. The
controller is the single source of truth, so a bubble can never claim to be
paused because some other bubble's recording happens to be paused - the
defect the previous one-shared-player design invited.

The controller talks to a :class:`PlaybackBackend`. Two ship:

* :class:`QtMultimediaBackend` - QMediaPlayer, when PySide6's QtMultimedia
  module is present (it is in the packaged app). Real pause, stop, seek,
  position and completion, taken from the player's own signals rather than
  from a timer.
* :class:`SystemOpenBackend` - hands the file to the operating system's
  default player when QtMultimedia is absent. It **cannot** pause, stop or
  seek that external application and says so: ``controllable`` is False, so
  the thread shows a plain Play and never an expanded control bar it could
  not honour.

Tests substitute a scripted backend through the same seam.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
import sys
from typing import Dict, Optional, Tuple

from PySide6 import QtCore

log = logging.getLogger(__name__)

#: Recordings longer than this get the expanded control bar; anything up to
#: and including exactly this many seconds plays straight through. The
#: boundary is a recommendation the operator has not yet confirmed - see the
#: handoff - and lives here so it is one number to change.
LONG_RECORDING_SECONDS = 5.0

#: How far one press of rewind / fast-forward moves. Fixed skips rather than
#: held seeking, also pending the operator's decision.
SKIP_MS = 5000

STOPPED, PLAYING, PAUSED = "stopped", "playing", "paused"


class PlaybackBackend(QtCore.QObject):
    """What the controller needs from a player. Subclasses fill it in."""

    stateChanged = QtCore.Signal(str)        # STOPPED / PLAYING / PAUSED
    positionChanged = QtCore.Signal(int)     # milliseconds
    durationChanged = QtCore.Signal(int)     # milliseconds
    finished = QtCore.Signal()               # reached the end on its own
    errorOccurred = QtCore.Signal(str)       # a human-readable reason

    name = "abstract"
    #: Can pause, stop and seek actually be honoured?
    controllable = False

    def load(self, path: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def play(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def pause(self) -> None:
        """No-op unless controllable."""

    def stop(self) -> None:
        """No-op unless controllable."""

    def seek(self, position_ms: int) -> None:
        """No-op unless controllable."""

    def position(self) -> int:
        return 0

    def duration(self) -> int:
        return 0

    def state(self) -> str:
        return STOPPED


class QtMultimediaBackend(PlaybackBackend):
    """QMediaPlayer, with its own signals mapped onto the seam."""

    name = "qtmultimedia"
    controllable = True

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        self._QMediaPlayer = QMediaPlayer
        self._player = QMediaPlayer(self)
        self._output = QAudioOutput(self)
        self._player.setAudioOutput(self._output)
        self._player.playbackStateChanged.connect(self._on_state)
        # Relayed through slots, not signal-to-signal: QMediaPlayer reports
        # position and duration as qlonglong, and PySide6 6.11 refuses to
        # connect a qlonglong signal straight to this int signal (seen on
        # the macOS build runner: "Failed to connect signal
        # positionChanged(qlonglong) to signal positionChanged(int)").
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)

    def _on_position(self, position_ms) -> None:
        self.positionChanged.emit(int(position_ms))

    def _on_duration(self, duration_ms) -> None:
        self.durationChanged.emit(int(duration_ms))

    def load(self, path: str) -> None:
        self._player.setSource(QtCore.QUrl.fromLocalFile(str(path)))

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def stop(self) -> None:
        self._player.stop()

    def seek(self, position_ms: int) -> None:
        self._player.setPosition(max(0, int(position_ms)))

    def position(self) -> int:
        return int(self._player.position())

    def duration(self) -> int:
        return int(self._player.duration())

    def state(self) -> str:
        return self._map_state(self._player.playbackState())

    def _map_state(self, qt_state) -> str:
        P = self._QMediaPlayer
        if qt_state == P.PlayingState:
            return PLAYING
        if qt_state == P.PausedState:
            return PAUSED
        return STOPPED

    def _on_state(self, qt_state) -> None:
        self.stateChanged.emit(self._map_state(qt_state))

    def _on_status(self, status) -> None:
        P = self._QMediaPlayer
        if status == P.EndOfMedia:
            self.finished.emit()
        elif status == P.InvalidMedia:
            self.errorOccurred.emit("the recording could not be decoded")

    def _on_error(self, error, message: str) -> None:
        self.errorOccurred.emit(message or f"playback error {error}")


class SystemOpenBackend(PlaybackBackend):
    """Open the file with the OS default player. Fire and forget, honestly."""

    name = "system"
    controllable = False

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self._path = ""

    def load(self, path: str) -> None:
        self._path = str(path)

    def play(self) -> None:
        opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
        try:
            subprocess.Popen([opener, self._path],
                             shell=(sys.platform == "win32"))
        except OSError as exc:
            self.errorOccurred.emit(f"could not hand the recording to the "
                                    f"system player: {exc}")
            return
        # The external player is out of our hands from here. Reporting
        # "finished" at once is the only truthful state we can offer: the
        # thread must not show a Pause that would pause nothing.
        self.stateChanged.emit(PLAYING)
        self.finished.emit()


def make_backend(parent: Optional[QtCore.QObject] = None) -> PlaybackBackend:
    try:
        return QtMultimediaBackend(parent)
    except Exception as exc:  # noqa: BLE001 - absent module, or no backend
        log.info("QtMultimedia unavailable (%s); using the system player", exc)
        return SystemOpenBackend(parent)


class PlaybackController(QtCore.QObject):
    """Which recording is playing, and what it is doing. One per thread."""

    #: Ownership or state changed, carrying the ids of the recordings whose
    #: controls that concerns (a frozenset): the one playing, the one that
    #: was, the one whose request failed. Bubbles for other recordings have
    #: nothing to redraw, and a thread of thousands must not redraw them.
    changed = QtCore.Signal(object)
    #: (owner transmission id, position ms, duration ms) - for the label only.
    positionChanged = QtCore.Signal(str, int, int)

    def __init__(self, backend: Optional[PlaybackBackend] = None,
                 parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self.backend = backend or make_backend(self)
        self.owner: Optional[str] = None
        self.path: str = ""
        self._state: str = STOPPED
        self._duration_ms: int = 0
        self.last_error: Dict[str, str] = {}
        self.backend.stateChanged.connect(self._on_backend_state)
        self.backend.positionChanged.connect(self._on_position)
        self.backend.durationChanged.connect(self._on_duration)
        self.backend.finished.connect(self._on_finished)
        self.backend.errorOccurred.connect(self._on_error)

    # -- queries ---------------------------------------------------------
    @property
    def controllable(self) -> bool:
        return bool(self.backend.controllable)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    def state_for(self, tx_id: str) -> str:
        """STOPPED, PLAYING or PAUSED - for *this* recording, nobody else's."""
        if self.owner != tx_id:
            return STOPPED
        return self._state

    def position_for(self, tx_id: str) -> Tuple[int, int]:
        if self.owner != tx_id:
            return 0, 0
        return self.backend.position(), self._duration_ms or self.backend.duration()

    # -- commands --------------------------------------------------------
    def play(self, tx_id: str, path: Optional[str]) -> bool:
        """Start or resume *this* recording. Returns False if it cannot."""
        if not path:
            self._fail(tx_id, "this transmission has no recording")
            return False
        if not pathlib.Path(path).is_file():
            self._fail(tx_id, f"the recording file is missing: {path}")
            return False
        if self.owner == tx_id and self._state == PAUSED and self.path == path:
            self.last_error.pop(tx_id, None)
            self.backend.play()          # resume where it was paused
            # The same rule as a fresh start: a backend that rejects the
            # resume synchronously has already retired this recording and
            # recorded why; say so rather than claim success. An earlier
            # version returned True here regardless.
            return tx_id not in self.last_error
        previous = self.owner
        if previous is not None and previous != tx_id:
            # One recording at a time. Retire the previous owner outright.
            self.backend.stop()
        self.owner = tx_id
        self.path = str(path)
        self.last_error.pop(tx_id, None)
        self._duration_ms = 0
        self._state = PLAYING
        self.backend.load(self.path)
        if tx_id in self.last_error:
            # The backend reported an error while loading - synchronously,
            # before returning. _on_error has already retired this request
            # and recorded the reason; nothing is played and nothing is
            # claimed. An earlier version pressed play regardless and
            # returned True. (A backend that *finishes* synchronously - the
            # fire-and-forget system player - records no error and is a
            # success: the recording was handed over.)
            return False
        self.backend.play()
        if tx_id in self.last_error:
            return False               # the same, from play() itself
        self._emit_changed(previous, tx_id)
        return True

    def toggle(self, tx_id: str, path: Optional[str]) -> None:
        if self.owner == tx_id and self._state == PLAYING:
            self.pause()
        else:
            self.play(tx_id, path)

    def pause(self) -> None:
        if self.owner is not None and self._state == PLAYING and self.controllable:
            self.backend.pause()

    def stop(self) -> None:
        if self.owner is None:
            return
        self.backend.stop()
        self._retire()

    def skip(self, delta_ms: int) -> None:
        if self.owner is None or not self.controllable:
            return
        position = self.backend.position() + int(delta_ms)
        duration = self._duration_ms or self.backend.duration()
        if duration:
            position = min(position, duration)
        self.backend.seek(max(0, position))

    def rewind(self) -> None:
        self.skip(-SKIP_MS)

    def forward(self) -> None:
        self.skip(SKIP_MS)

    # -- backend events ----------------------------------------------------
    def _on_backend_state(self, state: str) -> None:
        if self.owner is None:
            return
        if state == STOPPED:
            # Reaching the end, an explicit stop, or the backend giving up:
            # in every case this recording is no longer playing.
            self._retire()
            return
        if state != self._state:
            self._state = state
            self._emit_changed(self.owner)

    def _on_position(self, position_ms: int) -> None:
        if self.owner is not None:
            self.positionChanged.emit(self.owner, int(position_ms),
                                      self._duration_ms or self.backend.duration())

    def _on_duration(self, duration_ms: int) -> None:
        self._duration_ms = int(duration_ms)
        if self.owner is not None:
            self.positionChanged.emit(self.owner, self.backend.position(),
                                      self._duration_ms)

    def _on_finished(self) -> None:
        self._retire()

    def _on_error(self, message: str) -> None:
        owner = self.owner
        if owner is not None:
            self.last_error[owner] = message
        with_backend = self.controllable
        if with_backend:
            try:
                self.backend.stop()
            except Exception:  # noqa: BLE001 - already in an error path
                log.debug("stop after playback error failed", exc_info=True)
        self._retire()

    def _fail(self, tx_id: str, message: str) -> None:
        """A request for *this* recording could not be honoured.

        Scoped to that recording. If it is the one playing, it is retired -
        backend stopped, ownership released. If another recording is playing
        it is left exactly as the backend has it: owner, state, position and
        controls. An earlier version set the shared state to STOPPED for
        every failure, so a missing file on B collapsed A's controls while
        the backend went on playing A with nothing left to stop it.
        """
        if self.owner == tx_id:
            self.backend.stop()
            self.owner = None
            self.path = ""
            self._state = STOPPED
            self._duration_ms = 0
        self.last_error[tx_id] = message
        self._emit_changed(tx_id)

    def _retire(self) -> None:
        if self.owner is None and self._state == STOPPED:
            return
        owner = self.owner
        self.owner = None
        self.path = ""
        self._state = STOPPED
        self._duration_ms = 0
        self._emit_changed(owner)

    def _emit_changed(self, *tx_ids: Optional[str]) -> None:
        self.changed.emit(frozenset(tx_id for tx_id in tx_ids if tx_id))


def format_clock(position_ms: int, duration_ms: int) -> str:
    """``0:07 / 0:42`` - or ``0:07`` alone until the duration is known."""
    def mmss(ms: int) -> str:
        seconds = max(0, int(ms)) // 1000
        return f"{seconds // 60}:{seconds % 60:02d}"

    if duration_ms and duration_ms > 0:
        return f"{mmss(position_ms)} / {mmss(duration_ms)}"
    return mmss(position_ms)
