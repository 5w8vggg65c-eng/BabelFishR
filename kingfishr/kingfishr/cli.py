"""KingFishR's command line: activate, then the shared commands plus
``analyze``. Its own settings and data, never BabelFishR's."""

from __future__ import annotations

from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    from . import activate

    activate()
    from babelfishr.cli import main as shared_main

    return shared_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
