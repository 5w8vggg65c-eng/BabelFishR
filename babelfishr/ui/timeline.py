"""The chat-style timeline: one bubble per received transmission."""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..models import ProcessingState, Transmission
from .widgets import TagEditor, WaveformWidget

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

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(6)
        self.play_button = QtWidgets.QToolButton()
        self.play_button.setText("Play")
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_button)

        for text, slot in (("Edit", self._edit), ("Tag", self._edit_tags),
                           ("Note", self._edit_note), ("Export", self._export)):
            button = QtWidgets.QToolButton()
            button.setText(text)
            button.clicked.connect(slot)
            controls.addWidget(button)

        self.bookmark_button = QtWidgets.QToolButton()
        self.bookmark_button.setCheckable(True)
        self.bookmark_button.setText("Bookmark")
        self.bookmark_button.clicked.connect(self._toggle_bookmark)
        controls.addWidget(self.bookmark_button)

        self.retry_button = QtWidgets.QToolButton()
        self.retry_button.setText("Retry")
        self.retry_button.clicked.connect(
            lambda: self.retryRequested.emit(self.tx.id))
        self.retry_button.hide()
        controls.addWidget(self.retry_button)

        controls.addStretch(1)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("statusText")
        controls.addWidget(self.status_label)
        outer.addLayout(controls)

        self.update_from(tx)

    # -- rendering -------------------------------------------------------
    def update_from(self, tx: Transmission) -> None:
        self.tx = tx
        meta: List[str] = [tx.started_at.astimezone().strftime("%H:%M:%S"),
                           f"{tx.duration:.1f}s"]
        if tx.channel_name:
            meta.append(tx.channel_name)
        if tx.frequency_mhz is not None:
            meta.append(f"{tx.frequency_mhz:.4f} MHz")
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

        if tx.notes:
            self.notes_label.setText(f"📝 {_escape(tx.notes)}")
            self.notes_label.show()
        else:
            self.notes_label.hide()

        status_bits: List[str] = []
        pending = STATE_LABELS.get(tx.state, "")
        if pending:
            status_bits.append(pending)
        if tx.tags:
            status_bits.append(" ".join(f"#{t}" for t in tx.tags))
        if tx.needs_review and tx.state is not ProcessingState.FAILED:
            status_bits.append("needs review")
        if tx.transcript_confidence is not None and tx.transcript:
            status_bits.append(f"conf {tx.transcript_confidence:.0%}")
        self.status_label.setText("  ·  ".join(status_bits))

        self.bookmark_button.setChecked(tx.bookmarked)
        self.setProperty("state", tx.state.value)
        self.setProperty("review", bool(tx.needs_review))
        self.style().unpolish(self)
        self.style().polish(self)

        self.play_button.setEnabled(bool(tx.audio_path))
        if tx.audio_path and self.waveform._peaks is None:
            self.waveform.load_file(tx.audio_path)

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
        self.bookmarkToggled.emit(self.tx.id, self.bookmark_button.isChecked())


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
            "No transmissions yet.\nStart monitoring, or replay a WAV file.")
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
