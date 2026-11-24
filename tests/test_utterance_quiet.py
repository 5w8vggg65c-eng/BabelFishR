"""About 3 seconds of quiet ends a message; shorter pauses do not.

Start Monitoring listens, transcribes and fills the chat by itself: each
message ends after ``detector.hang_time`` seconds of quiet (default 3 s,
shown as "End a message after ... s of quiet") and the next audio starts the
next one, until Stop. "Quiet" is the input's audio level - a microphone or
interface cannot report a radio's squelch. Continuous audio that never goes
quiet is still handed on in parts, so text never needs Stop.

Synthetic audio and mock engines only.
"""

from __future__ import annotations

import datetime as dt
import os
import time

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import AudioBlock, CallbackAudioSource
from babelfishr.config import Config
from babelfishr.detect import DetectorSettings, RadioActivityDetector
from babelfishr.models import ProcessingState
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.testing import silence, speech_like

SR = 48_000
BLOCK = 2048
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def speech(seconds, seed=1):
    x = speech_like(seconds, SR, level_dbfs=-14.0, seed=seed)
    return x / np.max(np.abs(x)) * 0.7


def detect(audio, **overrides):
    """Messages found in *audio*, block by block, then at Stop."""
    settings = DetectorSettings(mode="fixed", threshold_dbfs=-20.0, **overrides)
    det = RadioActivityDetector(SR, settings)
    found = []
    for i in range(0, audio.size, BLOCK):
        found += det.push(AudioBlock(samples=audio[i:i + BLOCK], sample_rate=SR,
                                     timestamp=T0 + dt.timedelta(seconds=i / SR),
                                     offset=i / SR))
    live = len(found)
    found += det.flush()
    return found, live


def test_the_default_is_three_seconds_everywhere():
    assert DetectorSettings().hang_time == 3.0
    assert Config().detector.hang_time == 3.0
    assert Config().detector.to_settings().hang_time == 3.0


def test_a_short_pause_stays_inside_one_message():
    audio = np.concatenate([silence(0.5, SR), speech(2.0), silence(2.0, SR),
                            speech(2.0, seed=2), silence(4.0, SR)])
    found, live = detect(audio)
    assert len(found) == 1 and live == 1, [f.duration for f in found]
    # Both halves are in it: 2 + 2 + 2 s of pause, plus the kept roll.
    assert found[0].duration >= 6.0


def test_three_seconds_of_quiet_ends_a_message_and_the_next_audio_starts_another():
    audio = np.concatenate([silence(0.5, SR), speech(2.0), silence(3.5, SR),
                            speech(2.0, seed=2), silence(4.0, SR)])
    found, live = detect(audio)
    assert len(found) == 2 and live == 2, "each message closed by itself, before Stop"
    first, second = found
    assert first.duration < 3.5, "the quiet that ended it is not kept"
    assert second.start_offset >= 0.5 + 2.0 + 3.5 - 0.6


def test_the_opening_is_kept_by_the_pre_roll():
    audio = np.concatenate([silence(1.0, SR), speech(2.0), silence(4.0, SR)])
    (found,), _ = detect(audio)
    # The gate opens on the first loud frames; the 0.5 s before that is kept,
    # so the first loud sample sits about half a second into the message.
    onset = int(np.argmax(np.abs(found.audio) > 0.1)) / SR
    assert onset >= 0.4, f"the pre-roll before the first word was lost ({onset:.2f} s)"


def test_the_quiet_is_configurable_in_seconds():
    audio = np.concatenate([silence(0.5, SR), speech(2.0), silence(2.0, SR),
                            speech(2.0, seed=2), silence(4.0, SR)])
    assert len(detect(audio, hang_time=1.0)[0]) == 2     # 2 s pause now ends it
    assert len(detect(audio, hang_time=3.0)[0]) == 1
    with pytest.raises(ValueError):
        DetectorSettings(hang_time=0.0).validate()
    with pytest.raises(ValueError):
        DetectorSettings(hang_time=31.0).validate()


def test_a_stored_value_is_kept_as_written():
    """No migration of anyone's settings: a file holding 0.8 keeps 0.8."""
    assert Config.from_dict({"detector": {"hang_time": 0.8}}).detector.hang_time == 0.8


def test_long_continuous_audio_is_still_handed_on_in_parts():
    noisy = speech(40.0) + np.random.default_rng(3).normal(0, 10 ** (-23 / 20), 40 * SR)
    found, live = detect(np.concatenate([silence(0.5, SR), noisy]))
    assert live >= 2, "continuous audio reached recognition only at Stop"
    assert all(f.duration <= 15.0 + 3.0 + 0.1 for f in found[:live])


# --- the whole app: messages processed one by one while monitoring ----------

def _wait(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def default_config(tmp_path):
    """The real defaults (3 s), not the shared fixture's burst spacing."""
    cfg = Config()
    cfg.database = str(tmp_path / "db.sqlite3")
    cfg.recording.directory = str(tmp_path / "recordings")
    cfg.asr.engine = cfg.translate.engine = "mock"
    cfg.translate.target_language = "en"
    cfg.detector.mode, cfg.detector.threshold_dbfs = "fixed", -20.0
    assert cfg.detector.hang_time == 3.0
    return cfg


def test_monitoring_transcribes_each_message_before_stop(default_config):
    app = BabelFishRApp(config=default_config)
    app.transcription, app.translation = MockTranscriptionEngine(), MockTranslationEngine()
    source = CallbackAudioSource(SR, name="test")
    session = app.start_session(source=source, name="run", target_language="de")
    app.begin_capture()
    try:
        audio = np.concatenate([silence(0.5, SR), speech(1.5), silence(2.0, SR),
                                speech(1.5, seed=2), silence(4.0, SR),
                                speech(1.5, seed=3), silence(4.0, SR)])
        for i in range(0, audio.size, 4800):
            source.push(audio[i:i + 4800])

        def done():
            rows = app.store.list_transmissions(session_id=session.id)
            return len(rows) == 2 and all(r.state is ProcessingState.COMPLETE for r in rows)

        # Two messages (the 2 s pause did not split the first), both
        # transcribed and translated while monitoring is still on.
        assert _wait(done), [r.state for r in app.store.list_transmissions(session_id=session.id)]
        assert app.capture is not None, "still monitoring"
        rows = app.store.list_transmissions(session_id=session.id)
        assert all(r.transcript and r.translation for r in rows)
    finally:
        app.stop_session()
        app.close()


def test_long_continuous_input_produces_text_without_stop(default_config):
    app = BabelFishRApp(config=default_config)
    app.transcription, app.translation = MockTranscriptionEngine(), MockTranslationEngine()
    source = CallbackAudioSource(SR, name="test")
    session = app.start_session(source=source, name="run", target_language="de")
    app.begin_capture()
    try:
        noisy = speech(35.0) + np.random.default_rng(4).normal(0, 10 ** (-23 / 20), 35 * SR)
        for i in range(0, noisy.size, 4800):
            source.push(noisy[i:i + 4800])
        assert _wait(lambda: any(
            r.state is ProcessingState.COMPLETE and r.transcript
            for r in app.store.list_transmissions(session_id=session.id)))
        assert app.capture is not None, "text arrived while monitoring was still on"
    finally:
        app.stop_session()
        app.close()


# --- the window control ----------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_window_sets_the_quiet_in_seconds(qt_app, default_config, tmp_path):
    from babelfishr.ui.main_window import MainWindow

    default_config.source_path = str(tmp_path / "settings.toml")
    app = BabelFishRApp(config=default_config)
    window = MainWindow(app)
    try:
        box = window.quiet_seconds_box
        assert box.value() == 3.0 and "quiet" in box.suffix()
        assert "squelch" in box.toolTip()
        box.setValue(4.5)
        assert app.config.detector.hang_time == 4.5
        assert Config.load(str(tmp_path / "settings.toml")).detector.hang_time == 4.5
        window._set_controls_enabled(False)       # as while monitoring
        assert not box.isEnabled()
        window._set_controls_enabled(True)
        assert box.isEnabled()
        # The control's range is the validated range: a saved 0.2 shows 0.2.
        assert box.minimum() == 0.1 and box.maximum() == 30.0
    finally:
        window.close()
        app.close()


def test_a_short_saved_quiet_is_displayed_as_saved(qt_app, default_config):
    from babelfishr.ui.main_window import MainWindow

    default_config.detector.hang_time = 0.2
    default_config.detector.to_settings()          # valid
    app = BabelFishRApp(config=default_config)
    window = MainWindow(app)
    try:
        assert window.quiet_seconds_box.value() == pytest.approx(0.2)
        assert app.config.detector.hang_time == pytest.approx(0.2)
    finally:
        window.close()
        app.close()
