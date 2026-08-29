"""Engine registry and selection.

Selection is deliberately honest: ``auto`` picks the best engine that is
*actually usable right now*, and if that turns out to be a mock, the mock says
so.  Nothing here ever presents a placeholder as a working transcription or
translation path.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Callable, Dict, List, Optional

from .base import (Engine, EngineError, EngineUnavailable,
                   LanguageDetectionEngine, PrivacyProfile, TranscriptionEngine,
                   TranscriptionResult, TranslationEngine, TranslationResult)
from .glossary import Glossary, GlossaryEntry, protect_terms, restore_terms
from .mock import (MockLanguageDetectionEngine, MockTranscriptionEngine,
                   MockTranslationEngine)

log = logging.getLogger(__name__)

__all__ = [
    "Engine", "EngineError", "EngineUnavailable", "LanguageDetectionEngine",
    "PrivacyProfile", "TranscriptionEngine", "TranscriptionResult",
    "TranslationEngine", "TranslationResult", "Glossary", "GlossaryEntry",
    "protect_terms", "restore_terms", "MockTranscriptionEngine",
    "MockTranslationEngine", "MockLanguageDetectionEngine",
    "build_transcription_engine", "build_translation_engine",
    "transcription_engine_status", "translation_engine_status", "EngineStatus",
]

#: Preference order for ``auto``. Real engines first, mock only as a last resort.
TRANSCRIPTION_PREFERENCE = ("faster-whisper", "mock")
TRANSLATION_PREFERENCE = ("argos", "claude", "mock")


@dataclasses.dataclass
class EngineStatus:
    id: str
    name: str
    available: bool
    reason: str = ""
    is_placeholder: bool = False
    privacy: str = ""

    def to_dict(self) -> Dict[str, object]:
        return dataclasses.asdict(self)


def _transcription_factories(config=None) -> Dict[str, Callable[[], TranscriptionEngine]]:
    from .whisper_local import FasterWhisperEngine

    asr = getattr(config, "asr", None)
    return {
        "faster-whisper": lambda: FasterWhisperEngine(
            model=getattr(asr, "model", "small"),
            device=getattr(asr, "device", "auto"),
            compute_type=getattr(asr, "compute_type", "default"),
            beam_size=getattr(asr, "beam_size", 5),
        ),
        "mock": MockTranscriptionEngine,
    }


def _translation_factories(config=None) -> Dict[str, Callable[[], TranslationEngine]]:
    from .argos import ArgosTranslateEngine
    from .claude import ClaudeTranslationEngine

    translate = getattr(config, "translate", None)
    return {
        "argos": ArgosTranslateEngine,
        "claude": lambda: ClaudeTranslationEngine(
            model=getattr(translate, "model", None) or None),
        "mock": MockTranslationEngine,
    }


def _build(kind: str, requested: str, factories: Dict[str, Callable[[], Engine]],
           preference) -> Engine:
    requested = (requested or "auto").strip().lower()

    if requested == "none":
        raise EngineUnavailable(f"{kind} is disabled in the configuration")

    if requested != "auto":
        factory = factories.get(requested)
        if factory is None:
            raise EngineUnavailable(
                f"unknown {kind} engine {requested!r}; known: "
                f"{', '.join(sorted(factories))}")
        engine = factory()
        if not engine.available():
            # Explicitly requested: fail loudly rather than silently downgrade.
            raise EngineUnavailable(engine.unavailable_reason())
        return engine

    for name in preference:
        factory = factories.get(name)
        if factory is None:
            continue
        try:
            engine = factory()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not construct %s engine %s: %s", kind, name, exc)
            continue
        if engine.available():
            if name == "mock":
                log.warning(
                    "no real %s engine is installed; using the mock engine. "
                    "Output will be placeholder text, not a real %s.", kind, kind)
            return engine
    raise EngineUnavailable(f"no {kind} engine is available")


def build_transcription_engine(config=None,
                               requested: Optional[str] = None) -> TranscriptionEngine:
    asr = getattr(config, "asr", None)
    choice = requested or getattr(asr, "engine", "auto")
    return _build("transcription", choice, _transcription_factories(config),
                  TRANSCRIPTION_PREFERENCE)  # type: ignore[return-value]


def build_translation_engine(config=None,
                             requested: Optional[str] = None) -> TranslationEngine:
    translate = getattr(config, "translate", None)
    choice = requested or getattr(translate, "engine", "auto")
    return _build("translation", choice, _translation_factories(config),
                  TRANSLATION_PREFERENCE)  # type: ignore[return-value]


def _status(factories: Dict[str, Callable[[], Engine]]) -> List[EngineStatus]:
    out: List[EngineStatus] = []
    for name, factory in factories.items():
        try:
            engine = factory()
        except Exception as exc:  # noqa: BLE001
            out.append(EngineStatus(id=name, name=name, available=False,
                                    reason=str(exc)))
            continue
        out.append(EngineStatus(
            id=engine.id, name=engine.name, available=engine.available(),
            reason=engine.unavailable_reason(), is_placeholder=(name == "mock"),
            privacy=engine.privacy.describe(),
        ))
    return out


def transcription_engine_status(config=None) -> List[EngineStatus]:
    return _status(_transcription_factories(config))


def translation_engine_status(config=None) -> List[EngineStatus]:
    return _status(_translation_factories(config))


def is_placeholder(engine: Engine) -> bool:
    return getattr(engine, "id", "") == "mock"
