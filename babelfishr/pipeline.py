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
from .models import (ContentClass, ProcessingState, RadioProfile, Session,
                     SourceLanguageMode, Transmission, utcnow)
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
    """Plain-language note explaining why automatic ASR was not attempted."""
    return {
        "noise": "Classified as broadband noise, so speech recognition was not "
                 "attempted automatically. The recording is kept - use "
                 "'Transcribe anyway' to run it regardless.",
        "tone": "Classified as a steady tone (courtesy beep or unmodulated "
                "carrier). The recording is kept.",
        "digital-suspected": "Possibly a digital burst rather than analogue "
                             "voice. The recording is kept - try "
                             "'Analyze as digital', or 'Transcribe anyway'.",
    }.get(detected.content_class.value,
          "Automatic speech processing was skipped. The recording is kept.")


def _slug(text: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in text.strip())
    return safe.strip("-") or "unnamed"


class ProcessingPipeline:
    """Queued transcription and translation, isolated from audio capture."""

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
        self._session: Optional[Session] = None
        # Work in flight is not in the queue any more but is not done either;
        # counting only the queue would let a caller stop mid-transcription.
        self._active = 0
        self._active_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    # -- lifecycle -------------------------------------------------------
    def start(self, session: Session) -> None:
        self._session = session
        self._running = True
        for index in range(self.worker_count):
            thread = threading.Thread(target=self._work, daemon=True,
                                      name=f"babelfishr-processing-{index}")
            thread.start()
            self._threads.append(thread)

    def stop(self, wait: bool = True, timeout: float = 10.0) -> None:
        self._running = False
        for _ in self._threads:
            self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join(timeout=timeout)
        self._threads = []

    @property
    def pending(self) -> int:
        """Queued work plus work currently being processed."""
        with self._active_lock:
            return self._queue.qsize() + self._active

    def wait_until_idle(self, timeout: float = 120.0) -> bool:
        """Block until nothing is queued or in flight."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pending == 0:
                return True
            self._idle.wait(timeout=0.05)
        return self.pending == 0

    def submit(self, tx_id: str) -> None:
        self._idle.clear()
        self._queue.put(tx_id)

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
        self.submit(tx_id)
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
        self.submit(tx_id)
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
            with self._active_lock:
                self._active += 1
            try:
                self._process(tx_id)
            except Exception:  # noqa: BLE001 - a worker must never die
                log.exception("unhandled error processing %s", tx_id)
            finally:
                with self._active_lock:
                    self._active -= 1
                    if self._active == 0 and self._queue.empty():
                        self._idle.set()

    def _process(self, tx_id: str) -> None:
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            log.warning("transmission %s vanished before processing", tx_id)
            return

        if not self._transcribe(tx):
            return
        self._translate(tx)

        if tx.state is not ProcessingState.FAILED:
            tx.state = ProcessingState.COMPLETE
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        self.events.publish("state", PipelineState.LISTENING)

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
        self.state = PipelineState.IDLE
        self.transmissions_captured = 0
        self._level_divisor = max(1, int(0.1 * source.sample_rate
                                         / max(config.audio.block_size, 1)))
        self._block_count = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self.source.start()
        self._running = True
        self._set_state(PipelineState.LISTENING)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="babelfishr-capture")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        try:
            self.source.stop()
        except Exception:  # noqa: BLE001
            log.debug("error stopping source", exc_info=True)
        for detected in self.detector.flush():
            self._capture(detected)
        self.safety.close()
        self._set_state(PipelineState.IDLE)

    def run_to_completion(self, timeout: Optional[float] = None) -> int:
        """Synchronous variant used by replay and tests."""
        self.source.start()
        self._running = True
        self._set_state(PipelineState.LISTENING)
        self._pump(timeout=timeout)
        for detected in self.detector.flush():
            self._capture(detected)
        self.safety.close()
        self.source.stop()
        self._running = False
        self._set_state(PipelineState.IDLE)
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
            source_language_mode=self.session.source_language_mode,
            source_language=self.session.source_language,
            target_language=self.session.target_language,
            content_class=ContentClass(detected.content_class.value),
            state=ProcessingState.CAPTURED,
        )
        tx.ended_at = detected.ended_at

        # --- persistence, before any classification-driven decision ---------
        tx.audio_path = self.recorder.write(tx, self.session, detected.audio,
                                            detected.sample_rate)
        self.store.save_transmission(tx)
        self.transmissions_captured += 1
        self.events.publish("transmission", tx)

        # --- now, and only now, decide about automatic processing -----------
        auto = detected.should_auto_transcribe(self.detector.settings)
        if auto and self.pipeline is not None:
            self.pipeline.submit(tx.id)
            return tx

        tx.auto_processed = False
        tx.state = ProcessingState.SKIPPED
        tx.skip_reason = _skip_reason(detected)
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        return tx

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.events.publish("state", state)
