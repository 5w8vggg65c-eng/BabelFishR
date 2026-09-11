"""Stream cleanup and recording identity, each reproduced the way the review
found it: a decoder that stops reading its pipe (A1), a reconnect that
lands after Stop (A2), a source whose stop() or settle() fails under the
ordinary Stop/Quit ownership (B), a stream with a break in it (C1), and a
retune while a block is already being processed (C2). Stand-ins as in the
sibling files, plus a scripted source with explicit timestamps and a test
child that never reads its input. No radio, no Mac, mock recognition.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import AudioBlock
from babelfishr.models import Provenance
from babelfishr.pipeline import CaptureService
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.receiver import ReceiverController
from babelfishr.receiver import stream as stream_module
from babelfishr.receiver.stream import DecodedVoiceSource, StreamBoundary, TuningState
from babelfishr.sources import SignalMetadata, SignalSource
from babelfishr.testing import standard_fixture

STUBS = pathlib.Path(__file__).parent / "stubs"
FAKE_SDRPP = str(STUBS / "fake_sdrpp.py")
FAKE_DSD = str(STUBS / "fake_dsd_stream.py")
RATE = 48000


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def write_wav(path, samples, rate=RATE):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return str(path)


def speech_like(seconds, amplitude=0.3, rate=RATE):
    t = np.arange(int(seconds * rate)) / rate
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 4.0 * t)
    return amplitude * envelope * (0.6 * np.sin(2 * np.pi * 700 * t) + 0.4 * np.sin(2 * np.pi * 1300 * t))


def bursts_wav(path, talk=3.0, gap=1.5, lead=0.6):
    return write_wav(path, np.concatenate([np.zeros(int(lead * RATE)), speech_like(talk),
                                           np.zeros(int(gap * RATE))]))


@pytest.fixture
def receiver_config(config, tmp_path, monkeypatch):
    config.receiver.sdrpp_path = FAKE_SDRPP
    config.receiver.sdrpp_root = str(tmp_path / "sdrpp-root")
    config.receiver.rigctl_port = free_port()
    config.receiver.audio_port = free_port()
    config.receiver.frequency_hz = 155.1e6
    config.receiver.mode = "FM"
    config.receiver.connect_timeout_s = 10.0
    config.analysis.dsd_path = FAKE_DSD
    monkeypatch.setenv("FAKE_SDRPP_WAV", standard_fixture(RATE).write(str(tmp_path / "fixture48k.wav")))
    monkeypatch.setenv("FAKE_SDRPP_LOG", str(tmp_path / "sdrpp.log"))
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    for name in ("FAKE_SDRPP_NO_RIGCTL", "FAKE_SDRPP_DROP_AUDIO_AFTER", "FAKE_SDRPP_AUDIO_STOP_AFTER",
                 "FAKE_SDRPP_RIGCTL_DELAY", "FAKE_DSD_SIGTERM_DELAY", "FAKE_SDRPP_TUNE_FILE"):
        monkeypatch.delenv(name, raising=False)
    return config


def mock_app(config, store):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


def stored_rows(store):
    return [tuple(row) for row in store._conn.execute(
        "SELECT started_at, duration, frequency_mhz FROM transmissions ORDER BY rowid")]


def parse(stamp) -> _dt.datetime:
    return _dt.datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp


def process_alive(pid: int) -> bool:
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


# ---- A1: a decoder that stops reading must not hold the shutdown -------------------------------

DEAF_CHILD = ("import signal, time, sys\n"
              "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
              "sys.stderr.write('deaf child: never reads stdin\\n'); sys.stderr.flush()\n"
              "while True: time.sleep(0.1)\n")


def test_stop_reaches_the_decoder_even_when_the_pump_is_blocked_on_a_full_pipe(receiver_config, store,
                                                                              monkeypatch):
    monkeypatch.setattr(DecodedVoiceSource, "command",
                        lambda self: [sys.executable, "-c", DEAF_CHILD])
    receiver_config.receiver.digital = True
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app.start_session(name="deaf")
    source = app._signal_source
    app.begin_capture()
    # The pipe holds about 64 KB; once it is full the pump sits inside write.
    assert wait_for(lambda: source.process is not None and source.pcm_bytes_in > 60_000, timeout=20), \
        "the pipe never filled"
    assert wait_for(lambda: not source._stdin_lock.acquire(blocking=False)
                    or (source._stdin_lock.release() or False), timeout=10), \
        "the reproduction needs the pump blocked inside a write"
    time.sleep(0.3)
    assert source._pump.is_alive() and not source._stdin_lock.acquire(blocking=False), \
        "the reproduction needs the pump blocked inside a write"
    pid = source.process.pid
    began = time.monotonic()
    app.stop_session()
    assert time.monotonic() - began < 0.5
    capture = app._lingering_capture
    assert capture is None or wait_for(lambda: capture.settled, timeout=10), \
        "the capture's stopper never got past closing the decoder's input"
    assert wait_for(lambda: not process_alive(pid), timeout=10), "the decoder was never ended"
    assert wait_for(lambda: source.settled, timeout=10)
    assert not source._pump.is_alive() and source._sock is None
    assert app.close(wait=True, timeout=20)


# ---- A2: a connection that lands after Stop is not ours ----------------------------------------

def test_a_reconnect_that_completes_after_stop_is_closed_and_never_announced(receiver_config, monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_DROP_AUDIO_AFTER", "1.0")
    receiver_config.receiver.digital = True
    controller = ReceiverController(receiver_config)
    statuses = []
    controller.on_status = lambda kind, message: statuses.append(kind)
    gate = threading.Event()
    made = []
    real_connect = socket.create_connection

    def held_connect(address, timeout=None, **kwargs):
        sock = real_connect(address, timeout=timeout, **kwargs)
        if address[1] == receiver_config.receiver.audio_port and made:
            gate.wait(timeout=10)                     # connected, not yet returned
        made.append(sock)
        return sock
    monkeypatch.setattr(stream_module.socket, "create_connection", held_connect)
    source = controller.open_source()
    source.start()
    try:
        assert wait_for(lambda: "upstream-lost" in statuses, timeout=10), statuses
        assert wait_for(lambda: len(made) >= 1 and source._sock is None, timeout=5)
        time.sleep(0.5)                                # the reconnect is inside create_connection
        source.stop()
        assert wait_for(lambda: source.process.poll() is not None, timeout=10)
        assert not source.settled, "settled while the pump (and its connect) were still alive"
        gate.set()
        assert wait_for(lambda: source.settled, timeout=10)
        late = made[-1]
        assert late.fileno() == -1, "the late connection stayed open"
        assert source._sock is None
        assert "upstream-restored" not in statuses, statuses
        assert source.settle(2.0) and not source._pump.is_alive()
    finally:
        gate.set()
        controller.shutdown()


def test_restored_is_announced_when_pcm_arrives_not_when_a_socket_is_accepted(receiver_config,
                                                                              monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_DROP_AUDIO_AFTER", "1.0")
    receiver_config.receiver.digital = True
    controller = ReceiverController(receiver_config)
    events = []
    holder = {}
    # Each report is stamped with how much PCM had arrived when it was made.
    controller.on_status = lambda kind, message: events.append(
        (kind, holder["source"].pcm_bytes_in if "source" in holder else None))
    source = holder["source"] = controller.open_source()
    source.start()
    try:
        assert wait_for(lambda: any(k == "upstream-restored" for k, _ in events), timeout=15), events
        lost_bytes = [b for k, b in events if k == "upstream-lost"][0]
        restored_bytes = [b for k, b in events if k == "upstream-restored"][0]
        assert restored_bytes > lost_bytes, \
            f"restored was announced with no new PCM since the loss ({lost_bytes} → {restored_bytes})"
        assert source.decoder["upstream_lost"] is False
    finally:
        controller.shutdown()


# ---- B: a source whose cleanup fails stays the run's to finish -----------------------------------

def _digital_run(receiver_config, store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_WAV", bursts_wav(tmp_path / "bursts.wav"))
    receiver_config.receiver.digital = True
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    published = []
    real_publish = app.events.publish
    app.events.publish = lambda kind, payload: (published.append((kind, payload)),
                                                real_publish(kind, payload))[1]
    app.start_session(name="cleanup")
    app.begin_capture()
    source = app._signal_source
    assert wait_for(lambda: source.process is not None and source.receiving, timeout=15)
    return app, source, published


def test_a_stop_that_raises_keeps_the_source_owned_reported_and_retried_through_stop_and_quit(
        receiver_config, store, tmp_path, monkeypatch):
    app, source, published = _digital_run(receiver_config, store, tmp_path, monkeypatch)
    pid = source.process.pid
    real_stop = source.stop
    calls = {"stop": 0, "allow": False}

    def failing_stop():
        calls["stop"] += 1
        if not calls["allow"]:
            raise OSError("injected: stop failed")
        return real_stop()
    monkeypatch.setattr(source, "stop", failing_stop)
    app.stop_session()
    capture = app._lingering_capture
    assert capture is not None
    assert wait_for(lambda: not capture.stopping, timeout=10)
    assert not capture.settled, "a capture whose source did not stop reported itself settled"
    assert not capture.source_stopped and "injected" in capture.source_error
    assert process_alive(pid), "the decoder is still running - and still owned"
    failures = [p for k, p in published if k == "audio-status" and p.get("kind") == "source-stop-failed"]
    assert len(failures) == 1
    assert not app.close(wait=True, timeout=10), "Quit reported done with the decoder still running"
    assert "injected" in app.capture_error and calls["stop"] >= 2
    assert process_alive(pid) and app._lingering_capture is capture
    calls["allow"] = True
    assert app.close(wait=True, timeout=15)
    assert not process_alive(pid) and capture.settled and app.capture_error == ""
    failures = [p for k, p in published if k == "audio-status" and p.get("kind") == "source-stop-failed"]
    assert len(failures) == 1, "the same failure was reported again"


def test_a_settle_that_fails_is_retried_without_stopping_the_source_twice(receiver_config, store,
                                                                          tmp_path, monkeypatch):
    app, source, published = _digital_run(receiver_config, store, tmp_path, monkeypatch)
    pid = source.process.pid
    real_stop, real_settle = source.stop, source.settle
    counts = {"stop": 0, "settle": 0}

    def counted_stop():
        counts["stop"] += 1
        return real_stop()

    def failing_settle(timeout=5.0):
        counts["settle"] += 1
        if counts["settle"] <= 2:
            return False
        return real_settle(timeout)
    monkeypatch.setattr(source, "stop", counted_stop)
    monkeypatch.setattr(source, "settle", failing_settle)
    app.stop_session()
    capture = app._lingering_capture
    assert wait_for(lambda: capture is not None and not capture.stopping, timeout=10)
    assert not capture.settled and "did not end" in capture.source_error
    assert counts == {"stop": 1, "settle": 1}
    assert not app.close(wait=True, timeout=10)      # second settle fails too
    assert counts["stop"] == 1 and counts["settle"] == 2 and app.capture_error
    assert app.close(wait=True, timeout=15)          # third succeeds
    assert counts["stop"] == 1 and counts["settle"] == 3
    assert capture.settled and not process_alive(pid)


# ---- C1: audio keeps its own time across a break in the stream --------------------------------

class ScriptedSource(SignalSource):
    """Blocks with the timestamps the test says, nothing else."""

    measures_rf = True

    def __init__(self, blocks, tuning):
        self._blocks = list(blocks)
        self.tuning = tuning
        self.sample_rate = RATE
        self._running = False
        self._finished = False
        self.name = "scripted"

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    @property
    def running(self):
        return self._running

    @property
    def finished(self):
        return self._finished

    def read(self, timeout=1.0):
        if self._blocks:
            time.sleep(0.005)
            return self._blocks.pop(0)
        time.sleep(min(timeout, 0.05))
        return None

    def metadata(self, tuning=None):
        return (tuning or self.tuning).metadata("scripted", RATE)


def scripted_blocks(start, segments, block_seconds=0.02):
    """*segments*: (from_s, to_s, amplitude, timestamp_shift_s) - audio for
    [from, to) at the given level, stamped at stream time + shift."""
    blocks = []
    for from_s, to_s, amplitude, shift in segments:
        t = from_s
        while t < to_s - 1e-9:
            frames = int(round(min(block_seconds, to_s - t) * RATE))
            if amplitude:
                samples = speech_like(frames / RATE, amplitude)[:frames]
            else:
                samples = np.random.default_rng(int(t * 1000)).normal(0, 0.001, frames)
            blocks.append(AudioBlock(samples=samples, sample_rate=RATE,
                                     timestamp=start + _dt.timedelta(seconds=t + shift), offset=t))
            t += frames / RATE
    return blocks


def test_a_recording_keeps_its_time_when_the_stream_breaks_before_the_closing_silence(config, store):
    start = _dt.datetime(2026, 9, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    tuning = TuningState()
    tuning.confirmed_hz, tuning.mode = 155_100_000.0, "FM"
    # Silence 0-1.0 s, voice 1.0-2.2 s, then the closing silence arrives
    # stamped three seconds late (a break in the stream).
    source = ScriptedSource(scripted_blocks(start, [(0.0, 1.0, 0.0, 0.0), (1.0, 2.2, 0.3, 0.0),
                                                    (2.2, 4.0, 0.0, 3.0)]), tuning)
    app = mock_app(config, store)
    app.start_session(source=source, name="break")
    app.begin_capture()
    assert wait_for(lambda: len(stored_rows(store)) >= 1, timeout=20)
    app.stop_session()
    assert wait_for(lambda: app._lingering_capture is None or app._lingering_capture.settled, timeout=10)
    rows = stored_rows(store)
    assert len(rows) == 1, rows
    started, duration, frequency = rows[0]
    offset = (parse(started) - start).total_seconds()
    # The voice began at 1.0 s; the recording opens with its pre-roll a
    # little before that - never three seconds later.
    assert 0.4 <= offset <= 1.05, f"recording dated {offset:.3f}s after stream start: {rows}"
    assert 1.0 <= duration <= 1.7, rows
    assert frequency == pytest.approx(155.1)
    app.close(wait=True, timeout=20)


def test_audio_after_a_break_carries_its_own_later_time(config, store):
    start = _dt.datetime(2026, 9, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    tuning = TuningState()
    tuning.confirmed_hz, tuning.mode = 155_100_000.0, "FM"
    # Voice at 1.0-2.2 s, silence, then (after a 3 s break) voice at 5.0-6.0
    # stream seconds which is 8.0-9.0 in the source's own time.
    source = ScriptedSource(scripted_blocks(start, [(0.0, 1.0, 0.0, 0.0), (1.0, 2.2, 0.3, 0.0),
                                                    (2.2, 3.5, 0.0, 0.0),
                                                    (3.5, 5.0, 0.0, 3.0), (5.0, 6.0, 0.3, 3.0),
                                                    (6.0, 7.5, 0.0, 3.0)]), tuning)
    app = mock_app(config, store)
    app.start_session(source=source, name="break2")
    app.begin_capture()
    assert wait_for(lambda: len(stored_rows(store)) >= 2, timeout=25)
    app.stop_session()
    assert wait_for(lambda: app._lingering_capture is None or app._lingering_capture.settled, timeout=10)
    rows = stored_rows(store)
    offsets = [(parse(r[0]) - start).total_seconds() for r in rows]
    assert 0.4 <= offsets[0] <= 1.05, rows
    assert 7.4 <= offsets[1] <= 8.05, f"the second recording did not keep the source's own time: {rows}"
    app.close(wait=True, timeout=20)


# ---- C2: a block already being processed keeps its tuning ----------------------------------------

def test_a_recording_completed_by_a_block_in_hand_during_a_retune_keeps_that_blocks_frequency(
        receiver_config, store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_WAV", bursts_wav(tmp_path / "bursts.wav", talk=1.2, gap=2.0, lead=0.6))
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app.start_session(name="in hand")
    app.begin_capture()
    capture = app.capture
    gate = threading.Event()
    held = {"block": None}
    real_handle = capture._handle_block

    def holding_handle(block):
        detector = capture.detector
        # An old-frequency silence block, in hand, that will close the open
        # recording: hold it here, retune, then let it through.
        if (held["block"] is None and detector.open
                and float(np.max(np.abs(block.samples))) < 0.02
                and detector._hang_remaining <= detector.frame_dt + 1e-9):   # this block closes it
            held["block"] = block
            gate.wait(timeout=10)
        return real_handle(block)
    monkeypatch.setattr(capture, "_handle_block", holding_handle)
    try:
        assert wait_for(lambda: held["block"] is not None, timeout=25)
        result = app.receiver.tune(frequency_hz=162.55e6)
        assert result["confirmed_hz"] == 162_550_000.0 and result["changed"]
        assert app._signal_source.metadata().tuned_frequency_hz == 162_550_000.0
        gate.set()
        assert wait_for(lambda: len(stored_rows(store)) >= 1, timeout=15)
        started, duration, frequency = stored_rows(store)[0]
        assert frequency == pytest.approx(155.1), \
            f"audio heard on 155.100 MHz was saved as {frequency} MHz"
        tx = app.recent_transmissions()[0]
        assert tx.signal_metadata["sdrpp"]["extra"]["tuning_epoch"] == 0
        assert tx.frequency_provenance is Provenance.SDR
        # The identity travelled with the block itself.
        assert getattr(held["block"], "tuning", None) is not None
        assert held["block"].tuning.confirmed_hz == 155_100_000.0
    finally:
        gate.set()
        app.stop_session()
    app.close(wait=True, timeout=30)
