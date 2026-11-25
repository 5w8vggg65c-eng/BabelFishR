"""The receive pipeline.

    audio source -> level meter -> detector -> record original -> store
                                                     |
                                                     v
                              transcribe -> detect language -> translate -> store

Two thread pools, deliberately separated:

* **Capture** runs one thread that does nothing slow.  It reads blocks, updates
  the meter, feeds the detector and writes WAV files.  Transcription must never
  happen here - a 3-second Whisper call would drop audio on the floor.
* **Processing** runs a small pool that transcribes and translates.  Work is
  queued, so a backlog delays transcripts but never loses audio.

Capture first, classify second
------------------------------
Every detected event is written to disk *and* to the database before anything
classifies, transcribes, translates or analyses it.  Classification decides
only whether an ASR call happens automatically; it can never decide whether the
recording exists.  Static, tones and suspected digital bursts are all kept, and
the operator can force transcription or digital analysis on any of them
afterwards - which matters because a transmission cannot be received twice.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import pathlib
import queue
import threading
import time as _time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .audio.meter import LevelMeter, LevelReading
from .audio.safety import SafetyRecorder
from .audio.source import AudioBlock, AudioSource
from .audio.spectrum import SpectrumAnalyzer
from .audio.wavefile import write_wav
from .config import Config
from .detect import DetectedTransmission, RadioActivityDetector
from .models import (ContentClass, ProcessingState, Provenance, RadioProfile,
                     Session, SourceLanguageMode, Transmission, utcnow)
from .providers import (EngineError, EngineUnavailable, Glossary,
                        TranscriptionEngine, TranslationEngine)
from .storage import Store

log = logging.getLogger(__name__)


class PipelineState(str):
    """UI-facing states, as named in the product brief."""

    IDLE = "idle"
    LISTENING = "listening"
    RECEIVING = "receiving"
    TRANSCRIBING = "transcribing"
    TRANSLATING = "translating"
    COMPLETE = "complete"
    ERROR = "error"


@dataclasses.dataclass
class Event:
    """Something the UI may want to react to."""

    kind: str
    """``state``, ``level``, ``transmission``, ``updated``, ``audio-status``,
    ``error``, ``session``."""

    payload: Any = None
    at: _dt.datetime = dataclasses.field(default_factory=utcnow)
    run: Optional[str] = None
    """The monitoring run (session id) whose automatic work produced this,
    or None for manual work and everything else. The window drops a run's
    events once that run has been stopped."""


class EventBus:
    """Callbacks plus a drainable queue, so both push and poll UIs work.

    Qt wants to touch widgets only on the GUI thread, so the Qt front-end polls
    :meth:`drain` on a timer instead of subscribing directly.
    """

    def __init__(self, max_queue: int = 2000):
        self._subscribers: List[Callable[[Event], None]] = []
        self._queue: "queue.Queue[Event]" = queue.Queue(maxsize=max_queue)
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def publish(self, kind: str, payload: Any = None,
                run: Optional[str] = None) -> None:
        event = Event(kind=kind, payload=payload, run=run)
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a bad listener must not stop capture
                log.exception("event subscriber failed")
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except queue.Empty:  # pragma: no cover
                pass

    def drain(self, limit: int = 200) -> List[Event]:
        out: List[Event] = []
        for _ in range(limit):
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out


class Recorder:
    """Writes each transmission's original audio, unmodified, to disk."""

    def __init__(self, directory: str, layout: str = "{date}/{session}",
                 bit_depth: int = 16, enabled: bool = True):
        self.directory = pathlib.Path(directory).expanduser()
        self.layout = layout
        self.bit_depth = bit_depth
        self.enabled = enabled

    def path_for(self, tx: Transmission, session: Session) -> pathlib.Path:
        fields = {
            "date": tx.started_at.strftime("%Y-%m-%d"),
            "session": session.id,
            "profile": _slug(session.profile_label or "no-profile"),
            "channel": _slug(tx.channel_name or "no-channel"),
        }
        try:
            relative = self.layout.format(**fields)
        except KeyError as exc:
            log.warning("bad recording layout %r (%s); using date/session",
                        self.layout, exc)
            relative = f"{fields['date']}/{fields['session']}"
        stamp = tx.started_at.strftime("%Y%m%dT%H%M%S")
        return self.directory / relative / f"{stamp}_{tx.id}.wav"

    def write(self, tx: Transmission, session: Session,
              audio: np.ndarray, sample_rate: int) -> Optional[str]:
        if not self.enabled:
            return None
        target = self.path_for(tx, session)
        try:
            return write_wav(str(target), audio, sample_rate, self.bit_depth)
        except OSError as exc:
            log.error("could not write recording %s: %s", target, exc)
            return None


#: Below this much voiced audio (local VAD, seconds), the detector heard no
#: speech. An absolute figure, not a share: one short real phrase in a long
#: noisy recording is still speech. On its own this only marks the message
#: for review; translation is held back only when the speech engine agrees
#: (see assess_speech).
NO_SPEECH_VAD_SECONDS = 0.25
#: Used instead when the detector reports only a share.
NO_SPEECH_VAD_RATIO = 0.02
#: Whisper's own no-speech estimate at which it would skip a window by
#: itself (its default), used only when no VAD opinion is available, and
#: only together with a low transcript confidence.
NO_SPEECH_PROBABILITY = 0.6
NO_SPEECH_CONFIDENCE = 0.5
#: An automatically detected language below this probability is reported as
#: uncertain rather than as a missing language pack.
LANGUAGE_CERTAIN = 0.8

# signal_metadata key: translation was not attempted because an uncertain
# automatic language guess names a pair with no installed pack. Not a
# failure (nothing broke) and not a success (nothing was translated): the
# message needs review until it is retried in a chosen language or the
# operator saves the words. Cleared whenever the message is recognised or
# translated again.
TRANSLATION_UNRESOLVED = "translation_unresolved"


def degenerate_text(text: str) -> tuple:
    """Does a transcript look like recognition output with no speech behind
    it? Returns ``(kind, reason)``: kind is ``"severe"``, ``"repeated"`` or
    ``""``, and reason says why in words.

    Speech recognisers fed noise often return a loop, sometimes with high
    confidence (a field recording gave 0.93 for a repeated-symbol line).
    These checks use only the shape of the text, so they apply to every
    script alike: no alphabet or language is treated as suspect.

    ``"severe"`` is deliberately narrow and structural, either:
    one character, not a digit, repeated unbroken at least 12 times and
    making up at least 80% of the text (``ლლლლ...``, ``!!!!...``) - an
    elongated shout inside other words ("Nooooooooooooo! Stop!") is not it;
    or a long line (12+ non-space characters) at least 90% symbols, spaced or
    not - the shape of a real field loop (56 characters, three distinct
    brackets/symbols, every one separated by a space), still matched with a
    stray letter or a few more distinct symbols in it. Either is a heuristic: very unlikely to be a
    faithful transcript of speech, not proof that none was spoken - which
    is why the words and the audio are always kept for review.
    Punctuation, number formatting and repeated digits never count as
    severe ("NO!!!", "S.O.S.", "1, 2, 3", "00000000" are ordinary).

    ``"repeated"`` is a reason to look, not to hold: the same words over and
    over, loop-like compression, or text with no letters or digits at all.
    Noise produces these, but so do real calls ("mayday mayday mayday", a
    callsign or number repeated). See assess_speech.
    """
    import itertools
    import zlib

    text = (text or "").strip()
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return ("", "")
    runs = [(char, sum(1 for _ in run)) for char, run in itertools.groupby(text)
            if not char.isspace() and not char.isdigit()]
    char, longest = max(runs, key=lambda r: r[1], default=("", 0))
    if longest >= 12 and longest / len(chars) >= 0.8:
        return ("severe", "one character repeats unbroken")
    letterless = not any(c.isalnum() for c in chars)
    symbol_share = sum(not c.isalnum() for c in chars) / len(chars)
    if len(chars) >= 12 and symbol_share >= 0.9:
        # Speech is transcribed as words; a long line that is almost all
        # symbols or brackets is a loop, spaced or not - including one with
        # a stray letter or digit, or several different symbols in it.
        return ("severe", "it is almost all symbols, with almost no letters or digits")
    words = [w.strip(".,!?;:\u3001\u3002\u060c\u061f\"'()-").lower() for w in text.split()]
    words = [w for w in words if w]
    if len(words) >= 6 and len(set(words)) / len(words) <= 0.4:
        return ("repeated", "the same words repeat")
    raw = text.encode("utf-8")
    if len(raw) >= 30 and len(raw) / len(zlib.compress(raw, 9)) >= 2.4:
        return ("repeated", "it repeats itself")
    if len(chars) >= 3 and letterless:
        return ("repeated", "it has no letters or digits")
    return ("", "")


def assess_speech(result) -> Optional[dict]:
    """Was this recording speech at all? Evidence, not a filter.

    Returns None when there is nothing to say (no text, or no evidence).
    Otherwise a small record for ``signal_metadata["speech_check"]``:
    ``verdict`` is ``"likely_noise"`` (strong evidence of no speech - two
    independent signals agree, or the text itself is a loop: the transcript
    is kept, translation is not attempted, the message is marked for review),
    ``"uncertain"`` (one signal doubts it: kept, translated, marked for
    review) or ``"speech"``. ``reasons`` says why in words. The recording
    itself is never touched. Engine confidence alone never clears a
    transcript: a looping one can score high.
    """
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        return None
    ratio = getattr(result, "speech_ratio", None)
    seconds = getattr(result, "speech_seconds", None)
    nsp = getattr(result, "no_speech_probability", None)
    confidence = getattr(result, "confidence", None)
    loop_kind, loop_reason = degenerate_text(text)
    if ratio is None and nsp is None and not loop_kind:
        return None
    engine_doubts = nsp is not None and nsp >= NO_SPEECH_PROBABILITY
    engine_unsure = confidence is not None and confidence < NO_SPEECH_CONFIDENCE
    vad_heard_none = False
    if ratio is not None:
        vad_heard_none = (seconds < NO_SPEECH_VAD_SECONDS if seconds is not None
                          else ratio < NO_SPEECH_VAD_RATIO)
        # Strong evidence only: the local detector heard no speech AND the
        # speech engine itself doubts it. Either alone means "review".
        likely_noise = vad_heard_none and (engine_doubts or engine_unsure)
        uncertain = vad_heard_none or engine_doubts
    else:
        likely_noise = engine_doubts and engine_unsure
        uncertain = engine_doubts
    reasons = []
    if loop_kind == "severe":
        # Structural: held whatever else is known.
        likely_noise = True
        reasons.append(f"the transcript looks like a recognition loop: {loop_reason}")
    elif loop_kind == "repeated":
        # Plausible repeated words - a real call can repeat. Held only when
        # no-speech evidence agrees; otherwise translated and marked.
        uncertain = True
        if vad_heard_none or engine_doubts:
            likely_noise = True
        reasons.append(f"the transcript repeats itself: {loop_reason}")
    if vad_heard_none:
        reasons.append("the local voice detector heard almost no speech")
    if engine_doubts:
        reasons.append("the speech engine itself estimates there was no speech")
    if engine_unsure and (vad_heard_none or engine_doubts):
        reasons.append("the speech engine's confidence is low")
    verdict = "likely_noise" if likely_noise else ("uncertain" if uncertain else "speech")
    return {"verdict": verdict, "reasons": reasons, "repetition": loop_kind,
            "speech_ratio": ratio, "speech_seconds": seconds,
            "no_speech_probability": nsp,
            "transcript_confidence": confidence,
            "method": ("local VAD + engine estimate" if ratio is not None
                       else "engine estimate only" if nsp is not None
                       else "transcript text only")}


def _skip_reason(detected: DetectedTransmission) -> str:
    """Plain-language note explaining why automatic ASR was not attempted.

    There is deliberately no entry for ``digital-suspected`` or ``noise``.
    Neither classification routes anything away from speech recognition any
    more: on a real Mac the first landed on ordinary voice, and the second is
    where a weak voice under static ends up. Both are chips on the bubble now,
    not vetoes.
    """
    return {
        "tone": "Classified as a steady tone (courtesy beep or unmodulated "
                "carrier), which cannot contain speech. The recording is kept "
                "- use 'Transcribe anyway' if you disagree.",
    }.get(detected.content_class.value,
          "Automatic speech processing was skipped. The recording is kept.")


def _clear_processing_stopped(tx: Transmission) -> None:
    if is_processing_stopped(tx):
        tx.signal_metadata = {k: v for k, v in tx.signal_metadata.items()
                              if k != PROCESSING_STOPPED}


def _slug(text: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in text.strip())
    return safe.strip("-") or "unnamed"


class ProcessingStopped(RuntimeError):
    """Work was offered to a processor that has already begun shutting down."""


class RunStopped(ProcessingStopped):
    """Automatic work was offered for a monitoring run that has been stopped.

    The recording is already saved; it is marked saved-not-processed
    (PROCESSING_STOPPED) instead, for the operator to process by hand."""


#: signal_metadata key: monitoring was stopped before this was processed.
#: Saved with state SKIPPED (terminal, so nothing resumes it automatically);
#: Retry / Transcribe anyway clear it.
PROCESSING_STOPPED = "processing_stopped"


def mark_processing_stopped(tx: Transmission, run: Optional[str]) -> Transmission:
    """Saved audio, not processed because its monitoring run was stopped.

    Anything the row already holds (a transcript saved before Stop) stays.
    """
    tx.state = ProcessingState.SKIPPED
    tx.auto_processed = False
    tx.skip_reason = ("Monitoring was stopped before this recording was "
                      "processed. The recording is saved: use Transcribe "
                      "anyway (or Retry) to process it.")
    tx.signal_metadata = dict(tx.signal_metadata or {}, **{
        PROCESSING_STOPPED: {"run": run, "at": utcnow().isoformat()}})
    return tx


def is_processing_stopped(tx: Transmission) -> bool:
    return bool((tx.signal_metadata or {}).get(PROCESSING_STOPPED))


class _Abandon(Exception):
    """Internal: the item's monitoring run was stopped; stop at this stage."""


class ProcessingPipeline:
    """Queued transcription and translation, isolated from audio capture.

    Accounting rule: an id is *in flight* from the moment :meth:`submit`
    accepts it until the worker that ran it has finished with it. That
    covers three states the queue alone cannot see - queued, dequeued but not
    yet running, and running - so :attr:`pending` never reads zero while a
    job is anywhere between acceptance and completion. Admission and
    accounting share one lock, which is what makes :meth:`stop_if_idle`
    atomic: no submission can land between "nothing pending" and "stopped".
    """

    def __init__(self, store: Store, transcription: Optional[TranscriptionEngine],
                 translation: Optional[TranslationEngine], config: Config,
                 events: EventBus, glossary: Optional[Glossary] = None,
                 workers: int = 1):
        self.store = store
        self.transcription = transcription
        self.translation = translation
        self.config = config
        self.events = events
        self.glossary = glossary or Glossary()
        self.worker_count = max(1, workers)

        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._threads: List[threading.Thread] = []
        self._running = False
        #: True from start() until the first stop request. Read and written
        #: only under the accounting lock, so a submission cannot slip into a
        #: processor that has already begun retiring.
        self._accepting = False
        self._session: Optional[Session] = None
        #: One lock for admission and accounting.
        self._active_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        #: Ids accepted and not yet finished: queued, dequeued but not yet
        #: running, or running. Also what lets the application refuse to
        #: delete a message a worker is still writing, and say so.
        self._in_flight: set = set()
        #: For each id in flight, the monitoring run it was accepted for
        #: automatically, or None for manual work (Retry, Transcribe anyway).
        self._origin: Dict[str, Optional[str]] = {}
        #: When each automatic item was accepted (monotonic), for the
        #: per-message timing line - how long it waited before processing.
        self._accepted_at: Dict[str, float] = {}
        #: Monitoring runs that have been stopped: their automatic work is
        #: not started, not saved and not published (see cancel_run).
        self._stopped_runs: set = set()
        self._local = threading.local()

    # -- lifecycle -------------------------------------------------------
    def start(self, session: Optional[Session] = None) -> None:
        """Start the workers. ``session`` is optional.

        Processing a recording that is already on disk needs no session: every
        value the pipeline uses - source-language mode, target language,
        engines - comes from the transmission row or the configuration, never
        from a live capture.
        """
        self._session = session
        self._running = True
        with self._active_lock:
            self._accepting = True
        for index in range(self.worker_count):
            # A live run warms its speech engine first, on the worker itself
            # (never two loads at once, never the GUI thread): the first
            # message then does not also wait for the model to load.
            warm = session is not None and index == 0
            thread = threading.Thread(target=self._work, args=(warm,), daemon=True,
                                      name=f"babelfishr-processing-{index}")
            thread.start()
            self._threads.append(thread)

    def stop(self, wait: bool = True, timeout: float = 10.0) -> bool:
        """Ask the workers to stop, and report whether they all have.

        Admission closes first, under the lock, so nothing is accepted after
        this point. Work already accepted stays accounted for in
        :attr:`pending` whether or not it ever runs: a worker leaving because
        of this call finishes the job it holds and takes nothing new, and an
        id still queued behind the stop stays in flight rather than being
        forgotten. What was not done is never reported as done.

        Returns True only when every worker thread has actually ended. A
        thread that outlives the join is *kept* in ``_threads`` - visible
        through :attr:`finished` and :meth:`worker_threads` - together with
        the engines and store it can still reach. An earlier version cleared
        the list after a timed join, which made a still-running worker
        invisible to everything that decided whether its engines could be
        closed or its mode changed.
        """
        with self._active_lock:
            self._accepting = False
        self._running = False
        for _ in self._threads:
            self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join(timeout=timeout)
        self._threads = [t for t in self._threads if t.is_alive()]
        return not self._threads

    def stop_if_idle(self, timeout: float = 5.0, wait: bool = True) -> bool:
        """Stop only if nothing accepted is unfinished; atomic with admission.

        The idle check and the closing of admission happen under the same
        lock, so a submission cannot land between "nothing pending" and
        "stopped". With ``wait`` (the default) this joins the workers and
        returns True only when they have all ended; False either because
        work is accounted for (nothing changed) or because a worker outlived
        the join - the caller tells the two apart by :attr:`pending`.

        With ``wait=False`` nothing is joined - for a caller on the GUI
        thread. True then means the stop *began*: admission is closed and the
        sentinels are queued; whether the workers have ended is
        :attr:`finished`, which they reach on their own. False means work is
        accounted for and nothing changed.
        """
        with self._active_lock:
            if self._in_flight:
                return False
            self._accepting = False
        ended = self.stop(wait=wait, timeout=timeout)
        return ended if wait else True

    @property
    def pending(self) -> int:
        """Everything accepted and not yet finished, wherever it is."""
        with self._active_lock:
            return len(self._in_flight)

    @property
    def accepting(self) -> bool:
        """Still taking work - no stop has begun."""
        with self._active_lock:
            return self._accepting

    @property
    def finished(self) -> bool:
        """No worker thread is alive. True for a pipeline never started."""
        return not any(t.is_alive() for t in self._threads)

    @property
    def shutting_down(self) -> bool:
        """A stop has begun and at least one worker has not yet returned."""
        return not self.accepting and not self.finished

    def worker_threads(self) -> List[threading.Thread]:
        """The worker threads still alive - after a stop, the stragglers."""
        return [t for t in self._threads if t.is_alive()]

    def wait_until_idle(self, timeout: float = 120.0) -> bool:
        """Block until nothing is queued or in flight."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pending == 0:
                return True
            self._idle.wait(timeout=0.05)
        return self.pending == 0

    def submit(self, tx_id: str, run: Optional[str] = None) -> bool:
        """Accept one id. Returns False if it is already in flight.

        An id already accepted is not queued twice: one run is what was asked
        for, and a second queue entry would let ``pending`` reach zero while
        that entry still waited. Once a stop has begun this refuses, by
        exception, rather than dropping the id or queueing it behind a
        worker that will never take it - the caller learns the processor is
        retiring and can say so.
        """
        with self._active_lock:
            if run is not None and run in self._stopped_runs:
                raise RunStopped(f"monitoring run {run} has been stopped")
            if not self._accepting:
                raise ProcessingStopped(
                    "this processor is shutting down and takes no new work")
            if tx_id in self._in_flight:
                return False
            self._in_flight.add(tx_id)
            self._origin[tx_id] = run
            self._accepted_at[tx_id] = _time.monotonic()
            self._idle.clear()
        self._queue.put(tx_id)
        return True

    # -- per-run cancellation (Stop) ---------------------------------------
    def cancel_run(self, run: str):
        """Stop a monitoring run's automatic work, without waiting.

        From now on: nothing more is accepted for *run*; its queued items
        leave the queue (their recordings are already saved); an item of
        its already running finishes its current engine call - a native
        call cannot be interrupted - but nothing it produces is saved or
        published. Manual work, and other runs, are untouched.

        Returns ``(removed, running)``: ids taken out of the queue, which
        the caller marks saved-not-processed, and ids of the run still
        held by a worker, which the worker marks itself.
        """
        with self._active_lock:
            self._stopped_runs.add(run)
            ours = {tx_id for tx_id, origin in self._origin.items() if origin == run}
            kept, removed = [], []
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None and item in ours and item not in removed:
                    removed.append(item)
                else:
                    kept.append(item)
            for item in kept:
                self._queue.put_nowait(item)
            for tx_id in removed:
                self._in_flight.discard(tx_id)
                self._origin.pop(tx_id, None)
            running = sorted(ours - set(removed))
            if not self._in_flight:
                self._idle.set()
        return removed, running

    @property
    def visible_pending(self) -> int:
        """Work the window may show as processing: manual jobs, and
        automatic work of runs not stopped. An engine call abandoned at
        Stop still counts in :attr:`pending` (it owns the engines until it
        returns) but not here."""
        with self._active_lock:
            return sum(1 for tx_id in self._in_flight
                       if self._origin.get(tx_id) not in self._stopped_runs
                       or self._origin.get(tx_id) is None)

    @property
    def automatic_pending(self) -> int:
        """Accepted work that came from a monitoring run (not Retry /
        Transcribe anyway)."""
        with self._active_lock:
            return sum(1 for tx_id in self._in_flight
                       if self._origin.get(tx_id) is not None)

    @property
    def has_manual_work(self) -> bool:
        with self._active_lock:
            return any(self._origin.get(tx_id) is None for tx_id in self._in_flight)

    def is_manual(self, tx_id: str) -> bool:
        """In flight as manual work (Retry, Transcribe anyway)."""
        with self._active_lock:
            return tx_id in self._in_flight and self._origin.get(tx_id) is None

    def run_stopped(self, run: Optional[str]) -> bool:
        with self._active_lock:
            return run is not None and run in self._stopped_runs

    def _current_run(self) -> Optional[str]:
        return getattr(self._local, "run", None)

    def _publish(self, kind: str, payload: Any = None) -> None:
        """Publish, tagged with the run of the item this worker is running."""
        self.events.publish(kind, payload, run=self._current_run())

    def _settle_if_stopped(self, tx: Transmission) -> bool:
        """After an automatic item's last save: if its run was stopped in the
        gap between the last check and that save, the row is put back to
        saved-not-processed (and only that is published). The window has
        dropped the run's other events already. True when it did so."""
        run = self._current_run()
        if not self.run_stopped(run) or is_processing_stopped(tx):
            return False
        mark_processing_stopped(tx, run)
        self.store.save_transmission(tx)
        self._publish("updated", tx)
        return True

    def _check_run(self, tx: Transmission) -> None:
        """At a stage boundary: abandon the item if its run was stopped."""
        if self.run_stopped(self._current_run()):
            raise _Abandon()

    def is_in_flight(self, tx_id: str) -> bool:
        with self._active_lock:
            return tx_id in self._in_flight

    def resume_pending(self) -> int:
        """Re-queue anything left unfinished by a previous run."""
        pending = self.store.pending_transmissions()
        for tx in pending:
            self.submit(tx.id)
        if pending:
            log.info("resuming %d unfinished transmission(s)", len(pending))
        return len(pending)

    def force_transcribe(self, tx_id: str) -> bool:
        """Operator override: transcribe a recording that was skipped.

        Classification is advice; this is how the operator disagrees with it.
        """
        tx = self.store.get_transmission(tx_id)
        if tx is None or not tx.audio_path:
            return False
        tx.clear_error()
        tx.state = ProcessingState.CAPTURED
        tx.auto_processed = False
        tx.skip_reason = ""
        _clear_processing_stopped(tx)
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        try:
            self.submit(tx_id)
        except ProcessingStopped:
            # Saved as Captured - honestly pending - but this processor is
            # retiring and will not run it. The caller reports "not now".
            return False
        return True

    def retry(self, tx_id: str, source_language: Optional[str] = None) -> bool:
        """Retry a failed transmission. The audio was never lost.

        With *source_language* the operator is saying "this is speech, in
        this language": the message is transcribed again in that language
        (as if the session had specified it) and translated even if the
        speech check would otherwise have held the translation back. The
        check is still recorded, for the record.
        """
        # Admission first, atomically, before the row is read or changed: a
        # message in flight (the worker would save its own copy over any
        # change) or a processor shutting down is refused, and nothing about
        # the message is altered. Returns whether it was really queued.
        with self._active_lock:
            if not self._accepting or tx_id in self._in_flight:
                return False
            self._in_flight.add(tx_id)
            self._origin[tx_id] = None          # manual
            self._idle.clear()
        queued = False
        try:
            queued = self._prepare_retry(tx_id, source_language)
        finally:
            if queued:
                self._queue.put(tx_id)
            else:
                self._release(tx_id)
        return queued

    def _release(self, tx_id: str) -> None:
        with self._active_lock:
            self._in_flight.discard(tx_id)
            self._origin.pop(tx_id, None)
            self._accepted_at.pop(tx_id, None)
            if not self._in_flight:
                self._idle.set()

    def _prepare_retry(self, tx_id: str, source_language: Optional[str]) -> bool:
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return False
        if not tx.audio_path and "audio" in (
                (tx.signal_metadata or {}).get("history_cleared") or {}):
            # Clear Session History removed the recording: queuing would
            # only fail.
            return False
        if source_language:
            tx.source_language = source_language
            tx.source_language_mode = SourceLanguageMode.SPECIFIED
            tx.signal_metadata = dict(tx.signal_metadata or {}, speech_confirmed={
                "source_language": source_language, "by": "operator"})
            tx.skip_reason = ""
            # New words, not yet checked: an earlier review was of the old ones.
            tx.reviewed = False
            if tx.transcript_correction or tx.translation_correction:
                # An earlier correction would otherwise keep overriding the
                # new words on screen. It is kept, not discarded.
                earlier = list(tx.signal_metadata.get("superseded_corrections") or [])
                earlier.append({
                    "transcript": tx.transcript_correction,
                    "translation": tx.translation_correction,
                    "corrected_at": tx.corrected_at.isoformat() if tx.corrected_at else None})
                tx.signal_metadata = dict(tx.signal_metadata, superseded_corrections=earlier)
                tx.transcript_correction = ""
                tx.translation_correction = ""
        tx.clear_error()
        tx.state = ProcessingState.CAPTURED
        if is_processing_stopped(tx):
            # Saved-not-processed after a Stop: Retry is the manual route.
            _clear_processing_stopped(tx)
            tx.skip_reason = ""
        self.store.save_transmission(tx)
        self._publish("updated", tx)
        return True

    # -- worker ----------------------------------------------------------
    def _work(self, warm: bool = False) -> None:
        if warm:
            self._preload()
        while self._running:
            try:
                tx_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if tx_id is None:
                break
            # Between the get above and the work below the id is neither
            # queued nor finished. It stayed in _in_flight the whole way, so
            # pending never read zero during this hand-off - the gap an
            # earlier version opened by counting the queue plus a separate
            # "active" figure that was incremented only here.
            with self._active_lock:
                self._local.run = self._origin.get(tx_id)
                accepted = self._accepted_at.get(tx_id)
            began = _time.monotonic()
            try:
                self._process(tx_id)
                # Timing only, never words: where a message's time went.
                log.info("message %s: waited %.2f s in the queue, processed in %.2f s",
                         tx_id, (began - accepted) if accepted else 0.0,
                         _time.monotonic() - began)
            except Exception:  # noqa: BLE001 - a worker must never die
                log.exception("unhandled error processing %s", tx_id)
            finally:
                self._local.run = None
                self._release(tx_id)

    def _preload(self) -> None:
        """Load the speech model on this worker before any message, through
        the engine's optional preload() hook (not warm_up(), which is the
        readiness self-test and transcribes a fixture). A failure is logged
        and the worker carries on: the first real message reports it."""
        preload = getattr(self.transcription, "preload", None)
        if not callable(preload):
            return
        started = _time.monotonic()
        try:
            preload()
        except Exception as exc:  # noqa: BLE001 - a worker must never die
            log.warning("speech model not preloaded: %s", exc)
            return
        log.info("speech model loaded in %.2f s", _time.monotonic() - started)

    def _engine_gate(self, stage: str) -> None:
        """Refuse an engine call once this processor has been told to stop.

        Defence in depth behind admission and accounting, not a substitute
        for them: a mode change is refused while anything is in flight, so
        this is only reached by a stop that arrived mid-job. The stage is
        then recorded as failed, for this reason, rather than run on engines
        that may already belong to a retired mode. The recording and any
        transcript already saved are untouched and the message is retryable.
        """
        if not self._running:
            raise EngineError(
                f"processing was stopped before {stage} could run; "
                f"retry when processing is available again")

    def _process(self, tx_id: str) -> None:
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            log.warning("transmission %s vanished before processing", tx_id)
            return

        if self._current_run() is None:
            # Manual work (Retry, Transcribe anyway): whatever was marked
            # saved-not-processed is being processed now. Clear it on this
            # copy too, in case a Stop's marker write landed after the
            # operator's request was saved.
            _clear_processing_stopped(tx)
        try:
            try:
                self._check_run(tx)
                if not self._transcribe(tx):
                    self._settle_if_stopped(tx)
                    return
                self._check_run(tx)
                self._translate(tx)
                self._check_run(tx)
            except _Abandon:
                # The run was stopped: nothing more is saved or shown as a
                # result. The row is saved-not-processed, keeping whatever it
                # held before Stop; the event says only that.
                mark_processing_stopped(tx, self._current_run())
                self.store.save_transmission(tx)
                self._publish("updated", tx)
                return

            if tx.state is not ProcessingState.FAILED:
                tx.state = ProcessingState.COMPLETE
            self.store.save_transmission(tx)
            if not self._settle_if_stopped(tx):
                self._publish("updated", tx)
        finally:
            # Finished - on every path out, and that is all this pipeline can
            # honestly say. It used to publish LISTENING here, a claim about
            # the microphone it has no way of checking. Then it published
            # COMPLETE, but only after a *successful* transcription: an empty
            # result, a handled engine error and a missing recording all
            # returned before this line, and the window stayed on
            # Transcribing with nothing left to transcribe. The transmission's
            # own outcome - COMPLETE with no words, FAILED with its error - is
            # already saved and published above; this is only the activity
            # signal. The window resolves it against the actual capture.
            self._publish("state", PipelineState.COMPLETE)

    def _load_audio(self, tx: Transmission):
        from .audio.wavefile import read_wav

        if not tx.audio_path or not pathlib.Path(tx.audio_path).exists():
            raise EngineError("the recorded audio file is missing")
        return read_wav(tx.audio_path)

    def _transcribe(self, tx: Transmission) -> bool:
        if self.transcription is None:
            tx.state = ProcessingState.SKIPPED
            self.store.save_transmission(tx)
            self._publish("updated", tx)
            return False
        if tx.transcript and tx.state is not ProcessingState.CAPTURED:
            return True  # already done on an earlier attempt

        tx.state = ProcessingState.TRANSCRIBING
        self.store.save_transmission(tx)
        self._publish("updated", tx)
        self._publish("state", PipelineState.TRANSCRIBING)

        language = None
        if tx.source_language_mode is SourceLanguageMode.SPECIFIED:
            language = tx.source_language

        try:
            audio, rate = self._load_audio(tx)
            self._engine_gate("transcription")
            result = self.transcription.transcribe(
                audio, rate, language=language,
                vocabulary=self.glossary.vocabulary(language) or None)
        except (EngineError, EngineUnavailable, OSError, ValueError) as exc:
            self._check_run(tx)       # stopped meanwhile: not a failure to show
            tx.fail("transcription", str(exc))
            self.store.save_transmission(tx)
            self._publish("updated", tx)
            self._publish("error", {"transmission": tx.id, "stage": "transcription",
                                          "message": str(exc)})
            return False
        self._check_run(tx)           # stopped during the call: discard it

        tx.transcript = result.text
        tx.transcript_confidence = result.confidence
        tx.transcript_segments = list(result.segments)
        tx.transcription_engine = result.engine
        tx.transcription_engine_version = result.engine_version
        if result.language:
            tx.source_language = result.language
            tx.language_confidence = result.language_confidence
        check = assess_speech(result)
        metadata = dict(tx.signal_metadata or {})
        metadata.pop("speech_check", None)
        metadata.pop(TRANSLATION_UNRESOLVED, None)
        if check is not None:
            metadata["speech_check"] = check
        tx.signal_metadata = metadata
        tx.state = ProcessingState.TRANSCRIBED
        tx.clear_error()
        self.store.save_transmission(tx)
        self._publish("updated", tx)

        if not result.text.strip():
            self._check_run(tx)
            tx.state = ProcessingState.COMPLETE
            self.store.save_transmission(tx)
            self._publish("updated", tx)
            return False
        return True

    def _missing_pair(self, tx: Transmission, target: str) -> tuple:
        """``(kind, words)``: why this pair cannot be translated, or ("", "").

        ``kind`` is "uncertain" for an uncertain automatic language guess
        (the language is unresolved; nothing failed) or "missing" for a
        language pack that is really missing. Skipped when the engine installs packs
        itself (Online / Setup), which must get its chance. A capability
        lookup that raises EngineError/EngineUnavailable propagates to the
        caller's handled translation-failure path.
        """
        source = tx.source_language
        supports = getattr(self.translation, "supports_pair", None)
        if (not source or source == target or not callable(supports)
                or getattr(self.translation, "auto_install", False)
                or supports(source, target)):
            return "", ""
        # A pair can also look missing because the engine itself is broken
        # (Argos not importable, or no packs readable at all): report that,
        # with the engine's own reason, rather than blame one language pack.
        available = getattr(self.translation, "available", None)
        if callable(available) and not available():
            reason = getattr(self.translation, "unavailable_reason", None)
            raise EngineUnavailable((reason() if callable(reason) else "")
                                    or "the translation engine is not available")
        confidence = tx.language_confidence
        if (tx.source_language_mode is not SourceLanguageMode.SPECIFIED
                and (confidence is None or confidence < LANGUAGE_CERTAIN)):
            shown = f"{confidence:.0%}" if confidence is not None else "unknown certainty"
            return "uncertain", (
                f"Translation not attempted: language unresolved. Automatic "
                f"detection guessed '{source}' ({shown}), and no "
                f"{source}->{target} pack is installed. If this is speech, "
                f"choose its language with Message > Retry as speech in... "
                f"(Session options > Source language sets it for new "
                f"recordings).")
        return "missing", (f"no {source}->{target} language pack is installed. Install "
                           f"it in Online / Setup (Tools > Setup assistant), then Retry.")

    def _translate(self, tx: Transmission) -> None:
        if self.translation is None:
            return
        if TRANSLATION_UNRESOLVED in (tx.signal_metadata or {}):
            tx.signal_metadata = {k: v for k, v in tx.signal_metadata.items()
                                  if k != TRANSLATION_UNRESOLVED}
        target = tx.target_language or self.config.translate.target_language
        if (self.config.translate.skip_if_same_language
                and tx.source_language and tx.source_language == target):
            tx.translation = ""
            tx.translation_engine = ""
            return
        metadata = tx.signal_metadata or {}
        check = metadata.get("speech_check") or {}
        if check.get("verdict") == "likely_noise" and not metadata.get("speech_confirmed"):
            # Probably not speech: translating it would only add a confident
            # sentence about noise. The transcript stays, marked for review.
            # "Retry as speech in..." (retry with a source language) lifts this.
            tx.translation = ""
            tx.translation_engine = ""
            return
        tx.state = ProcessingState.TRANSLATING
        self.store.save_transmission(tx)
        self._publish("updated", tx)
        self._publish("state", PipelineState.TRANSLATING)

        try:
            self._engine_gate("translation")
            kind, missing = self._missing_pair(tx, target)
            if kind == "uncertain":
                # Not attempted, and said so: the transcript and audio stay,
                # the message is marked for review (needs_review and the
                # review queue read this marker), and no FAILED error claims
                # a breakage that did not happen.
                tx.translation = ""
                tx.translation_engine = ""
                tx.target_language = target
                tx.signal_metadata = dict(tx.signal_metadata or {}, **{
                    TRANSLATION_UNRESOLVED: {
                        "reason": "language-uncertain",
                        "guessed_language": tx.source_language,
                        "language_confidence": tx.language_confidence,
                        "target_language": target,
                        "message": missing}})
                return
            if missing:
                tx.fail("translation", missing)
                self.store.save_transmission(tx)
                self._publish("updated", tx)
                self._publish("error", {"transmission": tx.id,
                                              "stage": "translation",
                                              "message": missing})
                return
            result = self.translation.translate(
                tx.transcript, target, source_language=tx.source_language,
                glossary=self.glossary.mapping(tx.source_language) or None,
                do_not_translate=self.glossary.protected(tx.source_language) or None)
        except (EngineError, EngineUnavailable) as exc:
            self._check_run(tx)
            # The transcript survives: only the translation stage failed.
            tx.fail("translation", str(exc))
            self.store.save_transmission(tx)
            self._publish("updated", tx)
            self._publish("error", {"transmission": tx.id, "stage": "translation",
                                          "message": str(exc)})
            return

        self._check_run(tx)           # stopped during the call: discard it
        tx.translation = "" if result.untranslated else result.text
        tx.target_language = target
        tx.translation_engine = result.engine
        tx.translation_engine_version = result.engine_version
        tx.clear_error()


class CaptureService:
    """Owns the audio thread: source -> meter -> detector -> disk -> queue."""

    def __init__(self, source: AudioSource, store: Store, session: Session,
                 config: Config, events: EventBus,
                 pipeline: Optional[ProcessingPipeline] = None,
                 profile: Optional[RadioProfile] = None):
        self.source = source
        self.store = store
        self.session = session
        self.config = config
        self.events = events
        self.pipeline = pipeline
        self.profile = profile

        self.meter = LevelMeter()
        self.detector = RadioActivityDetector(source.sample_rate,
                                              config.detector.to_settings())
        self.recorder = Recorder(
            directory=config.recording.directory, layout=config.recording.layout,
            bit_depth=config.audio.bit_depth, enabled=config.recording.enabled)
        safety = config.audio.safety_recording
        self.safety = SafetyRecorder(
            directory=str(pathlib.Path(config.recording.directory) / "safety"),
            chunk_seconds=safety.chunk_seconds, enabled=safety.enabled,
            retention_hours=safety.retention_hours, max_bytes=safety.max_bytes,
            bit_depth=config.audio.bit_depth, session_id=session.id)

        self._thread: Optional[threading.Thread] = None
        self._running = False
        # The end of a run - flushing the last detected transmission, closing
        # the safety recording - happens exactly once, on whichever side gets
        # there first: stop() or the audio thread on its way out.
        self._finish_lock = threading.Lock()
        self._finished = False
        #: The thread stop_async() hands the waiting to. None until then.
        self._stopper: Optional[threading.Thread] = None
        self._source_stopped = False
        self._source_stop_called = False
        self._source_failure_reported = False
        self.source_error = ""
        #: Transmissions saved here that no processor accepted. Nothing is
        #: lost - the WAV and the row exist - but they are pending, and the
        #: application must not report them finished.
        self.unprocessed: List[str] = []
        #: Set by stop()/stop_async(): the run ends at the operator's cutoff.
        self._stop_requested = False
        self.state = PipelineState.IDLE
        self.transmissions_captured = 0
        # Level readings (and the spectrum they carry) are published about
        # every 0.1 s of audio, or every block if blocks are longer. Not
        # faster: the UI drains the bus at 10 Hz, and a faster-than-real-time
        # replay must not crowd transmissions out of the bounded queue.
        self._level_divisor = max(1, int(0.1 * source.sample_rate
                                         / max(config.audio.block_size, 1)))
        self._block_count = 0
        #: The visible INPUT spectrum. Fed here, on the capture thread - the
        #: PortAudio callback only queues blocks - and computed only when a
        #: reading is published. Fixed memory: one ring, no history.
        self.spectrum = SpectrumAnalyzer()

    #: How long stop() waits for the audio thread before reporting that it
    #: is still running. A bound on the caller's wait, not on the thread.
    stop_timeout = 5.0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self.source.start()
        self._running = True
        self._finished = False
        self._source_stopped = False
        self._source_stop_called = False
        self._set_state(PipelineState.LISTENING)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="babelfishr-capture")
        self._thread.start()

    def stop(self, timeout: Optional[float] = None) -> bool:
        """Stop the audio thread and finish the run, blocking. True once it has.

        For callers that may block - the command line, tests. The window uses
        :meth:`stop_async`. The last detected transmission and its recording
        are written by whichever side gets there first - the stopping side, or
        the audio thread on its way out - and only once (:meth:`_finish_once`).
        A thread that has not returned by the timeout is *not* forgotten: it
        stays referenced (:attr:`alive`), this returns False, and the thread
        completes the finish itself when it does return.
        """
        timeout = self.stop_timeout if timeout is None else timeout
        self._close_intake()
        self._running = False
        return self._stop_blocking(timeout)

    def stop_async(self) -> None:
        """Begin stopping without blocking the caller.

        Everything that can wait - joining the audio thread, closing the
        source, the final flush - runs on a small stopper thread, so the GUI
        thread that pressed Stop never waits on audio. Until :attr:`settled`
        the capture stays a producer that can still hand over its final
        transmission, and whoever owns it must treat it as one. When the audio
        thread has already returned (a replay that reached its end) there is
        nothing to wait for and the finish happens here, at once.
        """
        self._close_intake()
        self._running = False
        thread = self._thread
        ended = thread is None or not thread.is_alive()
        if ended and self._finished and self._source_stopped:
            self._thread = None
            return                     # nothing is left to do: settled already
        if self._stopper is not None and self._stopper.is_alive():
            return                     # a stop is already under way
        # Even with the audio thread gone, closing the source and flushing
        # the detector can take time (a device's stop, the last WAV), so they
        # too run on the stopper, never on the caller. An earlier version ran
        # them here when the thread had ended, and a slow source.stop() held
        # the GUI thread for its duration.
        self._stopper = threading.Thread(
            target=self._stop_blocking, args=(self.stop_timeout,),
            name="babelfishr-capture-stop", daemon=True)
        self._stopper.start()

    def _close_intake(self) -> None:
        """The audio cutoff: from the Stop request on, the source adds
        nothing new; what it already holds is still drained into the final
        flush (_drain_source). Without this, a live input kept filling its
        queue until the stopper closed the device - after the audio thread
        had already drained it - so post-Stop audio could reach the last
        recording."""
        # An operator cutoff only if the input had not already ended on its
        # own (a replay that reached its end keeps end-of-file rules).
        if not getattr(self.source, "finished", False):
            self._stop_requested = True
        close = getattr(self.source, "close_intake", None)
        if close is None:
            return
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - the stop itself must go on
            log.warning("closing the input's intake failed: %s", exc)

    def _stop_blocking(self, timeout: float) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._stop_source()
        if thread is not None and thread.is_alive():
            # Stopping the source is what frees a read that was blocking.
            thread.join(timeout=0.5)
            if thread.is_alive():
                log.warning("the capture thread has not returned yet; it "
                            "finishes the run itself when it does")
                return False
        self._thread = None
        self._finish_once()
        return True

    def _stop_source(self) -> None:
        """Stop the source, then settle it (a source with a process of its
        own - dsd-neo - is waited for here, on the stopper thread, never the
        caller's). A failure is kept, not swallowed: the source stays this
        capture's to finish, :attr:`settled` stays False, the failure is
        published once, and :meth:`retry_stop_source` tries again - only
        the part that did not succeed."""
        name = getattr(self.source, "name", "source")
        try:
            if not self._source_stop_called:
                self.source.stop()
                self._source_stop_called = True
            settle = getattr(self.source, "settle", None)
            if settle is not None and not settle(5.0):
                raise RuntimeError(f"{name} did not end when asked")
        except Exception as exc:  # noqa: BLE001 - kept, reported, retried
            self.source_error = f"{type(exc).__name__}: {exc}"
            log.warning("stopping %s failed: %s", name, self.source_error)
            if not self._source_failure_reported:
                self._source_failure_reported = True
                self.events.publish("audio-status", {
                    "kind": "source-stop-failed",
                    "message": f"The input did not stop cleanly ({self.source_error}); "
                               f"BabelFishR keeps trying and will not report the run "
                               f"closed until it has."})
            return
        self.source_error = ""
        self._source_stopped = True

    def retry_stop_source(self) -> bool:
        """A source stop that failed is tried again on a fresh stopper thread.
        True when a retry was started; False when nothing is left to do or a
        stop is already under way."""
        if self._source_stopped or self.stopping or self.alive:
            return False
        self._stopper = threading.Thread(
            target=self._stop_source, name="babelfishr-capture-stop-retry", daemon=True)
        self._stopper.start()
        return True

    @property
    def source_stopped(self) -> bool:
        return self._source_stopped

    @property
    def alive(self) -> bool:
        """The audio thread is still running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def stopping(self) -> bool:
        """A stopper thread is still waiting on the audio thread or source."""
        return self._stopper is not None and self._stopper.is_alive()

    @property
    def settled(self) -> bool:
        """The run is over: audio thread and stopper ended, finish done, and
        the source this capture owns actually stopped and settled.

        Only then has the last transmission been handed over (or recorded
        in :attr:`unprocessed`), so only then may the processor it feeds be
        retired; and only then is nothing of the run still running.
        """
        return (self._finished and not self.alive and not self.stopping
                and self._source_stopped)

    def wait_settled(self, timeout: Optional[float] = None) -> bool:
        """Block until settled, or until ``timeout``. For blocking callers."""
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in (self._stopper, self._thread):
            if thread is None or thread is threading.current_thread():
                continue
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(timeout=left)
        return self.settled

    def _finish_once(self) -> None:
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
        self._drain_source()
        # A Stop request (the audio cutoff) keeps all of an open burst; the
        # end of a file keeps the ordinary end-of-stream rules.
        for detected in self.detector.flush(cutoff=self._stop_requested):
            self._capture(detected, tuning=getattr(self, "_block_tuning", None))
        self.safety.close()
        self._set_state(PipelineState.IDLE)

    def run_to_completion(self, timeout: Optional[float] = None) -> int:
        """Synchronous variant used by replay and tests."""
        self.source.start()
        self._running = True
        self._finished = False
        self._source_stopped = False
        self._source_stop_called = False
        self._set_state(PipelineState.LISTENING)
        self._pump(timeout=timeout)
        self._running = False
        self._finish_once()
        self._stop_source()
        return self.transmissions_captured

    # -- the audio loop --------------------------------------------------
    def _run(self) -> None:
        try:
            self._pump()
        except Exception:  # noqa: BLE001
            log.exception("capture thread failed")
            self.events.publish("error", {"stage": "capture",
                                          "message": "capture thread stopped"})
            self._set_state(PipelineState.ERROR)
        if not self._running:
            # A stop was asked for. If stop() is still waiting it does
            # nothing further; if it gave up waiting, this is the finish.
            self._finish_once()

    def _pump(self, timeout: Optional[float] = None) -> None:
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while self._running:
            if deadline is not None and time.monotonic() > deadline:
                break
            block = self.source.read(timeout=0.5)
            if block is None:
                if self.source.finished:
                    break
                continue
            if isinstance(block, AudioBlock):
                self._handle_block(block)
            else:
                self._boundary(block)

    def _drain_source(self) -> None:
        """What the source still holds when the run ends was received before
        the end: audio goes to the detector and every tuning-boundary marker
        is honoured in order, so a transmission open across a retune made
        just before Stop closes under the tuning it was heard on rather than
        under whatever the receiver reports now. Bounded by the source's own
        buffer; a source without a buffer has nothing to drain."""
        read = getattr(self.source, "read", None)
        limit = getattr(self.source, "capacity", 0)
        if read is None or not limit:
            return
        for _ in range(limit + 8):
            try:
                item = read(timeout=0.0)
            except Exception:  # noqa: BLE001 - a closed source has nothing more
                return
            if item is None:
                return
            if isinstance(item, AudioBlock):
                self._handle_block(item)
            else:
                self._boundary(item)

    def _handle_block(self, block: AudioBlock) -> None:
        reading = self.meter.update(block)
        self.spectrum.feed(block.samples, block.sample_rate)
        self._block_count += 1
        if self._block_count % self._level_divisor == 0:
            reading.spectrum = self.spectrum.frame()
            self.events.publish("level", reading, run=self.session.id)
        self.safety.feed(block)

        was_open = self.detector.open
        # The tuning this block was received under travels with it; a
        # recording the detector completes while handling it is labelled
        # with that, never with whatever the receiver reports by now.
        self._block_tuning = getattr(block, "tuning", None)
        for detected in self.detector.push(block):
            self._capture(detected, tuning=self._block_tuning)
        if self.detector.open and not was_open:
            self._set_state(PipelineState.RECEIVING)
        elif was_open and not self.detector.open:
            self._set_state(PipelineState.LISTENING)

    def _boundary(self, boundary) -> None:
        """The source's tuning changed. Whatever the detector holds open was
        received under the tuning that just ended: it is closed now, under
        that tuning's metadata, and the detector starts afresh. Audio the
        source had buffered under the old tuning was dropped by the source
        before this marker arrived, so nothing is labelled with a frequency
        it may not have been received on."""
        was_open = self.detector.open
        for detected in self.detector.flush():
            self._capture(detected, metadata=boundary.metadata)
        self.detector.reset()
        if was_open:
            self._set_state(PipelineState.LISTENING)
        log.info("capture boundary (%s): %s", boundary.reason,
                 "transmission closed" if was_open else "nothing open")

    # -- capture ---------------------------------------------------------
    def _capture(self, detected: DetectedTransmission, metadata=None, tuning=None) -> Transmission:
        """Persist a detected event, then decide what to do with it.

        The order is the invariant: WAV to disk, row to the database, and only
        then any decision about processing.
        """
        tx = Transmission(
            session_id=self.session.id,
            started_at=detected.started_at,
            duration=detected.duration,
            audio_device=self.session.audio_device,
            sample_rate=detected.sample_rate,
            peak_dbfs=round(detected.peak_dbfs, 2),
            noise_floor_dbfs=round(detected.noise_floor_dbfs, 2),
            clipped=detected.clipped,
            detection_confidence=detected.confidence,
            profile_id=self.session.profile_id,
            profile_label=self.session.profile_label,
            channel_name=(self.profile.channel_name if self.profile else ""),
            frequency_mhz=(self.profile.frequency_mhz if self.profile else None),
            # Profile values are operator-declared labels. Marking them as such
            # is what stops a typed frequency being read later as a measurement.
            frequency_provenance=(Provenance.PROFILE if self.profile
                                  and self.profile.frequency_mhz is not None
                                  else Provenance.UNKNOWN),
            channel_provenance=(Provenance.PROFILE if self.profile
                                and self.profile.channel_name
                                else Provenance.UNKNOWN),
            source_language_mode=self.session.source_language_mode,
            source_language=self.session.source_language,
            target_language=self.session.target_language,
            content_class=ContentClass(detected.content_class.value),
            state=ProcessingState.CAPTURED,
        )
        tx.ended_at = detected.ended_at
        if metadata is None and tuning is not None:
            metadata = self._metadata_under(tuning)
        self._apply_measured_metadata(tx, metadata)
        if detected.continued or detected.continues_previous:
            # One stretch of continuous audio, handed on in consecutive parts.
            tx.signal_metadata = dict(tx.signal_metadata or {}, segment={
                "continued": bool(detected.continued),
                "continues_previous": bool(detected.continues_previous)})

        # --- persistence, before any classification-driven decision ---------
        tx.audio_path = self.recorder.write(tx, self.session, detected.audio,
                                            detected.sample_rate)
        run = self.session.id
        stopped = self.pipeline is not None and self.pipeline.run_stopped(run)
        if stopped:
            # Saved after Stop (the final flush): kept, not processed, and
            # not offered to the chat as new automatic traffic.
            mark_processing_stopped(tx, run)
        self.store.save_transmission(tx)
        self.transmissions_captured += 1
        self.events.publish("transmission", tx, run=run)
        if stopped:
            return tx

        # --- now, and only now, decide about automatic processing -----------
        auto = detected.should_auto_transcribe(self.detector.settings)
        if auto and self.pipeline is not None:
            try:
                self.pipeline.submit(tx.id, run=run)
            except RunStopped:
                # Stopped between the check above and here.
                mark_processing_stopped(tx, run)
                self.store.save_transmission(tx)
                self.events.publish("updated", tx, run=run)
            except ProcessingStopped:
                # The recording and the row are already safe. The message
                # stays Captured - honestly pending - and is recorded here so
                # the application can hand it to a processor rather than
                # report the run finished.
                log.warning("processor already stopped; %s stays pending", tx.id)
                self.unprocessed.append(tx.id)
            return tx

        tx.auto_processed = False
        tx.state = ProcessingState.SKIPPED
        tx.skip_reason = _skip_reason(detected)
        self.store.save_transmission(tx)
        self.events.publish("updated", tx, run=run)
        return tx

    def _metadata_under(self, tuning):
        """The source's metadata as it stands, but under the tuning a block
        was received with - for sources that can be asked."""
        source = self.source
        try:
            return source.metadata(tuning=tuning)
        except TypeError:
            return source.metadata() if hasattr(source, "metadata") else None
        except Exception:  # noqa: BLE001 - metadata must never break capture
            log.debug("signal source metadata failed", exc_info=True)
            return None

    def _apply_measured_metadata(self, tx: Transmission, metadata=None) -> None:
        """Overlay what a signal source genuinely reported, if there is one.

        Delegated to :func:`babelfishr.signal_metadata.apply_source_metadata`
        rather than repeated here. The hand-written version this replaced
        copied three fields and hardcoded SDR provenance for all of them, so
        SNR never reached a transmission, the raw record was never kept, and a
        recorded replay's values would have been labelled as measured.

        Metadata is never allowed to cost a recording: any failure is logged
        and capture continues.
        """
        from .signal_metadata import apply_source_metadata

        source = self.source
        if not getattr(source, "measures_rf", False):
            return
        try:
            if metadata is None:
                metadata = source.metadata()
            if metadata is None:
                return
            apply_source_metadata(tx, metadata)
        except Exception:  # noqa: BLE001 - metadata must never break capture
            log.debug("signal source metadata failed", exc_info=True)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            # Tagged: after Stop the window drops the run's activity (the
            # drain and final flush run after it has shown Idle).
            self.events.publish("state", state, run=self.session.id)
