"""Application facade: one object the CLI and the GUI both drive.

Owns the store, the engines, the session lifecycle and the two thread pools, so
neither front-end has to know how they fit together.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
from typing import Any, Dict, List, Optional

from .audio.devices import (AmbiguousInputDevice, AudioDevice, DeviceIdentity,
                            DeviceMatch, InputDeviceMissing, InputNotSelected,
                            backend_status, find_device, list_input_devices,
                            resolve_identity, resolve_input, unique_labels)
from .audio.source import AudioSource, LiveAudioSource, ReplayAudioSource
from .config import Config
from .models import (ProcessingState, RadioProfile, Session, SourceLanguageMode,
                     Transmission, utcnow)
from .pipeline import CaptureService, EventBus, PipelineState, ProcessingPipeline
from .providers import (EngineUnavailable, Glossary, TranscriptionEngine,
                        TranslationEngine, build_transcription_engine,
                        build_translation_engine, is_placeholder)
from .storage import Store

log = logging.getLogger(__name__)


@dataclasses.dataclass
class EngineSummary:
    """What the UI must show the operator before a session starts."""

    mode: Any = None
    transcription: str = "none"
    translation: str = "none"
    transcription_placeholder: bool = False
    translation_placeholder: bool = False
    privacy_notices: List[str] = dataclasses.field(default_factory=list)
    warnings: List[str] = dataclasses.field(default_factory=list)

    @property
    def sends_data_offsite(self) -> bool:
        return bool(self.privacy_notices)

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["sends_data_offsite"] = self.sends_data_offsite
        return d


class BabelFishRApp:
    """Coordinates capture, processing and storage for one monitoring session."""

    def __init__(self, config: Optional[Config] = None,
                 store: Optional[Store] = None):
        self.config = config or Config.load()
        self.store = store or Store(self.config.database,
                                    recordings_dir=self.config.recording.directory)
        self.events = EventBus()
        self.glossary = self._load_glossary()

        self.transcription: Optional[TranscriptionEngine] = None
        self.translation: Optional[TranslationEngine] = None
        self.session: Optional[Session] = None
        self.profile: Optional[RadioProfile] = None
        self.capture: Optional[CaptureService] = None
        self.pipeline: Optional[ProcessingPipeline] = None

    # -- setup -----------------------------------------------------------
    def glossary_path(self) -> pathlib.Path:
        """The one location the glossary is read from and written to.

        Resolved by the configuration, so it lands under Application Support
        with everything else rather than in a legacy ~/.config directory the
        rest of the application does not know about.
        """
        return self.config.glossary_file()

    def _load_glossary(self) -> Glossary:
        path = self.glossary_path()
        return Glossary.load(str(path)) if path.exists() else Glossary()

    def save_glossary(self) -> str:
        return self.glossary.save(str(self.glossary_path()))

    @property
    def mode(self):
        return self.config.operating_mode()

    def set_mode(self, mode, persist: bool = True) -> None:
        """Switch operating mode, persisting it so it survives a restart.

        An in-memory-only switch meant an operator who selected Field Offline
        was silently back in Online/Setup after relaunching - exactly the
        situation where a cloud engine could become selectable again.
        """
        from .modes import OperatingMode

        self.config.mode = OperatingMode(mode).value
        self.transcription = None
        self.translation = None
        if persist:
            try:
                self.config.save()
            except OSError as exc:  # noqa: BLE001 - never fatal
                log.warning("could not persist the operating mode: %s", exc)

    def select_engines(self, strict: bool = False) -> EngineSummary:
        """Resolve the engines and report exactly what the operator is getting."""
        from .modes import OperatingMode

        summary = EngineSummary()
        summary.mode = self.mode

        if self.mode is OperatingMode.RECORD_ONLY:
            self.transcription = None
            self.translation = None
            summary.transcription = "disabled (Record Only)"
            summary.translation = "disabled (Record Only)"
            summary.warnings.append(
                "Record Only mode: transmissions are recorded and stored, but "
                "not transcribed or translated. Recordings can be processed "
                "later once a local model is prepared.")
            return summary

        try:
            self.transcription = build_transcription_engine(self.config)
            summary.transcription = self.transcription.name
            summary.transcription_placeholder = is_placeholder(self.transcription)
            if self.transcription.privacy.is_cloud:
                summary.privacy_notices.append(
                    f"Transcription: {self.transcription.privacy.describe()}")
        except EngineUnavailable as exc:
            self.transcription = None
            summary.warnings.append(f"Transcription unavailable: {exc}")
            if strict:
                raise

        try:
            self.translation = build_translation_engine(self.config)
            summary.translation = self.translation.name
            summary.translation_placeholder = is_placeholder(self.translation)
            if self.translation.privacy.is_cloud:
                summary.privacy_notices.append(
                    f"Translation: {self.translation.privacy.describe()}")
        except EngineUnavailable as exc:
            self.translation = None
            summary.warnings.append(f"Translation unavailable: {exc}")
            if strict:
                raise

        if summary.transcription_placeholder:
            summary.warnings.append(
                "Transcription is using the MOCK engine: the text produced is "
                "placeholder content, not a real transcription. Install the ASR "
                "extra for real transcription.")
        if summary.translation_placeholder:
            summary.warnings.append(
                "Translation is using the MOCK engine: the text produced is "
                "placeholder content, not a real translation.")
        return summary

    # -- profiles --------------------------------------------------------
    def profiles(self) -> List[RadioProfile]:
        return self.store.list_profiles()

    def save_profile(self, profile: RadioProfile) -> RadioProfile:
        return self.store.save_profile(profile)

    def use_profile(self, profile_id: Optional[str]) -> Optional[RadioProfile]:
        self.profile = self.store.get_profile(profile_id) if profile_id else None
        return self.profile

    # -- devices ---------------------------------------------------------
    def devices(self) -> List[AudioDevice]:
        return list_input_devices()

    def audio_backend_status(self) -> str:
        return backend_status()

    def selected_input_identity(self) -> DeviceIdentity:
        """The input the operator chose, as a stable identity."""
        return self.config.selected_input()

    def resolve_selected_input(self) -> Optional[DeviceMatch]:
        """Is the chosen input connected right now?

        ``None`` means it is not. It never means "here is a different device
        that happens to be available".
        """
        return resolve_identity(self.selected_input_identity())

    def input_status(self) -> dict:
        """Everything the window needs to say what it is listening to.

        ``state`` is one of ``none`` (nothing chosen yet), ``system-default``
        (chosen deliberately), ``connected``, ``missing``, or ``ambiguous``.

        ``ambiguous`` is its own state rather than a flag on ``connected``,
        because a flag alongside a device is something a caller can forget to
        read, and the device is right there to be used.
        """
        selection = self.config.audio.input
        identity = self.selected_input_identity()
        if selection.use_system_default and selection.confirmed:
            return {"state": "system-default", "identity": identity,
                    "device": None, "label": "macOS system default input",
                    "expected": "macOS system default input", "candidates": []}
        if not self.config.has_confirmed_input():
            return {"state": "none", "identity": identity, "device": None,
                    "label": "", "expected": "", "candidates": []}

        resolution = resolve_input(identity)
        expected = selection.label or identity.describe()
        if resolution.ambiguous:
            labels = unique_labels(list(resolution.candidates))
            return {"state": "ambiguous", "identity": identity, "device": None,
                    "label": expected, "expected": expected,
                    "candidates": [labels[device.index]
                                   for device in resolution.candidates]}
        if resolution.device is None:
            return {"state": "missing", "identity": identity, "device": None,
                    "label": expected, "expected": expected, "candidates": []}
        return {"state": "connected", "identity": identity,
                "device": resolution.device, "label": resolution.device.name,
                "expected": expected, "basis": resolution.basis,
                "candidates": []}

    # -- session ---------------------------------------------------------
    def start_session(self, source: Optional[AudioSource] = None, *,
                      device: Optional[str] = None,
                      replay_path: Optional[str] = None,
                      realtime_replay: bool = False,
                      name: str = "", profile_id: Optional[str] = None,
                      target_language: Optional[str] = None,
                      source_language: Optional[str] = None,
                      source_language_mode: Optional[str] = None,
                      identity: Optional[DeviceIdentity] = None,
                      workers: int = 1) -> Session:
        """Open a session and begin capturing."""
        if self.capture is not None:
            raise RuntimeError("a session is already running")

        if source is None:
            source = self._build_source(device, replay_path, realtime_replay,
                                        identity)

        if self.transcription is None and self.translation is None:
            self.select_engines()

        profile_id = profile_id or self.config.session.profile_id
        profile = self.use_profile(profile_id)

        mode = source_language_mode or self.config.session.source_language_mode
        session = Session(
            name=name,
            audio_device=getattr(source, "name", "unknown"),
            audio_device_id=str(device) if device is not None else None,
            sample_rate=source.sample_rate,
            profile_id=profile.id if profile else None,
            profile_label=profile.label() if profile else "",
            source_language_mode=SourceLanguageMode(mode),
            source_language=(source_language or self.config.session.source_language
                             or (profile.default_source_language if profile else None)),
            target_language=target_language or self.config.translate.target_language,
            transcription_engine=self.transcription.name if self.transcription else "",
            translation_engine=self.translation.name if self.translation else "",
        )
        self.store.save_session(session)
        self.session = session

        self.pipeline = ProcessingPipeline(
            store=self.store, transcription=self.transcription,
            translation=self.translation, config=self.config, events=self.events,
            glossary=self.glossary, workers=workers)
        self.pipeline.start(session)

        self.capture = CaptureService(
            source=source, store=self.store, session=session, config=self.config,
            events=self.events, pipeline=self.pipeline, profile=profile)
        self.events.publish("session", session)
        return session

    def _build_source(self, device: Optional[str], replay_path: Optional[str],
                      realtime: bool,
                      identity: Optional[DeviceIdentity] = None) -> AudioSource:
        if replay_path:
            return ReplayAudioSource(replay_path, realtime=realtime,
                                     block_size=self.config.audio.block_size)

        selection = self.config.audio.input
        if identity is None or identity.empty:
            if device is not None:
                # An explicit selector from this call or the command line: the
                # operator is naming a device now and can see the result.
                identity = None
            elif selection.use_system_default and selection.confirmed:
                # A deliberate, visibly labelled choice. Not a fallback.
                identity = None
            elif selection.identity:
                identity = self.config.selected_input()
            else:
                raise InputNotSelected(
                    "No audio input has been chosen. Choose the input you want "
                    "to monitor - the built-in microphone for a bench test, or "
                    "your radio interface - before starting. BabelFishR will "
                    "not pick one for you.")

        if identity is not None and not identity.empty:
            # Resolve before a Session row and a pipeline exist. LiveAudioSource
            # would refuse on start() anyway, but by then a session has been
            # opened and has to be unwound; and "monitoring never began" is a
            # clearer thing to tell an operator than "monitoring stopped".
            resolution = resolve_input(identity)
            if resolution.ambiguous:
                raise AmbiguousInputDevice(identity, resolution.candidates)
            if resolution.device is None:
                raise InputDeviceMissing(identity)

        return LiveAudioSource(
            device=device if device is not None else self.config.audio.device,
            identity=identity,
            sample_rate=self.config.audio.sample_rate,
            block_size=self.config.audio.block_size,
            channels=self.config.audio.channels,
            reconnect=self.config.audio.reconnect,
            on_status=lambda kind, message: self.events.publish(
                "audio-status", {"kind": kind, "message": message}),
        )

    def begin_capture(self) -> None:
        """Start the capture thread (asynchronous)."""
        if self.capture is None:
            raise RuntimeError("no session started")
        self.capture.start()

    def run_replay(self, timeout: Optional[float] = None) -> int:
        """Run a replay session synchronously, then wait for processing."""
        if self.capture is None:
            raise RuntimeError("no session started")
        captured = self.capture.run_to_completion(timeout=timeout)
        self.wait_for_processing()
        return captured

    def wait_for_processing(self, timeout: float = 120.0) -> bool:
        """Block until queued *and in-flight* work is finished.

        Waiting on queue depth alone would return while a worker was still
        inside a slow transcription, so this delegates to the pipeline's own
        in-flight accounting.
        """
        if self.pipeline is None:
            return True
        return self.pipeline.wait_until_idle(timeout=timeout)

    def stop_session(self) -> Optional[Session]:
        session = self.session
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        if self.pipeline is not None:
            self.wait_for_processing(timeout=30.0)
            self.pipeline.stop()
            self.pipeline = None
        if session is not None:
            self.store.close_session(session.id)
            session.ended_at = utcnow()
            self.events.publish("session", session)
        self.session = None
        return session

    def resume_pending(self) -> int:
        return self.pipeline.resume_pending() if self.pipeline else 0

    # -- data ------------------------------------------------------------
    def transmissions(self, session_id: Optional[str] = None,
                      limit: int = 500) -> List[Transmission]:
        target = session_id or (self.session.id if self.session else None)
        return self.store.list_transmissions(session_id=target, limit=limit)

    def search(self, query: str = "", **filters) -> List[Transmission]:
        return self.store.search(query, **filters)

    def retry(self, tx_id: str) -> bool:
        if self.pipeline is None:
            return False
        return self.pipeline.retry(tx_id)

    def transcribe_anyway(self, tx_id: str) -> bool:
        """Force transcription of a recording the classifier routed away."""
        if self.pipeline is None:
            return False
        return self.pipeline.force_transcribe(tx_id)

    # -- digital analysis ------------------------------------------------
    def analyser(self):
        """The configured DSD-neo analyser, or None when unavailable."""
        from .analysis import DsdNeoAnalyser

        engine = DsdNeoAnalyser.from_config(self.config)
        return engine if engine.available() else None

    def analyze_digital(self, tx_id: str, protocol: str = "",
                        timeout: Optional[float] = None):
        """Run digital analysis over a recording. Returns the attempt, or None.

        Never raises on a missing tool or a failed decode: the outcome is
        recorded on the transmission and the recording is untouched.
        """
        from .analysis.base import AnalysisRequest
        from .analysis.dsd import DsdNeoAnalyser

        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return None
        engine = DsdNeoAnalyser.from_config(self.config)
        attempt = engine.analyse(AnalysisRequest(
            transmission=tx, protocol=protocol,
            timeout=timeout or self.config.analysis.timeout))
        tx.analysis_attempts.append(attempt)
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        return attempt

    def signal_metadata(self):
        """Measured RF metadata, when a signal source is supplying it."""
        source = getattr(self, "_signal_source", None)
        return source.metadata() if source is not None else None

    def readiness(self, run_smoke_tests: bool = True):
        """Run Field Check against the current configuration."""
        from .readiness import field_check

        return field_check(self.config, run_smoke_tests=run_smoke_tests,
                           mode=self.mode)

    def correct(self, tx_id: str, transcript: Optional[str] = None,
                translation: Optional[str] = None,
                notes: Optional[str] = None) -> Optional[Transmission]:
        """Record an operator correction without touching the originals."""
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return None
        if transcript is not None:
            tx.transcript_correction = transcript
        if translation is not None:
            tx.translation_correction = translation
        if notes is not None:
            tx.notes = notes
        tx.corrected_at = utcnow()
        tx.reviewed = True
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        return tx

    def set_tags(self, tx_id: str, tags: List[str]) -> Optional[Transmission]:
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return None
        tx.tags = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        return tx

    def bookmark(self, tx_id: str, value: bool = True) -> Optional[Transmission]:
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return None
        tx.bookmarked = value
        self.store.save_transmission(tx)
        self.events.publish("updated", tx)
        return tx

    def review_queue(self) -> List[Transmission]:
        return self.store.review_queue()

    def close(self) -> None:
        self.stop_session()
        for engine in (self.transcription, self.translation):
            if engine is not None:
                engine.close()
        self.store.close()
