"""The chat-style timeline: one bubble per received transmission."""

from __future__ import annotations

import os

import contextlib
import pathlib
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..models import (ContentClass, ProcessingState, Provenance,
                      Transmission)
from .playback import PAUSED, PLAYING, PlaybackController
from .widgets import TagEditor

#: Fixed widths for the TIME and REVIEW columns, shared with the column
#: header the window draws above the list so the two line up.
TIME_COLUMN_WIDTH = 96
REVIEW_COLUMN_WIDTH = 120

CONTENT_LABELS = {
    "speech": "speech",
    # What the audio itself shows, not a claim about the radio signal.
    "noise": "noise-like audio",
    "tone": "steady tone",
    "unknown": "unclassified",
}

#: Stored classes the window never badges. "digital-suspected" was a guess
#: from earlier versions (never produced now, see
#: detect.ContentClass.DIGITAL_SUSPECTED); a row saved with it keeps the
#: value in the database and in exports, but shows no badge.
UNBADGED_CLASSES = {"digital-suspected"}

STATE_LABELS = {
    ProcessingState.CAPTURED: "queued",
    ProcessingState.TRANSCRIBING: "transcribing...",
    ProcessingState.TRANSCRIBED: "transcribed",
    ProcessingState.TRANSLATING: "translating...",
    ProcessingState.COMPLETE: "",
    ProcessingState.FAILED: "failed",
    ProcessingState.SKIPPED: "not speech",
}


#: The one per-card action for "this is speech" (language or Automatic).
SPEECH_ACTION = "Transcribe as speech\u2026"

#: What the bubble says while it is filling itself in.
_IN_FLIGHT = {
    ProcessingState.CAPTURED: "Queued...",
    ProcessingState.TRANSCRIBING: "Transcribing...",
    ProcessingState.TRANSCRIBED: "Transcribing...",
    ProcessingState.TRANSLATING: "Translating...",
}


#: How the bubble labels a frequency, by where it came from.
#:
#: This used to be one boolean - measured, or "(entered)" - which made every
#: non-measured origin read as something the operator typed. A value an SDR
#: supplied without stating its provenance, one the software inferred, and one
#: DSD-neo decoded were all labelled "entered", which is a claim about who put
#: it there and was untrue in all three cases.
_FREQUENCY_SUFFIX = {
    Provenance.SDR: "",
    Provenance.RADIO: "",
    Provenance.OPERATOR: " (entered)",
    Provenance.PROFILE: " (entered)",
    Provenance.INFERRED: " (inferred)",
    Provenance.DSD: " (decoded)",
    Provenance.UNKNOWN: " (unverified)",
}


def _speech_check_note(tx: Transmission) -> str:
    """Plain words for the speech check (pipeline.assess_speech), or ""."""
    metadata = tx.signal_metadata or {}
    check = metadata.get("speech_check") or {}
    verdict = check.get("verdict")
    if verdict not in ("likely_noise", "uncertain"):
        return ""
    if operator_checked(tx):
        decision = ("You approved this result." if metadata.get("operator_approved")
                    else "You checked and saved this transcript.")
        return (decision + " The automatic speech check had marked it "
                + ("probably not speech." if verdict == "likely_noise" else "uncertain."))
    confirmed = metadata.get("speech_confirmed") or {}
    if confirmed:
        language = confirmed.get("source_language")
        said = (f"You marked this as speech in {language}" if language
                else "You marked this as speech (language detected automatically)")
        check_words = (" Check the new transcript against the audio. Approve it or use "
                       "Edit to save any corrections.")
        stage = tx.error.stage if tx.error else ""
        if tx.state is ProcessingState.FAILED:
            if stage == "translation":
                return (f"{said}. It was transcribed again; the translation failed "
                        f"(see the error below).{check_words}")
            return f"{said}. Transcribing it again failed (see the error below)."
        if tx.state is ProcessingState.CAPTURED:
            return f"{said}. It is queued to be transcribed again in that language."
        if tx.state in (ProcessingState.TRANSCRIBING, ProcessingState.TRANSLATING,
                        ProcessingState.TRANSCRIBED):
            return f"{said}. It is being processed again in that language."
        if tx.state is not ProcessingState.COMPLETE:
            return f"{said}. It was not transcribed again ({tx.state.value})."
        if tx.guess_withheld:
            return (f"{said}. The new guess still lacks speech evidence. "
                    "Listen to the recording and use Edit transcript and translation "
                    "if you can identify the words.")
        if tx.translation:
            return (f"{said}. It was transcribed again in that language and "
                    f"translated.{check_words}")
        if not _differs(tx.source_language, tx.target_language):
            return (f"{said}. It was transcribed again in that language; it is "
                    f"already in the target language, so it was not "
                    f"translated.{check_words}")
        return f"{said}. It was transcribed again in that language.{check_words}"
    ratio = check.get("speech_ratio")
    seconds = check.get("speech_seconds")
    nsp = check.get("no_speech_probability")
    evidence = []
    if ratio is not None:
        found = (f"{seconds:.1f} s of speech" if seconds is not None
                 else f"speech in {ratio:.0%} of it")
        evidence.append(f"the local voice detector found {found}")
    if nsp is not None:
        evidence.append(f"the speech engine's no-speech estimate is {nsp:.2f}")
    # Why the text itself looks wrong (pipeline.degenerate_text), if it does.
    evidence += [r for r in (check.get("reasons") or [])
                 if r.startswith("the transcript ")]
    said = f" ({'; '.join(evidence)})" if evidence else ""
    if verdict == "likely_noise":
        return (f"Probably not speech{said}. The transcript may be words "
                f"invented from noise, so it was not translated automatically. "
                f"Check it against the original audio. If it is speech, choose "
                f"Transcribe as speech\u2026 from the message menu: it is transcribed "
                f"again in the language you pick, then translated.")
    return (f"Speech uncertain{said}. Check the transcript against the "
            f"original audio; Transcribe as speech\u2026 re-runs it in a language "
            f"you choose.")


def _translation_unresolved_note(tx: Transmission) -> str:
    """Why no translation was attempted (pipeline TRANSLATION_UNRESOLVED),
    or "". Visible until the message is retried or its words are saved."""
    unresolved = (tx.signal_metadata or {}).get("translation_unresolved") or {}
    if not unresolved:
        return ""
    guessed = unresolved.get("guessed_language") or "?"
    target = unresolved.get("target_language") or "?"
    confidence = unresolved.get("language_confidence")
    shown = f"{confidence:.0%}" if isinstance(confidence, (int, float)) else "unknown certainty"
    words = (f"Translation not attempted: language unresolved. Automatic "
             f"detection guessed '{guessed}' ({shown}), and no {guessed}->{target} "
             f"language pack is installed. ")
    if operator_checked(tx):
        return words + "You have since checked this message."
    # No shortcut in the prose: Qt shows Ctrl as Command on macOS, and the
    # menu item itself shows the right key.
    return words + ("The recording and transcript are kept. If this is "
                    "speech, choose Transcribe as speech\u2026 in the message's "
                    "menu.")


def operator_checked(tx: Transmission) -> bool:
    """The operator approved or corrected the current recognition."""
    return tx.operator_checked


def original_present(tx: Transmission) -> bool:
    """The ORIGINAL recording is on disk as a regular file. A stored path
    alone is not enough, and a processed or decoded copy does not count.
    Only the file's type is checked; its audio is never read."""
    path = tx.audio_path
    if not path:
        return False
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


def _cleared(tx: Transmission) -> dict:
    return (tx.signal_metadata or {}).get("history_cleared") or {}


#: States in which the transcript is still to come.
_TEXT_PENDING = (ProcessingState.CAPTURED, ProcessingState.TRANSCRIBING)


def problem_reason(tx: Transmission) -> str:
    """The one short reason a card may show, or "".

    "Transcription error" only when recognition itself failed;
    "Translation error" when only the translation did (never reported as a
    recognition failure); "Audio unintelligible" when no intelligible speech
    was found - skipped as not speech, held as probably invented from noise,
    or recognised to nothing. Readable but uncertain speech, and anything the
    operator has checked, gets no reason.
    """
    if tx.state is ProcessingState.FAILED and tx.error:
        return ("Translation error" if tx.error.stage == "translation"
                else "Transcription error")
    if tx.is_static:
        return "Static"
    if operator_checked(tx) or (tx.signal_metadata or {}).get("processing_stopped"):
        return ""
    metadata = tx.signal_metadata or {}
    verdict = (metadata.get("speech_check") or {}).get("verdict")
    if tx.guess_withheld or (verdict == "likely_noise"
                             and not metadata.get("speech_confirmed")):
        return "Audio unintelligible"
    if tx.state is ProcessingState.SKIPPED:
        return "Audio unintelligible"
    if (tx.state is ProcessingState.COMPLETE and not (tx.display_transcript or "").strip()
            and "text" not in _cleared(tx)):
        return "Audio unintelligible"
    return ""


def pairing_status(tx: Transmission) -> tuple:
    """(text, ok) for the card's one audio + transcript check.

    Audio is the original recording, checked on disk; the transcript is the
    original-language text (an operator's correction counts, a translation
    alone does not). Intentional clears say so; text still being produced
    says pending. A short reason (see problem_reason) is appended.
    """
    cleared = _cleared(tx)
    if original_present(tx):
        audio = "ok"
    elif "audio" in cleared and not tx.audio_path:
        audio = "cleared"
    else:
        audio = "missing"
    if tx.guess_withheld:
        text = "withheld"
    elif (tx.display_transcript or "").strip():
        text = "ok"
    elif "text" in cleared or "transcript" in (tx.signal_metadata or {}).get("operator_cleared_fields", ()):
        text = "cleared"
    elif tx.state in _TEXT_PENDING and not (tx.signal_metadata or {}).get(
            "processing_stopped"):
        text = "pending"
    else:
        text = "missing"
    if text == "withheld":
        # A guess, not a transcript: one plain reason, the guess on inspection.
        reason = "Static" if tx.is_static else "Audio unintelligible"
        words = reason if audio == "ok" else f"Audio {audio} \u00b7 {reason}"
        return words, False
    if audio == "ok" and text == "ok":
        words = "Audio + transcript \u2713"
    elif audio == "ok":
        words = f"Transcript {text}"
    elif text == "ok" or (text == "pending" and audio != "ok"):
        words = f"Audio {audio}"
    elif audio == text:
        words = f"Audio and transcript {audio}"
    else:
        words = f"Audio {audio} \u00b7 transcript {text}"
    reason = problem_reason(tx)
    if reason:
        words += f" \u00b7 {reason}"
    return words, audio == "ok" and text == "ok" and not reason


def details_for_inspection(tx: Transmission) -> str:
    """The technical detail behind a card's status, for its tooltip only:
    the error message, the speech check's evidence, why a translation was
    not attempted, the skip reason. Never shown as text on the card."""
    parts: List[str] = []
    if tx.withheld_ending:
        parts.append(f"Withheld from the end, not supported by the recording: "
                     f"\u201c{tx.withheld_ending}\u201d. The full engine text is kept "
                     f"(Edit transcript and translation).")
    if tx.guess_withheld:
        parts.append(f"Engine guess, withheld as not speech: \u201c{tx.transcript.strip()}\u201d. "
                     f"Use Edit transcript and translation to correct the words, "
                     f"or {SPEECH_ACTION} to run recognition again.")
    if tx.state is ProcessingState.FAILED and tx.error:
        parts.append(f"{tx.error.stage} failed: {tx.error.message}")
    for note in (_speech_check_note(tx), _translation_unresolved_note(tx),
                 tx.skip_reason or ""):
        if note:
            parts.append(note)
    return "\n\n".join(parts)


def _transcript_caption(tx: Transmission) -> tuple:
    """The transcript box's caption and its tone, from the speech check.

    A transcript the speech check held back (``likely_noise``) is not
    presented as ordinary output: its caption says it is held for review.
    ``uncertain`` - or a held one the operator has since marked as speech -
    gets the milder "check" marker. The words themselves are always shown.
    """
    # The speech check's verdict is no longer spelled out on the card (the
    # operator found it noise): the card's one status line says "Audio
    # unintelligible" when that is the finding, and the detail is in its
    # tooltip.
    if operator_checked(tx):
        return "TRANSCRIPT \u00b7 CHECKED BY OPERATOR", "none"
    return "TRANSCRIPT", "none"


def _local_stamp(when) -> str:
    """``2026-09-07 10:41:20``: the date and time of one transmission, in
    the zone chosen under View > Show Times In (``... UTC`` when UTC).

    The header used to show only the time, which is enough while reading
    today's traffic and misleading for anything older - a bubble from last
    week looked exactly like one from an hour ago.
    """
    from ..timefmt import stamp

    return stamp(when)


def _frequency_suffix(provenance) -> str:
    """The label for one frequency. An unrecognised origin is unverified.

    Never blank by default: a missing entry must not silently promote a value
    to looking like a measurement.
    """
    return _FREQUENCY_SUFFIX.get(provenance or Provenance.UNKNOWN,
                                 " (unverified)")


def _differs(source: str, target: str) -> bool:
    """Are these two language tags actually different languages?

    ``en`` and ``en-GB`` are not, and neither is ``EN`` and ``en``. Getting
    this wrong shows the operator a translation row that repeats what they
    have already read.
    """
    if not source or not target:
        return bool(source and target)
    return (source.strip().lower().split("-")[0]
            != target.strip().lower().split("-")[0])


class WrappingText(QtWidgets.QTextEdit):
    """Read-only, selectable plain text that always fits its width.

    A word-wrapped QLabel cannot break a run with no spaces in it, so a long
    recognition loop or identifier was cut off at the box edge. This wraps
    at word boundaries, and anywhere within a run longer than a line
    (QTextOption.WrapAtWordBoundaryOrAnywhere), by layout alone: the text is
    not altered, so selection and copy give back exactly the stored words.
    Its height follows its document; it never scrolls by itself, and wheel
    events go on to the thread. ``text()``/``setText()`` mirror QLabel's for
    the callers and tests that read it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._html = ""
        self._heights = {}                 # width -> height, for this text
        self.setReadOnly(True)
        # Like the QLabel it replaces: focus on click only. The default
        # (wheel focus) moved focus - and with it the row selection - to
        # whatever transcript the pointer crossed while scrolling the thread,
        # and made every transcript a Tab stop.
        self.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
        self.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(0)
        self.viewport().setAutoFillBackground(False)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setContextMenuPolicy(QtCore.Qt.DefaultContextMenu)

    def setText(self, text: str) -> None:          # noqa: N802 - QLabel's name
        """Plain text, exactly as given: no markup, no escaping, whitespace
        kept. Labels such as "edited" or the language belong in the box
        caption, so what is selected and copied is only the words."""
        self._html = text
        self._heights.clear()
        self.setPlainText(text)
        self._fit()

    def text(self) -> str:
        return self._html

    def set_editable(self, editable: bool) -> None:
        if editable == (not self.isReadOnly()):
            return
        self.setReadOnly(not editable)
        self.setAcceptRichText(False)
        self.setTextInteractionFlags(QtCore.Qt.TextEditorInteraction if editable else
                                     QtCore.Qt.TextSelectableByMouse)
        if editable:
            self.textChanged.connect(self._fit_edit)
        else:
            self.textChanged.disconnect(self._fit_edit)

    def _fit_edit(self) -> None:
        self._html = self.toPlainText()
        self._heights.clear()
        self._fit()

    def setWordWrap(self, _on: bool) -> None:      # noqa: N802 - always wraps
        pass

    def hasHeightForWidth(self) -> bool:           # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:   # noqa: N802
        height = self._heights.get(width)
        if height is None:
            document = self.document().clone()
            document.setTextWidth(max(1, width - 2 * self.frameWidth()))
            height = int(document.size().height() + 0.999) + 2 * self.frameWidth()
            self._heights[width] = height
        return height

    def changeEvent(self, event):                  # noqa: N802
        if event.type() in (QtCore.QEvent.FontChange, QtCore.QEvent.StyleChange):
            self._heights.clear()                  # the same text, new metrics
        super().changeEvent(event)

    def sizeHint(self):                            # noqa: N802
        return QtCore.QSize(super().sizeHint().width(), self.heightForWidth(self.width()))

    def minimumSizeHint(self):                     # noqa: N802
        return QtCore.QSize(0, self.heightForWidth(self.width()))

    def resizeEvent(self, event):                  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def showEvent(self, event):                    # noqa: N802
        super().showEvent(event)
        self._fit()

    def _fit(self) -> None:
        height = self.heightForWidth(self.width())
        if self.height() != height or self.minimumHeight() != height:
            self.setFixedHeight(height)

    def wheelEvent(self, event):                   # noqa: N802
        event.ignore()                             # the thread scrolls, not the row


class FlowLayout(QtWidgets.QLayout):
    """Left-to-right items that wrap onto further lines (for chips).

    Its minimum width is the widest single item, not the sum of all of them,
    and it reports height-for-width, so a narrow row grows a line instead of
    growing wider than its window.
    """

    def __init__(self, parent=None, spacing: int = 5):
        super().__init__(parent)
        self._items: List[QtWidgets.QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):                       # noqa: N802 - Qt API
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):                       # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):                       # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):                 # noqa: N802
        return QtCore.Qt.Orientations()

    def hasHeightForWidth(self):                   # noqa: N802
        return True

    def heightForWidth(self, width):               # noqa: N802
        return self._arrange(QtCore.QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):                   # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):                            # noqa: N802
        return self.minimumSize()

    def minimumSize(self):                         # noqa: N802
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QtCore.QSize(margins.left() + margins.right(),
                                   margins.top() + margins.bottom())

    def _arrange(self, rect, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y, line = area.x(), area.y(), 0
        gap = self.spacing()
        for item in self._items:
            if item.widget() is not None and not item.widget().isVisibleTo(
                    self.parentWidget()):
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x, y, line = area.x(), y + line + gap, 0
            if apply:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += hint.width() + gap
            line = max(line, hint.height())
        return y + line - rect.y() + margins.bottom()


class TransmissionBubble(QtWidgets.QFrame):
    """One received transmission, rendered as a chat bubble.

    Original and translated text are always shown as separate, labelled rows -
    the operator must never be left guessing which one they are reading.
    """

    correctionRequested = QtCore.Signal(str, str, str)   # tx_id, transcript, translation
    approvalRequested = QtCore.Signal(str)
    editingChanged = QtCore.Signal(object)
    tagsChanged = QtCore.Signal(str, list)
    bookmarkToggled = QtCore.Signal(str, bool)
    retryRequested = QtCore.Signal(str)
    retryAsSpeechRequested = QtCore.Signal(str)       # tx_id: choose a language
    exportRequested = QtCore.Signal(str)
    noteChanged = QtCore.Signal(str, str)
    transcribeAnywayRequested = QtCore.Signal(str)
    analyzeDigitalRequested = QtCore.Signal(str, str)   # tx_id, protocol
    removeRequested = QtCore.Signal(str)                # tx_id
    restoreRequested = QtCore.Signal(str)               # tx_id
    #: The operator chose this row (click or keyboard focus).
    selectRequested = QtCore.Signal(str)                # tx_id

    def __init__(self, tx: Transmission, player: PlaybackController,
                 parent: Optional[QtWidgets.QWidget] = None, *,
                 follow_player: bool = True):
        super().__init__(parent)
        self.tx = tx
        self._player = player
        self.editing = False
        self.request_editor = None
        if follow_player:
            # A bubble on its own listens to the controller itself. Inside a
            # TimelineView it does not: the view hands each change to the
            # bubbles it concerns, so a notification costs work in proportion
            # to the recordings affected, not to the length of the thread.
            self._player.changed.connect(self._on_player_changed)
        self.setObjectName("bubble")
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)

        self.setProperty("selected", False)
        # A row in the review list: selectable by click or keyboard, and the
        # selection is what the waveform and Selected Audio panel follow.
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setAccessibleName("Message")

        # Columns, as the v5 review list: TIME | MESSAGE | REVIEW. The
        # MESSAGE holds the two text boxes and the editing actions. Playback
        # belongs to the shared transport so it stays put as the thread moves.
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)

        self.time_label = QtWidgets.QLabel()
        self.time_label.setObjectName("timeCell")
        self.time_label.setFixedWidth(TIME_COLUMN_WIDTH)
        self.time_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        grid.addWidget(self.time_label, 0, 0, QtCore.Qt.AlignTop)

        outer = QtWidgets.QVBoxLayout()
        outer.setSpacing(4)
        grid.addLayout(outer, 0, 1)
        review_column = QtWidgets.QVBoxLayout()
        review_column.setSpacing(4)
        grid.addLayout(review_column, 0, 2)

        self.review_label = QtWidgets.QLabel("\u2013")
        self.review_label.setObjectName("reviewMark")
        self.review_label.setAccessibleName("Review")
        self.review_label.setAlignment(QtCore.Qt.AlignCenter)
        self.review_label.setFixedWidth(REVIEW_COLUMN_WIDTH)
        review_column.addWidget(self.review_label, 0, QtCore.Qt.AlignRight)

        self.approve_button = QtWidgets.QToolButton()
        self.approve_button.setText("Approved")
        self.approve_button.setAccessibleName("Mark this transmission approved")
        self.approve_button.setToolTip("Mark this result as checked and keep its words")
        self.approve_button.clicked.connect(lambda: self.approvalRequested.emit(self.tx.id))
        review_column.addWidget(self.approve_button, 0, QtCore.Qt.AlignHCenter)

        self.header = QtWidgets.QLabel()
        self.header.setObjectName("bubbleHeader")
        self.header.setWordWrap(True)

        self.provisional = QtWidgets.QLabel()
        self.provisional.setObjectName("provisional")
        self.provisional.setWordWrap(True)
        self.provisional.hide()
        outer.addWidget(self.provisional)

        # The transcript: its own bounded, captioned box, as wide as the
        # text area. The caption says when the speech check holds it back.
        self.transcript_box = QtWidgets.QFrame()
        self.transcript_box.setObjectName("transcriptBox")
        self.transcript_box.setAccessibleName("Transcript")
        transcript_layout = QtWidgets.QVBoxLayout(self.transcript_box)
        transcript_layout.setContentsMargins(8, 4, 8, 6)
        transcript_layout.setSpacing(2)
        self.transcript_caption = QtWidgets.QLabel("TRANSCRIPT")
        self.transcript_caption.setObjectName("boxCaption")
        transcript_layout.addWidget(self.transcript_caption)
        # Wraps at word boundaries, and anywhere inside a run too long for
        # a line (a loop, an identifier, a callsign run), without changing
        # the text: what is selected and copied is exactly what was stored.
        self.original_label = WrappingText()
        self.original_label.setObjectName("originalText")
        transcript_layout.addWidget(self.original_label)
        outer.addWidget(self.transcript_box)

        # The translation, when there is one: a second labelled box under
        # the transcript, also full width. No box at all otherwise.
        self.translation_box = QtWidgets.QFrame()
        self.translation_box.setObjectName("translationBox")
        self.translation_box.setAccessibleName("Translation")
        translation_layout = QtWidgets.QVBoxLayout(self.translation_box)
        translation_layout.setContentsMargins(8, 4, 8, 6)
        translation_layout.setSpacing(2)
        self.translation_caption = QtWidgets.QLabel("TRANSLATION")
        self.translation_caption.setObjectName("boxCaption")
        translation_layout.addWidget(self.translation_caption)
        # Wraps at word boundaries, and anywhere inside a run too long for
        # a line (a loop, an identifier, a callsign run), without changing
        # the text: what is selected and copied is exactly what was stored.
        self.translated_label = WrappingText()
        self.translated_label.setObjectName("translatedText")
        translation_layout.addWidget(self.translated_label)
        outer.addWidget(self.translation_box)

        # Date, duration, language and measured signal: one muted line
        # under the text, outside the boxes.
        outer.addWidget(self.header)

        # Metadata chips: compact, scannable, and readable without colour.
        # Chips wrap onto further lines rather than widening the row: a row
        # with many chips (continuous audio, clipped, confidence, tags) once
        # needed 880 px and ran past a 1146 px laptop window.
        self.chip_box = QtWidgets.QWidget()
        self.chip_box.setObjectName("chipBox")
        self.chip_row = FlowLayout(self.chip_box, spacing=5)
        outer.addWidget(self.chip_box)

        self.error_label = QtWidgets.QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        outer.addWidget(self.error_label)

        self.notes_label = QtWidgets.QLabel()
        self.notes_label.setObjectName("noteText")
        self.notes_label.setWordWrap(True)
        self.notes_label.hide()
        outer.addWidget(self.notes_label)

        # No waveform here. This is a message thread: the transcript is what
        # the operator reads. An analyser belongs to a tool that analyses, not
        # to every line of a conversation.

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)

        # The card's one status: is the original recording on disk, and is
        # there a transcript for it (see pairing_status).
        self.pairing_label = QtWidgets.QLabel()
        self.pairing_label.setObjectName("pairingStatus")
        self.pairing_label.setWordWrap(True)
        controls.addWidget(self.pairing_label, 1)

        # Primary recovery action stays visible; everything else is in the menu.
        self.action_button = QtWidgets.QToolButton()
        self.action_button.setText(SPEECH_ACTION)
        self.action_button.setAccessibleName(
            "Transcribe this recording as speech, choosing its language or Automatic")
        self.action_button.clicked.connect(
            lambda: self.retryAsSpeechRequested.emit(self.tx.id))
        self.action_button.hide()
        controls.addWidget(self.action_button)

        # A removed message, wherever it is shown, can be put back in one
        # click (also in its menu).
        self.restore_button = QtWidgets.QToolButton()
        self.restore_button.setText("Restore")
        self.restore_button.setAccessibleName("Restore this message to the thread")
        self.restore_button.clicked.connect(
            lambda: self.restoreRequested.emit(self.tx.id))
        self.restore_button.hide()
        controls.addWidget(self.restore_button)

        self.retry_button = QtWidgets.QToolButton()
        self.retry_button.setText("Retry")
        self.retry_button.setAccessibleName("Retry processing")
        self.retry_button.clicked.connect(
            lambda: self.retryRequested.emit(self.tx.id))
        self.retry_button.hide()
        controls.addWidget(self.retry_button)

        controls.addStretch(1)

        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("statusText")
        review_column.addWidget(self.status_label, 0, QtCore.Qt.AlignRight)

        self.menu_button = QtWidgets.QToolButton()
        self.menu_button.setObjectName("menuButton")
        self.menu_button.setText("\u22ef")
        self.menu_button.setToolTip("More actions")
        self.menu_button.setAccessibleName("More actions")
        self.menu_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.menu_button.setMenu(self._build_menu())
        review_column.addWidget(self.menu_button, 0, QtCore.Qt.AlignRight)
        review_column.addStretch(1)
        outer.addLayout(controls)

        self.edit_button = QtWidgets.QToolButton()
        self.edit_button.setText("Edit")
        self.edit_button.setAccessibleName("Edit transcript and translation")
        self.edit_button.clicked.connect(self._edit)
        self.save_edit_button = QtWidgets.QToolButton()
        self.save_edit_button.setText("Save")
        self.save_edit_button.setAccessibleName("Save transcript changes")
        self.save_edit_button.clicked.connect(self._save_edit)
        self.cancel_edit_button = QtWidgets.QToolButton()
        self.cancel_edit_button.setText("Cancel")
        self.cancel_edit_button.setAccessibleName("Cancel transcript changes")
        self.cancel_edit_button.clicked.connect(self.cancel_edit)
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        for button in (self.edit_button, self.save_edit_button, self.cancel_edit_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        outer.addLayout(buttons)
        self.save_edit_button.hide()
        self.cancel_edit_button.hide()
        self._watch_children_for_selection()

        self.update_from(tx)

    def _build_menu(self) -> QtWidgets.QMenu:
        """Secondary actions, out of the way but one click deep."""
        menu = QtWidgets.QMenu(self)
        self.play_action = menu.addAction("Play original recording",
                                          self._toggle_play)
        self.decoded_action = menu.addAction("Play decoded audio",
                                             self._play_decoded)
        menu.addSeparator()
        self.edit_action = menu.addAction("Edit transcript and translation...", self._edit)
        menu.addAction("Add or edit note...", self._edit_note)
        menu.addAction("Edit tags...", self._edit_tags)
        self.bookmark_action = menu.addAction("Bookmark")
        self.bookmark_action.setCheckable(True)
        self.bookmark_action.triggered.connect(self._toggle_bookmark)
        menu.addSeparator()
        # One way to say "this is speech": choose its language or Automatic,
        # one job. (Transcribe anyway and Retry as speech in... were two
        # names for overlapping things.) transcribe_action and
        # retry_speech_action name this same action for older callers.
        self.speech_action = menu.addAction(
            SPEECH_ACTION, lambda: self.retryAsSpeechRequested.emit(self.tx.id))
        self.speech_action.setToolTip(
            "Transcribe this recording again as speech - in a language you "
            "choose, or Automatic - and translate it. Only this recording.")
        self.transcribe_action = self.retry_speech_action = self.speech_action
        # Digital analysis belongs to KingFishR, which adds its actions here
        # through babelfishr.extensions; BabelFishR registers none.
        from ..extensions import hooks

        for bubble_menu in hooks("bubble_menu"):
            bubble_menu(self, menu)
        menu.addSeparator()
        # Repeats the saved settings: offered where something did not
        # finish (a failure, or saved but not processed after Stop).
        self.retry_action = menu.addAction(
            "Retry processing", lambda: self.retryRequested.emit(self.tx.id))
        self.retry_action.setToolTip("Run this recording again with the same settings")
        menu.addAction("Export audio...", self._export)
        menu.addSeparator()
        # Two different things, named as two different things. Removing
        # keeps the data and can be undone from View > Show removed
        # messages; deleting is confirmed separately and cannot.
        self.remove_action = menu.addAction(
            "Remove message\u2026", lambda: self.removeRequested.emit(self.tx.id))
        self.restore_action = menu.addAction(
            "Restore to thread", lambda: self.restoreRequested.emit(self.tx.id))
        self.restore_action.setVisible(False)
        return menu

    # -- rendering -------------------------------------------------------
    def update_from(self, tx: Transmission) -> None:
        if (self.editing and tx.history_generation > self.tx.history_generation
                and _cleared(tx).get("text") != _cleared(self.tx).get("text")):
            # Clear Session History also discards an open draft of that text.
            self.tx = tx
            self.cancel_edit()
            return
        self.tx = tx
        # Date and time, from the transmission's own started_at converted to
        # this computer's local zone - never the clock at render time, and
        # never a network time source. Reprocessing a recording from last
        # month must still say last month.
        meta: List[str] = [_local_stamp(tx.started_at), f"{tx.duration:.1f}s"]
        if tx.channel_name:
            meta.append(tx.channel_name)
        if tx.frequency_mhz is not None:
            meta.append(f"{tx.frequency_mhz:.4f} MHz"
                        f"{_frequency_suffix(tx.frequency_provenance)}")
        if tx.source_language:
            language = tx.source_language
            if tx.language_confidence is not None:
                language += f" {tx.language_confidence:.0%}"
            meta.append(language)
        # No CLIPPED: the operator found it on every card and of no use; the
        # value stays recorded (and exported). The audio + transcript check
        # below the text is the status the card shows instead.
        # Whatever an SDR, a radio or a decoder actually supplied - and
        # nothing else. Absent values produce no entry at all rather than a
        # dash an operator could read as a measurement of zero, and anything
        # typed or defaulted is marked so it cannot pass for measured.
        meta += [entry["display"] for entry in tx.signal_summary()
                 if entry["label"] not in ("channel", "frequency")]
        self.header.setText("  ·  ".join(_escape(part) for part in meta))
        # TIME column: the local time of day this transmission began - the
        # clock on this computer, the same value as the header's.
        from ..timefmt import bare_clock

        # The column's own heading says the zone (TIME or TIME (UTC)); the
        # hover and the header line give the full date and time with it.
        self.time_label.setText(bare_clock(tx.started_at))
        self.time_label.setToolTip(_local_stamp(tx.started_at))
        self.time_label.setAccessibleName(f"Time {_local_stamp(tx.started_at)}")

        if not self.editing:
            self._render_words(tx)

        # No paragraphs of explanation on the card: its one status line (see
        # refresh_pairing) carries the short reason, and the technical detail
        # is in that line's tooltip.
        self.error_label.hide()
        self.retry_button.setVisible(tx.state is ProcessingState.FAILED)

        details: List[str] = []
        attempt = tx.latest_analysis
        if attempt is not None:
            line = (f"Digital analysis ({attempt.engine} "
                    f"{attempt.engine_version}): {_escape(attempt.summary())}")
            if attempt.metadata:
                line += " - " + _escape(", ".join(
                    f"{k}={v}" for k, v in attempt.metadata.items()
                    if k != "protocols_mentioned"))
            if attempt.error:
                line += f" - {_escape(attempt.error)}"
            details.append(line)
            warning = attempt.metadata.get("auto_hunt_warning")
            if warning:
                details.append(_escape(str(warning)))
        if tx.notes:
            details.append(f"Note: {_escape(tx.notes)}")
        if details:
            self.notes_label.setText("<br>".join(details))
            self.notes_label.show()
        else:
            self.notes_label.hide()

        self._rebuild_chips(tx)

        # Only what the operator must act on: a recording saved but not
        # processed because monitoring stopped. Routine states (queued,
        # needs review, translation not attempted) are not repeated here:
        # the text box, the REVIEW column and the status line say them.
        self.status_label.setText(
            "saved, not processed (monitoring stopped)"
            if (tx.signal_metadata or {}).get("processing_stopped") else "")
        # REVIEW column: the word, never only a colour.
        if tx.state is ProcessingState.FAILED:
            review, tone = "FAILED", "failed"
        elif tx.needs_review:
            review, tone = "CHECK", "check"
        elif tx.operator_checked and (tx.signal_metadata or {}).get("operator_approved"):
            review, tone = "APPROVED", "none"
        else:
            review, tone = "\u2013", "none"
        self.approve_button.setVisible(tx.can_approve and not self.editing)
        can_edit = tx.state not in _IN_FLIGHT
        self.edit_action.setEnabled(can_edit)
        self.edit_button.setEnabled(can_edit)
        self.review_label.setText(review)
        self.review_label.setProperty("tone", tone)
        self.review_label.setAccessibleDescription(
            {"failed": "Processing failed", "check": "Needs review",
             "none": "No review needed"}[tone])
        self.review_label.style().unpolish(self.review_label)
        self.review_label.style().polish(self.review_label)

        self.bookmark_action.setChecked(tx.bookmarked)
        self.setProperty("state", tx.state.value)
        self.setProperty("review", bool(tx.needs_review))
        self.setProperty("skipped", tx.state is ProcessingState.SKIPPED)
        self.style().unpolish(self)
        self.style().polish(self)

        # Recovery actions, shown only where they apply.
        can_force = tx.can_transcribe_anyway and tx.state in (
            ProcessingState.SKIPPED, ProcessingState.COMPLETE)
        self.action_button.setVisible(can_force)
        self.decoded_action.setVisible(bool(tx.decoded_audio_path))
        self.refresh_pairing()
        # A removed message, when shown at all, says so and offers only the
        # way back; an ordinary one offers removal.
        self.remove_action.setVisible(not tx.hidden)
        self.restore_action.setVisible(bool(tx.hidden))
        self.restore_button.setVisible(bool(tx.hidden))
        self.setProperty("removed", bool(tx.hidden))
        if tx.hidden:
            self.status_label.setText("Removed from thread \u2014 data kept")
        self._render_playback()

    def _render_words(self, tx: Transmission) -> None:
        # The transcript is the message. It is the primary content of the
        # bubble, in plain words, with no engine or language prefix competing
        # with it - the language already appears in the header line.
        original = tx.shown_transcript
        withheld = tx.guess_withheld
        self.transcript_box.setToolTip(details_for_inspection(tx)
                                       if withheld or tx.withheld_ending else "")
        if original:
            # Only the words: "edited" is said in the caption.
            self.original_label.setText(original)
            self.original_label.show()
            self.transcript_box.show()
        elif withheld or tx.is_static:
            # Not traffic: the recogniser's guess for noise is not shown as
            # words. The guess is in the tooltip and in Edit; the audio plays.
            self.original_label.setText(
                ("Static" if tx.is_static else "Audio unintelligible")
                + (" \u2014 no speech recognised (the engine's guess is withheld)"
                   if withheld else " \u2014 no speech recognised"))
            self.original_label.show()
            self.transcript_box.show()
        elif tx.state in _IN_FLIGHT:
            # Same bubble, not a separate placeholder row: the operator sees
            # one line per transmission that fills itself in.
            note = getattr(self, "_queue_note", "")
            self.original_label.setText(f"{_IN_FLIGHT[tx.state]} \u00b7 {note}"
                                        if note else _IN_FLIGHT[tx.state])
            self.original_label.show()
            self.transcript_box.show()
        else:
            self.original_label.hide()
            self.transcript_box.hide()
        caption, caption_tone = _transcript_caption(tx)
        if original and tx.transcript_correction:
            caption += " \u00b7 EDITED"
        self.transcript_caption.setText(caption)
        if self.transcript_box.property("held") != caption_tone:
            self.transcript_box.setProperty("held", caption_tone)
            for widget in (self.transcript_box, self.transcript_caption):
                widget.style().unpolish(widget)
                widget.style().polish(widget)

        # A translation row only when there is genuinely another language to
        # read. "already in English" under an English transcript is a line of
        # noise in every bubble of an English-speaking operator's thread.
        translated = tx.shown_translation
        if translated and _differs(tx.source_language, tx.target_language):
            # Only the words: the language and "edited" are in the caption.
            self.translated_label.setText(translated)
            self.translated_label.show()
            target = (tx.target_language or "").strip().upper()
            caption = f"TRANSLATION \u00b7 {target}" if target else "TRANSLATION"
            if tx.translation_correction:
                caption += " \u00b7 EDITED"
            self.translation_caption.setText(caption)
            self.translation_box.show()
        else:
            self.translated_label.hide()
            self.translation_box.hide()


    def refresh_pairing(self) -> None:
        """Re-check the original recording on disk and redraw the card's
        audio + transcript status, and Play/Retry with it. Cheap (one stat),
        so it runs on every update, on selection and on a view refresh -
        which is how a file removed or restored outside BabelFishR shows."""
        tx = self.tx
        words, ok = pairing_status(tx)
        self.pairing_label.setText(words)
        self.pairing_label.setAccessibleName(f"Status: {words}")
        self.pairing_label.setToolTip(details_for_inspection(tx) or words)
        if self.pairing_label.property("ok") != ok:
            self.pairing_label.setProperty("ok", ok)
            self.pairing_label.style().unpolish(self.pairing_label)
            self.pairing_label.style().polish(self.pairing_label)
        present = original_present(tx)
        # Nothing to play or re-process without the original recording.
        self.play_action.setEnabled(present)
        self.transcribe_action.setEnabled(present)
        for action in (getattr(self, "retry_action", None),
                       getattr(self, "retry_speech_action", None)):
            if action is not None:
                action.setEnabled(present)
        self.retry_button.setVisible(tx.state is ProcessingState.FAILED and present)
        # Retry repeats the saved settings, so it is offered only where
        # something did not finish; otherwise it would read as a third way
        # of saying "transcribe again".
        if getattr(self, "retry_action", None) is not None:
            self.retry_action.setVisible(
                tx.state is ProcessingState.FAILED
                or bool((tx.signal_metadata or {}).get("processing_stopped")))
        self.action_button.setVisible(
            present and tx.can_transcribe_anyway
            and tx.state in (ProcessingState.SKIPPED, ProcessingState.COMPLETE))

    def _rebuild_chips(self, tx: Transmission) -> None:
        """Compact metadata chips: class, confidence, tags, digital result."""
        while self.chip_row.count() > 0:
            item = self.chip_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # The operator's own marks only. Automatic diagnostics (content
        # class, confidence, clipped, continuous-audio part) are no longer
        # chips: the operator found them noise. They stay recorded, in
        # search and in exports.
        chips: List[tuple] = []
        for tag in tx.tags:
            chips.append((f"#{tag}", "accent"))
        if tx.bookmarked:
            chips.append(("bookmarked", "accent"))

        attempt = tx.latest_analysis
        if attempt is not None:
            tone = "accent" if attempt.outcome.is_success else "warning"
            chips.append((f"DSD: {attempt.summary()}", tone))

        for text, tone in chips:
            chip = QtWidgets.QLabel(text)
            chip.setObjectName("chip")
            chip.setProperty("tone", tone)
            chip.setAccessibleName(text)
            self.chip_row.addWidget(chip)
        self.chip_box.setVisible(bool(chips))

    def set_selected(self, selected: bool) -> None:
        """Mark this row as the one the waveform and Selected Audio follow."""
        if bool(self.property("selected")) == bool(selected):
            return
        self.setProperty("selected", bool(selected))
        self.setAccessibleDescription("Selected" if selected else "")
        for widget in [self] + self.findChildren(QtWidgets.QWidget):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.LeftButton:
            self.selectRequested.emit(self.tx.id)
        super().mousePressEvent(event)

    def _watch_children_for_selection(self) -> None:
        """A selectable transcript label or a button takes the click and the
        focus itself, so the row never saw them: watch the children too."""
        for child in self.findChildren(QtWidgets.QWidget):
            child.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        kind = event.type()
        if kind == QtCore.QEvent.FocusIn or (
                kind == QtCore.QEvent.MouseButtonPress
                and event.button() == QtCore.Qt.LeftButton):
            self.selectRequested.emit(self.tx.id)
        return False

    def focusInEvent(self, event: QtGui.QFocusEvent) -> None:  # noqa: N802
        self.selectRequested.emit(self.tx.id)
        super().focusInEvent(event)

    def set_provisional(self, text: str) -> None:
        """Show live partial text, visually marked as not final."""
        if text:
            self.provisional.setText(f"<i>… {_escape(text)}</i>")
            self.provisional.show()
        else:
            self.provisional.hide()

    # -- playback ----------------------------------------------------------
    def set_queue_note(self, note: str) -> None:
        """Where this held recording is: "transcription · 8 s" or "3rd in
        line" (from the processor's own queue); "" clears it."""
        if getattr(self, "_queue_note", "") == note:
            return
        self._queue_note = note
        if self.tx.state in _IN_FLIGHT and not self.tx.display_transcript:
            text = _IN_FLIGHT[self.tx.state]
            self.original_label.setText(f"{text} \u00b7 {note}" if note else text)

    #: Set by the list: starts the original *and what follows it* (the
    #: window's continuous playback). None: play just this one.
    start_original = None

    def _start_original(self) -> None:
        """The operator's deliberate Play on this card's original."""
        if not original_present(self.tx):
            self.refresh_pairing()           # gone since drawn: say so, play nothing
            return
        if self.start_original is not None:
            self.start_original(self.tx.id, self.tx.audio_path)
        else:
            self._player.play(self.tx.id, self.tx.audio_path)

    def _toggle_play(self) -> None:  # noqa: D401 - the menu's Play / Pause
        from .waveform import same_audio

        playing_this = (self._player.owner == self.tx.id
                        and same_audio(getattr(self._player, "path", None) or "",
                                       self.tx.audio_path or "")
                        and self._player.state_for(self.tx.id) in (PLAYING, PAUSED))
        if playing_this:
            self._player.toggle(self.tx.id, self.tx.audio_path)
        else:
            self._start_original()

    def _on_player_changed(self, affected) -> None:
        if self.tx.id in affected:
            # A play that failed because the file has gone shows on the card.
            self.refresh_pairing()
            self._render_playback()

    def _render_playback(self) -> None:
        """Keep the menu shortcut and failure status in sync with the player."""
        state = self._player.state_for(self.tx.id)
        self.play_action.setText("Pause" if state == PLAYING else "Play original recording")
        error = self._player.last_error.get(self.tx.id, "")
        if error:
            self.status_label.setText(f"Could not play: {error}")
            self.status_label.setToolTip(error)
            self._playback_error_shown = True
        elif getattr(self, "_playback_error_shown", False):
            # The error is gone (a retry succeeded): the line goes back to
            # describing the message, not a failure that is no longer true.
            self._playback_error_shown = False
            self.status_label.setToolTip("")
            self.update_from(self.tx)

    # -- actions ---------------------------------------------------------

    def _edit(self) -> None:
        if self.editing:
            self.original_label.setFocus()
            return
        if not self.edit_action.isEnabled():
            return
        if self.request_editor is not None and not self.request_editor(self.tx.id):
            return
        self.selectRequested.emit(self.tx.id)
        self.editing = True
        self.editingChanged.emit(self.tx.id)
        self.original_label.set_editable(True)
        self.original_label.setText(self.tx.display_transcript)
        self.original_label.show()
        self.transcript_box.show()
        self.transcript_caption.setText("EDIT TRANSCRIPT")
        self.translated_label.set_editable(True)
        self.translated_label.setText(self.tx.display_translation)
        self.translated_label.show()
        self.translation_box.setVisible(bool(self.tx.display_translation) or
                                        _differs(self.tx.source_language, self.tx.target_language))
        self.translation_caption.setText("EDIT TRANSLATION")
        self.edit_button.hide()
        self.approve_button.hide()
        self.save_edit_button.show()
        self.cancel_edit_button.show()
        self.original_label.setFocus()

    def _save_edit(self) -> None:
        if not self.editing:
            return
        transcript = self.original_label.toPlainText().strip()
        translation = self.translated_label.toPlainText().strip()
        self.correctionRequested.emit(self.tx.id, transcript, translation)
        self.cancel_edit()

    def cancel_edit(self) -> None:
        if not self.editing:
            return
        self.editing = False
        self.original_label.set_editable(False)
        self.translated_label.set_editable(False)
        self.save_edit_button.hide()
        self.cancel_edit_button.hide()
        self.edit_button.show()
        self.update_from(self.tx)
        self.editingChanged.emit(None)

    def _edit_tags(self) -> None:
        dialog = TagEditor(self.tx.tags, self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            self.tagsChanged.emit(self.tx.id, dialog.tags())

    def _edit_note(self) -> None:
        text, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "Note", "Note for this transmission:", self.tx.notes)
        if ok:
            self.noteChanged.emit(self.tx.id, text)

    def _export(self) -> None:
        self.exportRequested.emit(self.tx.id)

    def _toggle_bookmark(self) -> None:
        self.bookmarkToggled.emit(self.tx.id, self.bookmark_action.isChecked())

    def _play_decoded(self) -> None:
        decoded = self.tx.decoded_audio_path
        if decoded:
            self._player.play(self.tx.id, decoded)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))



#: How many turns of the event loop an anchor is re-applied for. Enough to
#: cover a wrapped label settling its height; small enough that it is over
#: before a person could possibly have scrolled.
_ANCHOR_SETTLE_PASSES = 3


class TimelineView(QtWidgets.QScrollArea):
    """The message thread: newest transmission at the top, older below.

    Two things follow from newest-first, and the second is the one that
    matters. New traffic is inserted *above* everything already on screen -
    so if the operator has scrolled down to read something from twenty
    minutes ago, an arriving transmission would push what they are reading
    down the screen. It would also do that every time a bubble above them
    grew: Captured to Transcribing to Translating, and again when a
    translation line appears.

    So every mutation that can change the height of anything above the
    viewport runs inside :meth:`_anchored`, which remembers one visible
    bubble and its exact pixel offset and puts it back afterwards. Nothing
    scrolls on its own: there is no follow-the-newest behaviour and no jump
    to the bottom, because both take the operator away from what they chose
    to look at.
    """

    correctionRequested = QtCore.Signal(str, str, str)
    approvalRequested = QtCore.Signal(str)
    editingChanged = QtCore.Signal(object)
    tagsChanged = QtCore.Signal(str, list)
    bookmarkToggled = QtCore.Signal(str, bool)
    retryRequested = QtCore.Signal(str)
    retryAsSpeechRequested = QtCore.Signal(str)       # tx_id: choose a language
    exportRequested = QtCore.Signal(str)
    noteChanged = QtCore.Signal(str, str)
    transcribeAnywayRequested = QtCore.Signal(str)
    analyzeDigitalRequested = QtCore.Signal(str, str)
    removeRequested = QtCore.Signal(str)
    restoreRequested = QtCore.Signal(str)
    #: The selected row changed: its transmission id, or None for none.
    selectionChanged = QtCore.Signal(object)
    #: The selected row's transmission changed in place (bookmark, review,
    #: text): the same row, new content.
    selectedUpdated = QtCore.Signal(str)
    #: set_playback() installed a different controller.
    playbackReplaced = QtCore.Signal(object)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.selected_id: Optional[str] = None
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._container = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)
        self.setWidget(self._container)

        self.editing_id: Optional[str] = None
        self._bubbles: Dict[str, TransmissionBubble] = {}
        #: Newest first, so index 0 in the layout is the most recent.
        self._order: List[str] = []
        self._pending_anchor = None
        self._anchor_passes = 0
        self._anchor_scheduled = False
        self.playback: Optional[PlaybackController] = None
        self._player: Optional[PlaybackController] = None
        self.set_playback(PlaybackController(parent=self))
        self.playback_backend = self.playback.backend_name

        self.empty_label = QtWidgets.QLabel(
            "No transmissions yet.\n\n"
            "Press Start monitoring to listen to the selected input, or use\n"
            "File \u25b8 Replay WAV file to run a recording through the pipeline.\n\n"
            "Every detected transmission is recorded before it is processed,\n"
            "so nothing is lost if transcription is unavailable.")
        self._default_empty_text = self.empty_label.text()
        self.empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_label.setObjectName("emptyState")
        self._layout.insertWidget(0, self.empty_label)

    def clear(self, keep_playback: bool = False) -> None:
        # Leaving a thread stops its audio: nothing keeps playing with its
        # controls out of sight. Changing the filter within the same thread
        # (keep_playback) does not: the window keeps the playing card shown.
        if not keep_playback:
            self.playback.stop()
        self._track_edit(None)
        for bubble in list(self._bubbles.values()):
            self._layout.removeWidget(bubble)
            bubble.deleteLater()
        self._bubbles.clear()
        self._order.clear()
        self.empty_label.show()
        self.select(None)

    def remove(self, tx_id: str) -> bool:
        """Take one bubble out of the thread. Its audio stops first."""
        bubble = self._bubbles.get(tx_id)
        if bubble is None:
            return False
        if self.editing_id == tx_id:
            self._track_edit(None)
        if self.playback.owner == tx_id:
            self.playback.stop()
        with self._anchored():
            self._layout.removeWidget(bubble)
            bubble.hide()
            bubble.deleteLater()
            self._bubbles.pop(tx_id, None)
            if tx_id in self._order:
                self._order.remove(tx_id)
        if not self._bubbles:
            self.empty_label.show()
        if self.selected_id == tx_id:
            self.select(None)
        return True

    def set_playback(self, controller: PlaybackController) -> None:
        """Use *controller* for every bubble here, present and future.

        Only affected cards refresh their menu and playback error. The shared
        transport receives position changes, independent of the row count.
        """
        if self.playback is not None:
            self.playback.changed.disconnect(self._on_playback_changed)
        self.playback = controller
        self._player = controller             # what bubbles are handed
        controller.changed.connect(self._on_playback_changed)
        for bubble in self._bubbles.values():
            bubble._player = controller
        self.playbackReplaced.emit(controller)

    def _on_playback_changed(self, affected) -> None:
        # A playback error can add a status line above the operator's place.
        bubbles = [self._bubbles[tx_id] for tx_id in affected if tx_id in self._bubbles]
        if not bubbles:
            return
        with self._anchored():
            for bubble in bubbles:
                bubble.refresh_pairing()          # a failed play may mean the file went
                bubble._render_playback()

    def count(self) -> int:
        return len(self._bubbles)

    def order(self) -> List[str]:
        """Transmission ids top to bottom. Newest first."""
        return list(self._order)

    def at_top(self) -> bool:
        return self.verticalScrollBar().value() <= 0

    # -- viewport anchoring ---------------------------------------------
    def _anchor(self):
        """The topmost bubble currently visible, and where it sits.

        Returned as (id, offset-from-the-viewport-top). Anchoring on a widget
        rather than a scroll value is what survives an insertion: scroll
        positions are measured from the top of the content, and the top of
        the content is exactly what moves when a bubble is added above.
        """
        if self.at_top():
            return None
        # The container is a child of the viewport, so its coordinates are
        # already what the scroll bar measures: the value is the distance from
        # the top of the content to the top of the viewport.
        viewport_top = self.verticalScrollBar().value()
        for tx_id in self._order:
            bubble = self._bubbles.get(tx_id)
            if bubble is None:
                continue
            bottom = bubble.y() + bubble.height()
            if bottom > viewport_top:
                return tx_id, bubble.y() - viewport_top
        return None

    def _restore(self, anchor) -> None:
        if anchor is None:
            return
        tx_id, offset = anchor
        bubble = self._bubbles.get(tx_id)
        if bubble is None:
            return
        self._container.layout().activate()
        bar = self.verticalScrollBar()
        target = bubble.y() - offset
        bar.setValue(max(bar.minimum(), min(bar.maximum(), target)))

    @contextlib.contextmanager
    def _anchored(self):
        """Keep whatever the operator is reading exactly where it is.

        Restored twice, deliberately. Qt lays a newly inserted widget out on
        the next pass through the event loop, so the geometry read
        immediately after the insertion is still the old one and a single
        correction lands short. The first restore keeps the jump invisible;
        the deferred one, on the very next turn, is the exact correction. If
        the first already landed it, the second is a no-op.
        """
        anchor = self._anchor()
        try:
            yield
        finally:
            self._restore(anchor)
            if anchor is not None:
                # A wrapped label settles its height over a resize round-trip,
                # so one deferred pass is not always enough. Bounded and
                # short: three turns of the event loop, which is microseconds,
                # then it stops for good and never fights a real scroll.
                # One chain of deferred passes at a time: two anchored
                # changes in the same turn (a bubble removed and its
                # replacement placed) used to start two chains, which spent
                # the passes twice as fast - before the scroll area had
                # resized the container to its new content, while a full
                # thread was still squeezed to the old height - and the
                # reading position settled a bubble's height off.
                self._pending_anchor = anchor
                self._anchor_passes = _ANCHOR_SETTLE_PASSES
                self._schedule_anchor_pass()

    def _schedule_anchor_pass(self) -> None:
        if not self._anchor_scheduled:
            self._anchor_scheduled = True
            QtCore.QTimer.singleShot(0, self._reapply_anchor)

    def _reapply_anchor(self) -> None:
        self._anchor_scheduled = False
        if self._pending_anchor is None or self._anchor_passes <= 0:
            self._pending_anchor = None
            return
        self._anchor_passes -= 1
        self._restore(self._pending_anchor)
        self._schedule_anchor_pass()

    def add(self, tx: Transmission, scroll: bool = False) -> TransmissionBubble:
        if tx.id in self._bubbles:
            self.update(tx)
            return self._bubbles[tx.id]
        # Position 0: newest at the top. The stretch stays last so the older
        # bubbles stack downwards and a short thread does not float.
        return self._insert(0, tx)

    def _insert(self, index: int, tx: Transmission) -> TransmissionBubble:
        """One new bubble at *index*, under the viewport anchor. The single
        insertion matters: a bubble inserted and then moved is laid out
        between the two, and the anchor taken for the move reads stale
        geometry, so the reading position lands a bubble's height off."""
        self.empty_label.hide()
        bubble = TransmissionBubble(tx, self._player, follow_player=False)
        bubble.start_original = self._start_original
        bubble.request_editor = self._allow_edit
        bubble.editingChanged.connect(self._track_edit)
        bubble.approvalRequested.connect(self.approvalRequested)
        bubble.correctionRequested.connect(self.correctionRequested)
        bubble.tagsChanged.connect(self.tagsChanged)
        bubble.bookmarkToggled.connect(self.bookmarkToggled)
        bubble.retryRequested.connect(self.retryRequested)
        bubble.retryAsSpeechRequested.connect(self.retryAsSpeechRequested)
        bubble.exportRequested.connect(self.exportRequested)
        bubble.noteChanged.connect(self.noteChanged)
        bubble.transcribeAnywayRequested.connect(self.transcribeAnywayRequested)
        bubble.analyzeDigitalRequested.connect(self.analyzeDigitalRequested)
        bubble.removeRequested.connect(self.removeRequested)
        bubble.restoreRequested.connect(self.restoreRequested)
        bubble.selectRequested.connect(self.select)
        with self._anchored():
            self._layout.insertWidget(index, bubble)
            self._order.insert(index, tx.id)
            self._bubbles[tx.id] = bubble
        return bubble

    def place(self, tx: Transmission) -> TransmissionBubble:
        """Show this transmission where its time puts it.

        Present: updated in place. Absent: inserted in newest-first order by
        (started_at, id) - the order set_transmissions uses - under the same
        anchoring every other growth gets. For a filtered view, where a
        record that has just come to qualify is not necessarily the newest
        thing on screen; add() is for live traffic, which always is.
        """
        if tx.id in self._bubbles:
            self.update(tx)
            return self._bubbles[tx.id]
        key = (tx.started_at, tx.id)
        index = len(self._order)                     # older than everything shown
        for position, other_id in enumerate(self._order):
            other = self._bubbles[other_id].tx
            if key > (other.started_at, other.id):
                index = position
                break
        return self._insert(index, tx)

    def append_older(self, tx: Transmission) -> TransmissionBubble:
        """Add a bubble at the bottom - for loading history, not new traffic."""
        if tx.id in self._bubbles:
            self.update(tx)
            return self._bubbles[tx.id]
        # Straight into its final place, below every bubble shown. Before,
        # it was inserted at the top and then removed and re-inserted at
        # the bottom: three layout operations per record of history.
        return self._insert(len(self._order), tx)

    def update(self, tx: Transmission) -> None:
        bubble = self._bubbles.get(tx.id)
        if bubble is None:
            self.add(tx)
            return
        # A bubble growing - Transcribing, then a translation line appearing -
        # pushes everything below it down. If that bubble is above what the
        # operator is reading, the text under their eyes would move.
        with self._anchored():
            bubble.update_from(tx)
        if tx.id == self.selected_id:
            self.selectedUpdated.emit(tx.id)

    def set_transmissions(self, transmissions: List[Transmission],
                          keep_playback: bool = False) -> None:
        """Replace the thread. ``transmissions`` may be in either order.

        Sorted here rather than trusted, because the callers differ: the
        thread wants newest-first, while search and export have their own
        deliberate orders and must not be quietly re-sorted by a view.
        """
        if self.editing_id and any(tx.id == self.editing_id for tx in transmissions):
            # Keep the actual editor, selection, cursor and undo history on
            # a refresh of this view. Removed results still leave the view.
            remaining = {tx.id for tx in transmissions}
            for tx_id in self.order():
                if tx_id not in remaining:
                    self.remove(tx_id)
            for tx in transmissions:
                self.place(tx)
            return
        self.clear(keep_playback=keep_playback)
        newest_first = sorted(
            transmissions, key=lambda t: (t.started_at, t.id), reverse=True)
        for tx in newest_first:
            self.append_older(tx)
        # Open at the newest transmission, which is the top.
        self.scroll_to_top()

    # -- selection -----------------------------------------------------------
    def select(self, tx_id: Optional[str], reveal: bool = False) -> None:
        """Make *tx_id* the selected row (None: no selection).

        Only rows that are shown can be selected; asking for one that is not
        clears the selection rather than pointing at something invisible.
        Selecting never scrolls - nothing in this view moves on its own -
        unless *reveal* asks for it: the keyboard moving the selection to a
        row that may be out of sight.
        """
        if tx_id is not None and tx_id not in self._bubbles:
            tx_id = None
        if tx_id == self.selected_id:
            return
        previous = self._bubbles.get(self.selected_id) if self.selected_id else None
        if previous is not None:
            previous.set_selected(False)
        self.selected_id = tx_id
        bubble = self._bubbles.get(tx_id) if tx_id else None
        if bubble is not None:
            bubble.set_selected(True)
            if reveal:
                self.ensureWidgetVisible(bubble, 0, 0)
        self.selectionChanged.emit(tx_id)

    def _allow_edit(self, tx_id: str) -> bool:
        if self.editing_id and self.editing_id != tx_id:
            current = self._bubbles.get(self.editing_id)
            if current is not None:
                self.select(self.editing_id, reveal=True)
                self.ensureWidgetVisible(current)
                current.original_label.setFocus()
                QtWidgets.QToolTip.showText(
                    QtGui.QCursor.pos(), "Save or cancel this edit first.", current)
                return False
        return True

    def _track_edit(self, tx_id) -> None:
        if tx_id != self.editing_id:
            self.editing_id = tx_id
            self.editingChanged.emit(tx_id)

    def set_empty_text(self, text: Optional[str]) -> None:
        """What an empty view says (None: the thread's own welcome)."""
        self.empty_label.setText(text or self._default_empty_text)

    #: Set by the window: plays an original and continues with newer ones.
    original_starter = None

    def _start_original(self, tx_id: str, path: str) -> None:
        if self.original_starter is not None:
            self.original_starter(tx_id, path)
        else:
            self._player.play(tx_id, path)

    def mark_now_playing(self, tx_id: Optional[str], reveal: bool = True) -> None:
        """Highlight the card playback is on and bring it into view - at each
        new clip, even if the operator had scrolled away. The scroll runs
        once layout has settled; a later call (the next clip, Stop, a
        Session change) makes a pending one stale."""
        previous = getattr(self, "_now_playing", None)
        if previous and previous in self._bubbles and previous != tx_id:
            old = self._bubbles[previous]
            old.setProperty("nowPlaying", False)
            old.style().unpolish(old)
            old.style().polish(old)
        self._now_playing = tx_id
        self._reveal_token = getattr(self, "_reveal_token", 0) + 1
        bubble = self._bubbles.get(tx_id) if tx_id else None
        if bubble is None:
            return
        bubble.setProperty("nowPlaying", True)
        bubble.style().unpolish(bubble)
        bubble.style().polish(bubble)
        if reveal:
            token = self._reveal_token
            QtCore.QTimer.singleShot(0, lambda: self._reveal_now_playing(token))

    def _reveal_now_playing(self, token: int, tries: int = 10) -> None:
        if token != getattr(self, "_reveal_token", 0):
            return                            # superseded: Stop, a new clip, a new view
        if self._pending_anchor is not None and self._anchor_passes > 0 and tries > 0:
            # Let the reading-position passes for this change settle first,
            # so the card's geometry is final - then move deliberately.
            QtCore.QTimer.singleShot(0, lambda: self._reveal_now_playing(token, tries - 1))
            return
        bubble = self._bubbles.get(self._now_playing or "")
        if bubble is not None:
            # A deliberate move: no earlier anchor may put the view back.
            self._pending_anchor = None
            self.ensureWidgetVisible(bubble, 0, 0)

    @property
    def now_playing(self) -> Optional[str]:
        return getattr(self, "_now_playing", None)

    def selected_transmission(self) -> Optional[Transmission]:
        bubble = self._bubbles.get(self.selected_id) if self.selected_id else None
        return bubble.tx if bubble is not None else None

    def redraw_times(self) -> None:
        """Re-render every bubble's times after the display zone changed.
        The rows themselves are unchanged."""
        for bubble in list(self._bubbles.values()):
            bubble.update_from(bubble.tx)

    def bubble(self, tx_id: str) -> Optional[TransmissionBubble]:
        return self._bubbles.get(tx_id)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        """Up/Down move the selection through the list, newest at the top."""
        key = event.key()
        if key in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down) and self._order:
            if self.selected_id in self._order:
                index = self._order.index(self.selected_id)
                index += -1 if key == QtCore.Qt.Key_Up else 1
                index = max(0, min(len(self._order) - 1, index))
            else:
                index = 0
            target = self._bubbles[self._order[index]]
            self.select(self._order[index], reveal=True)
            target.setFocus(QtCore.Qt.OtherFocusReason)
            event.accept()
            return
        super().keyPressEvent(event)

    def scroll_to_top(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.minimum())
