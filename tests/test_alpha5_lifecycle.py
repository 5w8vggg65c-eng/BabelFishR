"""Worker lifetime, mode transitions, and a Stop or Quit that never freezes.

What is substituted, and where: transcription engines are stand-ins
registered with the production factory under their own ids and selected
through ``config.asr.engine`` like any real engine; the cloud translator is a
stand-in registered under the real Claude id, so the factory's Field Offline
guard applies to it exactly as to the real one; audio comes from replay
fixtures or a callback source. The database and every recording live in a
temporary directory. No network, no device, no real model.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time

import pytest

from babelfishr.app import BabelFishRApp, ModeChangeRefused, ProcessingBusy
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.models import ProcessingState
from babelfishr.modes import OperatingMode
from babelfishr.pipeline import CaptureService, PipelineState
from babelfishr.providers.base import PrivacyProfile, TranslationResult
from babelfishr.providers.mock import MockTranscriptionEngine
from babelfishr.testing import build_fixture

SR = 48_000


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def wav(tmp_path) -> str:
    return build_fixture([{"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
                          {"gap": 1.0}], sample_rate=SR).write(
        str(tmp_path / "one.wav"))


def pump(qt_app, rounds: int = 20) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.004)


class HoldingEngine(MockTranscriptionEngine):
    """Blocks inside transcribe() until the test lets it go."""

    id = "test-holding"
    name = "Test holding transcription"

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        self.entered.set()
        assert self.release.wait(60.0), "the test never released the engine"
        return super().transcribe(audio, sample_rate, language=language,
                                  vocabulary=vocabulary)

    def close(self):
        self.closed = True


class FakeCloudTranslation:
    """Stands in for the Claude provider, under its id.

    Registered under ``"claude"`` so the production factory's mode guard
    (``_check_mode`` -> ``guard_cloud``) applies to it exactly as to the real
    provider. Every entry records the operating mode in force at that moment:
    the assertion that matters is made here, at the call boundary.
    """

    id = "claude"
    name = "fake cloud translation"
    version = "0"
    privacy = PrivacyProfile(is_cloud=True, sends_text=True,
                             destination="a fake cloud service")

    def __init__(self, config):
        self.config = config
        self.entries = []
        self.closed = False

    def available(self):
        return True

    def unavailable_reason(self):
        return ""

    def translate(self, text, target_language, *, source_language=None,
                  glossary=None, do_not_translate=None):
        self.entries.append(self.config.operating_mode())
        return TranslationResult(text=f"cloud: {text}", engine=self.id,
                                 engine_version=self.version)

    def close(self):
        self.closed = True


@pytest.fixture
def engines(monkeypatch):
    """Register the stand-ins with the production factories."""
    import babelfishr.providers as providers

    created = {"holding": [], "claude": []}
    real_asr = providers._transcription_factories
    real_mt = providers._translation_factories

    def asr(config=None, mode=None):
        table = real_asr(config, mode)

        def make():
            engine = HoldingEngine()
            created["holding"].append(engine)
            return engine
        table[HoldingEngine.id] = make
        return table

    def mt(config=None, mode=None):
        table = real_mt(config, mode)

        def make(config=config):
            engine = FakeCloudTranslation(config)
            created["claude"].append(engine)
            return engine
        table["claude"] = make
        return table

    monkeypatch.setattr(providers, "_transcription_factories", asr)
    monkeypatch.setattr(providers, "_translation_factories", mt)
    yield created
    for engine in created["holding"]:
        engine.release.set()          # never leave a worker parked


def saved_recording(config, store, wav, *, translation="mock"):
    """One recorded, unprocessed message and an Online/Setup app to process it."""
    config.asr.engine = HoldingEngine.id
    config.translate.engine = translation
    app = BabelFishRApp(config=config, store=store)
    app.set_mode(OperatingMode.RECORD_ONLY, persist=False)
    app.select_engines()
    app.start_session(replay_path=wav, name="record")
    app.run_replay()
    app.stop_session()
    app.set_mode(OperatingMode.ONLINE_SETUP, persist=False)
    saved = [t for t in store.recent_transmissions() if t.audio_path and not t.transcript]
    assert saved, "nothing was recorded to process later"
    return app, saved[0]


def hold_after_dequeue(pipeline):
    """Freeze the worker between queue.get() and the work.

    The id is then dequeued but not yet running - the hand-off an earlier
    version counted as nothing, because it added the queue depth to an
    "active" figure incremented only after this point.
    """
    armed = threading.Event()
    gate = threading.Event()
    installed = threading.Event()
    real_get = pipeline._queue.get

    def get(*args, **kwargs):
        installed.set()
        item = real_get(*args, **kwargs)
        if item is not None and not armed.is_set():
            armed.set()
            assert gate.wait(60.0), "the test never opened the gate"
        return item

    pipeline._queue.get = get
    # The idle worker is inside the previous, unwrapped get(timeout=0.5).
    # Wait for it to come round once so the next item goes through the hold.
    assert installed.wait(5.0), "the worker never came round to the hold"
    return armed, gate


def processing_threads():
    return [t for t in threading.enumerate()
            if t.name.startswith("babelfishr-processing") and t.is_alive()]


# ---- accounting and the mode transition ----------------------------------------


def test_a_job_between_the_queue_and_the_worker_still_counts(config, store, wav,
                                                             engines):
    app, tx = saved_recording(config, store, wav)
    pipeline = app._processing_pipeline()
    assert pipeline is not None
    armed, gate = hold_after_dequeue(pipeline)
    assert app.transcribe_anyway(tx.id)
    assert armed.wait(10.0), "the worker never took the job"

    # Dequeued, not running: exactly the hand-off.
    assert pipeline.pending == 1
    assert pipeline.is_in_flight(tx.id)
    assert "still being processed" in app.mode_change_problem()
    with pytest.raises(ModeChangeRefused):
        app.set_mode(OperatingMode.FIELD_OFFLINE, persist=False)
    assert app.mode is OperatingMode.ONLINE_SETUP
    assert app.standalone_pipeline is pipeline and pipeline.accepting

    gate.set()
    engine = engines["holding"][-1]
    assert engine.entered.wait(10.0)
    engine.release.set()
    assert pipeline.wait_until_idle(30.0)
    app.set_mode(OperatingMode.FIELD_OFFLINE, persist=False)
    assert app.standalone_pipeline is None and engine.closed
    assert app.close()


def test_the_cloud_translator_is_never_entered_once_field_offline_is_active(
        config, store, wav, engines):
    app, tx = saved_recording(config, store, wav, translation="claude")
    pipeline = app._processing_pipeline()
    assert pipeline is not None and pipeline.translation.id == "claude"
    armed, gate = hold_after_dequeue(pipeline)
    assert app.transcribe_anyway(tx.id)
    assert armed.wait(10.0)

    with pytest.raises(ModeChangeRefused):
        app.set_mode(OperatingMode.FIELD_OFFLINE, persist=False)
    assert app.mode is OperatingMode.ONLINE_SETUP, "the mode moved with a job in flight"

    gate.set()
    holding = engines["holding"][-1]
    assert holding.entered.wait(10.0)
    holding.release.set()
    assert pipeline.wait_until_idle(30.0)
    cloud = engines["claude"][-1]
    assert cloud.entries == [OperatingMode.ONLINE_SETUP], (
        "the cloud boundary was reached other than once, online")

    app.set_mode(OperatingMode.FIELD_OFFLINE, persist=False)
    assert cloud.closed

    # A second recording, processed in Field Offline: the factory refuses the
    # cloud provider before it is even constructed, and no fake ever sees the
    # offline mode at its call boundary.
    made_before = len(engines["claude"])
    config.asr.engine = "mock"        # transcription is not what this phase tests
    app.select_engines()
    app.start_session(replay_path=wav, name="offline")
    app.run_replay()
    app.stop_session()
    for pipe in (app.standalone_pipeline,):
        if pipe is not None:
            assert pipe.wait_until_idle(30.0)
    assert len(engines["claude"]) == made_before, "a cloud engine was built offline"
    assert all(mode is OperatingMode.ONLINE_SETUP
               for engine in engines["claude"] for mode in engine.entries)
    assert store.get_transmission(tx.id).translation.startswith("cloud:")
    assert app.close()


def test_duplicate_submissions_run_once_and_pending_stays_honest(config, store,
                                                                 wav, engines):
    app, tx = saved_recording(config, store, wav)
    pipeline = app._processing_pipeline()
    armed, gate = hold_after_dequeue(pipeline)
    assert pipeline.submit(tx.id) is True
    assert pipeline.submit(tx.id) is False, "the same id was queued twice"
    assert pipeline.pending == 1
    assert armed.wait(10.0)
    assert pipeline.pending == 1
    gate.set()
    engine = engines["holding"][-1]
    assert engine.entered.wait(10.0)
    engine.release.set()
    assert pipeline.wait_until_idle(30.0)
    assert engine.calls == 1
    assert pipeline.pending == 0
    assert app.close()


# ---- workers that outlive a stop --------------------------------------------------


def test_a_worker_that_outlives_the_stop_timeout_stays_tracked(config, store, wav,
                                                               engines):
    app, tx = saved_recording(config, store, wav, translation="claude")
    pipeline = app._processing_pipeline()
    assert app.transcribe_anyway(tx.id)
    engine = engines["holding"][-1]
    assert engine.entered.wait(10.0)

    # A deliberately short timeout: the worker is inside the engine.
    assert pipeline.stop(wait=True, timeout=0.2) is False
    assert not pipeline.finished
    assert pipeline.worker_threads(), "the surviving worker was forgotten"
    assert pipeline.pending == 1, "a timeout was reported as completion"
    assert not pipeline.accepting
    from babelfishr.pipeline import ProcessingStopped

    with pytest.raises(ProcessingStopped):
        pipeline.submit("anything-else")

    # Nothing it can still reach is closed under it.
    assert app.close(wait=False) is False
    assert not engine.closed
    assert store.get_transmission(tx.id) is not None, "the store was closed"
    assert app.mode_change_problem(), "the mode could change over a live worker"

    engine.release.set()
    for thread in pipeline.worker_threads():
        thread.join(10.0)
    assert pipeline.finished and pipeline.pending == 0
    after = store.get_transmission(tx.id)
    assert after.transcript, "the finished transcription was lost"
    # The translation stage did not run on a processor told to stop, and the
    # record says exactly that rather than claiming completion.
    assert after.state is ProcessingState.FAILED
    assert after.error.stage == "translation" and "stopped" in after.error.message
    assert engines["claude"][-1].entries == [], "a stopped processor called an engine"

    assert app.close(wait=True) is True
    assert engine.closed


def test_a_stopped_processor_refuses_new_work_and_is_replaced(config, store, wav,
                                                              engines):
    app, tx = saved_recording(config, store, wav)
    pipeline = app._processing_pipeline()
    assert pipeline.stop_if_idle(timeout=5.0) is True
    assert pipeline.finished
    from babelfishr.pipeline import ProcessingStopped

    with pytest.raises(ProcessingStopped):
        pipeline.submit(tx.id)
    assert pipeline.retry(tx.id) is False

    # The application notices the finished, stopped processor and replaces it.
    assert app.transcribe_anyway(tx.id)
    assert app.standalone_pipeline is not pipeline
    engine = engines["holding"][-1]
    assert engine.entered.wait(10.0)
    engine.release.set()
    assert app.standalone_pipeline.wait_until_idle(30.0)
    assert app.close()


def test_a_straggler_blocks_starts_and_mode_changes_until_it_has_left(config, store,
                                                                       wav, engines):
    """The application's rules for a stopped processor whose worker has not
    returned, exercised with a stand-in that reports exactly that."""
    app, tx = saved_recording(config, store, wav)

    class Straggler:
        alive = True
        accepting = False
        pending = 0

        @property
        def finished(self):
            return not self.alive

        def worker_threads(self):
            return []

    straggler = Straggler()
    app._track_retired(straggler)
    assert "shutting down" in app.mode_change_problem()
    with pytest.raises(ModeChangeRefused):
        app.set_mode(OperatingMode.FIELD_OFFLINE, persist=False)
    with pytest.raises(ProcessingBusy):
        app.start_session(replay_path=wav, name="beside-a-straggler")
    assert app.session is None
    assert "shutting down" in app.processing_problem(tx.id)
    assert app.transcribe_anyway(tx.id) is False
    assert app.close(wait=False) is False
    assert store.get_transmission(tx.id) is not None

    straggler.alive = False
    assert app.mode_change_problem() == ""
    assert app.close(wait=True) is True


def test_a_failed_start_with_running_workers_leaves_none_behind(config, store, wav,
                                                                monkeypatch):
    app = BabelFishRApp(config=config, store=store)
    before = set(processing_threads())

    def no_input(self, *args, **kwargs):
        raise RuntimeError("the interface is not connected")

    monkeypatch.setattr(CaptureService, "__init__", no_input)
    with pytest.raises(RuntimeError, match="not connected"):
        app.start_session(replay_path=wav, name="doomed")
    monkeypatch.undo()

    assert app.pipeline is None and app.session is None
    assert set(processing_threads()) - before == set(), (
        "the failed start left workers running")
    assert app._retired == [] and app.shutdown_problem() == ""
    app.start_session(replay_path=wav, name="fine")
    app.run_replay()
    app.stop_session()
    assert app.close()


# ---- Stop and Quit through the window --------------------------------------------


def heartbeat(qt_app):
    from PySide6 import QtCore

    ticks = []
    timer = QtCore.QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    return timer, ticks


def window_with_a_held_job(qt_app, config, store, wav, engines):
    from babelfishr.ui.main_window import MainWindow

    config.translate.engine = "mock"
    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)          # readiness smoke tests run on mock engines
    window.show()
    pump(qt_app)
    # From here the factory hands out the holding engine, and the live
    # pipeline is built with it - the readiness check never sees it.
    config.asr.engine = HoldingEngine.id
    app.select_engines()
    app.start_session(replay_path=wav, name="live")
    app.begin_capture()
    engine = app.pipeline.transcription
    assert isinstance(engine, HoldingEngine)
    assert engine.entered.wait(15.0), "the worker never entered the engine"
    return app, window, engine


def cleanup(qt_app, window, engine, timer):
    """Never leave a parked worker, a live heartbeat or an open window behind
    for the next test: that is how one failure becomes a crash later."""
    engine.release.set()
    timer.stop()
    if window.isVisible():
        deadline = time.monotonic() + 20.0
        while window.isVisible() and time.monotonic() < deadline:
            window.close()
            pump(qt_app, 5)


def test_stop_monitoring_returns_at_once_and_the_window_keeps_breathing(
        qt_app, config, store, wav, engines):
    app, window, engine = window_with_a_held_job(qt_app, config, store, wav, engines)
    timer, ticks = heartbeat(qt_app)
    try:
        _stop_monitoring_body(qt_app, app, window, engine, timer, ticks, wav)
    finally:
        cleanup(qt_app, window, engine, timer)


def _stop_monitoring_body(qt_app, app, window, engine, timer, ticks, wav):
    started = time.monotonic()
    window._stop_monitoring()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"Stop blocked the GUI thread for {elapsed:.1f}s"
    before = len(ticks)
    pump(qt_app, 30)
    assert len(ticks) >= before + 3, "the event loop did not turn while work was held"

    assert app.session is None and app.capture is None
    assert app.standalone_pipeline is not None
    assert app.standalone_pipeline.pending == 1, "the held job was dropped"
    assert "Transcribing" in window.state_badge.text(), window.state_badge.text()
    assert "finishing" in window.status.currentMessage().lower()
    assert window.start_button.text() == "Start monitoring"

    window._stop_monitoring()             # a second Stop changes nothing
    with pytest.raises(ProcessingBusy):
        app.start_session(replay_path=wav, name="too-soon")

    engine.release.set()
    assert app.standalone_pipeline.wait_until_idle(30.0)
    pump(qt_app, 40)
    tx = app.recent_transmissions()[0]
    assert tx.state is ProcessingState.COMPLETE and tx.transcript
    assert "Idle" in window.state_badge.text(), window.state_badge.text()
    timer.stop()
    assert window.close() is True


def test_quit_waits_on_the_event_loop_and_closes_in_order(qt_app, config, store,
                                                          wav, engines):
    app, window, engine = window_with_a_held_job(qt_app, config, store, wav, engines)
    timer, ticks = heartbeat(qt_app)
    try:
        _quit_body(qt_app, app, window, engine, timer, ticks, store)
    finally:
        cleanup(qt_app, window, engine, timer)


def _quit_body(qt_app, app, window, engine, timer, ticks, store):
    order = []
    real_engine_close, real_store_close = engine.close, store.close
    real_save = store.save_transmission

    def save(tx):
        order.append(("save", tx.state.value))
        return real_save(tx)

    engine.close = lambda: (order.append("engine"), real_engine_close())
    store.close = lambda: (order.append("store"), real_store_close())
    store.save_transmission = save

    started = time.monotonic()
    assert window.close() is False, "the close was accepted with a worker inside the engine"
    assert time.monotonic() - started < 2.0
    assert window.isVisible()
    before = len(ticks)
    pump(qt_app, 30)
    assert len(ticks) >= before + 3, "the event loop did not turn while quitting"
    assert "quitting once" in window.status.currentMessage().lower()
    assert "engine" not in order and "store" not in order
    assert store.get_transmission(app.recent_transmissions()[0].id) is not None
    assert window.close() is False, "a repeated Quit was treated differently"

    engine.release.set()
    deadline = time.monotonic() + 20.0
    while window.isVisible() and time.monotonic() < deadline:
        pump(qt_app, 5)
    assert not window.isVisible(), "the window never closed once the work finished"
    assert window._shutdown_complete and not window._timer.isActive()
    assert order[-2:] == ["engine", "store"], order
    assert ("save", "complete") in order
    assert order.index(("save", "complete")) < order.index("engine")
    assert engine.closed

    # A late event finds the drain stopped, on the GUI thread, and nothing
    # reaches the closed store.
    app.events.publish("state", PipelineState.COMPLETE)
    assert threading.current_thread() is threading.main_thread()
    window._drain_events()
    timer.stop()


def test_the_event_drain_never_touches_a_closed_store(qt_app, config, store, wav):
    """The traceback seen before: a QTimer callback on the GUI thread reaching
    ``store.is_deleted`` after ``app.close()`` had closed the connection."""
    from babelfishr.providers.mock import (MockTranscriptionEngine,
                                           MockTranslationEngine)
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    app.stop_session()
    tx = app.recent_transmissions()[0]
    assert window.close() is True
    assert window._shutdown_complete and not window._timer.isActive()

    app.events.publish("transmission", tx)
    assert threading.current_thread() is threading.main_thread()
    window._drain_events()               # raised sqlite3.ProgrammingError before


# ---- the capture's own shutdown ----------------------------------------------------


def push_fixture(source, spec):
    audio = build_fixture(spec, sample_rate=SR).audio
    block = 4800
    for start in range(0, audio.size, block):
        source.push(audio[start:start + block])


def test_capture_shutdown_keeps_the_final_transmission_and_its_recording(config, store):
    from babelfishr.providers.mock import (MockTranscriptionEngine,
                                           MockTranslationEngine)

    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    source = CallbackAudioSource(SR)
    app.start_session(source=source, name="live")
    app.begin_capture()
    # Voice still going when the operator presses Stop: nothing has closed it.
    push_fixture(source, [{"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14}])
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (
            source._queue.empty() and app.capture.detector.open):
        time.sleep(0.02)
    assert source._queue.empty(), "the capture thread never consumed the audio"
    assert app.capture.detector.open, "the detector never opened on the voice"

    app.stop_session()
    txs = store.recent_transmissions()
    assert len(txs) == 1, "the transmission still open at Stop was lost"
    assert pathlib.Path(txs[0].audio_path).is_file()
    if app.standalone_pipeline is not None:
        assert app.standalone_pipeline.wait_until_idle(30.0)
    assert store.get_transmission(txs[0].id).state is ProcessingState.COMPLETE
    assert app.close()


class StuckSource(CallbackAudioSource):
    """A source whose read ignores its timeout until the test lets it go."""

    def __init__(self, sample_rate):
        super().__init__(sample_rate, name="stuck")
        self.released = threading.Event()

    def read(self, timeout: float = 1.0):
        assert self.released.wait(60.0)
        return super().read(timeout=0.05)


def test_a_capture_thread_that_ignores_its_stop_is_kept_in_view(config, store, wav,
                                                                monkeypatch):
    from babelfishr.providers.mock import (MockTranscriptionEngine,
                                           MockTranslationEngine)

    monkeypatch.setattr(CaptureService, "stop_timeout", 0.2)
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    source = StuckSource(SR)
    app.start_session(source=source, name="stuck")
    app.begin_capture()
    capture = app.capture
    time.sleep(0.1)

    started = time.monotonic()
    app.stop_session()
    assert time.monotonic() - started < 2.0
    assert app.session is None and app.capture is None
    assert app._lingering_capture is capture and capture.alive
    assert not capture._finished, "the run was finished under a thread still feeding it"
    assert "audio input" in app.shutdown_problem()
    with pytest.raises(ProcessingBusy):
        app.start_session(replay_path=wav, name="beside-it")
    assert app.close(wait=False) is False
    assert store.recent_transmissions() == []

    source.released.set()
    capture._thread.join(10.0)
    assert not capture.alive and capture._finished
    assert capture.state == PipelineState.IDLE
    assert app.shutdown_problem() == ""
    assert app.close(wait=True) is True
