"""The chat-style timeline: one bubble per received transmission."""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..models import ContentClass, ProcessingState, Transmission
from .widgets import TagEditor, WaveformWidget

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


class _Player:
    """Audio playback via QtMultimedia, falling back to the system player.

    QtMultimedia ships in PySide6-Addons; on a minimal install it is absent, and
    handing the file to the OS is better than a dead button.
    """

    def __init__(self):
        self._player = None
        self._output = None
        self.backend = "system"
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self._player = QMediaPlayer()
            self._output = QAudioOutput()
            self._player.setAudioOutput(self._output)
            self.backend = "qtmultimedia"
        except Exception:  # noqa: BLE001
            self._player = None

    @property
    def available(self) -> bool:
        return True  # one path or the other always works

    def play(self, path: str) -> None:
        if self._player is not None:
            self._player.setSource(QtCore.QUrl.fromLocalFile(str(path)))
            self._player.play()
            return
        opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
        try:
            subprocess.Popen([opener, str(path)], shell=(sys.platform == "win32"))
        except OSError:
            pass

    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()

    def is_playing(self) -> bool:
        if self._player is None:
            return False
        try:
            from PySide6.QtMultimedia import QMediaPlayer

            return self._player.playbackState() == QMediaPlayer.PlayingState
        except Exception:  # noqa: BLE001
            return False


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

    def __init__(self, tx: Transmission, player: _Player,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.tx = tx
        self._player = player
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

        self.waveform = WaveformWidget()
        outer.addWidget(self.waveform)

        # Metadata chips: compact, scannable, and readable without colour.
        self.chip_row = QtWidgets.QHBoxLayout()
        self.chip_row.setSpacing(5)
        self.chip_row.addStretch(1)
        outer.addLayout(self.chip_row)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)

        self.play_button = QtWidgets.QToolButton()
        self.play_button.setText("Play")
        self.play_button.setToolTip("Play the original recording")
        self.play_button.setAccessibleName("Play original recording")
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_button)

        self.decoded_button = QtWidgets.QToolButton()
        self.decoded_button.setText("Play decoded")
        self.decoded_button.setToolTip("Play the audio decoded by DSD-neo")
        self.decoded_button.setAccessibleName("Play decoded audio")
        self.decoded_button.clicked.connect(self._play_decoded)
        self.decoded_button.hide()
        controls.addWidget(self.decoded_button)

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

        self.update_from(tx)

    def _build_menu(self) -> QtWidgets.QMenu:
        """Secondary actions, out of the way but one click deep."""
        menu = QtWidgets.QMenu(self)
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
            "Analyze as digital", lambda: self.analyzeDigitalRequested.emit(
                self.tx.id, ""))

        protocols = QtWidgets.QMenu("Reanalyze as...", menu)
        for protocol in ("DMR", "P25 Phase 1", "P25 Phase 2", "D-STAR",
                         "NXDN", "System Fusion", "dPMR"):
            protocols.addAction(
                protocol,
                lambda checked=False, name=protocol:
                self.analyzeDigitalRequested.emit(self.tx.id, name))
        self.protocol_menu = menu.addMenu(protocols)
        menu.addSeparator()
        menu.addAction("Retry processing",
                       lambda: self.retryRequested.emit(self.tx.id))
        menu.addAction("Export audio...", self._export)
        return menu

    # -- rendering -------------------------------------------------------
    def update_from(self, tx: Transmission) -> None:
        self.tx = tx
        meta: List[str] = [tx.started_at.astimezone().strftime("%H:%M:%S"),
                           f"{tx.duration:.1f}s"]
        if tx.channel_name:
            meta.append(tx.channel_name)
        if tx.frequency_mhz is not None:
            # Never let a typed value read as a measurement.
            suffix = "" if tx.frequency_is_measured else " (entered)"
            meta.append(f"{tx.frequency_mhz:.4f} MHz{suffix}")
        if tx.source_language:
            language = tx.source_language
            if tx.language_confidence is not None:
                language += f" {tx.language_confidence:.0%}"
            meta.append(language)
        if tx.clipped:
            meta.append("CLIPPED")
        self.header.setText("  ·  ".join(meta))

        original = tx.display_transcript
        if original:
            corrected = " (edited)" if tx.transcript_correction else ""
            label = tx.source_language or "original"
            self.original_label.setText(
                f"<b>{label}{corrected}:</b> {_escape(original)}")
            self.original_label.show()
        else:
            self.original_label.hide()

        translated = tx.display_translation
        if translated:
            corrected = " (edited)" if tx.translation_correction else ""
            self.translated_label.setText(
                f"<b>{tx.target_language}{corrected}:</b> {_escape(translated)}")
            self.translated_label.show()
        elif tx.transcript and tx.source_language == tx.target_language:
            self.translated_label.setText(
                f"<i>already in {tx.target_language}</i>")
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

        decoded = tx.decoded_audio_path
        self.decoded_button.setVisible(bool(decoded))

        self.play_button.setEnabled(bool(tx.audio_path))
        if tx.audio_path and self.waveform._peaks is None:
            self.waveform.load_file(tx.audio_path)

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

    # -- actions ---------------------------------------------------------
    def _toggle_play(self) -> None:
        if not self.tx.audio_path:
            return
        if self._player.is_playing():
            self._player.pause()
            self.play_button.setText("Play")
        else:
            self._player.play(self.tx.audio_path)
            self.play_button.setText("Pause")

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
            self._player.play(decoded)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class TimelineView(QtWidgets.QScrollArea):
    """Scrolling, chronological list of transmission bubbles."""

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
        self._player = _Player()
        self.playback_backend = self._player.backend

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
        for bubble in list(self._bubbles.values()):
            self._layout.removeWidget(bubble)
            bubble.deleteLater()
        self._bubbles.clear()
        self.empty_label.show()

    def count(self) -> int:
        return len(self._bubbles)

    def add(self, tx: Transmission, scroll: bool = True) -> TransmissionBubble:
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
        # Keep the stretch last so bubbles stack from the top.
        self._layout.insertWidget(self._layout.count() - 1, bubble)
        self._bubbles[tx.id] = bubble
        if scroll:
            QtCore.QTimer.singleShot(0, self._scroll_to_bottom)
        return bubble

    def update(self, tx: Transmission) -> None:
        bubble = self._bubbles.get(tx.id)
        if bubble is None:
            self.add(tx)
            return
        bubble.update_from(tx)

    def set_transmissions(self, transmissions: List[Transmission]) -> None:
        self.clear()
        for tx in transmissions:
            self.add(tx, scroll=False)
        QtCore.QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
