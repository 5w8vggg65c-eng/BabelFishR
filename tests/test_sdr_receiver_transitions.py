"""The remaining SDR transition faults, each reproduced the way the review
found it and closed at its boundary: the decoder's input identity (A1), the
ordinary Open → tune-in-SDR++ → Start journey (B1), recording times across
boundaries (B2), boundaries that arrive late or in pairs (B3), Stop against
a receiver lock held over slow rigctl I/O (C1), and cleanup failures that
must keep, not lose, what they failed to end (C2). Stand-ins as in the
sibling files; the real dsd-neo runs the stdin/EOF checks when
BABELFISHR_DSD_NEO and BABELFISHR_DSD_FIXTURE_WAV are set.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
import socket
import subprocess
import threading
import time
import wave

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.receiver import ReceiverController
from babelfishr.receiver.stream import DecodedVoiceSource, PcmTcpSource, StreamBoundary, TuningState
from babelfishr.testing import standard_fixture

STUBS = pathlib.Path(__file__).parent / "stubs"
FAKE_SDRPP = str(STUBS / "fake_sdrpp.py")
FAKE_DSD = str(STUBS / "fake_dsd_stream.py")
RATE = 48000
REAL_DSD = os.environ.get("BABELFISHR_DSD_NEO", "")
REAL_FIXTURE = os.environ.get("BABELFISHR_DSD_FIXTURE_WAV", "")


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


def bursts_wav(path, talk=1.2, gap=1.0, lead=0.6):
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
    monkeypatch.setenv("FAKE_SDRPP_TUNE_FILE", str(tmp_path / "gui-tune"))
    for name in ("FAKE_SDRPP_NO_RIGCTL", "FAKE_SDRPP_DROP_AUDIO_AFTER", "FAKE_SDRPP_AUDIO_STOP_AFTER",
                 "FAKE_SDRPP_RIGCTL_DELAY", "FAKE_DSD_SIGTERM_DELAY", "FAKE_SDRPP_LOOP"):
        monkeypatch.delenv(name, raising=False)
    return config


def sdrpp_log() -> str:
    path = os.environ.get("FAKE_SDRPP_LOG", "")
    return pathlib.Path(path).read_text() if path and os.path.exists(path) else ""


def gui_tune(hz, mode="", bw=""):
    pathlib.Path(os.environ["FAKE_SDRPP_TUNE_FILE"]).write_text(f"{hz} {mode} {bw}".strip())


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


class RawSink:
    def __init__(self):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.conn = None
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.conn, _ = self.server.accept()

    def send(self, samples):
        assert wait_for(lambda: self.conn is not None, timeout=5)
        self.conn.sendall((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())

    def close(self):
        for sock in (self.conn, self.server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


# ---- A1: the decoder has one possible input, whatever the timing of its diagnostics ------------

def test_no_other_input_can_reach_a_recording_when_the_receiver_audio_goes_away(
        receiver_config, store, tmp_path, monkeypatch):
    """The review's reproduction: zero PCM from the receiver, the sink then
    gone for good, dsd-neo's diagnostic line held back for 1.2 s while its
    stdout kept being read - and the fake decoder's substitute-input tone
    was saved as a 1.46 s SDR++/DSD-neo recording. With BabelFishR feeding
    the decoder's stdin there is no substitute input: the pipe closes, the
    decoder ends on end-of-file, and nothing is recorded."""
    monkeypatch.setenv("FAKE_SDRPP_WAV", write_wav(tmp_path / "zero.wav", np.zeros(RATE)))
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    monkeypatch.setenv("FAKE_SDRPP_AUDIO_STOP_AFTER", "1.0")
    monkeypatch.setattr(DecodedVoiceSource, "RECONNECT_SECONDS", 1.0, raising=False)
    # The review's schedule: the handling of the decoder's "Disconnected"
    # line is held for 1.2 s; stdout reading and the capture run on
    # regardless. (On the previous design that held back the only guard.)
    from babelfishr.receiver.stream import CallTracker
    original_note = CallTracker.note

    def held_note(self, line, now=None):
        if "Disconnected" in line:
            time.sleep(1.2)
        return original_note(self, line, now)
    monkeypatch.setattr(CallTracker, "note", held_note)
    receiver_config.receiver.digital = True
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    statuses = []
    app.events.subscribe = getattr(app.events, "subscribe", None)
    app.start_session(name="identity")
    source = app._signal_source
    app.on_status_for_test = None
    source.on_status = (lambda inner: (lambda kind, message: (statuses.append(kind), inner(kind, message))))(source.on_status) \
        if source.on_status else (lambda kind, message: statuses.append(kind))
    app.begin_capture()
    assert wait_for(lambda: "receiver-lost" in statuses, timeout=15), statuses
    time.sleep(2.5)                                   # anything a substitute would have produced
    app.stop_session()
    assert wait_for(lambda: app.capture is None or app.capture.settled, timeout=15)
    assert app.recent_transmissions() == [], \
        [(t.audio_device, t.duration, t.peak_dbfs) for t in app.recent_transmissions()]
    assert not list(pathlib.Path(receiver_config.recording.directory).rglob("*.wav")), \
        "a recording was saved although the receiver delivered only silence"
    # And the reason it cannot happen: the decoder's only input was ours.
    assert source.command()[1:3] == ["-i", "-"]
    assert source.finished and source.decoder["producer_ended"]
    assert wait_for(lambda: source.process.poll() is not None, timeout=10), \
        "dsd-neo outlived its only input"
    assert source.process.returncode == 0
    app.close(wait=True, timeout=30)


@pytest.mark.skipif(not (REAL_DSD and REAL_FIXTURE),
                    reason="set BABELFISHR_DSD_NEO and BABELFISHR_DSD_FIXTURE_WAV")
def test_real_dsd_neo_decodes_from_stdin_and_ends_on_end_of_file(receiver_config, monkeypatch):
    """The actual decoder behind the pump: it decodes what BabelFishR feeds
    it, waits (does not fall back) while nothing arrives, and ends by itself
    when the pipe closes because the receiver's audio is gone."""
    monkeypatch.setenv("FAKE_SDRPP_WAV", REAL_FIXTURE)
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    monkeypatch.setenv("FAKE_SDRPP_AUDIO_STOP_AFTER", "4.0")
    monkeypatch.setattr(DecodedVoiceSource, "RECONNECT_SECONDS", 1.5)
    receiver_config.analysis.dsd_path = REAL_DSD
    receiver_config.receiver.digital = True
    receiver_config.receiver.digital_protocol = "dmr-dual"
    controller = ReceiverController(receiver_config)
    statuses = []
    controller.on_status = lambda kind, message: statuses.append(kind)
    try:
        source = controller.open_source()
        source.start()
        voice = 0
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "receiver-lost" not in statuses:
            block = source.read(timeout=0.5)
            if block is not None and not isinstance(block, StreamBoundary) \
                    and float(np.max(np.abs(block.samples))) > 0.01:
                voice += block.samples.size
        assert source.decoder["sync_lines"] > 0, source.stderr_tail[-5:]
        assert voice > 8000, "the real decoder produced no speech from the fed audio"
        assert "receiver-lost" in statuses
        assert wait_for(lambda: source.process.poll() is not None, timeout=10), "no exit on EOF"
        assert source.process.returncode == 0
        assert any("Exiting" in line for line in source.stderr_tail), source.stderr_tail[-3:]
        assert not any("Interrupted" in line or "Disconnected" in line for line in source.stderr_tail)
    finally:
        controller.shutdown()


# ---- B1: the ordinary journey never restores an older choice over the operator's --------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds=20):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.004)


def test_open_then_tune_in_sdrpp_then_start_keeps_the_operators_tuning(qt_app, receiver_config, store):
    from PySide6 import QtWidgets

    from babelfishr.ui.main_window import MainWindow

    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    window._open_receiver()                          # Receiver › Open receiver window
    pump(qt_app)
    assert "rigctl F 155100000" in sdrpp_log(), "the stored choice is applied when we start SDR++"
    gui_tune(162_550_000, "FM", 12500)               # the operator, in SDR++'s own window
    assert wait_for(lambda: "gui tune 162550000" in sdrpp_log(), timeout=5)
    session = app.start_session(name="journey")      # Start monitoring, no flags touched
    try:
        assert app.receiver.tuning.confirmed_hz == 162_550_000.0, \
            "Start restored 155.100 MHz over the operator's 162.550 MHz"
        log = sdrpp_log()
        assert "rigctl F" not in log[log.index("gui tune 162550000"):]
        assert app._signal_source.metadata().tuned_frequency_hz == 162_550_000.0
        # An explicit Tune receiver request while SDR++ runs is applied at once.
        result = app.receiver.tune(frequency_hz=146.52e6)
        assert result["confirmed_hz"] == 146_520_000.0
    finally:
        app.stop_session()
    # The attach case: another controller finds SDR++ running and takes its tuning.
    other = ReceiverController(receiver_config)
    other.ensure_sdrpp()
    assert "attached to a running SDR++" in other.notes
    other.read_back()
    assert other.tuning.confirmed_hz == 146_520_000.0
    assert "rigctl F 155100000" not in sdrpp_log()[sdrpp_log().index("gui tune"):]
    window.close()
    pump(qt_app, 40)
    assert wait_for(lambda: app.receiver._process is None or not app.receiver._process.alive, timeout=20)


# ---- B2: recording times stay chronological across boundaries ---------------------------------

@pytest.mark.parametrize("digital", [False, True], ids=["analog", "decoded"])
def test_recordings_after_retunes_keep_their_true_times(receiver_config, store, tmp_path,
                                                        monkeypatch, digital):
    monkeypatch.setenv("FAKE_SDRPP_WAV", bursts_wav(tmp_path / "bursts.wav", talk=1.2, gap=1.0, lead=0.6))
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.receiver.digital = digital
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app.start_session(name="times")
    began = _dt.datetime.now(_dt.timezone.utc)
    boundaries = []
    try:
        app.begin_capture()
        for frequency in (155.16e6, 162.55e6, 146.52e6):
            assert wait_for(lambda: len(app.recent_transmissions()) >= len(boundaries) + 1, timeout=25)
            assert wait_for(lambda: app.capture.detector.open, timeout=25)       # a fresh burst
            time.sleep(0.7)                        # enough heard to be a recording
            boundaries.append(_dt.datetime.now(_dt.timezone.utc))
            app.receiver.tune(frequency_hz=frequency)
            time.sleep(1.3)                        # past the cut burst's tail and the hang time
    finally:
        app.stop_session()
    assert wait_for(lambda: app.capture is None or app.capture.settled, timeout=15)
    rows = stored_rows(store)
    assert len(rows) >= 4, rows
    times = [parse(row[0]) for row in rows]
    for earlier, later, row in zip(times, times[1:], rows[1:]):
        assert later > earlier, f"stored started_at went backwards: {rows}"
    for index, (started, duration, _) in enumerate(rows):
        start = parse(started)
        assert start >= began - _dt.timedelta(seconds=1)
        end = start + _dt.timedelta(seconds=duration)
        assert end <= _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=1)
        if index:
            previous_end = parse(rows[index - 1][0]) + _dt.timedelta(seconds=rows[index - 1][1])
            assert start >= previous_end - _dt.timedelta(seconds=0.25), \
                f"recording {index} overlaps the one before it: {rows}"
    # Every recording made after a boundary starts after it.
    for boundary in boundaries:
        after = [parse(r[0]) for r in rows if parse(r[0]) > boundary - _dt.timedelta(seconds=1.5)]
        assert all(t >= boundary - _dt.timedelta(seconds=1.5) for t in after)
    frequencies = [round(row[2], 4) for row in rows]
    # Each recording carries the tuning it was heard on: the first burst
    # closed by itself on 155.1, the next was cut by the retune to 155.16 (so
    # heard on 155.1), and so on. The last tuning has no recording only
    # because the run stopped before a burst arrived on it.
    assert frequencies[:4] == [155.1, 155.1, 155.16, 162.55], frequencies
    app.close(wait=True, timeout=30)


# ---- B3: boundaries are never erased; late consumers still close under the right tuning -------

def test_two_retunes_before_a_drain_keep_both_boundaries_in_order():
    sink = RawSink()
    tuning = TuningState()
    tuning.confirmed_hz, tuning.mode = 155_100_000.0, "FM"
    source = PcmTcpSource("127.0.0.1", sink.port, tuning, sample_rate=RATE)
    try:
        source.start()
        sink.send(speech_like(0.5))                    # heard on 155.100, not yet consumed
        assert wait_for(lambda: source.buffered >= 5, timeout=5)
        source.retuned("retune")                       # → 155.160
        tuning.confirmed_hz, tuning.epoch = 155_160_000.0, 1
        source.retuned("retune")                       # → 162.550, before anyone read
        tuning.confirmed_hz, tuning.epoch = 162_550_000.0, 2
        sink.send(speech_like(0.1))
        first = source.read(timeout=5.0)
        assert isinstance(first, StreamBoundary) and first.metadata.tuned_frequency_hz == 155_100_000.0
        assert first.metadata.extra["tuning_epoch"] == 0
        second = source.read(timeout=5.0)
        assert isinstance(second, StreamBoundary) and second.metadata.tuned_frequency_hz == 155_160_000.0
        assert second.metadata.extra["tuning_epoch"] == 1
        block = source.read(timeout=5.0)
        assert block is not None and not isinstance(block, StreamBoundary)
        assert source.metadata().tuned_frequency_hz == 162_550_000.0
        # Overflow pressure drops audio, never a marker.
        source.retuned("retune")
        tuning.confirmed_hz, tuning.epoch = 146_520_000.0, 3
        sink.send(speech_like(12.0))                   # more than the 10 s the buffer holds
        assert wait_for(lambda: source.dropped_frames > 0, timeout=15)
        items = []
        while True:
            item = source.read(timeout=0.5)
            if item is None:
                break
            items.append(item)
        markers = [i for i in items if isinstance(i, StreamBoundary)]
        assert len(markers) == 1 and markers[0].metadata.tuned_frequency_hz == 162_550_000.0
        assert items.index(markers[0]) == 0, "the marker was not first in line"
    finally:
        source.stop()
        sink.close()


def test_held_audio_is_saved_under_the_frequency_it_was_heard_on_after_two_quick_retunes(
        receiver_config, store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_WAV", bursts_wav(tmp_path / "bursts.wav", talk=3.0, gap=1.5, lead=0.6))
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app.start_session(name="held")
    try:
        app.begin_capture()
        assert wait_for(lambda: app.capture is not None and app.capture.detector.open, timeout=20)
        source = app._signal_source
        # The consumer is held: nothing is read while two retunes go by.
        gate = threading.Event()
        real_read = source.read

        def gated_read(timeout=1.0):
            gate.wait(timeout=5.0)
            return real_read(timeout)
        time.sleep(1.0)                                # a second of voice heard on 155.100
        monkeypatch.setattr(source, "read", gated_read)
        time.sleep(0.3)
        app.receiver.tune(frequency_hz=155.16e6)
        app.receiver.tune(frequency_hz=162.55e6)
        gate.set()
        assert wait_for(lambda: len(app.recent_transmissions()) >= 1, timeout=15)
        first = min(app.recent_transmissions(), key=lambda t: t.started_at)
        assert first.frequency_mhz == pytest.approx(155.1), \
            "voice heard on 155.100 MHz was saved under a later frequency"
        assert first.signal_metadata["sdrpp"]["extra"]["tuning_epoch"] == 0
    finally:
        app.stop_session()
    app.close(wait=True, timeout=30)


# ---- C1: Stop and Quit never wait on the poller's rigctl read -------------------------------------

def test_stop_and_quit_do_not_wait_for_a_slow_control_server(qt_app, receiver_config, store, monkeypatch):
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtTest import QTest

    from babelfishr.ui.input_panel import RECEIVER
    from babelfishr.ui.main_window import MainWindow

    monkeypatch.setenv("FAKE_SDRPP_RIGCTL_DELAY", "1.2")
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    ticks = []
    heartbeat = QtCore.QTimer(window)
    heartbeat.setInterval(20)
    heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
    heartbeat.start()

    def start_and_wait_for_poll_in_flight():
        app.start_session(name="slow rigctl")
        app.begin_capture()
        window._refresh_state() if hasattr(window, "_refresh_state") else None
        pump(qt_app, 10)
        # The poller reads every second and each reply takes 1.2 s: within
        # 1.5 s a read is in flight, holding the receiver's I/O lock.
        time.sleep(1.5)

    for round_number in range(2):                    # Stop, start again, Stop again
        start_and_wait_for_poll_in_flight()
        ticks.clear()
        began = time.monotonic()
        app.stop_session()
        pump(qt_app, 5)
        elapsed = time.monotonic() - began
        assert elapsed < 0.3, f"round {round_number}: Stop waited {elapsed:.2f}s for the receiver lock"
        assert app.capture is None
    assert ticks, "no heartbeat ticks during Stop"
    # Quit with a read in flight: the window stays responsive and the close completes.
    start_and_wait_for_poll_in_flight()
    ticks.clear()
    began = time.monotonic()
    window.close()
    first_return = time.monotonic() - began
    assert first_return < 0.5, f"closeEvent held the GUI thread for {first_return:.2f}s"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not window._shutdown_complete:
        pump(qt_app, 5)
    assert window._shutdown_complete
    assert len(ticks) > 5, "the window did not keep ticking while quitting"
    assert app.receiver._process is None
    assert app.receiver._poller is None or not app.receiver._poller.is_alive()


def test_an_old_polling_loop_never_touches_the_next_run(receiver_config, monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_RIGCTL_DELAY", "1.2")
    controller = ReceiverController(receiver_config)
    try:
        first = controller.open_source()
        first.start()
        time.sleep(1.5)                                # its poller is inside a delayed read
        old_poller = controller._poller
        controller.release_source()
        first.stop()
        second = controller.open_source()
        second.start()
        assert controller._poller is not old_poller
        # Make the old loop's read fail: SDR++ answers, but the old run is over.
        old_poller.join(timeout=6.0)
        assert not old_poller.is_alive()
        assert second.running and not second.finished, "the old loop ended the new run's source"
    finally:
        controller.shutdown()


# ---- C2: a cleanup that fails keeps what it failed to end --------------------------------------

def _digital_app_after_stop(receiver_config, store):
    receiver_config.receiver.digital = True
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app.start_session(name="cleanup")
    app.begin_capture()
    assert wait_for(lambda: app._signal_source.process is not None, timeout=10)
    app.stop_session()
    return app


def test_an_exception_while_ending_sdrpp_keeps_it_owned_and_the_retry_ends_it(receiver_config, store,
                                                                                monkeypatch):
    app = _digital_app_after_stop(receiver_config, store)
    process = app.receiver._process
    real_terminate = process.terminate
    calls = []

    def failing_once(timeout=5.0):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise OSError("injected: terminate failed")
        return real_terminate(timeout)
    monkeypatch.setattr(process, "terminate", failing_once)
    assert not app.close(wait=True, timeout=10), "a close that raised was reported done"
    assert app.receiver._process is process, "the live SDR++ was forgotten after the exception"
    assert process.alive
    assert app.close(wait=True, timeout=10), "the retry did not finish"
    assert len(calls) == 2 and not process.alive and app.receiver._process is None


def test_a_termination_that_times_out_keeps_ownership_until_a_retry_succeeds(receiver_config, store,
                                                                              monkeypatch):
    app = _digital_app_after_stop(receiver_config, store)
    process = app.receiver._process
    real_terminate = process.terminate
    monkeypatch.setattr(process, "terminate", lambda timeout=5.0: None)    # asks, waits, nothing ends
    assert not app.close(wait=True, timeout=10)
    assert app.receiver._process is process and process.alive
    assert app.receiver_error and "did not end" in app.receiver_error
    assert not app.close(wait=True, timeout=10), "still alive: still not done"
    monkeypatch.setattr(process, "terminate", real_terminate)
    assert app.close(wait=True, timeout=10)
    assert not process.alive and app.receiver._process is None and app.receiver_error == ""


def test_a_decoder_that_will_not_settle_stays_owned_and_sdrpp_is_not_touched_yet(receiver_config,
                                                                                  monkeypatch):
    receiver_config.receiver.digital = True
    controller = ReceiverController(receiver_config)
    source = controller.open_source()
    source.start()
    process = controller._process
    attempts = []

    def settle_fails_once(src, timeout=5.0):
        attempts.append(src)
        if len(attempts) == 1:
            return False
        return src.settle(timeout)
    monkeypatch.setattr(controller, "settle_source", settle_fails_once)
    handle = controller.begin_shutdown()
    handle.join(10)
    assert handle.settled and handle.error is not None and "dsd-neo" in str(handle.error)
    assert controller.source is source, "the unsettled decoder was dropped from the controller"
    assert controller._process is process and process.alive, "SDR++ was ended although the decoder had not settled"
    handle = controller.begin_shutdown()
    handle.join(10)
    assert handle.settled and handle.error is None, handle.error
    assert controller.source is None and controller._process is None and not process.alive
    assert len(attempts) == 2
    assert controller.begin_shutdown().settled, "a third shutdown must find nothing left and do nothing"
