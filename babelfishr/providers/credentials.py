"""Credential lookup. No key is ever stored in the repository or the config file.

Order of resolution:

1. the process environment (``ANTHROPIC_API_KEY`` and friends);
2. a ``.env`` file next to the config, if present (developer convenience);
3. the macOS Keychain, via ``security find-generic-password``.

Nothing here writes a credential to disk except :func:`store_in_keychain`,
which the operator triggers explicitly.
"""

from __future__ import annotations

import logging
import os
import pathlib
import platform
import shutil
import subprocess
from typing import Dict, Optional

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "BabelFishR"


def _read_dotenv(path: pathlib.Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_secret(name: str, dotenv_path: Optional[str] = None) -> Optional[str]:
    """Resolve a named secret, or ``None`` if it is not configured anywhere."""
    value = os.environ.get(name)
    if value:
        return value.strip()

    candidates = [pathlib.Path(dotenv_path)] if dotenv_path else []
    candidates += [pathlib.Path.cwd() / ".env",
                   pathlib.Path.home() / ".config" / "babelfishr" / ".env"]
    for candidate in candidates:
        found = _read_dotenv(candidate).get(name)
        if found:
            return found.strip()

    return get_from_keychain(name)


def keychain_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("security") is not None


def get_from_keychain(account: str,
                      service: str = KEYCHAIN_SERVICE) -> Optional[str]:
    """Read a password from the macOS Keychain. Silent no-op elsewhere."""
    if not keychain_available():
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        log.debug("keychain lookup failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def store_in_keychain(account: str, secret: str,
                      service: str = KEYCHAIN_SERVICE) -> bool:
    """Store a secret in the macOS Keychain. Returns True on success."""
    if not keychain_available():
        return False
    try:
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service,
             "-a", account, "-w", secret],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        log.debug("keychain store failed: %s", exc)
        return False
    return result.returncode == 0


def describe_source(name: str) -> str:
    """Where a secret would come from, for the settings UI."""
    if os.environ.get(name):
        return "environment variable"
    for candidate in (pathlib.Path.cwd() / ".env",
                      pathlib.Path.home() / ".config" / "babelfishr" / ".env"):
        if _read_dotenv(candidate).get(name):
            return f".env file ({candidate})"
    if get_from_keychain(name):
        return "macOS Keychain"
    return "not configured"
