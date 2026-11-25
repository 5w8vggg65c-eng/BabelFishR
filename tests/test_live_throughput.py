"""Sustained live monitoring through the real pipeline, with one costly clip.

The whole application path - CallbackAudioSource -> CaptureService ->
detector -> WAV + row -> ProcessingPipeline -> engines - with a scripted
speech engine that holds one clip for as long as the test likes. What this
proves: while that clip runs, monitoring stays armed and keeps saving and
queueing the clips behind it, in arrival order; the processor can say which
clip it holds, at what stage, and which wait in which order; once the costly
clip finishes, every later clip completes while monitoring is still on, in
capture order, each with its own automatically identified language; an
engine that breaks on one clip (a handled error or an unexpected exception)
fails that clip only and later clips carry on; Stop still returns at once
and finishes what was accepted.

What it cannot prove: how long Large v3 really takes on a Mac. Timing is
scripted here; see tests/test_whisper_encode_reuse.py for the engine side.

Synthetic audio and scripted engines only.
"""

from __future__ import annotations

import threading
import time

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.models import ProcessingState
from babelfishr.pipeline import QueueSnapshot
from babelfishr.providers.base import EngineError
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.testing import build_fixture

SR = 48_000
GAP = {"gap": 1.6}                       # well past the fixture's 0.8 s quiet
LANGUAGES = ["fr", "de", "es", "it", "pt", "nl", "pl", "sv"]


def _voice(seconds):
    return {"kind": "voice", "duration": seconds, "level_dbfs": -14}


class ScriptedASR(MockTranscriptionEngine):
    """Call n (1-based) behaves as scripted: 'hold' blocks until released,
    'raise' raises an unexpected RuntimeError, 'error' a handled EngineError.
    Each call reports its own language, as automatic detection would."""

    id = "test-scripted-asr"
    name = "scripted-asr"

    def __init__(self, script=None):
        super().__init__()
        self.script = dict(script or {})
        self.order = []                         # audio seconds, per call
        self.languages_asked = []
        self.held = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        with self.lock:
            index = len(self.order) + 1
            self.order.append(round(audio.size / sample_rate, 2))
            self.languages_asked.append(language)
        action = self.script.get(index)
        if action == "hold":
            self.held.set()
            assert self.release.wait(30.0), "the test never released the engine"
        elif action == "raise":
            raise RuntimeError("native decoder crashed (test)")
        elif action == "error":
            raise EngineError("whisper transcription failed (test)")
        result = super().transcribe(audio, sample_rate, language=language,
                                    vocabulary=vocabulary)
        if language is None:
            result.language = LANGUAGES[(index - 1) % len(LANGUAGES)]
            result.language_confidence = 0.9
        return result


class BrittleMT(MockTranslationEngine):
    """Raises an unexpected exception on the call numbers given."""

    def __init__(self, broken=()):
        super().__init__()
        self.broken = set(broken)
        self.count = 0

    def supports_pair(self, source, target):
        return True

    def translate(self, *args, **kwargs):
        self.count += 1
        if self.count in self.broken:
            raise RuntimeError("translation library crashed (test)")
        return super().translate(*args, **kwargs)


def _wait(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _start(config, store, asr, mt=None):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = asr
    app.translation = mt or MockTranslationEngine()
    source = CallbackAudioSource(SR, name="test")
    session = app.start_session(source=source, name="run", target_language="en")
    app.begin_capture()
    return app, source, session


def _push(source, spec, seed):
    audio = build_fixture(spec, sample_rate=SR, seed=seed).audio
    for start in range(0, audio.size, 4800):
        source.push(audio[start:start + 4800])


def _rows(store, session_id):
    """In capture order."""
    return sorted(store.list_transmissions(session_id=session_id),
                  key=lambda t: t.started_at)


def _states(store, session_id):
    return [r.state.value for r in _rows(store, session_id)]


# --- 1. a costly clip does not stop monitoring, reorder or lose later clips ---

def test_later_clips_queue_in_order_and_complete_while_monitoring_stays_armed(
        config, store):
    asr = ScriptedASR({2: "hold"})
    app, source, session = _start(config, store, asr)
    try:
        durations = [1.2, 1.5, 1.8, 2.1, 2.4]
        spec = [{"gap": 0.5}]
        for seconds in durations:
            spec += [_voice(seconds), GAP]
        _push(source, spec, seed=1)

        assert asr.held.wait(15.0), "the costly clip never started"
        # Monitoring keeps capturing while the engine is stuck on clip 2.
        assert _wait(lambda: len(_rows(store, session.id)) == 5), \
            _states(store, session.id)
        rows = _rows(store, session.id)
        ids = [r.id for r in rows]
        assert app.capture is not None and app.capture.alive, "monitoring stopped"
        assert rows[0].state is ProcessingState.COMPLETE
        assert rows[1].state is ProcessingState.TRANSCRIBING

        # Truthful feedback: which clip is running, at what stage, and the
        # order of the rest - read from the processor's own queue.
        snap = app.pipeline.queue_snapshot()
        assert isinstance(snap, QueueSnapshot)
        assert [j.tx_id for j in snap.running] == [ids[1]]
        assert snap.running[0].stage == "transcription"
        assert not snap.running[0].manual and snap.running[0].seconds >= 0
        assert list(snap.waiting) == ids[2:], "not in arrival order"
        assert [snap.position(i) for i in ids] == [None, 0, 1, 2, 3]
        assert app.pipeline.visible_pending == 1 + len(snap.waiting)

        # Sustained: more traffic still arrives and joins the end of the line.
        _push(source, [_voice(2.7), GAP], seed=2)
        assert _wait(lambda: len(_rows(store, session.id)) == 6)
        late = _rows(store, session.id)[-1].id
        assert app.pipeline.queue_snapshot().waiting[-1] == late
        assert asr.order[-1] == pytest.approx(asr.order[1], abs=0.01), \
            "a later clip was started while the costly one was still running"
        assert len(asr.order) == 2, "one model, one clip at a time"

        asr.release.set()
        assert _wait(lambda: all(r.state is ProcessingState.COMPLETE
                                 for r in _rows(store, session.id))), \
            _states(store, session.id)
        assert app.capture is not None and app.capture.alive, \
            "the later clips completed only because monitoring stopped"

        rows = _rows(store, session.id)
        # Processed in capture order (identified by each clip's length).
        assert asr.order == pytest.approx([r.duration for r in rows], abs=0.15)
        assert asr.order == sorted(asr.order)
        # Automatic language identification per transmission, never pinned.
        assert asr.languages_asked == [None] * 6
        assert [r.source_language for r in rows] == LANGUAGES[:6]
        assert all(r.language_confidence == pytest.approx(0.9) for r in rows)
        assert all(r.transcript and r.translation for r in rows)
        empty = app.pipeline.queue_snapshot()
        assert empty.running == () and empty.waiting == ()
    finally:
        asr.release.set()
        app.stop_session()
        app.close()


# --- 2. Stop with a costly clip running and work queued: unchanged ------------

def test_stop_during_a_costly_clip_returns_at_once_and_finishes_everything_accepted(
        config, store):
    asr = ScriptedASR({1: "hold"})
    app, source, session = _start(config, store, asr)
    try:
        _push(source, [{"gap": 0.5}, _voice(1.2), GAP, _voice(1.6), GAP,
                       _voice(2.0), GAP], seed=3)
        assert asr.held.wait(15.0)
        assert _wait(lambda: len(_rows(store, session.id)) == 3)
        pipeline = app.pipeline
        began = time.monotonic()
        app.stop_session()
        assert time.monotonic() - began < 0.5, "Stop waited for the engine"
        # (The exact intake cutoff is proven in tests/test_stop_finishes.py.)
        snap = pipeline.queue_snapshot()
        assert len(snap.running) == 1 and len(snap.waiting) == 2
        assert list(snap.waiting) == [r.id for r in _rows(store, session.id)[1:3]]
        asr.release.set()
        assert _wait(lambda: app.outstanding_work() == 0)
        rows = _rows(store, session.id)
        assert len(rows) == 3
        assert [r.state for r in rows] == [ProcessingState.COMPLETE] * 3
        assert asr.order == pytest.approx([r.duration for r in rows], abs=0.15)
        # As before: the processor that carried the stopped run's work on
        # stays as the standalone processor (Retry goes there) until idle.
        assert app.standalone_pipeline is pipeline and pipeline.pending == 0
    finally:
        asr.release.set()
        app.close()


# --- 3. one broken clip never wedges the others -------------------------------

def test_an_engine_failure_on_one_clip_fails_only_that_clip(config, store):
    asr = ScriptedASR({2: "raise", 3: "error"})
    app, source, session = _start(config, store, asr)
    errors = []
    app.events.subscribe(lambda e: errors.append(e.payload) if e.kind == "error" else None)
    try:
        spec = [{"gap": 0.5}]
        for seconds in (1.2, 1.5, 1.8, 2.1, 2.4):
            spec += [_voice(seconds), GAP]
        _push(source, spec, seed=5)

        def settled():
            rows = _rows(store, session.id)
            return len(rows) == 5 and app.pipeline.pending == 0 and all(
                r.state in (ProcessingState.COMPLETE, ProcessingState.FAILED)
                for r in rows)

        assert _wait(settled), _states(store, session.id)
        rows = _rows(store, session.id)
        assert [r.state.value for r in rows] == [
            "complete", "failed", "failed", "complete", "complete"]
        assert app.capture is not None and app.capture.alive
        # The unexpected exception is recorded, at its stage, not left
        # showing Transcribing (which a restart would also re-queue).
        crashed, handled = rows[1], rows[2]
        assert crashed.error.stage == "transcription"
        assert "RuntimeError" in crashed.error.message
        assert handled.error.stage == "transcription"
        assert not store.pending_transmissions()
        assert {e["transmission"] for e in errors} == {crashed.id, handled.id}
        assert len(asr.order) == 5, "no automatic retry loop"

        # Both are retryable by hand, and a retry joins the end of the line.
        asr.script = {6: "hold"}
        assert app.retry(crashed.id)
        assert asr.held.wait(10.0)
        assert app.retry(handled.id)
        snap = app.pipeline.queue_snapshot()
        assert [j.tx_id for j in snap.running] == [crashed.id]
        assert snap.running[0].manual
        assert list(snap.waiting) == [handled.id]
        asr.release.set()
        assert _wait(lambda: all(r.state is ProcessingState.COMPLETE
                                 for r in _rows(store, session.id)))
    finally:
        asr.release.set()
        app.stop_session()
        app.close()


def test_an_unexpected_translation_failure_keeps_the_transcript_and_moves_on(
        config, store):
    asr = ScriptedASR()
    app, source, session = _start(config, store, asr, mt=BrittleMT(broken={1}))
    try:
        _push(source, [{"gap": 0.5}, _voice(1.2), GAP, _voice(1.6), GAP], seed=6)
        assert _wait(lambda: len(_rows(store, session.id)) == 2
                     and app.pipeline.pending == 0
                     and _rows(store, session.id)[-1].state is ProcessingState.COMPLETE)
        first, second = _rows(store, session.id)
        assert first.state is ProcessingState.FAILED
        assert first.error.stage == "translation" and first.transcript
        assert second.translation
    finally:
        app.stop_session()
        app.close()
