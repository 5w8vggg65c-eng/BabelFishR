"""Application facade: one object the CLI and the GUI both drive.

Owns the store, the engines, the session lifecycle and the two thread pools, so
neither front-end has to know how they fit together.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import pathlib
import threading
from typing import Any, Dict, List, Optional

from .audio.devices import (AmbiguousInputDevice, AudioDevice, DeviceIdentity,
                            DeviceMatch, InputDeviceMissing, InputNotSelected,
                            backend_status, find_device, list_input_devices,
                            resolve_identity, resolve_input, unique_labels)
from .audio.source import AudioSource, LiveAudioSource, ReplayAudioSource
from .config import Config
from .models import (ProcessingState, RadioProfile, Session, SourceLanguageMode,
                     Transmission, utcnow)
from .pipeline import (CaptureService, EventBus, PipelineState,
                       ProcessingPipeline, ProcessingStopped)
from .providers import (EngineUnavailable, Glossary, TranscriptionEngine,
                        TranslationEngine, build_transcription_engine,
                        build_translation_engine, is_placeholder)
from .storage import Store

log = logging.getLogger(__name__)


#: Sentinel: "the caller said nothing", which is not "the caller said all".
_UNSCOPED = object()


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


class _FinalCleanup:
    """Runs the last step of shutdown on a thread of its own, once at a time.

    ``started`` is set the moment the first attempt begins and never cleared:
    from then on the store may close at any instant, so the owner must stop
    reading it before starting this. A failure is recorded, not hidden, and a
    later ``start()`` retries; a call while an attempt is running does
    nothing, so repeated Quit requests cannot overlap.
    """

    def __init__(self, run):
        self._run = run
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.started = False
        self.done = False
        self.error: Optional[BaseException] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running or self.done:
                return
            self.started = True
            self.error = None
            self._thread = threading.Thread(target=self._attempt, daemon=True,
                                            name="babelfishr-cleanup")
            self._thread.start()

    def _attempt(self) -> None:
        try:
            self._run()
            self.done = True
        except Exception as exc:  # noqa: BLE001 - recorded, reported, retried
            log.exception("final cleanup failed")
            self.error = exc

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)


class _SessionEnd:
    """Records the end of one run off the caller's thread, once, tracked.

    The Session identity and the ending time are captured at Stop, so a
    write that lands later cannot close the wrong run or stamp the wrong
    moment. A failure is recorded, not hidden, and ``start()`` retries; a
    call while an attempt is running does nothing.
    """

    def __init__(self, store: Store, session_id: str, ended_at):
        self.store = store
        self.session_id = session_id
        self.ended_at = ended_at
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.done = False
        self.error: Optional[BaseException] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running or self.done:
                return
            self.error = None
            self._thread = threading.Thread(target=self._attempt, daemon=True,
                                            name="babelfishr-session-end")
            self._thread.start()

    def _attempt(self) -> None:
        try:
            self.store.close_session(self.session_id, ended_at=self.ended_at)
            self.done = True
        except Exception as exc:  # noqa: BLE001 - recorded, reported, retried
            log.exception("recording the end of run %s failed", self.session_id)
            self.error = exc

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)


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
        #: Processors whose workers outlived a stop. Kept, not forgotten: while
        #: one is here nothing it can still reach (its engines, the store) is
        #: closed under it, no mode changes, and nothing new starts alongside
        #: it. Reaped once its threads have actually ended.
        self._retired: List[ProcessingPipeline] = []
        #: A capture whose audio thread outlived its stop, for the same reason.
        self._lingering_capture: Optional[CaptureService] = None
        self._closed = False
        #: Set by close(): Quit is pending. Nothing new starts - no session,
        #: no saved-recording processing, no analysis - while accepted work
        #: and the final capture hand-off finish.
        self._closing = False
        self._close_in_progress = False
        #: Other users of the store that shutdown must wait for, by name: a
        #: digital analysis that will save its result, say. See activity().
        self._activities: Dict[str, int] = {}
        self._activity_lock = threading.Lock()
        #: Transmissions a capture saved that no processor accepted.
        self._unprocessed: List[str] = []
        #: The last step of shutdown - engines, then the store - runs off
        #: the caller's thread, once, with its outcome recorded. See close().
        self._final = _FinalCleanup(self._close_resources)
        self._store_closed = False
        #: Set when every user of the store has finished and only the final
        #: cleanup remains. The window shuts its own routes to the store
        #: before it asks for that cleanup to begin.
        self._ready_for_cleanup = False
        #: The end-of-run writes still in flight, oldest first.
        self._session_ends: List[_SessionEnd] = []
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
        return self.shutdown_problem()

    # -- shutdown bookkeeping ----------------------------------------------
    def _reap(self) -> None:
        """Forget stopped processors and captures whose threads have ended."""
        self._retired = [p for p in self._retired if not p.finished]
        if (self.standalone_pipeline is not None
                and not self.standalone_pipeline.accepting
                and self.standalone_pipeline.finished):
            self.standalone_pipeline = None
            self._standalone_mode = None
        if (self._lingering_capture is not None
                and self._lingering_capture.settled):
            self._unprocessed.extend(self._lingering_capture.unprocessed)
            self._lingering_capture.unprocessed.clear()
            self._lingering_capture = None

    @property
    def closing(self) -> bool:
        """Quit is pending: accepted work finishes, nothing new starts."""
        return self._closing

    @contextlib.contextmanager
    def activity(self, name: str):
        """Register a user of the store that shutdown must wait for.

        The digital analysis runs on a worker thread and saves its result at
        the end; without this, close() could not see it and closed the
        database under it. Refused once Quit is pending, so that nothing
        new can begin using resources that are about to close.
        """
        with self._activity_lock:
            if self._closing:
                raise ProcessingBusy(
                    "BabelFishR is quitting; nothing new is started.")
            self._activities[name] = self._activities.get(name, 0) + 1
        try:
            yield
        finally:
            with self._activity_lock:
                self._activities[name] -= 1
                if self._activities[name] <= 0:
                    del self._activities[name]

    def active_operations(self) -> Dict[str, int]:
        with self._activity_lock:
            return dict(self._activities)

    def capture_finishing(self) -> bool:
        """A stopped capture has not settled: it may still hand over a job."""
        self._reap()
        return self._lingering_capture is not None

    def _track_retired(self, pipeline: ProcessingPipeline) -> None:
        if pipeline not in self._retired:
            self._retired.append(pipeline)

    def shutdown_problem(self) -> str:
        """Why nothing may start or change yet: a previous run is still leaving.

        A worker that outlived its stop timeout is not proof that its work
        finished, and a capture thread that has not returned still holds the
        detector. Until they have actually ended, the engines and the store
        they can reach stay open, the mode stays put, and monitoring or a new
        processor does not start beside them.
        """
        self._reap()
        stuck = [p for p in self._retired if p.pending]
        if self.standalone_pipeline is not None and self.standalone_pipeline.shutting_down:
            stuck.append(self.standalone_pipeline)
        if stuck:
            # A worker that has not returned *with work still accounted to
            # it* can still reach its engines. One that has finished its work
            # and is merely leaving cannot: it is tracked until it has gone
            # (close() waits for it), but it blocks nothing new.
            return ("A processor from the previous run is still shutting "
                    "down. Wait a moment, then try again - nothing is lost "
                    "either way.")
        if self._lingering_capture is not None:
            return ("The previous run has not released the audio input yet. "
                    "Wait a moment, then try again.")
        return ""

    def outstanding_work(self) -> int:
        """Unfinished jobs, plus anything that can still become one.

        Counts every processor's pending work, transmissions a capture saved
        that no processor accepted, and - as one - a stopped capture that has
        not settled, since its final flush may still produce a transmission.
        Zero therefore means nothing can still be waiting to be processed.
        """
        self._reap()
        total = len(self._unprocessed)
        for pipeline in (self.pipeline, self.standalone_pipeline, *self._retired):
            if pipeline is not None:
                total += pipeline.pending
        if self._lingering_capture is not None:
            total += 1 + len(self._lingering_capture.unprocessed)
        return total

    def _adopt_unprocessed(self) -> None:
        """Hand a processor whatever a capture saved but nothing accepted.

        Only reached with a settled capture. If no processor can be built
        (no engine in this mode) the ids stay recorded and are logged, never
        silently dropped and never reported as processed.
        """
        if not self._unprocessed:
            return
        pipeline = self._processing_pipeline(for_shutdown=True)
        if pipeline is None:
            log.warning("no processor for %d unprocessed transmission(s): %s",
                        len(self._unprocessed), ", ".join(self._unprocessed))
            return
        for tx_id in list(self._unprocessed):
            try:
                pipeline.submit(tx_id)
            except ProcessingStopped:
                break
            self._unprocessed.remove(tx_id)

    def _retire_processing(self) -> None:
        """Drop every engine and pipeline built under the outgoing mode.

        The idle check and the stop are one atomic step
        (:meth:`ProcessingPipeline.stop_if_idle`), so a job cannot slip in
        between them. Anything still accounted for, or a worker that has not
        returned, refuses the retirement - and with it the mode change - so
        the engines below are only ever closed when nothing can still call
        them.
        """
        if self.standalone_pipeline is not None:
            pipeline = self.standalone_pipeline
            if not pipeline.stop_if_idle(timeout=5.0):
                pending = pipeline.pending
                if pending:
                    raise ModeChangeRefused(
                        f"{pending} saved recording(s) are still being "
                        f"processed. Wait for that to finish, then change the "
                        f"mode - the recordings are safe either way.")
                # Idle and no longer accepting, but a worker has not returned.
                # It keeps its engines until it has; see shutdown_problem.
                raise ModeChangeRefused(
                    "A processor is still shutting down. Wait a moment, then "
                    "change the mode.")
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

        # Before anything is written: a pending Quit, a previous run still
        # shutting down, or a busy standalone processor refuses the start
        # outright rather than being waited on or started alongside.
        if self._closing:
            raise ProcessingBusy("BabelFishR is quitting; monitoring cannot "
                                 "start now.")
        problem = self.shutdown_problem()
        if problem:
            raise ProcessingBusy(problem)
        self._discard_standalone_pipeline()

        if self.transcription is None and self.translation is None:
            self.select_engines()

        # The thread selected right now is this run's destination, and it is
        # captured here rather than read later: switching tabs mid-watch must
        # not move traffic that is already arriving.
        self._capture_conversation_id = self.conversation_id
        try:
            return self._open_session(
                source, device, name, profile_id, target_language,
                source_language, source_language_mode, workers)
        except Exception as exc:
            self._abandon_failed_start(exc)
            raise

    def _abandon_failed_start(self, exc: BaseException) -> None:
        """Undo a start that did not finish, without hiding why it failed.

        Every step here is in a guard of its own - genuinely its own, one
        try block per operation. A cleanup that raises would
        replace the operator's real error - "the interface is not connected" -
        with whatever went wrong while tidying up, which is the less useful of
        the two and not the one they can act on.

        The row is the subtle part. ``_open_session`` writes the Session
        before it starts the workers, so a failure after that point leaves a
        run in the database with ``ended_at`` NULL, and by then ``self.session``
        has been cleared - so the caller's later ``stop_session()`` has nothing
        to close it with, and it stays open forever. A monitoring run that
        never began must not read as one still in progress.
        """
        session = self.session
        self._capture_conversation_id = ""
        self._session_mode = None
        self.session = None
        self.capture = None

        if self.pipeline is not None:
            pipeline, self.pipeline = self.pipeline, None
            try:
                # Workers may already be running: stop them rather than leave
                # threads behind on a session that does not exist. One that
                # does not return in time is kept in view, not forgotten.
                if not pipeline.stop(wait=True, timeout=5.0):
                    self._track_retired(pipeline)
            except Exception:  # noqa: BLE001 - never mask the real failure
                log.debug("failed-start pipeline cleanup failed", exc_info=True)

        if session is None:
            return

        # Closed, not deleted. An implementation choice, not a stated
        # requirement: the row records that a start was attempted and failed,
        # which is worth keeping, and deleting rows on an error path is how
        # audit history and - if the model ever changes - real data get lost.
        # It holds no transmissions, so it is invisible in the thread.
        #
        # Two independent operations, closing first. An earlier version put
        # both in one try block and claimed they were independently guarded:
        # they were not, so a failure writing the explanatory note skipped
        # close_session() entirely and left the run open with nothing able to
        # close it. The note is a nicety; the open row is the defect.
        session.ended_at = utcnow()
        try:
            self.store.close_session(session.id, ended_at=session.ended_at)
        except Exception:  # noqa: BLE001 - never mask the real failure
            log.debug("failed-start close_session failed", exc_info=True)

        try:
            session.notes = (
                f"Monitoring failed to start: "
                f"{type(exc).__name__}: {exc}".strip())[:500]
            # save_session writes ended_at too, so this is also the fallback
            # if close_session above did not get through.
            self.store.save_session(session)
        except Exception:  # noqa: BLE001 - never mask the real failure
            log.debug("failed-start note could not be saved", exc_info=True)

    def _open_session(self, source, device, name, profile_id, target_language,
                      source_language, source_language_mode,
                      workers: int) -> Session:
        """The rest of the start, after the destination has been pinned."""
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
        self._reap()
        if self.standalone_pipeline is None:
            return
        pipeline = self.standalone_pipeline
        if not pipeline.stop_if_idle(timeout=5.0):
            pending = pipeline.pending
            if pending:
                raise ProcessingBusy(
                    f"{pending} saved recording(s) are still being transcribed. "
                    f"Wait for that to finish, then start monitoring - nothing "
                    f"is lost either way.")
            raise ProcessingBusy(
                "The previous processor is still shutting down. Wait a "
                "moment, then start monitoring.")
        self.standalone_pipeline = None
        self._standalone_mode = None

    def stop_session(self) -> Optional[Session]:
        """Stop capture and forget where it was going.

        The destination is pinned for the life of a run so switching tabs
        cannot redirect live traffic. When the run ends there is no live
        traffic, and leaving it set made the window keep announcing
        "Recording into ..." after monitoring had stopped.
        """
        session = self.session
        if self.capture is not None:
            capture, self.capture = self.capture, None
            # Nothing here waits on audio. The join, the source close and the
            # final flush run on the capture's own stopper thread (or at once,
            # when the audio thread has already returned). Until it has
            # settled the capture stays in view as a producer that can still
            # hand over its last transmission; the processor below stays
            # accepting for exactly that reason.
            capture.stop_async()
            if capture.settled:
                self._unprocessed.extend(capture.unprocessed)
                capture.unprocessed.clear()
            else:
                self._lingering_capture = capture
        if self.pipeline is not None:
            pipeline, self.pipeline = self.pipeline, None
            # Nothing is waited for on the caller's thread - this runs on the
            # window's - and nothing accepted is abandoned. An idle processor
            # stops at once; one with work still outstanding, or one a
            # lingering capture may still hand a final transmission to,
            # carries on as the standalone processor: still accounted for by
            # every check, still where Retry and Transcribe anyway go, and
            # stopped only once it is idle. The 30-second wait this replaces
            # froze the window and, when it ran out, forgot the worker.
            if (self._lingering_capture is None
                    and pipeline.stop_if_idle(wait=False)):
                # Idle: admission is closed and the workers have been told to
                # leave. Nothing is joined here - an earlier version joined
                # for up to 5 s on this thread. The worker ends on its own;
                # it stays tracked until it has, and close() waits for it.
                if not pipeline.finished:
                    self._track_retired(pipeline)
            else:
                self.standalone_pipeline = pipeline
                self._standalone_mode = self._session_mode
        self._session_mode = None
        self._capture_conversation_id = ""
        if session is not None:
            # The end is recorded off this thread. The write takes the store
            # lock, which a worker saving a transcript may be holding; an
            # earlier version waited for it here, on the window's thread.
            # Identity and time are fixed now; the write is tracked until
            # it lands, and close() waits for it before the store closes.
            session.ended_at = utcnow()
            end = _SessionEnd(self.store, session.id, session.ended_at)
            self._session_ends.append(end)
            end.start()
            self.events.publish("session", session)
        self.session = None
        return session

    # -- the end-of-run write --------------------------------------------
    def _reap_session_ends(self) -> None:
        self._session_ends = [e for e in self._session_ends if not e.done]

    def session_end_pending(self) -> bool:
        """An end-of-run write has not landed yet (or failed and awaits retry)."""
        self._reap_session_ends()
        return bool(self._session_ends)

    @property
    def persistence_error(self) -> str:
        """Why the last end-of-run write failed, or "" - it will be retried."""
        for end in self._session_ends:
            if end.error is not None:
                return f"{type(end.error).__name__}: {end.error}"
        return ""

    def wait_for_session_ends(self, timeout: Optional[float] = None) -> bool:
        """Block until every end-of-run write has landed. For blocking callers."""
        from time import monotonic

        deadline = None if timeout is None else monotonic() + timeout
        for end in list(self._session_ends):
            left = None if deadline is None else max(0.0, deadline - monotonic())
            end.join(left)
        self._reap_session_ends()
        return not self._session_ends

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
        if self._closing:
            return "BabelFishR is quitting; nothing new is started."
        problem = self.shutdown_problem()
        if problem:
            return problem
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

    def _processing_pipeline(self, for_shutdown: bool = False
                             ) -> Optional[ProcessingPipeline]:
        """The live pipeline when monitoring, otherwise a standalone one.

        ``for_shutdown`` is close()'s own route in: once Quit is pending no
        new saved-recording work is taken from anyone else, but a
        transmission the capture saved on its way out still gets a processor.

        Deliberately not a fake capture session: no Session row is created, no
        audio device is opened, and nothing about the operator's monitoring
        state changes. It publishes on the same event bus, so a bubble updates
        exactly as it does during a live session, and it runs on its own
        worker thread so the window never freezes.
        """
        self._reap()
        if self._closing and not for_shutdown:
            return None
        if self.pipeline is not None:
            # A live pipeline cannot outlive its mode through set_mode, which
            # refuses while monitoring. This covers the other route in: a
            # direct write to config.mode. Nothing is processed rather than
            # processed by engines the current mode forbids.
            return self.pipeline if self._session_mode is self.mode else None
        if self.standalone_pipeline is not None:
            if self.standalone_pipeline.shutting_down:
                return None            # retiring: takes nothing new
            if self._standalone_mode is self.mode:
                return self.standalone_pipeline
            # Defence in depth. set_mode retires this already; reaching here
            # means the mode moved some other way, and the cached processor
            # belongs to the old one.
            if self.standalone_pipeline.pending:
                return None
            try:
                self._retire_processing()
            except ModeChangeRefused:
                return None
        if any(p.pending for p in self._retired):
            return None                # nothing new starts beside a straggler

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
    def conversations(self, include_hidden: bool = False):
        return self.store.list_conversations(include_hidden=include_hidden)

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

    def set_conversation_color(self, conversation_id: str, color: str):
        """Colour one Session's tab. "" restores the default."""
        return self.store.set_conversation_color(conversation_id, color)

    # -- removing messages -------------------------------------------------
    def owned_roots(self) -> List[str]:
        """Directories whose files BabelFishR created and may delete.

        The Recordings folder only. A WAV the operator replayed from their own
        folder, an export, a backup, a shared copy - none of those are ours.
        """
        return [str(pathlib.Path(self.config.recording.directory).expanduser())]

    def removal_problem(self, tx_id: str) -> str:
        """Why this message cannot be removed right now, or "" when it can."""
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return "That message is no longer in the database."
        for pipeline in (self.pipeline, self.standalone_pipeline):
            if pipeline is not None and pipeline.is_in_flight(tx_id):
                return ("This message is still being processed. Wait for the "
                        "transcript to finish, then try again.")
        return ""

    def remove_from_thread(self, tx_id: str):
        """Take a message out of view; keep its data. Restorable."""
        return self.store.hide_transmission(tx_id, hidden=True)

    def restore_to_thread(self, tx_id: str):
        return self.store.hide_transmission(tx_id, hidden=False)

    def deletion_inventory(self, tx_id: str):
        tx = self.store.get_transmission(tx_id)
        return None if tx is None else self.store.deletion_inventory(
            tx, self.owned_roots())

    def delete_permanently(self, tx_id: str):
        """Delete the message and the files that are its alone.

        Playback of it is the window's to stop first; processing is refused
        via removal_problem(). Returns the report, or None if already gone.
        """
        return self.store.delete_transmission_permanently(tx_id, self.owned_roots())

    def leftover_deletions(self):
        return self.store.leftover_deletions()

    # -- removing whole Sessions ---------------------------------------------
    GENERAL_REMOVAL_PENDING = (
        "General is the default Session and is kept for now. Whether it can be "
        "removed or cleared is a decision that has not been made yet.")

    def conversation_removal_problem(self, conversation_id: str) -> str:
        """Why this Session cannot be removed right now, or "" when it can."""
        conversation = self.store.get_conversation(conversation_id)
        if conversation is None:
            return "That Session no longer exists."
        if conversation.is_default:
            return self.GENERAL_REMOVAL_PENDING
        if self.capture is not None and self._capture_conversation_id == conversation_id:
            return ("Monitoring is recording into this Session. Stop monitoring "
                    "first, then remove it.")
        for session_id in self.store.session_ids_for_conversation(conversation_id):
            for tx in self.store.list_transmissions(session_id=session_id,
                                                    limit=1_000_000,
                                                    include_hidden=True):
                if self.removal_problem(tx.id).startswith("This message is still"):
                    return ("A message in this Session is still being processed. "
                            "Wait for it to finish, then try again.")
        return ""

    def hide_conversation(self, conversation_id: str):
        hidden = self.store.hide_conversation(conversation_id, hidden=True)
        if hidden is not None and self._conversation_id == conversation_id:
            self.select_conversation(self.store.default_conversation().id)
        return hidden

    def restore_conversation(self, conversation_id: str):
        return self.store.hide_conversation(conversation_id, hidden=False)

    def conversation_removal_inventory(self, conversation_id: str):
        return self.store.conversation_removal_inventory(conversation_id,
                                                         self.owned_roots())

    def delete_conversation_permanently(self, conversation_id: str):
        report = self.store.delete_conversation_permanently(conversation_id,
                                                            self.owned_roots())
        if report is not None and self._conversation_id == conversation_id:
            self.select_conversation(self.store.default_conversation().id)
        return report

    def retry_leftover_deletions(self):
        return self.store.retry_leftover_deletions(self.owned_roots())

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
                             newest_first: bool = False,
                             include_hidden: bool = False) -> List[Transmission]:
        """The message thread for one named Session.

        Across monitoring runs, deliberately. Stopping and restarting
        monitoring is not the end of the operator's log; it is a pause in one
        continuous radio watch, and every run filed under this thread belongs
        to it.
        """
        limit = self.HISTORY_LIMIT if limit is None else limit
        return self.store.conversation_transmissions(
            conversation_id or self.conversation_id, limit=limit,
            newest_first=newest_first, include_hidden=include_hidden)

    def search(self, query: str = "", **filters) -> List[Transmission]:
        """Search inside the named Session being viewed.

        Scoped by default rather than globally, because search is reached from
        a Session tab and returning another Session's traffic into that tab is
        the same misfiling the thread itself refuses. Pass
        ``conversation_id=None`` explicitly to search everything.
        """
        filters.setdefault("conversation_id", self.conversation_id)
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

        try:
            # Registered for the whole run, result save included: this is a
            # store user shutdown has to wait for. Refused once Quit is
            # pending - returns None rather than start work that could not
            # be saved.
            with self.activity("digital analysis"):
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
        except ProcessingBusy:
            return None

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

    def review_queue(self, conversation_id: Optional[str] = _UNSCOPED
                     ) -> List[Transmission]:
        """The review queue for the named Session being viewed."""
        if conversation_id is _UNSCOPED:
            conversation_id = self.conversation_id
        return self.store.review_queue(conversation_id=conversation_id)

    def close(self, wait: bool = True, timeout: Optional[float] = None,
              start_cleanup: bool = True) -> bool:
        """Shut down in order, closing engines and the store last - and only
        when nothing can still use them.

        Order: the capture settles (its stopper thread has joined the audio
        thread, closed the source and run the final flush, so the last
        transmission has been handed over); anything it saved that no
        processor accepted is handed to one; the processor finishes what it
        accepted and its workers end; stragglers end; other store users - a
        digital analysis saving its result - finish; then, on a cleanup
        thread of its own, the engines close (each once) and then the store.
        Each step waits for the previous to be *complete*, never merely timed
        out, and returns False with nothing closed if it is not.

        Once called, Quit is pending (:attr:`closing`): nothing new starts,
        accepted work and the final capture hand-off finish. Once the final
        cleanup has been handed to its thread, :attr:`cleaning` is set: from
        that moment the store may close at any time and nothing on the
        calling side may read it. ``wait=True`` (the command line, tests)
        blocks until done or until ``timeout`` and re-raises a cleanup
        failure; ``wait=False`` (the window) never joins a thread and never
        closes a resource itself. A second call while the cleanup thread is
        running does not start another; a failed cleanup is retried from
        where it stopped, never re-closing what closed.
        """
        if self._closed:
            return True
        if self._close_in_progress:
            return False
        self._close_in_progress = True
        try:
            return self._close(wait, timeout, start_cleanup)
        finally:
            self._close_in_progress = False

    @property
    def ready_for_cleanup(self) -> bool:
        """Every user of the store has finished; only the final cleanup is left.

        The window checks this, shuts every route it has to the store - menus,
        shortcuts, controls, its event drain - and only then calls
        :meth:`start_final_cleanup`. Until it does, the store stays open.
        """
        return self._ready_for_cleanup

    def start_final_cleanup(self) -> None:
        """Begin closing engines and the store, on the cleanup thread.

        Only valid once :attr:`ready_for_cleanup`; the caller has by then
        stopped reading the store itself. Starting twice is harmless.
        """
        if not self._ready_for_cleanup:
            raise RuntimeError("the application is not ready for its final cleanup")
        self._final.start()

    @property
    def cleaning(self) -> bool:
        """The final cleanup has begun: the store may close at any moment."""
        return self._final.started

    @property
    def cleanup_error(self) -> str:
        """Why the last cleanup attempt failed, or "" - it will be retried."""
        error = self._final.error
        return f"{type(error).__name__}: {error}" if error is not None else ""

    def _close(self, wait: bool, timeout: Optional[float],
               start_cleanup: bool = True) -> bool:
        from time import monotonic, sleep

        self._closing = True
        deadline = None if timeout is None else monotonic() + timeout

        def remaining() -> Optional[float]:
            return None if deadline is None else max(0.0, deadline - monotonic())

        def out_of_time() -> bool:
            left = remaining()
            return left is not None and left <= 0

        def finish() -> bool:
            # 6. Engines, then the store - on the cleanup thread, once.
            if self._final.running:
                if wait:
                    self._final.join(remaining())
                else:
                    return False
            if self._final.error is not None and not self._final.running:
                if wait:
                    raise self._final.error
                # Retry: a fresh attempt resumes from what is still open.
            if not self._closed and not self._final.running:
                self._final.start()
                if wait:
                    self._final.join(remaining())
                    if self._final.error is not None:
                        raise self._final.error
            return self._closed

        if self._final.started:
            return finish()            # everything before it is already done

        self.stop_session()

        # 1. The capture, first: until it has settled it can still produce
        #    the final transmission, and the processor must still be there
        #    to take it.
        capture = self._lingering_capture
        if capture is not None:
            if wait:
                capture.wait_settled(timeout=remaining())
            self._reap()
            if self._lingering_capture is not None:
                return False

        # 2. Whatever a capture saved that nothing accepted.
        self._adopt_unprocessed()

        # 3. The processor finishes what it accepted, then its workers end.
        pipeline = self.standalone_pipeline
        if pipeline is not None:
            if wait:
                while pipeline.pending:
                    if out_of_time():
                        return False
                    left = remaining()
                    pipeline.wait_until_idle(
                        timeout=1.0 if left is None else min(1.0, left))
            if pipeline.stop_if_idle(wait=False):
                if not pipeline.finished:
                    self._track_retired(pipeline)  # leaving; waited for below
                self.standalone_pipeline = None
                self._standalone_mode = None
            else:
                return False           # still working; everything stays open

        # 4. Every worker has actually ended - nothing is joined on this
        #    thread unless the caller asked to wait.
        if wait:
            for straggler in list(self._retired):
                for thread in straggler.worker_threads():
                    thread.join(timeout=remaining())
        self._reap()
        if self._retired:
            return False               # a thread can still reach the engines

        # 5. Every other user of the store.
        if wait:
            while self.active_operations():
                if out_of_time():
                    return False
                sleep(0.05)
        with self._activity_lock:
            if self._activities:
                return False
            # Nothing can register from here: closing is set and the check
            # above happened under the same lock.

        # 5b. The end of the run, recorded. A failed write is retried, never
        #     skipped: the store does not close over a run left open.
        self._reap_session_ends()
        for end in self._session_ends:
            if end.error is not None and not end.running:
                end.start()
        if wait:
            for end in list(self._session_ends):
                end.join(remaining())
        self._reap_session_ends()
        if self._session_ends:
            return False

        # 6. Only the final cleanup is left. A caller with routes of its own
        #    to the store (the window) shuts them first and then asks for it.
        self._ready_for_cleanup = True
        if not start_cleanup:
            return False
        return finish()

    def _close_resources(self) -> None:
        """The last step, run on the cleanup thread: engines once, then the
        store. Whatever closed stays closed if a later step fails; the retry
        resumes with what is still open."""
        for name in ("transcription", "translation"):
            engine = getattr(self, name)
            if engine is not None:
                engine.close()
                setattr(self, name, None)
        if not self._store_closed:
            self.store.close()
            self._store_closed = True
        self._closed = True
