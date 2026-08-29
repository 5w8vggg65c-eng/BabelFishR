"""Deterministic mock engines.

The whole test suite runs on these: no model downloads, no API keys, no
network, and identical output on every run.  They are also useful as a
first-run default so the application is demonstrably wired end to end before
anyone installs a multi-gigabyte model.
"""

from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Sequence

import numpy as np

from ..models import TranscriptSegment
from .base import (EngineError, LanguageDetectionEngine, PrivacyProfile,
                   TranscriptionEngine, TranscriptionResult, TranslationEngine,
                   TranslationResult)

#: Canned phrases, chosen so tests can assert on language handling.
_PHRASES = [
    ("es", "equipo uno en posicion esperando instrucciones"),
    ("en", "roger that, moving to the north entrance"),
    ("de", "achtung strassensperre voraus bitte umleiten"),
    ("fr", "message recu nous sommes en route"),
    ("en", "radio check, how do you read me"),
    ("uk", "прийом, ми на позиції"),
]

#: Word-for-word stand-in translations. Not real translation - just stable.
_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        "equipo uno en posicion esperando instrucciones":
            "team one in position awaiting instructions",
        "achtung strassensperre voraus bitte umleiten":
            "attention roadblock ahead please divert",
        "message recu nous sommes en route":
            "message received we are on the way",
        "прийом, ми на позиції": "receiving, we are in position",
    },
}


def _seed_for(audio: np.ndarray) -> int:
    """Stable seed from the audio itself, so the same clip always maps the same."""
    digest = hashlib.sha256(np.asarray(audio, dtype=np.float32).tobytes()).digest()
    return int.from_bytes(digest[:4], "big")


class MockTranscriptionEngine(TranscriptionEngine):
    """Returns a canned phrase chosen deterministically from the audio."""

    id = "mock"
    name = "Mock transcription"
    version = "1"
    detects_language = True
    privacy = PrivacyProfile()

    def __init__(self, fail: bool = False, fail_message: str = "mock failure",
                 confidence: float = 0.9, phrases: Optional[Sequence] = None):
        self.fail = fail
        self.fail_message = fail_message
        self.confidence = confidence
        self.phrases = list(phrases or _PHRASES)
        self.calls = 0

    def available(self) -> bool:
        return True

    def transcribe(self, audio: np.ndarray, sample_rate: int, *,
                   language: Optional[str] = None,
                   vocabulary: Optional[Sequence[str]] = None,
                   ) -> TranscriptionResult:
        self.calls += 1
        if self.fail:
            raise EngineError(self.fail_message)
        audio = np.asarray(audio, dtype=np.float64)
        if audio.size == 0:
            return TranscriptionResult(engine=self.id, engine_version=self.version,
                                       no_speech=True)
        detected, text = self.phrases[_seed_for(audio) % len(self.phrases)]
        if language:
            detected = language
        duration = audio.size / float(sample_rate or 1)
        if vocabulary:
            # Mimic a recogniser biased by the operator's vocabulary.
            text = f"{vocabulary[0]} {text}"
        return TranscriptionResult(
            text=text, language=detected, language_confidence=0.95,
            confidence=self.confidence,
            segments=[TranscriptSegment(0.0, duration, text, self.confidence)],
            engine=self.id, engine_version=self.version,
        )


class MockTranslationEngine(TranslationEngine):
    """Looks up a canned translation, or tags the text so tests can see it ran."""

    id = "mock"
    name = "Mock translation"
    version = "1"
    privacy = PrivacyProfile()

    def __init__(self, fail: bool = False, fail_message: str = "mock failure"):
        self.fail = fail
        self.fail_message = fail_message
        self.calls = 0

    def available(self) -> bool:
        return True

    def translate(self, text: str, target_language: str, *,
                  source_language: Optional[str] = None,
                  glossary: Optional[Dict[str, str]] = None,
                  do_not_translate: Optional[Sequence[str]] = None,
                  ) -> TranslationResult:
        self.calls += 1
        if self.fail:
            raise EngineError(self.fail_message)
        if source_language and source_language == target_language:
            return TranslationResult(
                text=text, source_language=source_language,
                target_language=target_language, engine=self.id,
                engine_version=self.version, untranslated=True)

        table = _TRANSLATIONS.get(target_language, {})
        translated = table.get(text.strip().lower())
        if translated is None:
            translated = f"[{target_language}] {text}"
        for term, preferred in (glossary or {}).items():
            translated = translated.replace(term, preferred)
        for term in do_not_translate or ():
            if term.lower() in text.lower() and term not in translated:
                translated = f"{translated} ({term})"
        return TranslationResult(
            text=translated, source_language=source_language,
            target_language=target_language, engine=self.id,
            engine_version=self.version)


class MockLanguageDetectionEngine(LanguageDetectionEngine):
    id = "mock"
    name = "Mock language detection"
    version = "1"

    #: Tiny stopword table - enough to be deterministic and vaguely plausible.
    _MARKERS = {
        "es": (" el ", " la ", " en ", " que ", "posicion"),
        "de": (" der ", " die ", " und ", "achtung", "strasse"),
        "fr": (" le ", " nous ", " est ", "recu", "route"),
        "en": (" the ", " that ", " we ", "roger", "radio"),
    }

    def available(self) -> bool:
        return True

    def detect(self, text: str) -> tuple:
        padded = f" {text.lower()} "
        scores = {lang: sum(1 for m in markers if m in padded)
                  for lang, markers in self._MARKERS.items()}
        best = max(scores, key=lambda k: scores[k])
        if scores[best] == 0:
            return ("en", 0.3)
        total = sum(scores.values()) or 1
        return (best, round(min(0.95, 0.5 + scores[best] / (total + 1)), 2))
