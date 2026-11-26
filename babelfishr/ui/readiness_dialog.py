"""Field Readiness screen: what works right now, and what to do about it."""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..readiness import Check, CheckStatus


class ReadinessDialog(QtWidgets.QDialog):
    """Runs Field Check and shows it as a scannable list with remedies."""

    def __init__(self, app, parent: Optional[QtWidgets.QWidget] = None,
                 report=None):
        """*report*: the readiness report already shown in the window (the
        one behind the Offline Field Use mark). Shown as is, so the details
        always agree with the mark; Re-check runs a new one."""
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Field readiness")
        self.resize(720, 620)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        self.headline = QtWidgets.QLabel()
        self.headline.setWordWrap(True)
        font = self.headline.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.headline.setFont(font)
        layout.addWidget(self.headline)

        self.subtitle = QtWidgets.QLabel()
        self.subtitle.setObjectName("sectionLabel")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Check", "Result"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        layout.addWidget(self.tree, 1)

        self.remedies = QtWidgets.QTextEdit()
        self.remedies.setReadOnly(True)
        self.remedies.setMaximumHeight(150)
        layout.addWidget(self.remedies)

        buttons = QtWidgets.QDialogButtonBox()
        self.recheck_button = buttons.addButton(
            "Re-check (loads models)", QtWidgets.QDialogButtonBox.ActionRole)
        self.recheck_button.setToolTip(
            "Runs the real transcription and translation smoke tests. "
            "Downloads nothing.")
        self.recheck_button.clicked.connect(lambda: self.refresh(True))
        buttons.addButton(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._worker = None
        if report is not None:
            self._render(report)
        else:
            self.refresh(False)

    def refresh(self, run_smoke_tests: bool) -> None:
        """Run Field Check on a worker thread.

        With smoke tests enabled this loads a Whisper model and transcribes a
        fixture, which takes seconds; on the GUI thread that is a frozen
        window.
        """
        from ..readiness import microphone_authorization
        from .workers import readiness_job, run_in_background

        self.recheck_button.setEnabled(False)
        self.headline.setText("Checking...")
        self.subtitle.setText(
            "Loading models and running real smoke tests."
            if run_smoke_tests else "Checking audio, storage and engines.")
        # The same inputs as the window's own check: macOS's microphone
        # decision read here on the GUI thread (app.readiness adds the
        # selected Session's save path).
        self._worker = run_in_background(
            readiness_job, self.app, run_smoke_tests,
            microphone_permission=microphone_authorization(),
            on_message=self.subtitle.setText,
            on_finished=self._render,
            on_failed=self._failed)

    def _failed(self, message: str) -> None:
        self.recheck_button.setEnabled(True)
        self.headline.setText("Field check could not complete")
        self.subtitle.setText(message)

    def _render(self, report) -> None:
        self.recheck_button.setEnabled(True)
        self.tree.clear()
        for check in report.checks:
            item = QtWidgets.QTreeWidgetItem(
                [f"{_symbol(check.status)}  {check.name}", check.detail])
            item.setToolTip(1, check.detail)
            if check.status is CheckStatus.FAIL:
                item.setForeground(0, QtGui.QBrush(QtGui.QColor("#cf222e")))
            elif check.status is CheckStatus.WARN:
                item.setForeground(0, QtGui.QBrush(QtGui.QColor("#9a6700")))
            self.tree.addTopLevelItem(item)

        from ..modes import OperatingMode
        from ..readiness import offline_field_problems

        if report.mode is OperatingMode.FIELD_OFFLINE:
            # The same verdict as the mark beside the mode, word for word.
            problems = offline_field_problems(report)
            if problems:
                self.headline.setText("\u2715 Not ready for Offline Field Use")
                self.subtitle.setText("\n".join(f"\u2022 {p}" for p in problems))
            else:
                self.headline.setText("\u2713 Ready for Offline Field Use")
                self.subtitle.setText(
                    "Audio, storage, the selected speech model and the selected "
                    "translation all passed on this computer.")
        elif report.field_ready:
            self.headline.setText("Ready for offline field operation")
            self.subtitle.setText(
                "Recording, local transcription and local translation all work "
                "with the network disconnected.")
        elif report.field_ready_unknown:
            # The dialog opens with smoke tests off: a prepared computer is
            # not checked yet, which is not the same as unavailable.
            self.headline.setText("Prepared - not checked yet")
            self.subtitle.setText(
                "Recording works, and the transcription model and language "
                "packs are installed. They have not been tested in this "
                "check: use Re-check to run the real smoke tests.")
        elif report.can_record:
            self.headline.setText("Partly ready - recording works")
            self.subtitle.setText(
                "Everything received will be recorded and kept. "
                f"Transcription: {_stage(report, 'transcribe')}; translation: "
                f"{_stage(report, 'translate')}. Processing can be run over "
                "these recordings later once prepared. Record Only mode is "
                "recommended.")
        else:
            self.headline.setText("Not ready - audio capture is not working")
            self.subtitle.setText(
                "Fix the audio input first: nothing can be preserved until "
                "capture works.")

        remedies = [f"• {c.remedy}" for c in report.checks
                    if c.remedy and c.status in (CheckStatus.FAIL,
                                                 CheckStatus.WARN)]
        self.remedies.setPlainText(
            "Next steps:\n" + "\n".join(remedies) if remedies
            else "No outstanding actions.")


def _stage(report, stage: str) -> str:
    return {"yes": "works", "NO": "not available yet",
            "not checked yet": "installed, not checked yet"}[report.stage_word(stage)]


def _symbol(status: CheckStatus) -> str:
    return {CheckStatus.PASS: "✓", CheckStatus.WARN: "⚠",
            CheckStatus.FAIL: "✕", CheckStatus.SKIP: "–"}[status]
