"""Engine protocols, selection honesty, glossary and credentials."""

from __future__ import annotations

import numpy as np
import pytest

from babelfishr.providers import (EngineUnavailable, build_transcription_engine,
                                  build_translation_engine, is_placeholder,
                                  transcription_engine_status,
                                  translation_engine_status)
from babelfishr.providers.base import EngineError
from babelfishr.providers.glossary import (Glossary, protect_terms, restore_terms)
from babelfishr.providers.mock import (MockLanguageDetectionEngine,
                                       MockTranscriptionEngine,
                                       MockTranslationEngine)


def test_mock_transcription_is_deterministic():
    audio = np.sin(np.arange(16_000) * 0.03)
    engine = MockTranscriptionEngine()
    assert engine.transcribe(audio, 16_000).text == engine.transcribe(audio, 16_000).text


def test_mock_transcription_varies_with_audio():
    engine = MockTranscriptionEngine()
    a = engine.transcribe(np.sin(np.arange(16_000) * 0.03), 16_000)
    b = engine.transcribe(np.cos(np.arange(16_000) * 0.11), 16_000)
    assert a.text != b.text


def test_transcription_failure_raises_engine_error():
    with pytest.raises(EngineError):
        MockTranscriptionEngine(fail=True).transcribe(np.ones(1000), 16_000)


def test_translation_passthrough_for_same_language():
    result = MockTranslationEngine().translate("hello", "en", source_language="en")
    assert result.untranslated and result.text == "hello"


def test_translation_applies_glossary():
    result = MockTranslationEngine().translate(
        "puente nuevo", "en", source_language="es",
        glossary={"puente nuevo": "New Bridge"})
    assert "New Bridge" in result.text


def test_language_detection_is_plausible():
    engine = MockLanguageDetectionEngine()
    assert engine.detect("achtung strassensperre voraus")[0] == "de"
    assert engine.detect("roger that we are moving")[0] == "en"


def test_auto_selection_prefers_real_engines_but_admits_a_mock():
    engine = build_transcription_engine()
    assert engine.available()
    if is_placeholder(engine):
        status = [s for s in transcription_engine_status() if s.id == "mock"][0]
        assert status.is_placeholder


def test_explicitly_requesting_a_missing_engine_fails_loudly():
    with pytest.raises(EngineUnavailable):
        build_transcription_engine(requested="faster-whisper")


def test_requesting_none_disables_the_stage():
    with pytest.raises(EngineUnavailable):
        build_translation_engine(requested="none")


def test_unknown_engine_name_is_rejected():
    with pytest.raises(EngineUnavailable):
        build_translation_engine(requested="not-a-real-engine")


def test_engine_status_lists_reasons():
    for status in translation_engine_status():
        if not status.available:
            assert status.reason, f"{status.id} must explain why it is unavailable"


def test_local_engines_declare_no_upload():
    for status in translation_engine_status():
        if status.id in ("argos", "mock"):
            assert "Nothing leaves" in status.privacy


def test_glossary_round_trips(tmp_path):
    glossary = Glossary()
    glossary.add("KD8XYZ", category="callsign", never_translate=True)
    glossary.add("Puente Nuevo", "New Bridge", category="place")
    path = glossary.save(str(tmp_path / "g.json"))
    reloaded = Glossary.load(path)
    assert len(reloaded) == 2
    assert reloaded.protected() == ["KD8XYZ"]
    assert reloaded.mapping()["Puente Nuevo"] == "New Bridge"


def test_protected_terms_survive_translation():
    text = "aqui KD8XYZ en posicion"
    protected, mapping = protect_terms(text, ["KD8XYZ"])
    assert "KD8XYZ" not in protected
    # A translator would mangle the placeholder-free text; restoring brings it back.
    assert restore_terms(protected, mapping) == text


def test_glossary_prompt_hint_marks_do_not_translate():
    glossary = Glossary()
    glossary.add("KD8XYZ", never_translate=True)
    assert "keep exactly" in glossary.prompt_hint()


def test_whisper_engine_reports_how_to_install_it():
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    engine = FasterWhisperEngine()
    if not engine.available():
        assert "pip install" in engine.unavailable_reason()


def test_claude_engine_never_sends_audio():
    from babelfishr.providers.claude import ClaudeTranslationEngine

    engine = ClaudeTranslationEngine()
    assert not engine.privacy.sends_audio
    assert engine.privacy.sends_text


def test_claude_system_prompt_carries_the_glossary():
    from babelfishr.providers.claude import ClaudeTranslationEngine

    blocks = ClaudeTranslationEngine()._build_system(
        {"Puente Nuevo": "New Bridge"}, ["KD8XYZ"])
    text = blocks[0]["text"]
    assert "KD8XYZ" in text and "New Bridge" in text
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_credentials_are_not_read_from_config(config):
    assert not hasattr(config, "api_key")
    assert "api_key" not in config.to_dict()
