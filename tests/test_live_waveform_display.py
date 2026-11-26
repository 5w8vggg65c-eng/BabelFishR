"""The live waveform's display floor and gain - rendering only.

On a continuous noisy input the fixed -60 dBFS scale draws the steady
noise as a solid bar. Raising the display floor above the noise band puts
it on the centre line and leaves the voice peaks standing; a display gain
draws quiet input larger. The scale is printed on the plot, Reset returns
to -60 dBFS / +0 dB (which is exactly the original drawing), and the
choice is remembered. Nothing else changes: the samples, the meter
reading, the start-recording threshold and the detector settings are the
same before and after. Deterministic synthetic envelopes.
"""

from __future__ import annotations

import numpy as np
import pytest
from PySide6 import QtGui, QtWidgets

from babelfishr.audio.envelope import WINDOW_SECONDS, WaveformFrame
from babelfishr.config import Config
from babelfishr.ui.live_waveform import (DEFAULT_FLOOR_DBFS, LiveWaveform,
                                         display_magnitude)

BUCKETS = WINDOW_SECONDS * 100


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def noisy_frame():
    """First half: steady noise at about -34 dBFS. Second half: voice
    bursts at about -6 dBFS between noise."""
    rng = np.random.default_rng(3)
    noise = 0.02 * (1 + 0.1 * rng.standard_normal(BUCKETS))
    peaks = noise.copy()
    voice = np.zeros(BUCKETS, bool)
    for start in range(BUCKETS // 2, BUCKETS, 60):
        voice[start:start + 25] = True
    peaks[voice] = 0.5
    extrema = np.stack([-peaks, peaks], axis=1).astype(np.float32)
    extrema.setflags(write=False)
    return WaveformFrame(extrema, 0.01), voice


class Reading:
    def __init__(self, frame):
        self.waveform = frame


def drawn_columns(widget, qt_app):
    """For each bucket's x position, the painted height in pixels."""
    widget.resize(BUCKETS + 16, widget.height())
    qt_app.processEvents()
    image = widget.grab().toImage()
    accent = QtGui.QColor(widget.palette().color(QtGui.QPalette.Highlight))
    from babelfishr.ui import theme

    accent = QtGui.QColor(theme.tokens(widget)["accent"])
    area_top, area_bottom = 22, widget.height() - 22
    heights = []
    for x in range(8, 8 + BUCKETS):
        count = sum(1 for y in range(area_top, area_bottom)
                    if QtGui.QColor(image.pixel(x, y)).rgb() == accent.rgb())
        heights.append(count)
    return np.array(heights)


def test_the_default_is_the_original_fixed_scale():
    peaks = np.array([1.0, 0.1, 0.01, 0.001, 0.0001])
    original = np.clip(1 + np.log10(np.maximum(peaks, 0.001)) / 3, 0, 1)
    assert np.allclose(display_magnitude(peaks), original)


def test_raising_the_floor_hides_the_noise_band_and_keeps_the_voice(qt_app):
    frame, voice = noisy_frame()
    widget = LiveWaveform()
    widget.show()
    widget.feed(Reading(frame))
    default = drawn_columns(widget, qt_app)
    noise_cols = default[: BUCKETS // 2]
    assert (noise_cols > 4).mean() > 0.9, "the noise should look like a solid bar by default"

    widget.set_display(floor_dbfs=-30.0)
    raised = drawn_columns(widget, qt_app)
    # The centre line is one pixel; columns beside a burst share its pen.
    near_voice = np.convolve(voice, np.ones(5), mode="same") > 0
    assert raised[~near_voice].max() <= 2, "the steady noise is still drawn"
    assert voice.sum() and raised[voice].mean() > 0.6 * default[voice].mean(), \
        "voice peaks were lost"
    assert "centre -30 dBFS" in widget.scale_text().replace("−", "-")


def test_gain_draws_quiet_input_larger_and_reset_restores(qt_app):
    quiet = np.array([0.01])                              # -40 dBFS
    assert display_magnitude(quiet, -60, 12) > display_magnitude(quiet, -60, 0)
    widget = LiveWaveform()
    widget.set_display(floor_dbfs=-24, gain_db=12)
    assert "gain +12 dB" in widget.scale_text()
    widget.reset_display_button.click()
    assert widget.is_default_display() and widget.floor_dbfs == DEFAULT_FLOOR_DBFS
    assert widget.floor_box.value() == -60 and widget.gain_box.value() == 0


def test_the_controls_are_labelled_and_change_the_scale(qt_app):
    widget = LiveWaveform()
    assert widget.display_button.accessibleName() == "Live waveform display scale"
    widget.floor_box.setValue(-36)
    widget.gain_box.setValue(6)
    assert (widget.floor_dbfs, widget.gain_db) == (-36, 6)
    widget.floor_box.setValue(-200)                       # clamped to its range
    assert widget.floor_dbfs == widget.floor_box.minimum()


def test_the_choice_is_remembered(qt_app, tmp_path):
    config = Config()
    config.source_path = str(tmp_path / "settings.toml")
    widget = LiveWaveform(display=config.display, save=config.save)
    widget.set_display(floor_dbfs=-33, gain_db=9)
    loaded = Config.load(config.source_path)
    assert (loaded.display.live_waveform_floor_dbfs, loaded.display.live_waveform_gain_db) \
        == (-33, 9)
    again = LiveWaveform(display=loaded.display)
    assert (again.floor_dbfs, again.gain_db) == (-33, 9)


def test_only_the_drawing_changes(qt_app, config, store, tmp_path):
    """The gate, the detector, the samples handed in and the meter stay put."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.audio.mic_threshold_dbfs = -30.0
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        before = (config.detector.to_settings(), config.audio.mic_threshold_dbfs,
                  window.app.capture_detector_settings())
        frame, _ = noisy_frame()
        original = frame.extrema.copy()
        window.live_waveform.feed(Reading(frame))
        window.live_waveform.set_display(floor_dbfs=-20, gain_db=18)
        window.live_waveform.grab()
        after = (config.detector.to_settings(), config.audio.mic_threshold_dbfs,
                 window.app.capture_detector_settings())
        assert before == after
        assert np.array_equal(frame.extrema, original), "the samples were altered"
    finally:
        window.close()
