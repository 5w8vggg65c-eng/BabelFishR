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

#: Approximate on-disk size of each Whisper model, for the storage estimate.
MODEL_SIZES_MB = {
    "tiny": 75, "base": 145, "small": 480, "medium": 1500,
    "large-v3": 3100, "large-v3-turbo": 1600,
}


@dataclasses.dataclass
class PreparationResult:
    steps: List[Tuple[str, bool, str]] = dataclasses.field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.steps)

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
                  skip_download: bool = False) -> PreparationResult:
    """Download and verify everything needed for offline operation."""
    say = report or (lambda text: None)
    result = PreparationResult()
    paths = config.paths().ensure()
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
    for source, target in (language_pairs or []):
        result.add(*_prepare_language(source, target, say, skip_download))

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
        return ("Local ASR model", False,
                "faster-whisper is not installed (pip install 'babelfishr[asr]')")

    from .providers.whisper_local import FasterWhisperEngine

    if skip_download:
        engine = FasterWhisperEngine(model=model_name,
                                     download_root=str(paths.models),
                                     local_files_only=True)
        if not engine.model_present():
            return ("Local ASR model", False,
                    f"not present at {engine.model_directory()} and downloads "
                    f"were skipped")
    else:
        say(f"Downloading Whisper model {model_name!r} into {paths.models} "
            f"(this is the step that needs the network)...")
        engine = FasterWhisperEngine(
            model=model_name, download_root=str(paths.models),
            local_files_only=False)
        try:
            engine.warm_up()
        except Exception as exc:  # noqa: BLE001
            return ("Local ASR model", False, f"could not prepare: {exc}")

    # Re-open exactly as the field would: local files only, no network.
    say("Verifying the model loads with downloads disabled...")
    offline = FasterWhisperEngine(model=model_name,
                                  download_root=str(paths.models),
                                  local_files_only=True)
    try:
        started = time.monotonic()
        result = offline.warm_up()
        elapsed = time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return ("Local ASR model", False,
                f"present but not loadable offline: {exc}")

    directory = offline.model_directory()
    size_mb = (sum(f.stat().st_size for f in directory.rglob('*') if f.is_file())
               / 1e6) if directory and directory.exists() else 0
    _write_manifest(paths, model_name, directory, size_mb, result.engine_version)
    return ("Local ASR model", True,
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


def _prepare_language(source: str, target: str, say: Reporter,
                      skip_download: bool):
    from .providers.argos import ArgosTranslateEngine

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
def installed_languages() -> List[Tuple[str, str]]:
    from .providers.argos import ArgosTranslateEngine

    return sorted(ArgosTranslateEngine().installed_pairs())


def available_languages() -> List[Tuple[str, str]]:
    """Packages published upstream. Needs the network."""
    try:
        import argostranslate.package as package
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "argostranslate is not installed "
            "(pip install 'babelfishr[translate]')") from exc
    package.update_package_index()
    return sorted((p.from_code, p.to_code) for p in package.get_available_packages())


def install_language(source: str, target: str, mode: OperatingMode) -> bool:
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


def translation_paths(target: str) -> Dict[str, List[str]]:
    """Direct and pivot routes into *target*, so the UI can be specific.

    Argos can translate via English, so a Spanish->German path may exist even
    with no direct package. Reporting only direct pairs would understate what
    is actually available offline.
    """
    pairs = installed_languages()
    direct = sorted({a for a, b in pairs if b == target})
    into_pivot = {a for a, b in pairs if b == "en"}
    pivot_out = {b for a, b in pairs if a == "en"}
    pivot: List[str] = []
    if target in pivot_out or target == "en":
        pivot = sorted(into_pivot - set(direct) - {target})
    return {"direct": direct, "pivot_via_en": pivot}
