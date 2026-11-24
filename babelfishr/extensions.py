"""Explicit extension hooks, and which product is running.

BabelFishR never looks for extensions. Nothing here (or anywhere in
BabelFishR) scans package entry points, imports a plugin package, reads a
plugin directory or honours a plugin environment variable. An extension
exists only when a separate program - KingFishR's own entry point - imports
it and calls :func:`register` before it starts the application. Started as
BabelFishR, the registry is empty and every hook below is a no-op.

The product identity decides where a program keeps its settings, database,
recordings, models and logs, and which environment variables it reads, so a
second product never opens BabelFishR's data by default.

Hooks an extension object may provide (each optional; looked up by name):

``receiver_status(config) -> dict``
    The named receiver input's status: ``available`` (bool), ``problems``
    (list of str), ``offered`` (list it as an input here), ``label`` and
    ``summary`` (short text for the INPUT line).
``receiver_input_text() -> str``
    Text for the receiver entry in the input list.
``receiver_controller(app) -> object``
    Owns the receiver while it is the input: ``open_source()``,
    ``release_source()`` and ``begin_shutdown()``.
``readiness_checks(config) -> list``
    Extra :class:`babelfishr.readiness.Check` rows.
``preparation_checks(config, result, say) -> None``
    Extra lines for the preparation result.
``cli_commands(subparsers) -> None``
    Extra command-line subcommands.
``window_built(window) -> None``
    Called once the main window exists (menus, labels, signal wiring).
``bubble_menu(bubble, menu) -> None``
    Extra actions for one message's menu.
``audio_status(window, kind, message) -> None``
    Wording for the receiver path's own audio-status events.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, List, Optional, Tuple


@dataclasses.dataclass(frozen=True)
class Product:
    """Who is running: names, data folder and environment prefix."""

    name: str
    """Shown in the window and used for the managed data folder."""

    env_prefix: str
    """Environment variables read are ``<prefix>_HOME``, ``<prefix>_DB``..."""

    config_stem: str
    """Config files looked for: ``<stem>.toml`` / ``<stem>.json``."""

    @property
    def config_names(self) -> Tuple[str, str]:
        return (f"{self.config_stem}.toml", f"{self.config_stem}.json")

    @property
    def database_name(self) -> str:
        return f"{self.config_stem}.sqlite3"

    @property
    def log_name(self) -> str:
        return f"{self.config_stem}.log"

    def env(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"


BABELFISHR = Product(name="BabelFishR", env_prefix="BABELFISHR",
                     config_stem="babelfishr")

_product: Product = BABELFISHR
_extensions: List[Any] = []


def product() -> Product:
    return _product


def set_product(value: Product) -> None:
    """Called by a separate product's entry point, before Config.load."""
    global _product
    _product = value


def register(extension: Any) -> None:
    """Add an extension. Only an explicit caller ever does this."""
    if extension not in _extensions:
        _extensions.append(extension)


def registered() -> Tuple[Any, ...]:
    return tuple(_extensions)


def hooks(name: str) -> List[Callable[..., Any]]:
    """Every registered implementation of hook *name*, in order."""
    found = (getattr(extension, name, None) for extension in _extensions)
    return [hook for hook in found if callable(hook)]


def first(name: str) -> Optional[Callable[..., Any]]:
    found = hooks(name)
    return found[0] if found else None


def reset() -> None:
    """Back to plain BabelFishR (tests)."""
    global _product
    _extensions.clear()
    _product = BABELFISHR


__all__ = ["BABELFISHR", "Product", "first", "hooks", "product", "register",
           "registered", "reset", "set_product"]
