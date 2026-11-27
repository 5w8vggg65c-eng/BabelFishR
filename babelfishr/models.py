"""Core entities: Session, RadioProfile and the first-class Transmission.

Vocabulary note
---------------
A *Transmission* is something BabelFishR **received**.  A remote operator
transmitted it; we are receive-only.  Nothing in this application models an
outgoing transmission, and the word "TX" is deliberately absent from the model.

What we can and cannot know
---------------------------
BabelFishR normally sees only the demodulated audio waveform arriving at a
computer audio input.  Frequency, channel, mode and the identity of the remote
operator are **not** derivable from that waveform - they are supplied by the
user, or by a :class:`RadioProfile` the user selected.  Fields carrying that
metadata record *what the user told us*, and say so.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def iso(ts: Optional[_dt.datetime]) -> Optional[str]:
    if ts is None:
        return None
    return ts.astimezone(_dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def parse_iso(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    if isinstance(text, _dt.datetime):
        return text
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


class ProcessingState(str, enum.Enum):
    """Lifecycle of one received transmission."""

    CAPTURED = "captured"
    """Audio is on disk. This state is reached before anything can fail."""

    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    TRANSLATING = "translating"
    COMPLETE = "complete"
    FAILED = "failed"
    """Processing failed; ``error`` explains it and the audio is still intact."""

    SKIPPED = "skipped"
    """Not processed automatically. The audio is recorded and playable, and
    the operator can force transcription at any time."""

    @property
    def is_terminal(self) -> bool:
        return self in (ProcessingState.COMPLETE, ProcessingState.FAILED,
                        ProcessingState.SKIPPED)

    @property
    def is_pending(self) -> bool:
        return not self.is_terminal


class ContentClass(str, enum.Enum):
    """Detector's read on what a transmission contains.

    Recorded for every transmission, and never a reason one was discarded:
    see :mod:`babelfishr.detect` for the capture-first invariant.
    """

    SPEECH = "speech"
    NOISE = "noise"
    TONE = "tone"
    DIGITAL_SUSPECTED = "digital-suspected"
    UNKNOWN = "unknown"


class Provenance(str, enum.Enum):
    """Where a metadata value came from.

    Recorded per field, because presenting an operator-typed frequency as a
    measured one would be a lie the operator might later act on.
    """

    OPERATOR = "operator-entered"
    PROFILE = "radio-profile-default"
    RADIO = "radio-reported"
    SDR = "sdr-measured"
    INFERRED = "software-inferred"
    DSD = "dsd-decoded"
    UNKNOWN = "unknown"

    @property
    def is_measured(self) -> bool:
        return self in (Provenance.SDR, Provenance.RADIO)

    @property
    def label(self) -> str:
        return {
            Provenance.OPERATOR: "entered by operator",
            Provenance.PROFILE: "from radio profile",
            Provenance.RADIO: "reported by radio",
            Provenance.SDR: "measured by SDR",
            Provenance.INFERRED: "inferred by software",
            Provenance.DSD: "decoded by DSD-neo",
            Provenance.UNKNOWN: "unknown origin",
        }[self]

    @property
    def short(self) -> str:
        """A few characters for a crowded metadata line.

        Measured values carry no suffix - they are the default reading. Every
        other origin is marked, so nothing typed or defaulted can be mistaken
        for something a receiver actually measured.
        """
        return {
            Provenance.OPERATOR: "entered",
            Provenance.PROFILE: "profile",
            Provenance.RADIO: "radio",
            Provenance.SDR: "SDR",
            Provenance.INFERRED: "inferred",
            Provenance.DSD: "decoded",
            Provenance.UNKNOWN: "unverified",
        }[self]


class AnalysisOutcome(str, enum.Enum):
    """Result taxonomy for a digital-analysis attempt.

    Deliberately fine-grained: "we could not get a decode out of this input" is
    a very different statement from "this is encrypted", and both are different
    from "the analysis tool failed to run".
    """

    SUSPECTED_DIGITAL = "suspected-digital"
    PROTOCOL_CANDIDATE = "protocol-candidate"
    PROTOCOL_IDENTIFIED = "protocol-identified"
    VOICE_DECODED = "voice-decoded"
    METADATA_ONLY = "metadata-only"
    ENCRYPTED_OR_UNSUPPORTED = "encrypted-or-unsupported"
    INSUFFICIENT_INPUT = "insufficient-input-quality"
    ANALYSIS_FAILED = "analysis-failed"
    NO_RESULT = "no-result"

    @property
    def label(self) -> str:
        return {
            AnalysisOutcome.SUSPECTED_DIGITAL: "suspected digital",
            AnalysisOutcome.PROTOCOL_CANDIDATE: "protocol candidate",
            AnalysisOutcome.PROTOCOL_IDENTIFIED: "protocol identified",
            AnalysisOutcome.VOICE_DECODED: "voice decoded",
            AnalysisOutcome.METADATA_ONLY: "metadata only",
            AnalysisOutcome.ENCRYPTED_OR_UNSUPPORTED: "encrypted or unsupported",
            AnalysisOutcome.INSUFFICIENT_INPUT: "insufficient input quality",
            AnalysisOutcome.ANALYSIS_FAILED: "analysis failed",
            AnalysisOutcome.NO_RESULT: "no usable decode from this input",
        }[self]

    @property
    def is_success(self) -> bool:
        return self in (AnalysisOutcome.PROTOCOL_IDENTIFIED,
                        AnalysisOutcome.VOICE_DECODED,
                        AnalysisOutcome.METADATA_ONLY)


@dataclasses.dataclass
class MetadataField:
    """A value plus where it came from."""

    value: Any = None
    provenance: Provenance = Provenance.UNKNOWN

    @property
    def measured(self) -> bool:
        return self.value is not None and self.provenance.is_measured

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "provenance": self.provenance.value,
                "measured": self.measured}

    @classmethod
    def from_dict(cls, d: Any) -> "MetadataField":
        if not isinstance(d, dict):
            return cls(value=d, provenance=Provenance.UNKNOWN)
        return cls(value=d.get("value"),
                   provenance=Provenance(d.get("provenance", "unknown")))


@dataclasses.dataclass
class AnalysisArtifact:
    """A file produced by an analysis run."""

    kind: str
    """``decoded-audio``, ``derived-input``, ``log`` or ``metadata``."""

    path: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


DEFAULT_CONVERSATION_NAME = "General"


@dataclasses.dataclass
class Conversation:
    """A named thread the operator groups monitoring runs under.

    Deliberately *not* the same thing as :class:`Session`. A Session is one
    monitoring run - one press of Start, one audio device, one set of engines
    - and the database keeps that meaning intact. An operator who starts and
    stops monitoring six times over an afternoon has six Sessions and expects
    one thread. This is that thread; the interface calls it a Session because
    that is the word an operator uses, and the low-level run is never shown.
    """

    id: str = dataclasses.field(default_factory=lambda: new_id("conv_"))
    name: str = DEFAULT_CONVERSATION_NAME
    created_at: _dt.datetime = dataclasses.field(default_factory=utcnow)
    is_default: bool = False
    """The permanent General thread. It exists in every database and is where
    every pre-existing monitoring run is migrated to."""

    position: int = 0
    notes: str = ""

    hidden: bool = False
    """Removed from the tab row by the operator, everything kept.

    Its monitoring runs, messages and recordings stay in the database and on
    disk; View > Show hidden Sessions brings the tab back. Distinct from
    permanent deletion. The default General thread is never hidden.
    """

    color: str = ""
    """An operator-chosen tab colour as ``#rrggbb``, or "" for the default.

    Bound to this id, not to the tab's position or name, so renaming or
    reordering never moves a colour onto a different Session.
    """

    save_path: str = ""
    """The folder the operator chose for this Session's recordings and
    portable transcripts, or "" for the standard destination. BabelFishR
    writes only inside ``<save_path>/BabelFishR`` (see babelfishr.savepath).
    """

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["created_at"] = iso(self.created_at)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Conversation":
        d = dict(d)
        d["created_at"] = parse_iso(d.get("created_at")) or utcnow()
        d["is_default"] = bool(d.get("is_default"))
        return cls(**{k: v for k, v in d.items()
                      if k in {f.name for f in dataclasses.fields(cls)}})


@dataclasses.dataclass
class AnalysisAttempt:
    """One run of an external analysis engine over one recording.

    First class rather than free text, because reruns, protocol-specific
    retries and honest failure reporting all need structure.
    """

    id: str = dataclasses.field(default_factory=lambda: new_id("an_"))
    transmission_id: str = ""
    engine: str = ""
    engine_version: str = ""
    started_at: _dt.datetime = dataclasses.field(default_factory=utcnow)
    finished_at: Optional[_dt.datetime] = None
    runtime_seconds: float = 0.0

    input_path: str = ""
    """The artifact analysed - the original, or a derived copy."""

    input_is_derived: bool = False
    command: List[str] = dataclasses.field(default_factory=list)
    options: Dict[str, Any] = dataclasses.field(default_factory=dict)
    requested_protocol: str = ""

    outcome: AnalysisOutcome = AnalysisOutcome.NO_RESULT
    protocol: str = ""
    confidence: float = 0.0
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)
    artifacts: List[AnalysisArtifact] = dataclasses.field(default_factory=list)

    exit_status: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    attempt_number: int = 1

    @property
    def decoded_audio(self) -> Optional[str]:
        for artifact in self.artifacts:
            if artifact.kind == "decoded-audio":
                return artifact.path
        return None

    def summary(self) -> str:
        text = self.outcome.label
        if self.protocol:
            text = f"{self.protocol}: {text}"
        return text

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["started_at"] = iso(self.started_at)
        d["finished_at"] = iso(self.finished_at)
        d["outcome"] = self.outcome.value
        d["summary"] = self.summary()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AnalysisAttempt":
        d = dict(d)
        d.pop("summary", None)
        d["started_at"] = parse_iso(d.get("started_at")) or utcnow()
        d["finished_at"] = parse_iso(d.get("finished_at"))
        if d.get("outcome"):
            d["outcome"] = AnalysisOutcome(d["outcome"])
        d["artifacts"] = [AnalysisArtifact(**a) if isinstance(a, dict) else a
                          for a in d.get("artifacts") or []]
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


class SourceLanguageMode(str, enum.Enum):
    AUTOMATIC = "automatic"
    SPECIFIED = "specified"


@dataclasses.dataclass
class ErrorInfo:
    """A recoverable failure. The audio is never lost because of one."""

    stage: str
    """``transcription`` or ``translation``."""

    message: str
    occurred_at: _dt.datetime = dataclasses.field(default_factory=utcnow)
    retry_count: int = 0
    recoverable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["occurred_at"] = iso(self.occurred_at)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ErrorInfo":
        d = dict(d)
        d["occurred_at"] = parse_iso(d.get("occurred_at")) or utcnow()
        return cls(**d)


@dataclasses.dataclass
class TranscriptSegment:
    """A timed slice of the transcript, with per-slice confidence if available."""

    start: float
    end: float
    text: str
    confidence: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class RadioProfile:
    """User-declared description of the radio and channel being monitored.

    Everything here is *asserted by the operator*, not measured. It exists so a
    transmission can be labelled "GMRS 16, 462.5750 MHz" in the log without
    pretending BabelFishR determined that from the audio.
    """

    id: str = dataclasses.field(default_factory=lambda: new_id("prof_"))
    name: str = "Unnamed radio"
    radio_make: str = ""
    radio_model: str = ""
    channel_name: str = ""
    frequency_mhz: Optional[float] = None
    mode: str = ""
    """Free text, e.g. "NFM", "DMR (radio decodes to analogue audio)"."""

    notes: str = ""
    default_source_language: Optional[str] = None
    created_at: _dt.datetime = dataclasses.field(default_factory=utcnow)

    def label(self) -> str:
        parts = [p for p in (self.channel_name, self.frequency_label()) if p]
        return " - ".join(parts) if parts else self.name

    def frequency_label(self) -> str:
        if self.frequency_mhz is None:
            return ""
        return f"{self.frequency_mhz:.4f} MHz"

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["created_at"] = iso(self.created_at)
        d["label"] = self.label()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RadioProfile":
        d = dict(d)
        d.pop("label", None)
        d["created_at"] = parse_iso(d.get("created_at")) or utcnow()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class Session:
    """One monitoring run: a start, an end, and the transmissions between.

    Unchanged in meaning. It is *not* what the interface calls a Session -
    that is a :class:`Conversation`, a named thread this run belongs to.
    Keeping them separate is what lets an operator start and stop monitoring
    all afternoon and still see one continuous log.
    """

    id: str = dataclasses.field(default_factory=lambda: new_id("sess_"))
    name: str = ""
    started_at: _dt.datetime = dataclasses.field(default_factory=utcnow)
    ended_at: Optional[_dt.datetime] = None

    audio_device: str = ""
    """Human-readable name of the input device the user selected."""

    audio_device_id: Optional[str] = None
    sample_rate: int = 0
    profile_id: Optional[str] = None
    profile_label: str = ""
    source_language_mode: SourceLanguageMode = SourceLanguageMode.AUTOMATIC
    source_language: Optional[str] = None
    target_language: str = "en"
    transcription_engine: str = ""
    translation_engine: str = ""
    notes: str = ""

    conversation_id: str = ""
    """The named thread this run belongs to. Backfilled to General for every
    run recorded before named threads existed."""

    save_path: str = ""
    """The Session's chosen folder as it was when this run started ("" for
    the standard destination). Pinned per run: changing the Session's
    destination later never moves where this run's recordings and portable
    transcripts go."""

    capture_gaps: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    """Audio the input lost during this run (capture buffer overflow), one
    :meth:`CaptureGap.to_dict` per gap, in stream order. Read-only here:
    filled by the store from its capture_gaps table, never written by
    save_session, so a stale Session object cannot erase the record."""

    @property
    def dropped_frames(self) -> int:
        return sum(int(g.get("frames") or 0) for g in self.capture_gaps)

    @property
    def dropped_seconds(self) -> float:
        return round(sum(float(g.get("duration") or 0.0)
                         for g in self.capture_gaps), 6)

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def duration(self) -> float:
        end = self.ended_at or utcnow()
        return max(0.0, (end - self.started_at).total_seconds())

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["started_at"] = iso(self.started_at)
        d["ended_at"] = iso(self.ended_at)
        d["source_language_mode"] = self.source_language_mode.value
        d["duration"] = self.duration
        d["dropped_frames"] = self.dropped_frames
        d["dropped_seconds"] = self.dropped_seconds
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Session":
        d = dict(d)
        d.pop("duration", None)
        d.pop("dropped_frames", None)
        d.pop("dropped_seconds", None)
        d["capture_gaps"] = list(d.get("capture_gaps") or [])
        d["started_at"] = parse_iso(d.get("started_at")) or utcnow()
        d["ended_at"] = parse_iso(d.get("ended_at"))
        _coerce_enum(d, "source_language_mode", SourceLanguageMode)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class CaptureGap:
    """A stretch of audio the input received but could not keep.

    The live input's buffer overflowed (the capture thread fell behind) and
    the oldest buffered audio was discarded to keep the device callback from
    stalling. ``start_frame`` is the position in the run's stream, in frames
    at ``sample_rate`` counted from the start of capture, where the missing
    audio began; ``None`` when the frames were counted at the drop but could
    not be placed (still unread when the run ended)."""

    session_id: str = ""
    frames: int = 0
    sample_rate: int = 0
    start_frame: Optional[int] = None
    started_at: Optional[_dt.datetime] = None
    """Wall-clock time of the first missing frame (stream start + offset)."""
    blocks: int = 0
    reason: str = "buffer-overflow"
    recorded_at: _dt.datetime = dataclasses.field(default_factory=utcnow)

    @property
    def duration(self) -> float:
        return self.frames / float(self.sample_rate) if self.sample_rate else 0.0

    @property
    def stream_offset(self) -> Optional[float]:
        """Seconds from the start of capture to the first missing frame."""
        if self.start_frame is None or not self.sample_rate:
            return None
        return self.start_frame / float(self.sample_rate)

    def to_dict(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "frames": int(self.frames),
                "sample_rate": int(self.sample_rate),
                "duration": round(self.duration, 6),
                "start_frame": self.start_frame,
                "stream_offset": (None if self.stream_offset is None
                                  else round(self.stream_offset, 6)),
                "started_at": iso(self.started_at), "blocks": int(self.blocks),
                "reason": self.reason, "recorded_at": iso(self.recorded_at)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CaptureGap":
        start = d.get("start_frame")
        return cls(session_id=d.get("session_id") or "",
                   frames=int(d.get("frames") or 0),
                   sample_rate=int(d.get("sample_rate") or 0),
                   start_frame=None if start is None else int(start),
                   started_at=parse_iso(d.get("started_at")),
                   blocks=int(d.get("blocks") or 0),
                   reason=d.get("reason") or "buffer-overflow",
                   recorded_at=parse_iso(d.get("recorded_at")) or utcnow())


def _enum_value(value: Any, default: Any) -> str:
    """The stored form of an enum field, tolerating a field that lost its type.

    Used on the way out. Capture-first means an event is written to disk before
    anything classifies it, so a half-populated row is a normal thing to read
    back; it must not be a thing that cannot be written out again.
    """
    if isinstance(value, enum.Enum):
        return value.value
    if value:
        return str(value)
    return default.value


def _coerce_enum(data: Dict[str, Any], key: str, enum_type) -> None:
    """Turn a stored value into its enum member, in place.

    A falsy value means nothing was recorded, so the key is removed and the
    dataclass default applies. An unrecognised value is also dropped rather
    than raising: a transmission that survived being received should not be
    unreadable because one column holds a string from a future version.
    """
    if key not in data:
        return
    value = data[key]
    if isinstance(value, enum_type):
        return
    if not value:
        data.pop(key)
        return
    try:
        data[key] = enum_type(value)
    except ValueError:
        log.warning("unrecognised %s %r; using the default", key, value)
        data.pop(key)


@dataclasses.dataclass
class Transmission:
    """One received transmission - the application's central entity."""

    # -- identity ------------------------------------------------------
    id: str = dataclasses.field(default_factory=lambda: new_id("tx_"))
    session_id: str = ""

    # -- timing --------------------------------------------------------
    started_at: _dt.datetime = dataclasses.field(default_factory=utcnow)
    ended_at: Optional[_dt.datetime] = None
    duration: float = 0.0

    # -- capture -------------------------------------------------------
    audio_device: str = ""
    audio_path: Optional[str] = None
    """The original, unmodified capture. Never overwritten, never processed."""

    processed_audio_path: Optional[str] = None
    """Optional derived copy (resampled/normalised) for the ASR engine."""

    sample_rate: int = 0
    peak_dbfs: float = -120.0
    noise_floor_dbfs: float = -120.0
    clipped: bool = False

    detection_confidence: float = 0.0
    """How sure the detector is that this was a real transmission, not noise."""

    content_class: ContentClass = ContentClass.UNKNOWN
    """What the detector thought this was. Advice, not a gate on persistence."""

    auto_processed: bool = True
    """False when classification routed this away from automatic ASR."""

    skip_reason: str = ""
    """Why automatic processing was skipped, shown verbatim in the UI."""

    # -- operator-declared context (never inferred from audio) ---------
    profile_id: Optional[str] = None
    profile_label: str = ""
    channel_name: str = ""
    frequency_mhz: Optional[float] = None

    frequency_provenance: Provenance = Provenance.UNKNOWN
    """Where ``frequency_mhz`` came from. Never silently "measured"."""

    channel_provenance: Provenance = Provenance.UNKNOWN
    rssi_dbm: Optional[float] = None
    """Only ever set by a source that genuinely measures it (an SDR)."""

    rssi_provenance: Provenance = Provenance.UNKNOWN
    snr_db: Optional[float] = None
    """Signal-to-noise as a *receiver* reports it, not the audio SNR the
    detector computes. Only an SDR or a radio that reports it may set this."""

    snr_provenance: Provenance = Provenance.UNKNOWN
    modulation: str = ""
    modulation_provenance: Provenance = Provenance.UNKNOWN

    # -- channel access signalling --------------------------------------
    squelch_code: str = ""
    """CTCSS/PL tone or DCS code, e.g. "127.3 Hz" or "D023N".

    This says which sub-channel the transmission opened, not who sent it.
    Two radios sharing a tone are indistinguishable by it, so it must never
    be labelled as a user, an operator or an identity.
    """

    squelch_code_provenance: Provenance = Provenance.UNKNOWN

    # -- digital identifiers, only ever from something that decodes them --
    talkgroup: str = ""
    """Digital talkgroup, when a protocol decoder genuinely reported one."""

    talkgroup_provenance: Provenance = Provenance.UNKNOWN
    unit_id: str = ""
    """Source radio/unit ID as supplied by an SDR, a radio protocol, a
    signalling decoder or the operator.

    A radio, not a person: one unit may be carried by anyone, and BabelFishR
    never infers who is speaking from the sound of a voice.
    """

    unit_id_provenance: Provenance = Provenance.UNKNOWN
    protocol: str = ""
    """Only when genuinely decoded - never guessed from a classification."""

    protocol_provenance: Provenance = Provenance.UNKNOWN

    signal_metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)
    """Raw, unmodelled metadata exactly as a decoder or source reported it.

    Kept so a later version can make sense of a field this one does not model,
    and so a claim in the interface can always be traced back to what the
    decoder actually said.
    """

    # -- digital analysis ----------------------------------------------
    analysis_attempts: List[AnalysisAttempt] = dataclasses.field(
        default_factory=list)

    # -- language ------------------------------------------------------
    source_language_mode: SourceLanguageMode = SourceLanguageMode.AUTOMATIC
    source_language: Optional[str] = None
    """Detected, or configured when the mode is SPECIFIED."""

    language_confidence: Optional[float] = None
    target_language: str = "en"

    # -- text products (kept strictly separate) ------------------------
    transcript: str = ""
    """Original-language transcript, exactly as produced by the engine."""

    transcript_confidence: Optional[float] = None
    transcript_segments: List[TranscriptSegment] = dataclasses.field(default_factory=list)
    translation: str = ""
    """Translated text. Never written back over ``transcript``."""

    transcript_correction: Optional[str] = None
    """Operator's corrected transcript. The original stays in ``transcript``."""

    translation_correction: Optional[str] = None
    corrected_at: Optional[_dt.datetime] = None

    # -- provenance ----------------------------------------------------
    transcription_engine: str = ""
    transcription_engine_version: str = ""
    translation_engine: str = ""
    translation_engine_version: str = ""

    # -- state ---------------------------------------------------------
    state: ProcessingState = ProcessingState.CAPTURED
    error: Optional[ErrorInfo] = None

    # -- operator annotations ------------------------------------------
    hidden: bool = False
    """Removed from the visible thread by the operator, data kept.

    Not shown in the thread, search, review or ordinary exports; still in the
    database and still on disk; restorable. Distinct from deletion, which
    leaves nothing to restore.
    """

    notes: str = ""
    tags: List[str] = dataclasses.field(default_factory=list)
    bookmarked: bool = False
    reviewed: bool = False

    history_generation: int = 0
    """Bumped each time Clear Session History changes this row. A copy
    loaded before a clear carries the older number, so the store refuses to
    save it and the window does not draw it (see Store.is_stale)."""

    # ---- derived ------------------------------------------------------
    @property
    def display_transcript(self) -> str:
        """What the UI shows as the original-language text."""
        if "transcript" in (self.signal_metadata or {}).get("operator_cleared_fields", ()):
            return ""
        return self.transcript_correction or self.transcript

    @property
    def display_translation(self) -> str:
        if "translation" in (self.signal_metadata or {}).get("operator_cleared_fields", ()):
            return ""
        return self.translation_correction or self.translation

    @property
    def has_correction(self) -> bool:
        return bool(self.transcript_correction or self.translation_correction
                    or (self.signal_metadata or {}).get("operator_cleared_fields"))

    @property
    def operator_checked(self) -> bool:
        """A saved correction or explicit approval of this recognition.

        Older releases also set reviewed when saving a note. A note alone
        cannot certify a transcript or clear its speech-check flag.
        """
        return self.reviewed and bool(self.has_correction or
                                      (self.signal_metadata or {}).get("operator_approved"))

    @property
    def guess_withheld(self) -> bool:
        """The recogniser's text is a guess with no intelligible speech behind
        it, and is not shown as traffic: the speech check held it as noise,
        or it is a stock video sign-off the recorded evidence does not
        support. Worked out from what is stored, so older records are judged
        the same way without rewriting them. A manual correction supplies
        the words. Requesting another decode still requires voice evidence
        for a stock sign-off."""
        if self.transcript_correction or not (self.display_transcript or "").strip():
            return False
        metadata = self.signal_metadata or {}
        check = metadata.get("speech_check") or {}
        if check.get("verdict") == "likely_noise" and not metadata.get("speech_confirmed"):
            return True
        kind = stock_signoff(self.transcript)
        if not kind:
            return False
        texts = [s.get("text") if isinstance(s, dict) else getattr(s, "text", "")
                 for s in (self.transcript_segments or [])]
        if any(t and t.strip() and not stock_signoff(t) for t in texts):
            return False        # real words too: see withheld_ending
        return signoff_is_noise(kind, check.get("speech_seconds"),
                                check.get("speech_ratio"),
                                check.get("no_speech_probability"),
                                check.get("transcript_confidence",
                                          self.transcript_confidence))

    def _ending_split(self):
        """(kept segments, withheld ending text) when the transcript ends in
        a stock sign-off the recording does not support, else None.

        Real words often come first ("The rain is easing.") and a recogniser
        then adds "Thanks for watching" over the unvoiced tail - timestamped
        past the end of the recording, or with low confidence. Only trailing
        segments that are themselves stock phrases AND unsupported are
        withheld; a sign-off within the audio with a confident engine (one a
        person said) is kept, and so is everything before it."""
        if self.transcript_correction or not self.transcript_segments:
            return None
        segments = [s if isinstance(s, TranscriptSegment) else TranscriptSegment(
            float(s.get("start") or 0.0), float(s.get("end") or 0.0),
            str(s.get("text") or ""), s.get("confidence"))
            for s in self.transcript_segments]
        duration = float(self.duration or 0.0)
        cut = len(segments)
        while cut > 0:
            seg = segments[cut - 1]
            if not stock_signoff(seg.text):
                break
            beyond = duration > 0 and (seg.start >= duration - 0.05
                                       or seg.end > duration + 0.75)
            doubtful = seg.confidence is not None and seg.confidence < 0.5
            if not (beyond or doubtful):
                break
            cut -= 1
        kept = [s for s in segments[:cut] if (s.text or "").strip()]
        if cut == len(segments) or not kept:
            return None                       # nothing to withhold, or nothing else said
        ending = " ".join(s.text.strip() for s in segments[cut:])
        return kept, ending

    @property
    def withheld_ending(self) -> str:
        """The unsupported stock sign-off withheld from the end of an
        otherwise real transcript, or "". The raw text keeps it."""
        if self.guess_withheld or not (self.display_transcript or "").strip():
            return ""
        split = self._ending_split()
        return split[1] if split else ""

    @property
    def shown_transcript(self) -> str:
        """The original-language text to present as traffic: "" for a
        withheld guess, and without an unsupported sign-off at its end (the
        raw candidate stays in ``transcript``)."""
        if self.guess_withheld or not (self.display_transcript or "").strip():
            return ""
        split = None if self.transcript_correction else self._ending_split()
        if split:
            return " ".join(s.text.strip() for s in split[0])
        return self.display_transcript

    #: What every routine display says instead of a withheld guess.
    UNINTELLIGIBLE = "Audio unintelligible"
    STATIC = "Static"

    @property
    def is_static(self) -> bool:
        """Captured static, positively: the recording measured as stationary
        broadband noise (babelfishr.static) AND no credible speech - no
        transcript, or only a guess withheld as not speech, and the voice
        detector did not hear half a second of speech. An operator's
        correction supplies the words directly. Rows made
        before the measurement existed carry none and are never called
        static."""
        if self.transcript_correction:
            return False
        metadata = self.signal_metadata or {}
        static = metadata.get("static_check") or {}
        if static.get("verdict") != "static":
            return False
        if (self.transcript or "").strip() and not self.guess_withheld:
            return False
        seconds = (metadata.get("speech_check") or {}).get("speech_seconds")
        return not (seconds is not None and seconds >= 0.5)

    @property
    def presented_transcript(self) -> str:
        """The original-language line for routine displays (the window, the
        command line, readable exports): the published words, "Audio
        unintelligible" for a withheld guess, never the raw guess. The raw
        engine text stays in ``transcript`` for Edit, tooltips and the
        forensic (JSON) export."""
        words = self.shown_transcript
        if words.strip():
            return words
        metadata = self.signal_metadata or {}
        if metadata.get("processing_stopped") or "text" in (metadata.get("history_cleared") or {}):
            return ""
        if self.is_static:
            return self.STATIC
        if self.guess_withheld or self.state in (ProcessingState.COMPLETE, ProcessingState.SKIPPED):
            return self.UNINTELLIGIBLE
        return ""

    def published_text(self) -> str:
        """Every word a search may match: the published transcript and
        translation, corrections, notes and tags - never a withheld guess."""
        return " ".join(filter(None, (
            self.shown_transcript, self.shown_translation,
            self.transcript_correction, self.translation_correction,
            self.notes, " ".join(self.tags or ()))))

    @property
    def shown_translation(self) -> str:
        if self.translation_correction:
            return self.translation_correction
        if self.guess_withheld:
            return ""
        text = self.display_translation
        if text and self.withheld_ending:
            # An older translation still carries the ending: drop trailing
            # sentences that are themselves stock sign-offs.
            sentences = _re.split(r"(?<=[.!?\u3002])\s+", text.strip())
            while sentences and stock_signoff(sentences[-1]):
                sentences.pop()
            text = " ".join(sentences)
        return text

    @property
    def frequency_is_measured(self) -> bool:
        """True only when something actually measured it. Never for typed values."""
        return (self.frequency_mhz is not None
                and self.frequency_provenance.is_measured)

    @property
    def latest_analysis(self) -> Optional[AnalysisAttempt]:
        return self.analysis_attempts[-1] if self.analysis_attempts else None

    @property
    def decoded_audio_path(self) -> Optional[str]:
        for attempt in reversed(self.analysis_attempts):
            if attempt.decoded_audio:
                return attempt.decoded_audio
        return None

    @property
    def can_transcribe_anyway(self) -> bool:
        """True when the operator may force ASR on a skipped recording."""
        return bool(self.audio_path) and not self.transcript

    def signal_summary(self) -> List[Dict[str, str]]:
        """Metadata worth showing beside a transmission, with its provenance.

        Absent values produce nothing at all: an empty row would invite the
        operator to read "-" as a measurement of zero. Every entry carries
        where it came from, and a value whose provenance is OPERATOR or
        PROFILE is labelled as entered, never as measured - which is the whole
        reason each field has its own provenance in the first place.
        """
        fields = (
            ("frequency", self.frequency_display(), self.frequency_provenance),
            ("channel", self.channel_name, self.channel_provenance),
            ("RSSI", (f"{self.rssi_dbm:.0f} dBm"
                      if self.rssi_dbm is not None else ""),
             self.rssi_provenance),
            ("SNR", (f"{self.snr_db:.0f} dB" if self.snr_db is not None else ""),
             self.snr_provenance),
            ("modulation", self.modulation, self.modulation_provenance),
            ("squelch", self.squelch_code, self.squelch_code_provenance),
            ("talkgroup", self.talkgroup, self.talkgroup_provenance),
            ("unit", self.unit_id, self.unit_id_provenance),
            ("protocol", self.protocol, self.protocol_provenance),
        )
        out: List[Dict[str, str]] = []
        for label, value, provenance in fields:
            if not value:
                continue
            provenance = provenance or Provenance.UNKNOWN
            out.append({"label": label, "value": str(value),
                        "provenance": provenance.value,
                        "measured": "yes" if provenance.is_measured else "no",
                        "display": f"{label} {value}" + (
                            "" if provenance.is_measured else
                            f" ({provenance.short})")})
        return out

    def frequency_display(self) -> str:
        if self.frequency_mhz is None:
            return ""
        return f"{self.frequency_mhz:.4f} MHz"

    @property
    def has_supplied_unit_id(self) -> bool:
        """Was a source radio/unit ID actually supplied by something?

        False for a squelch tone: CTCSS and DCS are channel access, shared by
        every radio set to them. False for anything BabelFishR worked out for
        itself. Only an SDR, a decoded protocol, a radio that reported it, or
        the operator typing it counts.
        """
        allowed = (Provenance.SDR, Provenance.DSD, Provenance.RADIO,
                   Provenance.OPERATOR)
        return bool(self.unit_id) and self.unit_id_provenance in allowed

    @property
    def worth_digital_analysis(self) -> bool:
        return self.content_class in (ContentClass.DIGITAL_SUSPECTED,
                                      ContentClass.NOISE, ContentClass.UNKNOWN)

    @property
    def needs_review(self) -> bool:
        """Low confidence, or a failure the operator should look at."""
        if self.state is ProcessingState.FAILED:
            return True
        metadata = self.signal_metadata or {}
        if metadata.get("processing_stopped"):
            return True
        if self.operator_checked:
            return False
        check = metadata.get("speech_check") or {}
        if (check.get("verdict") in ("likely_noise", "uncertain")
                or self.guess_withheld or metadata.get("translation_unresolved")):
            return True
        if self.reviewed:
            return False
        thresholds = [v for v in (self.transcript_confidence,
                                  self.language_confidence) if v is not None]
        return any(v < 0.6 for v in thresholds)

    @property
    def can_approve(self) -> bool:
        """Only a finished result can be acknowledged as checked."""
        return (self.state in (ProcessingState.COMPLETE, ProcessingState.SKIPPED)
                and not (self.signal_metadata or {}).get("processing_stopped")
                and self.needs_review)

    def finish(self, ended_at: Optional[_dt.datetime] = None) -> None:
        self.ended_at = ended_at or utcnow()
        self.duration = max(0.0, (self.ended_at - self.started_at).total_seconds())

    def fail(self, stage: str, message: str) -> None:
        """Record a failure without discarding anything already produced."""
        retry = self.error.retry_count + 1 if self.error else 0
        self.error = ErrorInfo(stage=stage, message=str(message)[:2000],
                               retry_count=retry)
        self.state = ProcessingState.FAILED

    def clear_error(self) -> None:
        self.error = None

    # ---- serialisation ------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["started_at"] = iso(self.started_at)
        d["ended_at"] = iso(self.ended_at)
        d["corrected_at"] = iso(self.corrected_at)
        # _enum_value rather than .value: serialisation must not be the thing
        # that loses a transmission. A field that somehow holds None or a bare
        # string still exports, as its default, instead of raising and taking
        # the whole export with it.
        d["state"] = _enum_value(self.state, ProcessingState.CAPTURED)
        d["content_class"] = _enum_value(self.content_class,
                                         ContentClass.UNKNOWN)
        d["source_language_mode"] = _enum_value(
            self.source_language_mode, SourceLanguageMode.AUTOMATIC)
        for field in ("frequency_provenance", "channel_provenance",
                      "rssi_provenance", "snr_provenance",
                      "modulation_provenance",
                      "squelch_code_provenance",
                      "talkgroup_provenance", "unit_id_provenance",
                      "protocol_provenance"):
            d[field] = _enum_value(getattr(self, field), Provenance.UNKNOWN)
        d["analysis_attempts"] = [a.to_dict() for a in self.analysis_attempts]
        d["frequency_is_measured"] = self.frequency_is_measured
        d["error"] = self.error.to_dict() if self.error else None
        d["display_transcript"] = self.display_transcript
        d["display_translation"] = self.display_translation
        d["needs_review"] = self.needs_review
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Transmission":
        d = dict(d)
        for derived in ("display_transcript", "display_translation", "needs_review"):
            d.pop(derived, None)
        d["started_at"] = parse_iso(d.get("started_at")) or utcnow()
        d["ended_at"] = parse_iso(d.get("ended_at"))
        d["corrected_at"] = parse_iso(d.get("corrected_at"))
        # A falsy stored value means "nothing was recorded", which must fall
        # back to the field's declared default. Leaving it in place passed the
        # raw None or "" straight into the constructor, overriding the default
        # with something that is not a member of the enum at all - and the
        # next to_dict() on that object raised AttributeError, so an event
        # stored before classification could not be exported or shown.
        _coerce_enum(d, "state", ProcessingState)
        _coerce_enum(d, "content_class", ContentClass)
        for field in ("frequency_provenance", "channel_provenance",
                      "rssi_provenance", "snr_provenance",
                      "modulation_provenance",
                      "squelch_code_provenance",
                      "talkgroup_provenance", "unit_id_provenance",
                      "protocol_provenance"):
            _coerce_enum(d, field, Provenance)
        d["analysis_attempts"] = [
            AnalysisAttempt.from_dict(a) if isinstance(a, dict) else a
            for a in d.get("analysis_attempts") or []]
        d.pop("frequency_is_measured", None)
        _coerce_enum(d, "source_language_mode", SourceLanguageMode)
        if d.get("error"):
            d["error"] = ErrorInfo.from_dict(d["error"])
        d["transcript_segments"] = [
            TranscriptSegment(**s) if isinstance(s, dict) else s
            for s in d.get("transcript_segments") or []
        ]
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# --- stock sign-offs: recognition output with no speech behind it ----------------
#
# Fed room noise, speech recognisers trained on subtitled video often return a
# stock video sign-off ("Thanks for watching!", "Takk for at du så på.",
# "Please subscribe", subtitle credits). The words alone never decide: each
# tier is judged against the local speech evidence recorded with the message
# (voice-detector seconds, the engine's no-speech estimate and confidence), so
# a sign-off genuinely spoken over the radio - clear speech, confident engine -
# is kept, and a short real "thank you" is withheld only when the evidence says
# nothing was spoken.

import re as _re
import unicodedata as _ud

#: Video sign-offs and subtitle credits: withheld unless speech is clearly
#: evident.
_SIGNOFF_PATTERNS = [_re.compile(p) for p in (
    r"\b(thank you|thanks|thank u)( (so|very) much)? (for|4) (watching|viewing)\b",
    r"\b(please )?(like (and|&) )?subscribe\b",
    r"\bsubtitles? (by|created by)( the)? amara ?org( community)?\b",
    r"\bamara ?org( community)?\b", r"\buntertitel\b",
    r"\b(tusen )?takk for (at (du|dere) (så|ser|har sett) (på)?|å se|å ha sett)\b",
    r"\bdanke (fürs|für's|für das) zuschauen\b",
    r"\bmerci d avoir regardé\b", r"\bgracias por (ver|mirar)\b",
    r"\bobrigad[oa] por (assistir|ver)\b", r"\bgrazie per la visione\b",
    r"\bспасибо за просмотр\b", r"ご視聴ありがとうございました",
)]
#: A bare thanks on its own: withheld only when the evidence says no speech.
_BARE_THANKS = _re.compile(
    r"^((thank you|thanks|thank u)( (so|very) much)?|(tusen )?takk|danke( schön)?|"
    r"merci( beaucoup)?|gracias|obrigad[oa]|grazie|спасибо|ありがとうございました)$")


def _normal(text: str) -> str:
    text = _ud.normalize("NFC", text or "").lower().replace("\u2019", "'")
    text = _re.sub(r"[^\w\s']+", " ", text).replace("'", " ")
    return " ".join(text.split())


def stock_signoff(text: str) -> str:
    """``"signoff"``, ``"thanks"`` or ``""`` for a whole transcript.

    Only a transcript that is essentially just the stock phrase counts (at
    most two other words, twelve in all): a real message that happens to
    contain "thank you" is never matched."""
    words = _normal(text)
    if not words:
        return ""
    if _BARE_THANKS.match(words):
        return "thanks"
    count = len(words.split())
    for pattern in _SIGNOFF_PATTERNS:
        match = pattern.search(words)
        if match and count <= 12:
            rest = (words[:match.start()] + " " + words[match.end():]).split()
            if len(rest) <= 2:
                return "signoff"
    return ""


def signoff_is_noise(kind: str, seconds=None, ratio=None, nsp=None,
                     confidence=None) -> bool:
    """Judge a stock phrase against the local speech evidence.

    A video sign-off is kept only with clear speech: the voice detector found
    at least a second (or 30% of the clip), the engine's no-speech estimate is
    under 0.3 and its confidence at least 0.6. A bare thanks is withheld only
    when the evidence says no speech: under a quarter second of voice, or a
    no-speech estimate of 0.4 or more. With no evidence at all, a sign-off is
    withheld and a bare thanks is kept."""
    if kind == "signoff":
        voiced = (seconds >= 1.0 if seconds is not None
                  else ratio >= 0.3 if ratio is not None else False)
        return not (voiced and (nsp is None or nsp < 0.3)
                    and (confidence is None or confidence >= 0.6))
    if kind == "thanks":
        return bool((seconds is not None and seconds < 0.25)
                    or (seconds is None and ratio is not None and ratio < 0.02)
                    or (nsp is not None and nsp >= 0.4))
    return False
