"""Core data model for a single received transmission."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import uuid
from typing import Any, Dict, List, Optional


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclasses.dataclass
class DecodeResult:
    """Output of one digital decoder run over a transmission's audio."""

    decoder: str
    """Short decoder id, e.g. ``ctcss``, ``dtmf``, ``aprs``."""

    label: str
    """Human readable summary, e.g. ``PL 100.0 Hz`` or ``DTMF: 1234#``."""

    confidence: float = 0.0
    """0..1. Decoders that verify a CRC report 1.0."""

    data: Dict[str, Any] = dataclasses.field(default_factory=dict)
    """Structured payload, decoder specific."""

    offset: float = 0.0
    """Seconds from the start of the transmission where the decode begins."""

    duration: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Transcript:
    text: str = ""
    language: Optional[str] = None
    """BCP-47-ish language code as detected by the ASR backend (e.g. ``es``)."""

    language_confidence: float = 0.0
    engine: str = ""
    segments: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    no_speech_prob: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Translation:
    text: str = ""
    target_language: str = ""
    source_language: Optional[str] = None
    engine: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Transmission:
    """One keyed-up transmission captured from the air.

    A transmission is the unit of everything BabelFishR does: it is recorded to
    disk, decoded, transcribed, translated and catalogued as a whole.
    """

    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:16])
    started_at: _dt.datetime = dataclasses.field(default_factory=utcnow)
    duration: float = 0.0

    # Where it came from.
    source: str = ""
    """Source URI the audio arrived on, e.g. ``soundcard:2`` or ``rtlsdr:0``."""

    band: str = ""
    """Band plan id being observed, e.g. ``gmrs``, ``vhf-ham-2m``."""

    channel: str = ""
    """Channel/label within the band plan, e.g. ``GMRS 16``."""

    frequency_hz: Optional[float] = None

    # Signal characteristics.
    sample_rate: int = 0
    peak_dbfs: float = -120.0
    snr_db: Optional[float] = None
    rssi_dbm: Optional[float] = None
    modulation: str = "nfm"

    # Products.
    audio_path: Optional[str] = None
    decodes: List[DecodeResult] = dataclasses.field(default_factory=list)
    transcript: Optional[Transcript] = None
    translation: Optional[Translation] = None

    # Classification: "voice", "data", "digital-voice", "noise", "unknown".
    kind: str = "unknown"
    notes: str = ""

    @property
    def ended_at(self) -> _dt.datetime:
        return self.started_at + _dt.timedelta(seconds=self.duration)

    def decoder_tags(self) -> List[str]:
        seen: List[str] = []
        for d in self.decodes:
            if d.decoder not in seen:
                seen.append(d.decoder)
        return seen

    def display_text(self) -> str:
        """Best single line describing this transmission for a log view."""
        if self.translation and self.translation.text:
            return self.translation.text
        if self.transcript and self.transcript.text:
            return self.transcript.text
        if self.decodes:
            return "; ".join(d.label for d in self.decodes[:3])
        return "(no decode)"

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["started_at"] = _iso(self.started_at)
        d["ended_at"] = _iso(self.ended_at)
        d["display_text"] = self.display_text()
        d["decoder_tags"] = self.decoder_tags()
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Transmission":
        d = dict(d)
        d.pop("ended_at", None)
        d.pop("display_text", None)
        d.pop("decoder_tags", None)
        started = d.get("started_at")
        if isinstance(started, str):
            d["started_at"] = _dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
        d["decodes"] = [DecodeResult(**x) for x in d.get("decodes") or []]
        if d.get("transcript"):
            d["transcript"] = Transcript(**d["transcript"])
        if d.get("translation"):
            d["translation"] = Translation(**d["translation"])
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
