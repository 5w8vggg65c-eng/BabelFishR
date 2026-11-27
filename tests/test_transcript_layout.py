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


def test_the_column_header_reads_time_transcription_review(qt_app, window_with):
    """Operator request: the message column's heading is exactly
    "Transcription"; the time (and its zone) and REVIEW stay."""
    window, _ = window_with([dict(transcript="Short.", translation="Kurz.")])
    header = window.findChild(QtWidgets.QFrame, "columnHeader")
    shown = [label.text() for label in header.findChildren(QtWidgets.QLabel)
             if label.isVisibleTo(header)]
    assert shown == [window.time_heading.text(), "Transcription", "REVIEW"]
    assert window.time_heading.text().startswith("TIME")
    assert not any("MESSAGE" in t or "TRANSLATION" in t for t in shown)
