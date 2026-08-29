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
import uuid
from typing import Any, Dict, List, Optional


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
    """One monitoring run: a start, an end, and the transmissions between."""

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
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Session":
        d = dict(d)
        d.pop("duration", None)
        d["started_at"] = parse_iso(d.get("started_at")) or utcnow()
        d["ended_at"] = parse_iso(d.get("ended_at"))
        if d.get("source_language_mode"):
            d["source_language_mode"] = SourceLanguageMode(d["source_language_mode"])
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


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
    modulation: str = ""
    modulation_provenance: Provenance = Provenance.UNKNOWN

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
    notes: str = ""
    tags: List[str] = dataclasses.field(default_factory=list)
    bookmarked: bool = False
    reviewed: bool = False

    # ---- derived ------------------------------------------------------
    @property
    def display_transcript(self) -> str:
        """What the UI shows as the original-language text."""
        return self.transcript_correction or self.transcript

    @property
    def display_translation(self) -> str:
        return self.translation_correction or self.translation

    @property
    def has_correction(self) -> bool:
        return bool(self.transcript_correction or self.translation_correction)

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

    @property
    def worth_digital_analysis(self) -> bool:
        return self.content_class in (ContentClass.DIGITAL_SUSPECTED,
                                      ContentClass.NOISE, ContentClass.UNKNOWN)

    @property
    def needs_review(self) -> bool:
        """Low confidence, or a failure the operator should look at."""
        if self.state is ProcessingState.FAILED:
            return True
        if self.reviewed:
            return False
        thresholds = [v for v in (self.transcript_confidence,
                                  self.language_confidence) if v is not None]
        return any(v < 0.6 for v in thresholds)

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
        d["state"] = self.state.value
        d["content_class"] = self.content_class.value
        d["source_language_mode"] = self.source_language_mode.value
        for field in ("frequency_provenance", "channel_provenance",
                      "rssi_provenance", "modulation_provenance"):
            d[field] = getattr(self, field).value
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
        if d.get("state"):
            d["state"] = ProcessingState(d["state"])
        if d.get("content_class"):
            d["content_class"] = ContentClass(d["content_class"])
        for field in ("frequency_provenance", "channel_provenance",
                      "rssi_provenance", "modulation_provenance"):
            if d.get(field):
                d[field] = Provenance(d[field])
        d["analysis_attempts"] = [
            AnalysisAttempt.from_dict(a) if isinstance(a, dict) else a
            for a in d.get("analysis_attempts") or []]
        d.pop("frequency_is_measured", None)
        if d.get("source_language_mode"):
            d["source_language_mode"] = SourceLanguageMode(d["source_language_mode"])
        if d.get("error"):
            d["error"] = ErrorInfo.from_dict(d["error"])
        d["transcript_segments"] = [
            TranscriptSegment(**s) if isinstance(s, dict) else s
            for s in d.get("transcript_segments") or []
        ]
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
