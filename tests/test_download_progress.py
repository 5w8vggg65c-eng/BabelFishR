"""Real byte progress while a Whisper model downloads.

faster-whisper's download_model hides every byte (it hands huggingface_hub
a disabled progress bar), so a multi-gigabyte download looked like a hang.
prepare_model now fetches the same files itself and forwards what
huggingface_hub reports: resolving, downloading (bytes and files, with a
total only when the Hub's metadata gave it), verifying, done.

The Hub is stood in for by a deterministic fake that delivers each file in
chunks, slowly, through the tqdm_class it is given - the way huggingface_hub
2.x does: into a temporary file that is moved into place only when complete.
No network.
"""

from __future__ import annotations

import pathlib
import time
import types
import uuid

import pytest

import babelfishr.download_progress as download_progress
from babelfishr.download_progress import (DownloadCancelled, DownloadProgress,
                                          ProgressTracker)
from babelfishr.modes import OperatingMode
from babelfishr.providers import whisper_local
from babelfishr.providers.whisper_local import (ModelState,
                                                inspect_model_directory,
                                                model_directory_for)

MODEL = "tiny"
REPO = "Systran/faster-whisper-tiny"
CHUNK = 1000

#: A small model: weights, config and tokenizer, plus a file faster-whisper
#: does not fetch (the allow patterns must leave it out).
FILES = {
    "config.json": b"c" * 2500,
    "tokenizer.json": b"t" * 4200,
    "model.bin": b"m" * 9100,
    "README.md": b"r" * 50,
}
WANTED = ("config.json", "tokenizer.json", "model.bin")


class FakeHub:
    """huggingface_hub, stood in for."""

    def __init__(self, files=FILES, metadata=True, delay=0.0005):
        self.files = dict(files)
        self.metadata = metadata
        self.delay = delay
        self.fetched = []          # (filename, bytes actually sent)
        self.chunks = []           # every chunk size delivered
        self.calls = []
        self.on_chunk = None       # hook(filename, bytes so far)

    # -- metadata -----------------------------------------------------------
    def api(self):
        hub = self

        class Api:
            def model_info(self, repo_id, files_metadata=False):
                hub.calls.append(("model_info", repo_id, files_metadata))
                if not hub.metadata:
                    raise OSError("metadata unavailable")
                return types.SimpleNamespace(siblings=[
                    types.SimpleNamespace(rfilename=name, size=len(data),
                                          lfs=None)
                    for name, data in hub.files.items()])
        return Api

    # -- the transfer -------------------------------------------------------
    def _send(self, name, local_dir, bar, extra_bars=()):
        destination = pathlib.Path(local_dir) / name
        if destination.exists():
            return str(destination)
        data = self.files[name]
        scratch = pathlib.Path(local_dir) / ".cache" / "huggingface" / "download"
        scratch.mkdir(parents=True, exist_ok=True)
        tmp = scratch / f"{name}.{uuid.uuid4().hex[:8]}.incomplete"
        sent = 0
        try:
            with tmp.open("wb") as handle:
                for start in range(0, len(data), CHUNK):
                    chunk = data[start:start + CHUNK]
                    time.sleep(self.delay)
                    handle.write(chunk)
                    sent += len(chunk)
                    self.chunks.append(len(chunk))
                    bar.update(len(chunk))
                    if hasattr(bar, "update_transfer"):
                        bar.update_transfer(len(chunk))
                    for other in extra_bars:
                        other.update(len(chunk))
                    if self.on_chunk:
                        self.on_chunk(name, sent)
            tmp.replace(destination)
        finally:
            tmp.unlink(missing_ok=True)
            self.fetched.append((name, sent))
        return str(destination)

    def hf_hub_download(self, repo_id, filename, local_dir=None,
                        tqdm_class=None, **kwargs):
        self.calls.append(("hf_hub_download", repo_id, filename))
        size = len(self.files[filename])
        bar = tqdm_class(total=size, initial=0, desc=filename, unit="B",
                         unit_scale=True)
        with bar:
            return self._send(filename, local_dir, bar)

    def snapshot_download(self, repo_id, local_dir=None, allow_patterns=None,
                          tqdm_class=None, local_files_only=False, **kwargs):
        """As huggingface_hub 2.x: one transfer bar and one reconstruction
        bar for the whole snapshot, both of the given class."""
        import fnmatch

        self.calls.append(("snapshot_download", repo_id, local_files_only))
        if local_files_only:
            return local_dir
        # As huggingface_hub 2.4.0 builds them: both measured in bytes.
        transfer = tqdm_class(desc="Downloading bytes", total=0, initial=0,
                              unit="B", unit_scale=True)
        reconstruct = tqdm_class(desc="Reconstructing (incomplete total...)",
                                 total=0, initial=0, unit="B", unit_scale=True)
        pathlib.Path(local_dir).mkdir(parents=True, exist_ok=True)
        for name in self.files:
            if any(fnmatch.fnmatch(name, p) for p in allow_patterns):
                self._send(name, local_dir, reconstruct, extra_bars=(transfer,))
        return local_dir

    def install(self, monkeypatch):
        import huggingface_hub

        monkeypatch.setattr(huggingface_hub, "HfApi", self.api())
        monkeypatch.setattr(huggingface_hub, "hf_hub_download",
                            self.hf_hub_download)
        monkeypatch.setattr(huggingface_hub, "snapshot_download",
                            self.snapshot_download)
        return self


@pytest.fixture
def hub(monkeypatch):
    # Every byte callback delivered, so the sums can be checked exactly.
    monkeypatch.setattr(download_progress, "DEFAULT_INTERVAL", 0.0)
    return FakeHub().install(monkeypatch)


class Verify:
    """The offline smoke test (warm_up), stood in for."""

    def __init__(self, monkeypatch, events):
        self.fail = None
        self.calls = 0

        verify = self

        class Result:
            engine_version = "test"

        def warm_up(engine):
            verify.calls += 1
            events.append(("warm_up", engine.model_size))
            if verify.fail:
                raise RuntimeError(verify.fail)
            return Result()

        monkeypatch.setattr(whisper_local.FasterWhisperEngine, "warm_up", warm_up)
        try:
            import faster_whisper  # noqa: F401
        except ImportError:  # pragma: no cover - the ASR extra is absent
            import sys
            monkeypatch.setitem(sys.modules, "faster_whisper",
                                types.SimpleNamespace(WhisperModel=object))


@pytest.fixture
def events():
    return []


@pytest.fixture
def verify(monkeypatch, events):
    return Verify(monkeypatch, events)


def install(config, tmp_path, events, **kwargs):
    from babelfishr.preparation import install_asr_model

    config.app_home = str(tmp_path / "home")
    return install_asr_model(config, MODEL, mode=OperatingMode.ONLINE_SETUP,
                             progress=lambda p: events.append(p), **kwargs)


def snapshots(events):
    return [e for e in events if isinstance(e, DownloadProgress)]


def model_dir(config):
    return model_directory_for(config.paths().models, MODEL)


# --- (a) a known total ---------------------------------------------------------

def test_known_total_reports_real_bytes_monotonically(config, tmp_path, hub,
                                                      verify, events):
    step, ok, detail = install(config, tmp_path, events)
    assert ok, detail
    seen = snapshots(events)
    assert all(isinstance(s, DownloadProgress) for s in seen)
    phases = [s.phase for s in seen]
    assert phases[0] == "resolving"
    assert phases[-1] == "done" and phases.count("done") == 1
    assert phases[-2] == "verifying"
    # done only after the verification ran
    warm = events.index(("warm_up", MODEL))
    assert all(s.phase != "done" for s in events[:warm]
               if isinstance(s, DownloadProgress))
    assert any(s.phase == "verifying" for s in events[:warm]
               if isinstance(s, DownloadProgress))

    total = sum(len(FILES[name]) for name in WANTED)
    downloading = [s for s in seen if s.phase == "downloading"]
    assert {s.bytes_total for s in downloading} == {total}
    fractions = [s.fraction for s in seen if s.phase != "resolving"]
    assert all(f is not None for f in fractions)
    assert fractions == sorted(fractions)        # monotonic non-decreasing
    assert fractions[-1] == 1.0
    assert seen[-1].bytes_done == sum(hub.chunks) == total
    # bytes reported equal the real chunks received, step by step
    byte_steps = [b - a for a, b in zip(
        [s.bytes_done for s in downloading], [s.bytes_done for s in downloading][1:])]
    assert [d for d in byte_steps if d] == hub.chunks
    assert seen[-1].files_total == 3 and seen[-1].files_done == 3
    files_done = [s.files_done for s in downloading]
    assert files_done == sorted(files_done) and files_done[0] == 0
    assert {s.file for s in downloading if s.file} == set(WANTED)
    # the same files faster-whisper fetches, weights last; README.md is not one
    assert [c[2] for c in hub.calls if c[0] == "hf_hub_download"] == [
        "config.json", "tokenizer.json", "model.bin"]
    assert inspect_model_directory(model_dir(config))[0] is ModelState.COMPLETE


def test_callbacks_are_throttled_but_file_boundaries_always_arrive():
    now = [0.0]
    received = []
    tracker = ProgressTracker(received.append, clock=lambda: now[0], interval=0.1)
    tracker.update(phase="downloading", bytes_total=100, files_total=2,
                   file="a")
    for _ in range(50):                  # 50 chunks within 50 ms
        now[0] += 0.001
        tracker.add_bytes(1)
    assert len(received) == 1            # only the forced phase change
    now[0] += 0.2
    tracker.add_bytes(1)
    assert received[-1].bytes_done == 51
    tracker.update(file="b", files_done=1)   # a boundary: never throttled
    assert received[-1].file == "b" and len(received) == 3
    # immutable snapshots
    with pytest.raises(Exception):
        received[-1].bytes_done = 0


# --- (b) an unknown total ------------------------------------------------------

def test_unknown_total_when_metadata_is_unavailable(config, tmp_path, monkeypatch,
                                                    verify, events):
    monkeypatch.setattr(download_progress, "DEFAULT_INTERVAL", 0.0)
    hub = FakeHub(metadata=False).install(monkeypatch)
    step, ok, detail = install(config, tmp_path, events)
    assert ok, detail
    assert any(c[0] == "snapshot_download" for c in hub.calls)
    downloading = [s for s in snapshots(events) if s.phase == "downloading"]
    assert downloading
    assert all(s.bytes_total is None and s.fraction is None for s in downloading)
    done = [s.bytes_done for s in downloading]
    assert done == sorted(done) and done[-1] > 0
    # the separate network-transfer bar is not counted twice
    assert done[-1] == sum(hub.chunks) == sum(len(FILES[n]) for n in WANTED)
    assert snapshots(events)[-1].phase == "done"
    assert inspect_model_directory(model_dir(config))[0] is ModelState.COMPLETE


# --- (c) cancellation ----------------------------------------------------------

def test_cancel_mid_file_leaves_an_incomplete_model_that_a_retry_completes(
        config, tmp_path, hub, verify, events):
    from babelfishr.preparation import install_asr_model

    stop = {"now": False}

    def on_chunk(name, sent):
        if name == "model.bin" and sent >= 3 * CHUNK:
            stop["now"] = True

    hub.on_chunk = on_chunk
    with pytest.raises(DownloadCancelled):
        install(config, tmp_path, events, cancelled=lambda: stop["now"])
    sent = dict(hub.fetched)["model.bin"]               # stopped mid-file
    assert 3 * CHUNK <= sent < len(FILES["model.bin"])
    assert verify.calls == 0
    assert all(s.phase != "done" for s in snapshots(events))
    state, missing = inspect_model_directory(model_dir(config))
    assert state is ModelState.INCOMPLETE and "model.bin" in missing
    assert not list(model_dir(config).rglob("*.incomplete"))

    # Retry: only the missing weights are fetched, and the model completes.
    hub.on_chunk = None
    hub.calls.clear()
    events.clear()
    step, ok, detail = install_asr_model(config, MODEL,
                                         mode=OperatingMode.ONLINE_SETUP,
                                         progress=events.append,
                                         cancelled=lambda: False)
    assert ok, detail
    assert [c[2] for c in hub.calls if c[0] == "hf_hub_download"] == ["model.bin"]
    last = snapshots(events)[-1]
    assert last.phase == "done" and last.files_done == last.files_total == 3
    assert last.bytes_total == len(FILES["model.bin"]) == last.bytes_done
    assert inspect_model_directory(model_dir(config))[0] is ModelState.COMPLETE


def test_cancel_through_the_worker_job_is_reported_as_cancelled(
        config, tmp_path, hub, verify, events):
    from babelfishr.ui.workers import (CancellationToken, Cancelled,
                                       install_model_job)

    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    token = CancellationToken()

    def on_chunk(name, sent):
        if name == "model.bin":
            token.cancel()

    hub.on_chunk = on_chunk
    with pytest.raises(Cancelled):
        install_model_job(config, MODEL, token=token, progress=events.append)
    assert inspect_model_directory(model_dir(config))[0] is ModelState.INCOMPLETE
    assert snapshots(events) and snapshots(events)[-1].phase == "downloading"


# --- (d) verification fails ----------------------------------------------------

def test_a_failed_verification_is_not_installed(config, tmp_path, hub, verify,
                                                events):
    verify.fail = "the smoke test produced nothing"
    step, ok, detail = install(config, tmp_path, events)
    assert not ok and "not loadable offline" in detail
    phases = [s.phase for s in snapshots(events)]
    assert "verifying" in phases and "done" not in phases


# --- (e) already complete ------------------------------------------------------

def test_an_already_complete_model_is_not_downloaded_again(config, tmp_path, hub,
                                                           verify, events):
    from babelfishr.preparation import prepare_field

    step, ok, detail = install(config, tmp_path, events)
    assert ok, detail
    hub.calls.clear()
    hub.chunks.clear()
    events.clear()
    # install_asr_model only verifies a complete model ...
    step, ok, detail = install(config, tmp_path, events)
    assert ok, detail
    assert hub.calls == [] and hub.chunks == []
    assert [s.phase for s in snapshots(events)] == ["verifying", "done"]
    assert snapshots(events)[-1].fraction == 1.0
    # ... and a full preparation run checks the file list but fetches nothing.
    events.clear()
    config.mode = OperatingMode.ONLINE_SETUP.value
    result = prepare_field(config, asr_model=MODEL, language_pairs=[],
                           progress=events.append)
    assert result.asr_ok, result.summary()
    assert [c[0] for c in hub.calls] == ["model_info"] and hub.chunks == []
    last = snapshots(events)[-1]
    assert last.phase == "done" and last.files_done == 3
    assert last.bytes_done == 0 and last.bytes_total == 0 and last.fraction == 1.0


# --- the worker signal ---------------------------------------------------------

def test_progress_reaches_the_gui_thread_through_the_worker_signal(
        config, tmp_path, hub, verify, events):
    from PySide6 import QtWidgets

    from babelfishr.ui.workers import install_model_job, run_in_background

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    received, finished = [], []
    run_in_background(install_model_job, config, MODEL,
                      on_progress=received.append,
                      on_finished=finished.append,
                      on_failed=lambda message: finished.append(("failed", message)))
    deadline = time.monotonic() + 30
    while not finished and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    for _ in range(20):
        app.processEvents()
    assert finished and finished[0][1] is True, finished
    assert received and all(isinstance(s, DownloadProgress) for s in received)
    assert received[-1].phase == "done"
    assert any(s.phase == "downloading" and s.bytes_done for s in received)
