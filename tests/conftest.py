"""Shared fixtures. Every test runs on mock engines: no models, keys or network."""

from __future__ import annotations

import pathlib

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.storage import Store
from babelfishr.testing import (build_fixture, gapped_transmission_fixture,
                                standard_fixture)

SAMPLE_RATE = 48_000

# KingFishR's tests live in tests/kingfishr/ and its package in its own
# distribution directory (kingfishr/). Set up here, not in a second
# conftest.py: shared tests import helpers with "from conftest import ...",
# and a second module named conftest would shadow this one.
_TESTS = pathlib.Path(__file__).resolve().parent
_KINGFISHR_TESTS = _TESTS / "kingfishr"
_KINGFISHR_DIST = _TESTS.parent / "kingfishr"


def _in_kingfishr_tests(path) -> bool:
    try:
        pathlib.Path(str(path)).resolve().relative_to(_KINGFISHR_TESTS)
        return True
    except ValueError:
        return False


def pytest_collect_file(file_path, parent):
    # Make the kingfishr package importable before its tests are imported.
    import sys

    if _in_kingfishr_tests(file_path) and str(_KINGFISHR_DIST) not in sys.path:
        sys.path.insert(0, str(_KINGFISHR_DIST))
    return None


@pytest.fixture(autouse=True)
def _product_for_this_test(request, tmp_path_factory, monkeypatch):
    """A KingFishR test runs as KingFishR - identity and hooks from
    kingfishr.activate(), as its entry point does - with KINGFISHR_HOME on
    a temporary folder (as the build sets BABELFISHR_HOME), then the process
    is plain BabelFishR again. Every other test runs as plain BabelFishR."""
    from babelfishr import extensions

    if not _in_kingfishr_tests(request.node.path):
        extensions.reset()
        yield
        return
    import kingfishr

    monkeypatch.setenv("KINGFISHR_HOME",
                       str(tmp_path_factory.mktemp("KingFishR-test-home")))
    kingfishr.activate()
    try:
        yield
    finally:
        extensions.reset()


@pytest.fixture
def workdir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


@pytest.fixture
def config(tmp_path: pathlib.Path) -> Config:
    cfg = Config()
    cfg.database = str(tmp_path / "test.sqlite3")
    cfg.recording.directory = str(tmp_path / "recordings")
    cfg.asr.engine = "mock"
    cfg.translate.engine = "mock"
    cfg.translate.target_language = "en"
    return cfg


@pytest.fixture
def store(config: Config) -> Store:
    store = Store(config.database, recordings_dir=config.recording.directory)
    yield store
    store.close()


@pytest.fixture
def app(config: Config, store: Store) -> BabelFishRApp:
    application = BabelFishRApp(config=config, store=store)
    yield application
    application.stop_session()


@pytest.fixture
def fixture_wav(tmp_path: pathlib.Path) -> str:
    """A WAV containing five separated voice transmissions."""
    return standard_fixture(SAMPLE_RATE).write(str(tmp_path / "fixture.wav"))


@pytest.fixture
def expected_transmissions() -> int:
    fixture = standard_fixture(SAMPLE_RATE)
    return len(recorded_events(fixture))


def recorded_events(fixture):
    """The fixture's events a capture keeps: every voice longer than the
    crackle, and the static burst (recorded and classified as noise)."""
    return [t for t in fixture.transmissions
            if (t.kind == "voice" and t.duration > 0.1) or t.kind == "static"]


@pytest.fixture
def gapped_wav(tmp_path: pathlib.Path) -> str:
    """One transmission with internal pauses - must not be split."""
    return gapped_transmission_fixture(SAMPLE_RATE).write(str(tmp_path / "gapped.wav"))
