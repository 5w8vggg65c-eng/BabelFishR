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
