"""Stop Monitoring stops capture and automatic chat updates immediately.

The operator's choice (2026-11-24): pressing Stop ends the run's automatic
processing at once. Every captured sample is still saved - including the
final flush - and anything not yet processed is kept as "saved, not
processed" (state SKIPPED + signal_metadata["processing_stopped"]) for
Transcribe anyway / Retry while idle. An engine call already running may
finish (a native call cannot be interrupted), but its result is neither
saved nor shown, and nothing from the stopped run - including events
queued before Stop, and completions arriving after a quick restart -
revives the chat. Manual jobs are not cancelled.

Controlled engines (blocking on events) and synthetic audio only.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp, ModeChangeRefused
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.audio.wavefile import read_wav
from babelfishr.models import ProcessingState
from babelfishr.pipeline import PROCESSING_STOPPED, is_processing_stopped
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.storage import Store
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


def _late_events(app, run):
    """Events of *run* after Stop that would add or change chat content:
    anything but a saved-not-processed indicator."""
    bad = []
    for event in app.events.drain(limit=2000):
        if event.run != run or event.kind not in ("transmission", "updated"):
            continue
        if not is_processing_stopped(event.payload):
            bad.append((event.kind, event.payload.state, event.payload.transcript))
    return bad


# --- 1. a running engine call, and queued work ------------------------------

def test_stop_discards_a_running_transcription_and_unqueues_the_rest(config, store):
    asr = SlowASR()
    app, source, session = _start(config, store, asr=asr)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET, VOICE, QUIET, VOICE, QUIET])
        assert asr.entered.wait(15.0), "the first message never reached recognition"
        assert _wait(lambda: len(_rows(store, session.id)) == 3)
        app.events.drain(limit=2000)              # what was shown before Stop

        began = time.monotonic()
        app.stop_session()
        assert time.monotonic() - began < 0.5, "Stop waited"
        asr.release.set()                          # the native call finishes
        assert _wait(lambda: app.outstanding_work() == 0)
        assert app.wait_for_session_ends(10.0)

        rows = _rows(store, session.id)
        assert len(rows) == 3
        for tx in rows:
            assert tx.state is ProcessingState.SKIPPED and is_processing_stopped(tx)
            assert tx.transcript == "" and tx.translation == ""
            assert tx.signal_metadata[PROCESSING_STOPPED]["run"] == session.id
            assert tx.audio_path and os.path.exists(tx.audio_path)
            assert tx.needs_review
        assert asr.calls == 1, "queued messages were still transcribed"
        assert _late_events(app, session.id) == []
    finally:
        asr.release.set()
        app.close()


def test_stop_discards_a_running_translation_and_keeps_the_saved_transcript(config, store):
    mt = SlowMT()
    app, source, session = _start(config, store, mt=mt)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert mt.entered.wait(15.0)
        (tx,) = _rows(store, session.id)
        transcript = store.get_transmission(tx.id).transcript
        assert transcript, "the transcript is saved before translation starts"
        app.events.drain(limit=2000)
        app.stop_session()
        mt.release.set()
        assert _wait(lambda: app.outstanding_work() == 0)
        app.wait_for_session_ends(10.0)
        tx = store.get_transmission(tx.id)
        assert tx.translation == "" and is_processing_stopped(tx)
        assert tx.transcript == transcript            # what was saved before Stop
        assert _late_events(app, session.id) == []
    finally:
        mt.release.set()
        app.close()


# --- 2. the final flush, and Stop pressed again --------------------------------

def test_stop_during_a_message_saves_every_sample_and_processes_nothing(config, store):
    asr = SlowASR()
    app, source, session = _start(config, store, asr=asr)
    try:
        pushed = _push(source, [{"gap": 0.5}, {"kind": "voice", "duration": 3.0,
                                               "level_dbfs": -14}])
        # Stop once the capture holds the audio (the test source has no
        # drainable buffer; a live input's is drained - see the test below).
        assert _wait(lambda: app.capture is not None and source._queue.empty()
                     and app.capture.detector.open)
        app.stop_session()                        # mid-message: the final flush
        assert app.stop_session() is None         # pressed again: nothing happens
        assert _wait(lambda: app.outstanding_work() == 0)
        app.wait_for_session_ends(10.0)
        (tx,) = _rows(store, session.id)
        assert is_processing_stopped(tx) and tx.state is ProcessingState.SKIPPED
        saved, rate = read_wav(tx.audio_path)
        assert rate == SR
        # The whole voice burst (3 s) is in the recording, plus pre-roll.
        assert saved.size >= int(3.0 * SR)
        assert np.max(np.abs(saved)) > 0.05
        assert asr.calls == 0 and not asr.entered.is_set()
        assert _late_events(app, session.id) == []
    finally:
        asr.release.set()
        app.close()


# --- 3. an immediate restart ------------------------------------------------------

def test_an_immediate_restart_reuses_the_processor_and_drops_old_results(config, store):
    asr = SlowASR()
    app, source, session = _start(config, store, asr=asr)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert asr.entered.wait(15.0)
        old_pipeline = app.pipeline
        app.events.drain(limit=2000)              # shown before Stop
        app.stop_session()

        # The previous call is still running: the mode cannot change under it.
        with pytest.raises(ModeChangeRefused):
            app.set_mode("record-only")

        # The stopped capture releases the audio input as its thread ends;
        # until then a start is refused rather than opening it twice.
        assert _wait(lambda: app.shutdown_problem() == "", timeout=5.0)
        source2 = CallbackAudioSource(SR, name="test-2")
        session2 = app.start_session(source=source2, name="run-2", target_language="fr")
        app.begin_capture()
        assert app.pipeline is old_pipeline, "a second processor was started beside it"
        assert app.pipeline.transcription is app.transcription
        _push(source2, [{"gap": 0.5}, VOICE, QUIET])
        assert _wait(lambda: len(_rows(store, session2.id)) == 1)
        asr.release.set()                          # old call returns, then the new one runs
        assert _wait(lambda: _rows(store, session2.id)[0].state
                     is ProcessingState.COMPLETE)
        (old,) = _rows(store, session.id)
        assert is_processing_stopped(old) and old.transcript == ""
        events = app.events.drain(limit=2000)
        assert not [e for e in events if e.run == session.id
                    and e.kind in ("transmission", "updated")
                    and not is_processing_stopped(e.payload)]
        assert [e for e in events if e.run == session2.id
                and e.kind == "updated" and e.payload.state is ProcessingState.COMPLETE]
        app.stop_session()
    finally:
        asr.release.set()
        app.close()


# --- 4. manual work: not cancelled by Stop, and the route for saved audio --------

def test_manual_processing_of_saved_audio_while_idle_and_after_a_relaunch(config, store):
    asr = SlowASR()
    app, source, session = _start(config, store, asr=asr)
    _push(source, [{"gap": 0.5}, VOICE, QUIET, VOICE, QUIET])
    assert asr.entered.wait(15.0)
    assert _wait(lambda: len(_rows(store, session.id)) == 2)
    app.stop_session()
    asr.release.set()
    assert _wait(lambda: app.outstanding_work() == 0)
    app.wait_for_session_ends(10.0)
    first, second = sorted(_rows(store, session.id), key=lambda t: t.started_at)
    app.close()

    # Relaunch: nothing resumes the stopped rows by itself.
    store.close()
    store2 = Store(config.database, recordings_dir=config.recording.directory)
    try:
        assert {t.id for t in store2.pending_transmissions()} & {first.id, second.id} == set()
        assert {first.id, second.id} <= {t.id for t in store2.review_queue()}
        app2 = BabelFishRApp(config=config, store=store2)
        app2.transcription, app2.translation = MockTranscriptionEngine(), MockTranslationEngine()
        assert app2.resume_pending() == 0
        assert app2.transcribe_anyway(first.id)          # manual, idle
        assert app2.retry(second.id)
        assert _wait(lambda: all(store2.get_transmission(i).state
                                 is ProcessingState.COMPLETE for i in (first.id, second.id)))
        for tx_id in (first.id, second.id):
            tx = store2.get_transmission(tx_id)
            assert not is_processing_stopped(tx) and tx.transcript
        # Manual updates are untagged: the window shows them.
        assert any(e.run is None and e.kind == "updated"
                   and e.payload.state is ProcessingState.COMPLETE
                   for e in app2.events.drain(limit=2000))
        app2.close()
    finally:
        store2.close()


def test_stop_does_not_cancel_a_manual_job(config, store):
    asr = SlowASR()
    asr.release.set()
    app, source, session = _start(config, store, asr=asr)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert _wait(lambda: _rows(store, session.id)
                     and _rows(store, session.id)[0].state is ProcessingState.COMPLETE)
        (done,) = _rows(store, session.id)
        asr.release.clear()
        asr.entered.clear()
        assert app.retry(done.id)                       # manual, during monitoring
        assert asr.entered.wait(15.0)
        app.stop_session()
        asr.release.set()
        assert _wait(lambda: store.get_transmission(done.id).state
                     is ProcessingState.COMPLETE)
        assert not is_processing_stopped(store.get_transmission(done.id))
    finally:
        asr.release.set()
        app.close()


# --- 5. the window: nothing late, nothing incidental -------------------------------

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


def test_the_window_shows_nothing_late_and_keeps_what_was_shown(qt_app, config, store):
    asr = SlowASR()
    app, window, source, session = _window_monitoring(qt_app, config, store, asr)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET, VOICE, QUIET])
        assert asr.entered.wait(15.0)
        assert _wait(lambda: len(_rows(store, session.id)) == 2)
        window._drain_events()
        shown = list(window.timeline.order())
        assert len(shown) == 2
        # An update queued before Stop (as if just published), not yet drawn.
        stale = store.get_transmission(shown[0])
        stale.transcript, stale.state = "late words", ProcessingState.COMPLETE
        app.events.publish("updated", stale, run=session.id)

        window._stop_monitoring()
        # Part of the Stop action: waiting rows show the stopped indicator.
        for tx_id in shown:
            bubble = window.timeline.bubble(tx_id)
            assert is_processing_stopped(bubble.tx)
            assert "saved, not processed" in bubble.status_label.text()
        asr.release.set()
        assert _wait(lambda: app.outstanding_work() == 0)
        app.wait_for_session_ends(10.0)
        window._drain_events()
        assert window.timeline.order() == shown, "the chat changed after Stop"
        for tx_id in shown:
            assert window.timeline.bubble(tx_id).tx.transcript != "late words"
            from PySide6 import QtWidgets
            texts = [w.toPlainText() for w in
                     window.timeline.bubble(tx_id).findChildren(QtWidgets.QTextEdit)]
            assert not any("late words" in t for t in texts)
    finally:
        asr.release.set()
        window.close()
        app.close()


def test_a_final_flush_is_withheld_until_the_operator_refreshes(qt_app, config, store):
    asr = SlowASR()
    app, window, source, session = _window_monitoring(qt_app, config, store, asr)
    try:
        _push(source, [{"gap": 0.5}, {"kind": "voice", "duration": 3.0, "level_dbfs": -14}])
        assert _wait(lambda: app.capture is not None and source._queue.empty()
                     and app.capture.detector.open)
        window._drain_events()
        before = list(window.timeline.order())
        window._stop_monitoring()
        assert _wait(lambda: len(_rows(store, session.id)) == 1)
        app.wait_for_session_ends(10.0)
        window._drain_events()
        (flushed,) = _rows(store, session.id)
        assert window.timeline.order() == before, "a late row was appended"
        assert flushed.id in window._withheld
        # The explicit route: View > Refresh shows the saved recording.
        window.refresh_action.trigger()
        assert flushed.id in window.timeline.order()
        assert "saved, not processed" in window.timeline.bubble(flushed.id).status_label.text()
        assert not window._withheld
    finally:
        asr.release.set()
        window.close()
        app.close()


@pytest.mark.parametrize("view", ["review", "search"])
def test_a_later_event_does_not_pull_a_withheld_row_into_a_filtered_view(
        qt_app, config, store, view):
    """Search / Needs Review re-run their query when any event arrives. A
    stopped run's row the window withheld must not come in that way - only
    when the operator opens or refreshes the view."""
    asr = SlowASR()
    asr.release.set()
    app, window, source, session = _window_monitoring(qt_app, config, store, asr)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert _wait(lambda: _rows(store, session.id)
                     and _rows(store, session.id)[0].state is ProcessingState.COMPLETE)
        window._drain_events()
        (done,) = _rows(store, session.id)
        # The view is open while monitoring goes on.
        if view == "review":
            window._show_review_queue()
        else:
            window._run_search(done.transcript.split()[0])
        asr.release.clear()
        asr.entered.clear()
        _push(source, [VOICE, QUIET])
        assert asr.entered.wait(15.0)            # the second message is running
        (late,) = [t for t in _rows(store, session.id) if t.id != done.id]
        window._drain_events()
        window._stop_monitoring()
        asr.release.set()                        # the abandoned call returns
        assert _wait(lambda: app.outstanding_work() == 0)
        app.wait_for_session_ends(10.0)
        # For Needs Review the saved-not-processed row itself qualifies. For
        # Search, the store as it would be after the race this guards
        # against: a result for the stopped run saved as Stop landed.
        row = store.get_transmission(late.id)
        if view == "search":
            row.transcript, row.state = done.transcript, ProcessingState.COMPLETE
            store.save_transmission(row)
            app.events.publish("updated", row, run=session.id)
        window._drain_events()
        assert late.id in window._withheld
        assert late.id not in window.timeline.order()

        # A later manual job (an unrelated message) makes the view re-query.
        assert app.retry(done.id)
        assert _wait(lambda: app.outstanding_work() == 0)
        window._drain_events()
        assert late.id not in window.timeline.order(), (
            "an unrelated event repopulated a stopped run's withheld row")

        # The operator refreshes: the store as it is.
        window.refresh_action.trigger()
        assert late.id in window.timeline.order()
    finally:
        asr.release.set()
        window.close()
        app.close()


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
    assert all(is_processing_stopped(t) for t in rows)
    assert _marked_samples(saved) == 0, "audio delivered after Stop was saved"
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

WRITER = "babelfishr-session-end"


class _GateBeforeClaim:
    """Wraps the app's row-claim lock: the marker writer thread pauses before
    taking it (at ``at``/``go``); every other thread passes straight through."""

    def __init__(self, real):
        self.real, self.at, self.go = real, threading.Event(), threading.Event()

    def __enter__(self):
        if threading.current_thread().name == WRITER and not self.at.is_set():
            self.at.set()
            assert self.go.wait(20.0)
        return self.real.__enter__()

    def __exit__(self, *exc):
        return self.real.__exit__(*exc)


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stopped_with_one_queued(config, store, asr):
    """A run stopped with message A running (abandoned) and B queued, so B
    is what the background writer marks. Returns app, session, B's id."""
    app, source, session = _start(config, store, asr=asr)
    _push(source, [{"gap": 0.5}, VOICE, QUIET, VOICE, QUIET])
    assert asr.entered.wait(15.0)
    assert _wait(lambda: len(_rows(store, session.id)) == 2)
    rows = sorted(_rows(store, session.id), key=lambda t: t.started_at)
    return app, session, rows[1].id


def _arm_writer_pause_after_read(monkeypatch, target):
    """Pause the writer inside its claim, after it read the row and before
    it writes: at ``at``, released by ``go``."""
    import babelfishr.app as app_module

    at, go = threading.Event(), threading.Event()
    real = app_module.mark_processing_stopped

    def paused(tx, run):
        if tx.id == target:
            at.set()
            assert go.wait(20.0)
        return real(tx, run)

    monkeypatch.setattr(app_module, "mark_processing_stopped", paused)
    return at, go


def _assert_manual_result_kept(store, tx_id):
    tx = store.get_transmission(tx_id)
    assert tx.state is ProcessingState.COMPLETE and tx.transcript
    assert not is_processing_stopped(tx), "the stop marker came back"


def test_writer_first_manual_waits_one_row_then_processes(monkeypatch, config, store):
    asr = SlowASR()
    app, session, target = _stopped_with_one_queued(config, store, asr)
    at, go = _arm_writer_pause_after_read(monkeypatch, target)
    try:
        app.stop_session()
        assert at.wait(10.0), "the writer never reached the queued row"
        asr.release.set()                         # the abandoned call returns
        assert _wait(lambda: app.outstanding_work() == 0)
        result = {}
        manual = threading.Thread(
            target=lambda: result.setdefault("ok", app.transcribe_anyway(target)))
        manual.start()
        manual.join(0.3)
        assert manual.is_alive(), "manual admission did not wait for the row write"
        assert store.get_transmission(target).state is not ProcessingState.COMPLETE
        go.set()                                  # the writer writes its marker
        manual.join(10.0)
        assert result.get("ok") is True
        assert _wait(lambda: store.get_transmission(target).state
                     is ProcessingState.COMPLETE)
        app.wait_for_session_ends(10.0)
        _assert_manual_result_kept(store, target)
    finally:
        go.set()
        asr.release.set()
        app.close()


def test_manual_completes_before_a_delayed_writer_reaches_the_row(
        monkeypatch, config, store):
    asr = SlowASR()
    app, session, target = _stopped_with_one_queued(config, store, asr)
    gate = _GateBeforeClaim(app._row_claims)
    app._row_claims = gate
    try:
        app.stop_session()
        assert gate.at.wait(10.0), "the writer never started"
        asr.release.set()
        assert _wait(lambda: app.outstanding_work() == 0)
        assert app.transcribe_anyway(target)       # not blocked: writer not holding
        assert _wait(lambda: store.get_transmission(target).state
                     is ProcessingState.COMPLETE)
        app.events.drain(limit=2000)
        gate.go.set()                              # now the writer reaches it
        assert app.wait_for_session_ends(10.0)
        _assert_manual_result_kept(store, target)
        assert not [e for e in app.events.drain(limit=2000)
                    if e.run == session.id and e.kind == "updated"
                    and e.payload.id == target], "the writer published over it"
    finally:
        gate.go.set()
        asr.release.set()
        app.close()


def test_a_manual_job_in_flight_is_skipped_by_the_writer(monkeypatch, config, store):
    asr = SlowASR()
    app, session, target = _stopped_with_one_queued(config, store, asr)
    gate = _GateBeforeClaim(app._row_claims)
    app._row_claims = gate
    try:
        app.stop_session()
        assert gate.at.wait(10.0)
        asr.release.set()
        assert _wait(lambda: app.outstanding_work() == 0)
        asr.release.clear()
        asr.entered.clear()
        assert app.transcribe_anyway(target)
        assert asr.entered.wait(10.0)              # the manual job is running
        gate.go.set()
        assert app.wait_for_session_ends(10.0)     # writer passed it by
        asr.release.set()
        assert _wait(lambda: store.get_transmission(target).state
                     is ProcessingState.COMPLETE)
        _assert_manual_result_kept(store, target)
    finally:
        gate.go.set()
        asr.release.set()
        app.close()


def test_without_the_claim_a_stale_writer_overwrites_the_manual_result(
        monkeypatch, config, store):
    """Not vacuous: with no shared claim, the writer reads the row, the
    manual job runs to completion, and the writer's stale copy lands on top."""
    asr = SlowASR()
    app, session, target = _stopped_with_one_queued(config, store, asr)
    app._row_claims = _NoLock()
    at, go = _arm_writer_pause_after_read(monkeypatch, target)
    try:
        app.stop_session()
        assert at.wait(10.0)
        asr.release.set()
        assert _wait(lambda: app.outstanding_work() == 0)
        assert app.transcribe_anyway(target)
        assert _wait(lambda: store.get_transmission(target).state
                     is ProcessingState.COMPLETE)
        go.set()
        app.wait_for_session_ends(10.0)
        tx = store.get_transmission(target)
        assert is_processing_stopped(tx) and tx.state is ProcessingState.SKIPPED
    finally:
        go.set()
        asr.release.set()
        app.close()


def test_a_stop_landing_during_the_final_save_still_ends_saved_not_processed(
        config, store):
    """Reviewer finding: the gap between the worker's last run check and its
    COMPLETE save. Stop is made to land exactly inside that save."""
    app, source, session = _start(config, store)
    real_save = app.store.save_transmission
    fired = threading.Event()

    def save(tx):
        result = real_save(tx)
        if (tx.state is ProcessingState.COMPLETE and tx.session_id == session.id
                and not fired.is_set()):
            fired.set()
            app.stop_session()                    # Stop, mid-save
        return result

    app.store.save_transmission = save
    try:
        app.events.drain(limit=2000)
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert fired.wait(15.0)
        assert _wait(lambda: app.outstanding_work() == 0)
        app.wait_for_session_ends(10.0)
        (tx,) = _rows(store, session.id)
        assert is_processing_stopped(tx) and tx.state is ProcessingState.SKIPPED
        events = app.events.drain(limit=2000)
        completed = [e for e in events if e.kind == "updated"
                     and e.payload.state is ProcessingState.COMPLETE]
        assert all(e.run == session.id for e in completed), (
            "a completed result went out untagged; the window would show it")
    finally:
        app.store.save_transmission = real_save
        app.close()


def test_capture_activity_events_carry_their_run(config, store):
    """Reviewer finding: level/state events from the drain after Stop were
    untagged and moved the window's activity indicator after Idle."""
    app, source, session = _start(config, store)
    try:
        _push(source, [{"gap": 0.5}, VOICE, QUIET])
        assert _wait(lambda: len(_rows(store, session.id)) == 1)
        app.stop_session()
        assert _wait(lambda: app.outstanding_work() == 0)
        events = app.events.drain(limit=2000)
        activity = [e for e in events if e.kind in ("level", "state")]
        assert activity
        assert all(e.run == session.id for e in activity)
    finally:
        app.close()
