"""The encoder pass that faster-whisper's automatic language detection
repeats, and what may and may not be shared.

faster-whisper 1.2.1 (read from its source; not needed here):

* ``FeatureExtractor.__call__`` pads the audio with 160 zero samples and
  returns ``N // 160 + 1`` mel frames for ``N`` samples
  (feature_extractor.py:198-230).
* ``WhisperModel.transcribe(language=None)`` calls ``detect_language`` on
  ``features[..., seek:]`` (transcribe.py:921-945), which encodes
  ``pad_or_trim(features[..., :3000])`` (transcribe.py:1817-1823).
* ``encoder_output`` stays ``None`` (transcribe.py:918), so
  ``generate_segments`` encodes the first window again: ``features[:, 0 :
  min(3000, content_frames)]`` padded, ``content_frames = features.shape[-1]
  - 1`` (transcribe.py:1111, 1166-1190).

So for a clip of 30 s or more both encodes see the same 3000 frames, and for
a shorter clip - every live clip - they differ in exactly one column, the
last 10 ms frame. ``FakeWhisper`` below reproduces exactly that call
pattern (through ``self.encode``, as the real class does), so these tests
pin down: the exact-reuse cache only ever reuses byte-identical input (and
therefore never changes a result); the stock path is unchanged for short
clips; and the opt-in shared encode hands faster-whisper the very window it
decodes while recording the detected language and probability per clip.
Whether that opt-in makes the same language decisions as stock detection on
real radio is for a native bench (tools/bench_language_encode.py).
"""

from __future__ import annotations

import logging
import types

import numpy as np
import pytest

from babelfishr.providers.whisper_local import (TARGET_RATE, FasterWhisperEngine,
                                                _EncodeReuse)

FRAMES = 3000


def _pad_or_trim(array, length=FRAMES):
    """faster_whisper.audio.pad_or_trim, for 2-D mel features."""
    if array.shape[-1] > length:
        array = array[:, :length]
    if array.shape[-1] < length:
        array = np.pad(array, [(0, 0), (0, length - array.shape[-1])])
    return array


class FakeExtractor:
    nb_max_frames = FRAMES

    def __call__(self, waveform, padding=160, chunk_length=None):
        waveform = np.pad(np.asarray(waveform, dtype=np.float32), (0, padding))
        frames = waveform.size // 160               # N // 160 + 1
        hop = waveform[:frames * 160].reshape(frames, 160)
        energy = np.log10(np.mean(hop ** 2, axis=1) + 1e-10)
        mel = np.vstack([energy + k * 0.01 for k in range(8)]).astype(np.float32)
        return (mel + 4.0) / 4.0


class FakeNative:
    is_multilingual = True

    def __init__(self, ranking=(("<|de|>", 0.62), ("<|en|>", 0.30))):
        self.ranking = list(ranking)
        self.detected_on = []

    def detect_language(self, encoder_output):
        self.detected_on.append(encoder_output)
        return [self.ranking]


class FakeWhisper:
    """The faster-whisper 1.2.1 encode/detect/decode pattern, no weights."""

    def __init__(self, native=None):
        self.feature_extractor = FakeExtractor()
        self.model = native or FakeNative()
        self.encoder_inputs = []
        self.decoded_with = []
        self.calls = []

    def encode(self, features):
        self.encoder_inputs.append(np.array(features, copy=True))
        return {"pass": len(self.encoder_inputs), "input": features.copy()}

    def transcribe(self, audio, language=None, **kwargs):
        self.calls.append(dict(kwargs, language=language))
        features = self.feature_extractor(audio)
        probability = 1
        if language is None:
            if not self.model.is_multilingual:
                language = "en"
            else:
                # detect_language(features=...), one segment
                out = self.encode(_pad_or_trim(features[..., :FRAMES]))
                token, probability = self.model.detect_language(out)[0][0]
                language = token[2:-2]
        content_frames = features.shape[-1] - 1
        segments = []
        seek = 0
        while seek < content_frames:              # generate_segments
            size = min(FRAMES, content_frames - seek)
            out = self.encode(_pad_or_trim(features[:, seek:seek + size]))
            self.decoded_with.append(out)
            segments.append(types.SimpleNamespace(
                text=f"words in {language}", start=seek / 100.0,
                end=(seek + size) / 100.0, avg_logprob=-0.2, no_speech_prob=0.1,
                temperature=0.0))
            seek += size
        info = types.SimpleNamespace(language=language,
                                     language_probability=probability)
        return iter(segments), info


def _engine(monkeypatch, model, shared=False, voiced=16000):
    eng = FasterWhisperEngine(shared_language_encode=shared)
    monkeypatch.setattr(eng, "_load", lambda: model)
    monkeypatch.setattr("babelfishr.providers.whisper_local._voiced_samples",
                        lambda samples, sample_rate=TARGET_RATE: voiced)
    return eng


def _audio(seconds, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * TARGET_RATE)) * 0.1).astype(np.float32)


def _stock(audio, language=None, native=None):
    """What faster-whisper itself does, with no wrapper involved."""
    model = FakeWhisper(native)
    segments, info = model.transcribe(audio, language=language)
    list(segments)
    return model, info


# --- the faithful model really shows the 1.2.1 behaviour --------------------

def test_stock_short_clip_encodes_twice_with_inputs_one_frame_apart():
    audio = _audio(5.0)
    model, _ = _stock(audio)
    detect, decode = model.encoder_inputs
    differing = np.where(np.any(detect != decode, axis=0))[0]
    frames = audio.size // 160 + 1
    assert differing.tolist() == [frames - 1], "not the final frame only"


def test_stock_long_clip_encodes_the_same_first_window_twice():
    model, _ = _stock(_audio(31.0))
    assert np.array_equal(model.encoder_inputs[0], model.encoder_inputs[1])


# --- default: exact reuse only; the short-clip path is unchanged -------------

def test_default_short_clip_is_exactly_the_stock_call(monkeypatch):
    audio = _audio(5.0)
    stock, stock_info = _stock(audio)
    model = FakeWhisper()
    result = _engine(monkeypatch, model).transcribe(audio, TARGET_RATE)
    assert model.calls[0]["language"] is None, "language must stay automatic"
    assert len(model.encoder_inputs) == 2, "inputs differ: nothing may be reused"
    for ours, theirs in zip(model.encoder_inputs, stock.encoder_inputs):
        assert np.array_equal(ours, theirs)
    assert result.language == stock_info.language == "de"
    assert result.language_confidence == pytest.approx(0.62)


def test_default_long_clip_reuses_the_identical_window_and_changes_nothing(monkeypatch):
    audio = _audio(31.0)
    stock, stock_info = _stock(audio)
    model = FakeWhisper()
    result = _engine(monkeypatch, model).transcribe(audio, TARGET_RATE)
    assert len(stock.encoder_inputs) == 3 and len(model.encoder_inputs) == 2
    # The decode of window 0 used the detection's output, whose input was
    # byte-identical to the input stock would have encoded for it.
    assert np.array_equal(model.decoded_with[0]["input"], stock.decoded_with[0]["input"])
    assert np.array_equal(model.decoded_with[1]["input"], stock.decoded_with[1]["input"])
    assert result.language == stock_info.language
    assert result.text == "words in de words in de"


def test_a_specified_language_is_untouched(monkeypatch):
    model = FakeWhisper()
    result = _engine(monkeypatch, model, shared=True).transcribe(
        _audio(4.0), TARGET_RATE, language="fr")
    assert model.calls[0]["language"] == "fr"
    assert model.model.detected_on == [] and len(model.encoder_inputs) == 1
    assert result.language == "fr"


# --- opt-in: one encode per short clip, the decode input unchanged -----------

@pytest.mark.parametrize("seconds", [0.6, 1.5, 5.0, 14.9, 18.0])
def test_shared_encode_decodes_the_stock_window_with_one_encoder_pass(
        monkeypatch, seconds):
    audio = _audio(seconds, seed=int(seconds * 10))
    stock, _ = _stock(audio, language="de")      # stock decode, same language
    model = FakeWhisper()
    result = _engine(monkeypatch, model, shared=True).transcribe(audio, TARGET_RATE)
    assert len(model.encoder_inputs) == 1, "the encoder ran more than once"
    assert np.array_equal(model.decoded_with[0]["input"], stock.decoded_with[0]["input"])
    assert model.model.detected_on[0] is model.decoded_with[0], \
        "language was not identified from the window that was decoded"
    # The language reached faster-whisper as found, never a default...
    assert model.calls[0]["language"] == "de"
    # ...and is recorded per transmission with its real probability, not
    # the 1.0 faster-whisper reports for a language it was given.
    assert result.language == "de"
    assert result.language_confidence == pytest.approx(0.62)


def test_shared_encode_follows_whatever_language_is_heard(monkeypatch):
    for token, language in (("<|ja|>", "ja"), ("<|en|>", "en"), ("<|uk|>", "uk")):
        model = FakeWhisper(FakeNative(ranking=[(token, 0.55)]))
        result = _engine(monkeypatch, model, shared=True).transcribe(
            _audio(3.0), TARGET_RATE)
        assert model.calls[0]["language"] == language == result.language


def test_shared_encode_keeps_the_zero_voice_single_pass_and_never_skips(monkeypatch):
    model = FakeWhisper()
    result = _engine(monkeypatch, model, shared=True, voiced=0).transcribe(
        _audio(6.0), TARGET_RATE)
    assert model.calls[0]["temperature"] == 0.0
    assert len(model.decoded_with) == 1 and result.text, "zero-VAD audio was skipped"


def test_shared_encode_leaves_an_english_only_model_to_faster_whisper(monkeypatch):
    class English(FakeNative):
        is_multilingual = False

    model = FakeWhisper(English())
    _engine(monkeypatch, model, shared=True).transcribe(_audio(2.0), TARGET_RATE)
    assert model.calls[0]["language"] is None and model.model.detected_on == []


def test_shared_encode_failure_leaves_language_to_faster_whisper(monkeypatch):
    class Broken(FakeNative):
        def detect_language(self, encoder_output):
            raise RuntimeError("no detection (test)")

    model = FakeWhisper(Broken())
    eng = _engine(monkeypatch, model, shared=True)
    with pytest.raises(Exception):
        # Stock detection inside transcribe() uses the same broken native
        # call, so the engine reports the failure rather than guessing.
        eng.transcribe(_audio(2.0), TARGET_RATE)
    assert model.calls and model.calls[0]["language"] is None


def test_the_log_counts_encoder_passes_but_holds_no_words(monkeypatch, caplog):
    model = FakeWhisper()
    with caplog.at_level(logging.INFO, logger="babelfishr.providers.whisper_local"):
        _engine(monkeypatch, model, shared=True).transcribe(_audio(3.0), TARGET_RATE)
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("whisper:"))
    assert "encoder passes 1 (1 reused)" in line
    assert "on the decode window" in line and "words in" not in line


# --- the cache itself -----------------------------------------------------------

def test_the_cache_reuses_only_byte_identical_input_within_an_armed_call():
    calls = []
    reuse = _EncodeReuse(lambda f: (calls.append(f.copy()), object())[1])
    a = np.ones((4, 10), dtype=np.float32)
    first = reuse(a)
    assert reuse(a) is not first and len(calls) == 2, "unarmed: never reused"
    reuse.arm()
    out = reuse(a)
    assert reuse(a.copy()) is out
    b = a.copy()
    b[3, 9] = np.nextafter(np.float32(1), np.float32(2))
    assert reuse(b) is not out, "a one-ulp difference was reused"
    assert reuse(a.astype(np.float64)) is not None and len(calls) == 5
    zero = np.zeros_like(a)
    reuse(zero)
    negzero = -np.zeros_like(a)
    assert reuse(negzero) is not None and len(calls) == 7, "-0.0 is not 0.0 bitwise"
    assert reuse.disarm() == (5, 1)
    reuse(a)
    assert len(calls) == 8, "disarmed: never reused"


def test_the_cache_is_per_thread():
    import threading

    calls = []
    reuse = _EncodeReuse(lambda f: (calls.append(1), object())[1])
    a = np.ones((2, 3), dtype=np.float32)
    reuse.arm()
    reuse(a)
    other = threading.Thread(target=lambda: reuse(a))
    other.start()
    other.join()
    assert len(calls) == 2, "another thread's call was served from this one's"


# --- the switch: baseline by default, comparable, per recording --------------

def test_baseline_is_the_default_and_the_switch_reaches_the_engine():
    from babelfishr.config import Config
    from babelfishr.providers import _transcription_factories

    assert Config().asr.shared_language_encode is False
    assert FasterWhisperEngine().shared_language_encode is False
    assert _transcription_factories(Config())["faster-whisper"]().shared_language_encode is False
    cfg = Config.from_dict({"asr": {"shared_language_encode": True}})
    assert cfg.asr.shared_language_encode is True
    assert Config.from_dict(cfg.to_dict()).asr.shared_language_encode is True
    assert _transcription_factories(cfg)["faster-whisper"]().shared_language_encode is True


def test_each_recording_is_detected_on_its_own(monkeypatch):
    """Nothing is carried between recordings: the same clip twice encodes
    twice, and a different language in the next clip is found."""
    native = FakeNative(ranking=[("<|de|>", 0.7)])
    model = FakeWhisper(native)
    eng = _engine(monkeypatch, model, shared=True)
    audio = _audio(4.0)
    eng.transcribe(audio, TARGET_RATE)
    native.ranking = [("<|fr|>", 0.8)]
    second = eng.transcribe(audio, TARGET_RATE)
    assert len(model.encoder_inputs) == 2, "an earlier recording's encoder output was reused"
    assert second.language == "fr" and model.calls[1]["language"] == "fr"


def test_a_long_recording_reuses_only_its_first_window(monkeypatch):
    """Over 30 s, later windows are encoded afresh (different bytes)."""
    audio = _audio(65.0)
    stock, _ = _stock(audio, language="de")
    model = FakeWhisper()
    _engine(monkeypatch, model, shared=True).transcribe(audio, TARGET_RATE)
    assert len(model.encoder_inputs) == len(stock.encoder_inputs) == 3
    for ours, theirs in zip(model.decoded_with, stock.decoded_with):
        assert np.array_equal(ours["input"], theirs["input"])


def test_the_native_bench_tool_compares_both_paths_on_one_model(tmp_path, monkeypatch,
                                                                capsys):
    """tools/bench_language_encode.py, over a stand-in model: both paths run,
    figures only are printed, and agreement decides the exit status."""
    import importlib.util
    import pathlib
    import sys

    from babelfishr.audio.wavefile import write_wav
    from test_model_layout import _write_complete_model

    built = []
    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = lambda *a, **k: built.append(FakeWhisper()) or built[-1]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    model_dir = tmp_path / "large-v3"
    _write_complete_model(model_dir)
    wavs = []
    for i, seconds in enumerate((2.0, 7.5)):
        path = tmp_path / f"clip{i}.wav"
        write_wav(str(path), _audio(seconds, seed=i), TARGET_RATE)
        wavs.append(str(path))
    tool = pathlib.Path(__file__).resolve().parents[1] / "tools" / "bench_language_encode.py"
    spec = importlib.util.spec_from_file_location("bench_language_encode", tool)
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    assert bench.main(["--model-dir", str(model_dir), *wavs]) == 0
    out = capsys.readouterr().out
    assert len(built) == 1, "the bench must compare on one loaded model"
    assert "same language 2/2, same transcript 2/2, same timestamps 2/2" in out
    assert "words in" not in out
    # stock: two encoder passes per short clip; shared: one.
    assert len(built[0].encoder_inputs) == 6
