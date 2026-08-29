"""Field Readiness screen: what works right now, and what to do about it."""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..readiness import Check, CheckStatus


class ReadinessDialog(QtWidgets.QDialog):
    """Runs Field Check and shows it as a scannable list with remedies."""

    def __init__(self, app, parent: Optional[QtWidgets.QWidget] = None):
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

        self.refresh(False)

    def refresh(self, run_smoke_tests: bool) -> None:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            report = self.app.readiness(run_smoke_tests=run_smoke_tests)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

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

        if report.field_ready:
            self.headline.setText("Ready for offline field operation")
            self.subtitle.setText(
                "Recording, local transcription and local translation all work "
                "with the network disconnected.")
        elif report.can_record:
            self.headline.setText("Partly ready - recording works")
            self.subtitle.setText(
                "Everything received will be recorded and kept. Transcription "
                "and translation are not available yet, and can be run over "
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


def _symbol(status: CheckStatus) -> str:
    return {CheckStatus.PASS: "✓", CheckStatus.WARN: "⚠",
            CheckStatus.FAIL: "✕", CheckStatus.SKIP: "–"}[status]
