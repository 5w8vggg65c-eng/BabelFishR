"""Select installed Whisper weights in the console's Local Engines section."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..model_integrity import Integrity, integrity_state
from ..providers.whisper_local import MODEL_SIZES, ModelState, inspect_model_directory


class WhisperModelPicker(QtWidgets.QComboBox):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setAccessibleName("Whisper speech model")
        self.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.refresh_choices()

    def refresh_choices(self) -> None:
        current = self.app.config.asr.model
        names = list(MODEL_SIZES)
        if current not in names:
            names.append(current)
        labels = {ModelState.COMPLETE: "Installed", ModelState.INCOMPLETE: "Incomplete",
                  ModelState.MISSING: "Not installed"}
        with QtCore.QSignalBlocker(self):
            self.clear()
            for name in names:
                directory = self.app.whisper_model_directory(name)
                try:
                    state, _ = inspect_model_directory(directory)
                    status, detail = labels[state], str(directory)
                    if state is ModelState.COMPLETE:
                        # From the install record only (cheap, no hashing).
                        checked, why = integrity_state(directory, name)
                        if (name == current and self.app.config.asr.model_path):
                            checked, why = Integrity.UNPINNED, "custom folder"
                        status += (", verified" if checked is Integrity.VERIFIED
                                   else ", checksum mismatch"
                                   if checked is Integrity.MISMATCH
                                   else ", unverified")
                        detail += f"\n{why}"
                except OSError as exc:
                    state, status, detail = None, "Unavailable", str(exc)
                label = name.replace("-", " ").capitalize()
                if name == current and self.app.config.asr.model_path:
                    label += " (custom folder)"
                self.addItem(f"{label} · {status}", name)
                item = self.model().item(self.count() - 1)
                item.setEnabled(state is ModelState.COMPLETE)
                item.setToolTip(detail)
            self.setCurrentIndex(self.findData(current))
        self.refresh_availability()

    def refresh_availability(self) -> None:
        enabled = self.app.can_change_whisper_model()
        self.setEnabled(enabled)
        self.setToolTip(
            f"{self.currentText()}\nSelect installed weights for the next "
            "Whisper transcription." if enabled else
            f"{self.currentText()}\nAvailable after monitoring and queued work finish.")

    def showPopup(self) -> None:
        # Preparation or removal outside this window can change the files.
        self.refresh_choices()
        if self.isEnabled():
            super().showPopup()
