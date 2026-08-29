"""Field preparation: the one-time, explicitly online setup step.

Everything that needs the network happens here and nowhere else.  After this
has run successfully, ``babelfishr field-check`` should pass with the network
disconnected, and that is the contract the two commands exist to enforce.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import shutil
import time
from typing import Callable, Dict, List, Optional, Tuple

from .modes import AppPaths, OperatingMode, OfflineViolation, guard_download

log = logging.getLogger(__name__)

Reporter = Callable[[str], None]

#: The name of the ASR step, in one place, because both the GUI and the tests
#: ask whether *that particular* step passed.
ASR_STEP = "Local ASR model"

#: Approximate on-disk size of each Whisper model, for the storage estimate.
MODEL_SIZES_MB = {
    "tiny": 75, "base": 145, "small": 480, "medium": 1500,
    "large-v3": 3100, "large-v3-turbo": 1600,
}


def working_config(config, asr_model: str):
    """A copy of *config* carrying the model actually being prepared.

    Preparation took an ``asr_model`` override while the following Field Check
    read ``config.asr.model``, so choosing ``tiny`` downloaded tiny and then
    failed readiness looking for ``small``. Both steps now run against one
    working copy, and the caller's configuration is left untouched until the
    whole operation succeeds - a failed or cancelled preparation must not
    rewrite a setting that was previously working.
    """
    import copy

    working = copy.deepcopy(config)
    if asr_model:
        working.asr.model = asr_model
    return working


@dataclasses.dataclass
class PreparationResult:
    steps: List[Tuple[str, bool, str]] = dataclasses.field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.steps)

    def succeeded(self, name: str) -> bool:
        """Did the step whose name starts with *name* pass?

        Used to persist a successful Whisper preparation even when translation
        packages failed: those are independent pieces of work and losing the
        model download because a language pack could not be fetched would make
        the operator do the slowest step twice.
        """
        return any(ok for step, ok, _ in self.steps if step.startswith(name))

    @property
    def asr_ok(self) -> bool:
        return self.succeeded(ASR_STEP)

    def failures(self) -> List[Tuple[str, str]]:
        return [(name, detail) for name, ok, detail in self.steps if not ok]

    def summary(self) -> str:
        lines = []
        for name, ok, detail in self.steps:
            lines.append(f"  [{'OK' if ok else 'XX'}] {name}"
                         + (f": {detail}" if detail else ""))
        lines.append("")
        lines.append("Field preparation complete." if self.ok
                     else "Field preparation INCOMPLETE - see the failures above.")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        return {"ok": self.ok,
                "steps": [{"name": n, "ok": o, "detail": d}
                          for n, o, d in self.steps]}


def prepare_field(config, *, asr_model: Optional[str] = None,
                  language_pairs: Optional[List[Tuple[str, str]]] = None,
                  report: Optional[Reporter] = None,
                  skip_download: bool = False,
                  mode: Optional[OperatingMode] = None) -> PreparationResult:
    """Download and verify everything needed for offline operation.

    Refuses to run in a mode that forbids downloads, unless the caller is only
    verifying what is already present (``skip_download``).
    """
    if not skip_download:
        guard_download(mode or OperatingMode(getattr(config, "mode",
                                                     "online-setup")),
                       "field preparation")
    say = report or (lambda text: None)
    result = PreparationResult()
    paths = config.paths().ensure()
    # Argos must see the managed directory before it is imported anywhere.
    from .modes import bootstrap_environment

    bootstrap_environment(config)
    model_name = asr_model or config.asr.model

    say(f"Preparing field assets in {paths.root}")

    # -- storage ---------------------------------------------------------
    needed_mb = MODEL_SIZES_MB.get(model_name, 500) + 200
    free_mb = paths.free_bytes() / 1e6
    say(f"Storage: {free_mb:.0f} MB free, about {needed_mb} MB needed")
    if free_mb < needed_mb:
        result.add("Free storage", False,
                   f"{free_mb:.0f} MB free, need about {needed_mb} MB")
    else:
        result.add("Free storage", True, f"{free_mb:.0f} MB free")

    result.add("Recording directory writable", paths.writable(),
               str(paths.recordings))

    # -- ASR model -------------------------------------------------------
    result.add(*_prepare_asr(config, paths, model_name, say, skip_download))

    # -- translation packages -------------------------------------------
    # A failure to fetch the package index is about the catalogue, not about
    # any one pair. Reporting it five times - once per requested language -
    # buries the real cause in duplicates and tells the operator nothing they
    # did not already know from the first line.
    pending = list(language_pairs or [])
    for index, (source, target) in enumerate(pending):
        try:
            result.add(*_prepare_language(source, target, say, skip_download))
        except _IndexUnavailable as stop:
            result.add(f"Language pack {source}->{target}", False, str(stop))
            for skipped_source, skipped_target in pending[index + 1:]:
                result.add(
                    f"Language pack {skipped_source}->{skipped_target}", False,
                    "not attempted: the package index could not be retrieved "
                    "(see the error above)")
            break

    # -- optional DSD ----------------------------------------------------
    from .analysis import DsdNeoAnalyser

    analyser = DsdNeoAnalyser.from_config(config)
    if analyser.available():
        result.add("DSD-neo (optional)", True,
                   f"{analyser.resolve_executable()} version {analyser.version()}")
    else:
        say("DSD-neo not found (optional; digital post-processing unavailable)")

    return result


def _prepare_asr(config, paths: AppPaths, model_name: str, say: Reporter,
                 skip_download: bool):
    """Fetch the model into our own directory, then prove it transcribes."""
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except Exception:
        return (ASR_STEP, False,
                "faster-whisper is not installed (pip install 'babelfishr[asr]')")

    from .providers.whisper_local import FasterWhisperEngine

    from .providers.whisper_local import (ModelState, inspect_model_directory,
                                          model_directory_for, prepare_model)

    directory = model_directory_for(paths.models, model_name)
    state, missing = inspect_model_directory(directory)

    if skip_download:
        if state is not ModelState.COMPLETE:
            return (ASR_STEP, False,
                    f"{state.value} at {directory} (missing: "
                    f"{', '.join(missing)}) and downloads were skipped")
    else:
        if state is ModelState.INCOMPLETE:
            # Repair rather than delete: download_model re-fetches only what is
            # missing, so an interrupted download costs the gap, not the whole
            # model, and a complete model is never destroyed by a re-run.
            say(f"Model at {directory} is incomplete (missing: "
                f"{', '.join(missing)}); repairing...")
        say(f"Preparing Whisper model {model_name!r} into {directory} "
            f"(this is the step that needs the network)...")
        try:
            resolved = prepare_model(model_name, paths.models)
        except Exception as exc:  # noqa: BLE001
            return (ASR_STEP, False, f"could not prepare: {exc}")
        state, missing = inspect_model_directory(resolved)
        if state is not ModelState.COMPLETE:
            return (ASR_STEP, False,
                    f"download finished but the directory is {state.value} "
                    f"(missing: {', '.join(missing)})")

    # Re-open exactly as the field would: from the resolved directory, local
    # files only, no network.
    say("Verifying the model loads with downloads disabled...")
    offline = FasterWhisperEngine(model=model_name,
                                  models_root=str(paths.models),
                                  local_files_only=True)
    try:
        started = time.monotonic()
        result = offline.warm_up()
        elapsed = time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return (ASR_STEP, False,
                f"present but not loadable offline: {exc}")

    directory = offline.model_directory()
    size_mb = (sum(f.stat().st_size for f in directory.rglob('*') if f.is_file())
               / 1e6) if directory and directory.exists() else 0
    _write_manifest(paths, model_name, directory, size_mb, result.engine_version)
    return (ASR_STEP, True,
            f"{model_name} at {directory} ({size_mb:.0f} MB), "
            f"offline transcription smoke test passed in {elapsed:.1f}s")


def _write_manifest(paths: AppPaths, model_name: str,
                    directory: Optional[pathlib.Path], size_mb: float,
                    version: str) -> None:
    """Record what was installed, so Field Check can verify it later."""
    import json

    manifest_path = paths.models / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError:
            manifest = {}
    manifest[model_name] = {
        "name": model_name, "path": str(directory) if directory else "",
        "size_mb": round(size_mb, 1), "engine_version": version,
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


class _IndexUnavailable(RuntimeError):
    """Internal signal: stop trying language packs, the catalogue is missing."""


def _prepare_language(source: str, target: str, say: Reporter,
                      skip_download: bool):
    from .providers.argos import ArgosTranslateEngine, PackageIndexUnavailable

    engine = ArgosTranslateEngine()
    if not engine.library_installed():
        return (f"Language pack {source}->{target}", False,
                "argostranslate is not installed "
                "(pip install 'babelfishr[translate]')")
    if (source, target) in set(engine.installed_pairs()):
        return (f"Language pack {source}->{target}", True, "already installed")
    if skip_download:
        return (f"Language pack {source}->{target}", False,
                "not installed and downloads were skipped")
    say(f"Installing Argos language pack {source} -> {target}...")
    try:
        installed = engine.install_pair(source, target)
    except PackageIndexUnavailable as exc:
        # Not this pair's fault. Stop the whole loop rather than repeating it.
        say(str(exc))
        raise _IndexUnavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        return (f"Language pack {source}->{target}", False, str(exc))
    if not installed:
        return (f"Language pack {source}->{target}", False,
                "no such package is published")
    try:
        checked = ArgosTranslateEngine(target_language=target)
        result = checked.smoke_test(source, target)
    except Exception as exc:  # noqa: BLE001
        return (f"Language pack {source}->{target}", False,
                f"installed but translation failed: {exc}")
    return (f"Language pack {source}->{target}", True,
            f"installed, smoke test produced {result.text[:40]!r}")


# ---- language pack management ----------------------------------------
def installed_languages(config=None) -> List[Tuple[str, str]]:
    from .modes import bootstrap_environment
    from .providers.argos import ArgosTranslateEngine

    bootstrap_environment(config)
    return ArgosTranslateEngine().installed_pairs()


def installed_routes(config=None) -> List[Tuple[str, str, str]]:
    """Usable routes with their kind (direct or pivot)."""
    from .modes import bootstrap_environment
    from .providers.argos import ArgosTranslateEngine

    bootstrap_environment(config)
    return ArgosTranslateEngine().routes()


def available_languages() -> List[Tuple[str, str]]:
    """Packages published upstream. Needs the network.

    Goes through the bounded refresh, so a failure here raises once with the
    real cause rather than recursing inside argostranslate.
    """
    from .providers.argos import available_packages

    return sorted((p.from_code, p.to_code) for p in available_packages())


def install_language(source: str, target: str, mode: OperatingMode) -> bool:
    """Install a language pack. Refused outside preparation mode."""
    guard_download(mode, f"installing the {source}->{target} language pack")
    from .providers.argos import ArgosTranslateEngine

    return ArgosTranslateEngine().install_pair(source, target)


def remove_language(source: str, target: str) -> bool:
    try:
        import argostranslate.package as package
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("argostranslate is not installed") from exc
    for installed in package.get_installed_packages():
        if installed.from_code == source and installed.to_code == target:
            package.uninstall(installed)
            return True
    return False


def translation_paths(target: str, config=None) -> Dict[str, List[str]]:
    """Sources that can reach *target*, split by how Argos gets there.

    Taken from Argos's resolved translation graph rather than inferred from the
    package list, so composite routes (es->en->de) are reported as the usable
    routes they actually are.
    """
    routes = installed_routes(config)
    direct = sorted({a for a, b, kind in routes if b == target and kind == "direct"})
    pivot = sorted({a for a, b, kind in routes if b == target and kind == "pivot"})
    return {"direct": direct, "pivot": pivot,
            "all": sorted(set(direct) | set(pivot))}
