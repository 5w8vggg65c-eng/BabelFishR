"""Field follow-up: arm once and capture each transmission whole, keep noise
from passing as reliable speech, and give the transcript room to be read.

Synthetic or public text and audio only; no private recording is used.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import numpy as np
import pytest

from babelfishr.audio.source import AudioBlock
from babelfishr.audio.wavefile import write_wav
from babelfishr.detect import DetectorSettings, RadioActivityDetector
from babelfishr.models import ProcessingState
from babelfishr.pipeline import assess_speech, degenerate_text
from babelfishr.providers.base import TranscriptionResult, TranscriptSegment
from babelfishr.testing import bandpass, silence, speech_like

SR = 48_000
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def at(x, dbfs):
    return x / np.max(np.abs(x)) * 10 ** (dbfs / 20.0)


def run_detector(parts, settings):
    """Feed named parts; return {name: share of its samples in recordings}."""
    marks, chunks, t = [], [], 0
    for name, x in parts:
        marks.append((name, t, t + x.size))
        chunks.append(x)
        t += x.size
    audio = np.concatenate(chunks)
    det = RadioActivityDetector(SR, settings)
    found = []
    for i in range(0, audio.size, 2048):
        found += det.push(AudioBlock(audio[i:i + 2048], SR,
                                     T0 + dt.timedelta(seconds=i / SR), i / SR))
    found += det.flush()
    kept = {}
    for name, a, b in marks:
        n = 0
        for d in found:
            s = int(round(d.start_offset * SR))
            n += max(0, min(b, s + d.audio.size) - max(a, s))
        kept[name] = n / (b - a)
    return kept, found


GATES = {"fixed -20 dBFS (field setting)": dict(mode="fixed", threshold_dbfs=-20.0),
         "auto": {}}


# --- capture: whole transmissions, from the first syllable to the last -------

@pytest.mark.parametrize("gate", GATES.values(), ids=GATES.keys())
def test_soft_opening_and_closing_syllables_are_kept(gate):
    body = at(speech_like(2.0, SR, seed=3), -8)
    onset = at(speech_like(0.25, SR, seed=9), -32)       # 24 dB under the body
    closing = at(speech_like(0.4, SR, seed=8), -30)      # 22 dB under the body
    kept, found = run_detector([("gap", silence(1, SR)), ("onset", onset), ("body", body),
                                ("closing", closing), ("gap2", silence(2, SR))],
                               DetectorSettings(**gate))
    assert len(found) == 1
    assert kept["onset"] == 1.0 and kept["body"] == 1.0 and kept["closing"] == 1.0, kept


def test_a_word_ending_in_a_fricative_is_not_trimmed_as_squelch():
    rng = np.random.default_rng(5)
    fricative = at(bandpass(rng.normal(0, 1, int(0.15 * SR)), 3500, 9000, SR,
                            numtaps=127), -24)
    kept, _ = run_detector([("gap", silence(1, SR)), ("body", at(speech_like(2.0, SR), -8)),
                            ("fricative", fricative), ("gap2", silence(2, SR))],
                           DetectorSettings(mode="fixed", threshold_dbfs=-20.0))
    assert kept["fricative"] == 1.0, kept


@pytest.mark.parametrize("seconds", [0.4, 0.5, 0.7])
def test_very_short_genuine_speech_is_kept(seconds):
    """Short replies ("Copy.", "Over.") at the level the other speech
    fixtures use. The signal length is measured from the frames that opened
    the gate to the last frame that held it, so trimming the post-roll
    cannot make a real burst look like crackle (the old count did: all three
    of these were dropped)."""
    word = at(speech_like(seconds, SR, seed=4), -3)
    kept, found = run_detector([("gap", silence(1.5, SR)), ("word", word),
                                ("gap2", silence(2, SR))],
                               DetectorSettings(mode="fixed", threshold_dbfs=-20.0))
    assert len(found) == 1 and kept["word"] == 1.0, (kept, len(found))


def test_crackle_shorter_than_min_duration_is_still_dropped():
    click = at(speech_like(0.06, SR, seed=6), -6)
    _, found = run_detector([("gap", silence(1.5, SR)), ("click", click),
                             ("gap2", silence(2, SR))],
                            DetectorSettings(mode="fixed", threshold_dbfs=-20.0))
    assert found == []


def test_new_installs_get_the_wider_rolls_and_stored_values_are_kept(tmp_path):
    from babelfishr.config import Config

    assert (Config().detector.pre_roll, Config().detector.post_roll) == (0.5, 0.5)
    # A settings file that says 0.3 / 0.2 - an older default or the
    # operator's choice - is read exactly as written.
    path = tmp_path / "settings.toml"
    path.write_text("[detector]\npre_roll = 0.3\npost_roll = 0.2\n", encoding="utf-8")
    loaded = Config.load(str(path))
    assert (loaded.detector.pre_roll, loaded.detector.post_roll) == (0.3, 0.2)


def test_state_words_say_audio_activity_is_estimated(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.pipeline import PipelineState
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        window._state = PipelineState.RECEIVING
        window._refresh_state_badge()
        assert "Audio active (est.)" in window.state_badge.text()
        assert "Receiving" not in window.state_badge.text()
        tip = window.state_badge.toolTip()
        assert "not the receiver's own status" in tip and "estimated" in tip
        window._state = PipelineState.LISTENING
        window._refresh_state_badge()
        assert "Armed" in window.state_badge.text()
        assert "not the radio's squelch" in window.state_badge.toolTip()
    finally:
        window.close()


# --- noise must not read as reliable speech ------------------------------------

LEGITIMATE = [
    "今日は天気がいいので公園に行きます。",                 # Japanese
    "这是一个测试，我们在等待信号。",                       # Chinese
    "مرحبا كيف حالك اليوم، نحن في الموقع",                  # Arabic
    "გამარჯობა, როგორ ხარ? ჩვენ პოზიციაზე ვართ.",          # Georgian
    "Команда на позиции, ждём сигнала.",                    # Russian
    "नमस्ते, हम स्थिति में हैं और संकेत की प्रतीक्षा कर रहे हैं।",  # Hindi
    "Copy.", "Yes", "OK", "Roger that.", "10-4", "Over.", "no no no",
    "Mayday, mayday, mayday, this is Kilo One.",
    "Echo echo echo, radio check, how copy?",
    "The quick brown fox jumps over the lazy dog near the river bank today.",
]

#: A synthetic line with the aggregate shape of the 0.933-confidence field
#: loop (its text was not shared): 56 non-space characters - one opening
#: bracket (Ps), 54 closing brackets (Pe), one symbol (So) - three distinct,
#: every one a separate token, longest unbroken run 1, no letter or digit.
FIELD_LOOP_SHAPE = " ".join(["("] + [")"] * 54 + ["\u266a"])

#: Structural loops: very unlikely to be faithful transcripts. Held on their own.
SEVERE_LOOPS = [
    FIELD_LOOP_SHAPE,
    "ლლლლლლლლლლლლლლლლლლლლლლლლლლლლლლ",
    "!!!!!!!!!!!!!!!!!!!!",
    "ーーーーーーーーーーーー",
    "...............",
    "?!?!?!?!?!?!",
]

#: Loop-like but short or with words in it: review only.
MILD_LOOPS = ["♪ ♪ ♪ ♪ ♪", "...", "の、の、の、の、の、の、の、の、"]

#: Root review of 44cb96b: each of these was held as a severe loop despite
#: speech heard (VAD 0.8 / 2 s), no-speech 0.01 and confidence 0.99.
#: Punctuation, number formatting and repeated digits are ordinary text.
ROOT_COUNTEREXAMPLES = {
    "NO!!!": "speech",
    "S.O.S. S.O.S. S.O.S.": "speech",
    "1, 2, 3, 4, 5.": "speech",
    "7, 7, 7, 7, 7, 7.": "uncertain",       # repeated: a look, never a hold
    "\u0661\u060c \u0662\u060c \u0663\u060c \u0664\u060c \u0665.": "speech",  # Arabic-Indic
    "00000000": "speech",
    "Nooooooooooooo! Stop!": "speech",
}

#: Repeated words: noise produces these, and so do real calls. A review
#: flag at most, never a hold on repetition alone.
REPEATED_CALLS = [
    "help help help help help help",
    "mayday mayday mayday mayday mayday mayday",
    "SOS SOS SOS SOS SOS SOS",
    "K1ABC K1ABC K1ABC K1ABC K1ABC K1ABC",          # a callsign
    "7 7 7 7 7 7",                                  # a number
    "10-4 10-4 10-4 10-4 10-4 10-4",
    "помогите помогите помогите помогите помогите помогите",   # Russian
    "助けて 助けて 助けて 助けて 助けて 助けて",                # Japanese
    "النجدة النجدة النجدة النجدة النجدة النجدة",            # Arabic
    "ლ ლ ლ ლ ლ ლ ლ ლ ლ ლ ლ ლ",
    "Thank you. Thank you. Thank you. Thank you. Thank you.",
]


@pytest.mark.parametrize("text", LEGITIMATE + ["help help help"])
def test_legitimate_speech_in_any_script_is_not_called_a_loop(text):
    assert degenerate_text(text) == ("", "")


@pytest.mark.parametrize("text", SEVERE_LOOPS)
def test_structural_loops_are_severe_in_any_script(text):
    assert degenerate_text(text)[0] == "severe"


@pytest.mark.parametrize("text", MILD_LOOPS)
def test_short_or_wordy_loops_are_only_a_reason_to_look(text):
    assert degenerate_text(text)[0] == "repeated"


@pytest.mark.parametrize("text,verdict", ROOT_COUNTEREXAMPLES.items())
def test_punctuation_and_numbers_are_never_held_against_speech_evidence(text, verdict):
    check = assess_speech(result(text, confidence=0.99, ratio=0.8, seconds=2.0,
                                 nsp=0.01))
    assert check["verdict"] == verdict, (text, check)
    assert degenerate_text(text)[0] != "severe"


def test_the_field_loop_shape_is_held_even_against_speech_evidence():
    """The safeguard root asked to keep: confidence and a voice detector
    hearing room speech do not make a bracket loop a transcript."""
    for kwargs in (dict(confidence=0.933, language="nn", language_confidence=0.531),
                   dict(confidence=0.99, ratio=0.8, seconds=2.0, nsp=0.01)):
        check = assess_speech(result(FIELD_LOOP_SHAPE, **kwargs))
        assert check["verdict"] == "likely_noise" and check["repetition"] == "severe"


@pytest.mark.parametrize("text", REPEATED_CALLS)
def test_repeated_calls_are_only_repetition_in_any_script(text):
    assert degenerate_text(text)[0] == "repeated"


@pytest.mark.parametrize("text", REPEATED_CALLS)
def test_a_repeated_call_is_not_held_for_repetition_alone(text):
    """With speech heard, or with no voice-detector opinion at all, a
    repeated call is translated; it is only marked for a look."""
    heard = assess_speech(result(text, ratio=0.6, seconds=3.0, nsp=0.1))
    assert heard["verdict"] == "uncertain" and heard["repetition"] == "repeated"
    no_vad = assess_speech(result(text, confidence=0.93))
    assert no_vad["verdict"] == "uncertain"


@pytest.mark.parametrize("text", REPEATED_CALLS[:3])
def test_a_repeated_line_is_held_when_no_speech_evidence_agrees(text):
    vad_silent = assess_speech(result(text, ratio=0.0, seconds=0.0, nsp=0.2))
    engine_silent = assess_speech(result(text, nsp=0.8))
    assert vad_silent["verdict"] == "likely_noise"
    assert engine_silent["verdict"] == "likely_noise"


def result(text, confidence=0.9, language="en", language_confidence=0.95,
           ratio=None, seconds=None, nsp=None):
    return TranscriptionResult(
        text=text, language=language, language_confidence=language_confidence,
        confidence=confidence, segments=[TranscriptSegment(0.0, 5.9, text, confidence)],
        engine="scripted", engine_version="1", no_speech_probability=nsp,
        speech_ratio=ratio, speech_seconds=seconds)


def test_engine_confidence_alone_never_clears_a_loop():
    """The root counterexample, in aggregate: a 5.9 s repeated-symbol line
    at transcript confidence 0.933, detected as 'nn' at 0.531."""
    check = assess_speech(result(FIELD_LOOP_SHAPE, confidence=0.933,
                                 language="nn", language_confidence=0.531))
    assert check["verdict"] == "likely_noise" and check["repetition"] == "severe"
    assert any("loop" in r for r in check["reasons"])


@pytest.mark.parametrize("text", LEGITIMATE[:6])
def test_non_latin_speech_with_good_evidence_is_speech(text):
    check = assess_speech(result(text, ratio=0.7, seconds=3.0, nsp=0.05))
    assert check["verdict"] == "speech" and check["reasons"] == []


def test_a_short_reply_is_not_held(qt_app):
    check = assess_speech(result("Copy.", ratio=0.05, seconds=0.2, nsp=0.1))
    assert check["verdict"] == "uncertain"          # review, translated


@pytest.fixture
def qt_app():
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _run_scripted(config, store, tmp_path, scripted, pairs=(("es", "en"),)):
    from test_live_audio_fixes import PairTranslation, ScriptedTranscription, run_one

    translation = PairTranslation(pairs=pairs)
    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(scripted),
                      translation, target_language="en")
    return tx, translation


def test_a_confident_loop_is_held_visibly_with_its_words_and_audio_kept(
        config, store, tmp_path):
    loop = FIELD_LOOP_SHAPE
    tx, translation = _run_scripted(config, store, tmp_path,
                                    result(loop, confidence=0.933, language="nn",
                                           language_confidence=0.531))
    assert tx.transcript == loop                      # raw words kept
    assert tx.translation == "" and translation.calls == 0
    assert tx.state is ProcessingState.COMPLETE and tx.error is None
    assert tx.signal_metadata["speech_check"]["verdict"] == "likely_noise"
    assert tx.needs_review and tx.id in {t.id for t in store.review_queue()}
    assert pathlib.Path(tx.audio_path).exists()        # audio kept


def test_non_latin_speech_is_translated_normally(config, store, tmp_path):
    text = "Команда на позиции, ждём сигнала."
    tx, translation = _run_scripted(config, store, tmp_path,
                                    result(text, language="ru", ratio=0.7, seconds=3.0,
                                           nsp=0.05),
                                    pairs=(("ru", "en"),))
    assert tx.translation and translation.calls == 1 and not tx.needs_review


def test_a_repeated_call_is_translated_and_marked(config, store, tmp_path):
    text = "mayday mayday mayday mayday mayday mayday"
    tx, translation = _run_scripted(config, store, tmp_path,
                                    result(text, language="es", ratio=0.6, seconds=3.0,
                                           nsp=0.1))
    assert translation.calls == 1 and tx.translation
    assert tx.state is ProcessingState.COMPLETE
    assert tx.signal_metadata["speech_check"]["verdict"] == "uncertain"
    assert tx.needs_review


def test_the_held_note_names_the_loop(qt_app):
    from babelfishr.models import Transmission, utcnow
    from babelfishr.pipeline import assess_speech
    from babelfishr.ui.timeline import _speech_check_note

    loop = FIELD_LOOP_SHAPE
    tx = Transmission(session_id="s", started_at=utcnow(), transcript=loop,
                      state=ProcessingState.COMPLETE,
                      signal_metadata={"speech_check": assess_speech(
                          result(loop, confidence=0.933, language="nn",
                                 language_confidence=0.531))})
    note = _speech_check_note(tx)
    assert note.startswith("Probably not speech (the transcript looks like a recognition loop")
    assert "()" not in note and "Retry as speech in" in note
    call = "mayday mayday mayday mayday mayday mayday"
    tx.transcript = call
    tx.signal_metadata = {"speech_check": assess_speech(result(call, confidence=0.93))}
    assert _speech_check_note(tx).startswith("Speech uncertain (the transcript repeats itself")
