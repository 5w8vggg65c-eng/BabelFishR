"""KingFishR tests: the package lives in its own distribution directory
(``kingfishr/``), and each test runs as KingFishR - product identity and
hooks registered by :func:`kingfishr.activate`, as its entry point does -
then back to plain BabelFishR, so nothing leaks into the BabelFishR tests.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_DIST = pathlib.Path(__file__).resolve().parents[2] / "kingfishr"
if str(_DIST) not in sys.path:
    sys.path.insert(0, str(_DIST))


@pytest.fixture(autouse=True)
def as_kingfishr():
    from babelfishr import extensions

    import kingfishr

    kingfishr.activate()
    try:
        yield
    finally:
        extensions.reset()
