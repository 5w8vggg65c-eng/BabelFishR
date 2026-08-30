"""Priority-0 invariant: capture first, classify second.

Every event crossing the activity threshold must be an immutable recording on
disk and a row in the database *before* anything classifies, transcribes,
translates or analyses it. A transmission cannot be received twice, so a
misclassification must never be the reason one is gone.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.wavefile import read_wav
from babelfishr.detect import ContentClass as DetectorClass
from babelfishr.detect import DetectorSettings, detect_in_array
from babelfishr.models import ContentClass, ProcessingState
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.testing import build_fixture

SR = 48_000

#: One fixture containing every content type the detector can classify.
MIXED_SPEC = [
    {"gap": 1.5},
    {"kind": "voice", "duration": 2.5, "level_dbfs": -14, "tail": True},
    {"gap": 1.5},
    {"kind": "static", "duration": 2.0, "level_dbfs": -20},
    {"gap": 1.5},
    {"kind": "tone", "duration": 1.5, "level_dbfs": -14},
    {"gap": 1.5},
    {"kind": "digital", "duration": 2.0, "level_dbfs": -14},
    {"gap": 1.5},
    {"kind": "voice", "duration": 2.0, "level_dbfs": -18},
    {"gap": 1.5},
]


@pytest.fixture
def mixed_wav(tmp_path):
    return build_fixture(MIXED_SPEC, sample_rate=SR).write(str(tmp_path / "mixed.wav"))


def test_detector_retains_every_content_type():
    fixture = build_fixture(MIXED_SPEC, sample_rate=SR)
    detected = detect_in_array(fixture.audio, SR)
    assert len(detected) == 5, "one or more events were discarded by classification"
    classes = {d.content_class for d in detected}
    assert DetectorClass.SPEECH in classes
    assert DetectorClass.NOISE in classes
    assert DetectorClass.TONE in classes
    assert DetectorClass.DIGITAL_SUSPECTED in classes


def test_every_event_is_recorded_and_logged(app, mixed_wav):
    app.select_engines()
    app.start_session(replay_path=mixed_wav, name="invariant")
    app.run_replay()

    transmissions = app.transmissions()
    assert len(transmissions) == 5
    for tx in transmissions:
        assert tx.audio_path, f"{tx.content_class} was not recorded"
        assert pathlib.Path(tx.audio_path).exists()
        samples, _ = read_wav(tx.audio_path)
        assert samples.size > 0


@pytest.mark.parametrize("content", [
    ContentClass.NOISE, ContentClass.TONE, ContentClass.DIGITAL_SUSPECTED])
def test_non_speech_classes_are_persisted(app, mixed_wav, content):
    app.select_engines()
    app.start_session(replay_path=mixed_wav, name="classes")
    app.run_replay()
    matching = [t for t in app.transmissions() if t.content_class is content]
    assert matching, f"no {content.value} transmission was retained"
    for tx in matching:
        assert pathlib.Path(tx.audio_path).exists()
        assert app.store.get_transmission(tx.id) is not None


def test_classification_does_not_gate_asr_but_still_never_gates_persistence(
        config, store, mixed_wav):
    """Everything reaches the disk, and almost everything reaches the engine.

    This test used to assert that only SPEECH was transcribed. That was the
    defect a real Mac exposed: ordinary voice through the MacBook microphone
    was classified DIGITAL_SUSPECTED, so it was never transcribed and there
    was nothing for Argos to translate. Classification is advisory now. Only a
    steady tone - which cannot contain speech - is routed away by default.
    """
    app = BabelFishRApp(config=config, store=store)
    engine = MockTranscriptionEngine()
    app.transcription = engine
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=mixed_wav, name="gating")
    app.run_replay()

    transmissions = app.transmissions()
    assert len(transmissions) == 5
    tones = [t for t in transmissions if t.content_class is ContentClass.TONE]
    processed = [t for t in transmissions
                 if t.content_class is not ContentClass.TONE]
    assert processed, "the fixture must contain something worth transcribing"

    assert engine.calls == len(processed), (
        "a classification other than TONE stopped speech recognition")
    for tx in processed:
        assert tx.state is not ProcessingState.SKIPPED
        assert pathlib.Path(tx.audio_path).exists()
    for tx in tones:
        assert tx.state is ProcessingState.SKIPPED
        assert not tx.auto_processed
        assert tx.skip_reason, "a skipped transmission must explain itself"
        assert "recording is kept" in tx.skip_reason
        assert pathlib.Path(tx.audio_path).exists()
    app.stop_session()


def test_transcribe_anyway_overrides_the_classifier(config, store, mixed_wav):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=mixed_wav, name="override")
    app.run_replay()

    skipped = [t for t in app.transmissions()
               if t.state is ProcessingState.SKIPPED]
    assert skipped
    target = skipped[0]
    assert target.can_transcribe_anyway

    assert app.transcribe_anyway(target.id)
    app.wait_for_processing(timeout=30)
    reloaded = app.store.get_transmission(target.id)
    assert reloaded.transcript, "forced transcription produced nothing"
    assert reloaded.state is ProcessingState.COMPLETE
    app.stop_session()


def test_original_audio_is_never_modified(app, mixed_wav):
    """Processing must not rewrite the immutable capture."""
    app.select_engines()
    app.start_session(replay_path=mixed_wav, name="immutable")
    app.run_replay()

    digests = {}
    for tx in app.transmissions():
        digests[tx.id] = hashlib.sha256(
            pathlib.Path(tx.audio_path).read_bytes()).hexdigest()

    # Force more processing over the same recordings.
    for tx_id in list(digests):
        app.transcribe_anyway(tx_id)
    app.wait_for_processing(timeout=60)

    for tx in app.transmissions():
        after = hashlib.sha256(pathlib.Path(tx.audio_path).read_bytes()).hexdigest()
        assert after == digests[tx.id], "original audio was rewritten"


def test_transcription_failure_leaves_every_recording_playable(config, store,
                                                               mixed_wav):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine(fail=True, fail_message="no model")
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=mixed_wav, name="failure")
    app.run_replay()

    for tx in app.transmissions():
        assert pathlib.Path(tx.audio_path).exists()
        samples, rate = read_wav(tx.audio_path)
        assert samples.size > 0 and rate > 0
    app.stop_session()


def test_recording_survives_when_no_engines_exist_at_all(config, store, mixed_wav):
    """Record Only: no ASR, no translation, still a complete set of clips."""
    app = BabelFishRApp(config=config, store=store)
    app.transcription = None
    app.translation = None
    app.start_session(replay_path=mixed_wav, name="record-only")
    app.run_replay()

    transmissions = app.transmissions()
    assert len(transmissions) == 5
    for tx in transmissions:
        assert pathlib.Path(tx.audio_path).exists()
    app.stop_session()


def test_settings_separate_recording_from_processing():
    """The two thresholds must be genuinely independent knobs."""
    settings = DetectorSettings()
    processing_knobs = {"auto_process_speech", "auto_process_tone",
                        "auto_process_unknown"}
    recording_knobs = {"min_duration", "open_margin_db", "threshold_dbfs"}
    for name in processing_knobs | recording_knobs:
        assert hasattr(settings, name)
    # No setting may exist whose purpose is to discard a classified event.
    assert not hasattr(settings, "reject_noise")
    # And none may exist that can cancel the transcription of a suspected
    # digital burst or of static: on a real Mac the first classification
    # landed on speech, and the second is where a voice under static goes.
    assert not hasattr(settings, "auto_process_digital")
    assert not hasattr(settings, "auto_process_noise")


def test_digital_shaped_audio_reaches_the_digital_queue(app, mixed_wav):
    app.select_engines()
    app.start_session(replay_path=mixed_wav, name="digital-queue")
    app.run_replay()
    digital = [t for t in app.transmissions()
               if t.content_class is ContentClass.DIGITAL_SUSPECTED]
    assert digital
    assert all(t.worth_digital_analysis for t in digital)
    assert all(pathlib.Path(t.audio_path).exists() for t in digital)
