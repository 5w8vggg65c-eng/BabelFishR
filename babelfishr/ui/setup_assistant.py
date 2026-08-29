"""First-run assistant: get an operator from install to field-ready.

Deliberately honest about the one-time online step: offline operation is only
real once a model and language packs are on disk, and pretending otherwise
would strand someone in the field.
"""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtWidgets

from ..modes import OperatingMode


class SetupAssistant(QtWidgets.QDialog):
    """Explains the path to offline readiness and offers to run preparation."""

    def __init__(self, app, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Welcome to BabelFishR")
        self.resize(640, 520)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(14)

        title = QtWidgets.QLabel("Set up for field use")
        font = title.font()
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        layout.addWidget(_paragraph(
            "BabelFishR listens to the audio your radio sends to its accessory "
            "connector, records every transmission, and transcribes and "
            "translates the speech.\n\n"
            "It is receive-only: it never transmits, and it cannot tell what "
            "frequency you are on. You supply that with a radio profile."))

        steps = QtWidgets.QGroupBox("What has to happen once, with internet")
        steps_layout = QtWidgets.QVBoxLayout(steps)
        steps_layout.addWidget(_paragraph(
            "1.  Download a local speech model.\n"
            "2.  Install the translation language packs you need.\n"
            "3.  Verify both actually run with downloads disabled.\n\n"
            "After that the app works with the network switched off. Until "
            "then, transcription and translation are unavailable - but "
            "recording already works, so nothing received is lost."))
        layout.addWidget(steps)

        options = QtWidgets.QGroupBox("Preparation")
        form = QtWidgets.QFormLayout(options)
        self.model_box = QtWidgets.QComboBox()
        for name, note in (("tiny", "fastest, least accurate"),
                           ("base", ""), ("small", "recommended"),
                           ("medium", "slower, more accurate"),
                           ("large-v3", "slowest, most accurate")):
            self.model_box.addItem(f"{name}  {note}".strip(), name)
        self.model_box.setCurrentIndex(2)
        form.addRow("Speech model", self.model_box)

        self.languages_field = QtWidgets.QLineEdit("es-en, de-en, fr-en")
        self.languages_field.setToolTip(
            "Comma-separated source-target pairs, e.g. es-en")
        form.addRow("Language packs", self.languages_field)
        layout.addWidget(options)

        self.command_label = QtWidgets.QLabel()
        self.command_label.setObjectName("sectionLabel")
        self.command_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        self.command_label.setWordWrap(True)
        layout.addWidget(self.command_label)
        self.model_box.currentIndexChanged.connect(self._update_command)
        self.languages_field.textChanged.connect(self._update_command)
        self._update_command()

        layout.addStretch(1)

        buttons = QtWidgets.QDialogButtonBox()
        skip = buttons.addButton("Skip - record only for now",
                                 QtWidgets.QDialogButtonBox.RejectRole)
        skip.clicked.connect(self._record_only)
        check = buttons.addButton("Check readiness",
                                  QtWidgets.QDialogButtonBox.ActionRole)
        check.clicked.connect(self._check)
        buttons.addButton(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def language_pairs(self):
        pairs = []
        for chunk in self.languages_field.text().split(","):
            chunk = chunk.strip()
            if "-" in chunk:
                source, target = chunk.split("-", 1)
                pairs.append((source.strip(), target.strip()))
        return pairs

    def _update_command(self) -> None:
        model = self.model_box.currentData()
        languages = " ".join(f"--language {a}-{b}" for a, b in self.language_pairs())
        self.command_label.setText(
            "Run this in a terminal with internet access:\n\n"
            f"    babelfishr prepare-field --asr-model {model} {languages}\n\n"
            "It downloads the model and packs into the application's own "
            "folder, then verifies they load with downloads disabled.")

    def _record_only(self) -> None:
        self.app.set_mode(OperatingMode.RECORD_ONLY.value)
        self.accept()

    def _check(self) -> None:
        from .readiness_dialog import ReadinessDialog

        ReadinessDialog(self.app, self).exec()


def _paragraph(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    return label
