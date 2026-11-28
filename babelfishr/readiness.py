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
import os
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
        """The floor: can we preserve incoming audio at all?

        Also false when the input the operator chose is missing or cannot be
        told apart from another (monitoring refuses to start on it), or when
        macOS has affirmatively denied the microphone. An input not chosen
        yet is a warning, not a failure: preparation comes first."""
        required = ("Audio backend", "Recording directory writable",
                    "Selected audio input", "Microphone permission")
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

    def _unverified(self, smoke: str, asset: str) -> bool:
        """The asset is there, but its smoke test was not run.

        "Not tested" and "not available" are different answers, and conflating
        them is what made alpha 3 report Record Only on a machine with a
        working model and working language packs.
        """
        test = self.get(smoke)
        present = self.get(asset)
        return (test is not None and test.status is CheckStatus.SKIP
                and present is not None and present.status is CheckStatus.PASS)

    @property
    def transcription_unverified(self) -> bool:
        return self._unverified("Local transcription smoke test",
                                "Local ASR model present")

    @property
    def translation_unverified(self) -> bool:
        # The production check is named "Installed translation paths" - see
        # _check_translation. An earlier version of this looked for a name no
        # check ever emits, so it silently answered False for every real
        # report, and the unit test that covered it used the same invented
        # name and passed without touching a real one.
        return self._unverified("Local translation smoke test",
                                "Installed translation paths")

    @property
    def processing_ready(self) -> bool:
        """What preparation produces: storage and audio backend, and local
        transcription and translation that ran. Independent of which input
        is plugged in right now, so Setup judges what it prepared."""
        floor = ("Audio backend", "Recording directory writable")
        return (all(c.status is not CheckStatus.FAIL for c in self.checks
                    if c.name in floor)
                and self.can_transcribe and self.can_translate)

    @property
    def field_ready(self) -> bool:
        """Fully ready: record, transcribe and translate with no network."""
        return self.can_record and self.can_transcribe and self.can_translate

    @property
    def field_ready_unknown(self) -> bool:
        """Everything needed is present, but not all of it has been tested.

        Neither ready nor unready: an honest third answer, so a caller can
        say "checking" instead of announcing a downgrade it has not
        established.
        """
        if self.field_ready or not self.can_record:
            return False
        transcription = self.can_transcribe or self.transcription_unverified
        translation = self.can_translate or self.translation_unverified
        return (transcription and translation
                and (self.transcription_unverified or self.translation_unverified))

    def stage_word(self, stage: str) -> str:
        """"yes", "not checked yet" or "NO" for "transcribe"/"translate".

        Wording only: "not checked yet" is a present asset whose smoke test
        did not run, which is neither working nor unavailable.
        """
        works, unverified = {
            "transcribe": (self.can_transcribe, self.transcription_unverified),
            "translate": (self.can_translate, self.translation_unverified),
        }[stage]
        if works:
            return "yes"
        return "not checked yet" if unverified else "NO"

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
        lines.append(f"  transcribe : {self.stage_word('transcribe')}")
        lines.append(f"  translate  : {self.stage_word('translate')}")
        lines.append("")
        if self.field_ready:
            lines.append("READY for offline field operation.")
        elif self.field_ready_unknown:
            lines.append(
                "PREPARED, NOT CHECKED YET. The model and language packs are "
                "installed but their smoke tests did not run; run the check "
                "without --no-smoke-tests to confirm they work.")
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
            "field_ready_unknown": self.field_ready_unknown,
            "transcription_unverified": self.transcription_unverified,
            "translation_unverified": self.translation_unverified,
            "recommended_mode": self.recommended_mode().value,
        }


#: Below this, a long monitoring session risks filling the disk.
MIN_FREE_BYTES = 2_000_000_000


#: What Offline Field Use needs, each of which must actually PASS for the
#: green tick beside the mode control: audio and storage, the selected
#: model present, loaded and run, translation installed and run, and the
#: mode's own guarantees. Advisory checks (free space, microphone
#: permission, which is not detectable before recording) stay warnings in
#: the full report. The selected input may warn (the built-in microphone
#: on a bench, or a deliberately chosen system default) but not fail.
OFFLINE_REQUIRED = (
    "Audio backend", "Audio input devices", "Selected audio input",
    "Recording directory writable",
    "Local ASR model present", "Local ASR model loadable",
    "Local transcription smoke test", "Installed translation paths",
    "Local translation smoke test", "Cloud processing disabled",
    "Mock engines disabled")
#: Required, failing closed: on macOS only a verified grant passes (denied,
#: not asked yet and cannot-be-read are all red); off macOS the check must
#: say explicitly that it does not apply (``data["not_applicable"]``).
OFFLINE_PERMISSION = "Microphone permission"


def offline_field_problems(report: "ReadinessReport") -> List[str]:
    """Why *report* does not show Offline Field Use ready: one line per
    required check that is missing, failed, partial, unavailable or not
    verified (skipped). Empty only when every required check passed."""
    problems = []
    if report.mode is not OperatingMode.FIELD_OFFLINE:
        problems.append(f"Checked for {report.mode.title}, not Offline Field Use")
    for name in OFFLINE_REQUIRED:
        check = report.get(name)
        if check is None:
            problems.append(f"{name}: not checked")
        elif check.status is not CheckStatus.PASS and not (
                check.status is CheckStatus.WARN and check.data.get("acceptable")):
            word = {CheckStatus.FAIL: "failed", CheckStatus.WARN: "partial",
                    CheckStatus.SKIP: "not verified"}[check.status]
            problems.append(f"{name}: {word}" + (f" ({check.detail})" if check.detail else ""))
    permission = report.get(OFFLINE_PERMISSION)
    if permission is None:
        problems.append(f"{OFFLINE_PERMISSION}: not checked")
    elif not (permission.status is CheckStatus.PASS or (
            permission.status is CheckStatus.SKIP
            and permission.data.get("not_applicable"))):
        word = {CheckStatus.FAIL: "failed", CheckStatus.WARN: "not verified",
                CheckStatus.SKIP: "not verified"}.get(permission.status, "not verified")
        problems.append(f"{OFFLINE_PERMISSION}: {word} ({permission.detail})")
    return problems


def field_check(config, *, run_smoke_tests: bool = True,
                mode: Optional[OperatingMode] = None,
                microphone_permission: Optional[str] = None,
                save_path: Optional[str] = None) -> ReadinessReport:
    """Verify offline readiness without downloading anything.

    *save_path*: the selected Session's chosen save folder (Conversation
    save_path), when it has one. "Recording directory writable" then checks
    that destination - ``<save_path>/BabelFishR``, with the same preflight
    as choosing it - instead of only the standard Recordings folder, and
    fails with the concrete reason ("not found (drive ejected or folder
    removed)", "read-only", ...). None or "" checks the standard folder.
    """
    from .audio.devices import backend_available, backend_status, list_input_devices

    mode = mode or OperatingMode.FIELD_OFFLINE
    report = ReadinessReport(mode=mode)

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

    report.add(_microphone_permission_check(microphone_permission))
    report.add(_selected_input_check(config))

    # -- storage --------------------------------------------------------
    free_target = None
    if save_path:
        from . import savepath

        owned = savepath.owned_dir(save_path)
        reason = savepath.preflight(save_path)
        if not reason:
            report.add(Check("Recording directory writable", CheckStatus.PASS,
                             f"Save path {owned}",
                             data={"save_path": save_path, "reason": ""}))
            free_target = owned
        else:
            report.add(Check(
                "Recording directory writable", CheckStatus.FAIL,
                savepath.describe(save_path, reason),
                data={"save_path": save_path, "reason": reason},
                remedy=f"Reconnect the drive or make {save_path} writable, or "
                       f"choose another save path (or the standard location) "
                       f"for this Session."))
    else:
        # The same standard folder the Recorder writes to (which is not the
        # managed Recordings folder when recording.directory points
        # elsewhere), probed the same way as a save path.
        standard = config.recordings_path()
        reason = _standard_dir_problem(standard)
        if not reason:
            report.add(Check("Recording directory writable", CheckStatus.PASS,
                             str(standard)))
            free_target = standard
        else:
            report.add(Check(
                "Recording directory writable", CheckStatus.FAIL,
                f"{standard}: {reason}",
                remedy=f"Grant write access to {standard} or change the "
                       f"recordings directory."))

    # Measured on the actual destination: the Session's drive when it is
    # available, else the standard folder (or, failing both, its nearest
    # existing parent).
    free = _free_bytes(free_target if free_target is not None
                       else config.recordings_path())
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
    # Checks a separate product registered (babelfishr.extensions): KingFishR
    # adds its receiver and decoder checks. None for BabelFishR.
    from .extensions import hooks

    for readiness_checks in hooks("readiness_checks"):
        for check in readiness_checks(config):
            report.add(check)

    # -- mode guarantees -------------------------------------------------
    report.add(_cloud_disabled_check(mode))
    report.add(_mock_disabled_check(mode))
    return report


def _standard_dir_problem(directory: pathlib.Path) -> str:
    """"" when the standard recordings folder takes a write, else why.

    Created if missing (as the Recorder would), then probed with a uniquely
    named temporary file that is synced, renamed and removed.
    """
    from . import savepath

    try:
        if directory.is_symlink() and not directory.is_dir():
            return "a broken link"
        if os.path.lexists(directory) and not directory.is_dir():
            return savepath.NOT_A_FOLDER
        directory.mkdir(parents=True, exist_ok=True)
        savepath.probe_dir(directory)
    except OSError as exc:
        reason = savepath.reason_for(exc)
        return "cannot be created or written" if reason == savepath.NOT_FOUND else reason
    return ""


def _free_bytes(target: pathlib.Path) -> int:
    path = pathlib.Path(target)
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _selected_input_check(config) -> Check:
    """Is the input the operator chose actually the one that would be used?

    Reported, not enforced here: readiness is about whether the models and the
    machine can do the work offline, and preparation legitimately happens
    before an interface is wired up. The hard refusal lives where it belongs -
    monitoring will not start on a missing or unchosen input.
    """
    from .audio.devices import list_input_devices, resolve_input

    selection = config.audio.input
    if selection.use_system_default and selection.confirmed:
        # The operating system's own default, resolved now: a Mac can list
        # inputs and still have no usable default. (default_input_device()
        # falls back to the first listed device, which is not the default.)
        try:
            devices = list_input_devices()
        except Exception:  # noqa: BLE001 - no backend: cannot resolve
            devices = []
        default = next((d for d in devices if d.is_default
                        and int(getattr(d, "max_input_channels", 0) or 0) > 0), None)
        if default is None:
            return Check(
                "Selected audio input", CheckStatus.FAIL,
                "system default input chosen, but the system reports no usable "
                "default input",
                remedy="Choose an input in System Settings > Sound > Input, or "
                       "select the interface itself in the Audio input panel.")
        return Check(
            "Selected audio input", CheckStatus.WARN,
            f"system default (chosen deliberately): now {default.name}",
            data={"acceptable": True, "device": default.to_dict()},
            remedy="For a radio watch, select the interface itself: the system "
                   "default can change without warning.")
    if not config.has_confirmed_input():
        return Check(
            "Selected audio input", CheckStatus.WARN, "none chosen yet",
            remedy="Choose an input in the Audio input panel, or run "
                   "'babelfishr input --select INDEX'. Monitoring will not "
                   "start until one is chosen.")

    identity = config.selected_input()
    resolution = resolve_input(identity)
    if resolution.ambiguous:
        return Check(
            "Selected audio input", CheckStatus.FAIL,
            f"{len(resolution.candidates)} connected inputs are "
            f"indistinguishable from {selection.label or identity.describe()}",
            data={"identity": identity.token(),
                  "candidates": [d.to_dict() for d in resolution.candidates]},
            remedy="BabelFishR cannot safely determine which interface is "
                   "carrying the radio, and will not guess. Disconnect the "
                   "one you do not want to monitor.")
    if resolution.device is None:
        return Check(
            "Selected audio input", CheckStatus.FAIL,
            f"{selection.label or identity.describe()} is NOT connected",
            data={"identity": identity.token()},
            remedy="Reconnect that device. BabelFishR will not substitute "
                   "another input for it - not the built-in microphone, not "
                   "the system default, and not another interface.")
    device = resolution.device
    # The built-in microphone, chosen explicitly and connected, is a
    # legitimate configured input (a bench test); the note says what it hears.
    kind = ("built-in microphone, chosen for a bench test" if device.is_builtin
            else "external input")
    detail = f"{device.name} ({kind}, matched by {resolution.basis})"
    remedy = ""
    if device.is_builtin:
        remedy = ("This is the microphone built into this Mac. It records the "
                  "room, not a radio.")
    return Check("Selected audio input", CheckStatus.PASS, detail, remedy=remedy)


def microphone_authorization() -> str:
    """macOS's microphone decision for this app, read without asking:
    ``"granted"``, ``"denied"``, ``"undetermined"`` (not asked yet),
    ``"not-applicable"`` (not macOS) or ``"unknown"`` (cannot be read here).

    Uses Qt's permission API (QCoreApplication.checkPermission), which never
    shows the system dialog. Call it on the GUI thread. Granted is trusted
    as is. Denied is trusted only from the packaged app, which carries the
    usage description and Qt's permission backend - without them Qt answers
    Denied for a working microphone - so a development run reads unknown.
    """
    if platform.system() != "Darwin":
        return "not-applicable"
    try:
        from PySide6 import QtCore
    except Exception:  # noqa: BLE001 - no Qt: the command line
        return "unknown"
    qt_app = QtCore.QCoreApplication.instance()
    permission_type = getattr(QtCore, "QMicrophonePermission", None)
    if qt_app is None or permission_type is None:
        return "unknown"
    main = getattr(QtCore.QThread, "isMainThread", None)
    if not (main() if main is not None
            else QtCore.QThread.currentThread() is qt_app.thread()):
        return "unknown"
    try:
        status = qt_app.checkPermission(permission_type())
    except Exception:  # noqa: BLE001
        return "unknown"
    statuses = QtCore.Qt.PermissionStatus
    if status == statuses.Granted:
        return "granted"
    if status == statuses.Undetermined:
        return "undetermined"
    if status == statuses.Denied and _permission_backend_deployed(QtCore):
        return "denied"
    return "unknown"


def _permission_backend_deployed(QtCore) -> bool:
    """The packaged app, with Qt's microphone permission plugin beside it."""
    import sys

    if not getattr(sys, "frozen", False):
        return False
    plugins = pathlib.Path(QtCore.QLibraryInfo.path(
        QtCore.QLibraryInfo.LibraryPath.PluginsPath)) / "permissions"
    return plugins.is_dir() and any("microphone" in p.name.lower()
                                    for p in plugins.iterdir())


def _microphone_permission_check(authorization: Optional[str] = None) -> Check:
    """Affirmative evidence where macOS gives it; never a dialog, never a
    test recording. *authorization* is microphone_authorization() read on
    the GUI thread by the caller; None reads as unknown."""
    if platform.system() != "Darwin":
        return Check("Microphone permission", CheckStatus.SKIP,
                     f"not applicable on {platform.system()}",
                     data={"not_applicable": True})
    if authorization == "granted":
        return Check("Microphone permission", CheckStatus.PASS,
                     "granted to BabelFishR in macOS")
    if authorization == "denied":
        return Check(
            "Microphone permission", CheckStatus.FAIL,
            "denied to BabelFishR in macOS: recordings would be silent",
            remedy="System Settings > Privacy & Security > Microphone: turn on "
                   "BabelFishR, then quit and reopen it.")
    if authorization == "undetermined":
        return Check(
            "Microphone permission", CheckStatus.WARN,
            "not asked yet: macOS asks the first time monitoring starts",
            remedy="Start monitoring once and allow BabelFishR to use the "
                   "microphone.")
    # Fail closed: without an affirmative grant this is not verified, and
    # Offline Field Use is not shown ready (the cross says why).
    return Check(
        "Microphone permission", CheckStatus.WARN,
        "not verified: macOS's decision could not be read here",
        data={"authorization": authorization or "unknown"},
        remedy="Use the packaged BabelFishR app (it can read the decision), "
               "and allow it in System Settings > Privacy & Security > "
               "Microphone.")


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
    # Integrity from the install record only (no hashing here): "verified"
    # only when the record matches the pin and the files as they are now.
    # A custom folder has no pinned identity, so it is never "verified".
    from .model_integrity import (CHANGED_WORDS, MISMATCH_WORDS, Integrity,
                                  integrity_state)

    if getattr(config.asr, "model_path", None):
        integrity, integrity_detail = (Integrity.UNPINNED,
                                       "custom folder, checksums not verified")
    else:
        integrity, integrity_detail = integrity_state(directory, config.asr.model)
    if integrity in (Integrity.MISMATCH, Integrity.CHANGED):
        # Not usable as is: a model whose hash failed, or one that was
        # verified and is no longer the bytes that were hashed. Both fail
        # until hashed again locally (the engine also refuses them).
        changed = integrity is Integrity.CHANGED
        words = CHANGED_WORDS if changed else MISMATCH_WORDS
        verify = "babelfishr field-check --verify (local, no network)"
        remedy = (f"Verify checksums: {verify}, or Verify checksums in Local "
                  f"engines." if changed else
                  f"Repair it online: babelfishr prepare-field --asr-model "
                  f"{config.asr.model}; or restore the original files and "
                  f"verify checksums: {verify}.")
        report.add(Check(
            "Local ASR model present", CheckStatus.FAIL,
            f"{config.asr.model} at {directory} {words} ({integrity_detail})",
            remedy=remedy,
            data={"path": str(directory), "bytes": size,
                  "integrity": integrity.value,
                  "integrity_detail": integrity_detail}))
        why = ("the model changed since it was verified" if changed
               else "the model failed its checksums")
        report.add(Check("Local ASR model loadable", CheckStatus.SKIP, why))
        report.add(Check("Local transcription smoke test", CheckStatus.SKIP, why))
        return
    # A legacy install (no record) is used, and said to be unverified.
    wording = (integrity_detail if integrity is Integrity.VERIFIED
               else "checksums not verified")
    report.add(Check("Local ASR model present", CheckStatus.PASS,
                     f"{config.asr.model} at {directory} ({size / 1e6:.0f} MB), "
                     f"{wording}",
                     data={"path": str(directory), "bytes": size,
                           "integrity": integrity.value,
                           "integrity_detail": integrity_detail}))

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
    """The translation the current settings would actually use.

    With a specified source language, that one route - Spanish -> English is
    not vouched for by a German -> English pack - or, when it is the target
    language itself and "skip if same language" is on, the genuine
    pass-through (the transcript is shown as is, no pack involved). In
    Automatic, every installed route into the target is smoke-tested and
    the detail names the ones verified, so a pass never implies other
    languages are covered.
    """
    from .providers.argos import ArgosTranslateEngine

    from .modes import bootstrap_environment

    target = config.translate.target_language
    session = getattr(config, "session", None)
    source = (session.source_language
              if session is not None and session.source_language_mode == "specified"
              and session.source_language else None)
    skip_same = bool(config.translate.skip_if_same_language)
    if source and source == target and skip_same:
        report.add(Check(
            "Installed translation paths", CheckStatus.PASS,
            f"{source}->{target}: the same language, shown as transcribed "
            f"(no translation pack needed)",
            data={"route": "pass-through", "source": source, "target": target}))
        report.add(Check(
            "Local translation smoke test", CheckStatus.PASS,
            f"same-language pass-through ({source}): nothing to translate",
            data={"route": "pass-through", "verified": []}))
        return

    bootstrap_environment(config)
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
            remedy=f"babelfishr languages install {source or '<source>'} {target}"))
        report.add(Check("Local translation smoke test", CheckStatus.SKIP,
                         "no language packages"))
        return
    if not usable:
        report.add(Check(
            "Installed translation paths", CheckStatus.FAIL,
            f"{len(pairs)} installed, none into {target!r}: "
            f"{', '.join(f'{a}->{b}' for a, b in pairs)}",
            remedy=f"babelfishr languages install {source or '<source>'} {target}",
            data={"pairs": pairs}))
        report.add(Check("Local translation smoke test", CheckStatus.SKIP,
                         f"nothing translates into {target}"))
        return
    if source and (source, target) not in usable:
        report.add(Check(
            "Installed translation paths", CheckStatus.FAIL,
            f"{source}->{target} (the selected source language) is not "
            f"installed; installed into {target}: "
            f"{', '.join(f'{a}->{b}' for a, b in usable)}",
            remedy=f"babelfishr languages install {source} {target}",
            data={"pairs": pairs, "source": source, "target": target}))
        report.add(Check("Local translation smoke test", CheckStatus.SKIP,
                         f"no {source}->{target} route to test"))
        return

    routes = [(source, target)] if source else usable
    direct = {(a, b) for a, b in engine.direct_pairs()}
    described = ", ".join(
        f"{a}->{b}" + ("" if (a, b) in direct else " (via pivot)")
        for a, b in routes)
    if not source:
        described = f"Automatic: {described}"
    report.add(Check(
        "Installed translation paths", CheckStatus.PASS, described,
        data=engine.readiness()))

    if not run_smoke_tests:
        report.add(Check("Local translation smoke test", CheckStatus.SKIP,
                         "smoke tests disabled"))
        return

    verified, failed, outputs = [], [], {}
    for a, b in routes:
        try:
            result = engine.smoke_test(a, b)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{a}->{b} ({str(exc)[:120]})")
            continue
        if result.text.strip():
            verified.append(f"{a}->{b}")
            outputs[f"{a}->{b}"] = result.text
        else:
            failed.append(f"{a}->{b} (the engine returned nothing)")
    data = {"source": source, "target": target, "verified": verified,
            "failed": failed, "outputs": outputs}
    if failed:
        report.add(Check(
            "Local translation smoke test", CheckStatus.FAIL,
            f"failed: {'; '.join(failed)}"
            + (f"; verified: {', '.join(verified)}" if verified else ""),
            remedy=f"babelfishr languages install {(source or '<source>')} {target}",
            data=data))
        return
    if source:
        detail = f"{verified[0]} produced {outputs[verified[0]][:48]!r}"
    else:
        detail = (f"Automatic: verified {', '.join(verified)}. Speech in any "
                  f"other language is transcribed but not translated")
        if skip_same:
            detail += f"; {target} speech is shown as transcribed"
    report.add(Check("Local translation smoke test", CheckStatus.PASS, detail,
                     data=data))


def _cloud_disabled_check(mode: OperatingMode) -> Check:
    if mode.allows_cloud:
        return Check("Cloud processing disabled", CheckStatus.WARN,
                     f"{mode.label} permits explicitly selected cloud engines",
                     remedy="Switch to Offline Field Use before going into the field.")
    return Check("Cloud processing disabled", CheckStatus.PASS,
                 f"{mode.label} cannot construct a cloud provider")


def _mock_disabled_check(mode: OperatingMode) -> Check:
    if mode.allows_mock:
        return Check("Mock engines disabled", CheckStatus.WARN,
                     f"{mode.label} permits placeholder engines",
                     remedy="Switch to Offline Field Use so output is never "
                            "placeholder text.")
    return Check("Mock engines disabled", CheckStatus.PASS,
                 f"{mode.label} refuses placeholder engines")


def _directory_size(path: pathlib.Path) -> int:
    if not path or not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
