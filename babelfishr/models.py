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
