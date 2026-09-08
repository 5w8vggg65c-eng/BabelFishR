"""Finishing the shutdown repair: nothing waits on the GUI thread, the last
transmission is handed over before its processor is retired, every store user
is waited for, and a failed close is never reported as success.

Substitutions, precisely: engines are the mock engines (fast) or a holding
transcription engine installed on the app; audio comes from a
CallbackAudioSource subclass whose read() blocks, ignoring its timeout, once
its queue is empty and it is held - what a stuck input looks like; the digital
analyser is a fake returned by DsdNeoAnalyser.from_config (the production
seam), blocking inside analyse() until released; store.close is replaced only
to make one close attempt fail. Everything else - the window, its Qt close,
the app, the pipeline, the store - is production code. The production capture
stop timeout (5.0 s) is asserted unchanged; nothing lowers it. These are real
Qt (offscreen) tests, not physical Mac tests.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time

import pytest

from babelfishr.app import BabelFishRApp, ProcessingBusy
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.models import AnalysisAttempt, ProcessingState
from babelfishr.pipeline import CaptureService
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
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


class HeldSource(CallbackAudioSource):
    """Delivers what was pushed; once ``hold`` is set and the queue is empty,
    read() blocks - ignoring its timeout - until released. stop() does not
    free it: only the test does. What a stuck input looks like."""

    def __init__(self, sample_rate):
        super().__init__(sample_rate, name="held")
        self.hold = threading.Event()
        self.entered_hold = threading.Event()
        self.released = threading.Event()

    def read(self, timeout: float = 1.0):
        block = super().read(timeout=0.05)
        if block is not None or self.finished or not self.hold.is_set():
            return block
        self.entered_hold.set()
        self.released.wait(60.0)
        return super().read(timeout=0.05)


class HoldingEngine(MockTranscriptionEngine):
    id = "test-holding-shutdown"
    name = "holding"

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = 0

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        self.entered.set()
        assert self.release.wait(60.0), "the test never released the engine"
        return super().transcribe(audio, sample_rate, language=language,
                                  vocabulary=vocabulary)

    def close(self):
        self.closed += 1


def push_fixture(source, spec):
    audio = build_fixture(spec, sample_rate=SR).audio
    for start in range(0, audio.size, 4800):
        source.push(audio[start:start + 4800])


def window_on_held_capture(qt_app, config, store, spec, transcription=None):
    """A window monitoring a held source with the given audio already
    consumed - the detector is left open on the last voice segment."""
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    app.transcription = transcription or MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    source = HeldSource(SR)
    app.start_session(source=source, name="held")
    app.begin_capture()
    push_fixture(source, spec)
    source.hold.set()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (
            source._queue.empty() and app.capture.detector.open):
        time.sleep(0.02)
    assert source._queue.empty() and app.capture.detector.open, (
        "the capture never consumed the audio with a transmission still open")
    assert source.entered_hold.wait(5.0), "the audio thread is not inside the held read"
    return app, window, source


def cleanup(qt_app, window, source, timer, engine=None):
    source.released.set()
    if engine is not None:
        engine.release.set()
    timer.stop()
    pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0) or window.close()


VOICE_OPEN = [{"gap": 1.0}, {"kind": "voice", "duration": 2.0, "level_dbfs": -14}]


# ---- A: Stop and Quit never wait on the GUI thread ------------------------------


def test_stop_and_quit_keep_the_gui_alive_with_a_held_source(qt_app, config, store):
    assert CaptureService.stop_timeout == 5.0, "the production timeout must stay"
    app, window, source = window_on_held_capture(qt_app, config, store, VOICE_OPEN)
    timer, ticks = heartbeat(qt_app)
    try:
        started = time.monotonic()
        window._stop_monitoring()
        assert time.monotonic() - started < 1.0, "Stop waited on the held source"
        before = len(ticks)
        pump(qt_app, 60)                           # ~0.3 s with the source still held
        assert len(ticks) >= before + 5, "the event loop did not turn during Stop"
        assert app.capture_finishing(), "the capture was treated as finished"
        assert app.outstanding_work() >= 1, "a capture that can still produce counted as nothing"
        assert "finishing" in window.status.currentMessage().lower()
        assert app.standalone_pipeline is not None and app.standalone_pipeline.accepting

        started = time.monotonic()
        assert window.close() is False, "Quit was accepted with the capture still held"
        assert time.monotonic() - started < 1.0, "Quit waited on the held source"
        before = len(ticks)
        pump(qt_app, 60)
        assert len(ticks) >= before + 5, "the event loop did not turn during Quit"
        assert window.isVisible()
        assert "quitting once" in window.status.currentMessage().lower()
        assert store.recent_transmissions() == []          # store open, nothing yet
        assert app.standalone_pipeline is not None and app.standalone_pipeline.accepting, (
            "the processor was retired while the capture could still feed it")
        assert window.close() is False                     # repeated Quit: same answer

        source.released.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0), (
            "the window never closed once the capture had finished")
        assert window._shutdown_complete
        with pytest.raises(Exception):
            store.get_transmission("x")                    # the store is closed now
        # What was saved before the store closed, read from disk through a new handle:
        from babelfishr.storage import Store
        again = Store(config.database, recordings_dir=config.recording.directory)
        rows = again.recent_transmissions()
        again.close()
        assert len(rows) == 1, "the transmission open at Stop was not handed over"
        assert pathlib.Path(rows[0].audio_path).is_file()
        assert rows[0].state is ProcessingState.COMPLETE and rows[0].transcript
        assert app.transcription is None                  # engine closed and dropped
    finally:
        cleanup(qt_app, window, source, timer)


def test_the_final_transmission_reaches_the_asr_before_the_store_closes(
        qt_app, config, store):
    """Codex's B: open transmission, lingering capture, Quit - the final
    hand-off must complete and be processed before anything closes."""
    engine = MockTranscriptionEngine()
    app, window, source = window_on_held_capture(qt_app, config, store, VOICE_OPEN,
                                                 transcription=engine)
    timer, _ = heartbeat(qt_app)
    order = []
    real_save, real_close = store.save_transmission, store.close
    store.save_transmission = lambda tx: (order.append(("save", tx.state.value)), real_save(tx))[1]
    store.close = lambda: (order.append("store-close"), real_close())[1]
    try:
        window._stop_monitoring()
        assert window.close() is False
        assert engine.calls == 0 and "store-close" not in order
        assert app.closing, "Quit is pending"

        source.released.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert engine.calls == 1, "the final transmission never reached the ASR"
        assert ("save", "complete") in order
        assert order.index(("save", "complete")) < order.index("store-close")
        assert app.outstanding_work() == 0 and app._unprocessed == []
    finally:
        cleanup(qt_app, window, source, timer)


def test_stop_only_still_processes_the_final_transmission_and_saved_recordings(
        qt_app, config, store):
    """The control case: Stop without Quit. The capture finishes off-thread,
    the processor stays, and saved-recording work is still possible after."""
    engine = MockTranscriptionEngine()
    app, window, source = window_on_held_capture(qt_app, config, store, VOICE_OPEN,
                                                 transcription=engine)
    timer, _ = heartbeat(qt_app)
    try:
        window._stop_monitoring()
        assert not app.closing
        source.released.set()
        assert pump_until(qt_app, lambda: not app.capture_finishing(), timeout=10.0)
        assert pump_until(qt_app, lambda: app.outstanding_work() == 0, timeout=20.0)
        pump(qt_app, 20)
        txs = store.recent_transmissions()
        assert len(txs) == 1 and txs[0].state is ProcessingState.COMPLETE
        assert engine.calls == 1
        assert "Idle" in window.state_badge.text(), window.state_badge.text()
        # Stop is not Quit: saved recordings can still be processed.
        assert app.shutdown_problem() == ""
        assert app.retry(txs[0].id) is True
        assert pump_until(qt_app, lambda: app.outstanding_work() == 0, timeout=20.0)
        assert engine.calls == 2
        timer.stop()
        window.close()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
    finally:
        cleanup(qt_app, window, source, timer)


def test_no_accepted_transmission_is_stranded_by_quit(qt_app, config, store):
    """Two transmissions: one already accepted and held inside the engine,
    one still open in the detector of a held capture. Quit finishes both."""
    holding = HoldingEngine()
    spec = [{"gap": 1.0}, {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
            {"gap": 1.5}, {"kind": "voice", "duration": 2.0, "level_dbfs": -12}]
    app, window, source = window_on_held_capture(qt_app, config, store, spec,
                                                 transcription=holding)
    timer, _ = heartbeat(qt_app)
    try:
        assert holding.entered.wait(10.0), "the first transmission never reached the engine"
        window._stop_monitoring()
        assert app.outstanding_work() >= 2
        assert window.close() is False
        assert holding.closed == 0

        holding.release.set()                              # first completes
        assert pump_until(qt_app, lambda: any(
            t.state is ProcessingState.COMPLETE for t in store.recent_transmissions()))
        assert window.isVisible(), "Quit went ahead with the capture still held"
        source.released.set()                              # second is flushed
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)

        from babelfishr.storage import Store
        again = Store(config.database, recordings_dir=config.recording.directory)
        rows = again.recent_transmissions()
        again.close()
        assert len(rows) == 2, [r.state for r in rows]
        assert all(r.state is ProcessingState.COMPLETE and r.transcript for r in rows)
        assert all(pathlib.Path(r.audio_path).is_file() for r in rows)
        assert holding.calls == 2 and holding.closed == 1
    finally:
        cleanup(qt_app, window, source, timer, holding)


# ---- C: every store user --------------------------------------------------------


class FakeAnalyser:
    """Stands in for dsd-neo at the production seam (from_config), which is
    consulted several times per run (availability, then the analysis
    itself), so the gates are shared by every instance. Blocks inside
    analyse() until released, then returns a real attempt."""

    entered = threading.Event()
    release = threading.Event()
    configured = True
    executable = "fake-dsd"

    def available(self):
        return True

    def unavailable_reason(self):
        return ""

    def version(self):
        return "0"

    def analyse(self, request):
        FakeAnalyser.entered.set()
        assert FakeAnalyser.release.wait(60.0)
        return AnalysisAttempt(transmission_id=request.transmission.id,
                               engine="fake-analyser", engine_version="0")


def install_fake_analyser(monkeypatch):
    from babelfishr.analysis.dsd import DsdNeoAnalyser

    FakeAnalyser.entered = threading.Event()
    FakeAnalyser.release = threading.Event()
    monkeypatch.setattr(DsdNeoAnalyser, "from_config",
                        classmethod(lambda cls, config: FakeAnalyser()))


def window_with_a_saved_message(qt_app, config, store, wav):
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
    pump(qt_app, 20)
    return app, window, app.recent_transmissions()[0]


def test_a_running_digital_analysis_saves_before_the_store_closes(qt_app, config, store,
                                                                  wav, monkeypatch):
    app, window, tx = window_with_a_saved_message(qt_app, config, store, wav)
    install_fake_analyser(monkeypatch)
    order = []
    real_save, real_close = store.save_transmission, store.close
    store.save_transmission = lambda t: (order.append("save"), real_save(t))[1]
    store.close = lambda: (order.append("store-close"), real_close())[1]
    timer, ticks = heartbeat(qt_app)
    try:
        window._on_analyze_digital(tx.id, "")              # the real window path
        assert FakeAnalyser.entered.wait(10.0), "the analysis never started"
        assert app.active_operations() == {"digital analysis": 1}

        assert window.close() is False, "Quit was accepted with an analysis still running"
        before = len(ticks)
        pump(qt_app, 40)
        assert len(ticks) >= before + 3
        assert "digital analysis" in window.status.currentMessage()
        assert "store-close" not in order and store.get_transmission(tx.id) is not None

        FakeAnalyser.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert order.index("save") < order.index("store-close"), order
        from babelfishr.storage import Store
        again = Store(config.database, recordings_dir=config.recording.directory)
        saved = again.get_transmission(tx.id)
        again.close()
        assert len(saved.analysis_attempts) == 1
        assert saved.analysis_attempts[0].engine == "fake-analyser"
    finally:
        timer.stop()
        FakeAnalyser.release.set()
        pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0) or window.close()


def test_new_work_is_refused_while_quit_is_pending(qt_app, config, store, wav,
                                                   monkeypatch):
    app, window, source = window_on_held_capture(qt_app, config, store, VOICE_OPEN)
    install_fake_analyser(monkeypatch)
    timer, _ = heartbeat(qt_app)
    try:
        window._stop_monitoring()
        assert window.close() is False
        assert app.closing
        with pytest.raises(ProcessingBusy, match="quitting"):
            app.start_session(replay_path=wav, name="too-late")
        assert app.session is None
        # A saved recording from an earlier run: exists, but is not taken now.
        from babelfishr.models import Session, Transmission
        store.save_session(Session(id="old", conversation_id=app.conversation_id))
        old = Transmission(id="old-tx", session_id="old", audio_path=wav,
                           state=ProcessingState.SKIPPED)
        store.save_transmission(old)
        assert app.transcribe_anyway("old-tx") is False
        assert app.retry("old-tx") is False
        assert "quitting" in app.processing_problem("old-tx")
        assert app.analyze_digital("old-tx") is None
        assert not FakeAnalyser.entered.is_set()
        window._on_analyze_digital("old-tx", "")
        assert "not started" in window.status.currentMessage()
        pump(qt_app, 10)
        assert not FakeAnalyser.entered.is_set()
        source.released.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
    finally:
        cleanup(qt_app, window, source, timer)


# ---- D: a failed close is not success -------------------------------------------


def test_a_failed_close_is_not_reported_complete_and_retries_safely(qt_app, config,
                                                                    store, wav,
                                                                    monkeypatch):
    """The store refuses to close. The cleanup runs on the application's own
    thread, so the outcome arrives a tick later - and it is a failure, shown,
    retried, and never reported as complete."""
    app, window, tx = window_with_a_saved_message(qt_app, config, store, wav)
    engine = app.transcription
    closes = {"engine": 0}
    real_engine_close = engine.close
    engine.close = lambda: (closes.__setitem__("engine", closes["engine"] + 1),
                            real_engine_close())[1]

    def failing_close():
        raise OSError("disk went away")

    monkeypatch.setattr(store, "close", failing_close)
    assert window.close() is False, "a close still to run was reported done"
    assert pump_until(qt_app, lambda: "could not finish quitting" in
                      window.status.currentMessage().lower(), timeout=10.0), (
        window.status.currentMessage())
    assert "disk went away" in window.status.currentMessage()
    assert not window._shutdown_complete and window.isVisible()
    assert window._quit_timer.isActive()
    assert closes["engine"] == 1
    pump(qt_app, 80)                                       # retries keep failing
    assert not window._shutdown_complete and window.isVisible()
    assert closes["engine"] == 1, "a retry closed the engine again"
    assert app.transcription is None and not app._closed

    monkeypatch.undo()                                      # the obstacle is gone
    assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
    assert window._shutdown_complete and app._closed
    assert closes["engine"] == 1
    assert not window._quit_timer.isActive() and not window._timer.isActive()
