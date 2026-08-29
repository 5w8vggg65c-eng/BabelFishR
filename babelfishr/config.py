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
class AudioConfig:
    device: Optional[str] = None
    """Device index, name fragment, or ``None`` for the system default."""

    sample_rate: int = 48_000
    block_size: int = 2048
    channels: int = 1
    bit_depth: int = 16
    """Bit depth for stored original recordings (16, 24 or 32)."""

    reconnect: bool = True
    safety_recording: SafetyRecordingConfig = dataclasses.field(
        default_factory=SafetyRecordingConfig)


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
    directory: str = "recordings"
    enabled: bool = True
    layout: str = "{date}/{session}"
    """Placeholders: date, session, profile, channel."""

    retention_days: Optional[int] = None
    keep_processed_audio: bool = False
    """Keep the resampled ASR copy as well as the original. Off by default."""


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
    database: str = "babelfishr.sqlite3"
    log_level: str = "INFO"
    experimental: bool = False
    """Enable the unvalidated signalling decoders. Off by default."""

    source_path: Optional[str] = None
    """Where this config was loaded from, if anywhere."""

    # ---- construction -------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        resolved = _resolve_path(path)
        data = _read_file(resolved) if resolved else {}
        cfg = cls.from_dict(data)
        cfg.source_path = str(resolved) if resolved else None
        cfg.apply_env(dict(os.environ))
        return cfg

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        cfg = cls()
        sections = {
            "audio": cfg.audio, "detector": cfg.detector, "asr": cfg.asr,
            "translate": cfg.translate, "recording": cfg.recording,
            "session": cfg.session,
        }
        for name, obj in sections.items():
            values = dict(data.get(name) or {})
            if name == "audio" and "safety_recording" in values:
                _update(cfg.audio.safety_recording, values.pop("safety_recording"))
            _update(obj, values)
        for key in ("database", "log_level", "experimental"):
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

    def save(self, path: Optional[str] = None) -> str:
        target = pathlib.Path(path or self.source_path or (APP_DIR / "babelfishr.toml"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix == ".json":
            target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        else:
            target.write_text(self.dump_toml(), encoding="utf-8")
        self.source_path = str(target)
        return str(target)

    def dump_toml(self) -> str:
        return _to_toml(self.to_dict())

    def recordings_path(self) -> pathlib.Path:
        return pathlib.Path(self.recording.directory).expanduser()


def _resolve_path(path: Optional[str]) -> Optional[pathlib.Path]:
    if path:
        p = pathlib.Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        return p
    for name in DEFAULT_CONFIG_NAMES:
        for base in (pathlib.Path.cwd(), APP_DIR):
            candidate = base / name
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


def _update(obj: Any, values: Dict[str, Any]) -> None:
    known = {f.name for f in dataclasses.fields(obj)}
    for key, value in values.items():
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
