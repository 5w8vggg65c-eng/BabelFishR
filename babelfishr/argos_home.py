"""Keep every file Argos Translate creates inside BabelFishR's own root.

``argostranslate.settings`` resolves three directories **at import time** and
creates them immediately::

    data_dir   = $XDG_DATA_HOME/argos-translate      (default ~/.local/share)
    config_dir = $XDG_CONFIG_HOME/argos-translate    (default ~/.config)
    cache_dir  = $XDG_CACHE_HOME/argos-translate     (default ~/.local/cache)

``ARGOS_PACKAGES_DIR`` moves the installed packages and nothing else, which is
why BabelFishR up to alpha 2 had its language packs under Application Support
while the local package index (``data_dir/index.json``), the downloads cache
(``cache_dir/downloads``) and the configuration all still landed in three
directories in the operator's home that BabelFishR never told them about.

So the XDG roots are set too - but only for the moment Argos reads them.
Leaving them set would relocate every other XDG-aware library in this process
and would be inherited by every child process BabelFishR launches, which is not
ours to change.

Nothing here opens a socket or touches TLS.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import pathlib
import sys
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Where the Argos roots live inside the managed Application Support root.
ARGOS_SUBDIRECTORY = "argos"

_XDG_VARS = ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")

#: Directories argostranslate created in the home directory before this
#: existed. Relative to the home directory, deepest first.
LEGACY_DIRECTORIES: Tuple[str, ...] = (
    ".local/cache/argos-translate/downloads",
    ".local/cache/argos-translate",
    ".local/share/argos-translate",
    ".config/argos-translate",
)

#: The only files we will delete from a legacy directory, by exact name, and
#: only when the accompanying check confirms the content is what we think.
#: Everything else is left alone and named in the report - a second Argos
#: installation may own it.
_LEGACY_FILES: Tuple[Tuple[str, str], ...] = (
    (".local/share/argos-translate/index.json", "argos-index"),
)


def argos_root(app_root: os.PathLike) -> pathlib.Path:
    return pathlib.Path(app_root) / ARGOS_SUBDIRECTORY


def argos_environment(app_root: os.PathLike) -> Dict[str, str]:
    """The XDG values that put every Argos root under ``app_root``."""
    root = argos_root(app_root)
    return {"XDG_DATA_HOME": str(root / "data"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache")}


def expected_argos_paths(app_root: os.PathLike) -> Dict[str, pathlib.Path]:
    """What ``argostranslate.settings`` will resolve to, for verification."""
    root = argos_root(app_root)
    data = root / "data" / "argos-translate"
    cache = root / "cache" / "argos-translate"
    return {"data_dir": data,
            "config_dir": root / "config" / "argos-translate",
            "cache_dir": cache,
            "downloads_dir": cache / "downloads",
            "local_package_index": data / "index.json"}


@contextlib.contextmanager
def argos_environment_applied(app_root: os.PathLike):
    """Set the XDG roots, and put the environment back afterwards."""
    wanted = argos_environment(app_root)
    previous = {name: os.environ.get(name) for name in _XDG_VARS}
    os.environ.update(wanted)
    try:
        yield wanted
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def configure_argos_home(app_root: os.PathLike) -> Dict[str, str]:
    """Import ``argostranslate.settings`` with our roots in the environment.

    Returns the values that were applied. Importing here, rather than hoping
    every later caller happens to import after us, is the whole point: the
    directories are created during that import and never re-read.
    """
    applied = argos_environment(app_root)
    if "argostranslate.settings" in sys.modules:
        # Somebody imported it before bootstrap ran. Reloading would leave
        # other modules holding stale references, so say so rather than
        # pretend, and let verify_argos_paths report the truth.
        log.warning("argostranslate.settings was imported before its "
                    "directories were configured; Argos data may be outside "
                    "%s", argos_root(app_root))
        return {}
    with argos_environment_applied(app_root):
        try:
            import argostranslate.settings  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - optional engine
            log.info("argostranslate is not importable (%s); its directories "
                     "will be configured when it is installed", exc)
            return applied
    return applied


def resolved_argos_paths() -> Dict[str, pathlib.Path]:
    """What Argos actually resolved, read back from the imported module."""
    import argostranslate.settings as settings

    return {name: pathlib.Path(getattr(settings, name))
            for name in ("data_dir", "config_dir", "cache_dir",
                         "downloads_dir", "package_data_dir",
                         "local_package_index")}


def verify_argos_paths(app_root: os.PathLike) -> List[str]:
    """Names of any Argos path that ended up outside ``app_root``."""
    root = pathlib.Path(app_root).resolve()
    stray: List[str] = []
    try:
        resolved = resolved_argos_paths()
    except Exception:  # noqa: BLE001 - not installed, nothing to verify
        return stray
    for name, path in resolved.items():
        try:
            candidate = path.resolve()
        except OSError:
            candidate = path
        if root != candidate and root not in candidate.parents:
            stray.append(f"{name}={path}")
    return stray


# ------------------------------------------------------------ legacy tidy


@dataclasses.dataclass
class LegacyCleanup:
    """What was tidied away, and - just as importantly - what was not."""

    removed: List[pathlib.Path] = dataclasses.field(default_factory=list)
    kept: List[Tuple[pathlib.Path, str]] = dataclasses.field(default_factory=list)
    dry_run: bool = False

    @property
    def complete(self) -> bool:
        return not self.kept

    def summary(self) -> str:
        lines: List[str] = []
        if self.removed:
            lines.append("Legacy Argos directories removed:")
            lines += [f"  ✓ {path}" for path in self.removed]
        if self.kept:
            lines.append("Left in place, because the content is not ours to "
                         "delete:")
            lines += [f"  · {path} - {why}" for path, why in self.kept]
        return "\n".join(lines)


def _is_argos_index(path: pathlib.Path) -> bool:
    """An Argos package index is a JSON list of package descriptions."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(parsed, list) or not parsed:
        return False
    # Every entry describes a package. A file of somebody else's JSON, or a
    # list of anything else, is not ours and is left alone.
    return all(isinstance(entry, dict) for entry in parsed)


_CHECKS = {"argos-index": _is_argos_index}


def clean_legacy_argos(home: Optional[os.PathLike] = None, *,
                       dry_run: bool = False) -> LegacyCleanup:
    """Remove what alpha 2 left in the home directory - and nothing else.

    The rules, in order of how much damage getting them wrong would do:

    1. Only exact, named files are deleted, and only when a content check
       confirms they are what the name says. Anything else is left.
    2. A legacy directory is removed only when it is **empty**, with
       ``os.rmdir``, which fails rather than recursing. There is no
       ``rmtree`` in this function and there must never be one: these
       directories are shared with any other Argos installation on the
       machine, and a stray package there belongs to somebody else.
    3. Whatever remains is reported by exact path. Nothing is ever described
       as removed unless it is gone.
    """
    root = (pathlib.Path(home).expanduser() if home is not None
            else pathlib.Path.home())
    report = LegacyCleanup(dry_run=dry_run)

    for relative, check in _LEGACY_FILES:
        path = root / relative
        if not path.exists() or path.is_dir() or path.is_symlink():
            continue
        if not _CHECKS[check](path):
            report.kept.append((path, "it is not the file BabelFishR wrote"))
            continue
        if dry_run:
            report.removed.append(path)
            continue
        try:
            path.unlink()
        except OSError as exc:
            report.kept.append((path, exc.strerror or str(exc)))
        else:
            report.removed.append(path)

    for relative in LEGACY_DIRECTORIES:
        path = root / relative
        if not path.exists() or path.is_symlink() or not path.is_dir():
            continue
        remaining = sorted(entry.name for entry in path.iterdir())
        if dry_run:
            if remaining:
                report.kept.append(
                    (path, f"not empty: {', '.join(remaining[:5])}"))
            else:
                report.removed.append(path)
            continue
        try:
            path.rmdir()          # never rmtree: fails if anything is left
        except OSError:
            report.kept.append(
                (path, f"not empty: {', '.join(remaining[:5])}"
                       if remaining else "it could not be removed"))
        else:
            report.removed.append(path)
    return report
