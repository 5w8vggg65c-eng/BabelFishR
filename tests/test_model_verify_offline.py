"""Verify checksums offline, and known-bad weights never reach ASR.

An independent inspection found two faults, reproduced here through the
operator-facing routes:

* Field Check passed a model that had been verified and then changed on
  disk as merely "unverified" - the cheap state could not tell a legacy
  install (never verified) from one whose files are no longer the bytes
  that were hashed.
* Monitoring still loaded a model whose record said its hash had failed.

And the only way to (re)verify went through the online install: refused in
Offline Field Use and Record Only, behind a download confirmation and a
full-download free-space check, registered as a network transfer.

Fixture weights are pinned through tests/pinning.py (the same table the
application reads), so these are genuine installed files whose bytes match
the pins - no real model, no network.
"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import threading
import time
import types

import pytest

import pinning
from babelfishr.modes import OperatingMode

MODEL = "small"
FILES = {"config.json": b'{"architecture": "fixture"}\n',
         "model.bin": bytes(range(256)) * 512,
         "tokenizer.json": b'{"tokens": ["a", "b"]}\n',
         "vocabulary.txt": b"a\nb\n"}
RESTRICTED = [OperatingMode.FIELD_OFFLINE, OperatingMode.RECORD_ONLY]


# ---- helpers ------------------------------------------------------------------
@pytest.fixture
def pinned(monkeypatch):
    return pinning.pin(monkeypatch, MODEL, FILES)


@pytest.fixture
def no_sockets(monkeypatch):
    """Every outbound internet connection, recorded and refused."""
    attempts = []
    real_connect = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(address)
            raise OSError("network disabled for this test")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection",
                        lambda address, *a, **k: attempts.append(address)
                        or (_ for _ in ()).throw(OSError("network disabled")))
    return attempts


@pytest.fixture
def hub_refuses(monkeypatch):
    """huggingface_hub's own HTTP client, recording and refusing."""
    httpx2 = pytest.importorskip("httpx2")
    huggingface_hub = pytest.importorskip("huggingface_hub")
    from huggingface_hub.utils import _http

    attempts = []

    def refuse(request):
        attempts.append(str(request.url))
        raise httpx2.ConnectError("network disabled for this test")

    previous = _http._GLOBAL_CLIENT_FACTORY
    huggingface_hub.set_client_factory(
        lambda: httpx2.Client(transport=httpx2.MockTransport(refuse)))
    yield attempts
    huggingface_hub.set_client_factory(previous)


def model_dir(config) -> pathlib.Path:
    from babelfishr.providers.whisper_local import model_directory_for

    return model_directory_for(config.paths().models, MODEL)


def install_files(directory: pathlib.Path, files=FILES) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    return directory


def state(directory) -> str:
    from babelfishr.model_integrity import integrity_state

    return integrity_state(directory, MODEL)[0].value


def present_check(config, mode=OperatingMode.FIELD_OFFLINE):
    from babelfishr.readiness import field_check

    config.asr.model = MODEL
    return field_check(config, run_smoke_tests=False, mode=mode).get(
        "Local ASR model present")


def change_bytes_keep_size(path: pathlib.Path) -> None:
    data = bytearray(path.read_bytes())
    data[0] ^= 0xFF
    stat = path.stat()
    path.write_bytes(bytes(data))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


class FakeWhisper:
    """faster_whisper stand-in: records every model construction."""

    def __init__(self, monkeypatch):
        self.built = []
        recorder = self

        class WhisperModel:
            def __init__(self, path, **kwargs):
                recorder.built.append(path)

        module = types.ModuleType("faster_whisper")
        module.WhisperModel = WhisperModel
        monkeypatch.setitem(sys.modules, "faster_whisper", module)


def engine_for(config):
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    return FasterWhisperEngine(model=MODEL, models_root=str(config.paths().models),
                               local_files_only=True)


@pytest.fixture
def home(tmp_path, monkeypatch, config):
    """A fresh BABELFISHR_HOME: no record, nothing verified before."""
    root = tmp_path / "fresh-home"
    monkeypatch.setenv("BABELFISHR_HOME", str(root))
    config.app_home = str(root)
    config.asr.model = MODEL
    return root


# ---- the cheap state ------------------------------------------------------------
def test_changed_is_distinct_from_legacy_and_both_from_mismatch(pinned, home, config):
    from babelfishr.providers import whisper_local

    directory = install_files(model_dir(config))
    assert state(directory) == "unverified", "a legacy install (no record)"
    whisper_local.verify_model(MODEL, config.paths().models)
    assert state(directory) == "verified"

    # Same bytes, same size, only the time moved: changed (not verified,
    # not legacy) until hashed again.
    stat = (directory / "config.json").stat()
    os.utime(directory / "config.json", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert state(directory) == "changed"
    whisper_local.verify_model(MODEL, config.paths().models)
    assert state(directory) == "verified"

    # Replaced by an identical copy (new inode, size and time preserved).
    path = directory / "tokenizer.json"
    stat = path.stat()
    copy = directory / "tokenizer.json.copy"
    copy.write_bytes(path.read_bytes())
    os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(copy, path)
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert state(directory) == "changed"
    whisper_local.verify_model(MODEL, config.paths().models)

    # A pinned file that disappeared, and an extra loadable asset.
    (directory / "vocabulary.txt").unlink()
    assert state(directory) == "changed"
    install_files(directory)
    whisper_local.verify_model(MODEL, config.paths().models)
    (directory / "vocabulary.json").write_bytes(b'{"x": 1}')
    assert state(directory) == "changed"
    with pytest.raises(whisper_local.IntegrityError, match="vocabulary.json unexpected"):
        whisper_local.verify_model(MODEL, config.paths().models)
    assert state(directory) == "mismatch"
    assert (directory / "vocabulary.json").exists(), "offline verification never deletes"


def test_a_record_without_inodes_still_counts(pinned, home, config):
    """Records written before inodes were recorded stay valid."""
    import json

    from babelfishr.model_integrity import record_path
    from babelfishr.providers import whisper_local

    directory = install_files(model_dir(config))
    whisper_local.verify_model(MODEL, config.paths().models)
    record = json.loads(record_path(directory).read_text())
    for entry in record["files"].values():
        entry.pop("ino", None)
    record_path(directory).write_text(json.dumps(record))
    assert state(directory) == "verified"


def test_loadable_patterns_match_the_downloaded_assets():
    from babelfishr.model_integrity import LOADABLE_PATTERNS
    from babelfishr.providers.whisper_local import ALLOW_PATTERNS

    assert tuple(LOADABLE_PATTERNS) == tuple(ALLOW_PATTERNS)


# ---- Field Check ------------------------------------------------------------------
def test_field_check_fails_a_model_changed_since_it_was_verified(pinned, home, config,
                                                                no_sockets):
    """The inspector's finding: this used to PASS as merely "unverified"."""
    from babelfishr.providers import whisper_local

    directory = install_files(model_dir(config))
    check = present_check(config)
    assert check.status.value == "pass" and check.data["integrity"] == "unverified"
    assert "checksums not verified" in check.detail, "legacy: used, and said so"

    whisper_local.verify_model(MODEL, config.paths().models)
    assert present_check(config).data["integrity"] == "verified"

    change_bytes_keep_size(directory / "model.bin")
    check = present_check(config)
    assert check.status.value == "fail", check.detail
    assert check.data["integrity"] == "changed"
    assert "changed since it was verified - verify checksums" in check.detail
    assert "--verify" in check.remedy
    for mode in RESTRICTED:
        assert present_check(config, mode).status.value == "fail"

    # Hashing it again finds the bytes are wrong: mismatch, still failing.
    with pytest.raises(whisper_local.IntegrityError):
        whisper_local.verify_model(MODEL, config.paths().models)
    check = present_check(config)
    assert check.status.value == "fail" and check.data["integrity"] == "mismatch"
    assert "does not match its published checksums" in check.detail

    # Restored and reverified locally: passes again.
    install_files(directory)
    assert present_check(config).status.value == "fail", "mismatch until reverified"
    whisper_local.verify_model(MODEL, config.paths().models)
    check = present_check(config)
    assert check.status.value == "pass" and check.data["integrity"] == "verified"
    assert no_sockets == []


# ---- the engine load path -----------------------------------------------------------
def test_a_mismatched_model_is_refused_on_every_load_path(pinned, home, config,
                                                          monkeypatch):
    from babelfishr.providers import EngineUnavailable
    from babelfishr.providers import whisper_local

    fake = FakeWhisper(monkeypatch)
    directory = install_files(model_dir(config))
    (directory / "model.bin").write_bytes(b"\0" * len(FILES["model.bin"]))
    with pytest.raises(whisper_local.IntegrityError):
        whisper_local.verify_model(MODEL, config.paths().models)
    assert state(directory) == "mismatch"
    import numpy as np

    engine = engine_for(config)
    assert engine.available(), "kept selected, so the failure is reported, not swapped"
    for load in (engine.preload, engine.warm_up,
                 lambda: engine.transcribe(np.zeros(16000, dtype=np.float32), 16000)):
        with pytest.raises(EngineUnavailable, match="does not match its published checksums"):
            load()
    assert fake.built == [], "known-bad weights reached WhisperModel"


def test_field_check_smoke_tests_never_load_bad_weights(pinned, home, config, monkeypatch):
    from babelfishr.readiness import field_check
    from babelfishr.providers import whisper_local

    fake = FakeWhisper(monkeypatch)
    directory = install_files(model_dir(config))
    whisper_local.verify_model(MODEL, config.paths().models)
    change_bytes_keep_size(directory / "config.json")
    report = field_check(config, run_smoke_tests=True, mode=OperatingMode.FIELD_OFFLINE)
    assert report.get("Local ASR model present").status.value == "fail"
    assert report.get("Local ASR model loadable").status.value == "skip"
    assert fake.built == []


def test_a_changed_model_is_rehashed_before_loading(pinned, home, config, monkeypatch):
    """Changed but genuine (only its time moved): hashed on load, allowed,
    recorded verified. Changed and wrong: hashed, refused, recorded mismatch."""
    from babelfishr.providers import EngineUnavailable
    from babelfishr.providers import whisper_local

    fake = FakeWhisper(monkeypatch)
    directory = install_files(model_dir(config))
    whisper_local.verify_model(MODEL, config.paths().models)
    engine = engine_for(config)
    engine.preload()                                   # verified real files: usable
    assert fake.built == [str(directory)]

    stat = (directory / "model.bin").stat()
    os.utime(directory / "model.bin", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert state(directory) == "changed"
    engine = engine_for(config)
    engine.preload()
    assert len(fake.built) == 2 and state(directory) == "verified"

    change_bytes_keep_size(directory / "model.bin")
    engine = engine_for(config)
    with pytest.raises(EngineUnavailable, match="changed since it was verified"):
        engine.preload()
    assert len(fake.built) == 2 and state(directory) == "mismatch"


def test_monitoring_with_a_mismatched_model_still_archives_every_recording(
        pinned, home, config, store, monkeypatch, fixture_wav):
    """Capture is independent of ASR: each message is recorded to disk and
    the store, and marked as a failed transcription (retryable) - not lost."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.models import ProcessingState
    from babelfishr.providers.mock import MockTranslationEngine
    from babelfishr.providers import whisper_local

    fake = FakeWhisper(monkeypatch)
    directory = install_files(model_dir(config))
    whisper_local.verify_model(MODEL, config.paths().models)
    change_bytes_keep_size(directory / "model.bin")
    with pytest.raises(whisper_local.IntegrityError):
        whisper_local.verify_model(MODEL, config.paths().models)

    app = BabelFishRApp(config=config, store=store)
    app.transcription = engine_for(config)
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=fixture_wav, name="mismatched model")
    app.run_replay()
    transmissions = app.transmissions()
    app.stop_session()
    assert transmissions
    for tx in transmissions:
        assert tx.audio_path and pathlib.Path(tx.audio_path).exists()
        assert tx.state is ProcessingState.FAILED
        assert tx.error.stage == "transcription"
        assert "does not match its published checksums" in tx.error.message
    assert fake.built == []


# ---- cold verification from a fresh profile -------------------------------------------
def test_a_fresh_profile_verifies_genuine_files_offline_with_no_request(
        pinned, home, config, no_sockets, hub_refuses, monkeypatch):
    from babelfishr.preparation import verify_asr_model
    from babelfishr.transfers import registry

    directory = install_files(model_dir(config))
    assert state(directory) == "unverified"
    from babelfishr.modes import AppPaths

    def no_space_check(self):
        raise AssertionError("verification checked free space for a download")

    monkeypatch.setattr(AppPaths, "free_bytes", no_space_check)
    seen = []
    step, ok, detail = verify_asr_model(config, MODEL, progress=seen.append)
    assert ok, detail
    assert state(directory) == "verified"
    assert no_sockets == [] and hub_refuses == []
    assert not registry().active()
    files = [(p.file, p.files_done, p.files_total) for p in seen if p.file]
    assert {f for f, _d, _t in files} == set(FILES)
    assert all(t == len(FILES) for _f, _d, t in files)
    assert all(p.bytes_done == 0 for p in seen), "hashed bytes are not transfers"


def test_verification_is_cancellable(pinned, home, config):
    from babelfishr.download_progress import DownloadCancelled
    from babelfishr.preparation import verify_asr_model

    directory = install_files(model_dir(config))
    with pytest.raises(DownloadCancelled):
        verify_asr_model(config, MODEL, cancelled=lambda: True)
    assert state(directory) == "unverified", "a cancelled check records nothing"


# ---- the CLI route -----------------------------------------------------------------------
@pytest.mark.parametrize("mode", RESTRICTED)
def test_cli_field_check_verify_works_offline(pinned, tmp_path, monkeypatch, capsys,
                                              mode, no_sockets):
    from test_network_policy_transfers import NetworkSpies, set_saved_mode

    from babelfishr import cli
    from babelfishr.config import Config

    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "profile"))
    monkeypatch.chdir(tmp_path)
    spies = NetworkSpies(monkeypatch)
    set_saved_mode(mode)
    config = Config.load(None)
    config.asr.model = MODEL
    directory = install_files(model_dir(config))
    from babelfishr.providers import whisper_local

    whisper_local.verify_model(MODEL, config.paths().models)
    change_bytes_keep_size(directory / "tokenizer.json")
    cli.main(["field-check", "--no-smoke-tests", "--mode", mode.value])
    out = capsys.readouterr().out
    assert "changed since it was verified - verify checksums" in out, out

    install_files(directory)                      # restored
    cli.main(["field-check", "--no-smoke-tests", "--mode", mode.value,
              "--verify", MODEL])
    out = capsys.readouterr().out
    assert "Verify checksums: OK" in out, out
    assert state(directory) == "verified"
    assert "changed since it was verified" not in out
    assert spies.calls == [] and no_sockets == []


# ---- the desktop route --------------------------------------------------------------------
@pytest.fixture(scope="module")
def qt_app():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _wait(qt_app, condition, seconds=30):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    for _ in range(10):
        qt_app.processEvents()
    return condition()


@pytest.mark.parametrize("mode", RESTRICTED + [OperatingMode.ONLINE_SETUP])
def test_verify_checksums_in_the_window_is_local_in_every_mode(
        pinned, home, config, store, monkeypatch, qt_app, mode, no_sockets, hub_refuses):
    from test_network_policy_transfers import NetworkSpies
    from test_offline_indicator import Jobs

    from babelfishr import model_integrity, preparation
    from babelfishr.app import BabelFishRApp
    from babelfishr.providers import whisper_local
    from babelfishr.transfers import registry
    from babelfishr.ui.main_window import MainWindow
    from PySide6 import QtWidgets

    spies = NetworkSpies(monkeypatch)
    Jobs(monkeypatch)                        # readiness refreshes are held, not run
    directory = install_files(model_dir(config))
    whisper_local.verify_model(MODEL, config.paths().models)
    change_bytes_keep_size(directory / "model.bin")
    install_files(directory)                 # same bytes again, new time: changed
    assert state(directory) == "changed"

    dialogs, space, hashing = [], [], []
    for name in ("question", "warning", "information", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            lambda *a, _n=name, **k: dialogs.append(_n))
    monkeypatch.setattr(preparation, "guard_download",
                        lambda *a, **k: dialogs.append("guard"))
    real_free = preparation.AppPaths.free_bytes
    monkeypatch.setattr(preparation.AppPaths, "free_bytes",
                        lambda self: space.append(1) or real_free(self))
    real_hash = model_integrity.sha256_file

    def recording(path, *a, **k):
        hashing.append((threading.current_thread() is threading.main_thread(),
                        [t.description for t in registry().active()]))
        return real_hash(path, *a, **k)

    monkeypatch.setattr(model_integrity, "sha256_file", recording)
    config.mode = mode.value
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        monkeypatch.setattr(window, "_choose",
                            lambda *a, **k: dialogs.append("choose") or "Download")
        verifier = window.checksum_verifier
        if mode is not OperatingMode.ONLINE_SETUP:
            assert not window.install_model_button.isEnabled(), "installing stays refused"
        assert verifier.button.isEnabled(), verifier.button.toolTip()
        box = window.whisper_model_box
        assert "changed since verified" in box.itemText(box.findData(MODEL))
        verifier.fill_menu()
        actions = [a for a in verifier.menu.actions() if MODEL.capitalize() in a.text()]
        assert actions and "changed since verified" in actions[0].text()
        actions[0].trigger()
        assert verifier.worker is not None and not verifier.button.isEnabled()
        assert verifier.panel.isVisibleTo(window)
        assert _wait(qt_app, lambda: verifier.worker is None)
        assert "matches its published checksums" in verifier.panel.status.text()
        assert state(directory) == "verified"
        assert "verified" in box.itemText(box.findData(MODEL))
        assert hashing and not any(on_gui for on_gui, _t in hashing), "hashed on the GUI thread"
        assert all(active == [] for _g, active in hashing), "registered as a transfer"
        assert dialogs == [] and space == []
        assert spies.calls == [] and no_sockets == [] and hub_refuses == []
        assert verifier.button.isEnabled()

        # A genuine mismatch is found and shown, locally.
        change_bytes_keep_size(directory / "config.json")
        assert verifier.start(MODEL)
        assert _wait(qt_app, lambda: verifier.worker is None)
        assert state(directory) == "mismatch"
        assert "config.json mismatch" in verifier.panel.status.text()
        assert "checksum mismatch" in box.itemText(box.findData(MODEL))
        assert dialogs == [] and spies.calls == [] and no_sockets == []
    finally:
        window.close()


def test_verify_checksums_can_be_cancelled_in_the_window(
        pinned, home, config, store, monkeypatch, qt_app):
    from test_offline_indicator import Jobs

    from babelfishr import model_integrity
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    Jobs(monkeypatch)
    directory = install_files(model_dir(config))
    started, release = threading.Event(), threading.Event()
    real_hash = model_integrity.sha256_file

    def slow(path, cancelled=None, *a, **k):
        started.set()
        release.wait(10)
        return real_hash(path, cancelled, *a, **k)

    monkeypatch.setattr(model_integrity, "sha256_file", slow)
    config.mode = OperatingMode.RECORD_ONLY.value
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        verifier = window.checksum_verifier
        assert verifier.start(MODEL)
        assert _wait(qt_app, started.is_set)
        assert verifier.panel.cancel_button.isEnabled()
        verifier.panel.cancel_button.click()
        release.set()
        assert _wait(qt_app, lambda: verifier.worker is None)
        assert "cancelled" in verifier.panel.status.text()
        assert state(directory) == "unverified"
    finally:
        window.close()


def test_the_install_menus_verify_entry_is_the_local_check(
        pinned, home, config, store, monkeypatch, qt_app, no_sockets):
    """Online, a changed (or legacy) model is offered as "Verify" in the
    install menu too; it runs the local check - no download dialog."""
    from test_offline_indicator import Jobs

    from babelfishr.app import BabelFishRApp
    from babelfishr.providers import whisper_local
    from babelfishr.ui.main_window import MainWindow

    Jobs(monkeypatch)
    directory = install_files(model_dir(config))
    whisper_local.verify_model(MODEL, config.paths().models)
    stat = (directory / "config.json").stat()
    os.utime(directory / "config.json", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    config.mode = OperatingMode.ONLINE_SETUP.value
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        asked = []
        monkeypatch.setattr(window, "_choose", lambda *a, **k: asked.append(a) or "")
        window._fill_install_model_menu()
        verify = [a for a in window.install_model_menu.actions()
                  if a.text().startswith("Verify Small")]
        assert verify, [a.text() for a in window.install_model_menu.actions()]
        verify[0].trigger()
        assert window.checksum_verifier.worker is not None
        assert not window.install_model_button.isEnabled(), "one job on the files at a time"
        assert _wait(qt_app, lambda: window.checksum_verifier.worker is None)
        assert asked == [] and no_sockets == []
        assert state(directory) == "verified"
        assert window.install_model_button.isEnabled()
    finally:
        window.close()
