"""The chat-style timeline: one bubble per received transmission."""

from __future__ import annotations

import contextlib
import pathlib
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..models import (ContentClass, ProcessingState, Provenance,
                      Transmission)
from .playback import (LONG_RECORDING_SECONDS, PAUSED, PLAYING, SKIP_MS,
                       PlaybackController, format_clock)
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
        return ("You checked and saved this transcript. The automatic speech "
                "check had marked it "
                + ("probably not speech." if verdict == "likely_noise" else "uncertain."))
    confirmed = metadata.get("speech_confirmed") or {}
    if confirmed:
        language = confirmed.get("source_language", "?")
        said = f"You marked this as speech in {language}"
        check_words = (" Check the new transcript against the audio; saving it with "
                       "Edit transcript and translation marks it reviewed.")
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
                f"Retry as speech in... from the message menu: it is transcribed "
                f"again in the language you pick, then translated.")
    return (f"Speech uncertain{said}. Check the transcript against the "
            f"original audio; Retry as speech in... re-runs it in a language "
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
        return words + "You have since checked and saved this message."
    # No shortcut in the prose: Qt shows Ctrl as Command on macOS, and the
    # menu item itself shows the right key.
    return words + ("The recording and transcript are kept. If this is "
                    "speech, choose its language with Message > Retry as "
                    "speech in...")


def operator_checked(tx: Transmission) -> bool:
    """The operator saved this message's transcript/translation (Edit
    transcript and translation) and it has not been re-recognised since.
    Not a note, and not Retry as speech: those leave the words unchecked."""
    return bool(tx.reviewed and tx.has_correction)


def _transcript_caption(tx: Transmission) -> tuple:
    """The transcript box's caption and its tone, from the speech check.

    A transcript the speech check held back (``likely_noise``) is not
    presented as ordinary output: its caption says it is held for review.
    ``uncertain`` - or a held one the operator has since marked as speech -
    gets the milder "check" marker. The words themselves are always shown.
    """
    metadata = tx.signal_metadata or {}
    verdict = (metadata.get("speech_check") or {}).get("verdict")
    if operator_checked(tx):
        return "TRANSCRIPT \u00b7 CHECKED BY OPERATOR", "none"
    if verdict == "likely_noise" and not metadata.get("speech_confirmed"):
        return "TRANSCRIPT \u00b7 HELD FOR REVIEW", "held"
    if verdict in ("likely_noise", "uncertain"):
        return "TRANSCRIPT \u00b7 CHECK", "check"
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
        if follow_player:
            # A bubble on its own listens to the controller itself. Inside a
            # TimelineView it does not: the view hands each change to the
            # bubbles it concerns, so a notification costs work in proportion
            # to the recordings affected, not to the length of the thread.
            self._player.changed.connect(self._on_player_changed)
            self._player.positionChanged.connect(self._on_playback_position)
        self.setObjectName("bubble")
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)

        self.setProperty("selected", False)
        # A row in the review list: selectable by click or keyboard, and the
        # selection is what the waveform and Selected Audio panel follow.
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setAccessibleName("Message")

        # Columns, as the v5 review list: TIME | MESSAGE | REVIEW. The
        # MESSAGE column holds the transcript in its own labelled box at the
        # full width of the text area, the translation (when there is one)
        # in a second box under it, and the compact metadata, chips, notes
        # and controls below both. Row 1, the playback bar, spans the whole
        # row: under the text column alone its buttons would set a minimum
        # width wider than a narrow list, widening every row past the view.
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

        # A compact Play on every bubble that has a recording - the operator
        # asked for it. Longer recordings expand a control bar across the
        # bottom of this bubble while they play; short ones play straight
        # through. Still a text chat bubble: no waveform, no analyser.
        self.play_button = QtWidgets.QToolButton()
        self.play_button.setObjectName("playButton")
        self.play_button.setText("Play original")
        self.play_button.setToolTip("Play this recording")
        self.play_button.setAccessibleName("Play recording")
        self.play_button.clicked.connect(self._on_play_clicked)
        controls.addWidget(self.play_button)

        # Primary recovery action stays visible; everything else is in the menu.
        self.action_button = QtWidgets.QToolButton()
        self.action_button.setText("Transcribe anyway")
        self.action_button.setAccessibleName("Transcribe this recording anyway")
        self.action_button.clicked.connect(
            lambda: self.transcribeAnywayRequested.emit(self.tx.id))
        self.action_button.hide()
        controls.addWidget(self.action_button)

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

        # The expanded bar: Play/Pause, Stop, rewind, fast-forward, position.
        # Hidden until a longer recording is playing; Stop hides it again.
        self.playback_bar = QtWidgets.QWidget()
        self.playback_bar.setObjectName("playbackBar")
        bar = QtWidgets.QHBoxLayout(self.playback_bar)
        bar.setContentsMargins(0, 2, 0, 0)
        bar.setSpacing(6)
        skip_seconds = SKIP_MS // 1000
        self.rewind_button = QtWidgets.QToolButton()
        self.rewind_button.setText(f"\u23ea {skip_seconds} s")
        self.rewind_button.setToolTip(f"Back {skip_seconds} seconds")
        self.rewind_button.setAccessibleName(f"Rewind {skip_seconds} seconds")
        self.rewind_button.clicked.connect(self._on_rewind_clicked)
        bar.addWidget(self.rewind_button)
        self.pause_button = QtWidgets.QToolButton()
        self.pause_button.setText("\u23f8 Pause")
        self.pause_button.setAccessibleName("Pause or resume")
        self.pause_button.clicked.connect(self._on_pause_clicked)
        bar.addWidget(self.pause_button)
        self.stop_button = QtWidgets.QToolButton()
        self.stop_button.setText("\u23f9 Stop")
        self.stop_button.setToolTip("Stop playback and hide these controls")
        self.stop_button.setAccessibleName("Stop playback")
        self.stop_button.clicked.connect(self._on_stop_clicked)
        bar.addWidget(self.stop_button)
        self.forward_button = QtWidgets.QToolButton()
        self.forward_button.setText(f"\u23e9 {skip_seconds} s")
        self.forward_button.setToolTip(f"Forward {skip_seconds} seconds")
        self.forward_button.setAccessibleName(f"Fast forward {skip_seconds} seconds")
        self.forward_button.clicked.connect(self._on_forward_clicked)
        bar.addWidget(self.forward_button)
        self.position_label = QtWidgets.QLabel("")
        self.position_label.setObjectName("statusText")
        bar.addWidget(self.position_label)
        bar.addStretch(1)
        self.playback_bar.hide()
        grid.addWidget(self.playback_bar, 1, 0, 1, 3)
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
        menu.addAction("Edit transcript and translation...", self._edit)
        menu.addAction("Add or edit note...", self._edit_note)
        menu.addAction("Edit tags...", self._edit_tags)
        self.bookmark_action = menu.addAction("Bookmark")
        self.bookmark_action.setCheckable(True)
        self.bookmark_action.triggered.connect(self._toggle_bookmark)
        menu.addSeparator()
        self.transcribe_action = menu.addAction(
            "Transcribe anyway", lambda: self.transcribeAnywayRequested.emit(
                self.tx.id))
        # Digital analysis belongs to KingFishR, which adds its actions here
        # through babelfishr.extensions; BabelFishR registers none.
        from ..extensions import hooks

        for bubble_menu in hooks("bubble_menu"):
            bubble_menu(self, menu)
        menu.addSeparator()
        menu.addAction("Retry processing",
                       lambda: self.retryRequested.emit(self.tx.id))
        menu.addAction("Retry as speech in...",
                       lambda: self.retryAsSpeechRequested.emit(self.tx.id))
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
        if tx.clipped:
            meta.append("CLIPPED")
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

        # The transcript is the message. It is the primary content of the
        # bubble, in plain words, with no engine or language prefix competing
        # with it - the language already appears in the header line.
        original = tx.display_transcript
        if original:
            # Only the words: "edited" is said in the caption.
            self.original_label.setText(original)
            self.original_label.show()
            self.transcript_box.show()
        elif tx.state in _IN_FLIGHT:
            # Same bubble, not a separate placeholder row: the operator sees
            # one line per transmission that fills itself in.
            self.original_label.setText(_IN_FLIGHT[tx.state])
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
        translated = tx.display_translation
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

        if tx.state is ProcessingState.FAILED and tx.error:
            self.error_label.setText(
                f"⚠ {tx.error.stage} failed: {_escape(tx.error.message)}<br>"
                f"<i>The recording is safe. Use Retry once the cause is fixed.</i>")
            self.error_label.show()
            self.retry_button.show()
        else:
            self.error_label.hide()
            self.retry_button.hide()

        details: List[str] = []
        if tx.skip_reason:
            details.append(_escape(tx.skip_reason))
        speech_note = _speech_check_note(tx)
        if speech_note:
            details.append(_escape(speech_note))
        unresolved_note = _translation_unresolved_note(tx)
        if unresolved_note:
            details.append(_escape(unresolved_note))
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

        status_bits: List[str] = []
        pending = STATE_LABELS.get(tx.state, "")
        if (tx.signal_metadata or {}).get("processing_stopped"):
            # Not "not speech": monitoring was stopped before processing.
            pending = "saved, not processed (monitoring stopped)"
        if pending:
            status_bits.append(pending)
        if (tx.signal_metadata or {}).get("translation_unresolved"):
            status_bits.append("translation not attempted")
        if tx.needs_review and tx.state is not ProcessingState.FAILED:
            status_bits.append("needs review")
        self.status_label.setText("  ·  ".join(status_bits))
        # REVIEW column: the word, never only a colour.
        if tx.state is ProcessingState.FAILED:
            review, tone = "FAILED", "failed"
        elif tx.needs_review:
            review, tone = "CHECK", "check"
        else:
            review, tone = "\u2013", "none"
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
        self.transcribe_action.setEnabled(bool(tx.audio_path))

        self.decoded_action.setVisible(bool(tx.decoded_audio_path))

        self.play_action.setEnabled(bool(tx.audio_path))
        # A removed message, when shown at all, says so and offers only the
        # way back; an ordinary one offers removal.
        self.remove_action.setVisible(not tx.hidden)
        self.restore_action.setVisible(bool(tx.hidden))
        self.setProperty("removed", bool(tx.hidden))
        if tx.hidden:
            self.status_label.setText("Removed from thread \u2014 data kept")
        # The menu's Play/Pause label, the buttons and the bar are all drawn
        # from the controller in one place.
        self._render_playback()

    def _rebuild_chips(self, tx: Transmission) -> None:
        """Compact metadata chips: class, confidence, tags, digital result."""
        while self.chip_row.count() > 0:
            item = self.chip_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        chips: List[tuple] = []
        label = CONTENT_LABELS.get(tx.content_class.value, tx.content_class.value)
        if (tx.content_class is not ContentClass.SPEECH
                and tx.content_class.value not in UNBADGED_CLASSES):
            chips.append((label, "warning"))
        if tx.transcript_confidence is not None and tx.transcript:
            tone = "warning" if tx.transcript_confidence < 0.6 else "plain"
            chips.append((f"confidence {tx.transcript_confidence:.0%}", tone))
        if tx.clipped:
            chips.append(("clipped", "error"))
        segment = (tx.signal_metadata or {}).get("segment") or {}
        if segment.get("continues_previous") and segment.get("continued"):
            chips.append(("continuous audio: middle part", "plain"))
        elif segment.get("continued"):
            chips.append(("continuous audio: continues in the next message", "plain"))
        elif segment.get("continues_previous"):
            chips.append(("continuous audio: last part", "plain"))
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
    @property
    def is_long_recording(self) -> bool:
        return float(self.tx.duration or 0.0) > LONG_RECORDING_SECONDS

    def _on_play_clicked(self) -> None:
        self._player.play(self.tx.id, self.tx.audio_path)

    def _on_pause_clicked(self) -> None:
        self._player.toggle(self.tx.id, self.tx.audio_path)

    def _on_stop_clicked(self) -> None:
        if self._player.owner == self.tx.id:
            self._player.stop()

    def _on_rewind_clicked(self) -> None:
        if self._player.owner == self.tx.id:
            self._player.rewind()

    def _on_forward_clicked(self) -> None:
        if self._player.owner == self.tx.id:
            self._player.forward()

    def _toggle_play(self) -> None:  # noqa: D401 - the menu's Play / Pause
        self._player.toggle(self.tx.id, self.tx.audio_path)

    def _on_playback_position(self, owner: str, position_ms: int,
                              duration_ms: int) -> None:
        if owner == self.tx.id:
            self.position_label.setText(format_clock(position_ms, duration_ms))

    def _on_player_changed(self, affected) -> None:
        if self.tx.id in affected:
            self._render_playback()

    def _render_playback(self) -> None:
        """Draw this bubble's playback controls from the controller's truth.

        Idempotent and cheap: called after every transcript update and after
        every controller change. It reads state; it never changes it, so a
        translation arriving mid-playback cannot reset the recording.
        """
        has_audio = bool(self.tx.audio_path)
        state = self._player.state_for(self.tx.id)
        error = self._player.last_error.get(self.tx.id, "")
        if not has_audio:
            self.play_button.hide()
            self.playback_bar.hide()
            return
        if state == PLAYING or state == PAUSED:
            if self.is_long_recording and self._player.controllable:
                self.play_button.hide()
                self.pause_button.setText(
                    "\u23f8 Pause" if state == PLAYING else "\u25b6 Play")
                self.pause_button.setToolTip(
                    "Pause" if state == PLAYING else "Resume from here")
                if not self.playback_bar.isVisible():
                    position, duration = self._player.position_for(self.tx.id)
                    self.position_label.setText(format_clock(position, duration))
                self.playback_bar.show()
            else:
                # Short: plays through, no extra controls, Play comes back
                # when it finishes. Uncontrollable backend: same shape,
                # because a Pause that pauses nothing must not be offered.
                self.playback_bar.hide()
                self.play_button.setText("Playing\u2026")
                self.play_button.setEnabled(False)
                self.play_button.show()
            self.play_action.setText("Pause" if state == PLAYING
                                     else "Play original recording")
        else:
            self.playback_bar.hide()
            self.play_button.setText("Play original")
            self.play_button.setEnabled(True)
            self.play_button.show()
            self.play_action.setText("Play original recording")
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
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Correct transcript and translation")
        dialog.resize(560, 320)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(
            "<b>Original transcript</b> (the engine's output is preserved "
            "separately)"))
        original = QtWidgets.QPlainTextEdit(self.tx.display_transcript)
        layout.addWidget(original)
        layout.addWidget(QtWidgets.QLabel("<b>Translation</b>"))
        translated = QtWidgets.QPlainTextEdit(self.tx.display_translation)
        layout.addWidget(translated)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            self.correctionRequested.emit(
                self.tx.id, original.toPlainText(), translated.toPlainText())

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
        self.empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_label.setObjectName("emptyState")
        self._layout.insertWidget(0, self.empty_label)

    def clear(self) -> None:
        # Leaving a thread stops its audio: nothing keeps playing with its
        # controls out of sight.
        self.playback.stop()
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

        The view is the controller's only listener on behalf of its bubbles:
        each change names the recordings it concerns and only their bubbles
        are redrawn; each position report goes to the owner's bubble alone.
        Before, every bubble listened for itself and the view redrew every
        bubble as well, so one change redrew a thread of N bubbles twice
        and every position tick reached all N.
        """
        if self.playback is not None:
            self.playback.changed.disconnect(self._on_playback_changed)
            self.playback.positionChanged.disconnect(self._on_playback_position)
        self.playback = controller
        self._player = controller             # what bubbles are handed
        controller.changed.connect(self._on_playback_changed)
        controller.positionChanged.connect(self._on_playback_position)
        for bubble in self._bubbles.values():
            bubble._player = controller
        self.playbackReplaced.emit(controller)

    def _on_playback_changed(self, affected) -> None:
        # Expanding or collapsing a control bar changes a bubble's height, so
        # it happens inside the same anchoring every other growth does: the
        # operator's reading position does not move.
        bubbles = [self._bubbles[tx_id] for tx_id in affected if tx_id in self._bubbles]
        if not bubbles:
            return
        with self._anchored():
            for bubble in bubbles:
                bubble._render_playback()

    def _on_playback_position(self, owner: str, position_ms: int,
                              duration_ms: int) -> None:
        bubble = self._bubbles.get(owner)
        if bubble is not None:
            bubble._on_playback_position(owner, position_ms, duration_ms)

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

    def set_transmissions(self, transmissions: List[Transmission]) -> None:
        """Replace the thread. ``transmissions`` may be in either order.

        Sorted here rather than trusted, because the callers differ: the
        thread wants newest-first, while search and export have their own
        deliberate orders and must not be quietly re-sorted by a view.
        """
        self.clear()
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
