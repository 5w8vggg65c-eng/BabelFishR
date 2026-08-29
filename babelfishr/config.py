"""Configuration: dataclass defaults, TOML/JSON file, environment overrides.

No credential is ever stored here - see :mod:`babelfishr.providers.credentials`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:
        _toml = None  # type: ignore

DEFAULT_CONFIG_NAMES = ("babelfishr.toml", "babelfishr.json")
APP_DIR = pathlib.Path.home() / ".config" / "babelfishr"


@dataclasses.dataclass
class SafetyRecordingConfig:
    enabled: bool = False
    """Continuous backup recording, so a segmentation failure loses nothing."""

    chunk_seconds: float = 300.0
    retention_hours: Optional[float] = 24.0
    max_bytes: Optional[int] = 5_000_000_000


@dataclasses.dataclass
class InputSelection:
    """Which audio input the operator chose, and how firmly.

    Stored as a :class:`babelfishr.audio.devices.DeviceIdentity` token rather
    than an index or a name, so it still means the same physical device after
    the interface has been unplugged, replugged, or the machine rebooted.
    """

    identity: str = ""
    """Serialised DeviceIdentity. Empty means nothing has been chosen yet."""

    label: str = ""
    """What the operator saw when they chose it. Used when it goes missing."""

    confirmed: bool = False
    """The operator selected this deliberately. Nothing else may set it."""

    use_system_default: bool = False
    """A deliberate, visibly labelled choice to follow the macOS setting.

    Never a fallback. When this is true the operator has said, in as many
    words, that they want whatever macOS currently calls the default input.
    """


@dataclasses.dataclass
class AudioConfig:
    device: Optional[str] = None
    """Device index, name fragment, or ``None`` for the system default.

    A *selector*, for the command line and for one-off use. It is not how a
    saved selection is restored - see ``input``.
    """

    sample_rate: int = 48_000
    block_size: int = 2048
    channels: int = 1
    bit_depth: int = 16
    """Bit depth for stored original recordings (16, 24 or 32)."""

    reconnect: bool = True
    safety_recording: SafetyRecordingConfig = dataclasses.field(
        default_factory=SafetyRecordingConfig)

    input: InputSelection = dataclasses.field(default_factory=InputSelection)
    """The persisted, operator-confirmed input."""

    profile_inputs: Dict[str, str] = dataclasses.field(default_factory=dict)
    """profile id -> DeviceIdentity token.

    A radio profile can remember which interface it is wired to, so selecting
    the profile selects the right input - and says so loudly when that input is
    not connected, instead of quietly using something else.
    """


@dataclasses.dataclass
class DetectorConfig:
    """Mirrors :class:`babelfishr.detect.DetectorSettings`."""

    mode: str = "auto"
    threshold_dbfs: float = -45.0
    open_margin_db: float = 8.0
    close_margin_db: float = 4.0
    hang_time: float = 0.8
    pre_roll: float = 0.30
    post_roll: float = 0.20
    min_duration: float = 0.30
    max_duration: float = 300.0
    trim_squelch_tail: bool = True

    # Automatic-processing routing. None of these can stop a recording being
    # made: they only decide whether an ASR call happens without being asked.
    auto_process_speech: bool = True
    auto_process_unknown: bool = True
    auto_process_noise: bool = False
    auto_process_tone: bool = False
    auto_process_digital: bool = False

    def to_settings(self):
        from .detect import DetectorSettings

        return DetectorSettings(**dataclasses.asdict(self)).validate()


@dataclasses.dataclass
class AsrConfig:
    engine: str = "auto"
    """``auto``, ``faster-whisper``, ``mock``, or ``none``."""

    model: str = "small"
    device: str = "auto"
    compute_type: str = "default"
    beam_size: int = 5
    model_path: Optional[str] = None
    """Explicit local model directory; overrides the managed models folder."""

    min_confidence: float = 0.0
    """Transcripts below this are still stored, but flagged for review."""


@dataclasses.dataclass
class TranslateConfig:
    engine: str = "auto"
    """``auto``, ``argos``, ``claude``, ``mock``, or ``none``."""

    target_language: str = "en"
    model: str = ""
    """Optional model override for cloud engines."""

    skip_if_same_language: bool = True
    glossary_path: str = ""


@dataclasses.dataclass
class RecordingConfig:
    directory: str = ""
    """Empty means the managed location (AppPaths.recordings). See resolve()."""

    enabled: bool = True
    layout: str = "{date}/{session}"
    """Placeholders: date, session, profile, channel."""

    retention_days: Optional[int] = None
    keep_processed_audio: bool = False
    """Keep the resampled ASR copy as well as the original. Off by default."""


@dataclasses.dataclass
class AnalysisConfig:
    """Optional local digital post-processing. Entirely opt-in."""

    dsd_path: str = ""
    """Path to a dsd-neo executable, or a bare name to find on PATH."""

    dsd_args: List[str] = dataclasses.field(default_factory=list)
    timeout: float = 120.0
    auto_analyse_suspected: bool = False
    """Automatically analyse events classified as suspected digital."""


@dataclasses.dataclass
class SdrConfig:
    """Optional SDR input. The ordinary audio path is the default."""

    driver: str = ""
    """``""``/``none`` (default), or ``recorded-iq``. No device drivers bundled."""

    recording_path: str = ""
    center_frequency_hz: Optional[float] = None
    tuned_frequency_hz: Optional[float] = None
    gain_db: Optional[float] = None


@dataclasses.dataclass
class SetupState:
    """What the operator has already chosen. Persisted across restarts."""

    completed: bool = False
    """True once preparation finished or Record Only was chosen deliberately."""

    completed_at: str = ""
    asr_model: str = ""
    language_pairs: List[str] = dataclasses.field(default_factory=list)
    audio_device: str = ""
    record_only_acknowledged: bool = False
    """The operator chose to proceed without processing, knowingly."""


@dataclasses.dataclass
class SessionConfig:
    profile_id: Optional[str] = None
    source_language_mode: str = "automatic"
    """``automatic`` or ``specified``."""

    source_language: Optional[str] = None
    auto_start: bool = False


@dataclasses.dataclass
class Config:
    audio: AudioConfig = dataclasses.field(default_factory=AudioConfig)
    detector: DetectorConfig = dataclasses.field(default_factory=DetectorConfig)
    asr: AsrConfig = dataclasses.field(default_factory=AsrConfig)
    translate: TranslateConfig = dataclasses.field(default_factory=TranslateConfig)
    recording: RecordingConfig = dataclasses.field(default_factory=RecordingConfig)
    session: SessionConfig = dataclasses.field(default_factory=SessionConfig)
    analysis: AnalysisConfig = dataclasses.field(default_factory=AnalysisConfig)
    sdr: SdrConfig = dataclasses.field(default_factory=SdrConfig)
    setup: SetupState = dataclasses.field(default_factory=SetupState)
    mode: str = "online-setup"
    """``field-offline``, ``online-setup`` or ``record-only``."""

    app_home: Optional[str] = None
    """Override for the Application Support directory holding field assets."""

    database: str = ""
    """Empty means the managed location (AppPaths.database). See resolve()."""

    log_level: str = "INFO"
    experimental: bool = False
    """Enable the unvalidated signalling decoders. Off by default."""

    source_path: Optional[str] = None
    """Where this config was loaded from, if anywhere."""

    # ---- construction -------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None,
             resolve_paths: bool = True) -> "Config":
        """Load configuration and resolve every runtime path exactly once.

        Search order for a config file: an explicit path, then the working
        directory, then the managed settings file under Application Support.
        """
        resolved = _resolve_path(path)
        data = _read_file(resolved) if resolved else {}
        cfg = cls.from_dict(data)
        cfg.source_path = str(resolved) if resolved else None
        cfg.apply_env(dict(os.environ))
        if resolve_paths:
            cfg.resolve_runtime_paths()
        return cfg

    def resolve_runtime_paths(self) -> "Config":
        """Make ``database`` and ``recording.directory`` absolute, once.

        The rules, in order:

        1. An explicit absolute path from config or environment wins outright.
        2. An explicit *relative* path resolves against the directory holding
           the config file that set it - never against the process working
           directory, which for a double-clicked ``.app`` is ``/`` and would
           scatter databases wherever Finder happened to launch from.
        3. Anything unset uses the managed Application Support location.

        Every consumer - Store, Recorder, readiness, the CLI, the GUI and the
        packaged entry point - reads the resolved values, so they cannot
        disagree about where data lives.
        """
        paths = self.paths()
        base = (pathlib.Path(self.source_path).parent
                if self.source_path else None)

        self.database = str(_resolve_runtime_path(
            self.database, base, paths.database))
        self.recording.directory = str(_resolve_runtime_path(
            self.recording.directory, base, paths.recordings))
        if self.translate.glossary_path:
            self.translate.glossary_path = str(_resolve_runtime_path(
                self.translate.glossary_path, base, paths.root / "glossary.json"))
        return self

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        cfg = cls()
        sections = {
            "audio": cfg.audio, "detector": cfg.detector, "asr": cfg.asr,
            "translate": cfg.translate, "recording": cfg.recording,
            "session": cfg.session, "analysis": cfg.analysis, "sdr": cfg.sdr,
            "setup": cfg.setup,
        }
        for name, obj in sections.items():
            values = dict(data.get(name) or {})
            if name == "audio":
                # Nested dataclasses have to be updated in place; assigning the
                # raw dict would leave a dict where the code expects an object.
                if "safety_recording" in values:
                    _update(cfg.audio.safety_recording,
                            values.pop("safety_recording"))
                if "input" in values:
                    _update(cfg.audio.input, dict(values.pop("input") or {}))
            _update(obj, values)
        for key in ("database", "log_level", "experimental", "mode", "app_home"):
            if key in data:
                setattr(cfg, key, data[key])
        return cfg

    def apply_env(self, env: Dict[str, str]) -> None:
        mapping = {
            "BABELFISHR_DEVICE": (self.audio, "device", str),
            "BABELFISHR_TARGET_LANG": (self.translate, "target_language", str),
            "BABELFISHR_SOURCE_LANG": (self.session, "source_language", str),
            "BABELFISHR_ASR_ENGINE": (self.asr, "engine", str),
            "BABELFISHR_ASR_MODEL": (self.asr, "model", str),
            "BABELFISHR_TRANSLATE_ENGINE": (self.translate, "engine", str),
            "BABELFISHR_RECORDINGS": (self.recording, "directory", str),
            "BABELFISHR_DB": (self, "database", str),
            "BABELFISHR_LOG_LEVEL": (self, "log_level", str),
            "BABELFISHR_MODE": (self, "mode", str),
            "BABELFISHR_HOME": (self, "app_home", str),
        }
        for key, (obj, attr, cast) in mapping.items():
            if env.get(key):
                setattr(obj, attr, cast(env[key]))
        if env.get("BABELFISHR_EXPERIMENTAL", "").lower() in ("1", "true", "yes", "on"):
            self.experimental = True

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("source_path", None)
        return d

    def settings_path(self) -> pathlib.Path:
        """Where *application* settings are written.

        An explicitly supplied config file keeps ownership - the operator's own
        file is never silently replaced by the managed one - otherwise settings
        go to the managed location under Application Support.
        """
        if self.source_path:
            return pathlib.Path(self.source_path)
        return self.paths().settings

    def save(self, path: Optional[str] = None) -> str:
        """Write settings atomically, so a crash cannot truncate them."""
        target = pathlib.Path(path) if path else self.settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (json.dumps(self.to_dict(), indent=2) if target.suffix == ".json"
                else self.dump_toml())
        # Write-then-rename: a settings file half-written by a crash would
        # leave the app unable to start.
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
        self.source_path = str(target)
        return str(target)

    def record_setup(self, *, asr_model: str = "", language_pairs=None,
                     audio_device: str = "", record_only: bool = False,
                     completed: bool = True) -> str:
        """Persist the operator's setup choices and save."""
        import datetime as _dt

        if asr_model:
            self.setup.asr_model = asr_model
            self.asr.model = asr_model
        if language_pairs is not None:
            self.setup.language_pairs = [f"{a}-{b}" for a, b in language_pairs]
        if audio_device:
            self.setup.audio_device = audio_device
            self.audio.device = audio_device
        if record_only:
            self.setup.record_only_acknowledged = True
        self.setup.completed = completed
        if completed:
            self.setup.completed_at = _dt.datetime.now(
                _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.save()

    # ---- audio input selection ----------------------------------------
    def selected_input(self):
        """The saved input identity, or an empty one when nothing is chosen."""
        from .audio.devices import DeviceIdentity

        return DeviceIdentity.parse(self.audio.input.identity)

    def has_confirmed_input(self) -> bool:
        """True only when the operator explicitly chose something.

        Monitoring is not allowed to start without this. A remembered device is
        not the same as a chosen one, and an unchosen device is how a laptop
        microphone ends up standing in for a radio.
        """
        selection = self.audio.input
        if not selection.confirmed:
            return False
        return bool(selection.use_system_default or selection.identity)

    def record_input_selection(self, device, *,
                               profile_id: Optional[str] = None,
                               save: bool = True) -> str:
        """Persist a device the operator chose, by stable identity.

        Every explicitly chosen device is pinned to its identity: capture opens
        that device or nothing. There is no setting to relax it, because the
        setting that used to exist did not actually relax or tighten anything,
        which is a worse state of affairs than either.
        """
        identity = device.identity
        self.audio.input = InputSelection(
            identity=identity.token(),
            label=getattr(device, "name", "") or identity.describe(),
            confirmed=True,
            use_system_default=False,
        )
        # Keep the CLI selector pointing at the same device, so `babelfishr
        # monitor` with no arguments agrees with the window.
        self.audio.device = identity.token()
        if profile_id:
            self.audio.profile_inputs[profile_id] = identity.token()
        return self.save() if save else ""

    def record_system_default_input(self, save: bool = True) -> str:
        """Record a deliberate choice to follow the macOS system default."""
        self.audio.input = InputSelection(
            identity="", label="macOS system default input", confirmed=True,
            use_system_default=True)
        self.audio.device = None
        return self.save() if save else ""

    def clear_input_selection(self, save: bool = True) -> str:
        self.audio.input = InputSelection()
        self.audio.device = None
        return self.save() if save else ""

    def preferred_input_for_profile(self, profile_id: Optional[str]):
        """The input this radio profile is wired to, if it has one."""
        from .audio.devices import DeviceIdentity

        if not profile_id:
            return DeviceIdentity()
        return DeviceIdentity.parse(self.audio.profile_inputs.get(profile_id, ""))

    def associate_profile_input(self, profile_id: str, device,
                                save: bool = True) -> str:
        if not profile_id:
            return ""
        self.audio.profile_inputs[profile_id] = device.identity.token()
        return self.save() if save else ""

    @property
    def needs_first_run_setup(self) -> bool:
        """True when no completed setup exists, so the assistant should show."""
        return not self.setup.completed

    def dump_toml(self) -> str:
        return _to_toml(self.to_dict())

    def operating_mode(self):
        from .modes import OperatingMode

        return OperatingMode(self.mode)

    def paths(self):
        from .modes import AppPaths

        return AppPaths.resolve(self.app_home)

    def recordings_path(self) -> pathlib.Path:
        return pathlib.Path(self.recording.directory).expanduser()

    def glossary_file(self) -> pathlib.Path:
        if self.translate.glossary_path:
            return pathlib.Path(self.translate.glossary_path).expanduser()
        return self.paths().root / "glossary.json"


def _resolve_runtime_path(value: str, base: Optional[pathlib.Path],
                          managed: pathlib.Path) -> pathlib.Path:
    """Apply the resolution rules documented on Config.resolve_runtime_paths."""
    if not value:
        return managed
    candidate = pathlib.Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    # Relative to the config file that declared it, not to os.getcwd().
    return (base / candidate).resolve() if base else (managed.parent / candidate)


def _resolve_path(path: Optional[str]) -> Optional[pathlib.Path]:
    if path:
        p = pathlib.Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        return p
    from .modes import AppPaths

    candidates = [pathlib.Path.cwd() / name for name in DEFAULT_CONFIG_NAMES]
    candidates.append(AppPaths.resolve().settings)
    candidates += [APP_DIR / name for name in DEFAULT_CONFIG_NAMES]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_file(path: pathlib.Path) -> Dict[str, Any]:
    text = path.read_bytes()
    if path.suffix == ".json":
        return json.loads(text.decode("utf-8"))
    if _toml is None:  # pragma: no cover
        raise RuntimeError(f"cannot read {path}: no TOML parser; use a .json config")
    return _toml.loads(text.decode("utf-8"))


#: Settings that used to exist and are now ignored, per dataclass. They are
#: accepted and dropped so a settings file written by an older version still
#: loads; rejecting them would leave an operator with an application that
#: refuses to start after an upgrade.
RETIRED_OPTIONS: Dict[str, set] = {
    # "Lock input to this device" was a checkbox that changed nothing: capture
    # resolved the saved identity whether it was ticked or not. Every
    # explicitly chosen device is now pinned to its identity unconditionally,
    # so there is no longer a setting for it to be wrong about.
    "InputSelection": {"locked"},
}


def _update(obj: Any, values: Dict[str, Any]) -> None:
    known = {f.name for f in dataclasses.fields(obj)}
    retired = RETIRED_OPTIONS.get(type(obj).__name__, set())
    for key, value in values.items():
        if key in retired:
            continue
        if key not in known:
            raise ValueError(f"unknown option {key!r} in [{type(obj).__name__}]")
        setattr(obj, key, value)


def _to_toml(data: Dict[str, Any], _prefix: str = "") -> str:
    scalars: List[str] = []
    tables: List[tuple] = []
    for key, value in data.items():
        if value is None:
            scalars.append(f"# {key} =")
        elif isinstance(value, dict):
            tables.append((key, value))
        else:
            scalars.append(f"{key} = {_toml_value(value)}")
    out = "\n".join(scalars)
    for key, value in tables:
        name = f"{_prefix}{key}"
        out += f"\n\n[{name}]\n" + _to_toml(value, _prefix=f"{name}.")
    return out.strip() + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value))
