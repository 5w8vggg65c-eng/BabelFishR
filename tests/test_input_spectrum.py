"""The visible INPUT meter is a live audio-frequency spectrum.

Frequency across, magnitude up, computed from the actual PCM at its actual
sample rate - on the capture thread, never the PortAudio callback - and
cleared to flat on silence, Stop and a change of input. Generated tones and
silence only: no audio devices, no recordings.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np
import pytest

from babelfishr.audio.spectrum import (FLOOR_DBFS, F_MAX_HZ, FFT_SIZE,
                                       SpectrumAnalyzer, analyse, band_edges)
from babelfishr.testing import tone

RATES = (16_000, 44_100, 48_000)
FREQUENCIES = (300.0, 1000.0, 3000.0, 6000.0)
LEVELS = (-6.0, -20.0, -40.0)


def _band_of(edges, hz):
    for index, (low, high) in enumerate(zip(edges[:-1], edges[1:])):
        if low <= hz <= high:
            return index
    raise AssertionError(f"{hz} Hz is outside the axis {edges[0]}..{edges[-1]}")


# --- the pure analysis ----------------------------------------------------------

@pytest.mark.parametrize("rate", RATES)
def test_band_edges_respect_nyquist(rate):
    edges = band_edges(rate)
    assert all(b > a for a, b in zip(edges[:-1], edges[1:]))
    assert edges[0] == pytest.approx(60.0)
    assert edges[-1] == pytest.approx(min(F_MAX_HZ, rate / 2))
    assert edges[-1] <= rate / 2


def test_a_low_rate_ends_the_axis_at_its_nyquist():
    assert band_edges(8_000)[-1] == pytest.approx(4_000)
    frame = analyse(tone(0.1, 3_500.0, 8_000, -6.0), 8_000)
    assert frame.edges_hz[-1] == pytest.approx(4_000)
    assert abs(frame.peak_band() - _band_of(frame.edges_hz, 3_500.0)) <= 1


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("level", LEVELS)
def test_the_peak_band_follows_the_tone(rate, level):
    peaks = []
    for hz in FREQUENCIES:
        frame = analyse(tone(0.2, hz, rate, level), rate)
        assert frame.sample_rate == rate
        expected = _band_of(frame.edges_hz, hz)
        # The loudest band is the tone's own (or its neighbour, for a tone
        # on a band edge), and reads its level within a few dB.
        assert abs(frame.peak_band() - expected) <= 1, (rate, hz, level)
        assert frame.levels_dbfs[frame.peak_band()] == pytest.approx(level, abs=2.0)
        # Far from the tone, the spectrum is near the floor.
        far = [v for i, v in enumerate(frame.levels_dbfs) if abs(i - expected) > 8]
        assert max(far) < level - 40, (rate, hz, level)
        peaks.append(frame.peak_band())
    assert peaks == sorted(peaks) and len(set(peaks)) == len(peaks), peaks


@pytest.mark.parametrize("rate", RATES)
def test_height_follows_amplitude(rate):
    for hz in FREQUENCIES:
        heights = []
        for level in LEVELS:
            frame = analyse(tone(0.2, hz, rate, level), rate)
            heights.append(frame.fractions()[frame.peak_band()])
        assert heights[0] > heights[1] > heights[2] > 0, (rate, hz, heights)
        # 20 dB of level is 20 dB of bar, on the -90..0 scale.
        assert heights[0] - heights[1] == pytest.approx(14 / 90, abs=0.03)


@pytest.mark.parametrize("rate", RATES)
def test_silence_is_a_flat_floor(rate):
    frame = analyse(np.zeros(rate // 5), rate)
    assert frame.silent
    assert set(frame.levels_dbfs) == {FLOOR_DBFS}
    assert not any(frame.fractions())
    # Idle hiss: low everywhere, no band standing out.
    hiss = np.random.default_rng(1).normal(0.0, 10 ** (-70 / 20), rate // 5)
    levels = analyse(hiss, rate).levels_dbfs
    assert max(levels) < -70


def test_the_analyzer_keeps_bounded_state_and_decays_to_flat():
    rate = 48_000
    analyzer = SpectrumAnalyzer()
    ring = analyzer._ring
    assert analyzer.frame() is None                 # nothing heard yet
    burst = tone(0.5, 1000.0, rate, -6.0)
    for i in range(0, burst.size, 1024):
        analyzer.feed(burst[i:i + 1024], rate)
    lit = analyzer.frame()
    assert lit.levels_dbfs[lit.peak_band()] > -8
    # Silence afterwards: the bars fall (not instantly - light smoothing),
    # and within a couple of seconds the spectrum is flat again.
    quiet = np.zeros(1024)
    frames = []
    for _ in range(int(2.5 * rate / 1024)):
        analyzer.feed(quiet, rate)
        frames.append(analyzer.frame())
    assert frames[0].levels_dbfs[lit.peak_band()] > FLOOR_DBFS
    assert frames[-1].silent and max(frames[-1].peaks_dbfs) == FLOOR_DBFS
    # Fixed memory: the same ring, never longer than one FFT.
    assert analyzer._ring is ring and ring.size == FFT_SIZE
    assert analyzer._levels.size == analyzer.bands


def test_a_new_sample_rate_starts_afresh():
    analyzer = SpectrumAnalyzer()
    analyzer.feed(tone(0.2, 1000.0, 48_000, -6.0), 48_000)
    assert not analyzer.frame().silent
    analyzer.feed(np.zeros(4096), 16_000)
    frame = analyzer.frame()
    assert frame.sample_rate == 16_000
    assert frame.edges_hz[-1] == pytest.approx(8_000)
    assert frame.silent, "the previous input's spectrum must not carry over"


# --- the widget -----------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _frame(hz, level, rate=48_000):
    analyzer = SpectrumAnalyzer()
    analyzer.feed(tone(0.1, hz, rate, level), rate)
    return analyzer.frame()


def test_the_widget_draws_what_it_is_fed(qt_app):
    from babelfishr.ui.widgets import SpectrumWidget

    widget = SpectrumWidget()
    widget.resize(260, 64)
    assert widget.flat
    widget.set_reading(0.5, 0.7, False, 0, spectrum=_frame(300.0, -6.0))
    low = widget.bar_fractions()
    widget.set_reading(0.5, 0.7, False, 0, spectrum=_frame(3000.0, -6.0))
    high = widget.bar_fractions()
    assert int(np.argmax(high)) > int(np.argmax(low))
    widget.set_reading(0.3, 0.4, False, 0, spectrum=_frame(3000.0, -40.0))
    assert max(widget.bar_fractions()) < max(high)
    assert widget.sample_rate == 48_000
    assert not widget.grab().isNull()
    # Gain information is kept: clipping and its count.
    widget.set_reading(0.99, 1.0, True, 12, spectrum=_frame(1000.0, -1.0))
    assert widget._clipped and widget._clip_count == 12
    assert not widget.grab().isNull()
    widget.reset()
    assert widget.flat and widget.sample_rate == 0 and not widget._clipped


def test_the_widget_falls_flat_when_readings_stop(qt_app):
    from PySide6 import QtCore

    from babelfishr.ui.widgets import SpectrumWidget

    widget = SpectrumWidget()
    widget._stale.setInterval(30)
    widget.set_reading(0.5, 0.7, False, 0, spectrum=_frame(1000.0, -6.0))
    assert not widget.flat
    deadline = time.monotonic() + 2.0
    while not widget.flat and time.monotonic() < deadline:
        qt_app.processEvents(QtCore.QEventLoop.AllEvents, 20)
        time.sleep(0.01)
    assert widget.flat, "a stalled input must not leave a frozen spectrum"


@pytest.mark.parametrize("dark", [True, False])
def test_the_orange_is_legible_in_both_appearances(dark):
    from babelfishr.ui.theme import CONSOLE, DAYLIGHT

    t = CONSOLE if dark else DAYLIGHT

    def luminance(colour):
        rgb = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    def contrast(a, b):
        a, b = luminance(a), luminance(b)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    for ground in (t["surface"], t["window"]):
        assert contrast(t["spectrum"], ground) >= 4.5
        assert contrast(t["spectrum_peak"], ground) >= 4.5
    red, green, blue = (int(t["spectrum"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    assert red > green > blue, "the spectrum is orange"


def test_the_input_panel_shows_the_spectrum_and_clears_it(qt_app, app):
    from babelfishr.ui.input_panel import InputPanel
    from babelfishr.ui.widgets import SpectrumWidget

    panel = InputPanel(app)
    assert isinstance(panel.meter, SpectrumWidget)
    panel.set_reading(0.5, 0.6, False, 0, spectrum=_frame(1000.0, -12.0))
    assert not panel.meter.flat
    panel.set_monitoring(True)                       # Start: a fresh meter
    assert panel.meter.flat
    panel.set_reading(0.5, 0.6, False, 0, spectrum=_frame(1000.0, -12.0))
    panel.set_monitoring(False)                      # Stop
    assert panel.meter.flat
    panel.set_reading(0.5, 0.6, False, 0, spectrum=_frame(1000.0, -12.0))
    panel.device_box.setCurrentIndex(panel.device_box.count() - 1)
    panel._on_choice()                               # a different input
    assert panel.meter.flat
    panel.set_reading(0.5, 0.6, False, 0, spectrum=_frame(1000.0, -12.0))
    panel.report_audio_status("disconnected", "gone")
    assert panel.meter.flat
    # Without a spectrum (an older producer) the level still shows.
    panel.set_reading(0.5, 0.6, True, 2)
    assert panel.meter._clip_count == 2 and panel.meter.flat


def test_the_window_forwards_the_spectrum(qt_app, app):
    from babelfishr.audio.meter import LevelReading
    from babelfishr.audio.source import CallbackAudioSource
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(app)
    try:
        session = app.start_session(source=CallbackAudioSource(48_000),
                                    name="spectrum", target_language="en")
        reading = LevelReading(-20.0, -10.0, -10.0, False, 0,
                               spectrum=_frame(3000.0, -10.0))
        app.events.publish("level", reading, run=session.id)
        window._drain_events()
        meter = window.input_panel.meter
        assert not meter.flat
        assert meter.edges_hz == reading.spectrum.edges_hz
    finally:
        app.stop_session()
        window.close()


# --- capture: off the callback, bounded --------------------------------------

def test_capture_publishes_spectrum_frames_off_the_callback_thread(app):
    from babelfishr.audio.source import CallbackAudioSource

    rate = 44_100
    source = CallbackAudioSource(rate, name="spectrum-test")
    frames, threads = [], set()
    pusher = threading.current_thread()

    def listen(event):
        if event.kind == "level" and event.payload.spectrum is not None:
            frames.append(event.payload.spectrum)
            threads.add(threading.current_thread())

    app.events.subscribe(listen)
    session = app.start_session(source=source, name="spec", target_language="en")
    app.begin_capture()
    capture = app.capture
    try:
        signal = np.concatenate([tone(1.0, 300.0, rate, -12.0),
                                 tone(1.0, 3000.0, rate, -12.0),
                                 np.zeros(3 * rate)])
        block = 1024
        for i in range(0, signal.size, block):
            source.push(signal[i:i + block])       # the "callback" just queues
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (
                not frames or not frames[-1].silent):
            time.sleep(0.02)
        assert frames and frames[-1].silent, "silence must clear the spectrum"
    finally:
        app.stop_session()

    assert pusher not in threads
    assert {t.name for t in threads} == {"babelfishr-capture"}
    assert all(f.sample_rate == rate for f in frames)
    assert frames[0].edges_hz[-1] == pytest.approx(min(F_MAX_HZ, rate / 2))
    peaks = [f.peak_band() for f in frames if not f.silent]
    edges = frames[0].edges_hz
    # The 300 Hz second, then the 3 kHz second, in that order.
    low = [i for i, p in enumerate(peaks) if abs(p - _band_of(edges, 300.0)) <= 1]
    high = [i for i, p in enumerate(peaks) if abs(p - _band_of(edges, 3000.0)) <= 1]
    assert low and high and low[0] < high[0]
    assert any(abs(p - _band_of(edges, 3000.0)) <= 1 for p in peaks)
    # About ten or more readings per second of audio (at the UI's own
    # refresh rate), never one per callback: responsive, and bounded.
    seconds = signal.size / rate
    # The callbacks actually made, the final partial block included (220500
    # samples in 1024s is 216 of them): at most one frame per two.
    blocks = -(-signal.size // block)
    assert 5 * seconds <= len(frames) <= blocks // 2
    # Fixed memory on the capture side: one FFT-sized ring, nothing growing.
    assert capture.spectrum._ring.size == FFT_SIZE
    assert not [name for name in vars(capture.spectrum)
                if isinstance(getattr(capture.spectrum, name), list)]
    assert sys.getsizeof(frames[-1].levels_dbfs) < 1024
