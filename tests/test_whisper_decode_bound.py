"""A bound on decode attempts for audio the local detector hears no speech in.

Field measurement (run 30, actual Mac): 20 s of synthetic noise with the
language on automatic took 113 s. Whisper's default temperature fallback
re-decodes a window at up to six temperatures when the output looks
unreliable - which noise output usually does. With the language automatic
and the local voice detector reporting *zero* voiced samples, the engine now
decodes once at temperature 0. Everything else - non-zero or unknown
detector results, a specified language (Retry as speech in...) - keeps the
default decoding. The audio is always transcribed in full; nothing is
trimmed, capped or dropped. This bounds decode attempts; it is not a
wall-clock timeout. No model is loaded here: a fake model records exactly
what the engine asks of it.
"""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass

import numpy as np
import pytest

from babelfishr.providers import whisper_local
from babelfishr.providers.whisper_local import TARGET_RATE, FasterWhisperEngine


@dataclass
class Segment:
    text: str
    start: float
    end: float
    avg_logprob: float = -0.3
    no_speech_prob: float = 0.2
    temperature: float = 0.0


class FakeModel:
    def __init__(self, text="noise words"):
        self.calls = []
        self.text = text

    def transcribe(self, samples, **kwargs):
        self.calls.append((np.array(samples, copy=True), dict(kwargs)))
        info = types.SimpleNamespace(language="fr", language_probability=0.4)
        return iter([Segment(self.text, 0.0, samples.size / TARGET_RATE)]), info


@pytest.fixture
def engine(monkeypatch):
    model = FakeModel()
    eng = FasterWhisperEngine()
    monkeypatch.setattr(eng, "_load", lambda: model)
    eng.fake = model
    return eng


def audio(seconds=2.0, seed=0):
    return np.random.default_rng(seed).normal(0, 0.05, int(seconds * TARGET_RATE)
                                             ).astype(np.float32)


def vad_returns(monkeypatch, value):
    """Stub the supported faster-whisper VAD API (get_speech_timestamps),
    which every version of the engine calls - not the engine's own helper -
    so these tests judge behaviour, not the presence of a function.

    *value*: voiced samples to report (0 = none), None = the detector is not
    available (import fails), or an exception for the detector to raise.
    Returns the list of calls made to it."""
    calls = []
    if value is None:
        monkeypatch.setitem(sys.modules, "faster_whisper.vad", None)   # ImportError
        return calls
    vad = types.ModuleType("faster_whisper.vad")

    class VadOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def get_speech_timestamps(samples, vad_options=None, sampling_rate=16000, **kwargs):
        calls.append(np.asarray(samples).size)
        if isinstance(value, Exception):
            raise value
        return [{"start": 0, "end": int(value)}] if value else []

    vad.VadOptions, vad.get_speech_timestamps = VadOptions, get_speech_timestamps
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    return calls


def test_automatic_language_and_zero_voiced_samples_decode_once(engine, monkeypatch):
    calls = vad_returns(monkeypatch, 0)
    clip = audio()
    result = engine.transcribe(clip, TARGET_RATE)
    samples, kwargs = engine.fake.calls[0]
    assert kwargs.get("temperature") == 0.0, (                # one decode temperature
        f"Whisper was asked for {kwargs.get('temperature', 'the default fallback')}")
    assert samples.dtype == np.float32 and samples.shape == clip.shape
    assert np.array_equal(samples, clip)                    # the full, unchanged audio
    assert len(calls) == 1, "the detector ran more than once"
    assert result.text == "noise words"                     # text kept, not dropped
    assert (result.speech_ratio, result.speech_seconds) == (0.0, 0.0)  # evidence reused


@pytest.mark.parametrize("voiced", [1, 160, 16000])
def test_any_voiced_sample_keeps_default_decoding(engine, monkeypatch, voiced):
    calls = vad_returns(monkeypatch, voiced)
    result = engine.transcribe(audio(), TARGET_RATE)
    _, kwargs = engine.fake.calls[0]
    assert "temperature" not in kwargs                      # Whisper's own fallback
    assert len(calls) == 1
    assert result.speech_seconds == round(voiced / TARGET_RATE, 2)


def test_unknown_detector_result_keeps_default_decoding(engine, monkeypatch):
    vad_returns(monkeypatch, None)
    result = engine.transcribe(audio(), TARGET_RATE)
    assert "temperature" not in engine.fake.calls[0][1]
    assert result.speech_ratio is None and result.speech_seconds is None


def _quiet_then_louder(seconds=12):
    clip = audio(seconds)
    clip[:-3 * TARGET_RATE] *= 0.04
    return clip


def test_late_voice_evidence_preserves_the_full_words_with_one_decode(engine, monkeypatch):
    clip = _quiet_then_louder()
    calls = []

    def voice(samples):
        calls.append(samples.size)
        return TARGET_RATE if len(calls) == 2 else 0

    monkeypatch.setattr(whisper_local, "_voiced_samples", voice)
    result = engine.transcribe(clip, TARGET_RATE)
    assert calls == [clip.size, 4 * TARGET_RATE]
    assert len(engine.fake.calls) == 1
    assert np.array_equal(engine.fake.calls[0][0], clip)
    assert result.speech_seconds == 1.0
    assert result.speech_ratio == round(1 / 12, 3)


@pytest.mark.parametrize("clip", [audio(12), np.zeros(12 * TARGET_RATE, dtype=np.float32)])
def test_steady_input_has_no_extra_voice_scan_or_decode(engine, monkeypatch, clip):
    calls = vad_returns(monkeypatch, 0)
    result = engine.transcribe(clip, TARGET_RATE)
    assert calls == [clip.size]
    assert len(engine.fake.calls) == 1 and result.speech_seconds == 0.0


@pytest.mark.parametrize("original", ["", "Thanks for watching."])
def test_blank_or_stock_output_can_recover_late_words_in_original_time(engine, monkeypatch, original):
    clip = _quiet_then_louder()
    monkeypatch.setattr(whisper_local, "_voiced_samples",
                        lambda x: TARGET_RATE if x.size == 4 * TARGET_RATE else 0)
    languages = []

    def transcribe(samples, **kwargs):
        languages.append(kwargs["language"])
        words = original if samples.size == clip.size else "Radio check"
        info = types.SimpleNamespace(language="es", language_probability=0.85)
        return iter([Segment(words, 0.5, 2.5)]), info

    monkeypatch.setattr(engine.fake, "transcribe", transcribe)
    result = engine.transcribe(clip, TARGET_RATE, language="es")
    assert languages == ["es", "es"]
    assert result.language == "es" and result.text == "Radio check"
    assert [(s.start, s.end) for s in result.segments] == [(8.5, 10.5)]
    assert result.speech_ratio == round(1 / 12, 3)


def test_late_voice_does_not_endorse_words_only_in_the_prefix(engine, monkeypatch):
    clip = _quiet_then_louder()
    monkeypatch.setattr(whisper_local, "_voiced_samples",
                        lambda x: TARGET_RATE if x.size == 4 * TARGET_RATE else 0)
    info = types.SimpleNamespace(language="en", language_probability=0.8)
    monkeypatch.setattr(engine.fake, "transcribe", lambda *a, **kw:
                        (iter([Segment("Guessed words", 0.0, 2.0)]), info))
    assert engine.transcribe(clip, TARGET_RATE).speech_seconds == 0.0


def test_a_specified_language_keeps_full_decoding_even_with_zero_voice(engine,
                                                                       monkeypatch):
    """Retry as speech in a language stays the full-quality path for weak
    speech the detector missed."""
    vad_returns(monkeypatch, 0)
    engine.transcribe(audio(), TARGET_RATE, language="fr")
    _, kwargs = engine.fake.calls[0]
    assert kwargs["language"] == "fr" and "temperature" not in kwargs


def test_a_failing_detector_falls_back_to_default_decoding(engine, monkeypatch):
    vad_returns(monkeypatch, RuntimeError("onnx session failed"))
    result = engine.transcribe(audio(), TARGET_RATE)
    assert "temperature" not in engine.fake.calls[0][1]
    assert result.text == "noise words" and result.speech_seconds is None


def test_resampled_input_reaches_whisper_whole(engine, monkeypatch):
    """48 kHz in: Whisper gets the whole clip at 16 kHz, nothing cut."""
    vad_returns(monkeypatch, 0)
    clip = np.random.default_rng(3).normal(0, 0.05, 3 * 48_000)
    engine.transcribe(clip, 48_000)
    samples, _ = engine.fake.calls[0]
    assert samples.size == 3 * TARGET_RATE
    assert np.array_equal(samples, engine._prepare(clip, 48_000))


def test_the_bench_log_holds_figures_not_words(engine, monkeypatch, caplog):
    vad_returns(monkeypatch, 0)
    engine.fake.text = "SECRET-TRANSCRIPT-WORDS"
    with caplog.at_level(logging.INFO, logger=whisper_local.log.name):
        engine.transcribe(audio(), TARGET_RATE)
    lines = [r.getMessage() for r in caplog.records if r.name == whisper_local.log.name]
    assert any("single pass at temperature 0" in line and "automatic" in line
               for line in lines), lines
    assert not any("SECRET" in line for line in lines)
