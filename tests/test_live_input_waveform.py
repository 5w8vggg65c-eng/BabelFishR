"""Live monitoring displays the input signal, including sub-gate audio."""

import os
import threading

import numpy as np
import pytest

from babelfishr.audio.envelope import BUCKETS_PER_SECOND, WINDOW_SECONDS, RollingEnvelope
from babelfishr.audio.meter import LevelReading
from babelfishr.audio.source import CallbackAudioSource


@pytest.mark.parametrize("rate", [11_025, 44_100, 48_000])
def test_envelope_keeps_signed_extrema_across_arbitrary_block_boundaries(rate):
    width = round(rate / BUCKETS_PER_SECOND)
    audio = np.random.default_rng(8).normal(0, 0.12, width * 12 + 1)
    envelope = RollingEnvelope(rate)
    envelope.feed(audio[:width - 1])
    assert envelope.frame() is None
    envelope.feed(audio[width - 1:width + 3])
    first = envelope.frame()
    envelope.feed(audio[width + 3:])
    frame = envelope.frame()
    buckets = audio[:-1].reshape(-1, width)
    expected = np.column_stack((buckets.min(axis=1), buckets.max(axis=1)))
    np.testing.assert_allclose(frame.extrema, expected)
    np.testing.assert_allclose(first.extrema, expected[:1])
    assert not frame.extrema.flags.writeable
    assert frame.seconds_per_bucket == width / rate


def test_long_input_block_has_only_the_latest_window_with_the_correct_alignment():
    rate = 16_000
    width = rate // BUCKETS_PER_SECOND
    audio = np.random.default_rng(2).uniform(-0.75, 1, rate * 18 + 13)
    envelope = RollingEnvelope(rate)
    envelope.feed(audio[:3])
    envelope.feed(audio[3:])
    frame = envelope.frame()
    trailing = audio[-(WINDOW_SECONDS * rate + 13):-13].reshape(-1, width)
    np.testing.assert_allclose(frame.extrema[:, 0], trailing.min(axis=1))
    np.testing.assert_allclose(frame.extrema[:, 1], trailing.max(axis=1))
    assert frame.extrema.nbytes == WINDOW_SECONDS * BUCKETS_PER_SECOND * 2 * 4
    envelope.feed(np.zeros((WINDOW_SECONDS + 1) * rate))
    assert np.all(envelope.frame().extrema == 0)


def test_live_level_event_includes_actual_audio_below_the_trigger(app):
    rate = 48_000
    source = CallbackAudioSource(rate)
    received = threading.Event()
    frames = []

    def hear(event):
        if event.kind == "level" and event.payload.waveform is not None:
            frames.append(event.payload.waveform)
            received.set()

    app.config.detector.mode = "fixed"
    app.config.detector.threshold_dbfs = -20
    app.events.subscribe(hear)
    app.start_session(source=source)
    app.begin_capture()
    try:
        samples = np.zeros(2048)
        samples[8] = -0.008
        samples[39] = 0.004
        for _ in range(3):
            source.push(samples)
        assert received.wait(5)
        assert frames[0].extrema.min() == pytest.approx(-0.008)
        assert frames[0].extrema.max() == pytest.approx(0.004)
        assert app.capture.transmissions_captured == 0
    finally:
        app.stop_session()


def test_widget_draws_a_snapshot_then_clears_the_old_input():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtGui, QtWidgets

    from babelfishr.ui import theme
    from babelfishr.ui.live_waveform import LiveWaveform

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = LiveWaveform()
    widget.resize(616, widget.height())
    rate = 16_000
    samples = np.zeros(rate * WINDOW_SECONDS)
    samples[:2 * rate:2] = 0.25
    samples[2 * rate:4 * rate:2] = -0.25
    envelope = RollingEnvelope(rate)
    envelope.feed(samples)
    frame = envelope.frame()
    try:
        assert widget.frame is None
        widget.feed(LevelReading(-20, -18, -18, False, 0, waveform=frame))
        assert widget.frame is frame
        image = widget.grab().toImage()
        accent = QtGui.QColor(theme.tokens(widget)["accent"])

        def lit(x, y):
            scale = image.devicePixelRatio()
            return image.pixelColor(round(x * scale), round(y * scale)) == accent

        # Positive, negative, and silent sections remain visibly distinct.
        assert lit(110, 49) and not lit(110, 75)
        assert lit(350, 75) and not lit(350, 49)
        assert not lit(560, 49) and not lit(560, 75)
        widget.reset()
        assert widget.frame is None
        assert not widget.grab().isNull()
    finally:
        widget.close()
        app.processEvents()
