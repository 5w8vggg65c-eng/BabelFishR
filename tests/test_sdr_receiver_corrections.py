"""Corrections to the SDR receiver path, each at the boundary where the
audit found the fault (items A1-A2, B4-B7, C8-C9, D10-D13). Every test here
failed against the previous commit's receiver package (checked by running
this file against a worktree of that commit with these stand-ins) and
passes now. The stand-ins are the same two as in test_sdr_receiver.py; their
new switches (FAKE_SDRPP_AUDIO_STOP_AFTER, FAKE_SDRPP_TUNE_FILE, the fake
decoder giving its TCP input up after a failed retry) reproduce behaviour
read in the upstream sources and, where marked, seen on the real programs.
Nothing here is a receiver, an antenna or a Mac.
"""

from __future__ import annotations

import importlib.util
import json
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

from babelfishr.app import BabelFishRApp, ProcessingBusy
from babelfishr.models import ProcessingState
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.receiver import (SDRPP, ReceiverController, ReceiverError,
                                 ReceiverUnavailable, receiver_status)
from babelfishr.receiver import controller as controller_module
from babelfishr.receiver import stream as stream_module
from babelfishr.receiver.sdrpp import RigctlClient, SdrppConfigurator, sdrpp_processes
from babelfishr.receiver.stream import (DecodedVoiceSource, PcmTcpSource, StreamBoundary,
                                        TuningState)
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


def tone(seconds, amplitude=0.3, hz=700.0, rate=RATE):
    t = np.arange(int(seconds * rate)) / rate
    return amplitude * np.sin(2 * np.pi * hz * t)


def speech_like(seconds, amplitude=0.3, rate=RATE):
    """Two speech-band tones under a syllable-rate envelope: loud enough for
    the fake decoder, shaped enough for the detector (a constant tone from
    the first sample is what its noise floor learns as silence)."""
    t = np.arange(int(seconds * rate)) / rate
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 4.0 * t)
    return amplitude * envelope * (0.6 * np.sin(2 * np.pi * 700 * t) + 0.4 * np.sin(2 * np.pi * 1300 * t))


def bursts_wav(path, talk=3.0, gap=1.5, lead=1.0):
    return write_wav(path, np.concatenate([np.zeros(int(lead * RATE)), speech_like(talk),
                                           np.zeros(int(gap * RATE))]))


@pytest.fixture
def fixture_48k(tmp_path):
    return standard_fixture(RATE).write(str(tmp_path / "fixture48k.wav"))


@pytest.fixture
def receiver_config(config, tmp_path, fixture_48k, monkeypatch):
    root = tmp_path / "sdrpp-root"
    config.receiver.sdrpp_path = FAKE_SDRPP
    config.receiver.sdrpp_root = str(root)
    config.receiver.rigctl_port = free_port()
    config.receiver.audio_port = free_port()
    config.receiver.frequency_hz = 462.5625e6
    config.receiver.mode = "FM"
    config.receiver.connect_timeout_s = 10.0
    config.analysis.dsd_path = FAKE_DSD
    monkeypatch.setenv("FAKE_SDRPP_WAV", fixture_48k)
    monkeypatch.setenv("FAKE_SDRPP_LOG", str(tmp_path / "sdrpp.log"))
    monkeypatch.setenv("FAKE_SDRPP_TUNE_FILE", str(tmp_path / "gui-tune"))
    monkeypatch.delenv("FAKE_SDRPP_NO_RIGCTL", raising=False)
    monkeypatch.delenv("FAKE_SDRPP_DROP_AUDIO_AFTER", raising=False)
    monkeypatch.delenv("FAKE_SDRPP_AUDIO_STOP_AFTER", raising=False)
    monkeypatch.delenv("FAKE_DSD_SIGTERM_DELAY", raising=False)
    return config


def sdrpp_log() -> str:
    path = os.environ.get("FAKE_SDRPP_LOG", "")
    return pathlib.Path(path).read_text() if path and os.path.exists(path) else ""


def gui_tune(hz, mode="", bw=""):
    """The operator tunes in the SDR++ window (not over rigctl)."""
    pathlib.Path(os.environ["FAKE_SDRPP_TUNE_FILE"]).write_text(f"{hz} {mode} {bw}".strip())


def mock_app(config, store):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


def fake_loader():
    """The fake SDR++'s typed config reader, the same typed reads SDR++ does."""
    spec = importlib.util.spec_from_file_location("fake_sdrpp", FAKE_SDRPP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_settings


class RawSink:
    """A bare TCP producer of int16 mono PCM, driven by the test."""

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

    def send_seconds(self, seconds, amplitude=0.3):
        assert wait_for(lambda: self.conn is not None, timeout=5)
        self.conn.sendall((tone(seconds, amplitude) * 32767).astype("<i2").tobytes())

    def close(self):
        for sock in (self.conn, self.server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


# ---- A1: \start and \stop write no reply ----------------------------------------------

def test_start_is_a_request_the_client_does_not_wait_on_and_nothing_confirms_it(receiver_config):
    controller = ReceiverController(receiver_config)
    try:
        controller.ensure_sdrpp()
        rig = RigctlClient("127.0.0.1", receiver_config.receiver.rigctl_port)
        rig.connect()
        began = time.monotonic()
        rig.start()                       # upstream: setPlayState(true), no reply line
        rig.stop()
        rig.start()
        elapsed = time.monotonic() - began
        assert elapsed < 1.0, f"the client waited {elapsed:.1f}s for a reply upstream never writes"
        # The next real query still lines up with its own reply.
        assert rig.frequency() == 100_000_000.0 or rig.frequency() > 0
        assert rig.mode()[0] in SDRPP.rigctl_modes
        rig.close()
        controller.request_start()
        status = controller.status()["tuning"]
        assert status["radio_start_requested"] is True
        assert status["radio_start_confirmed"] is None, "\\start confirms nothing; audio does"
        assert "rigctl \\start" in sdrpp_log() and "rigctl \\stop" in sdrpp_log()
    finally:
        controller.shutdown()


# ---- A2: complete configuration for SDR++'s typed reads ------------------------------

def test_fresh_and_partial_configs_are_completed_for_every_typed_read_and_the_rest_kept(tmp_path):
    load_settings = fake_loader()
    fresh = tmp_path / "fresh"
    SdrppConfigurator(fresh).ensure(audio_host="127.0.0.1", audio_port=7400, sample_rate=48000,
                                    rigctl_host="127.0.0.1", rigctl_port=4600)
    settings = load_settings(fresh)                  # SystemExit if any typed read would throw
    assert settings["rig_autostart"] is True and settings["audio_protocol"] == SDRPP.protocol_tcp
    assert settings["modules"]["Radio"] == {"module": "radio", "enabled": True}

    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "config.json").write_text(json.dumps({
        "moduleInstances": {"Radio": "radio",                      # the old string form
                            "Recorder": {"module": "recorder", "enabled": False}},
        "streams": {"Radio": {"sink": "Audio"}},                 # volume and muted missing
        "source": "Airspy", "theme": "Dark", "menuWidth": 333}))
    (partial / "rigctl_server_config.json").write_text(json.dumps({
        "Rigctl Server": {"host": "0.0.0.0", "port": 4532, "tuning": True,
                          "autoStart": False, "vfo": "Radio"},     # recording, recorder missing
        "Other": {"host": "localhost", "port": 4533}}))
    (partial / "network_sink_config.json").write_text(json.dumps({
        "Radio": {"hostname": "localhost", "port": 7355}}))          # four keys missing
    with pytest.raises(SystemExit):
        load_settings(partial)                                     # SDR++ would not survive this
    SdrppConfigurator(partial).ensure(audio_host="127.0.0.1", audio_port=7400, sample_rate=48000,
                                      rigctl_host="127.0.0.1", rigctl_port=4600)
    settings = load_settings(partial)
    core = json.loads((partial / "config.json").read_text())
    assert core["moduleInstances"]["Radio"] == {"module": "radio", "enabled": True}
    assert core["moduleInstances"]["Recorder"] == {"module": "recorder", "enabled": False}
    assert core["streams"]["Radio"] == {"sink": "Network", "volume": 1.0, "muted": False}
    assert core["source"] == "Airspy" and core["theme"] == "Dark" and core["menuWidth"] == 333
    rig = json.loads((partial / "rigctl_server_config.json").read_text())
    assert rig["Rigctl Server"] == {"host": "127.0.0.1", "port": 4600, "tuning": True,
                                    "recording": False, "autoStart": True, "vfo": "Radio",
                                    "recorder": ""}
    assert rig["Other"] == {"host": "localhost", "port": 4533}
    sink = json.loads((partial / "network_sink_config.json").read_text())
    assert sink["Radio"] == {"hostname": "127.0.0.1", "port": 7400, "protocol": SDRPP.protocol_tcp,
                             "sampleRate": 48000.0, "stereo": False, "listening": True}
    assert settings["audio_port"] == 7400 and settings["rig_port"] == 4600


# ---- B4: Stop and Quit never wait on the receiver inline ----------------------------------

def test_stop_returns_at_once_while_dsd_neo_takes_its_time_and_the_recording_is_kept(
        receiver_config, store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DSD_SIGTERM_DELAY", "1.5")
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    monkeypatch.setenv("FAKE_SDRPP_WAV", bursts_wav(tmp_path / "bursts.wav"))
    receiver_config.receiver.digital = True
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app.start_session(name="slow stop")
    source = app._signal_source
    app.begin_capture()
    assert wait_for(lambda: source.process is not None, timeout=15)
    pid = source.process.pid
    assert wait_for(lambda: app.capture is not None and app.capture.detector.open, timeout=30), \
        f"no decoded speech reached the detector; dsd-neo said {source.stderr_tail[-3:]}"
    time.sleep(0.5)                                     # a transmission is open, being recorded
    began = time.monotonic()
    app.stop_session()
    elapsed = time.monotonic() - began
    assert elapsed < 0.5, f"Stop waited {elapsed:.2f}s on the receiver's processes"
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0, \
        "dsd-neo was killed rather than asked to end"
    assert app.receiver.source is None, "the released source is still the controller's"
    # The stopper thread owns the wait; the process ends on its own time.
    assert wait_for(lambda: subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0,
                    timeout=10), "dsd-neo never ended after being asked"
    assert wait_for(lambda: any(t.audio_path and pathlib.Path(t.audio_path).is_file()
                                for t in app.recent_transmissions()), timeout=20), \
        "the recording made before Stop is gone"
    began = time.monotonic()
    handle = app.receiver.begin_shutdown()
    assert time.monotonic() - began < 0.2
    assert app.receiver.begin_shutdown() is handle, "a second Quit started a second shutdown"
    assert app.close(wait=True, timeout=30)
    assert handle.settled and handle.error is None


def test_a_receiver_that_will_not_shut_down_is_reported_once_never_counted_done(
        receiver_config, store, monkeypatch):
    receiver_config.receiver.digital = True
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    published = []
    real_publish = app.events.publish
    app.events.publish = lambda kind, payload: (published.append((kind, payload)),
                                                real_publish(kind, payload))[1]
    app.start_session(name="stuck")
    app.begin_capture()
    assert wait_for(lambda: app._signal_source.process is not None, timeout=10)
    app.stop_session()
    process = app.receiver._process
    monkeypatch.setattr(process, "terminate", lambda timeout=5.0: None)   # SDR++ ignores us
    assert not app.close(wait=True, timeout=10), "Quit reported done while SDR++ stayed up"
    failures = [p for k, p in published if k == "audio-status" and p.get("kind") == "receiver-shutdown-failed"]
    assert len(failures) == 1 and "did not shut down" in failures[0]["message"]
    app.close(wait=True, timeout=5)
    failures = [p for k, p in published if k == "audio-status" and p.get("kind") == "receiver-shutdown-failed"]
    assert len(failures) == 1, "the same failure was reported again on the second Quit"
    monkeypatch.undo()
    process.terminate()


# ---- B5: closing guards come before receiver work -----------------------------------------

def test_start_while_quitting_is_refused_before_sdrpp_is_launched(receiver_config, store):
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app._closing = True
    with pytest.raises(ProcessingBusy):
        app.start_session(name="late")
    assert getattr(app, "_receiver", None) is None or app._receiver._process is None
    assert sdrpp_log() == "", "SDR++ was started for a run that could not begin"
    assert not sdrpp_processes(FAKE_SDRPP, receiver_config.receiver.sdrpp_root)


# ---- B6: a running SDR++ without rigctl is not duplicated or rewritten ----------------------

def test_a_running_sdrpp_whose_rigctl_is_off_is_neither_duplicated_nor_rewritten(receiver_config,
                                                                                 monkeypatch):
    root = pathlib.Path(receiver_config.receiver.sdrpp_root)
    SdrppConfigurator(root).ensure(audio_host="127.0.0.1", audio_port=receiver_config.receiver.audio_port,
                                   sample_rate=48000, rigctl_host="127.0.0.1",
                                   rigctl_port=receiver_config.receiver.rigctl_port)
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.glob("*.json")}
    env = dict(os.environ, FAKE_SDRPP_NO_RIGCTL="1")
    theirs = subprocess.Popen([sys.executable, FAKE_SDRPP, "--root", str(root)], env=env)
    try:
        assert wait_for(lambda: "rigctl not started" in sdrpp_log(), timeout=10)
        controller = ReceiverController(receiver_config)
        with pytest.raises(ReceiverUnavailable) as failure:
            controller.ensure_sdrpp(timeout=3.0)
        assert "Rigctl Server" in str(failure.value) and "Module Manager" in str(failure.value)
        assert controller._process is None, "a second SDR++ was started on the same receiver"
        assert sdrpp_processes(FAKE_SDRPP, str(root)) == [theirs.pid]
        after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.glob("*.json")}
        assert after == before, "the running SDR++'s files were rewritten under it"
        controller.shutdown()
        assert theirs.poll() is None, "the operator's own SDR++ was stopped"
    finally:
        theirs.terminate()
        theirs.wait(timeout=5)


# ---- B7: discovery off the GUI thread; four facts kept apart; loopback only -----------------

def test_status_never_blocks_on_slow_discovery_and_keeps_its_four_facts_apart(receiver_config,
                                                                              monkeypatch):
    def slow_probe():
        time.sleep(1.0)
        return True
    monkeypatch.setattr(controller_module, "rtl_sdr_present", slow_probe)
    controller = ReceiverController(receiver_config)
    began = time.monotonic()
    status = controller.status()
    assert time.monotonic() - began < 0.4, "status waited for the USB probe"
    assert status["rtl_sdr_present"] is None and status["usb_probed"] is False
    assert status["sdrpp"] == FAKE_SDRPP and status["sdrpp_running"] is False
    assert status["control_connected"] is False and status["audio_receiving"] is False
    controller.discovery.wait()
    status = controller.status()
    assert status["rtl_sdr_present"] is True and status["usb_probed"] is True
    try:
        source = controller.open_source()
        status = controller.status()
        assert status["sdrpp_running"] and status["control_connected"]
        assert status["audio_receiving"] is False, "no audio has arrived yet"
        source.start()
        assert wait_for(lambda: controller.audio_receiving, timeout=10)
        assert controller.status()["audio_receiving"] is True
    finally:
        controller.shutdown()


def test_only_a_local_receiver_is_used(receiver_config):
    receiver_config.receiver.audio_host = "10.0.0.5"
    status = receiver_status(receiver_config)
    assert not status["available"] and any("not this computer" in p for p in status["problems"])
    controller = ReceiverController(receiver_config)
    with pytest.raises(ReceiverError):
        controller.open_source()
    assert controller._process is None and sdrpp_log() == ""
    receiver_config.receiver.allow_remote_receiver = True
    assert not any("not this computer" in p for p in receiver_status(receiver_config)["problems"])


# ---- C8: tuning done in the SDR++ window is respected --------------------------------------

def test_start_adopts_the_frequency_set_in_the_sdrpp_window_and_follows_later_changes(receiver_config):
    receiver_config.receiver.frequency_hz = 155.1e6            # an older Tune-receiver choice
    controller = ReceiverController(receiver_config)
    statuses = []
    controller.on_status = lambda kind, message: statuses.append((kind, message))
    try:
        controller.ensure_sdrpp()
        controller.pending_tune = False                        # the operator did not ask again
        gui_tune(162_550_000, "FM", 12500)                     # ... they tuned in SDR++ instead
        assert wait_for(lambda: "gui tune 162550000" in sdrpp_log(), timeout=5)
        source = controller.open_source()
        assert controller.tuning.confirmed_hz == 162_550_000.0, \
            "Start put the stored 155.1 MHz back over the operator's 162.55 MHz"
        assert controller.tuning.requested_hz is None
        assert source.metadata().tuned_frequency_hz == 162_550_000.0
        source.start()
        assert source.read(timeout=5.0) is not None
        gui_tune(146_520_000)                                  # a later turn of the dial
        assert wait_for(lambda: controller.tuning.confirmed_hz == 146_520_000.0, timeout=5), \
            "the change made in the SDR++ window never reached the metadata"
        assert wait_for(lambda: source.metadata().tuned_frequency_hz == 146_520_000.0, timeout=2)
        assert any(kind == "tuned" and "146.5200" in message for kind, message in statuses)
        assert controller.tuning.epoch == 1
        assert not any("rigctl F" in line for line in sdrpp_log().splitlines()), \
            "BabelFishR tuned the radio although the operator asked for nothing"
    finally:
        controller.shutdown()


# ---- C9: a retune is a recording boundary --------------------------------------------------

def test_a_block_read_across_a_retune_is_dropped_and_the_boundary_carries_the_old_tuning():
    sink = RawSink()
    tuning = TuningState()
    tuning.confirmed_hz = 462_562_500.0
    tuning.mode = "FM"
    source = PcmTcpSource("127.0.0.1", sink.port, tuning, sample_rate=RATE)
    try:
        source.start()
        sink.send_seconds(0.02)                                  # exactly one block
        first = source.read(timeout=5.0)
        assert first is not None and first.samples.size == source.block_frames
        # The reader is now blocked inside recv() for the next block.
        time.sleep(0.2)
        source.retuned("retune")                                 # the controller confirmed a change
        tuning.confirmed_hz = 155_160_000.0
        tuning.epoch += 1
        sink.send_seconds(0.02)                                  # read begun before the boundary
        marker = source.read(timeout=5.0)
        assert isinstance(marker, StreamBoundary) and marker.reason == "retune"
        assert marker.metadata.tuned_frequency_hz == 462_562_500.0, \
            "the boundary carries the tuning that is ending, not the new one"
        assert marker.metadata.extra["tuning_epoch"] == 0
        crossed = source.read(timeout=0.7)
        assert crossed is None, "audio read across the retune was delivered under the new frequency"
        sink.send_seconds(0.02)
        after = source.read(timeout=5.0)
        assert after is not None and not isinstance(after, StreamBoundary)
        assert source.metadata().tuned_frequency_hz == 155_160_000.0
        assert source.metadata().extra["tuning_epoch"] == 1
        assert source.metadata().center_frequency_hz is None, \
            "rigctl reports the VFO, not the RF centre frequency"
    finally:
        source.stop()
        sink.close()


def test_a_retune_during_a_transmission_closes_it_under_the_frequency_it_was_heard_on(
        receiver_config, store, tmp_path, monkeypatch):
    # Three seconds of talk then a gap, looped: a transmission is open for
    # long enough to retune in the middle of it.
    monkeypatch.setenv("FAKE_SDRPP_WAV", bursts_wav(tmp_path / "bursts.wav", talk=3.0, gap=1.5))
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app.start_session(name="retune")
    try:
        app.begin_capture()
        assert wait_for(lambda: app.capture is not None and app.capture.detector.open, timeout=20)
        time.sleep(0.8)
        assert app.capture.detector.open
        result = app.receiver.tune(frequency_hz=155.16e6)
        assert result["confirmed_hz"] == 155_160_000.0 and result["changed"]
        assert wait_for(lambda: len(app.recent_transmissions()) >= 1, timeout=15), \
            "the retune did not close the transmission that was open"
        first = app.recent_transmissions()[-1]
        assert first.frequency_mhz == pytest.approx(462.5625), \
            "the transmission heard on 462.5625 MHz was labelled with the new frequency"
        assert first.signal_metadata["sdrpp"]["extra"]["tuning_epoch"] == 0
        assert first.duration < 3.0, "buffered old-frequency audio was kept across the boundary"
        time.sleep(1.5)
    finally:
        app.stop_session()
    assert wait_for(lambda: len(app.recent_transmissions()) >= 2, timeout=20)
    latest = app.recent_transmissions()[0]
    assert latest.frequency_mhz == pytest.approx(155.16)
    assert latest.signal_metadata["sdrpp"]["extra"]["tuning_epoch"] == 1
    app.close(wait=True, timeout=30)


# ---- D10: per-slot, per-call metadata -------------------------------------------------------

def test_slot_two_events_never_replace_slot_one_identifiers_and_encryption_is_per_call():
    source = DecodedVoiceSource("dsd-neo", "127.0.0.1", 1, TuningState(), protocol_flag="-fs", slot=1)
    note = source._note_event
    note("07:00:00 Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC1")
    note("TGT=100 SRC=1001")
    meta = source.metadata()
    assert meta.talkgroup == "100" and meta.unit_id == "1001" and meta.extra["color_code"] == "02"
    note("07:00:00 Sync: +DMR   slot1  [SLOT2] | Color Code=02 | VC1")
    note("TGT=200 SRC=2002")                                       # the other conversation
    note(" LDU1 ALG ID: 0x84 KEY ID: 0x0001")                      # ... and it is encrypted
    meta = source.metadata()
    assert meta.talkgroup == "100" and meta.unit_id == "1001", "slot 2's identifiers replaced slot 1's"
    assert meta.extra["encrypted"] is False, "slot 2's encryption was pinned on slot 1"
    assert source.decoder["active_slot"] == 2
    note("07:00:01 Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC2")
    note(" LDU1 ALG ID: 0x84 KEY ID: 0x0001")
    assert source.metadata().extra["encrypted"] is True
    note(" LDU2 ALG ID: 0x80 KEY ID: 0x0000")                      # clear again, same call
    assert source.metadata().extra["encrypted"] is False, "the encrypted flag stuck after a clear ALG ID"
    # A new call on slot 1 starts clean: nothing from the previous call is kept.
    source.calls.note("07:00:05 Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC1",
                      now=time.monotonic() + 10.0)
    fresh = source.calls.current(now=time.monotonic() + 10.0)
    assert fresh.get("talkgroup") is None and fresh.get("encrypted") is False
    # Between calls nothing is attached at all.
    assert source.calls.current(now=time.monotonic() + 60.0) == {}


# ---- D11: an idle decoder emits nothing; time and calls stay honest ---------------------------

def test_silence_between_calls_is_kept_on_the_clock_and_calls_stay_separate(receiver_config, store,
                                                                            tmp_path, monkeypatch):
    burst = np.concatenate([tone(0.6), np.zeros(int(1.2 * RATE)), tone(0.6), np.zeros(int(1.6 * RATE))])
    monkeypatch.setenv("FAKE_SDRPP_WAV", write_wav(tmp_path / "bursts.wav", burst))
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.receiver.digital = True
    receiver_config.receiver.digital_protocol = "dmr-dual"
    controller = ReceiverController(receiver_config)
    try:
        source = controller.open_source()
        source.start()
        began = time.monotonic()
        blocks = []
        while time.monotonic() - began < 4.5:
            block = source.read(timeout=1.0)
            if block is not None and not isinstance(block, StreamBoundary):
                blocks.append(block)
        elapsed = time.monotonic() - began
        voice = [b for b in blocks if float(np.max(np.abs(b.samples))) > 0.02]
        quiet = [b for b in blocks if float(np.max(np.abs(b.samples))) <= 0.02]
        assert voice, f"no decoded audio arrived; dsd-neo said {source.stderr_tail[-3:]}"
        assert quiet, "the idle time between calls produced no audio at all: the detector cannot close a call"
        span = blocks[-1].offset - blocks[0].offset
        assert span > elapsed - 1.5, f"the stream's clock covers {span:.1f}s of {elapsed:.1f}s: time was derived from decoded samples"
        offsets = [b.offset for b in blocks]
        assert offsets == sorted(offsets), "timestamps went backwards"
        # Two bursts 1.2 s apart are two calls, not one: a run of quiet blocks sits between them.
        loud = np.array([float(np.max(np.abs(b.samples))) > 0.02 for b in blocks])
        runs = np.flatnonzero(np.diff(loud.astype(int)) != 0)
        assert len(runs) >= 3, "the calls were run together"
    finally:
        controller.shutdown()


def test_an_idle_decoder_does_not_end_the_stream(receiver_config, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_WAV", write_wav(tmp_path / "quiet.wav", np.zeros(RATE)))
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.receiver.digital = True
    controller = ReceiverController(receiver_config)
    try:
        source = controller.open_source()
        source.start()
        blocks = []
        began = time.monotonic()
        while time.monotonic() - began < 2.5:
            block = source.read(timeout=0.5)
            if block is not None and not isinstance(block, StreamBoundary):
                blocks.append(block)
        assert source.running and not source.finished, "a quiet channel ended the stream"
        assert source.process.poll() is None
        assert blocks and all(float(np.max(np.abs(b.samples))) == 0.0 for b in blocks), \
            "the clock is kept with synthesised silence while dsd-neo says nothing"
        assert blocks[-1].offset - blocks[0].offset > 1.5
        assert not controller.audio_receiving, \
            "synthesised silence must not be reported as audio arriving from the receiver"
    finally:
        controller.shutdown()


# ---- D12: the producer lost while dsd-neo lives ----------------------------------------------

def test_losing_sdrpps_audio_while_dsd_neo_lives_is_told_apart_from_the_decoder_dying(
        receiver_config, monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_DROP_AUDIO_AFTER", "1.5")
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.receiver.digital = True
    controller = ReceiverController(receiver_config)
    statuses = []
    controller.on_status = lambda kind, message: statuses.append(kind)
    try:
        source = controller.open_source()
        source.start()
        assert wait_for(lambda: "upstream-lost" in statuses, timeout=10), statuses
        assert source.process.poll() is None, "dsd-neo itself is alive; only its input went"
        assert "decoder-exited" not in statuses and "disconnected" not in statuses
        assert wait_for(lambda: "upstream-restored" in statuses, timeout=10), statuses
        assert source.running and not source.finished
        assert source.decoder["upstream_lost"] is False
        assert wait_for(lambda: source.receiving, timeout=10)
    finally:
        controller.shutdown()


def test_when_dsd_neo_gives_the_receiver_up_its_other_audio_is_never_recorded(receiver_config,
                                                                             monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_AUDIO_STOP_AFTER", "1.0")
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.receiver.digital = True
    controller = ReceiverController(receiver_config)
    statuses = []
    controller.on_status = lambda kind, message: statuses.append((kind, message))
    try:
        source = controller.open_source()
        source.start()
        assert wait_for(lambda: any(k == "receiver-lost" for k, _ in statuses), timeout=15), statuses
        message = [m for k, m in statuses if k == "receiver-lost"][0]
        assert "gave up" in message and "nothing else is recorded" in message
        assert source.finished and not source.running
        assert source.decoder["upstream_abandoned"] is True
        assert wait_for(lambda: source.process.poll() is not None, timeout=10), \
            "dsd-neo kept running on an input that is not the receiver"
        # Whatever it produced from its own input never arrives here.
        loud_after = 0
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            block = source.read(timeout=0.3)
            if block is not None and not isinstance(block, StreamBoundary) \
                    and float(np.max(np.abs(block.samples))) > 0.1:
                loud_after += 1
        assert loud_after == 0, "audio from dsd-neo's substitute input was taken as receiver audio"
        assert controller.status()["sdrpp_running"], "SDR++ itself is still up; only its audio went"
    finally:
        controller.shutdown()


# ---- D13: bounded queues with the loss reported --------------------------------------------

def test_a_slow_consumer_bounds_the_queue_and_the_loss_is_reported(monkeypatch):
    monkeypatch.setattr(stream_module, "QUEUE_SECONDS", 0.5)
    sink = RawSink()
    tuning = TuningState()
    statuses = []
    source = PcmTcpSource("127.0.0.1", sink.port, tuning, sample_rate=RATE,
                          on_status=lambda kind, message: statuses.append(kind))
    try:
        source.start()
        sink.send_seconds(6.0)                                    # nobody reads for a while
        assert wait_for(lambda: source.dropped_frames > 0, timeout=10), "the queue grew without bound"
        assert source._queue.qsize() <= source._queue.maxsize <= 30
        assert "audio-dropped" in statuses
        drained = 0
        while source.read(timeout=0.3) is not None:
            drained += 1
        assert drained <= 30
        assert source.dropped_frames >= 4 * RATE
        assert source.metadata().extra["dropped_frames"] == source.dropped_frames
        sink.send_seconds(0.1)
        assert wait_for(lambda: "audio-resumed" in statuses, timeout=5)
    finally:
        source.stop()
        sink.close()


# ---- the window: guards and the filter width -------------------------------------------------

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


def test_the_window_guards_receiver_work_when_quitting_and_offers_the_measured_width(
        qt_app, receiver_config, store, monkeypatch):
    from PySide6 import QtWidgets

    from babelfishr.ui.main_window import MainWindow

    app = mock_app(receiver_config, store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    app._closing = True
    window._open_receiver()
    window._tune_receiver()
    pump(qt_app)
    assert getattr(app, "_receiver", None) is None or app._receiver._process is None
    assert sdrpp_log() == "", "the receiver was started while BabelFishR was quitting"
    app._closing = False

    seen = {}

    def inspect_and_accept(self):
        dialog = window.tune_dialog
        width = dialog.findChild(QtWidgets.QComboBox, "filterWidth")
        seen["before"] = width.currentData()
        digital = [box for box in dialog.findChildren(QtWidgets.QCheckBox) if "Digital" in box.text()][0]
        digital.setChecked(True)
        seen["after_tick"] = width.currentData()
        return QtWidgets.QDialog.Accepted
    monkeypatch.setattr(QtWidgets.QDialog, "exec", inspect_and_accept)
    window._tune_receiver()
    pump(qt_app)
    assert seen["before"] == SDRPP.analog_bandwidth_hz
    assert seen["after_tick"] == SDRPP.digital_voice_bandwidth_hz == 20000
    assert receiver_config.receiver.bandwidth_hz == 20000 and receiver_config.receiver.digital
    assert app.receiver.pending_tune, "SDR++ is not running: the choice waits for it"
    window.close()
    pump(qt_app, 30)
