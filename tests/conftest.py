"""Shared fixtures. Every test runs on mock engines: no models, keys or network."""

from __future__ import annotations

import faulthandler
import os
import pathlib

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.storage import Store
from babelfishr.testing import (build_fixture, gapped_transmission_fixture,
                                standard_fixture)

SAMPLE_RATE = 48_000


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
    return len([t for t in fixture.transmissions
                if t.kind == "voice" and t.duration > 0.1])


@pytest.fixture
def gapped_wav(tmp_path: pathlib.Path) -> str:
    """One transmission with internal pauses - must not be split."""
    return gapped_transmission_fixture(SAMPLE_RATE).write(str(tmp_path / "gapped.wav"))


# ---- hang diagnostics (CI) -------------------------------------------------
# pytest-timeout's thread method needs the GIL to report a hung test, so a
# native call that holds the GIL hangs silently until the CI job is killed,
# and nothing names the test. faulthandler's watchdog is a C thread: it dumps
# every thread's Python stack without the GIL, then exits the run, so the
# build step fails (rather than the job timing out) and the always() report
# upload keeps the evidence. Off unless BABELFISHR_HANG_DUMP_SECONDS is set;
# the macOS build sets it above pytest-timeout's limit, so ordinary timeouts
# are still reported by pytest-timeout.
_HANG_SECONDS = float(os.environ.get("BABELFISHR_HANG_DUMP_SECONDS") or 0)
_hang_log = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _hang_log
    if _HANG_SECONDS <= 0:
        yield
        return
    if _hang_log is None:
        _hang_log = open(os.environ.get("BABELFISHR_HANG_DUMP_FILE") or "hang-dump.txt",
                         "a", buffering=1, encoding="utf-8")
    _hang_log.write(f"started {item.nodeid}\n")
    _hang_log.flush()
    faulthandler.dump_traceback_later(_HANG_SECONDS, exit=True, file=_hang_log)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
