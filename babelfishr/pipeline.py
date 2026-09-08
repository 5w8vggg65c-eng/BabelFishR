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
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .audio.meter import LevelMeter, LevelReading
from .audio.safety import SafetyRecorder
from .audio.source import AudioBlock, AudioSource
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

    def publish(self, kind: str, payload: Any = None) -> None:
        event = Event(kind=kind, payload=payload)
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


def _slug(text: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in text.strip())
    return safe.strip("-") or "unnamed"


class ProcessingStopped(RuntimeError):
    """Work was offered to a processor that has already begun shutting down."""


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
            thread = threading.Thread(target=self._work, daemon=True,
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

    def stop_if_idle(self, timeout: float = 5.0) -> bool:
        """Stop only if nothing accepted is unfinished; atomic with admission.

        The idle check and the closing of admission happen under the same
        lock, so a submission cannot land between "nothing pending" and
        "stopped". Returns False having changed nothing when work is
        accounted for, and False with admission closed when a worker outlived
        the join (see :attr:`finished`); the caller tells the two apart by
        :attr:`pending`.
        """
        with self._active_lock:
            if self._in_flight:
                return False
            self._accepting = False
        return self.stop(wait=True, timeout=timeout)

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

    def submit(self, tx_id: str) -> bool:
        """Accept one id. Returns False if it is already in flight.

        An id already accepted is not queued twice: one run is what was asked
        for, and a second queue entry would let ``pending`` reach zero while
        that entry still waited. Once a stop has begun this refuses, by
        exception, rather than dropping the id or queueing it behind a
        worker that will never take it - the caller learns the processor is
        retiring and can say so.
        """
        with self._active_lock:
            if not self._accepting:
                raise ProcessingStopped(
                    "this processor is shutting down and takes no new work")
            if tx_id in self._in_flight:
                return False
            self._in_flight.add(tx_id)
            self._idle.clear()
        self._queue.put(tx_id)
        return True

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
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        try:
            self.submit(tx_id)
        except ProcessingStopped:
            # Saved as Captured - honestly pending - but this processor is
            # retiring and will not run it. The caller reports "not now".
            return False
        return True

    def retry(self, tx_id: str) -> bool:
        """Retry a failed transmission. The audio was never lost."""
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return False
        tx.clear_error()
        tx.state = ProcessingState.CAPTURED
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        try:
            self.submit(tx_id)
        except ProcessingStopped:
            return False
        return True

    # -- worker ----------------------------------------------------------
    def _work(self) -> None:
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
            try:
                self._process(tx_id)
            except Exception:  # noqa: BLE001 - a worker must never die
                log.exception("unhandled error processing %s", tx_id)
            finally:
                with self._active_lock:
                    self._in_flight.discard(tx_id)
                    if not self._in_flight:
                        self._idle.set()

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

        try:
            if not self._transcribe(tx):
                return
            self._translate(tx)

            if tx.state is not ProcessingState.FAILED:
                tx.state = ProcessingState.COMPLETE
            self.store.save_transmission(tx)
            self.events.publish("updated", tx)
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
            self.events.publish("state", PipelineState.COMPLETE)

    def _load_audio(self, tx: Transmission):
        from .audio.wavefile import read_wav

        if not tx.audio_path or not pathlib.Path(tx.audio_path).exists():
            raise EngineError("the recorded audio file is missing")
        return read_wav(tx.audio_path)

    def _transcribe(self, tx: Transmission) -> bool:
        if self.transcription is None:
            tx.state = ProcessingState.SKIPPED
            self.store.save_transmission(tx)
            self.events.publish("updated", tx)
            return False
        if tx.transcript and tx.state is not ProcessingState.CAPTURED:
            return True  # already done on an earlier attempt

        tx.state = ProcessingState.TRANSCRIBING
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        self.events.publish("state", PipelineState.TRANSCRIBING)

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
            tx.fail("transcription", str(exc))
            self.store.save_transmission(tx)
            self.events.publish("updated", tx)
            self.events.publish("error", {"transmission": tx.id, "stage": "transcription",
                                          "message": str(exc)})
            return False

        tx.transcript = result.text
        tx.transcript_confidence = result.confidence
        tx.transcript_segments = list(result.segments)
        tx.transcription_engine = result.engine
        tx.transcription_engine_version = result.engine_version
        if result.language:
            tx.source_language = result.language
            tx.language_confidence = result.language_confidence
        tx.state = ProcessingState.TRANSCRIBED
        tx.clear_error()
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)

        if not result.text.strip():
            tx.state = ProcessingState.COMPLETE
            self.store.save_transmission(tx)
            self.events.publish("updated", tx)
            return False
        return True

    def _translate(self, tx: Transmission) -> None:
        if self.translation is None:
            return
        target = tx.target_language or self.config.translate.target_language
        if (self.config.translate.skip_if_same_language
                and tx.source_language and tx.source_language == target):
            tx.translation = ""
            tx.translation_engine = ""
            return

        tx.state = ProcessingState.TRANSLATING
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        self.events.publish("state", PipelineState.TRANSLATING)

        try:
            self._engine_gate("translation")
            result = self.translation.translate(
                tx.transcript, target, source_language=tx.source_language,
                glossary=self.glossary.mapping(tx.source_language) or None,
                do_not_translate=self.glossary.protected(tx.source_language) or None)
        except (EngineError, EngineUnavailable) as exc:
            # The transcript survives: only the translation stage failed.
            tx.fail("translation", str(exc))
            self.store.save_transmission(tx)
            self.events.publish("updated", tx)
            self.events.publish("error", {"transmission": tx.id, "stage": "translation",
                                          "message": str(exc)})
            return

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
        #: Transmissions saved here that no processor accepted. Nothing is
        #: lost - the WAV and the row exist - but they are pending, and the
        #: application must not report them finished.
        self.unprocessed: List[str] = []
        self.state = PipelineState.IDLE
        self.transmissions_captured = 0
        self._level_divisor = max(1, int(0.1 * source.sample_rate
                                         / max(config.audio.block_size, 1)))
        self._block_count = 0

    #: How long stop() waits for the audio thread before reporting that it
    #: is still running. A bound on the caller's wait, not on the thread.
    stop_timeout = 5.0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self.source.start()
        self._running = True
        self._finished = False
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
        self._running = False
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._thread = None
            self._stop_source()
            self._finish_once()
            return
        if self._stopper is not None and self._stopper.is_alive():
            return                     # a stop is already under way
        self._stopper = threading.Thread(
            target=self._stop_blocking, args=(self.stop_timeout,),
            name="babelfishr-capture-stop", daemon=True)
        self._stopper.start()

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
        try:
            self.source.stop()
        except Exception:  # noqa: BLE001
            log.debug("error stopping source", exc_info=True)

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
        """The run is over: audio thread and stopper ended, finish done.

        Only then has the last transmission been handed over (or recorded
        in :attr:`unprocessed`), so only then may the processor it feeds be
        retired.
        """
        return self._finished and not self.alive and not self.stopping

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
        for detected in self.detector.flush():
            self._capture(detected)
        self.safety.close()
        self._set_state(PipelineState.IDLE)

    def run_to_completion(self, timeout: Optional[float] = None) -> int:
        """Synchronous variant used by replay and tests."""
        self.source.start()
        self._running = True
        self._finished = False
        self._set_state(PipelineState.LISTENING)
        self._pump(timeout=timeout)
        self._running = False
        self._finish_once()
        self.source.stop()
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
            self._handle_block(block)

    def _handle_block(self, block: AudioBlock) -> None:
        reading = self.meter.update(block)
        self._block_count += 1
        if self._block_count % self._level_divisor == 0:
            self.events.publish("level", reading)
        self.safety.feed(block)

        was_open = self.detector.open
        for detected in self.detector.push(block):
            self._capture(detected)
        if self.detector.open and not was_open:
            self._set_state(PipelineState.RECEIVING)
        elif was_open and not self.detector.open:
            self._set_state(PipelineState.LISTENING)

    # -- capture ---------------------------------------------------------
    def _capture(self, detected: DetectedTransmission) -> Transmission:
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
        self._apply_measured_metadata(tx)

        # --- persistence, before any classification-driven decision ---------
        tx.audio_path = self.recorder.write(tx, self.session, detected.audio,
                                            detected.sample_rate)
        self.store.save_transmission(tx)
        self.transmissions_captured += 1
        self.events.publish("transmission", tx)

        # --- now, and only now, decide about automatic processing -----------
        auto = detected.should_auto_transcribe(self.detector.settings)
        if auto and self.pipeline is not None:
            try:
                self.pipeline.submit(tx.id)
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
        self.events.publish("updated", tx)
        return tx

    def _apply_measured_metadata(self, tx: Transmission) -> None:
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
            metadata = source.metadata()
            if metadata is None:
                return
            apply_source_metadata(tx, metadata)
        except Exception:  # noqa: BLE001 - metadata must never break capture
            log.debug("signal source metadata failed", exc_info=True)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.events.publish("state", state)
