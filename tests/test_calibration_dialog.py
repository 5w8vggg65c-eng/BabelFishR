"""Calibrate: a live, responsive listen with a result to apply.

Pressing Calibrate opens a themed box at once with the exact note "Note:
Please be quiet during this process.", live waveform samples from the
selected input, and progress plus time remaining for the real listening
(measured on audio received). The window keeps working meanwhile. Then the
existing summary and recommended threshold, with Apply. The threshold in
force changes only on Apply. The input is released when the listen
completes, is cancelled, or fails. A scripted input delivers known audio
(0.1 s blocks of steady noise, paced); nothing is recorded.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import numpy as np
import pytest
from PySide6 import QtCore, QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import AudioBlock
from babelfishr.config import Config
from babelfishr.ui.calibration_dialog import NOTE, CalibrationDialog
from test_offline_indicator import Jobs

SR = 16_000


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class ScriptedInput:
    """Delivers *seconds* of noise at *level*, a 0.1 s block every *pace* s."""

    def __init__(self, seconds=5.0, level=0.003, pace=0.01, fail_start=None):
        self.sample_rate = SR
        rng = np.random.default_rng(7)
        size = SR // 10
        self.blocks = [(level * rng.standard_normal(size)).astype(np.float32)
                       for _ in range(int(seconds * 10))]
        self.pace, self.fail_start = pace, fail_start
        self.started = self.stopped = False
        self.finished = False
        self._offset = 0.0

    def start(self):
        if self.fail_start:
            raise RuntimeError(self.fail_start)
        self.started = True

    def stop(self):
        self.stopped = True

    def read(self, timeout=1.0):
        if not self.blocks or self.stopped:
            self.finished = True
            return None
        time.sleep(self.pace)
        samples = self.blocks.pop(0)
        block = AudioBlock(samples=samples, sample_rate=SR,
                           timestamp=dt.datetime.now(dt.timezone.utc), offset=self._offset)
        self._offset += block.duration
        return block


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.source_path = str(tmp_path / "settings.toml")
    config.detector.mode, config.detector.threshold_dbfs = "fixed", -41.0
    config.save()
    Jobs(monkeypatch)
    win = MainWindow(BabelFishRApp(config=config, store=store))
    monkeypatch.setattr(win.input_panel, "ready_to_monitor", lambda: (True, ""))
    yield win
    win.close()


def run(window, monkeypatch, source, during):
    """Press Calibrate; *during(dialog, seen)* plays the operator."""
    monkeypatch.setattr(window, "_calibration_source_factory", lambda: (lambda: source))
    seen = {"progress": [], "status": [], "frames": 0, "ticks": 0}

    def exec_(dialog):
        seen["opened"] = (dialog.note.text(), dialog.progress.value(), dialog.isModal())
        tick = QtCore.QTimer()
        tick.timeout.connect(lambda: seen.__setitem__("ticks", seen["ticks"] + 1))
        tick.start(10)
        try:
            return during(dialog, seen)
        finally:
            tick.stop()

    def watch(dialog):
        seen["progress"].append(dialog.progress.value())
        seen["status"].append(dialog.status.text())
        if dialog.waveform.frame is not None:
            seen["frames"] += 1

    seen["watch"] = watch
    monkeypatch.setattr(CalibrationDialog, "exec", exec_)
    window._calibrate()
    return seen


def pump_until(predicate, timeout=10.0, each=None):
    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        app.processEvents()
        if each:
            each()
        time.sleep(0.005)
    return predicate()


def test_a_full_listen_shows_live_samples_progress_and_a_result_to_apply(
        qt_app, window, monkeypatch, config):
    source = ScriptedInput()

    def operator(dialog, seen):
        assert pump_until(lambda: dialog.result is not None, 15, lambda: seen["watch"](dialog))
        seen["threshold_before_apply"] = window.app.config.detector.threshold_dbfs
        seen["summary"] = dialog.summary.toPlainText()
        assert not dialog.apply_button.isHidden()
        dialog.apply_button.click()
        return 1

    seen = run(window, monkeypatch, source, operator)
    note, first_progress, modal = seen["opened"]
    assert note == NOTE == "Note: Please be quiet during this process."
    assert first_progress == 0
    assert seen["frames"] > 5, "no live samples were shown"
    values = seen["progress"]
    assert values == sorted(values) and values[-1] == 1000
    assert any("s remaining" in s for s in seen["status"])
    remaining = [float(s.split("… ")[1].split(" s")[0]) for s in seen["status"]
                 if "s remaining" in s]
    assert remaining == sorted(remaining, reverse=True) and remaining[-1] < 1.0
    assert seen["ticks"] > 10, "the window was not responsive during the listen"
    assert seen["threshold_before_apply"] == -41.0, "changed before Apply"
    assert "suggested detection threshold" in seen["summary"]
    result = window.calibration_dialog.result
    assert window.app.config.detector.threshold_dbfs == result.recommended_threshold_dbfs
    assert Config.load(config.source_path).detector.threshold_dbfs == \
        result.recommended_threshold_dbfs
    assert source.started and source.stopped, "the input was not released"


def test_closing_without_apply_keeps_the_threshold(qt_app, window, monkeypatch, config):
    source = ScriptedInput()

    def operator(dialog, seen):
        pump_until(lambda: dialog.result is not None, 15)
        dialog.close_button.click()
        return 0

    run(window, monkeypatch, source, operator)
    assert window.app.config.detector.threshold_dbfs == -41.0
    assert Config.load(config.source_path).detector.threshold_dbfs == -41.0
    assert source.stopped


def test_cancel_mid_listen_releases_the_input_and_changes_nothing(qt_app, window,
                                                                 monkeypatch):
    source = ScriptedInput(seconds=30, pace=0.02)

    def operator(dialog, seen):
        pump_until(lambda: dialog.progress.value() > 50, 10)
        dialog.close_button.click()                      # Cancel while listening
        assert dialog.source_released.wait(3), "the input stayed open after Cancel"
        pump_until(lambda: "cancelled" in dialog.status.text(), 2)
        seen["status_after"] = dialog.status.text()
        return 0

    seen = run(window, monkeypatch, source, operator)
    assert source.stopped and window.calibration_dialog.result is None
    assert window.app.config.detector.threshold_dbfs == -41.0


def test_a_device_that_cannot_open_says_so_and_releases(qt_app, window, monkeypatch):
    source = ScriptedInput(fail_start="the device is busy (permission denied)")

    def operator(dialog, seen):
        pump_until(lambda: "could not be measured" in dialog.status.text(), 5)
        seen["status_after"] = dialog.status.text()
        seen["apply"] = not dialog.apply_button.isHidden()
        return 0

    seen = run(window, monkeypatch, source, operator)
    assert "permission denied" in seen["status_after"] and not seen["apply"]
    assert window.calibration_dialog.source_released.is_set()
    assert window.app.config.detector.threshold_dbfs == -41.0


def test_silence_from_a_blocked_input_is_not_offered_as_a_result(qt_app, window,
                                                                 monkeypatch):
    source = ScriptedInput(level=0.0)                    # macOS gives zeros without access

    def operator(dialog, seen):
        pump_until(lambda: dialog.result is not None, 15)
        seen["apply"] = not dialog.apply_button.isHidden()
        seen["status_after"] = dialog.status.text()
        return 0

    seen = run(window, monkeypatch, source, operator)
    assert not seen["apply"] and "unchanged" in seen["status_after"]
    assert window.app.config.detector.threshold_dbfs == -41.0


def test_the_box_uses_the_console_look(qt_app):
    from babelfishr.ui import theme

    dialog = CalibrationDialog(lambda: ScriptedInput(), seconds=1.0)
    assert dialog.styleSheet() == theme.stylesheet(dialog)
    dialog.done(0)
