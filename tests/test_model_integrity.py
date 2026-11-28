"""Whisper model integrity: a pinned revision and sha256 for every file.

Two faults from an independent review, reproduced here through the actual
huggingface_hub client against an in-memory Hub (test_download_real_hub's
InMemoryHub: no network, no real model) that serves two legitimate
revisions of the same repository - A, which the test pin names, and a
newer B ("main") whose files have the same names and lengths but other
bytes:

* a stale local file with the expected length was reused as if valid
  (size-only check);
* a repair could mix files of different revisions (each file resolved
  "main" on its own).

Now every request names the pinned commit, every reused and fetched file is
hashed against the pin, a mismatch after a fetch fails closed, and only a
matching install record makes a model "verified".
"""

from __future__ import annotations

import pathlib
import time

import pytest

pytest.importorskip("httpx2")
huggingface_hub = pytest.importorskip("huggingface_hub")

from test_download_real_hub import (COMMIT, FILES, FILES_B, REPO,  # noqa: E402
                                    hub, verified_state)

MODEL = "large-v3-turbo"

assert hub  # the fixture, imported for use here


def models_root(tmp_path) -> pathlib.Path:
    return tmp_path / "models"


def target_dir(tmp_path) -> pathlib.Path:
    return models_root(tmp_path) / MODEL


def write_files(directory, files):
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)


def prepare(tmp_path, **kwargs):
    from babelfishr.providers import whisper_local

    return whisper_local.prepare_model(MODEL, models_root(tmp_path), **kwargs)


def requested_revisions(server):
    return set(server.revisions_asked)


@pytest.fixture
def no_network(monkeypatch):
    """huggingface_hub's client replaced by one that records and refuses."""
    import httpx2

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


@pytest.fixture
def smoke(monkeypatch):
    """The offline load and smoke test (warm_up), stood in for: no real
    Whisper weights here."""
    import babelfishr.providers.whisper_local as whisper_local

    calls = []

    class Result:
        engine_version = "test"

    monkeypatch.setattr(whisper_local.FasterWhisperEngine, "warm_up",
                        lambda self: calls.append(self.model_size) or Result())
    return calls


def install(config, tmp_path, model=MODEL, **kwargs):
    from babelfishr.modes import OperatingMode
    from babelfishr.preparation import install_asr_model

    config.app_home = str(tmp_path / "home")
    return install_asr_model(config, model, mode=OperatingMode.ONLINE_SETUP, **kwargs)


# ---- the reported faults ------------------------------------------------------

def test_a_stale_same_length_file_is_detected_and_replaced(hub, tmp_path):
    """Revision B's model.bin has exactly the length of A's. It used to be
    kept because its size matched; now its sha256 does not, so it is
    fetched again from the pinned commit."""
    server = hub()
    write_files(target_dir(tmp_path), {"config.json": FILES["config.json"],
                                       "tokenizer.json": FILES["tokenizer.json"],
                                       "model.bin": FILES_B["model.bin"]})
    assert len(FILES_B["model.bin"]) == len(FILES["model.bin"])
    target = pathlib.Path(prepare(tmp_path))
    assert (target / "model.bin").read_bytes() == FILES["model.bin"]
    assert server.served == {"model.bin": len(FILES["model.bin"])}, \
        "only the stale file is fetched; the good ones are reused after hashing"
    assert verified_state(target) == "verified"


def test_a_repair_fetches_only_the_pinned_revision(hub, tmp_path):
    """"main" has moved on to B. A repair of a half-installed A must fetch
    the rest from A - every revision-scoped request names A's commit."""
    server = hub()
    write_files(target_dir(tmp_path), {"config.json": FILES["config.json"]})
    target = pathlib.Path(prepare(tmp_path))
    for name, data in FILES.items():
        assert (target / name).read_bytes() == data, f"{name} is from another revision"
    assert requested_revisions(server) == {COMMIT}, server.revisions_asked
    assert not any("/main" in path or path == f"/api/models/{REPO}"
                   for _m, path, _q in server.requests), server.requests
    assert verified_state(target) == "verified"


def test_the_unknown_total_fallback_also_uses_the_pinned_revision(hub, tmp_path):
    server = hub(fail_model_info=True)
    write_files(target_dir(tmp_path), {"config.json": FILES_B["config.json"]})
    seen = []
    target = pathlib.Path(prepare(tmp_path, progress=seen.append))
    for name, data in FILES.items():
        assert (target / name).read_bytes() == data
    assert requested_revisions(server) == {COMMIT}
    downloading = [p for p in seen if p.phase == "downloading"]
    assert downloading and all(p.bytes_total is None for p in downloading)
    assert verified_state(target) == "verified"


# ---- fail closed ---------------------------------------------------------------

@pytest.mark.parametrize("fail_model_info", [False, True],
                         ids=["known-total", "unknown-total"])
def test_a_tampered_download_fails_closed(hub, tmp_path, config, smoke,
                                          fail_model_info):
    """What arrives for model.bin is not the pinned file (same length, a
    consistent ETag - only the sha256 tells). The file is deleted, the model
    is not installed, nothing says verified, and the smoke test never ran."""
    from babelfishr.model_integrity import read_record
    from babelfishr.providers.whisper_local import ModelState, inspect_model_directory

    hub(fail_model_info=fail_model_info, tamper={"model.bin": FILES_B["model.bin"]})
    seen = []
    step, ok, detail = install(config, tmp_path, progress=seen.append)
    assert not ok and "sha256 mismatch" in detail and "not installed" in detail, detail
    directory = pathlib.Path(config.paths().models) / MODEL
    assert not (directory / "model.bin").exists(), "the bad file was kept"
    assert inspect_model_directory(directory)[0] is not ModelState.COMPLETE
    assert verified_state(directory) != "verified"
    assert read_record(directory) is None
    assert smoke == [] and all(p.phase != "done" for p in seen)


def test_an_unknown_or_unpinned_model_fails_closed_without_network(
        config, tmp_path, smoke, no_network, monkeypatch):
    """No pin (the shipped table has none for this model) - identity cannot
    be established: nothing is requested and nothing is installed. An
    unknown name is refused as before."""
    from babelfishr import model_pins
    from babelfishr.model_integrity import UnpinnedModel
    from babelfishr.providers import whisper_local

    step, ok, detail = install(config, tmp_path, model="large-v9")
    assert not ok and "unknown Whisper model" in detail
    with pytest.raises(UnpinnedModel):
        whisper_local.prepare_model("large-v9", models_root(tmp_path))
    monkeypatch.setitem(model_pins.PINS, "base", {"repo": "Systran/faster-whisper-base",
                                                   "revision": None, "files": {}})
    step, ok, detail = install(config, tmp_path, model="base")
    assert not ok and "integrity cannot be established" in detail, detail
    assert no_network == [] and smoke == []
    assert not (pathlib.Path(config.paths().models) / "base" / "model.bin").exists()


def test_a_pin_without_a_full_commit_or_digest_is_refused():
    from babelfishr.model_integrity import UnpinnedModel, pin_for
    from pinning import pin_entry

    good = pin_entry(REPO, FILES, COMMIT)
    assert pin_for(MODEL, {MODEL: good}).revision == COMMIT
    for broken in ({**good, "revision": "main"}, {**good, "revision": COMMIT[:12]},
                   {**good, "files": {**good["files"],
                                      "model.bin": {"sha256": "", "size": 1}}},
                   {**good, "files": {"config.json": good["files"]["config.json"]}}):
        with pytest.raises(UnpinnedModel):
            pin_for(MODEL, {MODEL: broken})


# ---- offline verification and legacy installs ------------------------------------

def test_offline_verification_of_an_existing_install_makes_no_request(
        hub, tmp_path, monkeypatch):
    from babelfishr.model_integrity import remove_record
    from babelfishr.providers import whisper_local

    hub()
    target = pathlib.Path(prepare(tmp_path))
    remove_record(target)                       # as an install from before records
    import httpx2

    attempts = []

    def refuse(request):
        attempts.append(str(request.url))
        raise httpx2.ConnectError("offline")

    huggingface_hub.set_client_factory(
        lambda: httpx2.Client(transport=httpx2.MockTransport(refuse)))
    assert verified_state(target) == "unverified"
    seen = []
    whisper_local.verify_model(MODEL, models_root(tmp_path), progress=seen.append)
    assert attempts == [], "offline verification touched the network"
    assert verified_state(target) == "verified"
    assert {p.file for p in seen if p.phase == "verifying" and p.file} == set(FILES)
    assert all(p.bytes_done == 0 for p in seen), "hashed bytes counted as transferred"

    # Offline, a mismatch cannot be repaired: it fails and is recorded.
    (target / "tokenizer.json").write_bytes(FILES_B["tokenizer.json"])
    assert verified_state(target) == "changed"     # not merely unverified
    with pytest.raises(whisper_local.IntegrityError):
        whisper_local.verify_model(MODEL, models_root(tmp_path))
    assert verified_state(target) == "mismatch"
    assert (target / "tokenizer.json").exists(), "offline must not delete the only copy"
    assert attempts == []


def test_a_legacy_install_is_unverified_until_hashed(hub, tmp_path, config, smoke,
                                                     monkeypatch):
    """Files of revision A written without a record (a model installed by an
    earlier version): Field Check and the model picker say unverified, never
    verified. Installing it again hashes it - no transfer - and then it is."""
    from babelfishr.modes import OperatingMode
    from babelfishr.readiness import field_check

    server = hub()
    config.app_home = str(tmp_path / "home")
    config.asr.model = MODEL
    directory = pathlib.Path(config.paths().ensure().models) / MODEL
    write_files(directory, FILES)

    def present_check():
        report = field_check(config, run_smoke_tests=False,
                             mode=OperatingMode.FIELD_OFFLINE)
        return report.get("Local ASR model present")

    check = present_check()
    assert check.data["integrity"] == "unverified"
    assert "checksums not verified" in check.detail and "checksums verified" not in check.detail

    step, ok, detail = install(config, tmp_path)
    assert ok, detail
    assert "checksums verified" in detail
    assert server.requests == [], "matching files were downloaded again"
    check = present_check()
    assert check.data["integrity"] == "verified", check.detail
    assert "checksums verified (revision 0123456789ab)" in check.detail

    # A record that says mismatch fails Field Check (it is not usable as is).
    (directory / "config.json").write_bytes(FILES_B["config.json"])
    from babelfishr.providers import whisper_local

    with pytest.raises(whisper_local.IntegrityError):
        whisper_local.verify_model(MODEL, config.paths().models)
    check = present_check()
    assert check.status.value == "fail" and "checksum mismatch" in check.detail


def test_the_model_picker_says_unverified_for_a_legacy_install(hub, tmp_path, config,
                                                               store, qt_app):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.local_engines import WhisperModelPicker

    hub()
    config.app_home = str(tmp_path / "home")
    app = BabelFishRApp(config=config, store=store)
    directory = app.whisper_model_directory(MODEL)
    write_files(directory, FILES)
    box = WhisperModelPicker(app)
    text = box.itemText(box.findData(MODEL))
    assert "Installed, unverified" in text, text
    prepare_dir = directory.parent
    from babelfishr.providers import whisper_local

    whisper_local.verify_model(MODEL, prepare_dir)
    box.refresh_choices()
    text = box.itemText(box.findData(MODEL))
    assert "Installed, verified" in text, text


# ---- interruption ----------------------------------------------------------------

def test_an_interrupted_download_is_not_installed_and_a_retry_ends_verified(
        hub, tmp_path, monkeypatch):
    """Cancelled mid-weights: the partial transfer never sits at model.bin
    (huggingface_hub keeps it in an .incomplete scratch file, which is not
    counted as installed - a crash leftover is planted to prove it). The
    retry reuses the finished files after hashing them, fetches only the
    weights from the pinned commit, and ends verified."""
    import babelfishr.download_progress as download_progress
    from babelfishr.download_progress import DownloadCancelled
    from babelfishr.model_integrity import read_record
    from babelfishr.providers.whisper_local import ModelState, inspect_model_directory

    monkeypatch.setattr(download_progress, "DEFAULT_INTERVAL", 0.0)
    large = dict(FILES, **{"model.bin": bytes(range(256)) * (30 * 1024 * 4)})  # 30 MB
    hub(chunk=1024 * 1024, files=large)
    received = []

    def progress(p):
        if p.file == "model.bin" and p.phase == "downloading" and p.bytes_done > 5120:
            received.append(p.bytes_done)

    with pytest.raises(DownloadCancelled):
        prepare(tmp_path, progress=progress, cancelled=lambda: bool(received))
    target = target_dir(tmp_path)
    assert received and received[-1] < sum(len(v) for v in large.values())
    assert not (target / "model.bin").exists()
    scratch = target / ".cache" / "huggingface" / "download"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "deadbeef.0123.incomplete").write_bytes(large["model.bin"][:4096])
    assert inspect_model_directory(target)[0] is ModelState.INCOMPLETE
    assert read_record(target) is None and verified_state(target) != "verified"

    server = hub(chunk=1024 * 1024, files=large)
    seen = []
    prepare(tmp_path, progress=seen.append)
    assert server.served == {"model.bin": len(large["model.bin"])}
    assert requested_revisions(server) == {COMMIT}
    assert (target / "model.bin").read_bytes() == large["model.bin"]
    assert verified_state(target) == "verified"
    downloading = [p for p in seen if p.phase == "downloading"]
    assert downloading[-1].bytes_done == len(large["model.bin"]), \
        "progress is not the bytes actually transferred"
    assert seen[-1].phase == "verifying" and seen[-1].files_done == 3


def test_a_truncated_file_at_the_final_path_is_fetched_again(hub, tmp_path):
    server = hub()
    write_files(target_dir(tmp_path), dict(FILES, **{"model.bin": FILES["model.bin"][:1000]}))
    target = pathlib.Path(prepare(tmp_path))
    assert (target / "model.bin").read_bytes() == FILES["model.bin"]
    assert server.served == {"model.bin": len(FILES["model.bin"])}
    assert verified_state(target) == "verified"


def test_verification_runs_on_the_worker_thread_not_the_gui_thread(
        hub, tmp_path, config, smoke, monkeypatch, qt_app):
    """Hashing happens inside the install job's worker; the GUI thread only
    receives progress snapshots (including the "verifying" ones)."""
    import threading

    from babelfishr import model_integrity
    from babelfishr.modes import OperatingMode
    from babelfishr.ui.workers import install_model_job, run_in_background
    from PySide6 import QtWidgets

    hub()
    hashed_on = []
    real = model_integrity.sha256_file

    def recording(path, *args, **kwargs):
        hashed_on.append(threading.current_thread() is threading.main_thread())
        return real(path, *args, **kwargs)

    monkeypatch.setattr(model_integrity, "sha256_file", recording)
    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    received, finished = [], []
    run_in_background(install_model_job, config, MODEL, on_progress=received.append,
                      on_finished=finished.append,
                      on_failed=lambda m: finished.append(("failed", m)))
    deadline = time.monotonic() + 30
    while not finished and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    for _ in range(20):
        QtWidgets.QApplication.processEvents()
    assert finished and finished[0][1] is True, finished
    assert hashed_on and not any(hashed_on), "hashed on the GUI thread"
    assert any(p.phase == "verifying" and p.file for p in received)


@pytest.fixture(scope="module")
def qt_app():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_install_menu_offers_to_verify_a_legacy_install(hub, tmp_path, config, store,
                                                            monkeypatch, qt_app, smoke):
    """A complete but unverified model is offered as "Verify"; after the job
    (hashing only - no transfer) it leaves the menu."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.modes import OperatingMode
    from babelfishr.ui.main_window import MainWindow
    from test_offline_indicator import Jobs
    from PySide6 import QtWidgets

    server = hub()
    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    Jobs(monkeypatch)
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        write_files(window.app.whisper_model_directory(MODEL), FILES)
        labels = [label for _n, label, _s in window._installable_models()]
        assert any(label.startswith("Verify Large v3 turbo") for label in labels), labels
        monkeypatch.setattr(window, "_choose", lambda *a, **k: "Download")
        window._install_model(MODEL)
        deadline = time.monotonic() + 30
        while window._install_worker is not None and time.monotonic() < deadline:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.005)
        assert "verified" in window.install_panel.status.text()
        assert server.requests == []
        labels = [label for _n, label, _s in window._installable_models()]
        assert not any("Large v3 turbo" in label for label in labels), labels
    finally:
        window.close()


def test_a_record_made_under_the_old_turbo_repository_id_still_counts(tmp_path, monkeypatch):
    """The Hub redirects mobiuslabsgmbh/faster-whisper-large-v3-turbo to
    dropbox-dash/...: same commits, same files. An install recorded under the
    old id stays verified; a record from an unrelated repository does not."""
    import json

    from babelfishr import model_integrity, model_pins
    from pinning import pin_entry

    entry = dict(pin_entry("dropbox-dash/faster-whisper-large-v3-turbo", FILES, COMMIT),
                 repo_aliases=[REPO])
    monkeypatch.setitem(model_pins.PINS, MODEL, entry)
    directory = target_dir(tmp_path)
    write_files(directory, FILES)
    model_integrity.verify_directory(directory, model_integrity.pin_for(MODEL))
    model_integrity.write_record(directory, model_integrity.pin_for(MODEL),
                                 model_integrity.Integrity.VERIFIED)
    record = model_integrity.read_record(directory)
    for repo, expected in ((REPO, "verified"), ("someone/else", "unverified")):
        record["repo"] = repo
        model_integrity.record_path(directory).write_text(json.dumps(record))
        assert verified_state(directory) == expected, repo


def test_the_shipped_pins_name_full_commits_and_fail_closed_without_digests():
    """The shipped table: the commits as supplied (full 40-hex, the Turbo
    pin on the repository the Hub serves). Without per-file digests no model
    can be verified - refused, never guessed."""
    from babelfishr import model_pins
    from babelfishr.model_integrity import UnpinnedModel, pin_for
    from babelfishr.providers.whisper_local import MODEL_SIZES

    assert set(model_pins.PINS) == set(MODEL_SIZES)
    turbo = model_pins.PINS["large-v3-turbo"]
    assert turbo["repo"] == "dropbox-dash/faster-whisper-large-v3-turbo"
    assert REPO in turbo["repo_aliases"]
    for name, entry in model_pins.PINS.items():
        assert len(entry["revision"]) == 40 and int(entry["revision"], 16) >= 0
        if not entry["files"]:
            with pytest.raises(UnpinnedModel, match="no per-file checksums"):
                pin_for(name)
