"""Engine registry and selection.

Selection is deliberately honest: ``auto`` picks the best engine that is
*actually usable right now*, and if that turns out to be a mock, the mock says
so.  Nothing here ever presents a placeholder as a working transcription or
translation path.

Selection is also mode-aware, which is what makes Field Offline enforceable:

* a cloud engine is never *constructed* outside Online/Setup - not merely never
  called, never built, so there is no object that could be invoked by accident;
* a mock engine is never selected outside Online/Setup, so field output is
  never placeholder text;
* a missing local engine produces an honest failure and never falls through to
  either of the above.

The last point is the one that matters most: nothing leaves the Mac merely
because the local translation engine is missing.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ..modes import OperatingMode

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

#: Preference order for ``auto``. Real local engines first; a cloud engine is
#: never reached by ``auto`` - it must be named explicitly, so that audio or
#: text never leaves the machine because of a default.
TRANSCRIPTION_PREFERENCE = ("faster-whisper", "mock")
TRANSLATION_PREFERENCE = ("argos", "mock")

#: Engines that send data off the machine. Only ever selected by exact name.
CLOUD_ENGINES = frozenset({"claude"})
PLACEHOLDER_ENGINES = frozenset({"mock"})


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


def _transcription_factories(config=None, mode=None
                             ) -> Dict[str, Callable[[], TranscriptionEngine]]:
    from .whisper_local import FasterWhisperEngine

    from ..modes import AppPaths, OperatingMode

    asr = getattr(config, "asr", None)
    paths = AppPaths.resolve(getattr(config, "app_home", None))
    mode = _mode_of(config, mode)
    return {
        "faster-whisper": lambda: FasterWhisperEngine(
            model=getattr(asr, "model", "small"),
            device=getattr(asr, "device", "auto"),
            compute_type=getattr(asr, "compute_type", "default"),
            beam_size=getattr(asr, "beam_size", 5),
            models_root=str(paths.models),
            model_path=getattr(asr, "model_path", None) or None,
            # Downloads only ever happen in preparation mode.
            local_files_only=not mode.allows_downloads,
        ),
        "mock": MockTranscriptionEngine,
    }


def _translation_factories(config=None, mode=None
                           ) -> Dict[str, Callable[[], TranslationEngine]]:
    from .argos import ArgosTranslateEngine
    from .claude import ClaudeTranslationEngine

    translate = getattr(config, "translate", None)
    return {
        "argos": lambda: ArgosTranslateEngine(
            target_language=getattr(translate, "target_language", None)),
        "claude": lambda: ClaudeTranslationEngine(
            model=getattr(translate, "model", None) or None),
        "mock": MockTranslationEngine,
    }


def _check_mode(kind: str, name: str, mode: "OperatingMode") -> None:
    """Refuse to even construct an engine the current mode forbids."""
    from ..modes import guard_cloud, guard_mock

    if name in CLOUD_ENGINES:
        guard_cloud(mode, f"the {kind} engine {name!r}")
    if name in PLACEHOLDER_ENGINES:
        guard_mock(mode, f"the {kind} engine {name!r}")


def _build(kind: str, requested: str, factories: Dict[str, Callable[[], Engine]],
           preference, mode: Optional["OperatingMode"] = None) -> Engine:
    from ..modes import OperatingMode, OfflineViolation

    mode = mode or OperatingMode.ONLINE_SETUP
    requested = (requested or "auto").strip().lower()

    if requested == "none":
        raise EngineUnavailable(f"{kind} is disabled in the configuration")

    if requested != "auto":
        factory = factories.get(requested)
        if factory is None:
            raise EngineUnavailable(
                f"unknown {kind} engine {requested!r}; known: "
                f"{', '.join(sorted(factories))}")
        # Mode check before construction: a forbidden engine must not exist as
        # an object at all in this process.
        try:
            _check_mode(kind, requested, mode)
        except OfflineViolation as exc:
            raise EngineUnavailable(str(exc)) from exc
        engine = factory()
        if not engine.available():
            # Explicitly requested: fail loudly rather than silently downgrade.
            raise EngineUnavailable(engine.unavailable_reason())
        return engine

    reasons: List[str] = []
    for name in preference:
        factory = factories.get(name)
        if factory is None:
            continue
        try:
            _check_mode(kind, name, mode)
        except OfflineViolation:
            # Not an error: auto simply never selects it in this mode.
            continue
        try:
            engine = factory()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not construct %s engine %s: %s", kind, name, exc)
            continue
        if engine.available():
            if name in PLACEHOLDER_ENGINES:
                log.warning(
                    "no real %s engine is installed; using the mock engine. "
                    "Output will be placeholder text, not a real %s.", kind, kind)
            return engine
        reasons.append(f"{name}: {engine.unavailable_reason().splitlines()[0]}")

    detail = ("\n  " + "\n  ".join(reasons)) if reasons else ""
    raise EngineUnavailable(
        f"no {kind} engine is available in {mode.label} mode.{detail}\n"
        f"Recordings are still captured; run 'babelfishr field-check' for "
        f"details.")


def _mode_of(config, override=None):
    from ..modes import OperatingMode

    if override is not None:
        return override
    value = getattr(config, "mode", None)
    if isinstance(value, OperatingMode):
        return value
    if isinstance(value, str) and value:
        return OperatingMode(value)
    return OperatingMode.ONLINE_SETUP


def build_transcription_engine(config=None, requested: Optional[str] = None,
                               mode=None) -> TranscriptionEngine:
    asr = getattr(config, "asr", None)
    choice = requested or getattr(asr, "engine", "auto")
    resolved = _mode_of(config, mode)
    return _build("transcription", choice,
                  _transcription_factories(config, resolved),
                  TRANSCRIPTION_PREFERENCE, resolved)  # type: ignore[return-value]


def build_translation_engine(config=None, requested: Optional[str] = None,
                             mode=None) -> TranslationEngine:
    translate = getattr(config, "translate", None)
    choice = requested or getattr(translate, "engine", "auto")
    resolved = _mode_of(config, mode)
    return _build("translation", choice, _translation_factories(config, resolved),
                  TRANSLATION_PREFERENCE, resolved)  # type: ignore[return-value]


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
