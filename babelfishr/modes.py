"""Operating modes and the application's asset locations.

Three modes, because "offline" has to mean something enforceable rather than
aspirational:

* **Field Offline** - the operator has pulled the network. Cloud providers may
  not be constructed, mock output is forbidden, and nothing may download. ASR
  loads from a verified local directory with local-files-only semantics.
  If transcription or translation is unavailable, recording continues and the
  failure is displayed honestly; there is never a silent fallback.
* **Online / Setup** - preparation. Downloads permitted, cloud engines
  permitted when the operator explicitly selects them.
* **Record Only** - deliberately capture-and-store, no processing at all. The
  mode to choose when readiness fails but traffic still has to be preserved.

The rule that matters: nothing leaves the Mac merely because a local engine is
missing. A missing local model produces an honest failure, never a cloud call.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import os
import pathlib
import platform
import shutil
from typing import Dict, List, Optional

APP_NAME = "BabelFishR"


class OperatingMode(str, enum.Enum):
    FIELD_OFFLINE = "field-offline"
    ONLINE_SETUP = "online-setup"
    RECORD_ONLY = "record-only"

    @property
    def label(self) -> str:
        return {
            OperatingMode.FIELD_OFFLINE: "FIELD OFFLINE",
            OperatingMode.ONLINE_SETUP: "ONLINE / SETUP",
            OperatingMode.RECORD_ONLY: "RECORD ONLY",
        }[self]

    @property
    def allows_cloud(self) -> bool:
        """Only Online/Setup may even construct a cloud provider."""
        return self is OperatingMode.ONLINE_SETUP

    @property
    def allows_mock(self) -> bool:
        """Mock output is development scaffolding, never field output."""
        return self is OperatingMode.ONLINE_SETUP

    @property
    def allows_downloads(self) -> bool:
        return self is OperatingMode.ONLINE_SETUP

    @property
    def runs_processing(self) -> bool:
        return self is not OperatingMode.RECORD_ONLY

    def describe(self) -> str:
        return {
            OperatingMode.FIELD_OFFLINE:
                "Local processing only. No network access, no cloud providers, "
                "no downloads, no placeholder output.",
            OperatingMode.ONLINE_SETUP:
                "Preparation mode. Downloads and explicitly selected cloud "
                "providers are permitted.",
            OperatingMode.RECORD_ONLY:
                "Capture and store only. No transcription or translation is "
                "attempted; recordings are preserved for later processing.",
        }[self]


class OfflineViolation(RuntimeError):
    """Raised when something would breach the guarantees of Field Offline.

    Deliberately an exception rather than a silent downgrade: the operator has
    to be able to trust that Field Offline means what it says.
    """


@dataclasses.dataclass
class AppPaths:
    """Where field assets live.

    On macOS these sit under Application Support, not a cache directory: a
    cache is something the OS may delete, and losing a 1.5 GB model in the
    field because the disk got tight is not acceptable. Upgrades must not touch
    any of these.
    """

    root: pathlib.Path
    models: pathlib.Path
    language_packs: pathlib.Path
    recordings: pathlib.Path
    database: pathlib.Path
    logs: pathlib.Path
    settings: pathlib.Path

    @classmethod
    def resolve(cls, override: Optional[str] = None) -> "AppPaths":
        if override:
            root = pathlib.Path(override).expanduser()
        elif os.environ.get("BABELFISHR_HOME"):
            root = pathlib.Path(os.environ["BABELFISHR_HOME"]).expanduser()
        elif platform.system() == "Darwin":
            root = pathlib.Path.home() / "Library" / "Application Support" / APP_NAME
        else:
            base = os.environ.get("XDG_DATA_HOME")
            root = (pathlib.Path(base).expanduser() if base
                    else pathlib.Path.home() / ".local" / "share") / APP_NAME
        return cls(
            root=root,
            models=root / "models",
            language_packs=root / "language-packs",
            recordings=root / "Recordings",
            database=root / "babelfishr.sqlite3",
            logs=root / "Logs",
            settings=root / "settings.toml",
        )

    def ensure(self) -> "AppPaths":
        for directory in (self.root, self.models, self.language_packs,
                          self.recordings, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def free_bytes(self) -> int:
        target = self.root if self.root.exists() else self.root.parent
        try:
            return shutil.disk_usage(target).free
        except OSError:
            return 0

    def writable(self) -> bool:
        try:
            self.recordings.mkdir(parents=True, exist_ok=True)
            probe = self.recordings / ".babelfishr-write-test"
            probe.write_bytes(b"ok")
            probe.unlink()
            return True
        except OSError:
            return False

    def to_dict(self) -> Dict[str, str]:
        return {k: str(v) for k, v in dataclasses.asdict(self).items()}


def bootstrap_environment(config=None) -> Dict[str, str]:
    """Prepare process-wide state that must be set before libraries import.

    Argos resolves its package directory once, at import time, so the managed
    location has to be in the environment before anything touches it. Every
    entry point - GUI, CLI, preparation and the packaged app - calls this
    first.
    """
    paths = (config.paths() if config is not None else AppPaths.resolve()).ensure()
    applied = {"ARGOS_PACKAGES_DIR": str(paths.language_packs)}
    os.environ.setdefault("ARGOS_PACKAGES_DIR", str(paths.language_packs))
    os.environ["ARGOS_PACKAGES_DIR"] = str(paths.language_packs)

    # ARGOS_PACKAGES_DIR moves the installed packages and nothing else. Argos
    # also resolves a data, config and cache directory from the XDG variables
    # at import time and creates all three immediately, so up to alpha 2 its
    # package index, its downloads cache and its configuration lived in three
    # folders in the operator's home that BabelFishR never mentioned. Done
    # here, before anything imports argostranslate.
    from .argos_home import clean_legacy_argos, configure_argos_home

    applied.update(configure_argos_home(paths.root))

    # A frozen bundle carries its own OpenSSL but not the system trust store,
    # so without this every HTTPS download fails with "unable to get local
    # issuer certificate" - which is exactly what happened to Argos on a real
    # Mac. Done here because this runs before any library that opens a socket.
    from .certificates import configure_certificates

    bundle = configure_certificates()
    if bundle:
        applied["SSL_CERT_FILE"] = bundle

    # Tidy what earlier builds left in the home directory. Best effort and
    # never fatal: it removes named files and empty directories only, and
    # reports anything it will not touch.
    try:
        legacy = clean_legacy_argos()
        if legacy.removed or legacy.kept:
            logging.getLogger(__name__).info(
                "legacy Argos directories:\n%s", legacy.summary())
    except Exception:  # noqa: BLE001 - housekeeping must not block startup
        logging.getLogger(__name__).debug("legacy Argos tidy-up failed",
                                          exc_info=True)
    return applied


def guard_download(mode: OperatingMode, what: str) -> None:
    """Refuse a download unless the mode permits one."""
    if not mode.allows_downloads:
        raise OfflineViolation(
            f"{mode.label} forbids downloads, and {what} would need one. "
            f"Run 'babelfishr prepare-field' with a network connection first.")


def guard_cloud(mode: OperatingMode, engine_name: str) -> None:
    if not mode.allows_cloud:
        raise OfflineViolation(
            f"{mode.label} forbids cloud providers; {engine_name} would send "
            f"data off this computer. Nothing leaves the machine in this mode, "
            f"even when the local engine is missing.")


def guard_mock(mode: OperatingMode, engine_name: str) -> None:
    if not mode.allows_mock:
        raise OfflineViolation(
            f"{mode.label} forbids placeholder output, and {engine_name} "
            f"produces placeholder text rather than a real result.")


def offline_environment() -> Dict[str, str]:
    """Library switches that stop model hubs fetching anything.

    These are the documented offline flags for huggingface_hub, transformers
    and datasets. They are a second line of defence behind the explicit
    ``local_files_only`` loading options, not a network control.

    Deliberately NOT included: ``NO_PROXY``. It only tells HTTP clients to
    bypass a configured proxy - it blocks nothing, and listing it here as a
    denial mechanism would be a false claim. BabelFishR's actual guarantee is
    architectural: in Field Offline no cloud provider is constructed and no
    download or install path is reachable, so there is nothing to make a
    request in the first place.
    """
    return {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
    }


def apply_offline_environment() -> List[str]:
    """Set the model-hub offline flags in this process.

    Returns the names set. This reduces the chance of a library reaching out;
    it is not, and must not be described as, a firewall.
    """
    applied = []
    for key, value in offline_environment().items():
        os.environ[key] = value
        applied.append(key)
    return applied
