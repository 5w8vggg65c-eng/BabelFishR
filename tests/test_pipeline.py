"""Pipeline behaviour: threading isolation, state machine, ordering, recovery."""

from __future__ import annotations

import pathlib
import time

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState
from babelfishr.pipeline import EventBus, PipelineState, Recorder
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine


def test_event_bus_delivers_to_subscribers_and_queue():
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    bus.publish("state", "listening")
    assert [e.payload for e in seen] == ["listening"]
    assert [e.payload for e in bus.drain()] == ["listening"]


def test_event_bus_survives_a_broken_subscriber():
    bus = EventBus()
    bus.subscribe(lambda event: 1 / 0)
    good = []
    bus.subscribe(good.append)
    bus.publish("state", "x")  # must not raise
    assert good


def test_recorder_path_layout(tmp_path):
    from babelfishr.models import Session, Transmission

    recorder = Recorder(str(tmp_path), layout="{date}/{session}")
    session = Session(id="sess_1", profile_label="GMRS 16")
    tx = Transmission(id="tx_1", session_id="sess_1")
    path = recorder.path_for(tx, session)
    assert "sess_1" in str(path)
    assert path.suffix == ".wav"
    assert tx.started_at.strftime("%Y-%m-%d") in str(path)


def test_recorder_falls_back_on_a_bad_layout(tmp_path):
    from babelfishr.models import Session, Transmission

    recorder = Recorder(str(tmp_path), layout="{nonsense}")
    path = recorder.path_for(Transmission(), Session(id="s"))
    assert path.name.endswith(".wav")


def test_states_progress_through_the_expected_sequence(app, fixture_wav):
    states = []
    app.events.subscribe(
        lambda event: states.append(event.payload) if event.kind == "state" else None)
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="states")
    app.run_replay()
    assert PipelineState.LISTENING in states
    assert PipelineState.RECEIVING in states
    assert PipelineState.TRANSCRIBING in states


def test_transmission_is_stored_before_processing(app, fixture_wav):
    """Audio must be durable before any engine is allowed to fail."""
    captured = []
    app.events.subscribe(
        lambda event: captured.append(event.payload)
        if event.kind == "transmission" else None)
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="durable")
    app.run_replay()
    assert captured
    for tx in captured:
        assert tx.audio_path and pathlib.Path(tx.audio_path).exists()


def test_same_language_skips_translation(config, store, fixture_wav):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=fixture_wav, target_language="en", name="same")
    app.run_replay()
    english = [t for t in app.transmissions() if t.source_language == "en"]
    assert english
    for tx in english:
        assert tx.translation == ""
        assert tx.state is ProcessingState.COMPLETE
    app.stop_session()


def test_specified_source_language_is_honoured(config, store, fixture_wav):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=fixture_wav, source_language="pt",
                      source_language_mode="specified", name="forced")
    app.run_replay()
    assert all(t.source_language == "pt" for t in app.transmissions() if t.transcript)
    app.stop_session()


def test_an_operator_who_opts_out_of_noise_costs_no_asr_call(config, store,
                                                             tmp_path):
    """Static reaches ASR by default now; opting out must still work.

    The old version of this test asserted the opposite default. It was
    changed deliberately: a real Mac showed the classifier mislabelling
    speech, and a transmission that is silently never transcribed is a worse
    outcome than an ASR call spent on static.
    """
    from babelfishr.testing import build_fixture

    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "static", "duration": 3.0, "level_dbfs": -20},
         {"gap": 1.0}], sample_rate=48_000)
    path = fixture.write(str(tmp_path / "static.wav"))

    config.detector.auto_process_noise = False
    app = BabelFishRApp(config=config, store=store)
    engine = MockTranscriptionEngine()
    app.transcription = engine
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=path, name="noise")
    app.run_replay()

    noise = [t for t in app.transmissions()
             if t.state is ProcessingState.SKIPPED]
    assert noise, "the static burst should have been captured but skipped"
    assert engine.calls == 0
    app.stop_session()


def test_engine_metadata_is_recorded(app, fixture_wav):
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="provenance")
    app.run_replay()
    for tx in app.transmissions():
        if tx.transcript:
            assert tx.transcription_engine
            assert tx.transcription_engine_version


def test_capture_does_not_block_on_slow_transcription(config, store, fixture_wav):
    """A slow engine must delay transcripts, never the capture thread."""

    class SlowEngine(MockTranscriptionEngine):
        def transcribe(self, audio, sample_rate, **kwargs):
            time.sleep(0.25)
            return super().transcribe(audio, sample_rate, **kwargs)

    app = BabelFishRApp(config=config, store=store)
    app.transcription = SlowEngine()
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=fixture_wav, name="slow")

    started = time.monotonic()
    captured = app.capture.run_to_completion()
    capture_elapsed = time.monotonic() - started

    assert captured == 5
    # Five clips at 0.25 s each is 1.25 s of engine time; capture must not have
    # waited for it.
    assert capture_elapsed < 1.0
    app.wait_for_processing(timeout=60)
    assert all(t.transcript for t in app.transmissions())
    app.stop_session()


def test_tags_bookmarks_and_notes_persist(app, fixture_wav):
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="annotate")
    app.run_replay()
    tx = app.transmissions()[0]

    app.set_tags(tx.id, ["urgent", "net-control"])
    app.bookmark(tx.id, True)
    app.correct(tx.id, notes="checked against the log")

    reloaded = app.store.get_transmission(tx.id)
    assert reloaded.tags == ["urgent", "net-control"]
    assert reloaded.bookmarked
    assert reloaded.notes == "checked against the log"
    assert app.search(tag="urgent")


def test_missing_audio_file_is_a_recoverable_error(config, store, fixture_wav):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=fixture_wav, name="missing")
    app.capture.run_to_completion()
    app.wait_for_processing(timeout=30)

    tx = app.transmissions()[0]
    pathlib.Path(tx.audio_path).unlink()
    app.retry(tx.id)
    app.wait_for_processing(timeout=30)

    reloaded = app.store.get_transmission(tx.id)
    assert reloaded.state is ProcessingState.FAILED
    assert "missing" in reloaded.error.message
    app.stop_session()
