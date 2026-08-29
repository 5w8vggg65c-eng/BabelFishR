"""Engine interfaces.

Transcription, translation and language detection are pluggable.  The
application talks only to these protocols, so a local engine, a cloud engine or
a deterministic test double are interchangeable.

Privacy is part of the interface, not an afterthought: every engine declares
whether it sends audio or text off the machine, and the UI is required to show
that before a session starts.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

import numpy as np

from ..models import TranscriptSegment


class EngineUnavailable(RuntimeError):
    """The engine cannot run: missing dependency, model, or credentials.

    Raised at *selection* time. Once an engine reports available, a failure
    during processing raises :class:`EngineError` instead, which the pipeline
    turns into a recoverable per-transmission error state.
    """


class EngineError(RuntimeError):
    """A processing failure. The transmission's audio is always preserved."""


@dataclasses.dataclass
class TranscriptionResult:
    text: str = ""
    language: Optional[str] = None
    language_confidence: Optional[float] = None
    confidence: Optional[float] = None
    segments: List[TranscriptSegment] = dataclasses.field(default_factory=list)
    engine: str = ""
    engine_version: str = ""
    no_speech: bool = False
    """True when the engine believes there was no speech at all."""

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        return d


@dataclasses.dataclass
class TranslationResult:
    text: str = ""
    source_language: Optional[str] = None
    target_language: str = ""
    engine: str = ""
    engine_version: str = ""
    untranslated: bool = False
    """True when source and target matched and the text was passed through."""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class PrivacyProfile:
    """What an engine sends where. Surfaced in the UI before a session starts."""

    is_cloud: bool = False
    sends_audio: bool = False
    sends_text: bool = False
    destination: str = "this computer"

    def describe(self) -> str:
        if not self.is_cloud:
            return "Runs locally. Nothing leaves this computer."
        what = []
        if self.sends_audio:
            what.append("recorded audio")
        if self.sends_text:
            what.append("transcript text")
        payload = " and ".join(what) or "data"
        return f"Sends {payload} to {self.destination}."


LOCAL_PRIVACY = PrivacyProfile()


class Engine(abc.ABC):
    """Shared behaviour for all engines."""

    id: str = "engine"
    name: str = "Engine"
    version: str = "0"
    privacy: PrivacyProfile = LOCAL_PRIVACY

    @abc.abstractmethod
    def available(self) -> bool:
        """True when this engine can actually run right now."""

    def unavailable_reason(self) -> str:
        return "" if self.available() else f"{self.name} is not available"

    def warm_up(self) -> None:
        """Optional: load models ahead of the first transmission."""

    def close(self) -> None:
        """Optional: release models and connections."""

    def describe(self) -> str:
        return f"{self.name} - {self.privacy.describe()}"


class TranscriptionEngine(Engine):
    """Speech to text, with language detection where the engine supports it."""

    detects_language: bool = False

    @abc.abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int, *,
                   language: Optional[str] = None,
                   vocabulary: Optional[Sequence[str]] = None,
                   ) -> TranscriptionResult:
        """Transcribe one transmission.

        ``language`` forces a source language; ``None`` asks the engine to
        detect it. ``vocabulary`` is a list of terms (callsigns, place names)
        the engine may use to bias recognition.
        """


class TranslationEngine(Engine):
    """Text to text, preserving the original."""

    @abc.abstractmethod
    def translate(self, text: str, target_language: str, *,
                  source_language: Optional[str] = None,
                  glossary: Optional[Dict[str, str]] = None,
                  do_not_translate: Optional[Sequence[str]] = None,
                  ) -> TranslationResult:
        """Translate *text* into *target_language*.

        Implementations must never mutate or return the source text in place of
        a translation without setting ``untranslated``.
        """

    def supports_pair(self, source: Optional[str], target: str) -> bool:
        return True


class LanguageDetectionEngine(Engine):
    """Standalone detection, for engines whose transcription does not detect."""

    @abc.abstractmethod
    def detect(self, text: str) -> tuple:
        """Return ``(language_code, confidence)``."""


@runtime_checkable
class SupportsPartials(Protocol):
    """Optional capability: emit provisional text while a transmission runs."""

    def transcribe_partial(self, audio: np.ndarray, sample_rate: int
                           ) -> TranscriptionResult:
        ...
