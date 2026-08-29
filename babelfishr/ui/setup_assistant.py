"""First-run setup, performed in the GUI rather than described in prose.

The previous version told the operator to open Terminal and run a command,
which is not a setup workflow for an application whose entire point is being
double-clicked. This one does the work: choose a model and language pairs,
press a button, watch progress, and end with a real Field Check.

Everything slow runs on a worker thread. The terminal command is still shown,
as an advanced fallback for someone who prefers it or is scripting a fleet.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from ..modes import OperatingMode
from .workers import prepare_field_job, run_in_background

log = logging.getLogger(__name__)

MODELS = [
    ("tiny", "fastest, least accurate", 75),
    ("base", "", 145),
    ("small", "recommended balance", 480),
    ("medium", "slower, more accurate", 1500),
    ("large-v3", "slowest, most accurate", 3100),
]

LANGUAGES = [
    ("es", "Spanish"), ("de", "German"), ("fr", "French"), ("uk", "Ukrainian"),
    ("ru", "Russian"), ("pl", "Polish"), ("it", "Italian"), ("pt", "Portuguese"),
    ("ar", "Arabic"), ("tr", "Turkish"), ("nl", "Dutch"), ("zh", "Chinese"),
]


class SetupAssistant(QtWidgets.QDialog):
    """Choose assets, prepare them here, and verify offline readiness."""

    def __init__(self, app, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Set up BabelFishR")
        self.resize(680, 640)
        self._worker = None
        self._readiness = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(14)

        title = QtWidgets.QLabel("Set up for field use")
        font = title.font()
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        layout.addWidget(_paragraph(
            "BabelFishR records every transmission your radio receives, then "
            "transcribes and translates the speech. It is receive-only and "
            "never transmits.\n\n"
            "Preparation needs the internet once. Afterwards everything runs "
            "on this Mac with the network switched off."))

        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self._build_choices())
        self.stack.addWidget(self._build_progress())
        layout.addWidget(self.stack, 1)

        self.advanced = QtWidgets.QLabel()
        self.advanced.setObjectName("sectionLabel")
        self.advanced.setWordWrap(True)
        self.advanced.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.advanced)

        self.buttons = QtWidgets.QDialogButtonBox()
        self.prepare_button = self.buttons.addButton(
            "Prepare now", QtWidgets.QDialogButtonBox.AcceptRole)
        self.prepare_button.setObjectName("primaryButton")
        self.prepare_button.clicked.connect(self._start)

        self.record_only_button = self.buttons.addButton(
            "Record only for now", QtWidgets.QDialogButtonBox.DestructiveRole)
        self.record_only_button.setToolTip(
            "Skip preparation. Transmissions are still recorded and kept, and "
            "can be transcribed later.")
        self.record_only_button.clicked.connect(self._record_only)

        self.cancel_button = self.buttons.addButton(
            QtWidgets.QDialogButtonBox.Cancel)
        self.cancel_button.clicked.connect(self._cancel)
        layout.addWidget(self.buttons)

        self._update_advanced()

    # -- pages -----------------------------------------------------------
    def _build_choices(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        model_box = QtWidgets.QGroupBox("Speech model")
        model_layout = QtWidgets.QVBoxLayout(model_box)
        self.model_box = QtWidgets.QComboBox()
        for name, note, size in MODELS:
            label = f"{name} - about {size} MB" + (f", {note}" if note else "")
            self.model_box.addItem(label, name)
        self.model_box.setCurrentIndex(2)
        self.model_box.currentIndexChanged.connect(self._update_advanced)
        model_layout.addWidget(self.model_box)
        self.size_label = QtWidgets.QLabel()
        self.size_label.setObjectName("sectionLabel")
        model_layout.addWidget(self.size_label)
        layout.addWidget(model_box)

        language_box = QtWidgets.QGroupBox(
            "Languages to translate from (into your target language)")
        language_layout = QtWidgets.QGridLayout(language_box)
        self.language_checks = {}
        for index, (code, name) in enumerate(LANGUAGES):
            check = QtWidgets.QCheckBox(f"{name} ({code})")
            check.setChecked(code in ("es", "de", "fr"))
            check.stateChanged.connect(self._update_advanced)
            self.language_checks[code] = check
            language_layout.addWidget(check, index // 3, index % 3)
        layout.addWidget(language_box)

        layout.addWidget(_paragraph(
            "Only the pairs you install can be translated offline. Traffic in "
            "any other language is still recorded, and still transcribed when "
            "the speech model recognises it."))
        layout.addStretch(1)
        return page

    def _build_progress(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QtWidgets.QLabel("Preparing...")
        font = self.status_label.font()
        font.setBold(True)
        self.status_label.setFont(font)
        layout.addWidget(self.status_label)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate: sizes are not knowable
        layout.addWidget(self.progress)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)
        return page

    # -- choices ---------------------------------------------------------
    def language_pairs(self) -> List[Tuple[str, str]]:
        target = self.app.config.translate.target_language
        return [(code, target) for code, check in self.language_checks.items()
                if check.isChecked() and code != target]

    def selected_model(self) -> str:
        return self.model_box.currentData()

    def _update_advanced(self) -> None:
        model = self.selected_model()
        size = next((s for n, _, s in MODELS if n == model), 0)
        pairs = self.language_pairs()
        estimate = size + 120 * len(pairs)
        self.size_label.setText(
            f"About {estimate} MB will be downloaded into "
            f"{self.app.config.paths().models.parent}")
        languages = " ".join(f"--language {a}-{b}" for a, b in pairs)
        self.advanced.setText(
            "Advanced: the same preparation from a terminal —\n"
            f"    babelfishr prepare-field --asr-model {model} {languages}")

    # -- running ---------------------------------------------------------
    def _start(self) -> None:
        pairs = self.language_pairs()
        model = self.selected_model()
        self.stack.setCurrentIndex(1)
        self.prepare_button.setEnabled(False)
        self.record_only_button.setEnabled(False)
        self.cancel_button.setText("Cancel")
        self.status_label.setText(f"Preparing {model}...")
        self.log_view.clear()
        self._append("Starting preparation. This needs the internet.")

        # Off the GUI thread: a 500 MB download on the UI thread looks like a
        # crash, and a force-quit mid-download leaves a broken model.
        self._worker = run_in_background(
            prepare_field_job, self.app.config, model, pairs,
            on_message=self._append,
            on_finished=self._finished,
            on_failed=self._failed,
            on_cancelled=self._cancelled)

    def _append(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum())

    def _finished(self, payload) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._worker = None
        result = payload["preparation"]
        readiness = payload["readiness"]
        self._readiness = readiness

        self._append("")
        self._append(result.summary())
        self._append("")
        self._append(readiness.summary())

        # Readiness is only claimed when the model and the requested routes
        # actually loaded with downloads disabled.
        if readiness.field_ready:
            self.status_label.setText("Ready for offline field use")
            self.app.config.record_setup(
                asr_model=self.selected_model(),
                language_pairs=self.language_pairs())
            self.app.set_mode(OperatingMode.FIELD_OFFLINE.value)
        elif readiness.can_record:
            self.status_label.setText(
                "Partly ready - recording works, processing does not")
        else:
            self.status_label.setText("Not ready")
        self.cancel_button.setText("Close")
        self.prepare_button.setEnabled(True)
        self.prepare_button.setText("Prepare again")
        self.record_only_button.setEnabled(True)

    def _failed(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._worker = None
        self.status_label.setText("Preparation failed")
        self._append("")
        self._append(f"ERROR: {message}")
        self._append(
            "Nothing was lost. Recording works without a model - choose "
            "'Record only for now' and prepare later.")
        self.prepare_button.setEnabled(True)
        self.record_only_button.setEnabled(True)
        self.cancel_button.setText("Close")

    def _cancelled(self) -> None:
        self._worker = None
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status_label.setText("Preparation cancelled")
        self._append(
            "Cancelled. A partly downloaded model is detected as incomplete "
            "and repaired the next time you prepare.")
        self.prepare_button.setEnabled(True)
        self.record_only_button.setEnabled(True)
        self.cancel_button.setText("Close")
        self.stack.setCurrentIndex(1)

    def _cancel(self) -> None:
        if self._worker is not None:
            self._append("Cancelling after the current step...")
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            return
        self.reject()

    def _record_only(self) -> None:
        """A deliberate, remembered choice - not a silent fallback."""
        self.app.set_mode(OperatingMode.RECORD_ONLY.value)
        self.app.config.record_setup(record_only=True)
        self.accept()

    @property
    def readiness(self):
        return self._readiness

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self._worker is not None:
            self._worker.cancel()
        super().closeEvent(event)


def _paragraph(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    return label
