"""Background noise must not produce a transcript reading "Thanks for watching".

Recognisers trained on subtitled video turn room noise into stock sign-offs
("Thanks for watching!", "Takk for at du så på.", "Please subscribe",
subtitle credits). Each is judged against the local speech evidence, never
by the words alone: a sign-off is withheld unless the voice detector and the
engine show clear speech, and a bare "thank you" only when the evidence says
nothing was spoken - so short, faint real radio replies stay. A withheld
guess is shown as "Audio unintelligible", is not translated, is not found by
its words in search, and is labelled in exports; the WAV stays playable and
the operator can review the raw guess and correct it or retry it as speech.
Older records are judged the same way when shown, with nothing rewritten.

Synthetic audio and scripted engines only.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import pathlib

import pytest

from babelfishr.export import to_csv, to_json, to_markdown
from babelfishr.models import (ProcessingState, Session, Transmission,
                               signoff_is_noise, stock_signoff)
from babelfishr.pipeline import assess_speech

from test_live_audio_fixes import PairTranslation, ScriptedTranscription, result, run_one

T0 = dt.datetime(2026, 5, 1, 9, 0, tzinfo=dt.timezone.utc)


# --- which texts count, and the evidence ---------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("Thanks for watching!", "signoff"),
    ("Thank you so much for watching.", "signoff"),
    ("Takk for at du så på.", "signoff"),
    ("Tusen takk for at dere så på!", "signoff"),
    ("Please like and subscribe.", "signoff"),
    ("Subtitles by the Amara.org community", "signoff"),
    ("ご視聴ありがとうございました", "signoff"),
    ("Thanks for watching! Bye.", "signoff"),
    ("Thank you.", "thanks"),
    ("Takk.", "thanks"),
    # Real messages that merely contain the words: never matched.
    ("Thank you, base, copy that, over.", ""),
    ("Unit four watching the north gate, thanks, out.", ""),
    ("Thanks for watching the perimeter tonight, relief arrives at oh six hundred", ""),
    ("Mayday mayday mayday", ""),
])
def test_only_whole_stock_phrases_count(text, kind):
    assert stock_signoff(text) == kind


@pytest.mark.parametrize("kwargs,verdict", [
    # Room noise the voice detector partly took for voice: the classic case.
    (dict(text="Thanks for watching!", ratio=0.15, nsp=0.35, confidence=0.7), "likely_noise"),
    (dict(text="Takk for at du så på.", ratio=0.2, nsp=0.1, confidence=0.55,
          language="no"), "likely_noise"),
    (dict(text="Please subscribe.", nsp=0.2, confidence=0.8), "likely_noise"),   # engine only
    # The Mac bench's generated hiss through Large v3: zero voice, no-speech
    # 0.588, confidence 0.508 - Build 41 called it only "uncertain".
    (dict(text="Thank you for watching.", ratio=0.0, seconds=0.0, nsp=0.588,
          confidence=0.508), "likely_noise"),
    # A sign-off genuinely spoken: clear speech, confident engine - kept.
    (dict(text="Thanks for watching!", ratio=0.6, nsp=0.05, confidence=0.9), "speech"),
    # A short real "thank you" over the radio: kept.
    (dict(text="Thank you.", ratio=0.15, nsp=0.1, confidence=0.8), "speech"),
    (dict(text="Takk.", ratio=0.2, nsp=0.2, confidence=0.6, language="no"), "speech"),
    # A bare thanks with no voice at all, or a doubtful engine: withheld.
    (dict(text="Thank you.", ratio=0.02, nsp=0.2, confidence=0.9), "likely_noise"),
    (dict(text="Thank you.", ratio=0.3, nsp=0.5, confidence=0.9), "likely_noise"),
    # Ordinary words are judged as before (faint but real speech).
    (dict(text="Equipo en posición.", ratio=0.15, nsp=0.35, confidence=0.7,
          language="es"), "speech"),
])
def test_stock_phrases_are_judged_by_the_evidence(kwargs, verdict):
    check = assess_speech(result(**kwargs))
    assert check["verdict"] == verdict, check
    if verdict == "likely_noise" and stock_signoff(kwargs["text"]):
        assert any("stock sign-off" in r for r in check["reasons"])


def test_without_any_evidence_a_signoff_is_withheld_and_a_thanks_kept():
    assert signoff_is_noise("signoff")
    assert not signoff_is_noise("thanks")


# --- through the real pipeline -----------------------------------------------------------

def test_a_noise_signoff_is_withheld_not_translated_and_its_audio_kept(config, store,
                                                                      tmp_path):
    translation = PairTranslation(pairs=(("en", "de"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Thanks for watching!", ratio=0.15,
                                                   nsp=0.35, confidence=0.7)),
                      translation, target_language="de")
    assert tx.transcript == "Thanks for watching!", "the raw guess is kept for review"
    assert tx.guess_withheld and tx.shown_transcript == ""
    assert tx.translation == "" and translation.calls == 0, "a guess was translated"
    assert tx.state is ProcessingState.COMPLETE and tx.needs_review
    assert tx.audio_path and pathlib.Path(tx.audio_path).exists()
    assert tx.id in {t.id for t in store.review_queue()}


def test_a_genuinely_spoken_signoff_is_traffic(config, store, tmp_path):
    translation = PairTranslation(pairs=(("en", "de"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Thanks for watching!", ratio=0.6,
                                                   nsp=0.05, confidence=0.9)),
                      translation, target_language="de")
    assert not tx.guess_withheld and tx.shown_transcript == "Thanks for watching!"
    assert tx.translation and translation.calls == 1


# --- older records, judged when shown, nothing rewritten -------------------------------

def _old(**fields):
    base = dict(session_id="s", started_at=T0, duration=2.0,
                state=ProcessingState.COMPLETE, transcript="Takk for at du så på.",
                translation="Thanks for watching.", source_language="no",
                target_language="en")
    base.update(fields)
    return Transmission(**base)


def test_a_previous_builds_noise_records_are_withheld_on_display():
    tagged = _old(signal_metadata={"speech_check": {"verdict": "likely_noise"}})
    passed = _old(transcript_confidence=0.5,              # tagged "speech" then
                  signal_metadata={"speech_check": {"verdict": "speech",
                                                    "speech_seconds": 0.6,
                                                    "no_speech_probability": 0.2}})
    bare = _old(signal_metadata={})                       # no evidence stored at all
    for tx in (tagged, passed, bare):
        assert tx.guess_withheld
        assert tx.shown_transcript == "" and tx.shown_translation == ""
        assert tx.transcript == "Takk for at du så på.", "nothing was rewritten"
        assert tx.needs_review


def test_corrections_confirmations_and_genuine_speech_take_precedence():
    corrected = _old(transcript_correction="Takk, over.",
                     signal_metadata={"speech_check": {"verdict": "likely_noise"}})
    confirmed = _old(signal_metadata={"speech_check": {"verdict": "likely_noise"},
                                      "speech_confirmed": {"source_language": "no"}})
    genuine = _old(transcript_confidence=0.92,
                   signal_metadata={"speech_check": {"verdict": "speech",
                                                     "speech_seconds": 2.4,
                                                     "no_speech_probability": 0.03}})
    ordinary = _old(transcript="Bil to, kom inn.", signal_metadata={})
    for tx in (corrected, confirmed, genuine, ordinary):
        assert not tx.guess_withheld, tx.transcript
    assert corrected.shown_transcript == "Takk, over."


# --- exports -----------------------------------------------------------------------------

def test_exports_label_a_withheld_guess_and_never_present_it_as_traffic():
    held = _old(id="held", signal_metadata={"speech_check": {"verdict": "likely_noise"}})
    real = _old(id="real", transcript="Bil to, kom inn.", translation="Car two, come in.",
                signal_metadata={})
    md = to_markdown([held, real])
    assert "**Original (no):** Takk for at du så på." not in md
    assert "Audio unintelligible" in md and "Engine guess withheld" in md
    assert "**Translated (en):** Thanks for watching." not in md
    assert "**Original (no):** Bil to, kom inn." in md
    rows = {r["id"]: r for r in csv.DictReader(io.StringIO(to_csv([held, real])))}
    assert rows["held"]["transcript"] == "" and rows["held"]["translation"] == ""
    assert rows["held"]["withheld_guess"] == "Takk for at du så på."
    assert rows["held"]["transcript_status"].startswith("audio unintelligible")
    assert rows["real"]["transcript"] == "Bil to, kom inn."
    data = {t["id"]: t for t in json.loads(to_json([held, real]))["transmissions"]}
    assert data["held"]["guess_withheld"] is True and data["real"]["guess_withheld"] is False
    assert data["held"]["transcript"] == "Takk for at du så på.", "stored value kept"


# --- search and the window ---------------------------------------------------------------

def _saved(app, store, tx_id, **fields):
    store.save_session(Session(id="run", conversation_id=app.conversation_id))
    tx = _old(id=tx_id, session_id="run", **fields)
    store.save_transmission(tx)
    return tx


def test_search_does_not_find_a_withheld_guess_by_its_words(app, store):
    _saved(app, store, "held", signal_metadata={"speech_check": {"verdict": "likely_noise"}})
    _saved(app, store, "noted", notes="watching the harbour",
           signal_metadata={"speech_check": {"verdict": "likely_noise"}})
    _saved(app, store, "real", transcript="Takk, vi ser deg ved porten.", signal_metadata={})
    assert {t.id for t in app.search("takk")} == {"real"}
    assert {t.id for t in app.search("harbour")} == {"noted"}, "found by the operator's note"


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_card_says_audio_unintelligible_and_keeps_review_routes(qt_app, tmp_path):
    from babelfishr.ui.timeline import TimelineView

    wav = tmp_path / "noise.wav"
    wav.write_bytes(b"RIFF synthetic")
    view = TimelineView()
    bubble = view.add(_old(audio_path=str(wav),
                           signal_metadata={"speech_check": {"verdict": "likely_noise"}}))
    assert "Takk for at du så på" not in bubble.original_label.text()
    assert "Audio unintelligible" in bubble.original_label.text()
    assert bubble.pairing_label.text() == "Audio unintelligible"
    assert "Takk for at du så på." in bubble.transcript_box.toolTip()   # the raw guess
    assert bubble.translation_box.isHidden(), "a translation of the guess was shown"
    assert bubble.play_button.isEnabled(), "the WAV stays playable"
    labels = {a.text(): a for a in bubble.menu_button.menu().actions() if a.text()}
    assert labels["Retry as speech in..."].isEnabled()
    assert labels["Edit transcript and translation..."].isEnabled()


# --- real words first, then an unsupported sign-off ------------------------------------

def _outro_result(outro_start=10.0, outro_end=15.0, outro_conf=0.8):
    from babelfishr.providers.base import TranscriptionResult
    from babelfishr.models import TranscriptSegment

    text = "The rain is easing. Thanks for watching!"
    return TranscriptionResult(
        text=text, language="en", language_confidence=0.97, confidence=0.85,
        segments=[TranscriptSegment(0.5, 3.0, "The rain is easing.", 0.9),
                  TranscriptSegment(outro_start, outro_end, "Thanks for watching!",
                                    outro_conf)],
        engine="scripted", engine_version="1", no_speech_probability=0.1,
        speech_ratio=0.4, speech_seconds=2.5)


def _run_eight_seconds(config, store, tmp_path, scripted):
    import numpy as np

    from babelfishr.app import BabelFishRApp
    from babelfishr.audio.wavefile import write_wav
    from test_live_audio_fixes import SR, loud_speech, silence

    wav = write_wav(str(tmp_path / "eight.wav"),
                    np.concatenate([silence(0.5, SR), loud_speech(3.0),
                                    silence(4.5, SR)]), SR)          # 8 s
    translation = PairTranslation(pairs=(("en", "de"),))
    app = BabelFishRApp(config=config, store=store)
    app.transcription, app.translation = ScriptedTranscription(scripted), translation
    try:
        app.start_session(replay_path=wav, name="eight", target_language="de")
        app.run_replay()
        (tx,) = app.transmissions()
        return store.get_transmission(tx.id), translation
    finally:
        app.stop_session()


def test_an_outro_past_the_end_of_the_recording_is_withheld_and_the_words_kept(
        config, store, tmp_path):
    tx, translation = _run_eight_seconds(config, store, tmp_path, _outro_result())
    assert tx.transcript == "The rain is easing. Thanks for watching!", "raw kept"
    assert not tx.guess_withheld
    assert tx.shown_transcript == "The rain is easing."
    assert tx.withheld_ending == "Thanks for watching!"
    assert tx.translation and "watching" not in tx.translation.lower()
    assert "Zuschauen" not in tx.translation


@pytest.mark.parametrize("start,end,conf,withheld", [
    (6.5, 7.9, 0.3, True),        # an unvoiced tail inside the audio, engine unsure
    (4.0, 6.0, 0.9, False),       # said by a person, inside the audio, confident
    (7.5, 9.5, 0.8, True),        # runs well past the end of the 8 s recording
])
def test_only_an_unsupported_outro_is_withheld(start, end, conf, withheld):
    from babelfishr.models import TranscriptSegment

    tx = Transmission(duration=8.0, transcript="The rain is easing. Thanks for watching!",
                      state=ProcessingState.COMPLETE,
                      transcript_segments=[TranscriptSegment(0.5, 3.0, "The rain is easing.", 0.9),
                                           TranscriptSegment(start, end,
                                                             "Thanks for watching!", conf)])
    assert bool(tx.withheld_ending) is withheld
    assert tx.shown_transcript == ("The rain is easing." if withheld
                                   else "The rain is easing. Thanks for watching!")


def test_a_legacy_record_is_trimmed_by_its_stored_duration_and_segments():
    legacy = _old(duration=8.0, transcript="Det regner mindre nå. Takk for at du så på.",
                  translation="It is raining less now. Thanks for watching.",
                  signal_metadata={"speech_check": {"verdict": "speech"}},
                  transcript_segments=[
                      {"start": 0.4, "end": 3.1, "text": "Det regner mindre nå.",
                       "confidence": 0.88},
                      {"start": 9.0, "end": 12.0, "text": "Takk for at du så på.",
                       "confidence": 0.7}])
    assert legacy.shown_transcript == "Det regner mindre nå."
    assert legacy.shown_translation == "It is raining less now."
    assert legacy.transcript.endswith("Takk for at du så på."), "nothing rewritten"
    md = to_markdown([legacy])
    assert "Det regner mindre nå." in md and "Withheld from the end" in md
    assert "**Original (no):** Det regner mindre nå. Takk" not in md


def test_search_and_the_card_show_the_words_not_the_outro(app, store, qt_app, tmp_path):
    from babelfishr.models import TranscriptSegment
    from babelfishr.ui.timeline import TimelineView

    wav = tmp_path / "eight.wav"
    wav.write_bytes(b"RIFF synthetic")
    tx = _saved(app, store, "outro", duration=8.0, audio_path=str(wav),
                transcript="The rain is easing. Thanks for watching!", translation="",
                source_language="en", signal_metadata={},
                transcript_segments=[TranscriptSegment(0.5, 3.0, "The rain is easing.", 0.9),
                                     TranscriptSegment(10.0, 15.0, "Thanks for watching!", 0.8)])
    assert {t.id for t in app.search("rain")} == {"outro"}
    assert {t.id for t in app.search("watching")} == set()
    bubble = TimelineView().add(store.get_transmission(tx.id))
    assert bubble.original_label.text() == "The rain is easing."
    assert "Thanks for watching!" in bubble.transcript_box.toolTip()
    assert bubble.pairing_label.text() == "Audio + transcript ✓"
