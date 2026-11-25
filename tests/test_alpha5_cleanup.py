"""The terminal phase of Stop and Quit: an already-ended capture whose source
is slow to close, an idle worker slow to leave, an engine slow to close, a
database slow to close. None of it may wait on the GUI thread; ownership is
kept until each genuinely completes; repeated Quit never overlaps; nothing
reads the store once its closing has begun.

Substitutions, precisely: a CallbackAudioSource subclass that reports itself
finished once its queue is empty (so the audio thread ends on its own) and
whose stop() blocks until released; the live pipeline queue's get() wrapped
so the worker holds after receiving its stop sentinel (production 5 s join
timeout untouched, and not reached: nothing joins on the GUI thread);
MockTranscriptionEngine.close and Store.close replaced on the instance by
wrappers that block until released, then call the real method. Everything
else - window, Qt close, app, pipeline, store - is production code. Real Qt
(offscreen) tests, not physical Mac tests.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.models import ProcessingState
from babelfishr.pipeline import CaptureService
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.testing import build_fixture

SR = 48_000
VOICE_OPEN = [{"gap": 1.0}, {"kind": "voice", "duration": 2.0, "level_dbfs": -14}]


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
                          {"gap": 1.0}], sample_rate=SR).write(str(tmp_path / "one.wav"))


def pump(qt_app, rounds: int = 20) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.004)


def pump_until(qt_app, condition, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        pump(qt_app, 5)
    return condition()


def heartbeat(qt_app):
    from PySide6 import QtCore

    ticks = []
    timer = QtCore.QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    return timer, ticks


def alive_beat(qt_app, ticks, rounds: int = 60) -> None:
    """The event loop must turn while whatever is held stays held."""
    before = len(ticks)
    pump(qt_app, rounds)
    assert len(ticks) >= before + 5, "the event loop did not turn"


class Held:
    """Wraps a callable so it blocks until released, then runs for real."""

    def __init__(self, real):
        self.real = real
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(60.0), "the test never released the hold"
        return self.real(*args, **kwargs)


class EndingSource(CallbackAudioSource):
    """Finished once its queue is empty, so the audio thread ends on its own;
    stop() is held until the test releases it."""

    def __init__(self, sample_rate):
        super().__init__(sample_rate, name="ending")
        self.stop_held = Held(super().stop)
        self.ending = threading.Event()

    def read(self, timeout: float = 1.0):
        block = super().read(timeout=0.05)
        if block is None and self.ending.is_set() and self._queue.empty():
            self._finished = True                          # like a replay reaching its end
        return block

    def stop(self):
        self.stop_held()


def push_fixture(source, spec):
    audio = build_fixture(spec, sample_rate=SR).audio
    for start in range(0, audio.size, 4800):
        source.push(audio[start:start + 4800])


def window_with_app(qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app, window


def saved_message(qt_app, app, wav):
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    app.stop_session()
    pump(qt_app, 20)                    # the replay's own events are drained
    return app.recent_transmissions()[0]


def cleanup_threads():
    return [t for t in threading.enumerate()
            if t.name == "babelfishr-cleanup" and t.is_alive()]


def finish(qt_app, window, *holds):
    for hold in holds:
        hold.release.set()
    pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0) or window.close()


# ---- 1. already-ended capture, slow source close ---------------------------------


def test_stop_does_not_wait_on_a_slow_source_close_after_the_thread_ended(
        qt_app, config, store):
    assert CaptureService.stop_timeout == 5.0
    app, window = window_with_app(qt_app, config, store)
    source = EndingSource(SR)
    app.start_session(source=source, name="ending")
    app.begin_capture()
    push_fixture(source, VOICE_OPEN)
    source.ending.set()
    assert pump_until(qt_app, lambda: not app.capture.alive, timeout=10.0), (
        "the audio thread never ended on its own")
    assert app.capture.detector.open, "the last transmission should still be open"
    capture = app.capture
    timer, ticks = heartbeat(qt_app)
    try:
        started = time.monotonic()
        window._stop_monitoring()
        assert time.monotonic() - started < 1.0, "Stop waited on source.stop()"
        assert source.stop_held.entered.wait(5.0)
        alive_beat(qt_app, ticks)                          # source.stop still held
        assert app.capture_finishing(), "a capture still closing counted as finished"
        assert store.recent_transmissions() == [], "flushed before the source closed"
        assert app.standalone_pipeline is not None and app.standalone_pipeline.accepting

        source.stop_held.release.set()
        assert pump_until(qt_app, lambda: not app.capture_finishing(), timeout=10.0), (
            f"alive={capture.alive} stopping={capture.stopping} finished={capture._finished} "
            f"stopper={capture._stopper} thread={capture._thread}")
        assert pump_until(qt_app, lambda: app.outstanding_work() == 0, timeout=20.0)
        txs = store.recent_transmissions()
        assert len(txs) == 1 and txs[0].state is ProcessingState.COMPLETE
        assert pathlib.Path(txs[0].audio_path).is_file()
        assert source.stop_held.calls == 1
    finally:
        timer.stop()
        finish(qt_app, window, source.stop_held)


# ---- 2. idle worker slow to leave -----------------------------------------------


def hold_worker_at_its_sentinel(pipeline):
    """The worker receives its stop sentinel and then does not return."""
    held = Held(lambda: None)
    installed = threading.Event()
    real_get = pipeline._queue.get

    def get(*args, **kwargs):
        installed.set()
        item = real_get(*args, **kwargs)
        if item is None:
            held()
        return item

    pipeline._queue.get = get
    # The idle worker is inside the previous, unwrapped get(timeout=0.5);
    # wait for it to come round once so the sentinel goes through the hold.
    assert installed.wait(5.0), "the worker never came round to the hold"
    return held


def test_retiring_an_idle_worker_joins_nothing_on_the_gui_thread(qt_app, config, store,
                                                                 wav):
    app, window = window_with_app(qt_app, config, store)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()                                       # capture done, worker idle
    pipeline = app.pipeline
    held = hold_worker_at_its_sentinel(pipeline)
    timer, ticks = heartbeat(qt_app)
    try:
        started = time.monotonic()
        window._stop_monitoring()
        assert time.monotonic() - started < 1.0, "Stop joined the worker"
        assert held.entered.wait(5.0), "the worker never took its sentinel"
        alive_beat(qt_app, ticks)
        assert not pipeline.finished and not pipeline.accepting
        assert pipeline in app._retired, "the leaving worker was forgotten"
        assert app.standalone_pipeline is None
        # An idle worker merely leaving blocks nothing new...
        assert app.shutdown_problem() == ""
        # ...but Quit waits for it, without joining it here.
        started = time.monotonic()
        assert window.close() is False
        assert time.monotonic() - started < 1.0
        alive_beat(qt_app, ticks)
        assert not app.cleaning, "cleanup began with a worker still alive"
        assert window.isVisible()

        held.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert pipeline.finished and app._retired == [] and app._closed
    finally:
        timer.stop()
        finish(qt_app, window, held)


# ---- 3 and 4. slow engine close, slow database close -----------------------------


def test_quit_does_not_wait_on_a_slow_engine_close(qt_app, config, store, wav):
    app, window = window_with_app(qt_app, config, store)
    tx = saved_message(qt_app, app, wav)
    engine = app.transcription
    engine.close = Held(engine.close)
    reads = {"is_deleted": 0}
    real_is_deleted = store.is_deleted
    # Counted only once the cleanup has begun: before that, draining the
    # replay's own events reads the store legitimately.
    store.is_deleted = lambda tx_id: (reads.__setitem__(
        "is_deleted", reads["is_deleted"] + (1 if app.cleaning else 0)),
        real_is_deleted(tx_id))[1]
    timer, ticks = heartbeat(qt_app)
    try:
        started = time.monotonic()
        assert window.close() is False
        assert time.monotonic() - started < 1.0
        assert pump_until(qt_app, lambda: engine.close.entered.is_set(), timeout=10.0), (
            "the cleanup never reached the engine")
        alive_beat(qt_app, ticks)                          # engine.close still held
        assert app.cleaning and not app._closed
        assert not window._timer.isActive(), "the event drain still ran with the store closing"
        assert not window.centralWidget().isEnabled()
        # A late event and a direct drain: nothing reads the store.
        app.events.publish("transmission", tx)
        window._drain_events()
        assert reads["is_deleted"] == 0
        assert store.get_transmission(tx.id) is not None    # not closed yet: engines first
        assert "closing the engines and the database" in window.status.currentMessage()
        assert len(cleanup_threads()) == 1

        engine.close.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert app._closed and engine.close.calls == 1 and app.transcription is None
        assert reads["is_deleted"] == 0
    finally:
        timer.stop()
        finish(qt_app, window, engine.close)


def test_quit_does_not_wait_on_a_slow_database_close(qt_app, config, store, wav,
                                                     monkeypatch):
    app, window = window_with_app(qt_app, config, store)
    tx = saved_message(qt_app, app, wav)
    held = Held(store.close)
    monkeypatch.setattr(store, "close", held)
    timer, ticks = heartbeat(qt_app)
    try:
        started = time.monotonic()
        assert window.close() is False
        assert time.monotonic() - started < 1.0
        assert pump_until(qt_app, lambda: held.entered.is_set(), timeout=10.0), (
            "the cleanup never reached the store")
        alive_beat(qt_app, ticks)                          # store.close still held
        assert app.cleaning and not app._closed and not app._store_closed
        assert not window._timer.isActive()
        app.events.publish("transmission", tx)
        window._drain_events()                             # must not touch the store
        assert window.close() is False                     # repeated Quit: no second cleanup
        assert len(cleanup_threads()) == 1

        held.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert app._closed and app._store_closed and held.calls == 1
    finally:
        timer.stop()
        finish(qt_app, window, held)


# ---- 5. repeated Quit, one cleanup ------------------------------------------------


def test_repeated_quit_runs_one_cleanup_and_closes_each_resource_once(qt_app, config,
                                                                      store, wav):
    app, window = window_with_app(qt_app, config, store)
    saved_message(qt_app, app, wav)
    engine = app.transcription
    engine.close = Held(engine.close)
    translation = app.translation
    translation.close = Held(translation.close)
    translation.close.release.set()                        # only the ASR engine is slow
    timer, _ = heartbeat(qt_app)
    try:
        for _ in range(3):
            assert window.close() is False
            pump(qt_app, 5)
        assert pump_until(qt_app, lambda: engine.close.entered.is_set(), timeout=10.0)
        for _ in range(3):
            assert window.close() is False                 # while the cleanup runs
            pump(qt_app, 5)
        assert len(cleanup_threads()) == 1
        assert engine.close.calls == 1

        engine.close.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert engine.close.calls == 1 and translation.close.calls == 1
        assert app._closed
        assert app.close(wait=False) is True               # already closed: nothing more
        assert engine.close.calls == 1
    finally:
        timer.stop()
        finish(qt_app, window, engine.close)
