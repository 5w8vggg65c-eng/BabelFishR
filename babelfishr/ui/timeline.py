"""The chat-style timeline: one bubble per received transmission."""

from __future__ import annotations

import contextlib
import pathlib
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..analysis.dsd import AUTO_ROTATION_SECONDS
from ..analysis.dsd import PRESETS as DSD_PRESETS
from ..models import (ContentClass, ProcessingState, Provenance,
                      Transmission)
from .playback import (LONG_RECORDING_SECONDS, PAUSED, PLAYING, SKIP_MS,
                       PlaybackController, format_clock)
from .widgets import TagEditor

CONTENT_LABELS = {
    "speech": "speech",
    "noise": "noise / static",
    "tone": "tone only",
    "digital-suspected": "possibly digital",
    "unknown": "unclassified",
}

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


def _local_stamp(when) -> str:
    """``2026-09-07 10:41:20``: the local date and time of one transmission.

    The header used to show only the time, which is enough while reading
    today's traffic and misleading for anything older - a bubble from last
    week looked exactly like one from an hour ago.
    """
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S")


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


class TransmissionBubble(QtWidgets.QFrame):
    """One received transmission, rendered as a chat bubble.

    Original and translated text are always shown as separate, labelled rows -
    the operator must never be left guessing which one they are reading.
    """

    correctionRequested = QtCore.Signal(str, str, str)   # tx_id, transcript, translation
    tagsChanged = QtCore.Signal(str, list)
    bookmarkToggled = QtCore.Signal(str, bool)
    retryRequested = QtCore.Signal(str)
    exportRequested = QtCore.Signal(str)
    noteChanged = QtCore.Signal(str, str)
    transcribeAnywayRequested = QtCore.Signal(str)
    analyzeDigitalRequested = QtCore.Signal(str, str)   # tx_id, protocol

    def __init__(self, tx: Transmission, player: PlaybackController,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.tx = tx
        self._player = player
        self._player.changed.connect(self._render_playback)
        self._player.positionChanged.connect(self._on_playback_position)
        self.setObjectName("bubble")
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        self.header = QtWidgets.QLabel()
        self.header.setObjectName("bubbleHeader")
        outer.addWidget(self.header)

        self.provisional = QtWidgets.QLabel()
        self.provisional.setObjectName("provisional")
        self.provisional.setWordWrap(True)
        self.provisional.hide()
        outer.addWidget(self.provisional)

        self.original_label = QtWidgets.QLabel()
        self.original_label.setObjectName("originalText")
        self.original_label.setWordWrap(True)
        self.original_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        outer.addWidget(self.original_label)

        self.translated_label = QtWidgets.QLabel()
        self.translated_label.setObjectName("translatedText")
        self.translated_label.setWordWrap(True)
        self.translated_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        outer.addWidget(self.translated_label)

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

        # Metadata chips: compact, scannable, and readable without colour.
        self.chip_row = QtWidgets.QHBoxLayout()
        self.chip_row.setSpacing(5)
        self.chip_row.addStretch(1)
        outer.addLayout(self.chip_row)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)

        # A compact Play on every bubble that has a recording - the operator
        # asked for it. Longer recordings expand a control bar across the
        # bottom of this bubble while they play; short ones play straight
        # through. Still a text chat bubble: no waveform, no analyser.
        self.play_button = QtWidgets.QToolButton()
        self.play_button.setText("\u25b6 Play")
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
        controls.addWidget(self.status_label)

        self.menu_button = QtWidgets.QToolButton()
        self.menu_button.setText("\u22ef")
        self.menu_button.setToolTip("More actions")
        self.menu_button.setAccessibleName("More actions")
        self.menu_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.menu_button.setMenu(self._build_menu())
        controls.addWidget(self.menu_button)
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
        outer.addWidget(self.playback_bar)

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
        self.analyze_action = menu.addAction(
            "Analyze as digital (hunt all profiles)",
            lambda: self.analyzeDigitalRequested.emit(self.tx.id, "auto"))
        self.analyze_action.setToolTip(
            f"Tries every profile in turn. A full rotation takes about "
            f"{int(AUTO_ROTATION_SECONDS)} seconds at 48 kHz, so a shorter "
            f"recording may finish before the right profile is reached - "
            f"pick a specific preset below if you know the mode.")

        protocols = QtWidgets.QMenu("Analyze as a specific mode...", menu)
        for preset in DSD_PRESETS:
            if preset.id == "auto":
                continue
            action = protocols.addAction(
                preset.label,
                lambda checked=False, name=preset.id:
                self.analyzeDigitalRequested.emit(self.tx.id, name))
            action.setToolTip(preset.describe())
        self.protocol_menu = menu.addMenu(protocols)
        menu.addSeparator()
        menu.addAction("Retry processing",
                       lambda: self.retryRequested.emit(self.tx.id))
        menu.addAction("Export audio...", self._export)
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

        # The transcript is the message. It is the primary content of the
        # bubble, in plain words, with no engine or language prefix competing
        # with it - the language already appears in the header line.
        original = tx.display_transcript
        if original:
            corrected = (" <i>(edited)</i>" if tx.transcript_correction else "")
            self.original_label.setText(f"{_escape(original)}{corrected}")
            self.original_label.show()
        elif tx.state in _IN_FLIGHT:
            # Same bubble, not a separate placeholder row: the operator sees
            # one line per transmission that fills itself in.
            self.original_label.setText(
                f"<i>{_IN_FLIGHT[tx.state]}</i>")
            self.original_label.show()
        else:
            self.original_label.hide()

        # A translation row only when there is genuinely another language to
        # read. "already in English" under an English transcript is a line of
        # noise in every bubble of an English-speaking operator's thread.
        translated = tx.display_translation
        if translated and _differs(tx.source_language, tx.target_language):
            corrected = " <i>(edited)</i>" if tx.translation_correction else ""
            self.translated_label.setText(
                f"<b>{_escape(tx.target_language)}:</b> "
                f"{_escape(translated)}{corrected}")
            self.translated_label.show()
        else:
            self.translated_label.hide()

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
        if pending:
            status_bits.append(pending)
        if tx.needs_review and tx.state is not ProcessingState.FAILED:
            status_bits.append("needs review")
        self.status_label.setText("  ·  ".join(status_bits))

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
        self.analyze_action.setEnabled(bool(tx.audio_path))
        self.protocol_menu.setEnabled(bool(tx.audio_path))

        self.decoded_action.setVisible(bool(tx.decoded_audio_path))

        self.play_action.setEnabled(bool(tx.audio_path))
        # The menu's Play/Pause label, the buttons and the bar are all drawn
        # from the controller in one place.
        self._render_playback()

    def _rebuild_chips(self, tx: Transmission) -> None:
        """Compact metadata chips: class, confidence, tags, digital result."""
        while self.chip_row.count() > 1:
            item = self.chip_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        chips: List[tuple] = []
        label = CONTENT_LABELS.get(tx.content_class.value, tx.content_class.value)
        if tx.content_class is not ContentClass.SPEECH:
            chips.append((label, "warning"))
        if tx.transcript_confidence is not None and tx.transcript:
            tone = "warning" if tx.transcript_confidence < 0.6 else "plain"
            chips.append((f"confidence {tx.transcript_confidence:.0%}", tone))
        if tx.clipped:
            chips.append(("clipped", "error"))
        for tag in tx.tags:
            chips.append((f"#{tag}", "accent"))
        if tx.bookmarked:
            chips.append(("bookmarked", "accent"))

        attempt = tx.latest_analysis
        if attempt is not None:
            tone = "accent" if attempt.outcome.is_success else "warning"
            chips.append((f"DSD: {attempt.summary()}", tone))

        for index, (text, tone) in enumerate(chips):
            chip = QtWidgets.QLabel(text)
            chip.setObjectName("chip")
            chip.setProperty("tone", tone)
            chip.setAccessibleName(text)
            self.chip_row.insertWidget(index, chip)

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
            self.play_button.setText("\u25b6 Play")
            self.play_button.setEnabled(True)
            self.play_button.show()
            self.play_action.setText("Play original recording")
        if error:
            self.status_label.setText(f"Could not play: {error}")
            self.status_label.setToolTip(error)

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
    exportRequested = QtCore.Signal(str)
    noteChanged = QtCore.Signal(str, str)
    transcribeAnywayRequested = QtCore.Signal(str)
    analyzeDigitalRequested = QtCore.Signal(str, str)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._container = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(self._container)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self.setWidget(self._container)

        self._bubbles: Dict[str, TransmissionBubble] = {}
        #: Newest first, so index 0 in the layout is the most recent.
        self._order: List[str] = []
        self._pending_anchor = None
        self._anchor_passes = 0
        self.playback = PlaybackController(parent=self)
        self._player = self.playback          # what bubbles are handed
        self.playback_backend = self.playback.backend_name
        # Expanding or collapsing a control bar changes a bubble's height, so
        # it happens inside the same anchoring every other growth does: the
        # operator's reading position does not move.
        self.playback.changed.connect(self._on_playback_changed)

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
        return True

    def _on_playback_changed(self) -> None:
        with self._anchored():
            for bubble in self._bubbles.values():
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
                self._pending_anchor = anchor
                self._anchor_passes = _ANCHOR_SETTLE_PASSES
                QtCore.QTimer.singleShot(0, self._reapply_anchor)

    def _reapply_anchor(self) -> None:
        if self._pending_anchor is None or self._anchor_passes <= 0:
            self._pending_anchor = None
            return
        self._anchor_passes -= 1
        self._restore(self._pending_anchor)
        QtCore.QTimer.singleShot(0, self._reapply_anchor)

    def add(self, tx: Transmission, scroll: bool = False) -> TransmissionBubble:
        if tx.id in self._bubbles:
            self.update(tx)
            return self._bubbles[tx.id]
        self.empty_label.hide()
        bubble = TransmissionBubble(tx, self._player)
        bubble.correctionRequested.connect(self.correctionRequested)
        bubble.tagsChanged.connect(self.tagsChanged)
        bubble.bookmarkToggled.connect(self.bookmarkToggled)
        bubble.retryRequested.connect(self.retryRequested)
        bubble.exportRequested.connect(self.exportRequested)
        bubble.noteChanged.connect(self.noteChanged)
        bubble.transcribeAnywayRequested.connect(self.transcribeAnywayRequested)
        bubble.analyzeDigitalRequested.connect(self.analyzeDigitalRequested)

        # Position 0: newest at the top. The stretch stays last so the older
        # bubbles stack downwards and a short thread does not float.
        with self._anchored():
            self._layout.insertWidget(0, bubble)
            self._order.insert(0, tx.id)
            self._bubbles[tx.id] = bubble
        return bubble

    def append_older(self, tx: Transmission) -> TransmissionBubble:
        """Add a bubble at the bottom - for loading history, not new traffic."""
        if tx.id in self._bubbles:
            self.update(tx)
            return self._bubbles[tx.id]
        bubble = self.add(tx)
        with self._anchored():
            self._layout.removeWidget(bubble)
            self._layout.insertWidget(self._layout.count() - 1, bubble)
            self._order.remove(tx.id)
            self._order.append(tx.id)
        return bubble

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

    def scroll_to_top(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.minimum())
