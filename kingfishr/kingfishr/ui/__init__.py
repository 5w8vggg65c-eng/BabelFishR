"""KingFishR's desktop entry point: activate, then run the shared window."""

from __future__ import annotations

from typing import Optional


def run(config_path: Optional[str] = None, argv: Optional[list] = None) -> int:
    from .. import activate

    activate()
    from babelfishr.ui import run as run_window

    return run_window(config_path=config_path, argv=argv)


__all__ = ["run"]
