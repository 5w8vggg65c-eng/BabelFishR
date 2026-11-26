"""A named Session's own save destination: a folder the operator chose.

A Session (a Conversation) can be pointed at a removable drive or any
writable folder. BabelFishR never writes loose files into that folder and
never creates it: everything goes into one area of its own beneath it,

    <chosen>/BabelFishR/
        recordings/{date}/{session}/<stamp>_<tx>.wav   (same layout as the
                                                        standard Recordings)
        transcripts/{date}/{session}/<stamp>_<tx>.json (portable transcripts)
        exports/                                       (default export folder)

and only that area is ever treated as BabelFishR's to delete from. The
shared application index (the SQLite database) and the settings stay local,
so every other Session opens whatever happens to the drive.

``""`` means the standard destination (``config.recording.directory``).
"""

from __future__ import annotations

import dataclasses
import errno
import os
import pathlib
import tempfile
from typing import Optional

#: The BabelFishR-owned folder created beneath the operator's chosen folder.
OWNED_DIRNAME = "BabelFishR"
RECORDINGS_DIRNAME = "recordings"
TRANSCRIPTS_DIRNAME = "transcripts"
EXPORTS_DIRNAME = "exports"

#: The concrete reasons a destination can be unusable. Shown to the operator.
NOT_FOUND = "not found (drive ejected or folder removed)"
NOT_A_FOLDER = "not a folder"
READ_ONLY = "read-only"
FULL = "full (no space left on the drive)"
NOT_ABSOLUTE = "not a complete folder path"
OWNED_NOT_A_FOLDER = f"a file named {OWNED_DIRNAME} is in the way"
OWNED_IS_LINK = (f"the {OWNED_DIRNAME} folder there is a link to somewhere "
                 f"else")


class SavePathError(ValueError):
    """A save destination was refused; nothing was changed.

    Raised when a chosen folder fails its preflight, when the destination of
    a Session that is recording would change, and when monitoring cannot
    start because the Session's destination is unavailable. The message is
    the operator-facing reason.
    """


@dataclasses.dataclass
class SavePathInfo:
    """Where a Session's new recordings go, and whether that works now."""

    chosen: str = ""
    """The operator's folder as stored; "" is the standard destination."""
    owned_dir: Optional[pathlib.Path] = None
    """``<chosen>/BabelFishR``, or None for the standard destination."""
    effective_dir: pathlib.Path = pathlib.Path()
    """The folder new recordings actually go into. For an unavailable custom
    destination this is the standard folder (where a running watch falls
    back to); a new watch refuses to start instead."""
    standard_dir: pathlib.Path = pathlib.Path()
    state: str = "standard"
    """``"standard"``, ``"ok"`` or ``"unavailable"``."""
    reason: str = ""
    """Why it is unavailable (e.g. :data:`NOT_FOUND`), or ""."""

    @property
    def is_custom(self) -> bool:
        return bool(self.chosen)

    @property
    def available(self) -> bool:
        return self.state != "unavailable"

    def problem(self) -> str:
        """An operator-facing sentence for an unavailable destination, or ""."""
        if self.state != "unavailable":
            return ""
        return describe(self.chosen, self.reason)

    def to_dict(self) -> dict:
        return {"chosen": self.chosen,
                "owned_dir": str(self.owned_dir) if self.owned_dir else "",
                "effective_dir": str(self.effective_dir),
                "standard_dir": str(self.standard_dir),
                "state": self.state, "reason": self.reason,
                "is_custom": self.is_custom}


def normalise(folder: str) -> str:
    """The stored spelling of a chosen folder: expanded and absolute."""
    path = pathlib.Path(str(folder).strip()).expanduser()
    if not path.is_absolute():
        return ""
    return os.path.normpath(str(path))


def owned_dir(chosen: str) -> pathlib.Path:
    return pathlib.Path(chosen).expanduser() / OWNED_DIRNAME


def recordings_dir(chosen: str) -> pathlib.Path:
    return owned_dir(chosen) / RECORDINGS_DIRNAME


def transcripts_dir(chosen: str) -> pathlib.Path:
    return owned_dir(chosen) / TRANSCRIPTS_DIRNAME


def exports_dir(chosen: str) -> pathlib.Path:
    return owned_dir(chosen) / EXPORTS_DIRNAME


def describe(chosen: str, reason: str) -> str:
    return f"Save path {owned_dir(chosen)}: {reason}"


def reason_for(exc: OSError) -> str:
    """The operator-facing reason for a failed write under a chosen folder."""
    if exc.strerror in (OWNED_IS_LINK, OWNED_NOT_A_FOLDER, NOT_A_FOLDER,
                        NOT_FOUND):
        return exc.strerror
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
        return NOT_FOUND
    if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
        return READ_ONLY
    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return FULL
    if exc.errno == errno.ENOTDIR:
        return NOT_A_FOLDER
    return f"cannot be written ({exc.strerror or type(exc).__name__})"


def ensure_owned_dir(chosen: str) -> pathlib.Path:
    """The owned area, created if missing - but only beneath a chosen folder
    that exists. Never creates the chosen folder or any parent of it, so an
    ejected drive cannot leave a phantom ``/Volumes/X`` on the internal disk.
    Raises OSError (FileNotFoundError when the chosen folder is gone)."""
    base = pathlib.Path(chosen).expanduser()
    if not base.is_dir():
        if os.path.lexists(base):
            raise NotADirectoryError(errno.ENOTDIR, NOT_A_FOLDER, str(base))
        raise FileNotFoundError(errno.ENOENT, NOT_FOUND, str(base))
    owned = base / OWNED_DIRNAME
    if owned.is_symlink():
        raise OSError(errno.EPERM, OWNED_IS_LINK, str(owned))
    try:
        os.mkdir(owned)                    # one level: never parents=True
    except FileExistsError:
        if not owned.is_dir():
            raise NotADirectoryError(errno.ENOTDIR, OWNED_NOT_A_FOLDER, str(owned))
    return owned


def make_within(owned: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    """Create *target* (a folder inside *owned*) one level at a time.

    Never ``mkdir(parents=True)``: if the drive disappears after the owned
    area was checked, the first missing level fails with FileNotFoundError
    instead of recreating the whole path on the internal disk.
    """
    relative = target.relative_to(owned)
    current = owned
    for part in relative.parts:
        current = current / part
        try:
            os.mkdir(current)
        except FileExistsError:
            if not current.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, NOT_A_FOLDER, str(current))
    return target


def ensure_subdir(chosen: str, *parts: str) -> pathlib.Path:
    """A folder inside the owned area (created beneath it as needed)."""
    owned = ensure_owned_dir(chosen)
    return make_within(owned, owned.joinpath(*parts))


def preflight(chosen: str) -> str:
    """Can new recordings be written under *chosen* right now?

    Returns "" when they can, otherwise the concrete reason. Checks that the
    folder exists and is a folder, creates the owned area beneath it if
    needed, then writes a temporary file there, moves it into place
    atomically and deletes it - the same operations a recording and a
    portable transcript use.
    """
    if not chosen:
        return ""
    base = pathlib.Path(chosen).expanduser()
    if not base.is_absolute():
        return NOT_ABSOLUTE
    if not os.path.lexists(base):
        return NOT_FOUND
    if not base.is_dir():
        return NOT_A_FOLDER
    owned = base / OWNED_DIRNAME
    if owned.is_symlink():
        return OWNED_IS_LINK
    if os.path.lexists(owned) and not owned.is_dir():
        return OWNED_NOT_A_FOLDER
    try:
        owned = ensure_owned_dir(chosen)
    except OSError as exc:
        return reason_for(exc)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix=".babelfishr-probe-", dir=str(owned))
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"BabelFishR write check\n")
            handle.flush()
            os.fsync(handle.fileno())
        final = owned / ".babelfishr-write-check"
        os.replace(temp, final)
        temp = str(final)
        os.unlink(final)
        temp = None
    except OSError as exc:
        return reason_for(exc)
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass
    return ""


def write_json_atomic(path: pathlib.Path, text: str) -> None:
    """Write *text* to *path* so a reader sees the old file or the new one,
    never half of either: a temporary file beside it, flushed, then
    ``os.replace``. The temporary file is removed on failure."""
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
