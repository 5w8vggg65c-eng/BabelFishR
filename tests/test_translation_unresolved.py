"""An uncertain automatic language guess with no installed pack.

Field case: 50.2 s of mostly room noise, 0.54 s of it heard as speech,
recognised as repeated text and guessed as Norwegian Nynorsk ('nn'). No
nn->en pack is installed. The message used to end FAILED with a
"language uncertain" error, which reads as a breakage and sits beside real
provider failures. It is now COMPLETE with no translation, a
``translation_unresolved`` marker that says translation was not attempted
and why, and a review flag (Transmission.needs_review and
Store.review_queue). The marker survives a restart and an export, and a
retry in a chosen language clears it.

Real failures stay FAILED: a provider error, a broken engine, and a missing
pack for a confident or operator-specified language. Weak but real speech
and a supported low-confidence pair are still translated.

Synthetic audio and scripted engines only; no private recording is used.
"""

from __future__ import annotations

import csv
import io
import json
import os
import pathlib

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.export import to_csv, to_json
from babelfishr.models import ProcessingState, SourceLanguageMode
from babelfishr.providers import EngineError
from babelfishr.storage import Store

from test_live_audio_fixes import (PairTranslation, ScriptedTranscription,
                                   _retry, result, run_one)

MARKER = "translation_unresolved"


def field_case(**overrides):
    """The shape of the field recording: long, little speech, repeated text."""
    values = dict(text="Takk for at du så på. Takk for at du så på.",
                  ratio=0.54 / 50.2, seconds=0.54, duration=50.2, nsp=0.3,
                  language="nn", language_confidence=0.52)
    values.update(overrides)
    return result(**values)


def queued(store):
    return {t.id for t in store.review_queue()}


def test_the_field_case_is_not_attempted_rather_than_failed(config, store, tmp_path):
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      translation, target_language="en")
    assert tx.state is ProcessingState.COMPLETE
    assert tx.error is None, "nothing broke: no FAILED error"
    assert translation.calls == 0 and tx.translation == ""
    # Audio and raw recognition are kept as they were.
    assert tx.transcript == "Takk for at du så på. Takk for at du så på."
    assert tx.source_language == "nn"
    assert tx.audio_path and pathlib.Path(tx.audio_path).exists()
    unresolved = tx.signal_metadata[MARKER]
    assert unresolved["reason"] == "language-uncertain"
    assert unresolved["guessed_language"] == "nn"
    assert unresolved["target_language"] == "en"
    assert unresolved["language_confidence"] == pytest.approx(0.52)
    assert "Translation not attempted" in unresolved["message"]
    assert tx.needs_review and tx.id in queued(store)


def test_the_marker_alone_drives_the_review_flag_and_queue(config, store, tmp_path):
    """Speech check says speech and the guess is 0.70 (above the 0.6 review
    threshold): without the marker nothing would flag this message."""
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Thanks for watching.", ratio=0.5,
                                                   nsp=0.1, language="nn",
                                                   language_confidence=0.70)),
                      PairTranslation(pairs=()), target_language="en")
    assert tx.signal_metadata["speech_check"]["verdict"] == "speech"
    assert MARKER in tx.signal_metadata
    assert tx.needs_review and tx.id in queued(store)
    # The same row without the marker is not flagged by either path.
    tx.signal_metadata = {k: v for k, v in tx.signal_metadata.items() if k != MARKER}
    store.save_transmission(tx)
    assert not store.get_transmission(tx.id).needs_review
    assert tx.id not in queued(store)


def test_it_stays_visible_after_a_restart_and_in_exports(config, store, tmp_path):
    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      PairTranslation(pairs=()), target_language="en")
    store.close()
    reopened = Store(config.database, recordings_dir=config.recording.directory)
    try:
        again = reopened.get_transmission(tx.id)
        assert again.signal_metadata[MARKER]["reason"] == "language-uncertain"
        assert again.needs_review and tx.id in queued(reopened)
        exported = json.loads(to_json([again]))["transmissions"][0]
        assert exported["needs_review"] is True
        assert exported["signal_metadata"][MARKER]["guessed_language"] == "nn"
        assert exported["state"] == "complete" and exported["error"] is None
        row = next(csv.DictReader(io.StringIO(to_csv([again]))))
        assert row["needs_review"] == "True"
        assert row["translation_status"] == "not attempted: language unresolved"
        assert row["state"] == "complete"
    finally:
        reopened.close()


def test_a_csv_row_without_the_marker_has_an_empty_status(config, store, tmp_path):
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Equipo en posición.", ratio=0.5,
                                                   nsp=0.1, language="es")),
                      PairTranslation(pairs=(("es", "en"),)), target_language="en")
    row = next(csv.DictReader(io.StringIO(to_csv([tx]))))
    assert row["translation_status"] == "" and row["translation"]


# --- transitions after a per-message retry --------------------------------

def test_a_retry_in_a_language_with_a_pack_clears_it_and_translates(
        config, store, tmp_path):
    transcription = ScriptedTranscription(result("Equipo en posición.", ratio=0.5,
                                                 nsp=0.1, language="nn",
                                                 language_confidence=0.70))
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path, transcription, translation,
                      target_language="en")
    assert MARKER in tx.signal_metadata and tx.id in queued(store)

    _retry(config, store, transcription, translation, tx.id, source_language="es")
    tx = store.get_transmission(tx.id)
    assert tx.source_language == "es"
    assert tx.source_language_mode is SourceLanguageMode.SPECIFIED
    assert tx.state is ProcessingState.COMPLETE and tx.error is None
    assert tx.translation and translation.calls == 1
    assert MARKER not in tx.signal_metadata
    assert not tx.needs_review and tx.id not in queued(store)


def test_a_retry_in_english_clears_it_without_translating(config, store, tmp_path):
    transcription = ScriptedTranscription(field_case())
    translation = PairTranslation(pairs=())
    app, tx = run_one(config, store, tmp_path, transcription, translation,
                      target_language="en")
    _retry(config, store, transcription, translation, tx.id, source_language="en")
    tx = store.get_transmission(tx.id)
    assert tx.state is ProcessingState.COMPLETE and tx.error is None
    assert MARKER not in tx.signal_metadata
    assert tx.translation == "" and translation.calls == 0


def test_a_retry_in_a_language_without_a_pack_is_a_real_failure(
        config, store, tmp_path):
    """The operator named the language: the pack is genuinely missing."""
    transcription = ScriptedTranscription(field_case())
    translation = PairTranslation(pairs=())
    app, tx = run_one(config, store, tmp_path, transcription, translation,
                      target_language="en")
    _retry(config, store, transcription, translation, tx.id, source_language="nb")
    tx = store.get_transmission(tx.id)
    assert tx.state is ProcessingState.FAILED and tx.error.stage == "translation"
    assert "no nb->en language pack is installed" in tx.error.message
    assert MARKER not in tx.signal_metadata
    assert tx.id in queued(store)


def test_a_plain_retry_reaches_the_same_honest_state(config, store, tmp_path):
    transcription = ScriptedTranscription(field_case())
    translation = PairTranslation(pairs=())
    app, tx = run_one(config, store, tmp_path, transcription, translation,
                      target_language="en")
    _retry(config, store, transcription, translation, tx.id)
    tx = store.get_transmission(tx.id)
    assert tx.state is ProcessingState.COMPLETE and tx.error is None
    assert tx.signal_metadata[MARKER]["reason"] == "language-uncertain"
    assert tx.needs_review and tx.id in queued(store)


def test_saving_the_words_clears_the_review_flag(config, store, tmp_path):
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Thanks for watching.", ratio=0.5,
                                                   nsp=0.1, language="nn",
                                                   language_confidence=0.70)),
                      PairTranslation(pairs=()), target_language="en")
    BabelFishRApp(config=config, store=store).correct(
        tx.id, transcript="Thanks for watching.", translation="Thanks for watching.")
    tx = store.get_transmission(tx.id)
    assert MARKER in tx.signal_metadata, "kept on record"
    assert not tx.needs_review and tx.id not in queued(store)


# --- what is not affected ---------------------------------------------------

class FailingTranslation(PairTranslation):
    def translate(self, *args, **kwargs):
        self.calls += 1
        raise EngineError("argos: the model file could not be read")


def test_a_provider_error_is_still_failed(config, store, tmp_path):
    translation = FailingTranslation(pairs=(("nn", "en"),))
    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      translation, target_language="en")
    assert translation.calls == 1
    assert tx.state is ProcessingState.FAILED and tx.error.stage == "translation"
    assert "could not be read" in tx.error.message
    assert MARKER not in tx.signal_metadata


class BrokenEngine(PairTranslation):
    def available(self):
        return False

    def unavailable_reason(self):
        return "argostranslate is not importable"


def test_a_broken_engine_is_still_failed_with_its_own_reason(config, store, tmp_path):
    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      BrokenEngine(pairs=()), target_language="en")
    assert tx.state is ProcessingState.FAILED
    assert "argostranslate is not importable" in tx.error.message
    assert MARKER not in tx.signal_metadata


def test_a_confident_guess_without_a_pack_is_still_failed(config, store, tmp_path):
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(field_case(language="nb",
                                                       language_confidence=0.97)),
                      PairTranslation(pairs=()), target_language="en")
    assert tx.state is ProcessingState.FAILED
    assert "no nb->en language pack is installed" in tx.error.message
    assert MARKER not in tx.signal_metadata


def test_a_supported_low_confidence_pair_is_still_translated(config, store, tmp_path):
    translation = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Equipo en posición.", ratio=0.5,
                                                   nsp=0.1, language="es",
                                                   language_confidence=0.31)),
                      translation, target_language="en")
    assert tx.translation and translation.calls == 1
    assert tx.state is ProcessingState.COMPLETE and MARKER not in tx.signal_metadata
    assert tx.needs_review  # 0.31 is below the review threshold, as before


def test_weak_real_speech_with_a_pack_is_still_translated(config, store, tmp_path):
    """Same 0.54 s of speech in 50 s, uncertain guess, but the pair exists."""
    translation = PairTranslation(pairs=(("nn", "en"),))
    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      translation, target_language="en")
    assert tx.translation and translation.calls == 1
    assert MARKER not in tx.signal_metadata


# --- the message view ---------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_bubble_says_translation_was_not_attempted(qt_app, config, store, tmp_path):
    from babelfishr.ui.playback import PlaybackController, SystemOpenBackend
    from babelfishr.ui.timeline import TransmissionBubble

    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      PairTranslation(pairs=()), target_language="en")
    bubble = TransmissionBubble(tx, PlaybackController(SystemOpenBackend()))
    # Not spelled out on the card any more: the detail is on inspection
    # (the status tooltip), the REVIEW column still says CHECK.
    assert bubble.notes_label.isHidden()
    tip = bubble.pairing_label.toolTip()
    assert "Translation not attempted: language unresolved" in tip
    assert "'nn' (52%)" in tip and "Transcribe as speech\u2026" in tip
    assert "Translation error" not in bubble.pairing_label.text()
    assert "Transcription error" not in bubble.pairing_label.text()
    assert bubble.review_label.text() == "CHECK"
    assert bubble.error_label.isHidden(), "not shown as a failure"


# --- the readable log and the session bundle ---------------------------------

def test_markdown_explains_the_unattempted_translation(config, store, tmp_path):
    from babelfishr.export import to_markdown

    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      PairTranslation(pairs=()), target_language="en")
    text = to_markdown([tx])
    assert "**Translation not attempted (language unresolved):**" in text
    assert "'nn' (52%)" in text and "no nn->en pack is installed" in text
    assert "**Review:** needs review" in text
    assert "**Error" not in text, "not reported as a failure"


def test_markdown_for_an_ordinary_translated_row_is_unchanged(config, store, tmp_path):
    from babelfishr.export import to_markdown

    app, tx = run_one(config, store, tmp_path,
                      ScriptedTranscription(result("Equipo en posición.", ratio=0.5,
                                                   nsp=0.1, language="es")),
                      PairTranslation(pairs=(("es", "en"),)), target_language="en")
    text = to_markdown([tx])
    assert "not attempted" not in text and "**Review:**" not in text


def test_the_session_bundle_carries_it_and_leaves_the_audio_untouched(
        config, store, tmp_path):
    import hashlib

    from babelfishr.export import export_session

    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      PairTranslation(pairs=()), target_language="en")
    original = pathlib.Path(tx.audio_path)
    before = (hashlib.sha256(original.read_bytes()).hexdigest(),
              original.stat().st_mtime_ns)
    bundle = pathlib.Path(export_session(store, tx.session_id,
                                         str(tmp_path / "bundle")))
    markdown = next(bundle.rglob("transcript.md")).read_text(encoding="utf-8")
    assert "Translation not attempted (language unresolved)" in markdown
    assert "**Review:** needs review" in markdown
    copied = next(bundle.rglob(f"{tx.id}.wav"))
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == before[0]
    assert (hashlib.sha256(original.read_bytes()).hexdigest(),
            original.stat().st_mtime_ns) == before, "the recording was changed"
    assert store.get_transmission(tx.id).audio_path == tx.audio_path


def test_the_note_names_no_platform_shortcut(qt_app, config, store, tmp_path):
    from babelfishr.ui.timeline import _translation_unresolved_note

    app, tx = run_one(config, store, tmp_path, ScriptedTranscription(field_case()),
                      PairTranslation(pairs=()), target_language="en")
    note = _translation_unresolved_note(tx)
    assert "Transcribe as speech\u2026" in note
    assert "Ctrl" not in note and "⌘" not in note
