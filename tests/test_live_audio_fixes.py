"""Field report: a 52 s live-microphone test (English, some dialogue, mostly
room noise) produced one 50 s recording, transcribed only after Stop, and
the transcript was treated as confident speech.

Three repairs, each covered here with synthetic audio and mock engines (no
private recording is used):

1. Continuous audio is handed to speech recognition in parts while
   monitoring goes on (DetectorSettings.segment_seconds), with the parts
   joining sample for sample: nothing dropped, trimmed or repeated.
2. A transcript that local evidence says is probably not speech is kept,
   not translated, and marked for review (assess_speech).
3. A missing translation pack is reported differently from an uncertain
   automatic language guess, and English to English is not translated.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import AudioBlock
from babelfishr.audio.wavefile import read_wav, write_wav
from babelfishr.detect import DetectorSettings, RadioActivityDetector
from babelfishr.models import ProcessingState, SourceLanguageMode
from babelfishr.pipeline import assess_speech
from babelfishr.providers.base import TranscriptionResult, TranscriptSegment
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.testing import silence, speech_like

SR = 48_000
BLOCK = 2048
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def loud_speech(seconds: float, seed: int = 1) -> np.ndarray:
    """Syllables well above a -20 dBFS gate, as a close microphone gives."""
    x = speech_like(seconds, SR, level_dbfs=-14.0, seed=seed)
    return x / np.max(np.abs(x)) * 0.7


def room(seconds: float, level_dbfs: float = -23.0, seed: int = 2) -> np.ndarray:
    """Room noise loud enough that the gate never stays below close threshold."""
    return np.random.default_rng(seed).normal(0.0, 10 ** (level_dbfs / 20.0),
                                              int(seconds * SR))


def gate(**overrides) -> DetectorSettings:
    """A fixed -20 dBFS gate (close at -24), as in the field test."""
    values = dict(mode="fixed", threshold_dbfs=-20.0, hang_time=0.8)
    values.update(overrides)
    return DetectorSettings(**values)


def stream(audio: np.ndarray, settings: DetectorSettings, stop: bool = True):
    """Feed audio block by block. Returns [(emitted_at_seconds or 'stop', part)]."""
    det = RadioActivityDetector(SR, settings)
    out = []
    for i in range(0, audio.size, BLOCK):
        chunk = audio[i:i + BLOCK]
        for found in det.push(AudioBlock(samples=chunk, sample_rate=SR,
                                         timestamp=T0 + dt.timedelta(seconds=i / SR),
                                         offset=i / SR)):
            out.append(((i + chunk.size) / SR, found))
    if stop:
        out.extend(("stop", found) for found in det.flush())
    return out


def assert_contiguous(parts, source: np.ndarray):
    """Consecutive parts join exactly, and are the source audio itself."""
    for before, after in zip(parts, parts[1:]):
        assert before.continued and after.continues_previous
        end = before.start_offset + before.audio.size / SR
        assert after.start_offset == pytest.approx(end, abs=1e-9), "gap or overlap at a cut"
        assert after.started_at - before.started_at == pytest.approx(
            dt.timedelta(seconds=before.audio.size / SR), abs=dt.timedelta(microseconds=1))
    joined = np.concatenate([p.audio for p in parts])
    first = int(round(parts[0].start_offset * SR))
    assert np.array_equal(joined, source[first:first + joined.size]), (
        "the parts are not the recorded audio, sample for sample")
    return first, joined.size


# --- 1. continuous audio --------------------------------------------------

def test_continuous_speech_in_room_noise_is_handed_on_while_monitoring():
    """The field case: 50 s of speech over room noise. Before the repair the
    only emission was at Stop; now parts arrive while audio continues."""
    audio = loud_speech(50.0) + room(50.0)
    events = stream(audio, gate())
    live = [(at, p) for at, p in events if at != "stop"]
    assert len(live) >= 2, f"nothing was handed on before Stop: {events}"
    first_at = live[0][0]
    assert first_at <= 15.0 + 0.5, f"first part only reached processing at {first_at} s"
    parts = [p for _, p in events]
    for at, part in live:
        # Cut on a loud frame at or after 15 s: at most a hang time later.
        assert part.duration <= 15.0 + 0.8 + 0.05
        # Bounded latency: a part is handed on as soon as it is cut, and the
        # cut lies at most segment_search_seconds back.
        assert at - (part.start_offset + part.duration) <= 3.0 + 0.1
    first, length = assert_contiguous(parts, audio)
    # Nothing lost: from the first sample handed on to the end of the audio,
    # at most the squelch-tail trim at the very end is missing.
    assert first + length >= audio.size - int(0.5 * SR)
    assert not parts[0].continues_previous
    assert not parts[-1].continued and parts[-1].continues_previous


def test_cuts_fall_in_pauses_when_there_are_pauses():
    """Speech with a quiet gap near the segment boundary: the cut lands in
    the gap, not mid-word."""
    audio = np.concatenate([loud_speech(13.0, seed=4) + room(13.0, seed=6),
                            room(0.6, -40.0, seed=7),
                            loud_speech(10.0, seed=5) + room(10.0, seed=8)])
    parts = [p for _, p in stream(audio, gate(hang_time=5.0))]
    assert len(parts) == 2, [p.duration for p in parts]
    cut = parts[0].start_offset + parts[0].duration
    assert 13.0 <= cut <= 13.6 + 0.02, f"cut at {cut:.2f} s, not in the pause at 13.0-13.6 s"
    assert_contiguous(parts, audio)


def test_short_radio_burst_is_unchanged():
    audio = np.concatenate([silence(1.0, SR), loud_speech(3.0), silence(2.0, SR)])
    events = stream(audio, gate())
    assert len(events) == 1
    at, part = events[0]
    assert at != "stop", "a burst followed by silence closes by itself"
    assert not part.continued and not part.continues_previous
    assert 3.0 <= part.duration <= 4.0


def test_stop_mid_stream_hands_on_the_remainder_contiguously():
    audio = loud_speech(37.0) + room(37.0)
    events = stream(audio, gate())
    assert events[-1][0] == "stop"
    last = events[-1][1]
    assert last.continues_previous and not last.continued
    assert_contiguous([p for _, p in events], audio)


def test_a_short_tail_after_a_cut_is_kept_not_dropped_as_crackle():
    """Speech ending 0.1 s after a cut leaves a remainder shorter than
    min_duration. It is the end of a recording already partly handed on,
    so dropping it would leave a hole."""
    audio = np.concatenate([loud_speech(15.1) + room(15.1), silence(3.0, SR)])
    settings = gate(min_duration=0.5)
    parts = [p for _, p in stream(audio, settings)]
    assert len(parts) == 2, [p.duration for p in parts]
    assert parts[1].continues_previous
    assert_contiguous(parts, audio)


def test_short_tail_at_stop_is_kept():
    audio = loud_speech(15.1) + room(15.1)
    parts = [p for _, p in stream(audio, gate(min_duration=0.5))]
    assert parts[-1].continues_previous
    first, length = assert_contiguous(parts, audio)
    # Stop ends the audio, not a transmitter: no squelch-tail trim on the
    # last part of a recording already handed on in parts.
    assert first + length == audio.size


def test_long_noise_is_also_segmented_and_recorded():
    """Noise above the gate is recorded and handed on in parts too; the
    speech check (not the detector) decides what the transcript is worth."""
    audio = room(40.0, -10.0, seed=9)
    parts = [p for _, p in stream(audio, gate())]
    assert len(parts) >= 3
    assert all(p.duration <= 15.85 for p in parts)
    assert_contiguous(parts, audio)


def test_segmenting_can_be_turned_off_and_max_duration_still_applies():
    audio = loud_speech(50.0) + room(50.0)
    events = stream(audio, gate(segment_seconds=0, max_duration=20.0))
    live = [p for at, p in events if at != "stop"]
    assert live and all(p.truncated for p in live)
    assert not any(p.continued for _, p in events)


def test_segment_setting_is_validated():
    with pytest.raises(ValueError):
        DetectorSettings(segment_seconds=1.0).validate()
    with pytest.raises(ValueError):
        DetectorSettings(segment_seconds=3.0, segment_search_seconds=3.0).validate()
    DetectorSettings(segment_seconds=0).validate()


def test_capture_persists_every_part_in_order_with_its_own_audio(config, store, tmp_path):
    """End to end through replay: each part is a stored message with its
    own WAV, consecutive in time, marked as continuous audio."""
    audio = loud_speech(40.0) + room(40.0)
    wav = write_wav(str(tmp_path / "continuous.wav"), audio, SR)
    config.detector.mode = "fixed"
    config.detector.threshold_dbfs = -20.0
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    try:
        app.start_session(replay_path=wav, target_language="en", name="continuous")
        app.run_replay()
        txs = sorted(app.transmissions(), key=lambda t: t.started_at)
    finally:
        app.stop_session()
    assert len(txs) >= 3
    for before, after in zip(txs, txs[1:]):
        assert after.started_at >= before.started_at
        gap = (after.started_at - before.started_at).total_seconds() - before.duration
        assert abs(gap) < 0.01
    stored = np.concatenate([read_wav(t.audio_path)[0] for t in txs])
    assert abs(stored.size - audio.size) < SR, "audio missing from the stored parts"
    assert txs[0].signal_metadata["segment"] == {"continued": True,
                                                 "continues_previous": False}
    assert txs[-1].signal_metadata["segment"] == {"continued": False,
                                                  "continues_previous": True}
    assert all(t.transcript for t in txs)


# --- 2. probably-not-speech -----------------------------------------------

# Neutral radio words: the evidence table below is about the evidence, not
# the stock sign-offs (tests/test_signoff_noise.py covers those).
def result(text="Unit two, copy, over.", ratio=None, nsp=None, confidence=0.9,
           language="en", language_confidence=0.95, seconds="auto", duration=4.0):
    if seconds == "auto":
        seconds = None if ratio is None else round(ratio * duration, 2)
    return TranscriptionResult(
        text=text, language=language, language_confidence=language_confidence,
        confidence=confidence, segments=[TranscriptSegment(0.0, 2.0, text, confidence)],
        engine="scripted", engine_version="1",
        no_speech_probability=nsp, speech_ratio=ratio, speech_seconds=seconds)


@pytest.mark.parametrize("kwargs,verdict", [
    # Held back only when both signals agree there was no speech.
    (dict(ratio=0.0, nsp=0.8), "likely_noise"),
    (dict(ratio=0.03, nsp=0.9, duration=4.0), "likely_noise"),   # 0.12 s
    (dict(ratio=0.0, nsp=0.2, confidence=0.3), "likely_noise"),
    # The detector alone, against a confident engine: review, not held. A
    # short radio reply ("Copy.") can be under the detector's limit.
    (dict(ratio=0.05, nsp=0.1, duration=4.0), "uncertain"),     # 0.2 s
    (dict(ratio=0.0, nsp=0.2), "uncertain"),
    (dict(ratio=0.01, seconds=None), "uncertain"),       # share only
    (dict(ratio=0.01, seconds=None, nsp=0.7), "likely_noise"),
    # One short real phrase in a long noisy part (0.6 s of 15 s) is speech:
    # a low share alone flags nothing, or continuous audio would flood the
    # review queue.
    (dict(ratio=0.04, nsp=0.3, duration=15.0), "speech"),
    (dict(ratio=0.12, nsp=0.1), "speech"),
    (dict(ratio=0.6, nsp=0.7), "uncertain"),             # engine doubts it
    (dict(ratio=0.3, nsp=0.1), "speech"),                # faint but real speech
    (dict(ratio=None, nsp=0.8, confidence=0.3), "likely_noise"),
    (dict(ratio=None, nsp=0.8, confidence=0.9), "uncertain"),
    (dict(ratio=None, nsp=0.1), "speech"),
])
def test_assess_speech(kwargs, verdict):
    assert assess_speech(result(**kwargs))["verdict"] == verdict


def test_assess_speech_says_nothing_without_text_or_evidence():
    assert assess_speech(result(text="", ratio=0.0)) is None
    assert assess_speech(result()) is None


class ScriptedTranscription(MockTranscriptionEngine):
    """Returns the same scripted result for every recording."""

    def __init__(self, scripted: TranscriptionResult):
        super().__init__()
        self.scripted = scripted

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        self.calls += 1
        out = TranscriptionResult(**{**self.scripted.__dict__})
        if language:
            out.language = language
        return out


class PairTranslation(MockTranslationEngine):
    """A translator with a fixed set of installed pairs."""

    def __init__(self, pairs=(("es", "en"),), auto_install=False):
        super().__init__()
        self.pairs = set(pairs)
        self.auto_install = auto_install

    def supports_pair(self, source, target):
        return source is None or source == target or (source, target) in self.pairs


def run_one(config, store, tmp_path, transcription, translation, **session):
    wav = write_wav(str(tmp_path / "burst.wav"),
                    np.concatenate([silence(1.0, SR), loud_speech(3.0), silence(2.0, SR)]),
                    SR)
    app = BabelFishRApp(config=config, store=store)
    app.transcription = transcription
    app.translation = translation
    try:
        app.start_session(replay_path=wav, name="one", **session)
        app.run_replay()
        txs = app.transmissions()
        assert len(txs) == 1
        return app, store.get_transmission(txs[0].id)
    finally:
        app.stop_session()


def test_noise_transcript_is_kept_not_translated_and_marked_for_review(
        config, store, tmp_path):
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Gracias por ver el video.", ratio=0.01,
                                                   nsp=0.8, language="es")),
                      translation, target_language="en")
    assert tx.transcript == "Gracias por ver el video."      # kept, never deleted
    assert tx.translation == "" and translation.calls == 0
    assert tx.state is ProcessingState.COMPLETE
    assert tx.signal_metadata["speech_check"]["verdict"] == "likely_noise"
    assert tx.needs_review
    assert tx.audio_path and pathlib.Path(tx.audio_path).exists()
    assert tx.id in {t.id for t in store.review_queue()}


def test_faint_real_speech_is_still_translated(config, store, tmp_path):
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Equipo en posición.", ratio=0.3,
                                                   nsp=0.1, language="es")),
                      translation, target_language="en")
    assert tx.translation and translation.calls == 1
    assert tx.signal_metadata["speech_check"]["verdict"] == "speech"
    assert not tx.needs_review
    assert tx.id not in {t.id for t in store.review_queue()}


def test_uncertain_speech_is_translated_but_marked_for_review(config, store, tmp_path):
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Equipo en posición.", ratio=0.6,
                                                   nsp=0.7, language="es")),
                      translation, target_language="en")
    assert tx.translation and tx.needs_review
    assert tx.id in {t.id for t in store.review_queue()}


# --- 3. translation errors and English ------------------------------------

def test_uncertain_automatic_language_is_not_reported_as_a_missing_pack(
        config, store, tmp_path):
    """The field case: English speech guessed as Norwegian Nynorsk at 70%."""
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Thanks for watching.", ratio=0.5,
                                                   language="nn", language_confidence=0.696)),
                      translation, target_language="en")
    # Not a failure: translation was not attempted and the message says why
    # (tests/test_translation_unresolved.py covers the whole state).
    assert tx.state is ProcessingState.COMPLETE and tx.error is None
    unresolved = tx.signal_metadata["translation_unresolved"]
    assert unresolved["reason"] == "language-uncertain"
    assert "'nn' (70%)" in unresolved["message"]
    assert "Source language" in unresolved["message"]
    assert tx.transcript == "Thanks for watching."
    assert translation.calls == 0 and tx.needs_review


def test_confident_language_without_a_pack_says_the_pack_is_missing(
        config, store, tmp_path):
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Hei, dette er en test.", ratio=0.5,
                                                   language="nb", language_confidence=0.97)),
                      translation, target_language="en")
    assert tx.state is ProcessingState.FAILED
    assert "no nb->en language pack is installed" in tx.error.message
    assert "uncertain" not in tx.error.message


def test_specified_language_without_a_pack_says_the_pack_is_missing(
        config, store, tmp_path):
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Hei.", ratio=0.5, language="nb",
                                                   language_confidence=None)),
                      translation, target_language="en", source_language="nb",
                      source_language_mode="specified")
    assert tx.source_language_mode is SourceLanguageMode.SPECIFIED
    assert "no nb->en language pack is installed" in tx.error.message


def test_auto_install_still_gets_its_chance(config, store, tmp_path):
    """Online / Setup with auto-install: the engine is asked, not pre-empted."""
    translation = PairTranslation(pairs=(), auto_install=True)
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Hei.", ratio=0.5, language="nb",
                                                   language_confidence=0.97)),
                      translation, target_language="en")
    assert translation.calls == 1


@pytest.mark.parametrize("session", [
    dict(),                                                       # detected
    dict(source_language="en", source_language_mode="specified"),  # operator set
])
def test_english_to_english_is_not_translated(config, store, tmp_path, session):
    translation = PairTranslation(pairs=())
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Radio check, how copy.", ratio=0.6)),
                      translation, target_language="en", **session)
    assert tx.state is ProcessingState.COMPLETE and tx.error is None
    assert tx.translation == "" and translation.calls == 0
    assert tx.source_language == "en"


def test_specified_english_overrides_a_wrong_automatic_guess(config, store, tmp_path):
    """The English retest: the scripted engine would guess 'nn', but the
    session's own source-language setting decides."""
    translation = PairTranslation(pairs=())
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Thanks for watching.", ratio=0.5,
                                                   language="nn", language_confidence=0.696)),
                      translation, target_language="en", source_language="en",
                      source_language_mode="specified")
    assert tx.source_language == "en" and tx.error is None
    assert tx.state is ProcessingState.COMPLETE and translation.calls == 0


def test_detector_labels_stay_advisory_for_the_speech_check():
    """The detector's content class (which has misjudged real speech as
    noise before) plays no part in the speech check."""
    check = assess_speech(result("Equipo en posición.", ratio=0.5, nsp=0.1))
    assert check["verdict"] == "speech"


def test_segmenting_never_overrides_a_shorter_archival_cap():
    """max_duration below segment_seconds: a stuck transmitter is still
    closed as truncated, as before."""
    audio = loud_speech(9.0) + room(9.0)
    parts = [p for _, p in stream(audio, gate(max_duration=2.0))]
    assert len(parts) > 1 and any(p.truncated for p in parts)
    assert not any(p.continued or p.continues_previous for p in parts)


# --- recovery: the operator disagrees with the speech check or the guess ----

def test_retry_as_speech_lifts_the_noise_hold_and_translates(config, store, tmp_path):
    """"Retry as speech in...": transcribed again in the chosen language and
    translated, which editing the text by hand would not do."""
    transcription = ScriptedTranscription(result("Unidad dos, adelante.", ratio=0.01,
                                                 nsp=0.8, language="es"))
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path, transcription, translation,
                      target_language="en")
    assert tx.translation == "" and tx.needs_review
    calls = transcription.calls

    _retry(config, store, transcription, translation, tx.id, source_language="es")
    tx = store.get_transmission(tx.id)
    assert transcription.calls == calls + 1, "not transcribed again"
    assert tx.source_language == "es"
    assert tx.source_language_mode is SourceLanguageMode.SPECIFIED
    assert tx.translation and translation.calls == 1
    assert tx.signal_metadata["speech_check"]["verdict"] == "likely_noise"  # still on record
    assert tx.signal_metadata["speech_confirmed"]["source_language"] == "es"
    # Confirming it is speech is not checking the new words: still flagged.
    assert tx.needs_review
    assert tx.id in {t.id for t in store.review_queue()}
    # Saving the operator's check (Edit transcript and translation) clears it.
    app = BabelFishRApp(config=config, store=store)
    app.correct(tx.id, transcript=tx.transcript, translation=tx.translation)
    tx = store.get_transmission(tx.id)
    assert not tx.needs_review and tx.id not in {t.id for t in store.review_queue()}


@pytest.mark.parametrize("language", [None, "es"])
def test_retry_on_unvoiced_signoff_keeps_the_guess_out_of_translation(
        config, store, tmp_path, language):
    transcription = ScriptedTranscription(result("Gracias por ver el video.", ratio=0.0,
                                                 nsp=0.8, language="es"))
    translation = PairTranslation(pairs=(("es", "en"),))
    _, tx = run_one(config, store, tmp_path, transcription, translation, target_language="en")
    _retry(config, store, transcription, translation, tx.id, source_language=language,
           confirm_speech=True)
    tx = store.get_transmission(tx.id)
    assert tx.signal_metadata["speech_confirmed"]["source_language"] == language
    assert tx.transcript == "Gracias por ver el video."
    assert tx.guess_withheld and not tx.shown_transcript
    assert not tx.translation and translation.calls == 0
    pytest.importorskip("PySide6")
    from babelfishr.ui.timeline import _speech_check_note
    assert "new guess still lacks speech evidence" in _speech_check_note(tx)


def test_retry_as_speech_fixes_a_wrong_automatic_language(config, store, tmp_path):
    transcription = ScriptedTranscription(result("Thanks for watching.", ratio=0.5,
                                                 language="nn", language_confidence=0.696))
    translation = PairTranslation(pairs=())
    app, tx = run_one(config, store, tmp_path, transcription, translation,
                      target_language="en")
    assert tx.state is ProcessingState.COMPLETE and tx.error is None
    assert "Transcribe as speech\u2026" in tx.signal_metadata["translation_unresolved"]["message"]

    _retry(config, store, transcription, translation, tx.id, source_language="en")
    tx = store.get_transmission(tx.id)
    assert tx.state is ProcessingState.COMPLETE and tx.error is None
    assert tx.source_language == "en" and tx.translation == ""   # English: no translation
    assert translation.calls == 0


def test_plain_retry_keeps_the_noise_hold(config, store, tmp_path):
    """Retry without a language re-runs the same evidence and holds again:
    the note does not promise otherwise."""
    transcription = ScriptedTranscription(result("Gracias.", ratio=0.0, nsp=0.8,
                                                 language="es"))
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path, transcription, translation,
                      target_language="en")
    _retry(config, store, transcription, translation, tx.id)
    tx = store.get_transmission(tx.id)
    assert tx.translation == "" and translation.calls == 0 and tx.needs_review


def _retry(config, store, transcription, translation, tx_id, **kwargs):
    """Retry on a processor running the given engines, and wait for it."""
    from babelfishr.pipeline import EventBus, ProcessingPipeline

    pipeline = ProcessingPipeline(store=store, transcription=transcription,
                                  translation=translation, config=config,
                                  events=EventBus())
    pipeline.start(None)
    try:
        assert pipeline.retry(tx_id, **kwargs)
        assert pipeline.wait_until_idle(10.0)
    finally:
        pipeline.stop()


def test_app_retry_passes_the_chosen_language_on(app, monkeypatch):
    seen = []

    class Pipeline:
        def retry(self, tx_id, source_language=None):
            seen.append((tx_id, source_language))
            return True

    monkeypatch.setattr(app, "_processing_pipeline", lambda: Pipeline())
    assert app.retry("tx_1", source_language="en") and app.retry("tx_2")
    assert seen == [("tx_1", "en"), ("tx_2", None)]


# --- the local VAD itself -----------------------------------------------------

def test_local_vad_uses_the_bundled_detector_unpadded_and_switches_off_telemetry(
        monkeypatch):
    """faster-whisper 1.2.1's API: get_speech_timestamps(audio, vad_options,
    sampling_rate); onnxruntime 1.32's public disable_telemetry_events()."""
    import sys
    import types

    from babelfishr.providers import whisper_local

    seen = {}
    vad = types.ModuleType("faster_whisper.vad")

    class VadOptions:
        def __init__(self, **kwargs):
            seen["options"] = kwargs

    def get_speech_timestamps(audio, vad_options=None, sampling_rate=16000, **kwargs):
        seen["rate"] = sampling_rate
        return [{"start": 16000, "end": 24000}]        # 0.5 s of speech

    vad.VadOptions, vad.get_speech_timestamps = VadOptions, get_speech_timestamps
    ort = types.ModuleType("onnxruntime")
    ort.disable_telemetry_events = lambda: seen.setdefault("telemetry_off", True)
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setattr(whisper_local, "_telemetry_disabled", False)

    ratio, seconds = whisper_local.local_speech_activity(np.zeros(16000 * 4, np.float32))
    assert (ratio, seconds) == (0.125, 0.5)
    assert seen["options"] == {"speech_pad_ms": 0} and seen["rate"] == 16000
    assert seen.get("telemetry_off")


def test_local_vad_absent_is_no_opinion_not_a_guess(monkeypatch):
    import sys

    from babelfishr.providers import whisper_local

    monkeypatch.setitem(sys.modules, "faster_whisper.vad", None)
    assert whisper_local.local_speech_activity(np.zeros(16000)) == (None, None)


def test_bundled_vad_model_is_packaged():
    """The build collects faster_whisper's data files, which is where the
    Silero model lives (faster_whisper/assets/silero_vad_v6.onnx)."""
    spec = (pathlib.Path(__file__).resolve().parents[1] / "packaging" / "babelfishr.spec"
            ).read_text()
    assert 'for package in ("faster_whisper"' in spec
    assert "collect_data_files" in spec


# --- what the operator sees -----------------------------------------------------

def test_held_message_explains_itself_and_offers_retry_as_speech(config, store, tmp_path,
                                                                 monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from babelfishr.models import Session, Transmission, utcnow
    from babelfishr.ui.main_window import MainWindow

    qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    conversation = store.create_conversation("Held")
    session = Session(name="Held", conversation_id=conversation.id, sample_rate=SR)
    store.save_session(session)
    tx = Transmission(session_id=session.id, started_at=utcnow(), duration=4.0,
                      sample_rate=SR, source_language="es", transcript="Gracias.",
                      state=ProcessingState.COMPLETE,
                      signal_metadata={"speech_check": {
                          "verdict": "likely_noise", "speech_ratio": 0.0,
                          "speech_seconds": 0.0, "no_speech_probability": 0.4}})
    tx.audio_path = write_wav(str(tmp_path / "held.wav"), room(4.0), SR)
    store.save_transmission(tx)

    app = BabelFishRApp(config=config, store=store)
    app.select_conversation(conversation.id)
    window = MainWindow(app)
    try:
        window._reload_timeline()
        bubble = window.timeline.bubble(tx.id)
        details = bubble.findChildren(QtWidgets.QLabel)
        text = " ".join(label.text() for label in details)
        # One short status on the card; the evidence only on inspection.
        assert "Probably not speech" not in text
        assert "Audio unintelligible" in bubble.pairing_label.text()
        tip = bubble.pairing_label.toolTip()
        assert "Probably not speech" in tip and "0.0 s of speech" in tip
        assert "Transcribe as speech\u2026" in tip
        menu_texts = [a.text() for a in bubble.menu_button.menu().actions()]
        assert "Transcribe as speech\u2026" in menu_texts
        assert "Retry as speech in..." not in menu_texts
        assert "Transcribe anyway" not in menu_texts

        seen = []
        monkeypatch.setattr(app, "transcribe_as_speech",
                            lambda tx_id, source_language=None:
                            seen.append((tx_id, source_language)) or True)
        monkeypatch.setattr(QtWidgets.QInputDialog, "getItem",
                            staticmethod(lambda *a, **k: ("English (en)", True)))
        action = next(a for a in bubble.menu_button.menu().actions()
                      if a.text() == "Transcribe as speech\u2026")
        action.trigger()
        qt.processEvents()
        assert seen == [(tx.id, "en")]
    finally:
        window.close()
        qt.processEvents()


# --- review findings ------------------------------------------------------------

@pytest.mark.parametrize("seconds", [14.0, 14.5])
def test_a_burst_just_under_the_segment_length_is_one_message(seconds):
    """The cut used to land in the hang after the burst, leaving a silent
    0.2 s "last part" for speech recognition to invent words from."""
    from babelfishr.testing import tone

    audio = np.concatenate([silence(1.0, SR), tone(seconds, 1000.0, SR, -12.0),
                            silence(3.0, SR)])
    parts = [p for _, p in stream(audio, gate())]
    assert len(parts) == 1, [round(p.duration, 2) for p in parts]
    assert not parts[0].continued


def test_every_part_holds_signal():
    """No part is pure quiet: each holds at least one frame above the gate."""
    from babelfishr.detect import dbfs, rms

    audio = np.concatenate([loud_speech(31.0) + room(31.0), silence(3.0, SR)])
    frame = int(0.02 * SR)
    for part in (p for _, p in stream(audio, gate())):
        levels = [dbfs(rms(part.audio[i:i + frame]))
                  for i in range(0, part.audio.size - frame + 1, frame)]
        assert max(levels) >= -24.0          # at least one frame held the gate


def test_retry_as_speech_while_processing_is_refused_and_changes_nothing(
        config, store, tmp_path):
    from babelfishr.pipeline import EventBus, ProcessingPipeline

    transcription = ScriptedTranscription(result("Gracias.", ratio=0.0, nsp=0.8,
                                                 language="es"))
    app, tx = run_one(config, store, tmp_path, transcription,
                      PairTranslation(pairs=(("es", "en"),)), target_language="en")
    pipeline = ProcessingPipeline(store=store, transcription=transcription,
                                  translation=None, config=config, events=EventBus())
    pipeline._in_flight.add(tx.id)                     # the worker has it
    assert not pipeline.retry(tx.id, source_language="en")
    after = store.get_transmission(tx.id)
    assert "speech_confirmed" not in after.signal_metadata
    assert after.source_language == "es" and after.needs_review


def test_the_confirmation_note_says_only_what_happened():
    pytest.importorskip("PySide6")
    from babelfishr.models import Transmission, utcnow
    from babelfishr.ui.timeline import _speech_check_note

    meta = {"speech_check": {"verdict": "likely_noise"},
            "speech_confirmed": {"source_language": "en"}}
    tx = Transmission(session_id="s", started_at=utcnow(), source_language="en",
                      target_language="en", signal_metadata=meta,
                      state=ProcessingState.CAPTURED)
    assert "queued" in _speech_check_note(tx)
    tx.state = ProcessingState.COMPLETE
    assert "not translated" in _speech_check_note(tx)
    tx.source_language, tx.translation = "es", "Thanks."
    assert "and translated" in _speech_check_note(tx)
    assert "Edit transcript and translation marks it reviewed" in _speech_check_note(tx)
    from babelfishr.models import ErrorInfo
    tx.state = ProcessingState.FAILED
    tx.error = ErrorInfo(stage="transcription", message="engine unavailable")
    assert "Transcribing it again failed" in _speech_check_note(tx)
    tx.error = ErrorInfo(stage="translation", message="no pack")
    assert "the translation failed" in _speech_check_note(tx)
    tx.state, tx.error = ProcessingState.SKIPPED, None
    assert "not transcribed again" in _speech_check_note(tx)


def test_room_noise_between_close_and_open_thresholds_is_still_cut():
    """Speech opens the gate; room noise then sits above the close
    threshold (-24) but below the open one (-20). The gate never closes and
    no frame is "loud": parts must still be handed on while it runs."""
    audio = np.concatenate([loud_speech(1.0), room(60.0, -22.0, seed=11)])
    events = stream(audio, gate())
    live = [(at, p) for at, p in events if at != "stop"]
    assert live and live[0][0] <= 15.0 + 0.8 + 0.5, [round(a, 1) for a, _ in live]
    assert len(live) >= 3
    assert_contiguous([p for _, p in events], audio)


def _blocking_engine():
    import threading

    started, release = threading.Event(), threading.Event()

    class Blocking(ScriptedTranscription):
        def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
            started.set()
            release.wait(10)
            return super().transcribe(audio, sample_rate, language=language,
                                      vocabulary=vocabulary)

    return Blocking(result("Gracias.", ratio=0.0, nsp=0.8, language="es")), started, release


def _snapshot(store, tx_id):
    tx = store.get_transmission(tx_id)
    return (tx.state, tx.source_language, tx.source_language_mode, tx.reviewed,
            dict(tx.signal_metadata or {}), tx.error)


def test_retry_refused_while_the_worker_has_the_message_changes_nothing(
        config, store, tmp_path):
    from babelfishr.pipeline import EventBus, ProcessingPipeline

    engine, started, release = _blocking_engine()
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Gracias.", ratio=0.0, nsp=0.8,
                                                   language="es")),
                      PairTranslation(pairs=(("es", "en"),)), target_language="en")
    pipeline = ProcessingPipeline(store=store, transcription=engine,
                                  translation=PairTranslation(pairs=(("es", "en"),)),
                                  config=config, events=EventBus())
    pipeline.start(None)
    try:
        assert pipeline.retry(tx.id)                 # really queued
        assert started.wait(5)                       # the worker is inside it
        before = _snapshot(store, tx.id)
        assert not pipeline.retry(tx.id, source_language="en")
        assert not pipeline.retry(tx.id)
        assert _snapshot(store, tx.id) == before, "a refused retry changed the row"
    finally:
        release.set()
        pipeline.wait_until_idle(10)
        pipeline.stop()
    after = store.get_transmission(tx.id)
    assert after.source_language == "es" and "speech_confirmed" not in after.signal_metadata


def test_retry_refused_by_a_stopped_processor_changes_nothing(config, store, tmp_path):
    from babelfishr.pipeline import EventBus, ProcessingPipeline

    transcription = ScriptedTranscription(result("Gracias.", ratio=0.0, nsp=0.8,
                                                 language="es"))
    app, tx = run_one(config, store, tmp_path, transcription,
                      PairTranslation(pairs=(("es", "en"),)), target_language="en")
    pipeline = ProcessingPipeline(store=store, transcription=transcription,
                                  translation=None, config=config, events=EventBus())
    pipeline.start(None)
    pipeline.stop()
    before = _snapshot(store, tx.id)
    assert not pipeline.retry(tx.id, source_language="en")
    assert _snapshot(store, tx.id) == before
    assert not pipeline.is_in_flight(tx.id)


def test_retry_of_a_missing_message_reports_failure_and_releases_it(config, store):
    from babelfishr.pipeline import EventBus, ProcessingPipeline

    pipeline = ProcessingPipeline(store=store, transcription=MockTranscriptionEngine(),
                                  translation=None, config=config, events=EventBus())
    pipeline.start(None)
    try:
        assert not pipeline.retry("tx_gone", source_language="en")
        assert not pipeline.is_in_flight("tx_gone")
    finally:
        pipeline.stop()


@pytest.mark.parametrize("error", ["EngineError", "EngineUnavailable"])
def test_a_failing_pack_lookup_is_a_handled_translation_failure(
        config, store, tmp_path, error):
    from babelfishr.providers import base

    exc_type = getattr(base, error)

    class Broken(PairTranslation):
        def supports_pair(self, source, target):
            raise exc_type("language pack index could not be read")

    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Equipo en posición.", ratio=0.6,
                                                   language="es")),
                      Broken(), target_language="en")
    assert tx.transcript == "Equipo en posición."            # preserved
    assert tx.state is ProcessingState.FAILED
    assert tx.error.stage == "translation"
    assert "language pack index could not be read" in tx.error.message


def test_a_broken_translation_engine_is_not_reported_as_a_missing_pack(
        config, store, tmp_path):
    """Argos that cannot import or read any pack lists no pairs, so every
    pair looks missing. The real reason is reported, not "install es->en"."""

    class Broken(PairTranslation):
        def available(self):
            return False

        def unavailable_reason(self):
            return "Argos Translate is not installed."

    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Equipo en posición.", ratio=0.6,
                                                   language="es")),
                      Broken(pairs=()), target_language="en")
    assert tx.state is ProcessingState.FAILED and tx.error.stage == "translation"
    assert "Argos Translate is not installed" in tx.error.message
    assert "language pack is installed" not in tx.error.message
    assert "uncertain" not in tx.error.message
    assert tx.transcript == "Equipo en posición."
