"""Transcript cards: one short status, and the audio + transcript check.

The operator asked for the yellow notes and routine chips to go ("not
providing any useful or additional information") and for CLIPPED to be
replaced by a check that the transmission's ORIGINAL recording and its
transcript are both there. The card now shows one compact status line:

* ``Audio + transcript ✓`` when both are present;
* ``Audio missing`` / ``Transcript missing`` / ``Audio and transcript
  missing``, from the file on disk (not a stored path) and the
  original-language text (an operator correction counts, a translation
  alone does not, whitespace is nothing);
* ``Transcript pending`` while recognition is still to come, and ``Audio
  cleared`` / ``Transcript cleared`` after Clear Session History;
* one short reason only when there is one: ``Audio unintelligible``,
  ``Transcription error``, or ``Translation error`` (never reported as a
  recognition failure).

The technical detail stays available on deliberate inspection (the status
line's tooltip). Recorded values, search and exports are unchanged.
Synthetic rows and files only.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from babelfishr.models import ProcessingState, Transmission

CHECK = "Audio + transcript ✓"


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "rec" / "original.wav"
    path.parent.mkdir()
    path.write_bytes(b"RIFF synthetic")
    return str(path)


def card(qt_app, **fields):
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    tx = Transmission(**{"state": ProcessingState.COMPLETE, **fields})
    bubble = view.add(tx)
    card.views.append(view)           # keep the view (and its bubble) alive
    return bubble


card.views = []


def visible_text(bubble):
    from PySide6 import QtWidgets

    return " ".join(label.text() for label in bubble.findChildren(QtWidgets.QLabel)
                    if label.isVisibleTo(bubble))


# --- concise status --------------------------------------------------------------

def test_an_ordinary_message_has_the_check_and_nothing_else(qt_app, wav):
    bubble = card(qt_app, transcript="radio check", translation="Funkprobe",
                  source_language="en", target_language="de", audio_path=wav,
                  transcript_confidence=0.95, clipped=True)
    assert bubble.pairing_label.text() == CHECK
    assert bubble.pairing_label.property("ok") is True
    assert bubble.pairing_label.accessibleName() == f"Status: {CHECK}"
    text = visible_text(bubble)
    for gone in ("CLIPPED", "clipped", "confidence", "unclassified", "Probably",
                 "continuous audio"):
        assert gone not in text, gone
    assert bubble.notes_label.isHidden() and bubble.error_label.isHidden()
    assert bubble.chip_box.isHidden()


def test_low_confidence_but_readable_speech_gets_no_warning(qt_app, wav):
    bubble = card(qt_app, transcript="maybe a call sign", audio_path=wav,
                  transcript_confidence=0.31,
                  signal_metadata={"speech_check": {"verdict": "uncertain",
                                                    "speech_ratio": 0.3}})
    assert bubble.pairing_label.text() == CHECK
    assert "Speech uncertain" not in visible_text(bubble)
    assert "Speech uncertain" in bubble.pairing_label.toolTip()   # on inspection


@pytest.mark.parametrize("fields", [
    dict(state=ProcessingState.SKIPPED, skip_reason="Classified as noise."),
    dict(transcript="thank you for watching",
         signal_metadata={"speech_check": {"verdict": "likely_noise",
                                           "speech_ratio": 0.0, "speech_seconds": 0.0}}),
    dict(transcript="   "),                           # recognised to nothing
], ids=["skipped-as-noise", "held-as-invented", "empty"])
def test_noise_says_audio_unintelligible_once(qt_app, wav, fields):
    bubble = card(qt_app, audio_path=wav, **fields)
    status = bubble.pairing_label.text()
    assert status.endswith("Audio unintelligible") and status.count("·") <= 1
    text = visible_text(bubble)
    assert "Probably not speech" not in text and "local voice detector" not in text
    assert bubble.pairing_label.property("ok") is False


def test_a_recognition_failure_says_transcription_error(qt_app, wav):
    tx = Transmission(audio_path=wav)
    tx.fail("transcription", "model crashed: out of memory")
    bubble = card(qt_app, **{k: getattr(tx, k) for k in ("audio_path", "state", "error")})
    assert bubble.pairing_label.text() == "Transcript missing · Transcription error"
    assert "out of memory" in bubble.pairing_label.toolTip()
    assert "out of memory" not in visible_text(bubble)
    assert bubble.retry_button.isVisibleTo(bubble)


def test_a_translation_only_failure_is_not_called_a_recognition_failure(qt_app, wav):
    tx = Transmission(audio_path=wav, transcript="hola", source_language="es")
    tx.fail("translation", "no es->en pack")
    bubble = card(qt_app, **{k: getattr(tx, k) for k in
                             ("audio_path", "state", "error", "transcript",
                              "source_language")})
    assert bubble.pairing_label.text() == f"{CHECK} · Translation error"
    assert "Transcription error" not in bubble.pairing_label.text()
    assert bubble.original_label.text() == "hola"


def test_a_long_capture_continuation_has_no_part_chips(qt_app, wav):
    for segment in ({"continued": True}, {"continues_previous": True, "continued": True},
                    {"continues_previous": True}):
        bubble = card(qt_app, transcript="long net traffic", audio_path=wav,
                      signal_metadata={"segment": segment})
        assert bubble.pairing_label.text() == CHECK
        assert "continuous audio" not in visible_text(bubble)


def test_operator_marks_notes_and_bookmarks_stay(qt_app, wav):
    bubble = card(qt_app, transcript="net check", audio_path=wav, tags=["net"],
                  bookmarked=True, notes="relay to base")
    chips = [c.text() for c in bubble.chip_box.findChildren(type(bubble.header))]
    assert "#net" in chips and "bookmarked" in chips
    assert bubble.notes_label.text() == "Note: relay to base"


# --- the audio + transcript check: a crossing matrix -------------------------------

@pytest.mark.parametrize("audio,text,expected", [
    (True, True, CHECK),
    (False, True, "Audio missing"),
    (True, False, "Transcript missing"),
    (False, False, "Audio and transcript missing"),
])
def test_all_four_pairs(qt_app, wav, tmp_path, audio, text, expected):
    path = wav if audio else str(tmp_path / "gone.wav")      # a stored path, no file
    bubble = card(qt_app, audio_path=path, transcript="words" if text else "",
                  state=ProcessingState.COMPLETE if text else ProcessingState.FAILED)
    status = bubble.pairing_label.text().split(" · ")[0]
    assert status == expected
    assert bubble.play_button.isEnabled() is audio
    assert bubble.play_action.isEnabled() is audio
    assert bubble.retry_action.isEnabled() is audio


def test_whitespace_is_no_transcript_and_a_correction_counts(qt_app, wav):
    from babelfishr.ui.timeline import pairing_status

    assert pairing_status(Transmission(audio_path=wav, transcript=" \n\t ",
                                       state=ProcessingState.FAILED))[0] \
        == "Transcript missing"
    assert pairing_status(Transmission(audio_path=wav, transcript="",
                                       transcript_correction="operator words"))[0] == CHECK


def test_a_translation_alone_or_a_derived_copy_does_not_count(qt_app, wav, tmp_path):
    from babelfishr.ui.timeline import pairing_status

    only_translation = Transmission(audio_path=wav, translation="hello",
                                    state=ProcessingState.FAILED)
    assert pairing_status(only_translation)[0] == "Transcript missing"
    copy_only = Transmission(audio_path=str(tmp_path / "original-gone.wav"),
                             processed_audio_path=wav, transcript="words")
    assert pairing_status(copy_only)[0] == "Audio missing"
    folder = Transmission(audio_path=str(tmp_path), transcript="words")   # not a file
    assert pairing_status(folder)[0] == "Audio missing"


def test_pending_then_ready(qt_app, wav):
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    tx = Transmission(audio_path=wav, state=ProcessingState.TRANSCRIBING)
    bubble = view.add(tx)
    assert bubble.pairing_label.text() == "Transcript pending"
    tx.transcript, tx.state = "ready now", ProcessingState.COMPLETE
    view.update(tx)
    assert bubble.pairing_label.text() == CHECK


def test_stopped_before_processing_is_missing_not_pending(qt_app, wav):
    from babelfishr.ui.timeline import pairing_status

    tx = Transmission(audio_path=wav, state=ProcessingState.CAPTURED,
                      signal_metadata={"processing_stopped": {"reason": "stop"}})
    assert pairing_status(tx)[0] == "Transcript missing"


def test_cleared_says_cleared(qt_app):
    from babelfishr.ui.timeline import pairing_status

    tx = Transmission(audio_path=None, transcript="words",
                      signal_metadata={"history_cleared": {"audio": "t"}})
    assert pairing_status(tx)[0] == "Audio cleared"
    tx = Transmission(audio_path=None, transcript="",
                      signal_metadata={"history_cleared": {"audio": "t", "text": "t"}})
    assert pairing_status(tx)[0] == "Audio and transcript cleared"


# --- removed and restored outside the app, seen in the window --------------------------

def test_external_removal_and_restoration_show_on_selection_and_refresh(qt_app, config,
                                                                         store, wav):
    from babelfishr.app import BabelFishRApp
    from babelfishr.models import Session
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    store.save_session(Session(id="r1", conversation_id=app.conversation_id))
    tx = Transmission(session_id="r1", audio_path=wav, transcript="words",
                      state=ProcessingState.COMPLETE, notes="kept note")
    store.save_transmission(tx)
    window = MainWindow(app)
    try:
        window._reload_timeline()
        bubble = window.timeline.bubble(tx.id)
        assert bubble.pairing_label.text() == CHECK
        moved = pathlib.Path(wav).with_suffix(".moved")
        os.rename(wav, moved)                               # removed outside the app
        window.timeline.select(tx.id)                       # looking at it re-checks
        assert bubble.pairing_label.text() == "Audio missing"
        assert not bubble.play_button.isEnabled()
        assert not window.selected_play_button.isEnabled()
        assert "missing" in window.selected_hint.text()
        assert bubble.notes_label.text() == "Note: kept note"
        assert bubble.original_label.text() == "words"
        os.rename(moved, wav)                               # restored
        window._refresh_current_view()                      # View > Refresh
        bubble = window.timeline.bubble(tx.id)
        assert bubble.pairing_label.text() == CHECK
        assert bubble.play_button.isEnabled()
        # Recorded values are untouched by any of this.
        assert store.get_transmission(tx.id).audio_path == wav
    finally:
        window.close()
        app.close()
