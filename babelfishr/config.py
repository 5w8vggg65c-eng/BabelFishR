"""Configuration: layered defaults, TOML/JSON file, environment, CLI overrides."""

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


@dataclasses.dataclass
class SquelchConfig:
    """Controls how the continuous audio stream is cut into transmissions."""

    mode: str = "auto"
    """``auto`` tracks the noise floor; ``fixed`` uses ``threshold_dbfs``."""

    threshold_dbfs: float = -45.0
    open_margin_db: float = 8.0
    """dB above the tracked noise floor at which squelch opens."""

    close_margin_db: float = 4.0
    """Hysteresis: squelch closes below floor + this margin."""

    hangtime: float = 0.6
    """Seconds of sub-threshold audio tolerated before ending a transmission."""

    min_duration: float = 0.35
    """Transmissions shorter than this are discarded as clicks/noise."""

    max_duration: float = 300.0
    """Hard cap; a longer key-up is split into consecutive records."""

    pre_roll: float = 0.25
    """Seconds of audio kept from before squelch opened."""

    ctcss_gate: Optional[float] = None
    """If set, only record transmissions carrying this CTCSS tone (Hz)."""


@dataclasses.dataclass
class DecoderConfig:
    enabled: List[str] = dataclasses.field(default_factory=lambda: [
        "ctcss", "dcs", "dtmf", "cw", "afsk1200", "mdc1200", "pocsag", "rtty",
        "digital-voice",
    ])
    min_confidence: float = 0.5
    pocsag_baud: List[int] = dataclasses.field(default_factory=lambda: [512, 1200, 2400])
    rtty_baud: float = 45.45
    rtty_shift: float = 170.0


@dataclasses.dataclass
class AsrConfig:
    engine: str = "auto"
    """``auto``, ``faster-whisper``, ``whisper``, ``whisper.cpp``, or ``none``."""

    model: str = "small"
    device: str = "auto"
    compute_type: str = "default"
    language: Optional[str] = None
    """Force a source language; ``None`` auto-detects per transmission."""

    beam_size: int = 5
    vad_filter: bool = True
    min_speech_ratio: float = 0.25
    """Skip ASR when the segmenter thinks the audio is mostly data/noise."""

    binary: Optional[str] = None
    """Path to a whisper.cpp binary when ``engine = "whisper.cpp"``."""

    model_path: Optional[str] = None


@dataclasses.dataclass
class TranslateConfig:
    engine: str = "auto"
    """``auto``, ``argos``, ``whisper``, ``claude``, or ``none``."""

    target_language: str = "en"
    """The operator's native language. Everything is rendered into this."""

    skip_if_same_language: bool = True
    model: str = "claude-sonnet-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    glossary: Dict[str, str] = dataclasses.field(default_factory=dict)
    """Terms to keep verbatim, e.g. callsigns or place names."""


@dataclasses.dataclass
class RecordingConfig:
    directory: str = "recordings"
    enabled: bool = True
    format: str = "wav"
    layout: str = "{band}/{channel}/{date}"
    """Directory template. Placeholders: band, channel, date, freq_mhz, source."""

    write_sidecar: bool = True
    retention_days: Optional[int] = None
    max_bytes: Optional[int] = None
    """Prune oldest recordings once the store exceeds this many bytes."""


@dataclasses.dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    enabled: bool = False
    open_browser: bool = False


@dataclasses.dataclass
class SourceConfig:
    uri: str = "soundcard:default"
    sample_rate: int = 48_000
    channels: int = 1
    block_size: int = 4096
    gain_db: float = 0.0
    frequency_hz: Optional[float] = None
    band: str = "wideband"
    channel: Optional[str] = None
    deemphasis: bool = False
    """Apply 750 us de-emphasis (use for discriminator-tapped FM audio)."""


@dataclasses.dataclass
class Config:
    source: SourceConfig = dataclasses.field(default_factory=SourceConfig)
    squelch: SquelchConfig = dataclasses.field(default_factory=SquelchConfig)
    decoders: DecoderConfig = dataclasses.field(default_factory=DecoderConfig)
    asr: AsrConfig = dataclasses.field(default_factory=AsrConfig)
    translate: TranslateConfig = dataclasses.field(default_factory=TranslateConfig)
    recording: RecordingConfig = dataclasses.field(default_factory=RecordingConfig)
    server: ServerConfig = dataclasses.field(default_factory=ServerConfig)
    database: str = "babelfishr.sqlite3"
    log_level: str = "INFO"

    # ---- construction -------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        data: Dict[str, Any] = {}
        resolved = _resolve_path(path)
        if resolved is not None:
            data = _read_file(resolved)
        cfg = cls.from_dict(data)
        cfg.apply_env(os.environ)
        return cfg

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        cfg = cls()
        sections = {
            "source": cfg.source, "squelch": cfg.squelch, "decoders": cfg.decoders,
            "asr": cfg.asr, "translate": cfg.translate, "recording": cfg.recording,
            "server": cfg.server,
        }
        for name, obj in sections.items():
            _update(obj, data.get(name) or {})
        for key in ("database", "log_level"):
            if key in data:
                setattr(cfg, key, data[key])
        for raw in data.get("bands") or []:
            _register_custom_band(raw)
        return cfg

    def apply_env(self, env: Dict[str, str]) -> None:
        """``BABELFISHR_TARGET_LANG`` etc. override the file."""
        mapping = {
            "BABELFISHR_SOURCE": (self.source, "uri", str),
            "BABELFISHR_BAND": (self.source, "band", str),
            "BABELFISHR_TARGET_LANG": (self.translate, "target_language", str),
            "BABELFISHR_ASR_MODEL": (self.asr, "model", str),
            "BABELFISHR_ASR_ENGINE": (self.asr, "engine", str),
            "BABELFISHR_TRANSLATE_ENGINE": (self.translate, "engine", str),
            "BABELFISHR_RECORDINGS": (self.recording, "directory", str),
            "BABELFISHR_DB": (self, "database", str),
            "BABELFISHR_LOG_LEVEL": (self, "log_level", str),
            "BABELFISHR_PORT": (self.server, "port", int),
        }
        for key, (obj, attr, cast) in mapping.items():
            if env.get(key):
                setattr(obj, attr, cast(env[key]))

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def dump_toml(self) -> str:
        return _to_toml(self.to_dict())


def _resolve_path(path: Optional[str]) -> Optional[pathlib.Path]:
    if path:
        p = pathlib.Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        return p
    candidates: List[pathlib.Path] = []
    for name in DEFAULT_CONFIG_NAMES:
        candidates.append(pathlib.Path.cwd() / name)
        candidates.append(pathlib.Path.home() / ".config" / "babelfishr" / name)
    for c in candidates:
        if c.exists():
            return c
    return None


def _read_file(path: pathlib.Path) -> Dict[str, Any]:
    text = path.read_bytes()
    if path.suffix == ".json":
        return json.loads(text.decode("utf-8"))
    if _toml is None:  # pragma: no cover
        raise RuntimeError(
            f"cannot read {path}: no TOML parser available. "
            "Install tomli or use a .json config."
        )
    return _toml.loads(text.decode("utf-8"))


def _update(obj: Any, values: Dict[str, Any]) -> None:
    known = {f.name for f in dataclasses.fields(obj)}
    for key, value in values.items():
        if key in known:
            setattr(obj, key, value)
        else:
            raise ValueError(f"unknown option {key!r} in [{type(obj).__name__}]")


def _register_custom_band(raw: Dict[str, Any]) -> None:
    from .bandplan import Band, Channel, register_band

    channels = [Channel(**c) for c in raw.get("channels") or []]
    fields = {f.name for f in dataclasses.fields(Band)}
    kwargs = {k: v for k, v in raw.items() if k in fields and k != "channels"}
    register_band(Band(channels=channels, **kwargs))


def _to_toml(data: Dict[str, Any], _prefix: str = "") -> str:
    """Minimal TOML writer good enough for round-tripping our own config."""
    scalars, tables = [], []
    for key, value in data.items():
        if value is None:
            # TOML has no null: emit the key commented out so the file stays
            # round-trippable and still documents the option.
            scalars.append(f"# {key} = ")
        elif isinstance(value, dict) and not _is_flat_map(value):
            tables.append((key, value))
        elif isinstance(value, dict):
            scalars.append(f"{key} = {_toml_value(value)}")
        else:
            scalars.append(f"{key} = {_toml_value(value)}")
    out = "\n".join(scalars)
    for key, value in tables:
        name = f"{_prefix}{key}"
        out += f"\n\n[{name}]\n" + _to_toml(value, _prefix=f"{name}.")
    return out.strip() + "\n"


def _is_flat_map(value: Dict[str, Any]) -> bool:
    return all(not isinstance(v, dict) for v in value.values()) and len(value) == 0


def _toml_value(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + "}"
    return json.dumps(str(value))
