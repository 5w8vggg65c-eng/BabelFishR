"""Complete removal of BabelFishR from one macOS user account.

This is the logic behind ``Uninstall BabelFishR.app``. It is deliberately a
separate, boring module with no Qt import, so the dangerous part - deciding
what to delete - can be tested exhaustively against temporary fake homes.

Three rules shape everything here:

1. **Allowlist only.** Every path comes from :data:`_MANAGED` plus the two
   canonical application locations, joined to a home directory. Nothing is ever
   read from settings.toml or any other configuration file. A settings file an
   attacker (or a typo) could point at ``/`` must not be able to steer a
   recursive delete, so configuration is not consulted at all.

2. **Symlinks are never followed.** If an allowlisted path is itself a symlink
   we unlink the link and leave whatever it pointed at alone - and say so in
   the report. Directory trees are removed with :func:`shutil.rmtree`, which
   unlinks nested symlinks rather than descending through them.

3. **The report is honest.** Anything that could not be removed is named. The
   uninstaller never claims complete removal while something remains.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import shlex
import shutil
import subprocess
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

APP_BUNDLE_NAME = "BabelFishR.app"
APP_BUNDLE_ID = "org.babelfishr.app"
UNINSTALLER_BUNDLE_ID = "org.babelfishr.uninstaller"
APP_SUPPORT = "Library/Application Support/BabelFishR"

#: Everything BabelFishR creates inside a user's home directory, as fixed
#: relative paths. Order matters only for display: the operator reads this
#: list, so the irreplaceable things come first.
_MANAGED: Tuple[Tuple[str, str, bool], ...] = (
    (f"{APP_SUPPORT}/Recordings", "Recordings (audio captured from the radio)", True),
    (f"{APP_SUPPORT}/babelfishr.sqlite3",
     "Database of transmissions, transcripts and translations", True),
    (f"{APP_SUPPORT}/models", "Whisper speech-recognition models", False),
    (f"{APP_SUPPORT}/language-packs", "Argos language packs", False),
    (f"{APP_SUPPORT}/Logs", "Logs and diagnostic reports", False),
    (f"{APP_SUPPORT}/settings.toml", "Settings", False),
    (APP_SUPPORT, "Application Support folder (anything else left in it)", False),
    (".config/babelfishr", "Command-line configuration folder", False),
    (f"Library/Caches/{APP_BUNDLE_ID}", "Cache", False),
    ("Library/Caches/BabelFishR", "Cache", False),
    (f"Library/HTTPStorages/{APP_BUNDLE_ID}", "Cached web data", False),
    (f"Library/Preferences/{APP_BUNDLE_ID}.plist", "Preferences", False),
    (f"Library/Saved Application State/{APP_BUNDLE_ID}.savedState",
     "Saved application state", False),
)

#: A path is only ever handed to a privileged helper if it looks like this.
#: No quotes, no backslashes, no newlines, no shell metacharacters at all.
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9 ._/-]+$")


class UninstallRefused(Exception):
    """Removal was not attempted, because it would not have been safe."""


@dataclasses.dataclass(frozen=True)
class RemovalItem:
    """One allowlisted thing that would be deleted."""

    path: pathlib.Path
    description: str
    irreplaceable: bool = False

    @property
    def exists(self) -> bool:
        # lexists: a broken symlink still has to be cleaned up.
        return os.path.lexists(self.path)

    @property
    def is_symlink(self) -> bool:
        return os.path.islink(self.path)

    def size_bytes(self) -> int:
        """Best effort, and never through a symlink."""
        if self.is_symlink or not self.exists:
            return 0
        try:
            if self.path.is_dir():
                total = 0
                for root, dirs, files in os.walk(self.path, followlinks=False):
                    for name in files:
                        candidate = pathlib.Path(root) / name
                        if not candidate.is_symlink():
                            try:
                                total += candidate.stat().st_size
                            except OSError:
                                pass
                return total
            return self.path.stat().st_size
        except OSError:
            return 0


@dataclasses.dataclass(frozen=True)
class UninstallPlan:
    """Exactly what will be deleted, computed before anything is touched."""

    home: pathlib.Path
    items: Tuple[RemovalItem, ...]

    def present(self) -> List[RemovalItem]:
        return [item for item in self.items if item.exists]

    def missing(self) -> List[RemovalItem]:
        return [item for item in self.items if not item.exists]

    def total_bytes(self) -> int:
        return sum(item.size_bytes() for item in self.present())

    def has_recordings(self) -> bool:
        return any(item.irreplaceable for item in self.present())

    def paths(self) -> List[pathlib.Path]:
        return [item.path for item in self.items]


@dataclasses.dataclass
class UninstallReport:
    """What actually happened. Never rounded up into 'done'."""

    removed: List[pathlib.Path] = dataclasses.field(default_factory=list)
    links_removed: List[pathlib.Path] = dataclasses.field(default_factory=list)
    absent: List[pathlib.Path] = dataclasses.field(default_factory=list)
    failed: List[Tuple[pathlib.Path, str]] = dataclasses.field(default_factory=list)
    notes: List[str] = dataclasses.field(default_factory=list)
    microphone_permission_reset: Optional[bool] = None
    dry_run: bool = False

    @property
    def complete(self) -> bool:
        """True only when nothing allowlisted is left behind."""
        return not self.failed

    def leftovers(self) -> List[pathlib.Path]:
        return [path for path, _ in self.failed]

    def summary(self) -> str:
        lines: List[str] = []
        if self.dry_run:
            lines.append("DRY RUN - nothing was deleted.")
        if self.removed:
            lines.append(f"Removed ({len(self.removed)}):")
            lines += [f"  ✓ {path}" for path in self.removed]
        if self.links_removed:
            lines.append(
                f"Removed as links only, leaving what they pointed at "
                f"({len(self.links_removed)}):")
            lines += [f"  ✓ {path}" for path in self.links_removed]
        if self.absent:
            lines.append(f"Not present, nothing to do ({len(self.absent)}):")
            lines += [f"  · {path}" for path in self.absent]
        if self.failed:
            lines.append(f"COULD NOT BE REMOVED ({len(self.failed)}):")
            lines += [f"  ✗ {path} - {why}" for path, why in self.failed]
        if self.microphone_permission_reset is True:
            lines.append("Microphone permission for BabelFishR was reset.")
        elif self.microphone_permission_reset is False:
            lines.append(
                "Microphone permission could not be reset automatically. "
                "Remove BabelFishR under System Settings > Privacy & Security "
                "> Microphone if it is still listed.")
        lines += self.notes
        if self.failed:
            lines.append("")
            lines.append(
                "BabelFishR was NOT completely removed. The items above are "
                "still on this computer.")
        elif not self.dry_run:
            lines.append("")
            lines.append("BabelFishR was completely removed.")
        return "\n".join(lines)


# ---------------------------------------------------------------- planning


def application_locations(home: pathlib.Path) -> List[pathlib.Path]:
    """The only two places an installed BabelFishR.app is accepted from.

    Not a search of the whole disk: a Spotlight sweep could turn up a copy in
    a Downloads folder, a Time Machine backup or another user's account, and
    none of those are ours to delete.
    """
    return [pathlib.Path("/Applications") / APP_BUNDLE_NAME,
            home / "Applications" / APP_BUNDLE_NAME]


def build_plan(home: Optional[os.PathLike] = None) -> UninstallPlan:
    """Compute the removal plan for one home directory.

    ``home`` is injected rather than discovered so tests can only ever run
    against a temporary directory. Configuration is deliberately not consulted.
    """
    root = pathlib.Path(home).expanduser() if home is not None else pathlib.Path.home()
    root = root.resolve() if root.exists() else root
    items: List[RemovalItem] = [
        RemovalItem(root / relative, description, irreplaceable)
        for relative, description, irreplaceable in _MANAGED
    ]
    items += [RemovalItem(path, "The BabelFishR application")
              for path in application_locations(root)]
    for item in items:
        _assert_allowlisted(root, item.path)
    return UninstallPlan(home=root, items=tuple(items))


def _assert_allowlisted(home: pathlib.Path, path: pathlib.Path) -> None:
    """Refuse anything that is not one of the paths we constructed ourselves.

    Belt and braces: build_plan already only joins constants, so this can only
    fire if somebody later adds a path from an untrusted source.
    """
    allowed = {home / relative for relative, _, _ in _MANAGED}
    allowed |= set(application_locations(home))
    if path not in allowed:
        raise UninstallRefused(f"{path} is not on the removal allowlist")
    if path == home or path == pathlib.Path("/") or len(path.parts) <= 2:
        raise UninstallRefused(f"{path} is too broad to delete")


def describe_plan(plan: UninstallPlan) -> str:
    """The exact text shown to the operator before anything is deleted."""
    lines = ["These items will be permanently deleted:", ""]
    present = plan.present()
    if not present:
        lines.append("  (nothing found - BabelFishR does not appear to be "
                     "installed for this user)")
    for item in present:
        marker = " ← CANNOT BE RECOVERED" if item.irreplaceable else ""
        suffix = " (a link; only the link is removed)" if item.is_symlink else ""
        lines.append(f"  {item.path}{suffix}")
        lines.append(f"      {item.description}{marker}")
    missing = plan.missing()
    if missing:
        lines += ["", "Not present on this computer (nothing to do):"]
        lines += [f"  {item.path}" for item in missing]
    return "\n".join(lines)


# ---------------------------------------------------------- running check


def app_is_running(*, runner=subprocess.run) -> Optional[bool]:
    """Is BabelFishR running right now?

    ``None`` means we could not tell - which is treated as "assume it is",
    because deleting a running application's bundle out from under it leaves
    half-written files and a confusing crash.
    """
    try:
        result = runner(["/usr/bin/pgrep", "-f", "BabelFishR.app/Contents/MacOS"],
                        capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return bool((result.stdout or "").strip())
    if result.returncode == 1:
        return False
    return None


def request_quit(*, runner=subprocess.run) -> bool:
    """Ask BabelFishR to quit normally. Never force-kills anything."""
    try:
        result = runner(
            ["/usr/bin/osascript", "-e",
             'tell application id "org.babelfishr.app" to quit'],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# ------------------------------------------------------------- performing


def _remove_one(item: RemovalItem, report: UninstallReport, *,
                dry_run: bool) -> bool:
    """Delete one allowlisted item. Returns True when nothing is left."""
    if not item.exists:
        report.absent.append(item.path)
        return True
    if dry_run:
        report.removed.append(item.path)
        return True
    try:
        if item.is_symlink:
            # The link, not its target. Following it could delete anything.
            os.unlink(item.path)
            report.links_removed.append(item.path)
            return True
        if item.path.is_dir():
            # rmtree unlinks nested symlinks instead of descending them.
            shutil.rmtree(item.path)
        else:
            item.path.unlink()
    except OSError as exc:
        report.failed.append((item.path, exc.strerror or str(exc)))
        return False
    if os.path.lexists(item.path):
        report.failed.append((item.path, "it is still there after deletion"))
        return False
    report.removed.append(item.path)
    return True


def reset_microphone_permission(*, runner=subprocess.run) -> bool:
    """Best effort ``tccutil reset``; a failure is reported, never fatal."""
    try:
        result = runner(["/usr/bin/tccutil", "reset", "Microphone", APP_BUNDLE_ID],
                        capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _authorization_command(paths: Sequence[pathlib.Path]) -> str:
    """Build the one shell command a macOS authorization prompt may run.

    Every path is validated against :data:`_SAFE_PATH` and then quoted. A path
    that does not match is refused outright rather than escaped cleverly:
    there is no interpolation of operator-supplied text into a privileged
    command anywhere in this program.
    """
    quoted: List[str] = []
    for path in paths:
        text = str(path)
        if not _SAFE_PATH.match(text):
            raise UninstallRefused(
                f"refusing to pass {text!r} to a privileged command")
        quoted.append(shlex.quote(text))
    return "/bin/rm -rf -- " + " ".join(quoted)


def elevate_removal(paths: Sequence[pathlib.Path], *,
                    runner=subprocess.run) -> bool:
    """Ask macOS for authorization to remove paths a normal delete could not.

    Standard system prompt via osascript; the command is fixed and its
    arguments are validated absolute paths from the plan.
    """
    if not paths:
        return True
    command = _authorization_command(paths)
    script = ('do shell script "%s" with administrator privileges'
              % command.replace("\\", "\\\\").replace('"', '\\"'))
    try:
        result = runner(["/usr/bin/osascript", "-e", script],
                        capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def uninstall(plan: UninstallPlan, *, dry_run: bool = False,
              runner=subprocess.run, allow_elevation: bool = True,
              running_check=None) -> UninstallReport:
    """Carry out the plan, or describe it exactly without touching anything.

    Raises :class:`UninstallRefused` when BabelFishR is still running: a real
    dry run is still allowed then, because it deletes nothing.
    """
    report = UninstallReport(dry_run=dry_run)
    if not dry_run:
        check = running_check or (lambda: app_is_running(runner=runner))
        if check() is not False:
            raise UninstallRefused(
                "BabelFishR is still running (or could not be confirmed as "
                "stopped). Quit BabelFishR and try again; nothing has been "
                "deleted.")

    for item in plan.items:
        _assert_allowlisted(plan.home, item.path)
        _remove_one(item, report, dry_run=dry_run)

    if report.failed and allow_elevation and not dry_run:
        stubborn = list(report.leftovers())
        try:
            elevated = elevate_removal(stubborn, runner=runner)
        except UninstallRefused as exc:
            elevated = False
            report.notes.append(str(exc))
        if elevated:
            still_there = [(path, "it is still there after deletion")
                           for path in stubborn if os.path.lexists(path)]
            report.removed += [path for path in stubborn
                               if not os.path.lexists(path)]
            report.failed = still_there
        else:
            report.notes.append(
                "An administrator prompt was needed and did not complete, so "
                "some items were left in place.")

    if not dry_run:
        report.microphone_permission_reset = reset_microphone_permission(
            runner=runner)
    return report


def removal_scope() -> List[str]:
    """Plain-language list of what complete removal deletes.

    Used by the uninstaller window, the release notes and the documentation,
    so the three cannot drift apart.
    """
    return [
        "The BabelFishR application itself",
        "Whisper speech-recognition models (several gigabytes)",
        "Argos language packs",
        "Every recording BabelFishR has made - these cannot be recovered",
        "The transmission database, including all transcripts and translations",
        "All settings",
        "Diagnostic reports and logs",
        "BabelFishR's caches, preferences and saved application state",
    ]
