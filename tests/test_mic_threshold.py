"""The built-in microphone's start-recording level, under the live spectrum.

Only for the Mac's own microphone (or a deliberately chosen system default
that is it, or that cannot be told apart); a USB interface, a receiver and a
replayed file keep the detector's own settings. Unset - every older settings
file - changes nothing. Set, it is the gate: input below it makes no message,
no card, no queued row and no WAV, also at Stop; above it a message keeps its
pre- and post-roll, and real weak speech above the level is kept. It can be
changed while listening, and is remembered. Generated audio only.
"""

from __future__ import annotations

import os
import pathlib
import time

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.devices import AudioDevice, DeviceIdentity
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.config import Config
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.testing import silence, speech_like

SR = 48_000


def _level(x, dbfs):
    rms = float(np.sqrt(np.mean(x ** 2))) or 1.0
    return x / rms * 10 ** (dbfs / 20)


def ambient(seconds, dbfs, seed=7):
    return _level(np.random.default_rng(seed).normal(0, 1, int(seconds * SR)), dbfs)


def voice(seconds, dbfs, seed=1):
    return _level(speech_like(seconds, SR, level_dbfs=-14.0, seed=seed), dbfs)


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.database = str(tmp_path / "db.sqlite3")
    c.recording.directory = str(tmp_path / "rec")
    c.asr.engine = c.translate.engine = "mock"
    c.source_path = str(tmp_path / "settings.toml")
    c.detector.hang_time = 1.0
    return c


def _app(cfg, route="builtin"):
    app = BabelFishRApp(config=cfg)
    app.transcription, app.translation = MockTranscriptionEngine(), MockTranslationEngine()
    app.microphone_route = lambda: route
    return app


def _feed(source, audio):
    for i in range(0, audio.size, 4800):
        source.push(audio[i:i + 4800])


def _wait(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _wavs(cfg):
    return sorted(pathlib.Path(cfg.recording.directory).rglob("*.wav"))


def _run(app, audio, stop=True, during=None):
    source = CallbackAudioSource(SR, name="mic")
    session = app.start_session(source=source, name="mic", target_language="en")
    app.begin_capture()
    try:
        _feed(source, audio)
        if during:
            during(app, source)
        time.sleep(0.5)
    finally:
        if stop:
            app.stop_session()
            app.wait_for_session_ends(10)
    return session


# --- settings and routes -----------------------------------------------------------

def test_unset_changes_nothing_and_older_files_read_unset(cfg, tmp_path):
    assert Config().audio.mic_threshold_dbfs is None
    old = tmp_path / "old.toml"
    old.write_text('[detector]\nmode = "fixed"\nthreshold_dbfs = -33.0\n', encoding="utf-8")
    loaded = Config.load(str(old))
    assert loaded.audio.mic_threshold_dbfs is None
    assert (loaded.detector.mode, loaded.detector.threshold_dbfs) == ("fixed", -33.0)
    app = _app(loaded)
    try:
        assert app.capture_detector_settings() == loaded.detector.to_settings()
        assert app.mic_gate_dbfs() == -33.0              # what is in force, shown
    finally:
        app.close()


def test_routes_are_resolved_honestly(cfg, monkeypatch):
    import babelfishr.app as app_module

    builtin = AudioDevice(index=0, name="MacBook Pro Microphone", max_input_channels=1,
                          default_sample_rate=48000, host_api="Core Audio",
                          is_default=True, transport="builtin")
    usb = AudioDevice(index=1, name="USB Audio CODEC", max_input_channels=2,
                      default_sample_rate=48000, host_api="Core Audio",
                      is_default=False, transport="usb")
    app = BabelFishRApp(config=cfg)
    try:
        cfg.audio.input.confirmed = True
        cfg.audio.input.identity = DeviceIdentity(name=builtin.name,
                                                  transport="builtin").token()
        assert app.microphone_route() == "builtin"
        cfg.audio.input.identity = DeviceIdentity(name=usb.name, transport="usb").token()
        assert app.microphone_route() == ""
        cfg.audio.input.identity = ""
        cfg.audio.input.use_system_default = True
        monkeypatch.setattr(app_module, "list_input_devices", lambda: [builtin, usb])
        assert app.microphone_route() == "system-default-builtin"
        usb_default = AudioDevice(**{**usb.__dict__, "is_default": True})
        quiet_builtin = AudioDevice(**{**builtin.__dict__, "is_default": False})
        monkeypatch.setattr(app_module, "list_input_devices",
                            lambda: [quiet_builtin, usb_default])
        assert app.microphone_route() == "", "the default is the USB interface"

        def unavailable():
            raise RuntimeError("no audio backend")
        monkeypatch.setattr(app_module, "list_input_devices", unavailable)
        assert app.microphone_route() == "system-default-unconfirmed"
        cfg.audio.input.kind = "receiver"
        assert app.microphone_route() == ""
    finally:
        app.close()


def test_a_usb_interface_or_a_replay_keeps_the_detectors_own_settings(cfg):
    cfg.audio.mic_threshold_dbfs = -25.0
    app = _app(cfg, route="")
    try:
        assert app.capture_detector_settings().mode == cfg.detector.mode
    finally:
        app.close()
    app = _app(cfg, route="builtin")
    try:
        assert app.capture_detector_settings(live=False).mode == cfg.detector.mode
        live = app.capture_detector_settings()
        assert (live.mode, live.threshold_dbfs) == ("fixed", -25.0)
    finally:
        app.close()


# --- the gate at the capture intake -------------------------------------------------

def test_ambient_below_the_level_makes_nothing_even_at_stop(cfg):
    cfg.audio.mic_threshold_dbfs = -30.0
    app = _app(cfg)
    try:
        session = _run(app, ambient(6.0, -42.0))            # room hum under the gate
        assert app.store.list_transmissions(session_id=session.id) == []
        assert _wavs(cfg) == [], "a WAV was written for sub-threshold ambient"
        assert _wait(lambda: app.outstanding_work() == 0), "a row was queued"
        assert app.store.list_transmissions(session_id=session.id) == []
    finally:
        app.close()


def test_weak_speech_above_the_level_is_recorded_with_its_rolls(cfg):
    cfg.audio.mic_threshold_dbfs = -38.0
    app = _app(cfg)
    try:
        audio = np.concatenate([ambient(1.5, -50.0), voice(2.0, -30.0),
                                ambient(3.0, -50.0, seed=8)])
        session = _run(app, audio)
        assert _wait(lambda: len(app.store.list_transmissions(session_id=session.id)) == 1)
        (tx,) = app.store.list_transmissions(session_id=session.id)
        assert 2.0 + 0.4 <= tx.duration <= 2.0 + 0.5 + 1.0 + 0.6, tx.duration
        assert pathlib.Path(tx.audio_path).exists()
    finally:
        app.close()


def test_the_level_changes_while_listening_and_is_remembered(cfg):
    cfg.audio.mic_threshold_dbfs = -20.0
    app = _app(cfg)
    try:
        def lower_it(app, source):
            assert app.capture.mic_gate
            _wait(lambda: False, timeout=0.3)
            assert app.store.list_transmissions(session_id=app.session.id) == [], \
                "speech under the -20 dBFS gate was recorded"
            app.set_mic_threshold(-40.0)                    # from the control, live
            _feed(source, np.concatenate([voice(2.0, -30.0, seed=3),
                                          ambient(2.5, -55.0, seed=9)]))

        audio = np.concatenate([voice(2.0, -30.0), ambient(2.5, -55.0)])
        session = _run(app, audio, during=lower_it)
        assert _wait(lambda: len(app.store.list_transmissions(session_id=session.id)) == 1)
        assert Config.load(cfg.source_path).audio.mic_threshold_dbfs == -40.0
        assert app.config.detector.mode == "auto", "the detector settings were rewritten"
    finally:
        app.close()


def test_a_short_natural_transmission_is_kept_whole(cfg):
    cfg.audio.mic_threshold_dbfs = -35.0
    app = _app(cfg)
    try:
        audio = np.concatenate([silence(1.0, SR), voice(1.2, -20.0), silence(2.5, SR)])
        session = _run(app, audio)
        assert _wait(lambda: len(app.store.list_transmissions(session_id=session.id)) == 1)
    finally:
        app.close()


# --- the control ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_control_sits_under_the_spectrum_for_the_microphone_only(qt_app, cfg):
    from babelfishr.ui.input_panel import InputPanel

    app = _app(cfg)
    try:
        panel = InputPanel(app)
        panel.show()
        assert panel.gate_row.isVisibleTo(panel)
        assert panel.gate_box.suffix() == " dBFS"
        assert "15 s" in panel.gate_box.toolTip() and "3 s" in panel.gate_box.toolTip()
        panel.set_reading(0.2, 0.3, False, 0, rms_dbfs=-47.4)
        assert panel.level_now.text() == "Level now: -47 dBFS (RMS)"
        panel.gate_box.setValue(-36.0)
        assert app.config.audio.mic_threshold_dbfs == -36.0
        app.microphone_route = lambda: ""                 # a USB interface chosen
        panel._refresh_gate()
        assert not panel.gate_row.isVisibleTo(panel)
    finally:
        app.close()
