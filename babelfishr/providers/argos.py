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
import os
import pathlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import (EngineError, EngineUnavailable, PrivacyProfile,
                   TranslationEngine, TranslationResult)
from .glossary import protect_terms, restore_terms

log = logging.getLogger(__name__)

#: Argos reads this at *import* time (argostranslate.settings resolves
#: package_data_dir once), so it has to be set before anything imports the
#: library. Every entry point - GUI, CLI and preparation - calls
#: configure_package_dir() before touching Argos.
PACKAGES_DIR_ENV = "ARGOS_PACKAGES_DIR"


def configure_package_dir(directory) -> bool:
    """Point Argos at the managed language-pack directory.

    Returns True if the setting will take effect. Returns False - and warns -
    if argostranslate was already imported, because by then it has resolved its
    package directory and this call would silently do nothing.
    """
    import sys

    target = pathlib.Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    current = os.environ.get(PACKAGES_DIR_ENV)
    os.environ[PACKAGES_DIR_ENV] = str(target)

    if "argostranslate.settings" in sys.modules:
        already = getattr(sys.modules["argostranslate.settings"],
                          "package_data_dir", None)
        if already is not None and pathlib.Path(already) != target:
            log.warning(
                "argostranslate was imported before %s was set; it is using %s "
                "and will ignore %s until the process restarts",
                PACKAGES_DIR_ENV, already, target)
            return False
    if current and current != str(target):
        log.info("%s changed from %s to %s", PACKAGES_DIR_ENV, current, target)
    return True


class PackageIndexUnavailable(EngineError):
    """The upstream package index could not be retrieved or read.

    Its own type because the answer is specific: this is not "no such language
    pair", it is "we never got the catalogue", and the operator needs the
    underlying cause - usually a certificate or connectivity failure - rather
    than a list of packages that failed to install.
    """


#: Result of the one index refresh this process is allowed to attempt, so five
#: requested language pairs produce one network attempt and one error rather
#: than five identical failures. Either ("ok", path) or ("failed", exception).
_INDEX_ATTEMPT: Optional[Tuple[str, Any]] = None


def reset_package_index_state() -> None:
    """Forget the cached outcome, so a later attempt really tries again.

    Called when the operator asks for preparation again - having fixed their
    network, say - and by tests.
    """
    global _INDEX_ATTEMPT
    _INDEX_ATTEMPT = None


def package_index_path() -> Optional[pathlib.Path]:
    """Where argostranslate keeps the downloaded package index, if we can tell."""
    try:
        import argostranslate.settings as settings
    except Exception:  # noqa: BLE001
        return None
    for attribute in ("local_package_index", "package_index_path",
                      "downloaded_packages_index"):
        value = getattr(settings, attribute, None)
        if value:
            return pathlib.Path(str(value))
    directory = getattr(settings, "downloads_dir", None)
    return pathlib.Path(str(directory)) / "index.json" if directory else None


def _index_is_readable(path: Optional[pathlib.Path]) -> bool:
    """True only when the index exists and parses as JSON.

    An empty or truncated file - a download that died halfway - is worse than a
    missing one, because upstream treats it as present and then fails obscurely
    later.
    """
    if path is None or not path.is_file():
        return False
    try:
        import json

        return bool(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        log.warning("package index at %s is not readable: %s", path, exc)
        return False


#: Where the Argos catalogue lives, when argostranslate does not tell us.
DEFAULT_INDEX_URL = ("https://raw.githubusercontent.com/argosopentech/"
                     "argospm-index/main/index.json")


def package_index_url() -> str:
    """The catalogue URL, preferring whatever argostranslate is configured for."""
    try:
        import argostranslate.settings as settings
    except Exception:  # noqa: BLE001
        return DEFAULT_INDEX_URL
    for attribute in ("remote_package_index", "package_index_url",
                      "remote_index_url"):
        value = getattr(settings, attribute, None)
        if value:
            return str(value)
    return DEFAULT_INDEX_URL


def fetch_package_index(url: Optional[str] = None,
                        destination: Optional[pathlib.Path] = None,
                        timeout: float = 30.0) -> pathlib.Path:
    """Fetch the catalogue ourselves, verified, and write it atomically.

    We do this rather than relying on ``update_package_index()`` because that
    function *swallows* its own errors: on the operator's Mac it caught the
    certificate failure, logged it somewhere we never saw, and returned
    normally without writing an index. All BabelFishR could then say was "the
    index was not written", which tells nobody anything.

    Doing the request here means the exception is ours to keep, so the operator
    is shown the actual cause. TLS verification is never relaxed: the request
    goes through the default HTTPS context, which
    :mod:`babelfishr.certificates` has already pointed at a real CA bundle.
    """
    import os
    import tempfile
    import urllib.request

    url = url or package_index_url()
    destination = destination or package_index_path()
    if destination is None:
        raise PackageIndexUnavailable(
            "could not determine where the Argos package index should be "
            "written")

    from ..certificates import configure_certificates

    configure_certificates()

    log.info("fetching the Argos package index from %s", url)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = response.read()

    # Parse before writing: a truncated or non-JSON body must not land on disk
    # looking like a usable catalogue.
    import json

    parsed = json.loads(payload.decode("utf-8"))
    if not parsed:
        raise PackageIndexUnavailable(
            f"the package index at {url} is empty")

    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(destination.parent),
                                         prefix=".index-", suffix=".json")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    log.info("wrote %d bytes of package index to %s", len(payload), destination)
    return destination


def refresh_package_index(force: bool = False):
    """One bounded attempt to fetch the package index. Never recurses.

    Two upstream behaviours make this necessary.

    ``get_available_packages()`` calls ``update_package_index()`` when the
    index file is missing, and that path can come straight back into
    ``get_available_packages()``. With HTTPS failing, that loop ran until the
    interpreter raised ``RecursionError: maximum recursion depth exceeded``.

    And ``update_package_index()`` catches its own network errors and returns
    without writing anything, so the real cause never reaches us at all.

    So the fetch is ours: one verified HTTPS request, parsed and written
    atomically, with the exception kept. Upstream is consulted only as a
    fallback, and ``get_available_packages()`` is never called until the index
    is known to be on disk.
    """
    global _INDEX_ATTEMPT
    if force:
        _INDEX_ATTEMPT = None
    if _INDEX_ATTEMPT is not None:
        status, value = _INDEX_ATTEMPT
        if status == "failed":
            raise value
        return value

    try:
        import argostranslate.package as package
    except Exception as exc:  # noqa: BLE001
        failure = PackageIndexUnavailable(
            "argostranslate is not installed "
            "(pip install 'babelfishr[translate]')")
        failure.__cause__ = exc
        _INDEX_ATTEMPT = ("failed", failure)
        raise failure from exc

    path = package_index_path()
    original: Optional[BaseException] = None

    # 1. Our own request, so a failure keeps its exception.
    try:
        path = fetch_package_index(destination=path)
    except PackageIndexUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        original = exc
        log.warning("could not fetch the Argos package index: %s: %s",
                    type(exc).__name__, exc)

    # 2. Upstream as a fallback, in case it can reach somewhere we cannot.
    #    Its errors are swallowed internally, so nothing is learned from it -
    #    which is exactly why it is second.
    if original is not None and not _index_is_readable(path):
        try:
            package.update_package_index()
        except RecursionError as exc:  # pragma: no cover - upstream guard
            log.error("argostranslate recursed while refreshing its package "
                      "index; treating the index as unavailable")
            original = original or exc
        except Exception as exc:  # noqa: BLE001
            original = original or exc

    if not _index_is_readable(path):
        failure = PackageIndexUnavailable(_index_failure_message(original, path))
        if original is not None:
            failure.__cause__ = original
        _INDEX_ATTEMPT = ("failed", failure)
        raise failure

    _INDEX_ATTEMPT = ("ok", path)
    return path


def _index_failure_message(original: Optional[BaseException],
                           path: Optional[pathlib.Path]) -> str:
    """An error an operator can act on, with the real cause in it."""
    from ..certificates import describe as describe_certificates

    if original is None:
        detail = (f"the index was not written to {path}"
                  if path else "the index file was not written")
        return ("Could not retrieve the Argos language package index: "
                f"{detail}.")

    name = type(original).__name__
    lines = [f"Could not retrieve the Argos language package index: "
             f"{name}: {original}"]
    text = f"{name}: {original}".lower()
    if "certificate" in text or "ssl" in text:
        lines.append("")
        lines.append(
            "This is a certificate verification failure, not a missing "
            "language pack. The application could not verify the identity of "
            "the download server, so it refused to continue - it will never "
            "disable verification to get past this.")
        lines.append(describe_certificates())
        lines.append(
            "If you are on a network that inspects TLS traffic, set "
            "SSL_CERT_FILE to your organisation's CA bundle and try again.")
    return "\n".join(lines)


def available_packages():
    """Upstream packages, after a bounded index refresh.

    The only supported way to reach ``get_available_packages()``: calling it
    directly with no index on disk is what recursed.
    """
    refresh_package_index()
    import argostranslate.package as package

    return package.get_available_packages()


def active_package_dir():
    """Where Argos is actually reading packages from, right now."""
    try:
        from argostranslate import settings

        return pathlib.Path(settings.package_data_dir)
    except Exception:  # noqa: BLE001
        value = os.environ.get(PACKAGES_DIR_ENV)
        return pathlib.Path(value) if value else None

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
        if package_dir:
            # Applied before any Argos import so it actually takes effect.
            configure_package_dir(package_dir)
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
        location = active_package_dir()
        if not pairs:
            return (f"Argos Translate is installed but no language packages are. "
                    f"Looking in: {location}\n"
                    f"Install one with a network connection:\n"
                    f"    babelfishr languages install es en")
        return (f"No installed Argos route translates into "
                f"{self.target_language!r}. Usable routes: "
                f"{', '.join(f'{a}->{b}' for a, b in pairs)}\n"
                f"Looking in: {location}\n"
                f"    babelfishr languages install <source> "
                f"{self.target_language}")

    def readiness(self) -> Dict[str, object]:
        """Structured view for Field Check."""
        return {
            "library_installed": self.library_installed(),
            "package_dir": str(active_package_dir() or ""),
            "pairs": self.installed_pairs(),
            "direct": self.direct_pairs(),
            "pivot": self.pivot_pairs(),
            "target_language": self.target_language,
            "available": self.available(),
            "reason": self.unavailable_reason(),
        }

    def smoke_test(self, source: str, target: str) -> "TranslationResult":
        """Actually translate a fixed phrase, to prove the path works."""
        return self.translate(_SMOKE_PHRASES.get(source, "hello"), target,
                              source_language=source)

    # -- pair management -------------------------------------------------
    def routes(self) -> List[Tuple[str, str, str]]:
        """Usable routes as ``(from, to, kind)`` from Argos's own graph.

        Argos composes *pivot* routes at load time (es->en plus en->de yields a
        CompositeTranslation for es->de), so asking the resolved graph reports
        what can genuinely be translated. Assuming one package equals one route
        would understate what is available offline.

        Identity translations (a language to itself) are excluded: Argos adds
        them to the graph, but "en->en is installed" is not a translation
        capability.
        """
        if self.package_dir:
            configure_package_dir(self.package_dir)
        try:
            import argostranslate.translate as translate
        except Exception:  # noqa: BLE001
            return []
        try:
            languages = translate.get_installed_languages()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not enumerate Argos languages: %s", exc)
            return []

        out: List[Tuple[str, str, str]] = []
        for language in languages:
            for translation in getattr(language, "translations_from", []):
                target = getattr(getattr(translation, "to_lang", None), "code", None)
                if not target or target == language.code:
                    continue  # identity translation
                kind = ("pivot"
                        if type(translation).__name__ == "CompositeTranslation"
                        else "direct")
                out.append((language.code, target, kind))
        return sorted(set(out))

    def installed_pairs(self) -> List[Tuple[str, str]]:
        """Every usable ``(from, to)`` route, direct or pivoted."""
        return sorted({(a, b) for a, b, _ in self.routes()})

    def direct_pairs(self) -> List[Tuple[str, str]]:
        return sorted({(a, b) for a, b, kind in self.routes() if kind == "direct"})

    def pivot_pairs(self) -> List[Tuple[str, str]]:
        return sorted({(a, b) for a, b, kind in self.routes() if kind == "pivot"})

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
        # Index first, bounded and cached. A failure here is about the
        # catalogue, not about this pair, so it propagates unchanged: five
        # requested pairs must not produce five identical certificate errors.
        candidates = available_packages()
        try:
            for candidate in candidates:
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
