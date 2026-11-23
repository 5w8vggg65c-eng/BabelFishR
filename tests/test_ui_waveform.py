"""RECORDING WAVEFORM panel: envelope streaming, async loading, states,
ruler and the playback marker. Synthetic audio only."""

from __future__ import annotations

import os
import threading
import time
import wave

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from babelfishr.audio.wavefile import write_wav  # noqa: E402
from babelfishr.testing import speech_like, tone  # noqa: E402
from babelfishr.ui import waveform  # noqa: E402
from babelfishr.ui.waveform import (RecordingWaveform,  # noqa: E402
                                    WaveformError, compute_envelope,
                                    format_duration, ruler_ticks)

SR = 48_000


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def widget(qt_app):
    w = RecordingWaveform()
    w.resize(640, w.HEIGHT)
    yield w
    w.wait_idle()
    w.deleteLater()


def _wav(tmp_path, name, samples, bits=16):
    return write_wav(str(tmp_path / name), samples, SR, bit_depth=bits)


# -- pure envelope -----------------------------------------------------------

def test_silence_stays_flat(tmp_path):
    env = compute_envelope(_wav(tmp_path, "s.wav", np.zeros(SR * 3)))
    assert env.mins.dtype == np.float32 and env.mins.size == 1200
    assert max(np.abs(env.mins).max(), np.abs(env.maxs).max()) < 1e-3
    assert env.display_gain == 1.0


def test_single_sample_transient_survives_bucketing(tmp_path):
    data = np.zeros(SR * 10)
    data[SR * 7 + 123] = 0.8
    env = compute_envelope(_wav(tmp_path, "click.wav", data))
    assert env.maxs.max() >= 0.9 * 0.8
    # Negative transient preserved as a signed minimum too.
    data[SR * 7 + 123] = -0.8
    env = compute_envelope(_wav(tmp_path, "click2.wav", data))
    assert env.mins.min() <= -0.9 * 0.8


def test_short_clip_and_duration(tmp_path):
    env = compute_envelope(_wav(tmp_path, "short.wav", tone(200 / SR)))
    assert env.frames == 200 and env.mins.size == 200
    assert env.duration_s == pytest.approx(200 / SR)
    env = compute_envelope(_wav(tmp_path, "sp.wav", speech_like(2.5)))
    assert env.frames == int(2.5 * SR) and env.sample_rate == SR
    assert env.duration_s == pytest.approx(2.5)
    assert env.sample_width == 2 and env.channels == 1


def test_24bit_and_stereo(tmp_path):
    data = np.zeros(SR)
    data[1000] = 0.5
    env = compute_envelope(_wav(tmp_path, "24.wav", data, bits=24))
    assert env.sample_width == 3
    assert env.maxs.max() == pytest.approx(0.5, abs=1e-3)

    # Stereo 24-bit written directly: spike only on the right channel.
    left = np.zeros(SR, dtype=np.int32)
    right = np.zeros(SR, dtype=np.int32)
    right[30_000] = int(-0.7 * 8388607)
    inter = np.stack([left, right], axis=1).ravel().astype("<i4")
    raw = inter.view("<u1").reshape(-1, 4)[:, :3].tobytes()
    path = str(tmp_path / "stereo.wav")
    with wave.open(path, "wb") as h:
        h.setnchannels(2)
        h.setsampwidth(3)
        h.setframerate(SR)
        h.writeframes(raw)
    env = compute_envelope(path)
    assert env.channels == 2 and env.frames == SR
    assert env.mins.min() == pytest.approx(-0.7, abs=1e-3)


def test_streams_in_bounded_chunks(tmp_path, monkeypatch):
    path = _wav(tmp_path, "long.wav", speech_like(6.0))
    calls = []
    real = wave.Wave_read.readframes

    def spy(self, n):
        calls.append(n)
        return real(self, n)

    monkeypatch.setattr(wave.Wave_read, "readframes", spy)
    env = compute_envelope(path, chunk_frames=16_384)
    assert len(calls) > 1
    assert max(calls) <= 16_384 < env.frames
    assert env.analysed_frames == env.frames


def test_max_frames_cap(tmp_path):
    env = compute_envelope(_wav(tmp_path, "c.wav", np.zeros(SR * 4)),
                           max_frames=SR)
    assert env.frames == SR * 4 and env.analysed_frames == SR
    assert env.truncated and env.duration_s == pytest.approx(4.0)


def test_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        compute_envelope(str(tmp_path / "nope.wav"))
    bad = tmp_path / "bad.wav"
    bad.write_bytes(np.random.default_rng(3).bytes(4096))
    with pytest.raises(WaveformError):
        compute_envelope(str(bad))
    env = compute_envelope(_wav(tmp_path, "empty.wav", np.zeros(0)))
    assert env.frames == 0 and env.empty


def test_quiet_audio_gain_never_inflates_hiss(tmp_path):
    hiss = np.random.default_rng(0).normal(0, 10 ** (-70 / 20), SR)
    env = compute_envelope(_wav(tmp_path, "hiss.wav", hiss))
    assert env.display_gain == 1.0


# -- widget states -----------------------------------------------------------

def test_initial_and_no_recording(widget):
    assert widget.objectName() == "recordingWaveform"
    assert widget.accessibleName() == "Recording waveform"
    assert widget.state == "empty" and widget.envelope is None
    widget.show_recording("tx1", None)
    assert widget.state == "none"
    assert widget.message == "This message has no recording."
    assert not widget.grab().isNull()


def test_ready_state_title_and_accessibility(widget, tmp_path):
    path = _wav(tmp_path, "clip_a.wav", speech_like(12.4))
    widget.show_recording("tx1", path)
    assert widget.state == "loading" and widget.message.startswith("Loading")
    assert widget.wait_until_loaded(5)
    assert widget.state == "ready" and widget.current_tx_id == "tx1"
    assert widget.envelope.frames == int(12.4 * SR)
    title = widget.title_text()
    assert title.startswith("clip_a.wav · 00:12")
    # Drawn louder than recorded? The title says so (reviewer finding 5).
    gain = widget.envelope.display_gain
    assert ("shown \u00d7" in title) == (gain > 1.05), (title, gain)
    assert "clip_a.wav" in widget.accessibleDescription()
    assert "00:12" in widget.accessibleDescription()


def test_missing_corrupt_and_empty(widget, tmp_path):
    widget.show_recording("tx1", str(tmp_path / "gone.wav"))
    widget.wait_until_loaded(5)
    assert widget.state == "missing"
    assert widget.message == "Recording file missing: gone.wav"
    assert widget.envelope is None

    bad = tmp_path / "bad.wav"
    bad.write_bytes(np.random.default_rng(7).bytes(2048))
    widget.show_recording("tx2", str(bad))
    widget.wait_until_loaded(5)
    assert widget.state == "error"
    assert widget.message.startswith("Recording could not be read: ")
    assert widget.envelope is None

    widget.show_recording("tx3", _wav(tmp_path, "e.wav", np.zeros(0)))
    widget.wait_until_loaded(5)
    assert widget.state == "clip_empty"
    assert widget.message == "Recording is empty (0 s)"
    assert widget.envelope is None
    assert not widget.grab().isNull()

    widget.clear()
    assert widget.state == "empty" and widget.current_tx_id is None


class _SlowCompute:
    """A stand-in for compute_envelope that reads in many small "chunks",
    honours cancellation at each one, and records what really ran."""

    def __init__(self, chunks=400, delay=0.005):
        self.real = waveform.compute_envelope
        self.chunks, self.delay = chunks, delay
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.started: list = []
        self.read: dict = {}
        self.finished: list = []
        self.first_started = threading.Event()

    def __call__(self, path, *a, cancelled=None, **k):
        name = os.path.basename(path)
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.started.append(name)
        self.first_started.set()
        try:
            if name.startswith("slow"):
                for n in range(self.chunks):
                    if cancelled is not None and cancelled():
                        self.read[name] = n
                        raise waveform.WaveformCancelled(path)
                    time.sleep(self.delay)
                self.read[name] = self.chunks
            env = self.real(path, *a, cancelled=cancelled, **k)
            self.finished.append(name)
            return env
        finally:
            with self.lock:
                self.running -= 1


def test_rapid_selection_cancels_obsolete_reads_and_the_latest_wins(
        widget, tmp_path, monkeypatch):
    """Holding Down through a long Session: the read in progress stops at its
    next chunk, the requests in between are never started, at most one read
    runs at a time, and the last selection is the one shown."""
    fake = _SlowCompute()
    monkeypatch.setattr(waveform, "compute_envelope", fake)
    paths = {n: _wav(tmp_path, f"{n}.wav", speech_like(0.5))
             for n in ("slow_a", "slow_b", "slow_c")}
    last = _wav(tmp_path, "last.wav", speech_like(2.0))

    widget.show_recording("A", paths["slow_a"])
    assert fake.first_started.wait(5)
    widget.show_recording("B", paths["slow_b"])
    widget.show_recording("C", paths["slow_c"])
    widget.show_recording("D", last)
    assert widget.wait_until_loaded(10)

    assert widget.current_tx_id == "D" and widget.state == "ready"
    assert widget.envelope.frames == 2 * SR
    assert fake.max_running == 1, "more than one read ran at once"
    # The first and the last ran; anything in between was either never
    # started or stopped early - none of it ran to the end.
    assert fake.started[0] == "slow_a.wav" and fake.started[-1] == "last.wav", fake.started
    assert len(fake.started) <= 3, fake.started
    for name in fake.started[:-1]:
        assert fake.read[name] < fake.chunks, f"the obsolete read {name} ran to the end"
        assert name not in fake.finished


def test_clear_stops_the_read_in_progress(widget, tmp_path, monkeypatch):
    fake = _SlowCompute()
    monkeypatch.setattr(waveform, "compute_envelope", fake)
    widget.show_recording("A", _wav(tmp_path, "slow_a.wav", speech_like(0.5)))
    assert fake.first_started.wait(5)
    widget.clear()
    widget.wait_idle(5)
    assert fake.read["slow_a.wav"] < fake.chunks
    assert fake.finished == []
    assert widget.state == "empty" and widget.envelope is None


def test_shutdown_and_deletion_end_the_worker(qt_app, tmp_path, monkeypatch):
    fake = _SlowCompute()
    monkeypatch.setattr(waveform, "compute_envelope", fake)

    first = RecordingWaveform()
    first.show_recording("A", _wav(tmp_path, "slow_a.wav", speech_like(0.5)))
    assert fake.first_started.wait(5)
    first.shutdown()                       # returns at once; nothing waits
    assert first._loader.join(2.0), "the worker did not leave after shutdown"
    assert fake.read["slow_a.wav"] < fake.chunks

    fake.first_started.clear()
    second = RecordingWaveform()
    loader = second._loader
    second.show_recording("B", _wav(tmp_path, "slow_b.wav", speech_like(0.5)))
    assert fake.first_started.wait(5)
    second.deleteLater()                   # a widget destroyed without shutdown()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    qt_app.processEvents()
    assert loader.join(3.0), "the worker outlived its widget"
    assert fake.read["slow_b.wav"] < fake.chunks
    # A request after shutdown is refused visibly, never stuck "loading".
    first.show_recording("late", _wav(tmp_path, "late.wav", speech_like(0.2)))
    assert first.state == "error"
    first.deleteLater()


def test_a_collected_widget_stops_its_worker_without_crashing(qt_app, tmp_path,
                                                             monkeypatch):
    """Dropped and garbage-collected mid-read, no shutdown(), no deleteLater:
    the read stops at its next chunk and the idle worker leaves - nothing is
    called on the half-collected widget."""
    import gc

    fake = _SlowCompute()
    monkeypatch.setattr(waveform, "compute_envelope", fake)

    def make():
        w = RecordingWaveform()
        w.show_recording("A", _wav(tmp_path, "slow_gc.wav", speech_like(0.5)))
        return w._loader, w

    loader, w = make()
    assert fake.first_started.wait(5)
    del w
    gc.collect()
    qt_app.processEvents()
    assert loader.join(4.0), "the worker outlived a collected widget"
    assert fake.read["slow_gc.wav"] < fake.chunks


def test_selecting_a_message_without_audio_cancels_the_read(widget, tmp_path,
                                                            monkeypatch):
    fake = _SlowCompute()
    monkeypatch.setattr(waveform, "compute_envelope", fake)
    widget.show_recording("A", _wav(tmp_path, "slow_a.wav", speech_like(0.5)))
    assert fake.first_started.wait(5)
    widget.show_recording("B", None)                      # no recording
    widget.wait_idle(5)
    assert fake.read["slow_a.wav"] < fake.chunks
    assert widget.state == "none"
    widget.show_recording("C", str(tmp_path / "gone.wav"))  # missing file
    assert widget.state == "missing"


def test_the_real_reader_stops_at_a_chunk_boundary(tmp_path, monkeypatch):
    path = _wav(tmp_path, "long.wav", speech_like(10.0))
    reads = []
    real_open = wave.open

    def spying_open(*a, **k):
        handle = real_open(*a, **k)
        original = handle.readframes
        handle.readframes = lambda n: reads.append(n) or original(n)
        return handle

    monkeypatch.setattr(wave, "open", spying_open)
    asked = []

    def cancelled():
        asked.append(1)
        return len(asked) > 3

    with pytest.raises(waveform.WaveformCancelled):
        compute_envelope(path, chunk_frames=4096, cancelled=cancelled)
    assert len(reads) == 3, "reading went past the cancellation"


def test_chunk_bytes_are_bounded_whatever_the_channel_count(tmp_path, monkeypatch):
    """65,536 frames is fine for mono; for 2,000 channels of 32-bit it would
    be half a gigabyte per read. The read size is bounded in bytes."""
    path = tmp_path / "many.wav"
    channels, frames = 2000, 600
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(4)
        handle.setframerate(SR)
        handle.writeframes(bytes(channels * 4 * frames))
    reads = []
    real_open = wave.open

    def spying_open(*a, **k):
        handle = real_open(*a, **k)
        original = handle.readframes
        handle.readframes = lambda n: reads.append(n) or original(n)
        return handle

    monkeypatch.setattr(wave, "open", spying_open)
    env = compute_envelope(str(path))
    assert env.frames == frames and env.channels == channels
    assert max(reads) * channels * 4 <= waveform.MAX_CHUNK_BYTES
    assert len(reads) >= 2


def test_resize_does_not_reread_file(widget, tmp_path, monkeypatch):
    path = _wav(tmp_path, "r.wav", speech_like(3.0))
    calls = []
    real = waveform.compute_envelope

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(waveform, "compute_envelope", counting)
    widget.show_recording("tx", path)
    widget.wait_until_loaded(5)
    opened = []
    real_open = wave.open
    monkeypatch.setattr(wave, "open",
                        lambda *a, **k: opened.append(a) or real_open(*a, **k))
    img1 = widget.grab()
    widget.resize(300, widget.HEIGHT)
    img2 = widget.grab()
    assert not img1.isNull() and not img2.isNull()
    assert img1.width() != img2.width()
    assert len(calls) == 1 and opened == []


def test_ruler_is_clip_relative(widget, tmp_path):
    path = _wav(tmp_path, "rul.wav", speech_like(37.0, seed=2))
    widget.show_recording("tx", path)
    widget.wait_until_loaded(5)
    labels = widget.ruler_labels()
    assert labels[0] == (0.0, "00:00")
    assert labels[-1][1] == format_duration(37.0) == "00:37"
    assert 3 <= len(labels) <= 8
    times = [t for t, _ in labels]
    assert times == sorted(times)
    # Short clip uses SS.s labels, still from zero to its own length.
    short = ruler_ticks(4.2, 600)
    assert short[0][1] == "00.0" and short[-1][1] == "04.2"
    # Narrow width yields fewer labels.
    assert len(ruler_ticks(37.0, 150)) < len(ruler_ticks(37.0, 600))


# -- playback marker ---------------------------------------------------------

class FakeController(QtCore.QObject):
    changed = QtCore.Signal(object)
    positionChanged = QtCore.Signal(str, int, int)

    def __init__(self):
        super().__init__()
        self.owner = None
        self.state = "stopped"
        self.pos = (0, 0)
        #: The file playing, as PlaybackController.path.
        self.path = None

    def state_for(self, tx_id):
        return self.state if tx_id == self.owner else "stopped"

    def position_for(self, tx_id):
        return self.pos if tx_id == self.owner else (0, 0)

    def set(self, owner, state, pos=0, dur=0):
        previous = self.owner
        self.owner, self.state, self.pos = owner, state, (pos, dur)
        self.changed.emit(frozenset(x for x in (previous, owner) if x))


def test_playback_marker_follows_selected_clip_only(widget, tmp_path):
    path = _wav(tmp_path, "m.wav", speech_like(10.0))
    widget.show_recording("sel", path)
    widget.wait_until_loaded(5)
    ctl = FakeController()
    ctl.path = path
    widget.set_playback(ctl)
    assert not widget.marker_visible

    ctl.set("sel", "playing", 2500, 10_000)
    assert widget.marker_visible and widget.marker_ms == 2500
    ctl.positionChanged.emit("sel", 5000, 0)  # backend duration unknown
    assert widget.marker_ms == 5000
    assert widget.marker_fraction() == pytest.approx(0.5)
    assert not widget.grab().isNull()

    ctl.set("sel", "paused", 5000, 10_000)
    assert widget.marker_visible

    ctl.set("other", "playing", 100, 3000)
    assert not widget.marker_visible
    ctl.positionChanged.emit("other", 200, 3000)
    assert not widget.marker_visible

    ctl.set("sel", "playing", 1000, 10_000)
    assert widget.marker_visible
    ctl.set("sel", "stopped")
    assert not widget.marker_visible

    # Detaching disconnects: later signals no longer reach the widget.
    widget.set_playback(None)
    ctl.set("sel", "playing", 1000, 10_000)
    assert not widget.marker_visible

    # Selecting a clip that already owns playback shows the marker at once.
    widget.set_playback(ctl)
    widget.show_recording("sel", path)
    widget.wait_until_loaded(5)
    assert widget.marker_visible
    widget.show_recording("x", path)
    assert not widget.marker_visible


def test_the_marker_needs_the_same_file_not_just_the_same_message(widget, tmp_path):
    """A message's decoded audio shares its id; the ORIGINAL's waveform must
    not show a playhead for it."""
    original = _wav(tmp_path, "orig.wav", speech_like(4.0))
    decoded = _wav(tmp_path, "decoded.wav", speech_like(4.0))
    widget.show_recording("sel", original)
    widget.wait_until_loaded(5)
    ctl = FakeController()
    widget.set_playback(ctl)
    ctl.path = decoded
    ctl.set("sel", "playing", 1000, 4000)
    ctl.positionChanged.emit("sel", 1000, 4000)
    assert not widget.marker_visible
    ctl.path = original
    ctl.set("sel", "playing", 1500, 4000)
    ctl.positionChanged.emit("sel", 1500, 4000)
    assert widget.marker_visible
