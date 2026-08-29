"""Integration tests that need real model weights.

These are SKIPPED unless a prepared model or language pack is actually present.
They exist so that "the test suite passes" can never be mistaken for "real
offline transcription works" - that claim requires these to have run.

Run them after `babelfishr prepare-field`:

    pytest -m real_model tests/test_real_engines.py -v
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from babelfishr.modes import AppPaths
from babelfishr.testing import speech_like

pytestmark = pytest.mark.real_model


def _whisper_engine(model: str = "small"):
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    paths = AppPaths.resolve()
    engine = FasterWhisperEngine(model=model, download_root=str(paths.models),
                                 local_files_only=True)
    if not engine.available():
        pytest.skip(f"no prepared Whisper model at {engine.model_directory()}; "
                    f"run 'babelfishr prepare-field --asr-model {model}'")
    return engine


def test_real_model_loads_without_network():
    """The field guarantee: loading must not need the network."""
    engine = _whisper_engine()
    engine.warm_up()
    assert engine._model is not None


def test_real_transcription_produces_a_result():
    engine = _whisper_engine()
    result = engine.transcribe(speech_like(3.0, 16_000, level_dbfs=-14), 16_000)
    # The fixture is speech-shaped noise, so the *text* is meaningless; what
    # matters is that the engine ran locally and returned a well-formed result.
    assert result.engine == "faster-whisper"
    assert result.engine_version
    assert result.language is not None
    assert isinstance(result.text, str)


def test_real_transcription_of_a_recorded_clip(tmp_path):
    """Transcribe an actual WAV through the same path the pipeline uses."""
    from babelfishr.audio.wavefile import read_wav, write_wav

    engine = _whisper_engine()
    path = tmp_path / "clip.wav"
    write_wav(str(path), speech_like(4.0, 48_000, level_dbfs=-14), 48_000)
    samples, rate = read_wav(str(path))
    result = engine.transcribe(samples, rate)
    assert result.engine_version
    assert result.confidence is None or 0.0 <= result.confidence <= 1.0


def test_real_end_to_end_with_a_local_model(tmp_path):
    """A full offline session: capture, transcribe, store - no network."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.config import Config
    from babelfishr.modes import OperatingMode
    from babelfishr.storage import Store
    from babelfishr.testing import standard_fixture

    _whisper_engine()  # skips if unprepared
    config = Config()
    config.database = str(tmp_path / "db.sqlite3")
    config.recording.directory = str(tmp_path / "rec")
    config.mode = OperatingMode.FIELD_OFFLINE.value
    config.asr.engine = "faster-whisper"
    config.translate.engine = "none"

    wav = standard_fixture(48_000).write(str(tmp_path / "fixture.wav"))
    app = BabelFishRApp(config=config, store=Store(config.database))
    app.select_engines()
    assert app.transcription is not None
    assert app.transcription.id == "faster-whisper", "must not be a mock"

    app.start_session(replay_path=wav, name="real-offline")
    app.run_replay()
    transmissions = app.transmissions()
    assert transmissions
    for tx in transmissions:
        assert pathlib.Path(tx.audio_path).exists()
        if tx.transcript:
            assert tx.transcription_engine == "faster-whisper"
    app.stop_session()
    app.close()


def _argos_engine(target: str = "en"):
    from babelfishr.providers.argos import ArgosTranslateEngine

    engine = ArgosTranslateEngine(target_language=target)
    if not engine.available():
        pytest.skip("no Argos language pack installed; run "
                    "'babelfishr languages install es en'")
    return engine


def test_real_translation_produces_different_text():
    engine = _argos_engine()
    source = engine.installed_pairs()[0][0]
    if source == "en":
        pytest.skip("need a non-English source pack")
    result = engine.smoke_test(source, "en")
    assert result.text.strip()
    assert not result.untranslated
    assert result.engine == "argos"


def test_real_translation_preserves_protected_terms():
    """A callsign must survive translation verbatim."""
    engine = _argos_engine()
    source = engine.installed_pairs()[0][0]
    if source == "en":
        pytest.skip("need a non-English source pack")
    result = engine.translate("KD8XYZ", "en", source_language=source,
                              do_not_translate=["KD8XYZ"])
    assert "KD8XYZ" in result.text


def test_field_check_passes_with_real_engines():
    from babelfishr.config import Config
    from babelfishr.modes import OperatingMode
    from babelfishr.readiness import field_check

    _whisper_engine()
    _argos_engine()
    report = field_check(Config(), run_smoke_tests=True,
                         mode=OperatingMode.FIELD_OFFLINE)
    assert report.can_transcribe, "real transcription smoke test failed"
    assert report.can_translate, "real translation smoke test failed"
