"""The SDR receiver path: SDR++ receives, DSD-neo decodes, BabelFishR records.

Every component boundary here is the one BabelFishR really uses - the files
SDR++ reads at start, its rigctl line protocol, its TCP network sink's raw
int16 stream, dsd-neo's command line, stdout stream and stderr events - but
the components themselves are stand-ins (tests/stubs/fake_sdrpp.py,
tests/stubs/fake_dsd_stream.py) unless BABELFISHR_DSD_NEO names a real
dsd-neo and BABELFISHR_DSD_FIXTURE_WAV a 48 kHz discriminator-audio fixture,
in which case the digital path runs the actual decoder. Nothing here uses a
receiver, an antenna or a Mac.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import wave

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState, Provenance
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.receiver import (DSD_NEO, SDRPP, ReceiverController, ReceiverError,
                                 ReceiverUnavailable, receiver_status)
from babelfishr.receiver.sdrpp import RigctlClient, RigctlError, SdrppConfigurator, find_sdrpp
from babelfishr.receiver.stream import DecodedVoiceSource, PcmTcpSource, TuningState
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


@pytest.fixture
def fixture_48k(tmp_path):
    return standard_fixture(RATE).write(str(tmp_path / "fixture48k.wav"))


@pytest.fixture
def receiver_config(config, tmp_path, fixture_48k, monkeypatch):
    """A config whose receiver is the fake SDR++ started by BabelFishR."""
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
    return config


def sdrpp_log(config) -> str:
    path = os.environ.get("FAKE_SDRPP_LOG", "")
    return pathlib.Path(path).read_text() if path and os.path.exists(path) else ""


def mock_app(config, store):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


# ---- SDR++ configuration -------------------------------------------------------------

def test_the_configurator_writes_exactly_what_monitoring_needs_and_keeps_the_rest(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({
        "moduleInstances": {"Audio Sink": {"module": "audio_sink", "enabled": True},
                            "Recorder": {"module": "recorder", "enabled": True}},
        "streams": {"Radio": {"muted": False, "sink": "Audio", "volume": 0.7}},
        "source": "Airspy", "theme": "Dark", "vfoColors": {"Radio": "#FF0000"}}))
    (root / "network_sink_config.json").write_text(json.dumps({
        "Radio": {"hostname": "localhost", "port": 7355, "protocol": SDRPP.protocol_udp,
                  "sampleRate": 48000.0, "stereo": True, "listening": False},
        "Other": {"hostname": "10.0.0.5", "port": 9000}}))
    changes = SdrppConfigurator(root).ensure(audio_host="127.0.0.1", audio_port=7400,
                                             sample_rate=48000, rigctl_host="127.0.0.1",
                                             rigctl_port=4600)
    core = json.loads((root / "config.json").read_text())
    assert core["moduleInstances"]["Network Sink"] == {"module": "network_sink", "enabled": True}
    assert core["moduleInstances"]["Rigctl Server"] == {"module": "rigctl_server", "enabled": True}
    assert core["moduleInstances"]["RTL-SDR Source"] == {"module": "rtl_sdr_source", "enabled": True}
    assert core["moduleInstances"]["Recorder"] == {"module": "recorder", "enabled": True}
    assert core["streams"]["Radio"] == {"muted": False, "sink": "Network", "volume": 0.7}
    assert core["source"] == "Airspy", "the operator's own source choice was overwritten"
    assert core["theme"] == "Dark" and core["vfoColors"] == {"Radio": "#FF0000"}
    sink = json.loads((root / "network_sink_config.json").read_text())
    assert sink["Radio"] == {"hostname": "127.0.0.1", "port": 7400, "protocol": SDRPP.protocol_tcp,
                             "sampleRate": 48000.0, "stereo": False, "listening": True}
    assert sink["Other"] == {"hostname": "10.0.0.5", "port": 9000}
    rig = json.loads((root / "rigctl_server_config.json").read_text())
    assert rig["Rigctl Server"] == {"host": "127.0.0.1", "port": 4600, "tuning": True,
                                    "autoStart": True, "vfo": "Radio"}
    assert (root / "config.before-babelfishr.json").exists()
    assert json.loads((root / "config.before-babelfishr.json").read_text())["streams"]["Radio"]["sink"] == "Audio"
    assert (root / "network_sink_config.before-babelfishr.json").exists()
    assert not (root / "rigctl_server_config.before-babelfishr.json").exists(), \
        "a file that did not exist has no backup to make"
    assert len(changes) >= 6
    # Idempotent: a second pass changes nothing and touches no backup.
    stamp = (root / "config.before-babelfishr.json").read_text()
    assert SdrppConfigurator(root).ensure(audio_host="127.0.0.1", audio_port=7400, sample_rate=48000,
                                          rigctl_host="127.0.0.1", rigctl_port=4600) == []
    assert (root / "config.before-babelfishr.json").read_text() == stamp


def test_a_fresh_root_gets_the_rtl_sdr_source_only_because_none_was_chosen(tmp_path):
    root = tmp_path / "fresh"
    SdrppConfigurator(root).ensure(audio_host="127.0.0.1", audio_port=7355, sample_rate=48000,
                                   rigctl_host="127.0.0.1", rigctl_port=4532)
    core = json.loads((root / "config.json").read_text())
    assert core["source"] == "RTL-SDR"
    assert not list(root.glob("*.before-babelfishr.json"))


def test_find_sdrpp_prefers_the_configured_path_and_never_invents_one(tmp_path):
    assert find_sdrpp(str(tmp_path / "nowhere")) is None
    exe = tmp_path / "sdrpp"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert find_sdrpp(str(exe)) == str(exe)
    app = tmp_path / "SDR++.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "sdrpp").write_text("")
    assert find_sdrpp(str(tmp_path / "SDR++.app")) == str(app / "sdrpp")


# ---- rigctl -------------------------------------------------------------------------

def test_the_rigctl_client_speaks_sdrpps_protocol(receiver_config, tmp_path):
    controller = ReceiverController(receiver_config)
    try:
        controller.ensure_sdrpp()
        rig = RigctlClient("127.0.0.1", receiver_config.receiver.rigctl_port)
        rig.connect()
        rig.set_frequency(462.5625e6)
        assert rig.frequency() == 462562500.0
        rig.set_mode("FM", 12500)
        assert rig.mode() == ("FM", 12500)
        with pytest.raises(RigctlError):
            rig.set_mode("DMR")                       # not one of SDR++'s modes
        rig.start()
        rig.close()
        log = sdrpp_log(receiver_config)
        assert "rigctl F 462562500" in log and "rigctl f" in log
        assert "rigctl M FM 12500" in log and "rigctl \\start" in log and "rigctl q" in log
        assert '"rig_autostart": true' in log and '"audio_protocol": 0' in log
        assert '"sink_selected": "Network"' in log
    finally:
        controller.shutdown()


def test_a_refused_tune_is_an_error_not_a_guess(receiver_config, monkeypatch):
    monkeypatch.setenv("FAKE_SDRPP_REFUSE_TUNE", "1")
    controller = ReceiverController(receiver_config)
    try:
        controller.ensure_sdrpp()
        with pytest.raises(ReceiverError):
            controller.tune()
        assert controller.tuning.confirmed_hz is None
    finally:
        controller.shutdown()


# ---- the analog stream ---------------------------------------------------------------

def test_the_pcm_stream_arrives_as_float_blocks_at_the_sink_rate_and_stops_cleanly(receiver_config):
    controller = ReceiverController(receiver_config)
    statuses = []
    controller.on_status = lambda kind, message: statuses.append(kind)
    try:
        source = controller.open_source()
        assert isinstance(source, PcmTcpSource) and source.sample_rate == RATE
        source.start()
        blocks = []
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and sum(b.samples.size for b in blocks) < 4 * RATE:
            block = source.read(timeout=1.0)
            if block is not None:
                blocks.append(block)
        assert blocks, "nothing arrived from the fake sink"
        assert all(b.sample_rate == RATE and b.samples.dtype == np.float64 for b in blocks)
        assert max(float(np.max(np.abs(b.samples))) for b in blocks) <= 1.0
        assert any(float(np.max(np.abs(b.samples))) > 0.05 for b in blocks), "only silence arrived"
        assert blocks[1].offset > blocks[0].offset
        meta = source.metadata()
        assert meta.provenance is Provenance.SDR and meta.tuned_frequency_hz == 462562500.0
        assert meta.extra["requested_frequency_hz"] == 462.5625e6 and meta.extra["receiver_confirmed"]
        assert meta.modulation == "FM" and meta.rssi_dbm is None and meta.snr_db is None
        assert "connected" in statuses and "tuned" in statuses
        source.stop()
        assert not source.running
        assert source.read(timeout=0.2) is None
    finally:
        controller.shutdown()


def test_a_retune_drops_audio_buffered_under_the_previous_frequency(receiver_config):
    controller = ReceiverController(receiver_config)
    try:
        source = controller.open_source()
        source.start()
        assert wait_for(lambda: source._queue.qsize() >= 3, timeout=5)
        buffered = source._queue.qsize()
        assert buffered >= 3
        result = controller.tune(frequency_hz=155.16e6)
        assert result["confirmed_hz"] == 155160000.0
        # Whatever was queued is gone; what arrives next is under the new tuning.
        block = source.read(timeout=2.0)
        assert block is not None
        assert source.metadata().tuned_frequency_hz == 155160000.0
        assert source._queue.qsize() <= 2
    finally:
        controller.shutdown()


def test_the_stream_ending_is_reported_and_never_replaced(receiver_config):
    controller = ReceiverController(receiver_config)
    statuses = []
    controller.on_status = lambda kind, message: statuses.append((kind, message))
    try:
        source = controller.open_source()
        source.start()
        assert source.read(timeout=5.0) is not None
        controller._process.terminate()                      # SDR++ quits under us
        assert wait_for(lambda: source.finished, timeout=5)
        assert not source.running
        assert any(kind == "disconnected" for kind, _ in statuses)
        assert source.read(timeout=0.2) is None
    finally:
        controller.shutdown()


# ---- SDR++ lifecycle -----------------------------------------------------------------

def test_without_sdrpp_monitoring_refuses_and_nothing_else_is_recorded(config, store, tmp_path):
    config.receiver.sdrpp_path = str(tmp_path / "no-such-sdrpp")
    config.receiver.rigctl_port = free_port()
    config.receiver.audio_port = free_port()
    config.record_receiver_input(save=False)
    app = mock_app(config, store)
    assert app.input_status()["state"] == "receiver"
    assert not app.input_status()["receiver"]["available"]
    with pytest.raises(ReceiverUnavailable):
        app.start_session(name="run")
    assert app.capture is None and app.session is None
    # Nothing was opened in its place: no session row remains open.
    assert all(row[0] is not None for row in store._conn.execute("SELECT ended_at FROM sessions"))


def test_an_sdrpp_already_running_is_attached_to_and_left_running(receiver_config):
    first = ReceiverController(receiver_config)
    first.ensure_sdrpp()
    process = first._process
    try:
        second = ReceiverController(receiver_config)
        second.ensure_sdrpp()
        assert second._process is None and "attached to a running SDR++" in second.notes
        second.shutdown()
        assert process.alive, "shutting down the attached controller stopped SDR++"
    finally:
        first.shutdown()
    assert not process.alive, "the SDR++ we started was not stopped by our shutdown"


# ---- end to end: analog ---------------------------------------------------------------

def test_analog_reception_is_recorded_transcribed_and_translated(receiver_config, store, tmp_path):
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    session = app.start_session(name="sdr run")
    try:
        assert session.audio_device.startswith("SDR++ receiver (analog)")
        app.begin_capture()
        assert wait_for(lambda: len([t for t in app.recent_transmissions()
                                     if t.state is ProcessingState.COMPLETE]) >= 2, timeout=40)
    finally:
        app.stop_session()
    txs = app.recent_transmissions()
    done = [t for t in txs if t.state is ProcessingState.COMPLETE]
    assert len(done) >= 2
    for tx in done:
        assert tx.audio_path and pathlib.Path(tx.audio_path).is_file()
        with wave.open(tx.audio_path) as handle:
            assert handle.getframerate() == RATE and handle.getnchannels() == 1
        assert tx.transcript and tx.session_id == session.id
        assert tx.frequency_mhz == pytest.approx(462.5625)
        assert tx.frequency_provenance is Provenance.SDR
        assert tx.signal_metadata["sdrpp"]["extra"]["receiver_confirmed"] is True
        assert tx.signal_metadata["sdrpp"]["extra"]["path"] == "analog"
    assert any(t.translation for t in done)
    log = sdrpp_log(receiver_config)
    assert "rigctl F 462562500" in log and "rigctl \\start" in log
    process = app.receiver._process
    assert process.alive, "SDR++ should stay for the operator while BabelFishR runs"
    app.close(wait=True, timeout=30)
    assert not process.alive, "the SDR++ we started outlived Quit"


# ---- end to end: digital ----------------------------------------------------------------

def test_digital_reception_runs_through_the_decoder_and_takes_one_slot(receiver_config, store):
    receiver_config.receiver.digital = True
    receiver_config.receiver.digital_protocol = "dmr-dual"
    receiver_config.receiver.digital_slot = 1
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    session = app.start_session(name="digital run")
    try:
        source = app._signal_source
        assert isinstance(source, DecodedVoiceSource) and source.sample_rate == DSD_NEO.output_sample_rate
        assert source.command()[1:8] == ["-i", f"tcp:127.0.0.1:{receiver_config.receiver.audio_port}",
                                         "-s", "48000", "-fs", "-V", "1"]
        app.begin_capture()
        assert wait_for(lambda: source.process is not None and source.process.poll() is None, timeout=5)
        pid = source.process.pid
        assert wait_for(lambda: len([t for t in app.recent_transmissions()
                                     if t.state is ProcessingState.COMPLETE]) >= 1, timeout=40)
    finally:
        app.stop_session()
    done = [t for t in app.recent_transmissions() if t.state is ProcessingState.COMPLETE]
    assert done
    tx = done[0]
    with wave.open(tx.audio_path) as handle:
        assert handle.getframerate() == 8000 and handle.getnchannels() == 1
    assert tx.transcript
    assert tx.protocol == "DMR" and tx.talkgroup == "2501" and tx.unit_id == "1234567"
    record = tx.signal_metadata["sdrpp+dsd-neo"]
    assert record["extra"]["path"] == "digital" and record["extra"]["slot"] == 1
    assert record["extra"]["encrypted"] is False
    assert tx.frequency_provenance is Provenance.SDR
    # dsd-neo went with the run.
    assert wait_for(lambda: not pathlib.Path(f"/proc/{pid}").exists(), timeout=10) or \
        subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0
    app.close(wait=True, timeout=30)


def test_the_decoder_exiting_is_reported_and_no_other_input_takes_over(receiver_config, store):
    receiver_config.receiver.digital = True
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    events = []
    app.events.publish = (lambda publish: (lambda kind, payload: (events.append((kind, payload)),
                                                                  publish(kind, payload))[1]))(app.events.publish)
    app.start_session(name="run")
    try:
        source = app._signal_source
        app.begin_capture()
        assert wait_for(lambda: source.process is not None, timeout=5)
        source.process.kill()
        assert wait_for(lambda: source.finished, timeout=10)
        kinds = [payload.get("kind") for kind, payload in events if kind == "audio-status"]
        assert "decoder-exited" in kinds and "disconnected" in kinds
        assert app.capture is not None                       # the run is the operator's to stop
    finally:
        app.stop_session()
    app.close(wait=True, timeout=30)


REAL_DSD = os.environ.get("BABELFISHR_DSD_NEO", "")
REAL_FIXTURE = os.environ.get("BABELFISHR_DSD_FIXTURE_WAV", "")


@pytest.mark.skipif(not (REAL_DSD and REAL_FIXTURE),
                    reason="set BABELFISHR_DSD_NEO (a real dsd-neo) and BABELFISHR_DSD_FIXTURE_WAV "
                           "(48 kHz mono discriminator audio of a digital voice call)")
def test_real_dsd_neo_decodes_a_digital_fixture_into_speech_for_the_pipeline(receiver_config, store,
                                                                            monkeypatch):
    """The actual decoder on genuine digital audio: dsd-neo's DMR voice IQ
    fixture, FM-discriminated to 48 kHz - the shape SDR++'s network sink
    delivers. Proves the receiver→decoder→BabelFishR software path, not
    radio reception."""
    monkeypatch.setenv("FAKE_SDRPP_WAV", REAL_FIXTURE)
    monkeypatch.setenv("FAKE_SDRPP_LOOP", "1")
    receiver_config.analysis.dsd_path = REAL_DSD
    receiver_config.receiver.digital = True
    receiver_config.receiver.digital_protocol = "dmr-dual"
    receiver_config.receiver.digital_slot = 1
    receiver_config.record_receiver_input(save=False)
    app = mock_app(receiver_config, store)
    app.start_session(name="real dsd")
    source = app._signal_source
    try:
        app.begin_capture()
        # The 2 s fixture is looped, so the decoded speech is nearly
        # continuous: one call as far as the detector is concerned. Receive
        # for a while, then stop; the transmission closes with the run.
        assert wait_for(lambda: source.decoder.get("sync_lines", 0) >= 20, timeout=30), \
            f"dsd-neo never synchronised; it said: {source.stderr_tail[-5:]}"
        time.sleep(6)
    finally:
        app.stop_session()
    assert wait_for(lambda: any(t.state is ProcessingState.COMPLETE for t in app.recent_transmissions()),
                    timeout=60), f"no transmission from decoded speech; dsd-neo said: {source.stderr_tail[-5:]}"
    txs = app.recent_transmissions()
    assert source.decoder["protocol"] == "DMR" and source.decoder["sync_lines"] > 0
    tx = [t for t in txs if t.state is ProcessingState.COMPLETE][0]
    with wave.open(tx.audio_path) as handle:
        frames = handle.getnframes()
        assert handle.getframerate() == 8000 and handle.getnchannels() == 1 and frames > 8000 * 2
        data = np.frombuffer(handle.readframes(frames), dtype=np.int16).astype(float) / 32768
    assert float(np.sqrt(np.mean(data ** 2))) > 0.01, "the recorded decode is silent"
    assert tx.transcript, "the decoded speech was not transcribed"
    assert tx.protocol == "DMR"
    assert tx.signal_metadata["sdrpp+dsd-neo"]["extra"]["color_code"] == "02"
    assert tx.frequency_provenance is Provenance.SDR and tx.frequency_mhz == pytest.approx(462.5625)
    app.close(wait=True, timeout=30)


# ---- decoder events -------------------------------------------------------------------------

def test_decoder_events_become_metadata_and_clear_p25_is_not_called_encrypted(tmp_path):
    source = DecodedVoiceSource("dsd-neo", "127.0.0.1", 1, TuningState(), protocol_flag="-f1")
    for line in ("06:13:40 Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC1",
                 "TGT=2501 SRC=1234567",
                 "\x1b[33m LDU2 ALG ID: 0x80 KEY ID: 0x0000 MI: 0x0000000000000000\x1b[0m",
                 "NAC: 293"):
        source._note_event(line)
    meta = source.metadata()
    assert meta.protocol == "DMR" and meta.talkgroup == "2501" and meta.unit_id == "1234567"
    assert meta.extra["color_code"] == "02" and meta.extra["nac"] == "293"
    assert meta.extra["encrypted"] is False, "P25 ALG ID 0x80 means clear, not encrypted"
    assert meta.extra["slot"] == 1 and source.decoder["active_slot"] == 1
    assert meta.provenance is Provenance.UNKNOWN, "nothing confirmed by the receiver yet"
    source._note_event("Voice frame ENC: encrypted, KEY ID 42")
    assert source.metadata().extra["encrypted"] is True
    other = DecodedVoiceSource("dsd-neo", "127.0.0.1", 1, TuningState())
    other._note_event(" LDU1 ALG ID: 0x84 KEY ID: 0x0001")             # AES-256: encrypted
    assert other.metadata().extra["encrypted"] is True


# ---- the window --------------------------------------------------------------------------

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


def test_the_window_offers_the_receiver_and_starts_monitoring_from_it(qt_app, receiver_config, store,
                                                                      monkeypatch):
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtTest import QTest

    from babelfishr.ui.input_panel import RECEIVER
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(receiver_config, store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    box = window.input_panel.device_box
    index = box.findData({"kind": RECEIVER})
    assert index > 0, "the receiver is not offered although SDR++ is installed"
    box.setCurrentIndex(index)
    pump(qt_app)
    assert receiver_config.input_is_receiver
    assert "SDR receiver" in window.input_panel.status_label.text()
    ok, message = window.input_panel.ready_to_monitor()
    assert ok, message
    assert [a.text() for a in window.menuBar().actions()].count("&Receiver") == 1

    # Tune… saves the operator's choice through the real dialog widgets.
    def fill_and_accept(dialog_exec):
        dialog = window.tune_dialog
        for spin in dialog.findChildren(QtWidgets.QDoubleSpinBox):
            spin.setValue(155.16)
        return QtWidgets.QDialog.Accepted
    monkeypatch.setattr(QtWidgets.QDialog, "exec", lambda self: fill_and_accept(None))
    window.tune_receiver_action.trigger()
    pump(qt_app)
    assert receiver_config.receiver.frequency_hz == pytest.approx(155.16e6)

    QTest.mouseClick(window.start_button, QtCore.Qt.LeftButton)
    pump(qt_app, 40)
    assert app.capture is not None, "monitoring did not start from the receiver"
    assert isinstance(app._signal_source, PcmTcpSource)
    assert app.receiver.tuning.confirmed_hz == 155160000.0
    assert "155.1600" in window.sdr_label.text()
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and not any(
            t.state is ProcessingState.COMPLETE for t in app.recent_transmissions()):
        pump(qt_app, 10)
    assert any(t.state is ProcessingState.COMPLETE for t in app.recent_transmissions())
    QTest.mouseClick(window.start_button, QtCore.Qt.LeftButton)      # Stop monitoring
    pump(qt_app, 40)
    assert app.capture is None
    process = app.receiver._process
    window.close()
    pump(qt_app, 60)
    assert wait_for(lambda: not process.alive, timeout=20)
