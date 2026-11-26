"""Continuous playback through a session.

The operator presses Play on one recording and playback carries on through
the session's later transmissions - upwards in the newest-first thread - one
after another, until the operator stops it. :class:`SequencePlayer` adds that
on top of an existing :class:`~babelfishr.ui.playback.PlaybackController`;
the controller stays the single source of truth for what is playing, and the
bubbles go on rendering from it exactly as before.

What the player decides, and why:

* It advances **only** when the controller reports that the clip it started
  reached its end on its own (``clipFinished``). A pause is not an ending,
  a seek is not an ending, and a stop is the operator saying "enough".
* Anything else that takes the player away from its clip ends the sequence:
  ``stop()`` on the controller (``clipStopped``), or the controller being
  told to play some other recording - or another file of the same one.
* An error while a clip plays skips that clip and goes on, so one damaged
  file does not silently end a long listen. After
  :data:`MAX_CONSECUTIVE_ERRORS` failures in a row it gives up instead: by
  then something is wrong with the player, not with one file.
* ``controller.play()`` refusing a clip outright ends the sequence. There is
  no retry loop: a refused clip will be refused again.
* Which recording comes next is the caller's business (``next_after``): it
  knows the session's order, hidden rows and missing originals. The player
  still checks that the file exists, and never plays or considers the same
  transmission twice in one sequence, so a confused provider cannot make it
  loop.
* When the newest recording has played and the session is still being
  monitored (``is_live``), it waits - visibly, and stoppably - for the next
  one (``notify_new_recording``). Otherwise it ends. It never wraps around.
* Every advance runs from the event loop (``QTimer.singleShot(0)``), after
  the controller has finished its own bookkeeping, and carries a generation
  number. Stopping, cancelling or starting again bumps the generation, so a
  late or queued callback from before can never start playback.

A backend that cannot be controlled (the system player) reports "finished"
the moment it hands the file over. Advancing on that would open every
recording in the session at once, so with such a backend :meth:`start`
plays the one clip and no sequence begins.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional, Set, Tuple

from PySide6 import QtCore

log = logging.getLogger(__name__)

IDLE, PLAYING, WAITING = "idle", "playing", "waiting"

#: Errors in a row (clips that failed while playing) before the sequence
#: gives up rather than skipping on.
MAX_CONSECUTIVE_ERRORS = 3

#: The most candidates one advance will look at before giving up. A real
#: session's skippable run is far shorter; this only stops a runaway provider.
MAX_CANDIDATES_PER_ADVANCE = 10_000

#: How long to wait, after the controller lets go of the clip with no word
#: why, for the backend's "finished" or error to arrive before treating it
#: as the end of the sequence. Players report the stop and the completion
#: back to back, so this is generous.
SETTLE_MS = 250

NextAfter = Callable[[str], Optional[Tuple[str, str]]]


class SequencePlayer(QtCore.QObject):
    """Plays a session's recordings one after another, oldest to newest."""

    #: The transmission now playing (or None when the sequence ends), so the
    #: thread can highlight and reveal it.
    currentChanged = QtCore.Signal(object)
    #: IDLE, PLAYING or WAITING.
    stateChanged = QtCore.Signal(str)

    def __init__(self, controller, next_after: NextAfter,
                 is_live: Callable[[], bool],
                 parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self.controller = controller
        self._next_after = next_after
        self._is_live = is_live
        self._state = IDLE
        self._held_between = False
        self._gen = 0
        self._current_id: Optional[str] = None
        self._current_path = ""
        self._last_id: Optional[str] = None     # where WAITING resumes from
        self._seen: Set[str] = set()
        self._errors = 0
        self._resolved = False      # the current clip's ending is known
        self._starting = False      # inside controller.play() for our clip
        self._sync_end: Optional[str] = None    # "finished"/"failed" during it
        self.settle_ms = SETTLE_MS
        controller.clipFinished.connect(self._on_clip_finished)
        controller.clipFailed.connect(self._on_clip_failed)
        controller.clipStopped.connect(self._on_clip_stopped)
        controller.changed.connect(self._on_controller_changed)

    # -- queries ---------------------------------------------------------
    @property
    def active(self) -> bool:
        """Playing a clip of the sequence, or waiting for the next one."""
        return self._state != IDLE

    @property
    def waiting(self) -> bool:
        return self._state == WAITING

    @property
    def current_id(self) -> Optional[str]:
        """The transmission the sequence is playing; None when not playing."""
        return self._current_id if self._state == PLAYING else None

    @property
    def state(self) -> str:
        return self._state

    # -- commands --------------------------------------------------------
    def start(self, tx_id: str, path: str) -> bool:
        """Play this recording and carry on through the later ones.

        Replaces any sequence already running. Returns what
        ``controller.play`` returned; when it refuses, no sequence begins.
        """
        self._end(announce=self.active)
        self._seen = {tx_id}
        self._errors = 0
        if not self.controller.controllable:
            # See the module notes: "finished" from this backend means
            # "handed over", not "heard to the end".
            return self.controller.play(tx_id, path)
        return self._play(tx_id, path, first=True)

    def stop(self) -> None:
        """The operator's Stop: end the sequence and the clip it is playing."""
        owned = (self._state == PLAYING
                 and self.controller.owner == self._current_id)
        self._end(announce=self.active)
        if owned:
            self.controller.stop()

    def skip_forward(self, delta_ms: int = 5000) -> None:
        """The playing card's +5 s.

        More than *delta_ms* left: an ordinary seek. *delta_ms* or less left
        while continuous playback owns the clip: this clip is over - the
        sequence moves once to the START of the next playable recording,
        by the same rules as a natural end (missing files skipped, waiting
        while the Session is live, the end of the Session). A second press
        before the next clip starts, a Stop, a Pause or a stale end report
        cannot advance twice or restart anything.
        """
        controller = self.controller
        owned = (self._state == PLAYING and not self._resolved and not self._starting
                 and controller.controllable
                 and controller.owner == self._current_id
                 and controller.path == self._current_path)
        if not owned:
            controller.skip(delta_ms)
            return
        position, duration = controller.position_for(self._current_id)
        if duration <= 0 or duration - position > delta_ms:
            controller.skip(delta_ms)
            return
        self._resolved = True                 # this clip's end is decided
        controller.end_early(self._current_id)
        self._errors = 0
        self._schedule_advance()

    def cancel(self) -> None:
        """End the sequence without touching the controller.

        For a session switch or shutdown, where the caller deals with the
        controller itself. Whatever is playing plays out, and nothing
        follows it.
        """
        self._end(announce=self.active)

    @property
    def paused_between_clips(self) -> bool:
        return self._held_between

    def toggle_between_clips(self) -> None:
        """Pause at an end-of-media boundary or while waiting for live audio."""
        if not self.active or self.controller.owner is not None:
            return
        self._held_between = not self._held_between
        self.stateChanged.emit(self._state)
        if not self._held_between and (self.waiting or self._resolved):
            self._schedule_advance()

    def notify_new_recording(self) -> None:
        """A new recording was saved to the session; carry on if waiting."""
        if self._state == WAITING:
            gen = self._gen
            QtCore.QTimer.singleShot(0, lambda: self._advance(gen))

    def notify_live_ended(self) -> None:
        """Monitoring of the session stopped; nothing more will arrive."""
        if self._state == WAITING:
            self._end(announce=True)

    # -- controller events -------------------------------------------------
    def _owns(self, tx_id: str) -> bool:
        return self._state == PLAYING and tx_id == self._current_id

    def _on_clip_finished(self, tx_id: str) -> None:
        if not self._owns(tx_id) or self._resolved:
            return
        self._resolved = True
        if self._starting:
            self._sync_end = "finished"
            return
        self._errors = 0
        self._schedule_advance()

    def _on_clip_failed(self, tx_id: str) -> None:
        if not self._owns(tx_id) or self._resolved:
            return
        self._resolved = True
        if self._starting:
            self._sync_end = "failed"
            return
        self._after_failure(tx_id)

    def _on_clip_stopped(self, tx_id: str) -> None:
        if self._owns(tx_id) and not self._starting:
            self._end(announce=True)

    def _on_controller_changed(self, _ids) -> None:
        if self._starting or self._state == IDLE:
            return
        owner = self.controller.owner
        if self._state == WAITING:
            if owner is not None:
                self._end(announce=True)  # played by hand
            return
        if owner is not None:
            if (owner != self._current_id
                    or self.controller.path != self._current_path):
                self._end(announce=True)  # replaced
            return
        if not self._resolved:
            # Let go of with no reason yet. The reason ("finished", an
            # error, a stop) normally follows at once; if none comes, the
            # backend gave up quietly and the sequence ends.
            gen = self._gen
            QtCore.QTimer.singleShot(self.settle_ms,
                                     lambda: self._settle(gen))

    def _settle(self, gen: int) -> None:
        if gen != self._gen or self._state != PLAYING or self._resolved:
            return
        if self.controller.owner is None:
            log.info("playback of %s stopped with no reason given; "
                     "continuous playback ends", self._current_id)
            self._end(announce=True)

    # -- advancing ---------------------------------------------------------
    def _schedule_advance(self) -> None:
        gen = self._gen
        QtCore.QTimer.singleShot(0, lambda: self._advance(gen))

    def _after_failure(self, tx_id: str) -> None:
        self._errors += 1
        if self._errors >= MAX_CONSECUTIVE_ERRORS:
            log.warning("continuous playback stopped after %d failed "
                        "recordings in a row (last: %s)", self._errors, tx_id)
            self._end(announce=True)
            return
        log.info("recording %s failed to play; skipping to the next", tx_id)
        self._schedule_advance()

    def _advance(self, gen: int) -> None:
        if gen != self._gen or self._state == IDLE or self._held_between:
            return
        if self._state == PLAYING and self.controller.owner is not None:
            # Something is playing (it can only be another clip by now, which
            # the changed handler deals with). Never start a second one.
            return
        last = self._current_id if self._state == PLAYING else self._last_id
        for _ in range(MAX_CANDIDATES_PER_ADVANCE):
            try:
                found = self._next_after(last)
            except Exception:  # noqa: BLE001 - the provider is not ours
                log.exception("finding the next recording after %s failed", last)
                self._end(announce=True)
                return
            if gen != self._gen:
                return                      # the provider re-entered us
            if found is None:
                self._wait_or_end(last)
                return
            tx_id, path = found
            if tx_id in self._seen:
                log.warning("the next recording after %s is %s again; "
                            "continuous playback ends", last, tx_id)
                self._end(announce=True)
                return
            self._seen.add(tx_id)
            if not path or not os.path.isfile(path):
                log.info("skipping %s: its recording is missing", tx_id)
                last = tx_id
                continue
            self._play(tx_id, path, first=False)
            return
        log.warning("gave up looking for the next recording after %d "
                    "candidates", MAX_CANDIDATES_PER_ADVANCE)
        self._end(announce=True)

    def _wait_or_end(self, last: Optional[str]) -> None:
        try:
            live = bool(self._is_live())
        except Exception:  # noqa: BLE001 - the provider is not ours
            log.exception("checking whether the session is live failed")
            live = False
        if not live:
            self._end(announce=True)
            return
        self._last_id = last
        if self._state != WAITING:
            self._state = WAITING
            self._current_id = None
            self._current_path = ""
            self.currentChanged.emit(None)
            self.stateChanged.emit(WAITING)

    def _play(self, tx_id: str, path: str, *, first: bool) -> bool:
        self._gen += 1
        was = self._state
        self._state = PLAYING
        self._current_id = tx_id
        self._current_path = str(path)
        self._last_id = tx_id
        self._resolved = False
        self._sync_end = None
        self._starting = True
        try:
            ok = self.controller.play(tx_id, path)
        finally:
            self._starting = False
        if not ok:
            log.info("recording %s could not be played; continuous playback "
                     "%s", tx_id, "does not start" if first else "ends")
            self._end(announce=not first)
            return False
        self.currentChanged.emit(tx_id)
        if was != PLAYING:
            self.stateChanged.emit(PLAYING)
        # A clip that ended inside play() (a very short one, or a backend
        # that reports at once) is dealt with now, from the event loop.
        if self._sync_end == "finished":
            self._errors = 0
            self._schedule_advance()
        elif self._sync_end == "failed":
            self._after_failure(tx_id)
        return True

    def _end(self, *, announce: bool) -> None:
        """Back to idle; every pending callback becomes stale."""
        self._gen += 1
        was = self._state
        self._state = IDLE
        self._held_between = False
        self._current_id = None
        self._current_path = ""
        self._last_id = None
        self._resolved = True
        if announce and was != IDLE:
            self.currentChanged.emit(None)
            self.stateChanged.emit(IDLE)
