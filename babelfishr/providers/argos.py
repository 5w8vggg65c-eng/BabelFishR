"""Offline translation via Argos Translate.

Fully local: language pairs are downloaded once, then translation runs on the
machine with nothing leaving it.  Quality is below a frontier model's, but for
an operator who cannot send traffic to a third party it is the right default.

Argos cannot be instructed, so the glossary is applied mechanically: protected
terms are swapped for placeholders before translation and restored afterwards,
and preferred renderings are substituted in the output.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import (EngineError, EngineUnavailable, PrivacyProfile,
                   TranslationEngine, TranslationResult)
from .glossary import protect_terms, restore_terms

log = logging.getLogger(__name__)

#: Short phrases used to prove a translation path really runs.
_SMOKE_PHRASES = {
    "es": "el equipo esta en posicion",
    "de": "das team ist in position",
    "fr": "l equipe est en position",
    "en": "the team is in position",
    "it": "la squadra e in posizione",
    "pt": "a equipe esta em posicao",
    "uk": "команда на позиції",
    "ru": "команда на позиции",
}


class ArgosTranslateEngine(TranslationEngine):
    """Local neural MT. Requires the language pair to be installed."""

    id = "argos"
    name = "Argos Translate (local)"
    privacy = PrivacyProfile()  # local

    def __init__(self, auto_install: bool = False,
                 target_language: Optional[str] = None,
                 package_dir: Optional[str] = None):
        self.auto_install = auto_install
        #: The session's target language, so availability can mean "can
        #: actually translate into the language the operator asked for".
        self.target_language = target_language
        self.package_dir = package_dir
        self.version = "argos"
        self._checked_pairs: Dict[Tuple[str, str], bool] = {}

    def library_installed(self) -> bool:
        try:
            import argostranslate.translate  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def available(self) -> bool:
        """Installed AND holding at least one usable language pair.

        Reporting availability from a successful import is what allowed a
        field install with no language packs to look ready and then fail on
        the first transmission.
        """
        if not self.library_installed():
            return False
        pairs = self.installed_pairs()
        if not pairs:
            return False
        if self.target_language:
            return any(target == self.target_language for _, target in pairs)
        return True

    def unavailable_reason(self) -> str:
        if self.available():
            return ""
        if not self.library_installed():
            return ("Argos Translate is not installed. Install the translate "
                    "extra:\n    pip install 'babelfishr[translate]'")
        pairs = self.installed_pairs()
        if not pairs:
            return ("Argos Translate is installed but no language packages are. "
                    "Install one with a network connection:\n"
                    "    babelfishr languages install es en")
        return (f"No installed Argos package translates into "
                f"{self.target_language!r}. Installed paths: "
                f"{', '.join(f'{a}->{b}' for a, b in sorted(pairs))}\n"
                f"    babelfishr languages install <source> "
                f"{self.target_language}")

    def readiness(self) -> Dict[str, object]:
        """Structured view for Field Check."""
        return {
            "library_installed": self.library_installed(),
            "pairs": sorted(self.installed_pairs()),
            "target_language": self.target_language,
            "available": self.available(),
            "reason": self.unavailable_reason(),
        }

    def smoke_test(self, source: str, target: str) -> "TranslationResult":
        """Actually translate a fixed phrase, to prove the path works."""
        return self.translate(_SMOKE_PHRASES.get(source, "hello"), target,
                              source_language=source)

    # -- pair management -------------------------------------------------
    def installed_pairs(self) -> List[Tuple[str, str]]:
        try:
            import argostranslate.translate as translate
        except Exception:  # noqa: BLE001
            return []
        pairs: List[Tuple[str, str]] = []
        for language in translate.get_installed_languages():
            for target in getattr(language, "translations_from", []):
                pairs.append((language.code, target.to_lang.code))
        return pairs

    def supports_pair(self, source: Optional[str], target: str) -> bool:
        if source is None:
            return True  # cannot know until the language is detected
        if source == target:
            return True
        key = (source, target)
        if key not in self._checked_pairs:
            self._checked_pairs[key] = key in set(self.installed_pairs())
        return self._checked_pairs[key]

    def install_pair(self, source: str, target: str) -> bool:
        """Download and install a language pair. Returns True on success."""
        try:
            import argostranslate.package as package
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(self.unavailable_reason()) from exc
        try:
            package.update_package_index()
            for candidate in package.get_available_packages():
                if candidate.from_code == source and candidate.to_code == target:
                    package.install_from_path(candidate.download())
                    self._checked_pairs.pop((source, target), None)
                    return True
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"could not install {source}->{target}: {exc}") from exc
        return False

    # -- translation -----------------------------------------------------
    def translate(self, text: str, target_language: str, *,
                  source_language: Optional[str] = None,
                  glossary: Optional[Dict[str, str]] = None,
                  do_not_translate: Optional[Sequence[str]] = None,
                  ) -> TranslationResult:
        try:
            import argostranslate.translate as translate
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(self.unavailable_reason()) from exc

        if not text.strip():
            return TranslationResult(text="", source_language=source_language,
                                     target_language=target_language,
                                     engine=self.id, engine_version=self.version)
        if source_language and source_language == target_language:
            return TranslationResult(
                text=text, source_language=source_language,
                target_language=target_language, engine=self.id,
                engine_version=self.version, untranslated=True)
        if source_language is None:
            raise EngineError(
                "Argos needs a source language; enable language detection or set "
                "the session's source language explicitly")

        if not self.supports_pair(source_language, target_language):
            if self.auto_install and self.install_pair(source_language, target_language):
                pass
            else:
                raise EngineError(
                    f"no Argos language pair installed for "
                    f"{source_language} -> {target_language}. Install it with "
                    f"'babelfishr languages --install {source_language} "
                    f"{target_language}'.")

        protected, mapping = protect_terms(text, list(do_not_translate or ()))
        try:
            translated = translate.translate(protected, source_language,
                                             target_language)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"argos translation failed: {exc}") from exc

        translated = restore_terms(translated, mapping)
        for term, preferred in (glossary or {}).items():
            if preferred:
                translated = translated.replace(term, preferred)

        return TranslationResult(
            text=translated.strip(), source_language=source_language,
            target_language=target_language, engine=self.id,
            engine_version=self.version)
