"""The review list's message layout: the transcript in its own full-width,
captioned box, the translation in a second box under it, compact metadata
outside both, and a selection that tints and outlines rather than fills.

Real widgets, offscreen, measured after the layout has run. Synthetic text
and synthetic WAVs only."""

from __future__ import annotations

import datetime as _dt
import os
import time

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from babelfishr.models import ProcessingState, Session, Transmission, utcnow  # noqa: E402

LONG_ENGLISH = ("Sample text for the layout: net control to all stations, this "
                "is a routine check of the repeater, please acknowledge in turn "
                "and keep transmissions short. ") * 4
ARABIC = ("هذا نص تجريبي. إلى جميع المحطات، هنا مركز التحكم. نقوم بالفحص "
          "الدوري لمكرر الإشارة، يرجى الرد بالترتيب. ") * 3
JAPANESE = ("これはサンプルの文章です。全局へ、こちらは管制局です。中継器の定時点検を"
            "行っています。順番に応答してください。") * 3
GEORGIAN = ("ეს არის სანიმუშო ტექსტი. ყველა სადგურს, აქ არის მართვის "
            "ცენტრი. გთხოვთ, უპასუხოთ რიგრიგობით. ") * 3


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds: int = 12):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.002)


def _message(session_id, index, start, tmp_path, **fields):
    from babelfishr.audio.wavefile import write_wav
    from babelfishr.testing import speech_like

    base = dict(session_id=session_id,
                started_at=start + _dt.timedelta(minutes=index),
                duration=1.5, sample_rate=48_000, source_language="en",
                target_language="en", state=ProcessingState.COMPLETE,
                transcript=f"Sample message {index}.")
    base.update(fields)
    tx = Transmission(**base)
    tx.ended_at = tx.started_at + _dt.timedelta(seconds=tx.duration)
    path = tmp_path / f"layout_{index}.wav"
    write_wav(str(path), speech_like(1.5, 48_000), 48_000)
    tx.audio_path = str(path)
    return tx


@pytest.fixture
def window_with(qt_app, config, store, tmp_path):
    """A real MainWindow on a Session holding the given messages."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    opened = []

    def build(specs):
        conversation = store.create_conversation("Layout")
        session = Session(name="Layout", conversation_id=conversation.id,
                          sample_rate=48_000, target_language="en")
        store.save_session(session)
        start = utcnow() - _dt.timedelta(minutes=30)
        made = []
        for index, fields in enumerate(specs):
            tx = _message(session.id, index, start, tmp_path, **fields)
            store.save_transmission(tx)
            made.append(tx)
        app = BabelFishRApp(config=config, store=store)
        app.select_conversation(conversation.id)
        window = MainWindow(app)
        window._reload_timeline()
        window.resize(1200, 900)
        window.show()
        pump(qt_app)
        opened.append(window)
        return window, made

    yield build
    for window in opened:
        window.timeline.playback.stop()
        window.waveform.clear()
        window.close()
    pump(qt_app)


def _resize(qt_app, window, width):
    window.resize(width, 1000)
    pump(qt_app)
    return window.width()


def _text_area(bubble) -> QtCore.QRect:
    """The MESSAGE column of the row: everything between TIME and REVIEW,
    measured from the widgets the layout placed (row coordinates)."""
    spacing = bubble.layout().horizontalSpacing()
    left = bubble.time_label.geometry().right() + 1 + spacing
    right = bubble.review_label.geometry().left() - spacing
    return QtCore.QRect(left, 0, right - left, bubble.height())


def _inside(inner: QtWidgets.QWidget, outer: QtWidgets.QWidget) -> bool:
    top_left = inner.mapTo(outer, QtCore.QPoint(0, 0))
    return outer.rect().contains(QtCore.QRect(top_left, inner.size()))


def _overlaps(a: QtWidgets.QWidget, b: QtWidgets.QWidget, root) -> bool:
    ra = QtCore.QRect(a.mapTo(root, QtCore.QPoint(0, 0)), a.size())
    rb = QtCore.QRect(b.mapTo(root, QtCore.QPoint(0, 0)), b.size())
    return ra.intersects(rb)


def _chips(bubble):
    return [w for w in bubble.findChildren(QtWidgets.QLabel)
            if w.objectName() == "chip" and w.isVisibleTo(bubble)]


# ---- width ----------------------------------------------------------------------


def test_without_a_translation_the_transcript_spans_the_text_area(qt_app, window_with):
    window, made = window_with([dict(transcript=LONG_ENGLISH, clipped=True)])
    bubble = window.timeline.bubble(made[0].id)
    for width in (2000, window.minimumSizeHint().width()):
        actual = _resize(qt_app, window, width)
        area = _text_area(bubble)
        box = bubble.transcript_box
        assert box.isVisibleTo(bubble)
        assert not bubble.translation_box.isVisibleTo(bubble), (
            "an empty translation box takes space")
        assert box.width() >= 0.85 * area.width(), (actual, box.width(), area.width())
        assert box.geometry().right() <= bubble.width()
        if actual >= 2000:
            # Much more than half of the row: the old equal-stretch layout
            # left the original in one half of the space.
            assert box.width() > 0.6 * bubble.width()
            # The transcript itself wraps at the box's width, not ~500 px.
            assert bubble.original_label.width() >= box.width() - 24


def test_with_a_translation_both_boxes_are_labelled_and_wide(qt_app, window_with):
    window, made = window_with([dict(source_language="fr", transcript="Bonjour à tous.",
                                     translation="Hello everyone.")])
    bubble = window.timeline.bubble(made[0].id)
    _resize(qt_app, window, 2000)
    area = _text_area(bubble)
    for box, caption, word in ((bubble.transcript_box, bubble.transcript_caption,
                                "TRANSCRIPT"),
                               (bubble.translation_box, bubble.translation_caption,
                                "TRANSLATION")):
        assert box.isVisibleTo(bubble)
        assert caption.isVisibleTo(bubble) and word in caption.text()
        assert box.width() >= 0.85 * area.width()
    assert "EN" in bubble.translation_caption.text()
    assert "Hello everyone." in bubble.translated_label.text()
    # Stacked: the translation under the transcript, not beside it.
    assert bubble.translation_box.y() >= bubble.transcript_box.geometry().bottom()


def test_held_and_failed_messages_take_no_empty_translation_box(qt_app, window_with):
    from babelfishr.models import ErrorInfo

    window, made = window_with([
        dict(source_language="it", transcript="Ripeto, nessun movimento.",
             state=ProcessingState.FAILED,
             error=ErrorInfo(stage="translation", message="sample: no pack")),
        dict(source_language="es", transcript="Gracias por ver.",
             signal_metadata={"speech_check": {"verdict": "likely_noise"}}),
    ])
    for tx in made:
        bubble = window.timeline.bubble(tx.id)
        assert bubble.transcript_box.isVisibleTo(bubble)
        assert not bubble.translation_box.isVisibleTo(bubble)


def test_metadata_chips_and_notes_sit_outside_the_transcript_box(qt_app, window_with):
    window, made = window_with([dict(
        transcript=LONG_ENGLISH, clipped=True, transcript_confidence=0.4,
        tags=["sample"], notes="Sample note.",
        signal_metadata={"segment": {"continued": True},
                         "speech_check": {"verdict": "uncertain", "speech_ratio": 0.3,
                                          "speech_seconds": 0.5}})])
    bubble = window.timeline.bubble(made[0].id)
    for width in (2000, window.minimumSizeHint().width()):
        _resize(qt_app, window, width)
        box = bubble.transcript_box
        chips = _chips(bubble)
        texts = [c.text() for c in chips]
        # Operator marks only: no clipped / confidence / continuous chips.
        assert texts == ["#sample"]
        assert "CLIPPED" not in bubble.header.text()
        for widget in [bubble.header, bubble.notes_label, bubble.pairing_label] + chips:
            assert widget.isVisibleTo(bubble), widget.objectName()
            assert not _inside(widget, box)
            assert not _overlaps(widget, box, bubble), widget.objectName()
        # The note stays; the speech-check paragraph does not (it is in the
        # status tooltip, for deliberate inspection).
        assert bubble.notes_label.text() == "Note: Sample note."
        from babelfishr.ui.timeline import _speech_check_note
        assert _speech_check_note(bubble.tx).split(" (")[0] in bubble.pairing_label.toolTip()
        assert "Speech uncertain" not in bubble.notes_label.text()


@pytest.mark.parametrize("text", [LONG_ENGLISH, ARABIC, JAPANESE, GEORGIAN],
                         ids=["english", "arabic", "japanese", "georgian"])
def test_long_text_in_any_script_wraps_without_clipping(qt_app, window_with, text):
    window, made = window_with([dict(transcript="Short."),
                                dict(transcript=text)])
    short = window.timeline.bubble(made[0].id)
    long_ = window.timeline.bubble(made[1].id)
    for width in (2000, window.minimumSizeHint().width()):
        _resize(qt_app, window, width)
        label = long_.original_label
        line = label.fontMetrics().lineSpacing()
        # Taller than one line, as tall as its text needs at this width.
        assert long_.transcript_box.height() > short.transcript_box.height()
        assert label.height() >= label.heightForWidth(label.width()) - 1
        assert label.height() >= 2 * line
        # Nothing runs past the box, and the box stays inside the row.
        assert _inside(label, long_.transcript_box)
        assert long_.transcript_box.geometry().right() <= _text_area(long_).right() + 1
        # The whole text is there.
        assert text.strip()[:20] in label.text()


# ---- selection ---------------------------------------------------------------------


def test_selection_is_an_outline_and_tint_not_a_solid_accent_fill(qt_app, window_with):
    from babelfishr.ui import theme

    window, made = window_with([dict(transcript=LONG_ENGLISH)])
    bubble = window.timeline.bubble(made[0].id)
    _resize(qt_app, window, 1600)
    window.timeline.select(made[0].id)
    pump(qt_app)
    assert bubble.property("selected") is True
    accent = QtGui.QColor(theme.tokens(window)["accent"])
    image = bubble.grab().toImage()
    area = _text_area(bubble)
    total = accent_like = 0
    for x in range(area.left(), area.right(), 3):
        for y in range(area.top(), area.bottom(), 3):
            color = image.pixelColor(x, y)
            total += 1
            if (abs(color.red() - accent.red()) < 24
                    and abs(color.green() - accent.green()) < 24
                    and abs(color.blue() - accent.blue()) < 24):
                accent_like += 1
    assert total and accent_like / total < 0.1, accent_like / total
    # And the transcript keeps its ordinary text colour, not on-accent.
    sheet = window.styleSheet() or QtWidgets.QApplication.instance().styleSheet()
    assert "#bubble[selected=\"true\"] QLabel" not in sheet
    assert bubble.original_label.palette().color(QtGui.QPalette.WindowText) == QtGui.QColor(
        theme.tokens(window)["text"])


def test_selected_review_and_failed_edges_stay_distinguishable():
    from babelfishr.ui import theme

    sheet = theme.stylesheet()
    selected = sheet.index('#bubble[selected="true"] {')
    review = sheet.index('#bubble[review="true"]')
    failed = sheet.index('#bubble[state="failed"]')
    # Equal specificity: the later rule wins, so the amber and red left
    # edges are declared after the selection and still show on a selected row.
    assert selected < review < failed
    block = sheet[selected:sheet.index("}", selected)]
    background = block.split("background:")[1].split(";")[0].strip()
    tokens = theme.tokens()
    assert background == tokens["selected_bg"], background   # the faint tint...
    assert background != tokens["accent"]                     # ...not the solid accent


# ---- held / uncertain ---------------------------------------------------------------


def test_held_and_uncertain_transcripts_say_so_and_show_every_word(qt_app, window_with):
    held_text = "Sample text: thank you for watching, please subscribe."
    window, made = window_with([
        dict(transcript=held_text,
             signal_metadata={"speech_check": {"verdict": "likely_noise",
                                               "speech_ratio": 0.02,
                                               "speech_seconds": 0.1}}),
        dict(transcript="Sample text, maybe speech.",
             signal_metadata={"speech_check": {"verdict": "uncertain"}}),
        dict(transcript="Sample text, ordinary."),
    ])
    held, uncertain, plain = (window.timeline.bubble(tx.id) for tx in made)
    # A guess held as noise is not shown as traffic: the card says Audio
    # unintelligible, and the guess is there on inspection (and in Edit).
    assert held.transcript_caption.text() == "TRANSCRIPT"
    assert held.original_label.isVisibleTo(held)
    assert held_text not in held.original_label.text()
    assert "Audio unintelligible" in held.original_label.text()
    assert held_text in held.transcript_box.toolTip()
    assert held.notes_label.isHidden()
    assert held.pairing_label.text() == "Audio unintelligible"
    assert "Probably not speech" in held.pairing_label.toolTip()
    assert held_text in held.pairing_label.toolTip()

    # Marginal but readable: no warning at all.
    assert uncertain.transcript_caption.text() == "TRANSCRIPT"
    assert "Sample text, maybe speech." in uncertain.original_label.text()
    assert "unintelligible" not in uncertain.pairing_label.text()
    assert "error" not in uncertain.pairing_label.text()

    assert plain.transcript_caption.text() == "TRANSCRIPT"


def test_the_column_header_no_longer_claims_a_half_width_translation(qt_app,
                                                                     window_with):
    window, _ = window_with([dict(transcript="Short.")])
    header = window.findChild(QtWidgets.QFrame, "columnHeader")
    words = " ".join(label.text() for label in header.findChildren(QtWidgets.QLabel))
    assert "TIME" in words and "MESSAGE" in words and "REVIEW" in words
    assert "ORIGINAL" not in words
    # The translation is named as following the transcript, in one column.
    assert window.original_heading.text().endswith("THEN")
    assert window.translation_heading.text().startswith("TRANSLATION")


# ---- existing behaviour, one row ---------------------------------------------------


def test_play_menu_and_click_selection_still_work_for_a_row(qt_app, tmp_path):
    from PySide6.QtTest import QTest

    from test_alpha5_playback import make_scripted_backend, make_view
    from babelfishr.testing import build_fixture

    wav = build_fixture([{"kind": "voice", "duration": 2.0, "level_dbfs": -14}],
                        sample_rate=48_000).write(str(tmp_path / "row.wav"))
    backend = make_scripted_backend()
    backend.durations[wav] = 2000
    view, controller = make_view(backend)
    view.resize(1400, 600)
    view.show()
    first = view.add(Transmission(
        id="one", session_id="s", duration=2.0, audio_path=wav,
        started_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        transcript=LONG_ENGLISH, state=ProcessingState.COMPLETE,
        source_language="en", target_language="en"))
    view.add(Transmission(
        id="two", session_id="s", duration=2.0, audio_path=wav,
        started_at=_dt.datetime(2026, 1, 1, 0, 1, tzinfo=_dt.timezone.utc),
        transcript="Another sample.", state=ProcessingState.COMPLETE,
        source_language="en", target_language="en"))
    pump(qt_app)

    view.select("two")
    QTest.mouseClick(first.original_label, QtCore.Qt.LeftButton)
    pump(qt_app)
    assert view.selected_id == "one", "clicking the transcript did not select the row"

    assert first.play_button.text() == "Play original"
    QTest.mouseClick(first.play_button, QtCore.Qt.LeftButton)
    pump(qt_app)
    assert backend.loaded == wav and controller.owner == "one"

    labels = [a.text() for a in first.menu_button.menu().actions()]
    assert "Retry as speech in..." in labels
    view.close()


# ---- the operator's laptop width -------------------------------------------------

#: The Mac's current window width in logical pixels, as root measured it.
LAPTOP_WIDTH = 1146

BUSY_ROW = dict(
    transcript="Net control to all stations, this is a routine check of the repeater; "
               "please acknowledge in turn and keep transmissions short.",
    clipped=True, transcript_confidence=0.42, tags=["net-control", "repeater"],
    signal_metadata={"segment": {"continued": True, "continues_previous": True},
                     "speech_check": {"verdict": "uncertain", "reasons": [],
                                      "speech_ratio": 0.1, "speech_seconds": 0.2,
                                      "no_speech_probability": 0.3}})


def test_the_window_fits_the_laptop_width_with_every_chip(qt_app, window_with):
    """Resize to the real width, not just minimumSizeHint(): an enlarged
    minimum would hold the window wider than the screen."""
    window, made = window_with([BUSY_ROW, dict(BUSY_ROW, translation="Hola.",
                                               source_language="es")])
    assert _resize(qt_app, window, LAPTOP_WIDTH) == LAPTOP_WIDTH, (
        f"the window will not shrink to {LAPTOP_WIDTH} px")
    viewport = window.timeline.viewport() if hasattr(window.timeline, "viewport") \
        else window.timeline
    scroll = window.timeline.findChildren(QtWidgets.QScrollArea)
    for area in scroll:
        assert not area.horizontalScrollBar().isVisible(), "rows overflow sideways"
    for tx in made:
        bubble = window.timeline.bubble(tx.id)
        assert bubble.isVisible()
        right = bubble.mapTo(window, QtCore.QPoint(bubble.width(), 0)).x()
        assert right <= window.width(), "a row runs past the window"
        chips = _chips(bubble)
        texts = " ".join(c.text() for c in chips)
        assert "clipped" not in texts and "continuous audio" not in texts
        assert bubble.pairing_label.isVisibleTo(bubble)
        assert _inside(bubble.pairing_label, bubble)
        for chip in chips:
            assert _inside(chip, bubble), f"chip {chip.text()!r} runs out of its row"
        assert bubble.review_label.text() != "–"          # the review mark shows
    del viewport


def test_minimum_width_is_reported_and_rows_still_fit_there(qt_app, window_with):
    window, made = window_with([BUSY_ROW])
    smallest = _resize(qt_app, window, 300)            # Qt clamps to the real minimum
    print(f"\nreal minimum window width with every chip: {smallest} px "
          f"(minimumSizeHint {window.minimumSizeHint().width()} px)")
    assert smallest <= LAPTOP_WIDTH
    bubble = window.timeline.bubble(made[0].id)
    right = bubble.mapTo(window, QtCore.QPoint(bubble.width(), 0)).x()
    assert right <= window.width(), f"at {smallest} px a row runs past the window"
    for chip in _chips(bubble):
        assert _inside(chip, bubble)


# ---- what the caption says after the operator acts --------------------------------

HELD = {"speech_check": {"verdict": "likely_noise", "reasons": [],
                         "speech_ratio": 0.0, "speech_seconds": 0.0,
                         "no_speech_probability": 0.9}}


def _held(store, tmp_path, **fields):
    session = Session(name="held", sample_rate=48_000, target_language="en")
    store.save_session(session)
    tx = _message(session.id, 0, utcnow(), tmp_path, transcript="thank you for watching",
                  source_language="es", signal_metadata=dict(HELD), **fields)
    store.save_transmission(tx)
    return tx


def test_an_explicit_correction_is_shown_as_checked(qt_app, config, store, tmp_path):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.timeline import _speech_check_note, _transcript_caption

    tx = _held(store, tmp_path)
    app = BabelFishRApp(config=config, store=store)
    app.correct(tx.id, transcript="gracias por ver", translation="thanks for watching")
    tx = store.get_transmission(tx.id)
    assert tx.reviewed and not tx.needs_review
    assert tx.id not in {t.id for t in store.review_queue()}
    assert _transcript_caption(tx)[0] == "TRANSCRIPT · CHECKED BY OPERATOR"
    assert _speech_check_note(tx).startswith("You checked and saved this transcript")


def test_a_note_alone_does_not_certify_the_words(qt_app, config, store, tmp_path):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.timeline import _transcript_caption

    tx = _held(store, tmp_path)
    app = BabelFishRApp(config=config, store=store)
    app.correct(tx.id, notes="heard a carrier, nothing else")
    tx = store.get_transmission(tx.id)
    assert tx.notes == "heard a carrier, nothing else"
    assert not tx.reviewed and tx.needs_review
    assert tx.id in {t.id for t in store.review_queue()}
    assert _transcript_caption(tx)[0] == "TRANSCRIPT"
    from babelfishr.ui.timeline import problem_reason
    assert problem_reason(tx) == "Audio unintelligible"


def test_retry_as_speech_after_a_correction_is_unchecked_again(qt_app, config, store,
                                                               tmp_path):
    """Retry as speech produces new words: they are what is shown, they are
    not checked, and the earlier correction is kept, not lost."""
    from test_live_audio_fixes import PairTranslation, ScriptedTranscription, _retry
    from test_live_audio_fixes import result as scripted

    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.timeline import _transcript_caption

    tx = _held(store, tmp_path)
    app = BabelFishRApp(config=config, store=store)
    app.correct(tx.id, transcript="gracias por ver", translation="thanks for watching")
    _retry(config, store,
           ScriptedTranscription(scripted("equipo uno en posicion", ratio=0.01, nsp=0.8,
                                          language="es")),
           PairTranslation(pairs=(("es", "en"),)), tx.id, source_language="es")
    tx = store.get_transmission(tx.id)
    assert not tx.reviewed and tx.needs_review
    assert tx.display_transcript == "equipo uno en posicion"
    assert tx.signal_metadata["superseded_corrections"][-1]["transcript"] == "gracias por ver"
    assert _transcript_caption(tx)[0] == "TRANSCRIPT"
    from babelfishr.ui.timeline import problem_reason
    assert problem_reason(tx) == ""          # the operator said it is speech


# ---- review findings ------------------------------------------------------------------

def test_an_unbroken_transcript_wraps_instead_of_being_clipped(qt_app, window_with):
    """A recognition loop, URL or callsign run has no spaces to wrap at. It
    used to be cut at the box edge with no sign anything was missing."""
    loop = "ლ" * 400
    window, made = window_with([dict(transcript=loop)])
    _resize(qt_app, window, 1146)
    bubble = window.timeline.bubble(made[0].id)
    label = bubble.original_label
    line = label.fontMetrics().height()
    assert label.height() > 5 * line, "the run did not wrap onto further lines"
    assert label.heightForWidth(label.width()) <= label.height() + 2, "clipped"
    assert label.toPlainText() == loop          # displayed exactly, nothing inserted


def test_a_note_reviewed_row_from_an_older_version_stays_consistent(qt_app, config,
                                                                    store, tmp_path):
    """Older versions set reviewed on a note-only edit. Caption, review mark
    and Review queue agree: the words were never checked."""
    from babelfishr.ui.timeline import _transcript_caption

    tx = _held(store, tmp_path, reviewed=True, notes="carrier only")
    tx = store.get_transmission(tx.id)
    assert tx.needs_review and tx.id in {t.id for t in store.review_queue()}
    assert _transcript_caption(tx)[0] == "TRANSCRIPT"
    from babelfishr.ui.timeline import problem_reason
    assert problem_reason(tx) == "Audio unintelligible"


def test_retry_as_speech_confirmation_alone_shows_check(qt_app, config, store, tmp_path):
    from babelfishr.ui.timeline import _transcript_caption

    tx = _held(store, tmp_path)
    tx.signal_metadata = dict(tx.signal_metadata,
                              speech_confirmed={"source_language": "es", "by": "operator"})
    assert _transcript_caption(tx)[0] == "TRANSCRIPT"
    from babelfishr.ui.timeline import problem_reason
    assert problem_reason(tx) == ""          # the operator said it is speech
    assert tx.needs_review


@pytest.mark.parametrize("seconds,words", [(20.0, "about 20 seconds"),
                                           (0, "Segmenting is off")])
def test_the_activity_tooltip_follows_the_segment_setting(qt_app, config, store,
                                                          seconds, words):
    from babelfishr.app import BabelFishRApp
    from babelfishr.pipeline import PipelineState
    from babelfishr.ui.main_window import MainWindow

    config.detector.segment_seconds = seconds
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        window._state = PipelineState.RECEIVING
        window._refresh_state_badge()
        assert words in window.state_badge.toolTip()
    finally:
        window.close()


# ---- copy gives back exactly what was said ---------------------------------------------

EXACT = {
    "identifier": "W1AW/K2ABC-UNIT-0042-ALPHA-BRAVO-CHARLIE-DELTA-ECHO-FOXTROT-GOLF-HOTEL"
                  "-INDIA-JULIETT-KILO-LIMA-MIKE-NOVEMBER-OSCAR-PAPA-QUEBEC-ROMEO",
    "arabic": "\u0645\u0631\u062d\u0628\u0627 \u0643\u064a\u0641 \u062d\u0627\u0644\u0643 "
              "\u0627\u0644\u064a\u0648\u0645\u060c \u0646\u062d\u0646 \u0641\u064a "
              "\u0627\u0644\u0645\u0648\u0642\u0639",
    # Persian with its own zero-width non-joiner (U+200C), which must survive.
    "persian_zwnj": "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 "
                    "\u0628\u0627 \u0634\u0645\u0627 \u0635\u062d\u0628\u062a "
                    "\u06a9\u0646\u0645",
    "loop": "\u10da" * 400,
    # Repeated spaces, a line break, a tab and characters HTML would eat.
    "whitespace": "a  b\nc & <d>\tx  end",
}


@pytest.mark.parametrize("name", EXACT)
def test_selecting_and_copying_a_transcript_gives_the_exact_text(qt_app, window_with, name):
    from PySide6.QtTest import QTest

    text = EXACT[name]
    window, made = window_with([dict(transcript=text)])
    _resize(qt_app, window, 1146)
    label = window.timeline.bubble(made[0].id).original_label
    assert label.toPlainText() == text
    clipboard = QtWidgets.QApplication.clipboard()

    # Select all and copy, as the keyboard does.
    clipboard.clear()
    label.selectAll()
    label.copy()
    pump(qt_app)
    assert clipboard.text() == text

    # A real mouse drag from before the first character to after the last.
    clipboard.clear()
    label.moveCursor(QtGui.QTextCursor.End)
    end = label.cursorRect().center() + QtCore.QPoint(2, 0)
    viewport = label.viewport()
    rtl = text[0] in "\u0645\u0627"
    begin = QtCore.QPoint(viewport.width() - 1 if rtl else 0, 2)
    QTest.mousePress(viewport, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, begin)
    QTest.mouseMove(viewport, end)
    QTest.mouseRelease(viewport, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, end)
    label.copy()
    pump(qt_app)
    assert clipboard.text() == text, "a mouse-selected copy differs from the transcript"
    assert len(label.toPlainText()) == len(text)        # nothing added for wrapping



def test_scrolling_over_transcripts_neither_takes_focus_nor_moves_the_selection(
        qt_app, window_with):
    """The transcript widget takes focus on click only (as the QLabel did):
    a wheel over another row scrolls the thread and leaves the selection."""
    rows = [dict(transcript=f"Message {i}: " + "routine traffic " * 12) for i in range(12)]
    window, made = window_with(rows)
    _resize(qt_app, window, 1146)
    timeline = window.timeline
    first = timeline.bubble(made[0].id)
    assert first.original_label.focusPolicy() == QtCore.Qt.ClickFocus
    timeline.select(made[0].id)
    pump(qt_app)
    other = timeline.bubble(made[-1].id).original_label
    bar = timeline.verticalScrollBar()              # TimelineView is the scroll area
    assert bar.maximum() > 0, "not enough rows to scroll"
    before = bar.value()
    target = other.viewport()
    point = QtCore.QPointF(5, 5)
    event = QtGui.QWheelEvent(point, target.mapToGlobal(point.toPoint()).toPointF(),
                              QtCore.QPoint(0, 0), QtCore.QPoint(0, -120),
                              QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
                              QtCore.Qt.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(target, event)
    pump(qt_app)
    assert QtWidgets.QApplication.focusWidget() is not other
    assert timeline.selected_transmission().id == made[0].id, "the selection moved"
    # Qt hands an ignored wheel event on to the parent widgets - the thread's
    # scroll area - only for real (spontaneous) input, which a test cannot
    # synthesise; a plain row frame is the same. What can be checked is the
    # precondition: the transcript leaves the event unaccepted, as the row
    # frame does, and the thread itself scrolls when the event reaches it.
    assert not event.isAccepted(), "the transcript kept the wheel event"
    QtWidgets.QApplication.sendEvent(timeline.viewport(), QtGui.QWheelEvent(
        point, timeline.viewport().mapToGlobal(point.toPoint()).toPointF(),
        QtCore.QPoint(0, 0), QtCore.QPoint(0, -120), QtCore.Qt.NoButton,
        QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False))
    pump(qt_app)
    assert bar.value() != before
    assert timeline.selected_transmission().id == made[0].id



def test_edited_and_translated_text_copies_as_just_the_words(qt_app, window_with):
    """"edited" and the language are said in the captions; copying the
    transcript or the translation gives only the operator's words."""
    window, made = window_with([dict(
        transcript="unit one  in position", source_language="es", target_language="en",
        translation="team one in position",
        transcript_correction="unidad uno  en posici\u00f3n\tcambio",
        translation_correction="unit one in position,\nover")])
    _resize(qt_app, window, 1146)
    bubble = window.timeline.bubble(made[0].id)
    assert bubble.transcript_caption.text().endswith("\u00b7 EDITED")
    assert bubble.translation_caption.text() == "TRANSLATION \u00b7 EN \u00b7 EDITED"
    clipboard = QtWidgets.QApplication.clipboard()
    for label, expected in ((bubble.original_label, "unidad uno  en posici\u00f3n\tcambio"),
                            (bubble.translated_label, "unit one in position,\nover")):
        clipboard.clear()
        label.selectAll()
        label.copy()
        pump(qt_app)
        assert clipboard.text() == expected



def test_every_superseded_correction_is_kept(qt_app, config, store, tmp_path):
    """Two corrections, each followed by Retry as speech: both are kept."""
    from test_live_audio_fixes import PairTranslation, ScriptedTranscription, _retry
    from test_live_audio_fixes import result as scripted

    from babelfishr.app import BabelFishRApp

    tx = _held(store, tmp_path)
    app = BabelFishRApp(config=config, store=store)
    engine = ScriptedTranscription(scripted("equipo uno", ratio=0.01, nsp=0.8, language="es"))
    for words in ("first correction", "second correction"):
        app.correct(tx.id, transcript=words, translation=words)
        _retry(config, store, engine, PairTranslation(pairs=(("es", "en"),)), tx.id,
               source_language="es")
    kept = store.get_transmission(tx.id).signal_metadata["superseded_corrections"]
    assert [c["transcript"] for c in kept] == ["first correction", "second correction"]
