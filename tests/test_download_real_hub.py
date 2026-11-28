"""Model downloads through the real huggingface_hub client, in memory.

Found on a Mac (director's check of ab9757b): with the file sizes
unavailable (model_info returned 503) the download fell back to
huggingface_hub.snapshot_download, which called set_description_str on
the progress class and failed with AttributeError - the unit fakes had
stood in for the Hub client and never made that call. Here the actual
huggingface_hub client runs every request against an in-memory Hub
(huggingface_hub.set_client_factory with an httpx2 MockTransport): no
network, no real model.

- verified total: all three files (262,160 bytes) arrive exactly, the
  progress is determinate and ends at 100%;
- no verified total (model_info fails): the snapshot_download fallback
  completes, the bytes are counted once (not the separate network bar,
  not the count of files), and the fraction stays unknown;
- through the application: the install job and the Local Engines panel
  report the download and finish only after the offline verification.
"""

from __future__ import annotations

import hashlib
import pathlib
import time

import pytest

httpx2 = pytest.importorskip("httpx2")
huggingface_hub = pytest.importorskip("huggingface_hub")

REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
#: Revision A: the commit the test pin names.
COMMIT = "0123456789abcdef0123456789abcdef01234567"
#: Revision B: a newer, equally legitimate commit with the same file names
#: and lengths but different content. "main" points here.
COMMIT_B = "fedcba9876543210fedcba9876543210fedcba98"
FILES = {
    "config.json": b'{"alignment_heads": []}'.ljust(1024, b" "),
    "tokenizer.json": bytes(range(256)) * 16,              # 4,096
    "model.bin": (b"\x00\x01binary-weights" * 20_000)[:257_040],
}
TOTAL = sum(len(v) for v in FILES.values())               # 262,160
_ROTATE = bytes((i + 1) % 256 for i in range(256))


def other_revision(files):
    """Same names, same lengths, different bytes."""
    return {name: data.translate(_ROTATE) for name, data in files.items()}


FILES_B = other_revision(FILES)


def blob_id(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


class InMemoryHub:
    """Enough of the Hub's HTTP API for model_info, snapshot_download and
    hf_hub_download, with real lengths, commits, Git blob ETags and Range
    requests, serving two revisions (``main`` is the newer one, B).
    *tamper* replaces what is served for a file (any revision)."""

    def __init__(self, fail_model_info=False, chunk=8192, files=None, tamper=None):
        files = files or FILES
        self.revisions = {COMMIT: files, COMMIT_B: other_revision(files)}
        self.main = COMMIT_B
        self.fail_model_info = fail_model_info
        self.chunk = chunk
        self.tamper = dict(tamper or {})
        self.requests = []
        self.revisions_asked = []           # the revision of every rev-scoped request
        self.ranges = []
        self.served = {}

    @property
    def files(self):
        return self.revisions[COMMIT]

    def _commit(self, revision):
        self.revisions_asked.append(revision)
        return self.main if revision == "main" else revision

    def info(self, commit):
        files = self.revisions[commit]
        return {"id": REPO, "modelId": REPO, "sha": commit, "private": False,
                "siblings": [{"rfilename": name, "size": len(data), "blobId": blob_id(data)}
                             for name, data in files.items()]}

    def __call__(self, request):
        path, method = request.url.path, request.method
        query = str(request.url.query)
        self.requests.append((method, path, query))
        api = f"/api/models/{REPO}"
        if path == api:
            self.revisions_asked.append("main")      # no revision named = main
            return httpx2.Response(200, json=self.info(self.main))
        if path.startswith(f"{api}/revision/"):
            commit = self._commit(path[len(f"{api}/revision/"):])
            if commit not in self.revisions:
                return httpx2.Response(404)
            if self.fail_model_info and "blobs=true" in query:
                return httpx2.Response(503, json={"error": "Service Unavailable"})
            return httpx2.Response(200, json=self.info(commit))
        if path.startswith(f"{api}/tree/"):
            commit = self._commit(path[len(f"{api}/tree/"):].split("/", 1)[0])
            return httpx2.Response(200, json=[
                {"type": "file", "path": name, "size": len(data), "oid": blob_id(data)}
                for name, data in self.revisions.get(commit, {}).items()])
        prefix = f"/{REPO}/resolve/"
        if path.startswith(prefix):
            revision, name = path[len(prefix):].split("/", 1)
            commit = self._commit(revision)
            data = self.revisions.get(commit, {}).get(name)
            if data is None:
                return httpx2.Response(404)
            data = self.tamper.get(name, data)
            headers = {"X-Repo-Commit": commit, "ETag": f'"{blob_id(data)}"',
                       "Content-Length": str(len(data)),
                       "Content-Type": "application/octet-stream",
                       "Accept-Ranges": "bytes"}
            if method == "HEAD":
                return httpx2.Response(200, headers=headers)
            status, start = 200, 0
            wanted = request.headers.get("Range", "")
            if wanted.startswith("bytes="):
                start = int(wanted[len("bytes="):].split("-", 1)[0] or 0)
                self.ranges.append((name, start))
                status = 206
                headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
                headers["Content-Length"] = str(len(data) - start)
            body = data[start:]
            self.served[name] = self.served.get(name, 0) + len(body)
            chunks = [body[i:i + self.chunk] for i in range(0, len(body), self.chunk)]
            return httpx2.Response(status, headers=headers, content=iter(chunks))
        return httpx2.Response(404)


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """Install an in-memory Hub as huggingface_hub's HTTP client, and pin
    the model to its revision A."""
    from huggingface_hub.utils import _http
    from pinning import pin

    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-home"))
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    previous = _http._GLOBAL_CLIENT_FACTORY
    made = {}

    def install(**options):
        made["hub"] = InMemoryHub(**options)
        pin(monkeypatch, "large-v3-turbo", made["hub"].files, repo=REPO, revision=COMMIT)
        huggingface_hub.set_client_factory(
            lambda: httpx2.Client(transport=httpx2.MockTransport(made["hub"])))
        return made["hub"]

    yield install
    huggingface_hub.set_client_factory(previous)


def download(tmp_path, cancelled=None):
    from babelfishr.providers import whisper_local

    seen = []
    target = whisper_local.prepare_model("large-v3-turbo", tmp_path / "models",
                                         progress=seen.append, cancelled=cancelled)
    return pathlib.Path(target), seen


def verified_state(target, model="large-v3-turbo"):
    from babelfishr.model_integrity import integrity_state

    return integrity_state(target, model)[0].value


def assert_files(target):
    for name, data in FILES.items():
        assert (target / name).read_bytes() == data, f"{name} is not byte-exact"
    assert target.name == "large-v3-turbo"


def test_a_verified_total_downloads_all_three_files_exactly(hub, tmp_path):
    server = hub()
    target, seen = download(tmp_path)
    assert_files(target)
    assert sum(server.served.values()) == TOTAL
    downloading = [p for p in seen if p.phase == "downloading"]
    assert downloading and all(p.bytes_total == TOTAL for p in downloading)
    fractions = [p.fraction for p in downloading]
    assert fractions == sorted(fractions)
    final = seen[-1]
    assert final.bytes_done == TOTAL and final.fraction == 1.0
    assert final.files_total == 3
    assert set(server.revisions_asked) == {COMMIT}
    assert verified_state(target) == "verified"


def test_without_a_verified_total_the_fallback_completes(hub, tmp_path):
    server = hub(fail_model_info=True)
    target, seen = download(tmp_path)                    # used to raise AttributeError
    assert_files(target)
    assert any(path.endswith(f"/revision/{COMMIT}") for _, path, _ in server.requests), \
        "the snapshot_download fallback was not exercised"
    assert set(server.revisions_asked) == {COMMIT}, "the fallback asked for another revision"
    assert verified_state(target) == "verified"
    downloading = [p for p in seen if p.phase == "downloading"]
    assert downloading and all(p.bytes_total is None and p.fraction is None
                               for p in downloading)
    counts = [p.bytes_done for p in downloading]
    assert counts == sorted(counts)
    assert seen[-1].bytes_done == TOTAL, \
        "bytes miscounted (network bar or file count counted as bytes?)"


def test_cancel_on_the_fallback_path_stops_and_a_retry_completes(hub, tmp_path):
    """Cancelled as soon as real bytes arrive: the cancellation propagates
    out of snapshot_download (it is checked on every byte update, which on
    this path comes in blocks of up to 10 MB), the model is left
    incomplete, and a retry completes it."""
    from babelfishr.download_progress import DownloadCancelled
    from babelfishr.providers import whisper_local
    from babelfishr.providers.whisper_local import ModelState, inspect_model_directory

    large = dict(FILES, **{"model.bin": b"w" * (30 * 1024 * 1024)})   # > 10 MB blocks
    hub(fail_model_info=True, chunk=1024 * 1024, files=large)
    received = []

    def progress(p):
        if p.bytes_done:
            received.append(p.bytes_done)

    with pytest.raises(DownloadCancelled):
        whisper_local.prepare_model("large-v3-turbo", tmp_path / "models",
                                    progress=progress, cancelled=lambda: bool(received))
    full = sum(len(v) for v in large.values())
    assert received and received[-1] < full, "cancelled only after everything arrived"
    state, _ = inspect_model_directory(tmp_path / "models" / "large-v3-turbo")
    assert state is not ModelState.COMPLETE, "a cancelled model looks complete"
    hub(files=large)
    target, _ = download(tmp_path)
    assert (target / "model.bin").stat().st_size == len(large["model.bin"])


@pytest.mark.parametrize("fail_model_info", [False, True], ids=["verified-total", "unknown-total"])
def test_the_install_panel_finishes_after_verification(hub, tmp_path, monkeypatch, qt_app,
                                                       config, store, fail_model_info):
    """The real install job (Local Engines' Install) over the in-memory Hub:
    progress reaches the panel and "installed" comes only after the offline
    verification (stood in for: no real Whisper weights here)."""
    import babelfishr.providers.whisper_local as whisper_local
    from babelfishr.app import BabelFishRApp
    from babelfishr.modes import OperatingMode
    from babelfishr.ui.main_window import MainWindow
    from test_offline_indicator import Jobs
    from PySide6 import QtWidgets

    hub(fail_model_info=fail_model_info)
    verified = []

    class Result:
        engine_version = "test"

    monkeypatch.setattr(whisper_local.FasterWhisperEngine, "warm_up",
                        lambda self: verified.append(time.monotonic()) or Result())
    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    Jobs(monkeypatch)
    window = MainWindow(BabelFishRApp(config=config, store=store))
    monkeypatch.setattr(window, "_choose", lambda *a, **k: "Download")
    texts = []
    try:
        window._install_model("large-v3-turbo")
        deadline = time.monotonic() + 60
        while window._install_worker is not None and time.monotonic() < deadline:
            QtWidgets.QApplication.processEvents()
            texts.append(window.install_panel.status.text())
            time.sleep(0.005)
        final = window.install_panel.status.text()
        assert "is installed and verified offline" in final, final
        assert verified, "installed without the offline verification"
        assert_files(window.app.whisper_model_directory("large-v3-turbo"))
        if fail_model_info:
            assert any("received (total not known)" in t for t in texts) or \
                any("Verifying" in t for t in texts)
    finally:
        window.close()


@pytest.fixture(scope="module")
def qt_app():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_file_count_bar_never_adds_bytes_and_an_unknown_total_stays_unknown():
    """The SDK's own "Fetching 3 files" bar (hf_thread_map: unit unset,
    total=3) counts finished files, not bytes: it must not add one "byte"
    per file (262,163 for 262,160 received), and it must not turn an
    unknown total into a known one."""
    from babelfishr.download_progress import ProgressTracker, progress_bar_class

    seen = []
    tracker = ProgressTracker(seen.append, interval=0)
    tracker.update(phase="downloading")
    bar_class = progress_bar_class(tracker)
    reconstruct = bar_class(desc="Reconstructing (incomplete total...)", total=0,
                            initial=0, unit="B", unit_scale=True)
    network = bar_class(desc="Downloading bytes", total=0, initial=0, unit="B")
    with bar_class(total=3, desc="Fetching 3 files") as files:
        for size in (1024, 4096, 257040):
            reconstruct.update(size)
            network.update(size)
            files.update(1)
    assert tracker.snapshot.bytes_done == TOTAL
    assert tracker.snapshot.bytes_total is None and tracker.snapshot.fraction is None
    assert all(p.bytes_total is None for p in seen)
