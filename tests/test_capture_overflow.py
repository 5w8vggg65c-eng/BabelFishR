"""Capture-buffer overflow is counted, located, recorded and reported.

The live input's device callback discards the oldest buffered block when the
capture thread falls behind (it must never block). That used to be silent: a
block counter nobody read. These tests drive the real LiveAudioSource callback
(only the physical stream and device lookup are stubbed) with a deliberately
slow consumer and a tiny queue, and check:

* the exact number of frames dropped, counted in the callback at the drop;
* each gap located in the stream and persisted with the run (session), and
  on the transmission whose audio spans it; totals survive a restart;
* drops just before Stop are not lost;
* the operator warning is rate limited (fake clock) and reaches the GUI
  status bar and the CLI ``listen`` output;
* surviving audio is exactly the non-dropped frames, in order, and every
  recording is on disk before it is processed;
* the callback itself does no file/store I/O, no logging, no status calls.
"""

from __future__ import annotations

import builtins
import datetime
import io
import logging
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.devices import AudioDevice
from babelfishr.audio.source import LiveAudioSource
from babelfishr.pipeline import EventBus
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.storage import Store
from babelfishr.testing import build_fixture

SR = 48_000


class _FakeStream:
    active = True

    def __init__(self):
        self.closed = threading.Event()

    def stop(self):
        pass

    def close(self):
        self.closed.set()


def _live_source(monkeypatch, block_size=960, max_queue_blocks=4):
    """The real LiveAudioSource - callback, queue, cutoff, stop - with only
    the physical stream and the device lookup stubbed."""
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))
    source = LiveAudioSource(device=None, sample_rate=SR, block_size=block_size,
                             reconnect=False, max_queue_blocks=max_queue_blocks)
    device = AudioDevice(index=0, name="stub input", max_input_channels=1,
                         default_sample_rate=SR)
    monkeypatch.setattr(source, "_resolve_device", lambda: device)
    stream = _FakeStream()
    monkeypatch.setattr(source, "_open_stream",
                        lambda: setattr(source, "_stream", stream))
    return source, stream


def _callback(source, samples, status=None):
    """One PortAudio callback delivering ``samples`` (float32, one channel)."""
    block = np.asarray(samples, dtype=np.float32).reshape(-1, 1)
    source._callback(block, block.shape[0], None, status)


def _numbered(start, size):
    """Samples that identify their own stream position, exact in float32."""
    return (np.arange(start, start + size) % 4096) / 8192.0 - 0.25


def _wait(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _with_sleep(sleep):
    """A stand-in for the time module whose sleep() is ``sleep``."""
    proxy = types.ModuleType("time")
    proxy.__dict__.update({k: getattr(time, k) for k in dir(time)
                           if not k.startswith("__")})
    proxy.sleep = sleep
    return proxy


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


# --- the source: counting at the drop, locating on the read ---------------------

def test_the_callback_counts_every_dropped_frame_and_the_read_places_the_gap(monkeypatch):
    source, _ = _live_source(monkeypatch, block_size=480, max_queue_blocks=4)
    source.start()
    sizes = [480, 300, 700, 480, 512, 480, 256, 480, 480, 100]
    position, delivered = 0, []
    for size in sizes:
        samples = _numbered(position, size)
        _callback(source, samples)
        delivered.append((position, samples))
        position += size
    # Counted in the callback, before anything has been read: the six
    # oldest blocks of these ten, frame for frame (not blocks x block size).
    assert source.dropped_blocks == 6
    assert source.dropped_frames == sum(sizes[:6])

    read = [source.read(timeout=0.0) for _ in range(4)]
    assert source.read(timeout=0.0) is None
    # Surviving audio is intact and in order: exactly the last four blocks.
    for block, (start, samples) in zip(read, delivered[6:]):
        assert block.start_frame == start
        assert np.array_equal(block.samples, samples.astype(np.float32))

    gaps = source.take_capture_gaps()
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.start_frame == 0 and gap.frames == sum(sizes[:6])
    assert gap.duration == pytest.approx(sum(sizes[:6]) / SR)
    assert gap.started_at == source._start_time
    assert source.take_capture_gaps(final=True) == []   # nothing unplaced
    source.stop()


def test_a_gap_mid_stream_is_placed_where_the_audio_is_missing(monkeypatch):
    source, _ = _live_source(monkeypatch, block_size=480, max_queue_blocks=4)
    source.start()
    for i in range(3):
        _callback(source, _numbered(i * 480, 480))
    assert [source.read(timeout=0.0).start_frame for _ in range(2)] == [0, 480]
    for i in range(3, 9):                   # 1 queued + 6 = 7 > 4: three dropped
        _callback(source, _numbered(i * 480, 480))
    starts = []
    while (block := source.read(timeout=0.0)) is not None:
        starts.append(block.start_frame)
    assert starts == [5 * 480, 6 * 480, 7 * 480, 8 * 480]
    assert source.dropped_frames == 3 * 480
    (gap,) = source.take_capture_gaps()
    assert (gap.start_frame, gap.frames) == (2 * 480, 3 * 480)
    assert gap.stream_offset == pytest.approx(960 / SR)
    source.stop()


def test_drops_nothing_after_revealed_are_still_accounted_at_the_end(monkeypatch):
    source, _ = _live_source(monkeypatch, block_size=480, max_queue_blocks=2)
    source.start()
    for i in range(5):
        _callback(source, _numbered(i * 480, 480))
    assert source.dropped_frames == 3 * 480
    # Nothing has been read, so nothing placed - but the final reckoning
    # returns the counted frames as one unplaced gap: none is lost.
    assert source.take_capture_gaps() == []
    (gap,) = source.take_capture_gaps(final=True)
    assert gap.start_frame is None and gap.frames == 3 * 480
    # Read afterwards: not counted twice.
    source.read(timeout=0.0)
    source.take_capture_gaps(final=True)
    assert source.take_capture_gaps(final=True) == []
    source.stop()


def test_a_new_run_starts_its_accounting_afresh(monkeypatch):
    source, _ = _live_source(monkeypatch, block_size=480, max_queue_blocks=2)
    source.start()
    for i in range(4):
        _callback(source, _numbered(i * 480, 480))
    assert source.dropped_frames == 2 * 480
    source.stop()
    source.start()
    assert (source.dropped_blocks, source.dropped_frames) == (0, 0)
    assert source.take_capture_gaps(final=True) == []
    source.stop()


def test_the_callback_does_no_io_logging_or_status_calls(monkeypatch, config):
    """Every blocking or outward-facing call raises while the callback runs;
    the callback still counts every drop, and the status it saw is reported
    afterwards by the reading thread."""
    source, _ = _live_source(monkeypatch, block_size=480, max_queue_blocks=2)
    statuses = []
    source.on_status = lambda kind, message: statuses.append((kind, message))
    source.start()
    statuses.clear()

    inside = threading.local()

    def forbid(name, real):
        def guard(*args, **kwargs):
            if getattr(inside, "active", False):
                raise AssertionError(f"{name} called from the audio callback")
            return real(*args, **kwargs)
        return guard

    monkeypatch.setattr(builtins, "open", forbid("open", builtins.open))
    monkeypatch.setattr(io, "open", forbid("io.open", io.open))
    monkeypatch.setattr(os, "write", forbid("os.write", os.write))
    monkeypatch.setattr(logging.Logger, "_log", forbid("logging", logging.Logger._log))
    monkeypatch.setattr(logging.Logger, "handle", forbid("logging", logging.Logger.handle))
    for name in ("add_capture_gap", "save_transmission", "save_session"):
        monkeypatch.setattr(Store, name, forbid(f"Store.{name}", getattr(Store, name)))
    monkeypatch.setattr(EventBus, "publish", forbid("EventBus.publish", EventBus.publish))
    monkeypatch.setattr(source, "on_status", forbid("on_status", source.on_status))
    monkeypatch.setattr(source, "_notify", forbid("_notify", source._notify))

    class Flags:
        input_overflow = True

        def __bool__(self):
            return True

        def __str__(self):
            return "input overflow"

    real_callback = source._callback

    def guarded_callback(*args):
        inside.active = True
        try:
            real_callback(*args)
        finally:
            inside.active = False

    monkeypatch.setattr(source, "_callback", guarded_callback)
    for i in range(6):
        _callback(source, _numbered(i * 480, 480), status=Flags() if i == 3 else None)
    assert source.dropped_blocks == 4 and source.dropped_frames == 4 * 480
    assert source.overflow_count == 1
    assert statuses == []                  # nothing reported from the callback

    source.read(timeout=0.0)               # the reading thread reports it
    assert statuses == [("stream-status", "input overflow")]
    assert source.take_capture_gaps()[0].frames == 4 * 480
    source.stop()


# --- the capture: recorded per run, on transmissions, warned, survives restart --

def _held_capture(monkeypatch, config, store, block_size=960, queue_blocks=4):
    source, stream = _live_source(monkeypatch, block_size=block_size,
                                  max_queue_blocks=queue_blocks)
    app = BabelFishRApp(config=config, store=store)
    app.transcription, app.translation = MockTranscriptionEngine(), MockTranslationEngine()
    events = []
    archived_at_event = []

    def on_event(event):
        if event.kind == "audio-status" and event.payload.get("kind") == "capture-overflow":
            events.append(event.payload)
        if event.kind == "transmission":
            tx = event.payload
            # Archive before transcription: the WAV and the row exist, and
            # nothing has been transcribed yet, when the capture announces it.
            stored = store.get_transmission(tx.id)
            archived_at_event.append(bool(
                tx.audio_path and os.path.exists(tx.audio_path)
                and stored is not None and not stored.transcript))

    app.events.subscribe(on_event)
    session = app.start_session(source=source, name="live", target_language="fr")
    capture = app.capture
    clock = _Clock()
    capture.clock = clock
    gate = {"armed": False}
    entered, release = threading.Event(), threading.Event()
    handled = []
    real_handle = capture._handle_block

    def slow_handle(block):
        if gate["armed"]:
            gate["armed"] = False
            entered.set()
            assert release.wait(20.0)
        handled.append(block.samples.copy())
        return real_handle(block)

    monkeypatch.setattr(capture, "_handle_block", slow_handle)
    return types.SimpleNamespace(
        app=app, source=source, stream=stream, session=session, capture=capture,
        clock=clock, gate=gate, entered=entered, release=release, handled=handled,
        events=events, archived=archived_at_event)


def _paced(h, samples, block):
    """Deliver at the pace the consumer keeps up with: no overflow."""
    for start in range(0, samples.size, block):
        _callback(h.source, samples[start:start + block])
        assert _wait(lambda: h.source._queue.qsize() == 0)


def test_a_slow_consumer_loses_exact_frames_recorded_with_the_run_and_transmission(
        monkeypatch, config, store):
    block = 960
    h = _held_capture(monkeypatch, config, store, block_size=block, queue_blocks=4)
    h.app.begin_capture()
    try:
        audio = np.concatenate([
            build_fixture([{"gap": 0.4}], sample_rate=SR).audio,
            build_fixture([{"kind": "voice", "duration": 3.0, "level_dbfs": -14}],
                          sample_rate=SR).audio,
            build_fixture([{"gap": 3.0}], sample_rate=SR).audio])
        audio = audio[:audio.size // block * block].astype(np.float32).astype(np.float64)
        blocks = [audio[i:i + block] for i in range(0, audio.size, block)]
        stall = 80                       # 1.6 s in: mid-voice

        _paced(h, np.concatenate(blocks[:stall]), block)
        h.gate["armed"] = True
        _callback(h.source, blocks[stall])           # the consumer stalls on it
        assert h.entered.wait(10.0)
        for b in blocks[stall + 1:stall + 10]:       # 4 queue, 5 are dropped
            _callback(h.source, b)
        assert h.source.dropped_blocks == 5
        assert h.source.dropped_frames == 5 * block
        h.release.set()
        assert _wait(lambda: h.source._queue.qsize() == 0)
        _paced(h, np.concatenate(blocks[stall + 10:]), block)
        h.app.stop_session()
        assert _wait(lambda: h.app.outstanding_work() == 0 and h.stream.closed.is_set())
        h.app.wait_for_session_ends(10.0)
    finally:
        h.release.set()

    lost = slice(stall + 1, stall + 6)
    gap_start = (stall + 1) * block
    # Surviving audio: exactly the non-dropped frames, in order.
    survivors = blocks[:lost.start] + blocks[lost.stop:]
    assert len(h.handled) == len(survivors)
    assert np.array_equal(np.concatenate(h.handled), np.concatenate(survivors))

    # Recorded with the run, in the store.
    (gap,) = store.capture_gaps(h.session.id)
    assert (gap.start_frame, gap.frames, gap.sample_rate) == (gap_start, 5 * block, SR)
    assert gap.duration == pytest.approx(0.1)
    expected_at = h.source._start_time + datetime.timedelta(seconds=gap_start / SR)
    assert abs((gap.started_at - expected_at).total_seconds()) < 0.001  # ms in the store
    assert h.capture.dropped_frames == h.source.dropped_frames == 5 * block

    # And on the transmission whose audio spans it.
    rows = store.list_transmissions(session_id=h.session.id)
    carrying = [t for t in rows if (t.signal_metadata or {}).get("capture_gaps")]
    assert len(carrying) == 1
    tx = carrying[0]
    assert tx.signal_metadata["capture_gaps"] == [{
        "start_frame": gap_start, "frames": 5 * block, "duration": 0.1,
        "sample_rate": SR, "started_at": gap.to_dict()["started_at"]}]
    assert tx.started_at <= gap.started_at <= tx.ended_at + datetime.timedelta(
        seconds=gap.duration)

    # Archive before transcription still holds, overflow or not.
    assert h.archived and all(h.archived)
    assert all(t.transcript for t in rows if t.auto_processed)

    # One warning, with the count.
    assert len(h.events) == 1
    assert h.events[0]["dropped_frames"] == 5 * block
    assert h.events[0]["session_id"] == h.session.id
    assert "0.100 s" in h.events[0]["message"]

    # The run carries it - and still does after a restart.
    loaded = store.get_session(h.session.id)
    assert loaded.dropped_frames == 5 * block
    assert loaded.to_dict()["capture_gaps"][0]["frames"] == 5 * block
    assert loaded.to_dict()["dropped_seconds"] == pytest.approx(0.1)
    h.app.close()
    store.close()
    reopened = Store(config.database, recordings_dir=config.recording.directory)
    try:
        again = reopened.get_session(h.session.id)
        assert again.dropped_frames == 5 * block
        assert [s.dropped_frames for s in reopened.list_sessions()
                if s.id == h.session.id] == [5 * block]
        assert reopened.capture_gaps(h.session.id)[0].start_frame == gap_start
    finally:
        reopened.close()


def test_drops_just_before_stop_are_recorded(monkeypatch, config, store):
    block = 960
    h = _held_capture(monkeypatch, config, store, block_size=block, queue_blocks=4)
    h.app.begin_capture()
    try:
        _paced(h, np.zeros(block * 5), block)
        h.gate["armed"] = True
        _callback(h.source, np.zeros(block))
        assert h.entered.wait(10.0)
        for _ in range(11):                      # 4 queue, 7 dropped
            _callback(h.source, np.zeros(block))
        h.app.stop_session()                     # Stop while the drops are queued
        _callback(h.source, np.zeros(block))     # after Stop: not this run's
        h.release.set()
        assert _wait(lambda: h.stream.closed.is_set())
        h.app.wait_for_session_ends(10.0)
    finally:
        h.release.set()
    assert h.source.dropped_frames == 7 * block
    gaps = store.capture_gaps(h.session.id)
    assert sum(g.frames for g in gaps) == 7 * block
    assert [g.start_frame for g in gaps] == [6 * block]
    # The last warning is not held back by the rate limit at the end.
    assert sum(e["dropped_frames"] for e in h.events) == 7 * block
    h.app.close()


def test_unplaced_drops_at_the_end_are_still_recorded(monkeypatch, config, store):
    """Even if the end of the run reads nothing more (the drain is skipped
    here), what the callback counted is recorded as an unplaced gap."""
    block = 960
    h = _held_capture(monkeypatch, config, store, block_size=block, queue_blocks=2)
    monkeypatch.setattr(h.capture, "_drain_source", lambda: None)
    h.app.begin_capture()
    try:
        h.gate["armed"] = True
        _callback(h.source, np.zeros(block))
        assert h.entered.wait(10.0)
        for _ in range(5):                        # 2 queue, 3 dropped
            _callback(h.source, np.zeros(block))
        h.capture._running = False                # the loop ends after this block
        h.app.stop_session()
        h.release.set()
        assert _wait(lambda: h.stream.closed.is_set())
        h.app.wait_for_session_ends(10.0)
    finally:
        h.release.set()
    gaps = store.capture_gaps(h.session.id)
    assert sum(g.frames for g in gaps) == h.source.dropped_frames == 3 * block
    assert gaps[-1].start_frame is None
    h.app.close()


def test_the_warning_is_rate_limited_and_accumulates(monkeypatch, config, store):
    block = 480
    h = _held_capture(monkeypatch, config, store, block_size=block, queue_blocks=4)
    capture, source, clock = h.capture, h.source, h.clock
    assert capture.overflow_warning_interval == 10.0
    source.start()                     # consumer driven by hand: the real drain
    position = 0

    def burst(at, dropped):
        nonlocal position
        clock.now = at
        for _ in range(4 + dropped):
            _callback(source, np.zeros(block))
        position += 4 + dropped
        capture._drain_source()

    burst(1000.0, 2)                   # first overflow: warned at once
    burst(1001.0, 1)
    burst(1005.0, 3)                   # inside the interval: accumulated
    assert len(h.events) == 1
    assert h.events[0]["dropped_frames"] == 2 * block and h.events[0]["gaps"] == 1
    burst(1010.0, 1)                   # interval elapsed: everything since
    assert len(h.events) == 2
    assert h.events[1]["dropped_frames"] == (1 + 3 + 1) * block
    assert h.events[1]["gaps"] == 3
    assert h.events[1]["total_dropped_frames"] == 7 * block
    burst(1012.0, 2)
    assert len(h.events) == 2
    h.app.stop_session()               # the end reports what is left
    h.app.wait_for_session_ends(10.0)
    assert len(h.events) == 3
    assert h.events[2]["dropped_frames"] == 2 * block
    assert h.events[2]["total_dropped_frames"] == 9 * block
    assert sum(g.frames for g in store.capture_gaps(h.session.id)) == 9 * block
    h.app.close()


def test_a_replay_records_no_gaps(app, fixture_wav, store):
    app.select_engines()
    session = app.start_session(replay_path=fixture_wav, name="replay")
    app.run_replay()
    assert store.capture_gaps(session.id) == []
    assert store.get_session(session.id).to_dict()["dropped_frames"] == 0


# --- operator surfaces ------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_window_status_bar_shows_the_overflow_warning(monkeypatch, qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    h = _held_capture(monkeypatch, config, store, block_size=480, queue_blocks=2)
    window = MainWindow(h.app)
    window.show()
    try:
        h.source.start()
        for _ in range(5):
            _callback(h.source, np.zeros(480))
        h.capture._drain_source()
        assert len(h.events) == 1
        deadline = time.monotonic() + 5.0
        text = ""
        while time.monotonic() < deadline:
            qt_app.processEvents()
            text = window.status.currentMessage()
            if "capture-overflow" in text:
                break
            time.sleep(0.02)
        assert "capture-overflow" in text and "Audio input buffer overflowed" in text
        assert "0.030 s" in text
    finally:
        h.app.stop_session()
        h.app.wait_for_session_ends(10.0)
        window.close()
        h.app.close()


def test_cli_listen_prints_the_overflow_warning(monkeypatch, config, capsys, tmp_path):
    """``babelfishr listen`` with the real capture: the overflow warning
    appears in its monitoring output, and the run carries the gap."""
    from babelfishr import cli
    from babelfishr.pipeline import CaptureService

    settings = tmp_path / "settings.json"
    config.save(str(settings))
    source, stream = _live_source(monkeypatch, block_size=480, max_queue_blocks=2)
    monkeypatch.setattr(BabelFishRApp, "_build_source",
                        lambda self, *a, **k: source)
    entered, release = threading.Event(), threading.Event()
    real_handle = CaptureService._handle_block

    def slow_handle(self, block):
        if not entered.is_set():
            entered.set()
            assert release.wait(20.0)
        return real_handle(self, block)

    monkeypatch.setattr(CaptureService, "_handle_block", slow_handle)
    real_sleep = time.sleep

    def operator(seconds):
        _callback(source, np.zeros(480))
        assert entered.wait(10.0)
        for _ in range(5):                      # 2 queue, 3 dropped
            _callback(source, np.zeros(480))
        release.set()
        assert _wait(lambda: source._queue.qsize() == 0)
        real_sleep(0.2)
        raise KeyboardInterrupt

    # Only the command's own wait: other threads keep the real sleep.
    monkeypatch.setattr(cli, "time", _with_sleep(operator))
    try:
        assert cli.main(["--config", str(settings), "listen", "--name", "bench"]) == 0
    finally:
        release.set()
    err = capsys.readouterr().err
    assert "[audio] capture-overflow: Audio input buffer overflowed" in err
    assert "1440 frames" in err
    check = Store(config.database)
    try:
        (session,) = [s for s in check.list_sessions() if s.name == "bench"]
        assert session.dropped_frames == 3 * 480
    finally:
        check.close()
