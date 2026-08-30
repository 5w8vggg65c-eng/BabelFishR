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


class ModeChangeRefused(RuntimeError):
    """The operating mode was not changed, and nothing else changed either.

    Raised rather than returned so a caller cannot ignore it and leave the
    badge, the combo box and ``config.mode`` disagreeing with the engines that
    are actually loaded.
    """


class ProcessingBusy(RuntimeError):
    """An operation was refused because saved-recording work is in flight."""


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
        # Processing a recording that is already on disk has nothing to do
        # with monitoring. This pipeline exists so "Transcribe anyway" and
        # Retry work after the session has stopped, and after a relaunch.
        self.standalone_pipeline: Optional[ProcessingPipeline] = None
        # The mode each pipeline was built under. An engine chosen in
        # Online/Setup may be cloud-capable; reusing it after a switch to
        # Field Offline would send audio off the machine in the one mode that
        # promises it never will.
        self._standalone_mode = None
        self._session_mode = None
        # The named thread the operator is working in. One capture service and
        # one pipeline exist globally; this only decides which thread a run is
        # filed under and which rows the window shows.
        self._conversation_id: str = ""
        self._capture_conversation_id: str = ""

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

    def mode_change_problem(self) -> str:
        """Why the mode cannot be changed right now, or "" when it can.

        Checked before anything is mutated, so a refusal leaves the
        configuration, the engines and the badge exactly as they were.
        """
        if self.session is not None:
            return ("Monitoring is running. Stop monitoring before changing "
                    "the processing mode - switching underneath a live "
                    "capture would leave the session recording with engines "
                    "chosen for a different mode.")
        pending = (self.standalone_pipeline.pending
                   if self.standalone_pipeline is not None else 0)
        if pending:
            return (f"{pending} saved recording(s) are still being processed. "
                    f"Wait for that to finish, then change the mode - the "
                    f"recordings are safe either way.")
        return ""

    def _retire_processing(self) -> None:
        """Drop every engine and pipeline built under the outgoing mode.

        Only ever called when nothing is in flight, so the stop is immediate.
        """
        if self.standalone_pipeline is not None:
            self.standalone_pipeline.stop(wait=True, timeout=5.0)
            self.standalone_pipeline = None
        self._standalone_mode = None
        for engine in (self.transcription, self.translation):
            if engine is not None:
                try:
                    engine.close()
                except Exception:  # noqa: BLE001 - closing must not block a switch
                    log.debug("engine close failed during mode change",
                              exc_info=True)
        self.transcription = None
        self.translation = None

    def set_mode(self, mode, persist: bool = True) -> None:
        """Switch operating mode, persisting it so it survives a restart.

        An in-memory-only switch meant an operator who selected Field Offline
        was silently back in Online/Setup after relaunching - exactly the
        situation where a cloud engine could become selectable again.

        Two things happen in this order, and the order is the point. The
        refusal is checked first, so an unsafe switch changes nothing at all.
        Then every engine and pipeline built under the outgoing mode is
        retired *before* ``config.mode`` moves, so there is no instant at
        which a cloud-capable processor is reachable while the mode says
        Field Offline.
        """
        from .modes import OperatingMode

        target = OperatingMode(mode)
        if target is self.mode:
            return

        problem = self.mode_change_problem()
        if problem:
            raise ModeChangeRefused(problem)

        self._retire_processing()
        self.config.mode = target.value
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

        # Before anything is written: a busy standalone processor refuses the
        # start outright rather than being waited on.
        self._discard_standalone_pipeline()

        if self.transcription is None and self.translation is None:
            self.select_engines()

        # The thread selected right now is this run's destination, and it is
        # captured here rather than read later: switching tabs mid-watch must
        # not move traffic that is already arriving.
        self._capture_conversation_id = self.conversation_id

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
            conversation_id=self._capture_conversation_id,
        )
        self.store.save_session(session)
        self.session = session

        self.pipeline = ProcessingPipeline(
            store=self.store, transcription=self.transcription,
            translation=self.translation, config=self.config, events=self.events,
            glossary=self.glossary, workers=workers)
        self.pipeline.start(session)
        self._session_mode = self.mode

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

    def _discard_standalone_pipeline(self) -> None:
        """Retire an idle standalone pipeline, or refuse immediately.

        The first version waited up to thirty seconds for in-flight work,
        which froze the window when the operator pressed Start Monitoring
        while a saved recording was being transcribed. Waiting is not this
        method's decision to make: it either retires an idle processor at
        once, or refuses and says why.
        """
        if self.standalone_pipeline is None:
            return
        pending = self.standalone_pipeline.pending
        if pending:
            raise ProcessingBusy(
                f"{pending} saved recording(s) are still being transcribed. "
                f"Wait for that to finish, then start monitoring - nothing is "
                f"lost either way.")
        self.standalone_pipeline.stop(wait=True, timeout=5.0)
        self.standalone_pipeline = None
        self._standalone_mode = None

    def stop_session(self) -> Optional[Session]:
        session = self.session
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        if self.pipeline is not None:
            self.wait_for_processing(timeout=30.0)
            self.pipeline.stop()
            self.pipeline = None
        self._session_mode = None
        if session is not None:
            self.store.close_session(session.id)
            session.ended_at = utcnow()
            self.events.publish("session", session)
        self.session = None
        return session

    def resume_pending(self) -> int:
        return self.pipeline.resume_pending() if self.pipeline else 0

    # -- processing recordings that are already on disk -------------------
    def processing_problem(self, tx_id: str) -> str:
        """Why a saved recording cannot be processed, or "" when it can.

        A precise sentence naming the real obstacle. It is never "start
        monitoring first": a WAV on disk does not need a microphone.
        """
        from .modes import OperatingMode

        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return "That transmission is no longer in the database."
        if not tx.audio_path:
            return "No audio file was recorded for this transmission."
        if not pathlib.Path(tx.audio_path).exists():
            return (f"The recording file is missing:\n{tx.audio_path}\n\n"
                    f"It may have been moved or deleted outside BabelFishR.")
        if self.mode is OperatingMode.RECORD_ONLY:
            return ("Record Only mode has transcription switched off. Change "
                    "the operating mode, then try again - the recording is "
                    "kept either way.")
        if (self.pipeline is not None
                and self._session_mode is not self.mode):
            return ("Monitoring is running with engines chosen for a "
                    "different operating mode. Stop monitoring, then try "
                    "again.")
        if self._processing_pipeline() is None:
            summary = self.select_engines()
            detail = "; ".join(summary.warnings) or "no transcription engine"
            return (f"No transcription engine is available in "
                    f"{self.mode.label}: {detail}")
        return ""

    def _processing_pipeline(self) -> Optional[ProcessingPipeline]:
        """The live pipeline when monitoring, otherwise a standalone one.

        Deliberately not a fake capture session: no Session row is created, no
        audio device is opened, and nothing about the operator's monitoring
        state changes. It publishes on the same event bus, so a bubble updates
        exactly as it does during a live session, and it runs on its own
        worker thread so the window never freezes.
        """
        if self.pipeline is not None:
            # A live pipeline cannot outlive its mode through set_mode, which
            # refuses while monitoring. This covers the other route in: a
            # direct write to config.mode. Nothing is processed rather than
            # processed by engines the current mode forbids.
            return self.pipeline if self._session_mode is self.mode else None
        if self.standalone_pipeline is not None:
            if self._standalone_mode is self.mode:
                return self.standalone_pipeline
            # Defence in depth. set_mode retires this already; reaching here
            # means the mode moved some other way, and the cached processor
            # belongs to the old one.
            if self.standalone_pipeline.pending:
                return None
            self._retire_processing()

        self.select_engines()          # honours the current operating mode
        if self.transcription is None:
            return None
        pipeline = ProcessingPipeline(
            store=self.store, transcription=self.transcription,
            translation=self.translation, config=self.config,
            events=self.events, glossary=self.glossary)
        pipeline.start(None)
        self.standalone_pipeline = pipeline
        self._standalone_mode = self.mode
        return pipeline

    # -- data ------------------------------------------------------------
    def transmissions(self, session_id: Optional[str] = None,
                      limit: int = 500) -> List[Transmission]:
        target = session_id or (self.session.id if self.session else None)
        return self.store.list_transmissions(session_id=target, limit=limit)

    # -- named Session threads (Conversations) ---------------------------
    def conversations(self):
        return self.store.list_conversations()

    @property
    def conversation_id(self) -> str:
        """The thread the operator is viewing. Defaults to General."""
        if not self._conversation_id:
            self._conversation_id = self.store.default_conversation().id
        return self._conversation_id

    def select_conversation(self, conversation_id: str) -> str:
        """Change the *viewed* thread.

        Deliberately does not touch capture. An operator reviewing an older
        thread while a watch is running must not have their incoming traffic
        silently refiled - the destination was fixed when monitoring started.
        """
        if self.store.get_conversation(conversation_id) is not None:
            self._conversation_id = conversation_id
            self.config.session.conversation_id = conversation_id
        return self.conversation_id

    def create_conversation(self, name: str):
        conversation = self.store.create_conversation(name)
        return conversation

    def rename_conversation(self, conversation_id: str, name: str):
        return self.store.rename_conversation(conversation_id, name)

    @property
    def capture_conversation_id(self) -> str:
        """Where the *running* capture files its transmissions.

        Fixed at Start Monitoring and untouched by tab switching, so an
        operator who wanders off to read history cannot misfile live traffic.
        """
        return self._capture_conversation_id

    def restore_selected_conversation(self) -> str:
        """Re-select the thread the operator last had open, if it still exists."""
        saved = getattr(self.config.session, "conversation_id", "") or ""
        if saved and self.store.get_conversation(saved) is not None:
            self._conversation_id = saved
        return self.conversation_id

    #: How much of the thread the window restores on open. Bounded so a
    #: long-running installation does not build thousands of widgets at
    #: startup, and large enough that a day's traffic is all there.
    HISTORY_LIMIT = 500

    def recent_transmissions(self, limit: Optional[int] = None, *,
                             conversation_id: Optional[str] = None,
                             newest_first: bool = False) -> List[Transmission]:
        """The message thread for one named Session.

        Across monitoring runs, deliberately. Stopping and restarting
        monitoring is not the end of the operator's log; it is a pause in one
        continuous radio watch, and every run filed under this thread belongs
        to it.
        """
        limit = self.HISTORY_LIMIT if limit is None else limit
        return self.store.conversation_transmissions(
            conversation_id or self.conversation_id, limit=limit,
            newest_first=newest_first)

    def search(self, query: str = "", **filters) -> List[Transmission]:
        return self.store.search(query, **filters)

    def retry(self, tx_id: str) -> bool:
        """Retry a failed recording - monitoring or not."""
        pipeline = self._processing_pipeline()
        return pipeline.retry(tx_id) if pipeline is not None else False

    def transcribe_anyway(self, tx_id: str) -> bool:
        """Force transcription of a recording the classifier routed away.

        The WAV is already on disk, so this needs no live session: it works
        after monitoring stops and after the application has been quit and
        reopened. The saved transmission carries its own session metadata,
        source-language mode and target language, so the result is the same as
        it would have been at capture time.
        """
        pipeline = self._processing_pipeline()
        return pipeline.force_transcribe(tx_id) if pipeline is not None else False

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
        from .signal_metadata import apply_decoded_metadata

        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return None
        engine = DsdNeoAnalyser.from_config(self.config)
        attempt = engine.analyse(AnalysisRequest(
            transmission=tx, protocol=protocol,
            timeout=timeout or self.config.analysis.timeout))
        tx.analysis_attempts.append(attempt)
        apply_decoded_metadata(tx, attempt)
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
        if self.standalone_pipeline is not None:
            self.standalone_pipeline.wait_until_idle(timeout=30.0)
            self.standalone_pipeline.stop()
            self.standalone_pipeline = None
            self._standalone_mode = None
        for engine in (self.transcription, self.translation):
            if engine is not None:
                engine.close()
        self.store.close()
