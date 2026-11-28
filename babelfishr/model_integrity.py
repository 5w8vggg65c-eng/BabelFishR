"""Integrity of installed Whisper models: pinned revision, sha256, record.

Two faults made this necessary. A stale local file with the length of the
expected one was reused as if it were valid (presence was judged by size
alone), and a repair could combine files of different repository revisions
(each file resolved "main" at its own moment). Now:

* every model has a pin (model_pins.PINS): one immutable commit and the
  sha256 of each file at that commit. Every request names that commit.
* every reused and every fetched file is hashed (streaming, on the calling
  worker thread) and compared with the pin. Size is only a cheap first
  rejection, never an acceptance.
* the outcome is recorded next to the model (:data:`RECORD_NAME`), and only a
  record that matches the pin and the files as they are now on disk counts as
  "verified" (:func:`integrity_state`, cheap: no hashing). A model installed
  before records existed is "unverified" until it is hashed; a model without
  a usable pin is "unpinned" and never verified.

The cheap state distinguishes four situations, because they deserve
different treatment:

* ``unverified`` - no record (a legacy install from before records, or one
  recorded against an older pin). Not known to be good, not known to be bad:
  it is used, and said to be unverified. Hashing it would make every first
  load after an upgrade read the whole model, and refusing it would strand
  field kits prepared by an earlier version with no network to repair them.
* ``verified`` - a record that matches the pin and every file as it is now.
* ``changed`` - a record said verified, but a pinned file's size,
  modification time or inode is no longer what was hashed, a pinned file is
  missing, or an extra loadable asset appeared. These are no longer the bytes
  that were checked: Field Check fails until they are hashed again
  (:func:`verify_model` / "Verify checksums", local only), and the engine
  re-hashes them before loading and refuses them if they fail.
* ``mismatch`` - hashing found files that are not the published ones. Never
  loaded until a local verification (after restoring) or a repair passes.
"""

from __future__ import annotations

import dataclasses
import enum
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

#: The per-install record, inside the model's own directory (so removing the
#: model removes its record).
RECORD_NAME = ".babelfishr-integrity.json"
RECORD_FORMAT = 1

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: Hashing reads this much at a time (and checks for cancellation between).
CHUNK = 4 * 1024 * 1024


class IntegrityError(RuntimeError):
    """A model's files do not match its pinned identity."""


class UnpinnedModel(IntegrityError):
    """No usable pinned identity: the model cannot be verified, so it is not
    installed (fail closed)."""


class Integrity(str, enum.Enum):
    VERIFIED = "verified"
    """A record matching the pin and the files currently on disk."""

    UNVERIFIED = "unverified"
    """No record (a legacy install), or one made against another pin."""

    CHANGED = "changed"
    """Verified once, but the files are no longer the ones that were hashed
    (size, modification time or inode differs, a file is missing, or an
    extra loadable asset appeared). Not usable until hashed again."""

    MISMATCH = "mismatch"
    """Hashing found files that do not match the pin."""

    UNPINNED = "unpinned"
    """No pinned identity for this model: it cannot be verified."""


@dataclasses.dataclass(frozen=True)
class PinnedFile:
    name: str
    sha256: str
    size: Optional[int] = None
    git_oid: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Pin:
    model: str
    repo: str
    revision: str
    files: Tuple[PinnedFile, ...]
    #: Former ids of the repository (the Hub redirects them to ``repo``);
    #: records made under one of them still count.
    aliases: Tuple[str, ...] = ()

    @property
    def names(self) -> List[str]:
        return [f.name for f in self.files]

    def file(self, name: str) -> PinnedFile:
        for pinned in self.files:
            if pinned.name == name:
                return pinned
        raise KeyError(name)


def _pins() -> dict:
    from . import model_pins

    return model_pins.PINS


def pin_for(model_name: str, pins: Optional[dict] = None) -> Pin:
    """The pinned identity of *model_name*, or :class:`UnpinnedModel`.

    A pin must name a full 40-hex commit and a sha256 for each file, and its
    files must make a loadable model (weights, config and a tokenizer asset);
    anything less cannot establish identity and is refused.
    """
    from .providers.whisper_local import REQUIRED_ASSETS, TOKENIZER_ASSETS

    entry = (pins if pins is not None else _pins()).get(str(model_name))
    hint = ("this build has no pinned revision and checksums for the Whisper "
            f"model {model_name!r}, so its integrity cannot be established "
            "(maintainers: run scripts/refresh_model_pins.py)")
    if not isinstance(entry, dict):
        raise UnpinnedModel(hint)
    revision = str(entry.get("revision") or "")
    raw_files = entry.get("files") or {}
    if not _COMMIT.match(revision) or not entry.get("repo"):
        raise UnpinnedModel(hint)
    if not raw_files:
        raise UnpinnedModel(
            f"the Whisper model {model_name!r} is pinned to {entry['repo']} at "
            f"{revision[:12]}, but this build has no per-file checksums for it, "
            "so its integrity cannot be established (maintainers: run "
            "scripts/refresh_model_pins.py --revision pinned)")
    files = []
    for name, meta in raw_files.items():
        digest = str((meta or {}).get("sha256") or "").lower()
        if not _SHA256.match(digest):
            raise UnpinnedModel(f"{hint} - {name} has no sha256")
        size = meta.get("size")
        files.append(PinnedFile(str(name), digest,
                                int(size) if size is not None else None,
                                meta.get("git_oid")))
    names = {f.name for f in files}
    if not set(REQUIRED_ASSETS) <= names or not names & set(TOKENIZER_ASSETS):
        raise UnpinnedModel(f"{hint} - the pinned file list is not a complete model")
    # Smallest first and the weights last, as faster-whisper fetches them.
    files.sort(key=lambda f: (f.name == "model.bin", f.size or 0, f.name))
    aliases = tuple(str(a) for a in entry.get("repo_aliases") or ())
    return Pin(str(model_name), str(entry["repo"]), revision, tuple(files), aliases)


def sha256_file(path, cancelled: Optional[Callable[[], None]] = None,
                chunk: int = CHUNK) -> str:
    """Streaming sha256 of *path*. *cancelled* is called between chunks and
    may raise to stop."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if cancelled is not None:
                cancelled()
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


#: The model assets a loader reads (whisper_local.ALLOW_PATTERNS, repeated
#: here to avoid a circular import; a test keeps them equal).
LOADABLE_PATTERNS = ("config.json", "preprocessor_config.json", "model.bin",
                     "tokenizer.json", "vocabulary.*")


def unexpected_files(directory, pin: Pin) -> List[str]:
    """Loadable model assets in *directory* that the pin does not name (for
    example a vocabulary.txt beside a pinned vocabulary.json): the loader
    could read them, and they were never checked."""
    try:
        entries = list(pathlib.Path(directory).iterdir())
    except OSError:
        return []
    names = set(pin.names)
    return sorted(entry.name for entry in entries
                  if entry.name not in names and entry.is_file()
                  and any(fnmatch.fnmatchcase(entry.name, pattern)
                          for pattern in LOADABLE_PATTERNS))


def check_file(directory, pinned: PinnedFile,
               cancelled: Optional[Callable[[], None]] = None) -> str:
    """"ok", "missing" or "mismatch" for one pinned file, by sha256."""
    path = pathlib.Path(directory) / pinned.name
    if not path.is_file():
        return "missing"
    if pinned.size is not None and path.stat().st_size != pinned.size:
        return "mismatch"                     # cheap rejection, never acceptance
    return "ok" if sha256_file(path, cancelled) == pinned.sha256 else "mismatch"


# ---- the record -------------------------------------------------------------
def record_path(directory) -> pathlib.Path:
    return pathlib.Path(directory) / RECORD_NAME


def read_record(directory) -> Optional[dict]:
    try:
        data = json.loads(record_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_record(directory) -> None:
    try:
        record_path(directory).unlink()
    except FileNotFoundError:
        pass


def write_record(directory, pin: Pin, state: Integrity,
                 problems: Optional[Dict[str, str]] = None) -> dict:
    """Record the outcome of hashing *directory* against *pin* (atomically)."""
    directory = pathlib.Path(directory)
    files = {}
    for pinned in pin.files:
        entry = {"sha256": pinned.sha256}
        path = directory / pinned.name
        if path.is_file():
            stat = path.stat()
            entry.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                         ino=stat.st_ino)
        files[pinned.name] = entry
    record = {"format": RECORD_FORMAT, "model": pin.model, "repo": pin.repo,
              "revision": pin.revision, "state": state.value,
              "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "files": files, "problems": dict(problems or {})}
    tmp = directory / (RECORD_NAME + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, record_path(directory))
    return record


def integrity_state(directory, model_name: str,
                    pins: Optional[dict] = None) -> Tuple[Integrity, str]:
    """``(Integrity, detail)`` without hashing anything (cheap enough for a
    readiness check or a menu): a record counts only when it names the
    current pin and each file still has the size, modification time and
    inode it had when it was hashed (records written before inodes were
    recorded are compared on size and time). A verified record whose files
    no longer match that is CHANGED, not merely unverified."""
    try:
        pin = pin_for(model_name, pins)
    except UnpinnedModel as exc:
        return Integrity.UNPINNED, str(exc)
    record = read_record(directory)
    if record is None:
        return Integrity.UNVERIFIED, "checksums not verified yet"
    if (record.get("repo") not in (pin.repo,) + pin.aliases
            or record.get("revision") != pin.revision):
        return Integrity.UNVERIFIED, "verified against a different revision"
    if record.get("state") == Integrity.MISMATCH.value:
        bad = ", ".join(sorted(record.get("problems") or {})) or "files"
        return Integrity.MISMATCH, f"checksum mismatch: {bad}"
    if record.get("state") != Integrity.VERIFIED.value:
        return Integrity.UNVERIFIED, "checksums not verified yet"
    recorded = record.get("files") or {}
    if any((recorded.get(pinned.name) or {}).get("sha256") != pinned.sha256
           for pinned in pin.files):
        return Integrity.UNVERIFIED, "verified against different checksums"
    for pinned in pin.files:
        entry = recorded.get(pinned.name) or {}
        try:
            stat = (pathlib.Path(directory) / pinned.name).stat()
        except OSError:
            return (Integrity.CHANGED,
                    f"{pinned.name} is missing since the model was verified")
        if (stat.st_size != entry.get("size")
                or stat.st_mtime_ns != entry.get("mtime_ns")
                or ("ino" in entry and stat.st_ino != entry.get("ino"))):
            return Integrity.CHANGED, f"{pinned.name} changed since it was verified"
    extra = unexpected_files(directory, pin)
    if extra:
        return (Integrity.CHANGED,
                f"{', '.join(extra)} appeared since the model was verified")
    return Integrity.VERIFIED, f"checksums verified (revision {pin.revision[:12]})"


def is_verified(directory, model_name: str) -> bool:
    return integrity_state(directory, model_name)[0] is Integrity.VERIFIED


#: What Field Check and the engine say about a model that is not usable as is.
CHANGED_WORDS = "changed since it was verified - verify checksums"
MISMATCH_WORDS = "does not match its published checksums"


def verify_directory(directory, pin: Pin, names: Optional[Iterable[str]] = None,
                     cancelled: Optional[Callable[[], None]] = None,
                     on_file: Optional[Callable[[str], None]] = None
                     ) -> Dict[str, str]:
    """Hash the pinned files of *directory* (all, or *names*): ``{name:
    "ok"|"missing"|"mismatch"}``. Reads local files only - no network."""
    wanted = list(names) if names is not None else pin.names
    outcome = {}
    for name in wanted:
        if on_file is not None:
            on_file(name)
        outcome[name] = check_file(directory, pin.file(name), cancelled)
    return outcome
