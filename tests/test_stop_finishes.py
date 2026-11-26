"""Stop Monitoring cuts the input off and finishes everything accepted.

The operator's decision (2026-11-25), revising the build 34 behaviour:
pressing Stop cuts microphone intake at once, then finishes every
transmission accepted up to that cutoff - the running job, queued ones, and
the message still open at Stop (its tail, without waiting for trailing
silence) - and posts each transcript/translation in its own session. No
Retry is needed. Audio arriving after the cutoff is not saved. A quick
restart queues behind the stopped run's work on the same processor; each
run's results stay in their own session.

Controlled engines (blocking on events) and synthetic audio only.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.audio.wavefile import read_wav
from babelfishr.models import ProcessingState, Transmission
from babelfishr.pipeline import is_processing_stopped, mark_processing_stopped
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.testing import build_fixture

SR = 48_000
VOICE = {"kind": "voice", "duration": 1.5, "level_dbfs": -14}
QUIET = {"gap": 4.0}                     # longer than the 3 s that ends a message


class SlowASR(MockTranscriptionEngine):
    """Blocks inside transcribe() until released; counts calls."""
    id = "test-slow-asr"
    name = "slow-asr"

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        self.entered.set()
        assert self.release.wait(30.0), "the test never released the engine"
        return super().transcribe(audio, sample_rate, language=language,
                                  vocabulary=vocabulary)


class SlowMT(MockTranslationEngine):
    id = "test-slow-mt"
    name = "slow-mt"

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def translate(self, *args, **kwargs):
        self.entered.set()
        assert self.release.wait(30.0), "the test never released the engine"
        return super().translate(*args, **kwargs)


def _wait(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _start(config, store, asr=None, mt=None, target="fr"):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = asr or MockTranscriptionEngine()
    app.translation = mt or MockTranslationEngine()
    source = CallbackAudioSource(SR, name="test")
    session = app.start_session(source=source, name="run", target_language=target)
    app.begin_capture()
    return app, source, session


def _push(source, spec):
    audio = build_fixture(spec, sample_rate=SR).audio
    for start in range(0, audio.size, 4800):
        source.push(audio[start:start + 4800])
    return audio


def _rows(store, session_id):
    return [t for t in store.list_transmissions(session_id=session_id)]




class RecordingASR(MockTranscriptionEngine):
    """Records the order (by audio length) in which it was asked; can block."""
    id = "test-recording-asr"
    name = "recording-asr"

    def __init__(self):
        super().__init__()
        self.order = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        self.order.append(round(audio.size / sample_rate, 2))
        self.entered.set()
        assert self.release.wait(30.0)
        return super().transcribe(audio, sample_rate, language=language,
                                  vocabulary=vocabulary)


def _all_complete(store, session_id, n):
    rows = _rows(store, session_id)
    return len(rows) == n and all(r.state is ProcessingState.COMPLETE for r in rows)


# --- 1. the processor: running, queued and the final flush all finish ---------

def test_stop_finishes_the_running_and_queued_messages(config, store):
    asr = SlowASR()
    app, source, session = _start(config, store, asr=asr)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET, VOICE, QUIET, VOICE, QUIET])
        assert asr.entered.wait(15.0)
        assert _wait(lambda: len(_rows(store, session.id)) == 3)
        began = time.monotonic()
        app.stop_session()
        assert time.monotonic() - began < 0.5, "Stop waited"
        asr.release.set()
        assert _wait(lambda: _all_complete(store, session.id, 3))
        rows = _rows(store, session.id)
        assert all(r.transcript and not is_processing_stopped(r) for r in rows)
        assert asr.calls == 3
        assert all(r.session_id == session.id for r in rows)
    finally:
        asr.release.set()
        app.close()


def test_a_translation_running_at_stop_is_finished_too(config, store):
    mt = SlowMT()
    app, source, session = _start(config, store, mt=mt)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert mt.entered.wait(15.0)
        app.stop_session()
        mt.release.set()
        assert _wait(lambda: _all_complete(store, session.id, 1))
        (tx,) = _rows(store, session.id)
        assert tx.transcript and tx.translation
    finally:
        mt.release.set()
        app.close()


def test_stop_mid_message_finishes_its_tail_without_trailing_silence(config, store):
    """A short active speech tail: the message is still open (no quiet after
    it) when Stop is pressed; it is closed at the cutoff and transcribed."""
    app, source, session = _start(config, store)
    try:
        pushed = _push(source, [{"gap": 0.5}, {"kind": "voice", "duration": 1.2,
                                               "level_dbfs": -14}])
        assert _wait(lambda: app.capture is not None and source._queue.empty()
                     and app.capture.detector.open)
        app.stop_session()
        assert app.stop_session() is None              # again: nothing happens
        assert _wait(lambda: _all_complete(store, session.id, 1))
        (tx,) = _rows(store, session.id)
        assert tx.transcript and not is_processing_stopped(tx)
        saved, rate = read_wav(tx.audio_path)
        assert rate == SR and saved.size >= int(1.2 * SR)   # the whole tail
        assert np.max(np.abs(saved[-int(0.2 * SR):])) > 0.05, "the cut tail is missing"
    finally:
        app.close()


# --- 2. the normal button path, in the window --------------------------------

def test_the_stop_button_shows_finishing_and_posts_each_result(qt_app, config, store):
    asr = SlowASR()
    app, window, source, session = _window_monitoring(qt_app, config, store, asr)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET, VOICE])   # second still open
        assert asr.entered.wait(15.0)
        assert _wait(lambda: source._queue.empty() and app.capture.detector.open)
        window._drain_events()
        window._stop_monitoring()                     # the button's handler
        message = window.status.currentMessage().lower()
        assert "finishing" in message and "its session" in message
        # Stopped with accepted work: "Finishing", and how many wait behind.
        assert "Finishing" in window.state_badge.text(), window.state_badge.text()
        asr.release.set()
        assert _wait(lambda: _all_complete(store, session.id, 2))
        for _ in range(5):
            window._drain_events()
            _pump(qt_app, 3)
        for tx in _rows(store, session.id):
            bubble = window.timeline.bubble(tx.id)
            assert bubble is not None, "a finished message was not posted"
            assert bubble.tx.transcript == tx.transcript
        assert "Idle" in window.state_badge.text(), window.state_badge.text()
    finally:
        asr.release.set()
        window.close()
        app.close()


def test_an_ended_runs_meter_does_not_move_after_stop(qt_app, config, store):
    from babelfishr.audio.meter import LevelReading

    asr = SlowASR()
    asr.release.set()
    app, window, source, session = _window_monitoring(qt_app, config, store, asr)
    try:
        reading = LevelReading(rms_dbfs=-6.0, peak_dbfs=-0.1, peak_hold_dbfs=-0.1,
                               clipped=True, clip_count=11_000, sample_count=48_000)
        app.events.publish("level", reading, run=session.id)
        window._drain_events()
        assert window.clip_label.text() == "11,000 clipped samples"
        assert "22.92%" in window.clip_label.toolTip()
        window._stop_monitoring()
        assert window.clip_label.text() == ""
        app.events.publish("level", reading, run=session.id)
        window._drain_events()
        assert window.clip_label.text() == "", "an ended run moved the meter"
    finally:
        window.close()
        app.close()


# --- 3. a quick restart: order kept, each run in its own session ---------------

def test_a_quick_restart_queues_behind_the_stopped_run_and_keeps_sessions_apart(
        config, store):
    asr = RecordingASR()
    asr.release.clear()
    app, source, first = _start(config, store, asr=asr)
    try:
        _push(source, [{"gap": 0.5}, {"kind": "voice", "duration": 1.0, "level_dbfs": -14},
                       QUIET, {"kind": "voice", "duration": 2.0, "level_dbfs": -14}, QUIET])
        assert asr.entered.wait(15.0)
        assert _wait(lambda: len(_rows(store, first.id)) == 2)
        held = app.pipeline
        app.stop_session()
        assert _wait(lambda: app.shutdown_problem() == "", timeout=5.0)
        source2 = CallbackAudioSource(SR, name="second")
        second = app.start_session(source=source2, name="second", target_language="fr")
        app.begin_capture()
        assert second.id != first.id and app.pipeline is held
        _push(source2, [{"gap": 0.5}, {"kind": "voice", "duration": 3.0, "level_dbfs": -14},
                        QUIET])
        assert _wait(lambda: len(_rows(store, second.id)) == 1)
        asr.release.set()
        assert _wait(lambda: _all_complete(store, first.id, 2)
                     and _all_complete(store, second.id, 1))
        # The stopped run's messages were finished first, in their order.
        durations = asr.order
        assert len(durations) == 3 and durations[0] < durations[1] < durations[2]
        assert {r.session_id for r in _rows(store, first.id)} == {first.id}
        assert {r.session_id for r in _rows(store, second.id)} == {second.id}
        app.stop_session()
    finally:
        asr.release.set()
        app.close()


# --- 4. what is unchanged ---------------------------------------------------------

def test_a_row_saved_not_processed_by_build_34_is_still_processed_by_hand(config, store):
    """Rows build 34 marked saved-not-processed keep their label, never
    resume by themselves, and Transcribe anyway finishes them."""
    app, source, session = _start(config, store)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert _wait(lambda: _all_complete(store, session.id, 1))
        (tx,) = _rows(store, session.id)
        old = store.get_transmission(tx.id)
        old.transcript = ""
        mark_processing_stopped(old, session.id)
        store.save_transmission(old)
        assert tx.id not in {t.id for t in store.pending_transmissions()}
        assert store.get_transmission(tx.id).needs_review
        app.stop_session()
        assert app.transcribe_anyway(tx.id)
        assert _wait(lambda: store.get_transmission(tx.id).state is ProcessingState.COMPLETE)
        done = store.get_transmission(tx.id)
        assert done.transcript and not is_processing_stopped(done)
    finally:
        app.close()


def test_record_only_mode_still_saves_without_transcribing(config, store):
    config.mode = "record-only"
    app = BabelFishRApp(config=config, store=store)
    app.select_engines()
    assert app.transcription is None
    source = CallbackAudioSource(SR, name="test")
    session = app.start_session(source=source, name="ro")
    app.begin_capture()
    try:
        _push(source, [{"gap": 0.5}, {"kind": "voice", "duration": 1.2, "level_dbfs": -14}])
        assert _wait(lambda: app.capture is not None and source._queue.empty()
                     and app.capture.detector.open)
        app.stop_session()
        assert _wait(lambda: len(_rows(store, session.id)) == 1 and app.outstanding_work() == 0)
        (tx,) = _rows(store, session.id)
        assert os.path.exists(tx.audio_path) and not tx.transcript
        assert tx.state is not ProcessingState.FAILED
    finally:
        app.close()


def test_each_message_logs_its_timing_but_no_words(config, store, caplog):
    caplog.set_level(logging.INFO, logger="babelfishr.pipeline")
    app, source, session = _start(config, store)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert _wait(lambda: _all_complete(store, session.id, 1))
        (tx,) = _rows(store, session.id)
        lines = [r.getMessage() for r in caplog.records if "waited" in r.getMessage()]
        assert any(tx.id in line and "queue" in line for line in lines)
        assert not any(tx.transcript in r.getMessage() for r in caplog.records)
    finally:
        app.stop_session()
        app.close()


class _PreloadASR(MockTranscriptionEngine):
    id = "test-preload-asr"
    name = "preload-asr"

    def __init__(self, fail_preload=False):
        super().__init__()
        self.fail_preload = fail_preload    # not .fail: the mock's own switch
        self.preloaded_on = None
        self.calls_before_preload = None
        self.warm_up_calls = 0

    def preload(self):
        self.preloaded_on = threading.current_thread().name
        self.calls_before_preload = self.calls
        if self.fail_preload:
            raise RuntimeError("model files unreadable (test)")

    def warm_up(self):                       # the readiness self-test
        self.warm_up_calls += 1
        raise AssertionError("monitoring must not run the readiness self-test")


def test_start_preloads_the_speech_model_on_the_worker(config, store):
    asr = _PreloadASR()
    app, source, session = _start(config, store, asr=asr)
    try:
        assert _wait(lambda: asr.preloaded_on is not None, timeout=5.0)
        assert asr.preloaded_on.startswith("babelfishr-processing")
        assert asr.calls_before_preload == 0 and asr.warm_up_calls == 0
    finally:
        app.stop_session()
        app.close()


def test_a_failed_preload_still_processes_and_settles_the_work(config, store, caplog):
    caplog.set_level(logging.WARNING, logger="babelfishr.pipeline")
    asr = _PreloadASR(fail_preload=True)
    app, source, session = _start(config, store, asr=asr)
    try:
        assert _wait(lambda: asr.preloaded_on is not None, timeout=5.0)
        _push(source, [{"gap": 0.5}, VOICE, QUIET, VOICE, QUIET])
        assert _wait(lambda: _all_complete(store, session.id, 2))
        assert any("not preloaded" in r.getMessage() for r in caplog.records)
        app.stop_session()
        assert _wait(lambda: app.outstanding_work() == 0)
        assert asr.warm_up_calls == 0
    finally:
        app.close()


def _whisper_with_fake_library(tmp_path, monkeypatch, constructor):
    """A real FasterWhisperEngine over a complete model folder, with the
    faster_whisper module replaced by a stand-in (no weights here)."""
    import sys
    import types

    from babelfishr.providers.whisper_local import FasterWhisperEngine, model_directory_for
    from test_model_layout import _write_complete_model

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = constructor
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    _write_complete_model(model_directory_for(tmp_path, "small"))
    return FasterWhisperEngine(model="small", models_root=str(tmp_path),
                               local_files_only=True)


def test_concurrent_preloads_construct_the_model_once(tmp_path, monkeypatch):
    built = []
    gate = threading.Event()

    class SlowModel:
        def __init__(self, *args, **kwargs):
            built.append(threading.current_thread().name)
            gate.wait(5.0)

    engine = _whisper_with_fake_library(tmp_path, monkeypatch, SlowModel)
    threads = [threading.Thread(target=engine.preload, name=f"p{i}") for i in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5.0)
    assert len(built) == 1, f"the model was constructed {len(built)} times"
    assert engine._model is not None


def test_a_failed_preload_raises_and_claims_nothing(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bad weights (test)")

    engine = _whisper_with_fake_library(tmp_path, monkeypatch, broken)
    with pytest.raises(Exception):
        engine.preload()
    assert engine._model is None, "a failed load must not leave a model in place"


def test_the_readiness_self_test_is_unchanged(tmp_path, monkeypatch):
    """warm_up() still loads AND transcribes a fixture and returns the
    result (Field Check relies on it); preload() does not replace it."""
    from babelfishr.providers.base import TranscriptionResult

    engine = _whisper_with_fake_library(tmp_path, monkeypatch, lambda *a, **k: object())
    calls = []
    result = TranscriptionResult(text="ok", language="en", language_confidence=1.0,
                                 confidence=1.0, segments=[], engine="faster-whisper",
                                 engine_version="test")
    monkeypatch.setattr(engine, "transcribe",
                        lambda audio, rate, **kw: (calls.append(audio.size), result)[1])
    assert engine.warm_up() is result
    assert calls, "the readiness self-test did not transcribe"


# --- 5. the operator cutoff keeps every sample of an open burst, exactly -------

def _q(x):
    """As the WAV writer stores it: int16 of x * 32767, truncated."""
    return (np.asarray(x) * 32767.0).astype("<i2")


def _detect(audio, cutoff, **overrides):
    import datetime as dt

    from babelfishr.audio.source import AudioBlock
    from babelfishr.detect import DetectorSettings, RadioActivityDetector

    settings = DetectorSettings(mode="fixed", threshold_dbfs=-20.0, **overrides)
    det = RadioActivityDetector(SR, settings)
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    emitted = []
    for i in range(0, audio.size, 1000):            # blocks not frame-aligned
        emitted += det.push(AudioBlock(samples=audio[i:i + 1000], sample_rate=SR,
                                       timestamp=t0 + dt.timedelta(seconds=i / SR),
                                       offset=i / SR))
    return emitted, det.flush(cutoff=cutoff)


def _loud(seconds, seed=1):
    from babelfishr.testing import speech_like

    x = speech_like(seconds, SR, level_dbfs=-14.0, seed=seed)
    return x / np.max(np.abs(x)) * 0.7


def _silence(seconds):
    return np.zeros(int(seconds * SR))


def test_cutoff_keeps_a_short_open_burst_and_its_partial_last_frame():
    audio = np.concatenate([_silence(0.6), _loud(0.12)])
    audio = audio[: audio.size - 337]               # ends mid-frame
    emitted, flushed = _detect(audio, cutoff=True)
    assert emitted == [] and len(flushed) == 1
    (found,) = flushed
    start = int(round(found.start_offset * SR))
    assert np.array_equal(found.audio, audio[start:]), "samples before the cutoff were lost"
    assert found.audio.size == audio.size - start
    # Ordinary end-of-stream rules are unchanged: under min_duration, dropped.
    assert _detect(audio, cutoff=False)[1] == []


def test_cutoff_keeps_the_quiet_after_speech_without_waiting_or_trimming():
    """Stop 0.6 s into the quiet after the last word (inside the hang): the
    cutoff keeps all of it; the ordinary flush trims back to the post-roll."""
    audio = np.concatenate([_silence(0.6), _loud(1.0), _silence(0.6)])
    _, (cut,) = _detect(audio, cutoff=True, hang_time=1.0, post_roll=0.2)
    start = int(round(cut.start_offset * SR))
    assert np.array_equal(cut.audio, audio[start:])
    assert cut.trimmed_tail == 0.0
    _, (ordinary,) = _detect(audio, cutoff=False, hang_time=1.0, post_roll=0.2)
    assert ordinary.audio.size < cut.audio.size


def test_stop_saves_exactly_the_pre_cutoff_samples_of_an_open_burst(config, store):
    """End to end through the app: the saved WAV is exactly the pushed audio
    from the message start to the cutoff (as int16), including a short
    burst under min_duration and a partial last block."""
    app, source, session = _start(config, store)
    try:
        audio = np.concatenate([_silence(0.6), _loud(0.2)])[:-123]
        for i in range(0, audio.size, 777):
            source.push(audio[i:i + 777])
        assert _wait(lambda: app.capture is not None and source._queue.empty()
                     and app.capture.detector.open)
        app.stop_session()
        assert _wait(lambda: len(_rows(store, session.id)) == 1 and app.outstanding_work() == 0)
        (tx,) = _rows(store, session.id)
        saved, rate = read_wav(tx.audio_path)
        expected = _q(audio)
        got = np.round(saved * 32768.0).astype("<i2")
        # The saved samples are the tail of the pushed audio, ending at the cutoff.
        assert got.size <= expected.size
        assert np.array_equal(got, expected[expected.size - got.size:]), (
            "the saved recording is not the pre-cutoff audio, sample for sample")
        assert got.size >= int(0.2 * SR) - 123        # the whole short burst
    finally:
        app.close()


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(qt_app, rounds=10):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.01)


def _window_monitoring(qt_app, config, store, asr):
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window.show()
    _pump(qt_app)
    app.transcription, app.translation = asr, MockTranslationEngine()
    source = CallbackAudioSource(SR, name="test")
    session = app.start_session(source=source, name="run", target_language="fr")
    app.begin_capture()
    return app, window, source, session



# --- the audio cutoff on a real LiveAudioSource (no hardware) -------------------

MARK = 0.5          # post-Stop callbacks deliver this flat value


class _FakeStream:
    active = True

    def __init__(self):
        self.closed = threading.Event()

    def stop(self):
        pass

    def close(self):
        self.closed.set()


def _live_source(monkeypatch):
    """The real LiveAudioSource - its callback, queue, cutoff and stop - with
    only the physical stream and the device lookup stubbed."""
    import sys
    import types

    from babelfishr.audio.devices import AudioDevice
    from babelfishr.audio.source import LiveAudioSource

    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))
    source = LiveAudioSource(device=None, sample_rate=SR, block_size=4800,
                             reconnect=False, max_queue_blocks=64)
    device = AudioDevice(index=0, name="stub input", max_input_channels=1,
                         default_sample_rate=SR)
    monkeypatch.setattr(source, "_resolve_device", lambda: device)
    stream = _FakeStream()
    monkeypatch.setattr(source, "_open_stream",
                        lambda: setattr(source, "_stream", stream))
    return source, stream


def _deliver(source, samples):
    """As PortAudio's thread would: one callback per 4800-sample block."""
    for start in range(0, samples.size, 4800):
        block = samples[start:start + 4800].astype(np.float32).reshape(-1, 1)
        source._callback(block, block.shape[0], None, None)


def _marked_samples(audio):
    return int(np.count_nonzero(np.abs(audio - MARK) < 2e-3))


def _stop_with_audio_buffered(monkeypatch, config, store, cutoff=True):
    """Pre-Stop audio delivered and still partly queued at Stop (the capture
    thread is held inside a block); then post-Stop callbacks. Returns the
    saved recording and the count of pre-Stop voice samples delivered."""
    from babelfishr.audio.source import LiveAudioSource

    if not cutoff:
        monkeypatch.setattr(LiveAudioSource, "close_intake", lambda self: None)
    source, stream = _live_source(monkeypatch)
    app = BabelFishRApp(config=config, store=store)
    app.transcription, app.translation = MockTranscriptionEngine(), MockTranslationEngine()
    session = app.start_session(source=source, name="live", target_language="fr")
    capture = app.capture
    held, release, handled = threading.Event(), threading.Event(), [0]
    real_handle = capture._handle_block

    def slow_handle(block):
        handled[0] += 1
        if handled[0] == 8:                      # hold mid-message
            held.set()
            assert release.wait(20.0)
        return real_handle(block)

    monkeypatch.setattr(capture, "_handle_block", slow_handle)
    app.begin_capture()
    try:
        lead = build_fixture([{"gap": 0.5}], sample_rate=SR).audio
        voice = build_fixture([{"kind": "voice", "duration": 4.0, "level_dbfs": -14}],
                              sample_rate=SR).audio
        _deliver(source, np.concatenate([lead, voice]))
        assert held.wait(10.0), "the capture never reached the held block"
        assert source._queue.qsize() > 5, "nothing was left buffered at Stop"
        app.stop_session()                      # the cutoff, on this thread
        _deliver(source, np.full(SR, MARK))     # one second after Stop
        release.set()
        assert _wait(lambda: app.outstanding_work() == 0 and stream.closed.is_set())
        app.wait_for_session_ends(10.0)
        rows = _rows(store, session.id)
        saved = np.concatenate([read_wav(t.audio_path)[0] for t in rows])
        return saved, voice, rows
    finally:
        release.set()
        app.close()


def test_buffered_pre_stop_audio_survives_and_post_stop_audio_is_not_saved(
        monkeypatch, config, store):
    saved, voice, rows = _stop_with_audio_buffered(monkeypatch, config, store)
    # Finished, not left for Retry (2026-11-25): everything up to the cutoff.
    assert rows and all(t.state is ProcessingState.COMPLETE and t.transcript for t in rows)
    assert _marked_samples(saved) == 0, "audio delivered after Stop was saved"
    # Exact: the saved samples are the delivered pre-Stop voice's last ones,
    # as int16, ending where the voice ended (the cutoff).
    got = np.round(saved * 32768.0).astype("<i2")
    expected = _q(voice.astype(np.float32).astype(np.float64))
    tail = min(got.size, expected.size)
    assert np.array_equal(got[-tail:], expected[-tail:]), "pre-Stop samples differ"
    # Every voice sample delivered before Stop is in the recording (the
    # queued part reached the final flush through the drain).
    assert saved.size >= voice.size
    tail = voice[-48000:]
    assert np.allclose(saved[-tail.size:], tail, atol=1e-3), (
        "the end of the pre-Stop audio is missing")


def test_without_the_cutoff_post_stop_audio_would_be_saved(monkeypatch, config, store):
    """Not vacuous: with the intake left open, the drain picks up audio the
    device delivered after Stop - what the cutoff exists to prevent."""
    saved, _voice, _rows_ = _stop_with_audio_buffered(monkeypatch, config, store,
                                                      cutoff=False)
    assert _marked_samples(saved) > 0


def test_closing_the_intake_is_idempotent_and_reopened_by_start(monkeypatch):
    source, stream = _live_source(monkeypatch)
    source.start()
    _deliver(source, np.zeros(4800))
    source.close_intake()
    source.close_intake()                       # again: harmless
    _deliver(source, np.full(4800, MARK))
    assert source.read(timeout=0.0) is not None  # the pre-cutoff block
    assert source.read(timeout=0.0) is None      # nothing after it
    source.stop()
    source.stop()                                # stop twice: harmless
    assert stream.closed.is_set()
    source.start()                               # a new run accepts audio again
    _deliver(source, np.zeros(4800))
    assert source.read(timeout=0.0) is not None
    source.stop()


# --- manual work on the SAME id racing the Stop's background marker writer -----
