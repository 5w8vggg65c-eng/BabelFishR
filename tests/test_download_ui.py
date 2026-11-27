"""Speech-model download progress, in place, at real window sizes.

From the field test: a large-v3-turbo download ran a long time with no
visible progress, and the progress bar and its box stretched off the right
of the screen. Here a slow, chunked download is stood in for (the
network layer only; the job, worker, signals and widgets are real), from
both entry points - Local Engines' Install and the Setup Assistant:

- with a verified total: a determinate bar and "X of Y (n%)" from the bytes
  actually received, across several files;
- with no verified total: the bytes received and a busy bar;
- preparation and offline verification named as their own phases, and
  "installed" only once the install returned success; a failed
  verification says not installed; Cancel stops it and Install retries;
- the window stays responsive, and every line - a long model folder or
  file name included - stays inside a MacBook-size and a minimum-size
  window.
"""

from __future__ import annotations

import time

import pytest
from PySide6 import QtCore, QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.download_progress import DownloadCancelled, DownloadProgress
from babelfishr.modes import OperatingMode
from test_offline_indicator import Jobs

TURBO = "large-v3-turbo"
LONG_FILE = "model.bin" if False else (
    "a-very-long-repository-path/" * 6 + "model-00001-of-00002.safetensors")


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump_until(predicate, timeout=20.0, each=None):
    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        app.processEvents()
        if each:
            each()
        time.sleep(0.005)
    return predicate()


class FakeHub:
    """install_asr_model, with its download stood in for: chunks of real
    byte counts over several files, slowly, honouring cancellation."""

    def __init__(self, files=((LONG_FILE, 400_000), ("config.json", 2_000),
                              ("tokenizer.json", 98_000)),
                 known_total=True, verify_ok=True, chunk=20_000, pace=0.01):
        self.files, self.known_total, self.verify_ok = files, known_total, verify_ok
        self.chunk, self.pace = chunk, pace
        self.delivered = 0
        self.started = None

    def __call__(self, config, model_name, *, report=None, mode=None,
                 progress=None, cancelled=None):
        self.started = time.monotonic()
        total = sum(size for _, size in self.files) if self.known_total else None
        progress(DownloadProgress(phase="resolving"))
        done = 0
        for index, (name, size) in enumerate(self.files):
            sent = 0
            while sent < size:
                if cancelled and cancelled():
                    raise DownloadCancelled()
                time.sleep(self.pace)
                step = min(self.chunk, size - sent)
                sent += step
                done += step
                self.delivered = done
                progress(DownloadProgress(phase="downloading", bytes_done=done,
                                          bytes_total=total, file=name, files_done=index,
                                          files_total=len(self.files)))
        progress(DownloadProgress(phase="verifying", bytes_done=done, bytes_total=total,
                                  files_done=len(self.files), files_total=len(self.files)))
        time.sleep(0.05)
        if not self.verify_ok:
            return ("Local ASR model", False, "present but not loadable offline: bad file")
        progress(DownloadProgress(phase="done", bytes_done=done, bytes_total=total,
                                  files_done=len(self.files), files_total=len(self.files)))
        return ("Local ASR model", True, f"{model_name} verified")


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    home = tmp_path / ("Users/eric-field-operator/Library/Application Support/" * 2)
    config.app_home = str(home / "BabelFishR")
    config.mode = OperatingMode.ONLINE_SETUP.value
    Jobs(monkeypatch)
    win = MainWindow(BabelFishRApp(config=config, store=store))
    win.resize(1280, 800)                     # a MacBook-size window
    win.show()
    monkeypatch.setattr(win, "_choose", lambda *a, **k: "Download")
    yield win
    deadline = time.monotonic() + 10
    while win._install_worker is not None and time.monotonic() < deadline:
        qt_app.processEvents()
    win.close()


def install(window, monkeypatch, hub):
    import babelfishr.preparation as preparation

    monkeypatch.setattr(preparation, "install_asr_model", hub)
    seen = {"values": [], "texts": [], "ranges": set(), "ticks": 0, "widths": set()}
    tick = QtCore.QTimer()
    tick.timeout.connect(lambda: seen.__setitem__("ticks", seen["ticks"] + 1))
    tick.start(10)
    panel = window.install_panel
    window._install_model(TURBO)

    def watch():
        seen["values"].append(panel.bar.value())
        seen["ranges"].add((panel.bar.minimum(), panel.bar.maximum()))
        seen["texts"].append(panel.status.text())
        seen["widths"].add(window.width())

    return seen, watch, tick


def inside(window, widget):
    left = widget.mapTo(window, QtCore.QPoint(0, 0)).x()
    return left >= 0 and left + widget.width() <= window.width()


def test_known_total_shows_real_bytes_and_settles_installed(qt_app, window, monkeypatch):
    hub = FakeHub()
    seen, watch, tick = install(window, monkeypatch, hub)
    panel = window.install_panel
    assert panel.isVisible() and "Installing Whisper" in panel.status.text()
    assert pump_until(lambda: window._install_worker is None, 30, watch)
    tick.stop()
    determinate = [v for v in seen["values"] if v > 0]
    assert determinate == sorted(determinate) and max(determinate) >= 990
    assert (0, 1000) in seen["ranges"]
    texts = seen["texts"]
    assert any(" of 500.0 KB (" in t and "file 1 of 3" in t for t in texts)
    assert any("Verifying" in t for t in texts)
    verifying_at = next(i for i, t in enumerate(texts) if "Verifying" in t)
    assert not any("installed" in t for t in texts[:verifying_at]), \
        "said installed before verification"
    assert "is installed and verified offline" in panel.status.text()
    assert seen["ticks"] > 20, "the window was not responsive during the download"
    assert seen["widths"] == {1280}, "the window changed width"


def test_unknown_total_shows_bytes_received_and_a_busy_bar(qt_app, window, monkeypatch):
    hub = FakeHub(known_total=False)
    seen, watch, tick = install(window, monkeypatch, hub)
    assert pump_until(lambda: window._install_worker is None, 30, watch)
    tick.stop()
    received = [t for t in seen["texts"] if "received (total not known)" in t]
    assert received, "no byte count was shown without a total"
    assert (0, 0) in seen["ranges"], "no busy indicator without a total"
    amounts = [float(t.split("\u2014 ")[1].split(" KB")[0]) for t in received]
    assert amounts == sorted(amounts) and amounts[-1] >= 400, amounts


def test_a_failed_verification_is_not_installed_and_can_retry(qt_app, window, monkeypatch):
    hub = FakeHub(verify_ok=False)
    seen, watch, tick = install(window, monkeypatch, hub)
    assert pump_until(lambda: window._install_worker is None, 30, watch)
    tick.stop()
    text = window.install_panel.status.text()
    assert "was not installed" in text and "tries again" in text
    assert "installed and verified" not in text
    assert window.install_model_button.isEnabled()


def test_cancel_stops_the_download_and_install_retries(qt_app, window, monkeypatch):
    hub = FakeHub(files=(("model.bin", 5_000_000),), pace=0.01)
    seen, watch, tick = install(window, monkeypatch, hub)
    panel = window.install_panel
    pump_until(lambda: hub.delivered > 100_000, 10, watch)
    panel.cancel_button.click()
    assert pump_until(lambda: window._install_worker is None, 10, watch)
    tick.stop()
    assert "cancelled" in panel.status.text() and hub.delivered < 5_000_000
    retry = FakeHub()
    seen, watch, tick = install(window, monkeypatch, retry)
    assert pump_until(lambda: window._install_worker is None, 30, watch)
    tick.stop()
    assert "is installed and verified offline" in panel.status.text()


@pytest.mark.parametrize("size", ["macbook", "minimum"])
def test_progress_stays_inside_the_window(qt_app, window, monkeypatch, size):
    if size == "minimum":
        window.resize(window.minimumSizeHint().width(), window.minimumSizeHint().height())
    qt_app.processEvents()
    width = window.width()
    hub = FakeHub(pace=0.02)
    seen, watch, tick = install(window, monkeypatch, hub)
    panel = window.install_panel

    def check():
        watch()
        if panel.isVisible():
            for part in (panel, panel.bar, panel.status, panel.file, panel.cancel_button):
                assert inside(window, part), f"{part} reaches past the window"
            viewport = window.sidebar.viewport()
            assert panel.mapTo(viewport, QtCore.QPoint(panel.width(), 0)).x() \
                <= viewport.width() + 1

    assert pump_until(lambda: window._install_worker is None, 30, check)
    tick.stop()
    assert seen["widths"] == {width}, "the window grew"
    assert panel.file.full_text() == "" or panel.file.toolTip()


def test_setup_shows_the_same_progress_inside_its_window(qt_app, config, store, tmp_path,
                                                         monkeypatch):
    import babelfishr.ui.workers as workers
    from babelfishr.preparation import PreparationResult
    from babelfishr.readiness import ReadinessReport
    from babelfishr.ui.setup_assistant import SetupAssistant

    hub = FakeHub()

    def prepare_field_job(config, asr_model, language_pairs, report=None, token=None,
                          progress=None):
        step = hub(config, asr_model, progress=progress,
                   cancelled=lambda: token.cancelled)
        result = PreparationResult()
        result.add(*step)
        return {"preparation": result, "readiness": ReadinessReport(),
                "asr_model": asr_model, "language_pairs": []}

    import babelfishr.ui.setup_assistant as setup_module

    monkeypatch.setattr(setup_module, "prepare_field_job", prepare_field_job)
    config.app_home = str(tmp_path / ("Users/eric/" * 8) / "BabelFishR")
    app = BabelFishRApp(config=config, store=store)
    setup = SetupAssistant(app)
    setup.resize(720, 560)
    setup.show()
    setup.model_box.setCurrentIndex(setup.model_box.findData(TURBO))
    widths, texts = set(), []
    setup._start()
    panel = setup.download_panel

    def watch():
        widths.add(setup.width())
        texts.append(panel.status.text())
        assert inside(setup, panel.bar) and inside(setup, setup.status_label)

    assert pump_until(lambda: setup._worker is None, 30, watch)
    assert any(" of 500.0 KB (" in t for t in texts)
    assert panel.status.text() == "Speech model installed and verified offline."
    assert widths == {720}, "the Setup window grew"
    setup.close()


def test_no_progress_dialog_resizes_to_a_long_path(qt_app, window, monkeypatch):
    """The director's baseline probe on dee017b: the Local Engines
    QProgressDialog (range 0, 0) grew from 226 px to 1,587 px on an 800 px
    screen when its label received a long model path. The install now
    shows progress in place, with no dialog, and a long path changes no
    width."""
    long_path = "/Users/" + "very-long-folder-name/" * 30 + "large-v3-turbo/model.bin"
    hub = FakeHub(files=((long_path, 200_000),), pace=0.02)
    seen, watch, tick = install(window, monkeypatch, hub)
    panel = window.install_panel
    widths = set()

    def check():
        watch()
        assert not window.findChildren(QtWidgets.QProgressDialog), \
            "a progress dialog was opened"
        widths.add((window.width(), panel.width(), window.sidebar.width()))
        panel.set_message(long_path)

    assert pump_until(lambda: window._install_worker is None, 30, check)
    tick.stop()
    assert len(widths) == 1, f"a width changed: {widths}"
    assert panel.file.toolTip() in ("", long_path)
