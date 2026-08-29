"""Field readiness: prove the machine can work with the network unplugged.

Field Check answers one question honestly: *if the operator pulled the network
right now, what would still work?*  It answers it by exercising the real code
paths - loading the real model, running a real transcription of a bundled
fixture, running a real translation through an installed language pair - rather
than by checking that imports succeed.

Nothing here downloads. Preparation is a separate, explicitly online workflow.
"""

from __future__ import annotations

import dataclasses
import enum
import pathlib
import platform
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from .modes import AppPaths, OperatingMode, guard_download


class CheckStatus(str, enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"

    @property
    def symbol(self) -> str:
        return {"pass": "OK", "warn": "!!", "fail": "XX", "skip": "--"}[self.value]


@dataclasses.dataclass
class Check:
    name: str
    status: CheckStatus
    detail: str = ""
    remedy: str = ""
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["status"] = self.status.value
        return d

    def line(self) -> str:
        text = f"  [{self.status.symbol}] {self.name}"
        if self.detail:
            text += f": {self.detail}"
        return text


@dataclasses.dataclass
class ReadinessReport:
    checks: List[Check] = dataclasses.field(default_factory=list)
    mode: OperatingMode = OperatingMode.FIELD_OFFLINE

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def get(self, name: str) -> Optional[Check]:
        return next((c for c in self.checks if c.name == name), None)

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status is CheckStatus.WARN]

    @property
    def can_record(self) -> bool:
        """The floor: can we preserve incoming audio at all?"""
        required = ("Audio backend", "Recording directory writable")
        return all(c.status is not CheckStatus.FAIL
                   for c in self.checks if c.name in required)

    @property
    def can_transcribe(self) -> bool:
        check = self.get("Local transcription smoke test")
        return check is not None and check.status is CheckStatus.PASS

    @property
    def can_translate(self) -> bool:
        check = self.get("Local translation smoke test")
        return check is not None and check.status is CheckStatus.PASS

    @property
    def field_ready(self) -> bool:
        """Fully ready: record, transcribe and translate with no network."""
        return self.can_record and self.can_transcribe and self.can_translate

    def recommended_mode(self) -> OperatingMode:
        if self.field_ready:
            return OperatingMode.FIELD_OFFLINE
        if self.can_record:
            return OperatingMode.RECORD_ONLY
        return OperatingMode.ONLINE_SETUP

    def summary(self) -> str:
        lines = [f"Field readiness ({self.mode.label})", ""]
        lines += [c.line() for c in self.checks]
        lines.append("")
        lines.append(f"  record     : {'yes' if self.can_record else 'NO'}")
        lines.append(f"  transcribe : {'yes' if self.can_transcribe else 'NO'}")
        lines.append(f"  translate  : {'yes' if self.can_translate else 'NO'}")
        lines.append("")
        if self.field_ready:
            lines.append("READY for offline field operation.")
        elif self.can_record:
            lines.append(
                "NOT fully ready. Recording works, so Record Only mode will "
                "preserve everything received; transcription and translation "
                "can be run later once prepared.")
        else:
            lines.append("NOT ready: audio capture itself is not working.")
        remedies = [c.remedy for c in self.checks if c.remedy and
                    c.status in (CheckStatus.FAIL, CheckStatus.WARN)]
        if remedies:
            lines.append("")
            lines.append("Next steps:")
            lines += [f"  - {r}" for r in remedies]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "checks": [c.to_dict() for c in self.checks],
            "can_record": self.can_record,
            "can_transcribe": self.can_transcribe,
            "can_translate": self.can_translate,
            "field_ready": self.field_ready,
            "recommended_mode": self.recommended_mode().value,
        }


#: Below this, a long monitoring session risks filling the disk.
MIN_FREE_BYTES = 2_000_000_000


def field_check(config, *, run_smoke_tests: bool = True,
                mode: Optional[OperatingMode] = None) -> ReadinessReport:
    """Verify offline readiness without downloading anything."""
    from .audio.devices import backend_available, backend_status, list_input_devices

    mode = mode or OperatingMode.FIELD_OFFLINE
    report = ReadinessReport(mode=mode)
    paths = config.paths()

    # -- audio ----------------------------------------------------------
    if backend_available():
        report.add(Check("Audio backend", CheckStatus.PASS, backend_status()))
        devices = list_input_devices()
        if devices:
            report.add(Check(
                "Audio input devices", CheckStatus.PASS,
                f"{len(devices)} available",
                data={"devices": [d.to_dict() for d in devices]}))
        else:
            report.add(Check(
                "Audio input devices", CheckStatus.FAIL, "none found",
                remedy="Connect an audio interface and check macOS microphone "
                       "permission (System Settings > Privacy & Security)."))
    else:
        report.add(Check(
            "Audio backend", CheckStatus.FAIL, backend_status(),
            remedy="pip install 'babelfishr[audio]'"))
        report.add(Check("Audio input devices", CheckStatus.SKIP,
                         "no audio backend"))

    report.add(_microphone_permission_check())

    # -- storage --------------------------------------------------------
    if paths.writable():
        report.add(Check("Recording directory writable", CheckStatus.PASS,
                         str(paths.recordings)))
    else:
        report.add(Check(
            "Recording directory writable", CheckStatus.FAIL,
            str(paths.recordings),
            remedy=f"Grant write access to {paths.recordings} or change the "
                   f"recordings directory."))

    free = paths.free_bytes()
    free_gb = free / 1e9
    if free >= MIN_FREE_BYTES:
        report.add(Check("Free storage", CheckStatus.PASS, f"{free_gb:.1f} GB"))
    else:
        report.add(Check(
            "Free storage", CheckStatus.WARN, f"{free_gb:.1f} GB",
            remedy="Free disk space before a long session, or shorten the "
                   "recording retention period."))

    # -- transcription ---------------------------------------------------
    _check_transcription(config, report, run_smoke_tests)

    # -- translation -----------------------------------------------------
    _check_translation(config, report, run_smoke_tests)

    # -- optional extras -------------------------------------------------
    report.add(_dsd_check(config))
    report.add(_sdr_check(config))

    # -- mode guarantees -------------------------------------------------
    report.add(_cloud_disabled_check(mode))
    report.add(_mock_disabled_check(mode))
    return report


def _microphone_permission_check() -> Check:
    """Best effort: macOS does not expose a reliable pre-flight API."""
    if platform.system() != "Darwin":
        return Check("Microphone permission", CheckStatus.SKIP,
                     f"not applicable on {platform.system()}")
    from .audio.devices import list_input_devices

    if not list_input_devices():
        return Check(
            "Microphone permission", CheckStatus.WARN,
            "cannot be determined; no input devices are visible",
            remedy="System Settings > Privacy & Security > Microphone, then "
                   "restart the app or terminal.")
    return Check(
        "Microphone permission", CheckStatus.WARN,
        "not directly detectable; verified only by recording",
        remedy="Run 'babelfishr test-record' and confirm the clip is not silent.")


def _check_transcription(config, report: ReadinessReport,
                         run_smoke_tests: bool) -> None:
    from .providers.whisper_local import FasterWhisperEngine

    paths = config.paths()
    engine = FasterWhisperEngine(
        model=config.asr.model, device=config.asr.device,
        compute_type=config.asr.compute_type,
        models_root=str(paths.models),
        model_path=getattr(config.asr, "model_path", None) or None,
        local_files_only=True)

    if not engine.library_installed():
        report.add(Check("Local ASR model present", CheckStatus.FAIL,
                         "faster-whisper is not installed",
                         remedy="pip install 'babelfishr[asr]'"))
        report.add(Check("Local ASR model loadable", CheckStatus.SKIP, ""))
        report.add(Check("Local transcription smoke test", CheckStatus.SKIP, ""))
        return

    if not engine.model_present():
        from .providers.whisper_local import ModelState

        state, missing = engine.model_state()
        detail = (f"incomplete at {engine.model_directory()} "
                  f"(missing: {', '.join(missing)})"
                  if state is ModelState.INCOMPLETE
                  else f"no model at {engine.model_directory()}")
        report.add(Check(
            "Local ASR model present", CheckStatus.FAIL, detail,
            remedy=f"babelfishr prepare-field --asr-model {config.asr.model}",
            data={"state": state.value, "missing": missing,
                  "path": str(engine.model_directory())}))
        report.add(Check("Local ASR model loadable", CheckStatus.SKIP,
                         "no complete model to load"))
        report.add(Check("Local transcription smoke test", CheckStatus.SKIP,
                         "no complete model to test"))
        return

    directory = engine.model_directory()
    size = _directory_size(directory) if directory else 0
    report.add(Check("Local ASR model present", CheckStatus.PASS,
                     f"{config.asr.model} at {directory} ({size / 1e6:.0f} MB)",
                     data={"path": str(directory), "bytes": size}))

    if not run_smoke_tests:
        report.add(Check("Local ASR model loadable", CheckStatus.SKIP,
                         "smoke tests disabled"))
        report.add(Check("Local transcription smoke test", CheckStatus.SKIP,
                         "smoke tests disabled"))
        return

    started = time.monotonic()
    try:
        result = engine.warm_up()
    except Exception as exc:  # noqa: BLE001 - readiness must never crash
        report.add(Check("Local ASR model loadable", CheckStatus.FAIL, str(exc)[:200],
                         remedy="Re-run 'babelfishr prepare-field' to repair the "
                                "model directory."))
        report.add(Check("Local transcription smoke test", CheckStatus.FAIL,
                         "model would not load"))
        return

    elapsed = time.monotonic() - started
    report.add(Check("Local ASR model loadable", CheckStatus.PASS,
                     f"loaded in {elapsed:.1f}s"))
    # The fixture is synthetic speech-shaped noise, so the transcript text is
    # meaningless. What is being proved is that the engine ran end to end
    # locally and returned a well-formed result.
    report.add(Check(
        "Local transcription smoke test", CheckStatus.PASS,
        f"ran locally in {elapsed:.1f}s "
        f"(engine {result.engine_version}, detected {result.language})",
        data={"engine": result.engine, "version": result.engine_version,
              "language": result.language}))


def _check_translation(config, report: ReadinessReport,
                       run_smoke_tests: bool) -> None:
    from .providers.argos import ArgosTranslateEngine

    from .modes import bootstrap_environment

    bootstrap_environment(config)
    target = config.translate.target_language
    engine = ArgosTranslateEngine(target_language=target,
                                  package_dir=str(config.paths().language_packs))

    if not engine.library_installed():
        report.add(Check("Installed translation paths", CheckStatus.FAIL,
                         "argostranslate is not installed",
                         remedy="pip install 'babelfishr[translate]'"))
        report.add(Check("Local translation smoke test", CheckStatus.SKIP, ""))
        return

    pairs = sorted(engine.installed_pairs())
    usable = [p for p in pairs if p[1] == target]
    if not pairs:
        report.add(Check(
            "Installed translation paths", CheckStatus.FAIL, "none installed",
            remedy=f"babelfishr languages install <source> {target}"))
        report.add(Check("Local translation smoke test", CheckStatus.SKIP,
                         "no language packages"))
        return
    if not usable:
        report.add(Check(
            "Installed translation paths", CheckStatus.FAIL,
            f"{len(pairs)} installed, none into {target!r}: "
            f"{', '.join(f'{a}->{b}' for a, b in pairs)}",
            remedy=f"babelfishr languages install <source> {target}",
            data={"pairs": pairs}))
        report.add(Check("Local translation smoke test", CheckStatus.SKIP,
                         f"nothing translates into {target}"))
        return

    direct = {(a, b) for a, b in engine.direct_pairs()}
    described = ", ".join(
        f"{a}->{b}" + ("" if (a, b) in direct else " (via pivot)")
        for a, b in usable)
    report.add(Check(
        "Installed translation paths", CheckStatus.PASS, described,
        data=engine.readiness()))

    if not run_smoke_tests:
        report.add(Check("Local translation smoke test", CheckStatus.SKIP,
                         "smoke tests disabled"))
        return

    source = usable[0][0]
    try:
        result = engine.smoke_test(source, target)
    except Exception as exc:  # noqa: BLE001
        report.add(Check("Local translation smoke test", CheckStatus.FAIL,
                         str(exc)[:200],
                         remedy=f"babelfishr languages install {source} {target}"))
        return
    if not result.text.strip():
        report.add(Check("Local translation smoke test", CheckStatus.FAIL,
                         "the engine returned nothing"))
        return
    report.add(Check(
        "Local translation smoke test", CheckStatus.PASS,
        f"{source}->{target} produced {result.text[:48]!r}",
        data={"source": source, "target": target, "output": result.text}))


def _dsd_check(config) -> Check:
    from .analysis.dsd import DsdNeoAnalyser

    analyser = DsdNeoAnalyser.from_config(config)
    if not analyser.configured:
        return Check("DSD-neo", CheckStatus.SKIP, "not configured (optional)")
    if not analyser.available():
        return Check(
            "DSD-neo", CheckStatus.WARN, analyser.unavailable_reason(),
            remedy="Install dsd-neo and set analysis.dsd_path, or leave it "
                   "unset - BabelFishR works without it.")
    return Check("DSD-neo", CheckStatus.PASS,
                 f"{analyser.executable} ({analyser.version()})",
                 data={"path": analyser.executable, "version": analyser.version()})


def _sdr_check(config) -> Check:
    from .sources import sdr_status

    status = sdr_status(config)
    if not status["configured"]:
        return Check("SDR input", CheckStatus.SKIP,
                     "not configured (optional; the audio path is the default)")
    if not status["available"]:
        return Check("SDR input", CheckStatus.WARN, status["reason"])
    return Check("SDR input", CheckStatus.PASS, status["detail"])


def _cloud_disabled_check(mode: OperatingMode) -> Check:
    if mode.allows_cloud:
        return Check("Cloud processing disabled", CheckStatus.WARN,
                     f"{mode.label} permits explicitly selected cloud engines",
                     remedy="Switch to Field Offline before going into the field.")
    return Check("Cloud processing disabled", CheckStatus.PASS,
                 f"{mode.label} cannot construct a cloud provider")


def _mock_disabled_check(mode: OperatingMode) -> Check:
    if mode.allows_mock:
        return Check("Mock engines disabled", CheckStatus.WARN,
                     f"{mode.label} permits placeholder engines",
                     remedy="Switch to Field Offline so output is never "
                            "placeholder text.")
    return Check("Mock engines disabled", CheckStatus.PASS,
                 f"{mode.label} refuses placeholder engines")


def _directory_size(path: pathlib.Path) -> int:
    if not path or not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
