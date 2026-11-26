"""The microphone's start-recording control during Replay, and its Auto reset.

- While a WAV file is replayed, the built-in microphone's start level and
  "Level now" readout are hidden (the replayed audio is not the
  microphone, and its gate does not apply to it); they come back on Stop
  and when monitoring the microphone again. A USB interface never shows
  them, before, during or after.
- Once a fixed level is set for the microphone, a compact Auto button
  offers the way back to the detector profile: saved, and - while
  monitoring the microphone - applied to the running gate at once, with
  the profile's real behaviour (following the room, or its own fixed
  threshold). It never changes a USB interface's or a replay's gate.

The real window starts and stops a real Replay; the microphone is a
callback source fed with generated audio. Temporary data only.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from babelfishr.audio.source import CallbackAudioSource
from babelfishr.config import Config
from test_alpha5_session_removal import mock_app, pump, wav  # noqa: F401
from test_mic_threshold import SR, _app, _feed, _wait, ambient, cfg, voice  # noqa: F401


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _window(qt_app, cfg, route):
    from babelfishr.ui.main_window import MainWindow

    app = _app(cfg, route=route)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    window.input_panel.refresh_status()
    return app, window


def _mic_session(app):
    source = CallbackAudioSource(SR, name="mic")
    app.start_session(source=source, name="mic", target_language="en")
    app.begin_capture()
    return source


def _shown(panel):
    return panel.gate_row.isVisibleTo(panel) and panel.level_now.isVisibleTo(panel)


# --- Replay hides the microphone's gate and readout ---------------------------------

def test_replay_hides_the_gate_and_readout_and_stop_brings_them_back(qt_app, cfg, wav):
    cfg.audio.mic_threshold_dbfs = -30.0
    app, window = _window(qt_app, cfg, "builtin")
    panel = window.input_panel
    try:
        assert _shown(panel)
        panel.set_reading(0.3, 0.4, False, 0, rms_dbfs=-44.0)
        assert panel.level_now.text() == "Level now: -44 dBFS (RMS)"

        window._start_monitoring(replay_path=wav)
        pump(qt_app)
        assert app.replaying()
        assert not panel.gate_row.isVisibleTo(panel), "mic gate shown during Replay"
        assert not panel.gate_state.isVisibleTo(panel)
        panel.set_reading(0.9, 0.9, False, 0, rms_dbfs=-6.0)      # the file's level
        assert "-6" not in panel.level_now.text(), "the replay's level was shown as the mic's"
        assert app.capture.detector.settings.mode == cfg.detector.mode, \
            "the microphone's level gated the replay"

        window._stop_monitoring()
        app.wait_for_session_ends(10)
        pump(qt_app)
        assert _shown(panel) and panel.gate_box.value() == -30.0
        assert panel.level_now.text() == "Level now: –", "a stale reading came back"

        # Back to the microphone: shown, live, and gating.
        source = _mic_session(app)
        panel.set_monitoring(True, replay=False)
        assert _shown(panel) and app.capture.mic_gate
        panel.set_reading(0.2, 0.3, False, 0, rms_dbfs=-51.0)
        assert panel.level_now.text() == "Level now: -51 dBFS (RMS)"
        assert source is app.capture.source
    finally:
        window._stop_monitoring()
        window.close()


def test_a_usb_interface_never_shows_it_and_keeps_its_settings(qt_app, cfg, wav):
    cfg.audio.mic_threshold_dbfs = -30.0
    app, window = _window(qt_app, cfg, "")
    panel = window.input_panel
    try:
        assert not panel.gate_row.isVisibleTo(panel)
        window._start_monitoring(replay_path=wav)
        pump(qt_app)
        assert not panel.gate_row.isVisibleTo(panel)
        window._stop_monitoring()
        app.wait_for_session_ends(10)
        pump(qt_app)
        assert not panel.gate_row.isVisibleTo(panel)
        _mic_session(app)                               # the USB input, live
        panel.set_monitoring(True, replay=False)
        assert not panel.gate_row.isVisibleTo(panel) and not app.capture.mic_gate
        assert app.capture.detector.settings == cfg.detector.to_settings()
    finally:
        window._stop_monitoring()
        window.close()


# --- Auto ------------------------------------------------------------------------------

def test_auto_is_offered_only_for_a_fixed_level_and_resets_it_idle(qt_app, cfg):
    from babelfishr.ui.input_panel import InputPanel

    cfg.detector.mode, cfg.detector.threshold_dbfs = "fixed", -33.0
    app = _app(cfg)
    try:
        panel = InputPanel(app)
        panel.show()
        assert not panel.gate_auto_button.isVisibleTo(panel), "nothing to reset yet"
        assert panel.gate_box.value() == -33.0
        panel.gate_box.setValue(-25.0)
        assert app.config.audio.mic_threshold_dbfs == -25.0
        assert panel.gate_auto_button.isVisibleTo(panel)
        panel.gate_auto_button.click()
        assert app.config.audio.mic_threshold_dbfs is None
        assert Config.load(cfg.source_path).audio.mic_threshold_dbfs is None
        assert not panel.gate_auto_button.isVisibleTo(panel)
        assert panel.gate_box.value() == -33.0 and app.mic_gate_dbfs() == -33.0
        assert (app.config.detector.mode, app.config.detector.threshold_dbfs) == ("fixed", -33.0)
        assert app.capture_detector_settings() == cfg.detector.to_settings()

        # An automatic detector: the panel says it follows the room again.
        cfg.detector.mode = "auto"
        panel.gate_box.setValue(-25.0)
        panel.gate_auto_button.click()
        assert app.mic_gate_dbfs() is None and "automatic" in panel.gate_state.text()
        # And after a relaunch the setting stays reset.
        reopened = _app(Config.load(cfg.source_path))
        try:
            assert reopened.config.audio.mic_threshold_dbfs is None
            assert InputPanel(reopened).gate_auto_button.isHidden()
        finally:
            reopened.close()
    finally:
        app.close()


def test_auto_while_listening_restores_the_rooms_own_gate_now(qt_app, cfg):
    """Fixed at -20 dBFS, speech at -30 is not recorded; after Auto the
    automatic detector (room floor + 8 dB) records the next one at once."""
    from babelfishr.ui.input_panel import InputPanel

    cfg.audio.mic_threshold_dbfs = -20.0
    app = _app(cfg)
    panel = InputPanel(app)
    try:
        source = _mic_session(app)
        session = app.session
        panel.set_monitoring(True)
        assert app.capture.mic_gate and app.capture.detector.settings.mode == "fixed"
        _feed(source, np.concatenate([ambient(2.0, -55.0), voice(2.0, -30.0),
                                      ambient(2.5, -55.0, seed=8)]))
        _wait(lambda: False, timeout=0.4)
        assert app.store.list_transmissions(session_id=session.id) == []

        panel.gate_auto_button.click()                     # from the control, live
        detector = app.capture.detector
        assert (detector.settings.mode, app.capture.mic_gate) == ("auto", False)
        assert detector.settings == cfg.detector.to_settings()
        _feed(source, np.concatenate([voice(2.0, -30.0, seed=3),
                                      ambient(2.5, -55.0, seed=9)]))
        assert _wait(lambda: len(app.store.list_transmissions(session_id=session.id)) == 1), \
            "the restored automatic gate did not record speech above the room"
        assert Config.load(cfg.source_path).audio.mic_threshold_dbfs is None
    finally:
        app.stop_session()
        app.wait_for_session_ends(10)
        app.close()


def test_auto_while_listening_restores_a_fixed_detector_profile(cfg):
    cfg.detector.mode, cfg.detector.threshold_dbfs = "fixed", -45.0
    cfg.audio.mic_threshold_dbfs = -20.0
    app = _app(cfg)
    try:
        _mic_session(app)
        assert app.capture.detector.settings.threshold_dbfs == -20.0
        app.reset_mic_threshold()
        settings = app.capture.detector.settings
        assert (settings.mode, settings.threshold_dbfs) == ("fixed", -45.0)
        assert settings == cfg.detector.to_settings()
        # A later level still applies live: the control keeps working.
        app.set_mic_threshold(-30.0)
        assert app.capture.mic_gate and app.capture.detector.settings.threshold_dbfs == -30.0
    finally:
        app.stop_session()
        app.wait_for_session_ends(10)
        app.close()


def test_auto_never_changes_a_usb_or_replay_gate(cfg, wav):
    cfg.audio.mic_threshold_dbfs = -20.0
    cfg.detector.open_margin_db = 11.0                     # a USB/replay-specific tune
    for route, start in (("", lambda app: _mic_session(app)),
                         ("builtin", lambda app: app.start_session(replay_path=wav,
                                                                   name="replay"))):
        app = _app(cfg, route=route)
        try:
            start(app)
            before = app.capture.detector.settings
            assert not app.capture.mic_gate and before == cfg.detector.to_settings()
            app.reset_mic_threshold()
            assert app.capture.detector.settings is before, "Auto touched a non-mic gate"
            assert cfg.detector.open_margin_db == 11.0
        finally:
            app.stop_session()
            app.wait_for_session_ends(10)
            app.close()
        cfg.audio.mic_threshold_dbfs = -20.0
