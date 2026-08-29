"""The window ``Uninstall BabelFishR.app`` shows.

Deliberately one window with no menu, no preferences and no way to reach it
from inside BabelFishR itself. An application that can delete all of its own
data from its own menu bar is one misclick away from losing a day's
recordings; removal is a separate program the operator has to go and open.

Nothing is deleted until BOTH of these are true:

* the acknowledgement box is ticked, and
* the word DELETE has been typed exactly.

Cancel, or closing the window, changes nothing at all.
"""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..uninstall import (UninstallPlan, UninstallRefused, app_is_running,
                         build_plan, describe_plan, removal_scope,
                         request_quit, uninstall)

CONFIRM_WORD = "DELETE"


def confirmation_ready(acknowledged: bool, typed: str) -> bool:
    """The single gate both the checkbox and the text field feed.

    Exact match, not case-insensitive and not stripped of inner text: typing
    "delete" or "DELETE IT" is not confirmation.
    """
    return bool(acknowledged) and typed.strip() == CONFIRM_WORD


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"


class UninstallWindow(QtWidgets.QWidget):
    """Show exactly what will go, then require two deliberate confirmations."""

    def __init__(self, plan: Optional[UninstallPlan] = None,
                 *, running_check=None, parent=None):
        super().__init__(parent)
        self.plan = plan if plan is not None else build_plan()
        self._running_check = running_check or (lambda: app_is_running())
        self._finished = False
        self.setWindowTitle("Uninstall BabelFishR")
        self.resize(720, 640)
        self._build()

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)

        heading = QtWidgets.QLabel("Remove BabelFishR from this computer")
        font = heading.font()
        font.setPointSize(font.pointSize() + 6)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)

        scope = QtWidgets.QLabel(
            "This permanently deletes:\n"
            + "\n".join(f"  •  {line}" for line in removal_scope()))
        scope.setWordWrap(True)
        layout.addWidget(scope)

        self.warning = QtWidgets.QLabel(
            "⚠️  Recordings cannot be recovered. There is no undo and nothing "
            "is moved to the Trash. If you want to keep any recordings, "
            "copy them somewhere else before continuing.")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #b00020; font-weight: bold;")
        layout.addWidget(self.warning)

        layout.addWidget(QtWidgets.QLabel("Exactly these paths will be deleted:"))
        self.detail = QtWidgets.QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlainText(describe_plan(self.plan))
        self.detail.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.FixedFont))
        layout.addWidget(self.detail, 1)

        total = self.plan.total_bytes()
        self.size_label = QtWidgets.QLabel(
            f"About {_human_bytes(total)} will be freed."
            if total else "Nothing of BabelFishR's was found for this user.")
        layout.addWidget(self.size_label)

        self.acknowledge = QtWidgets.QCheckBox(
            "I understand that my recordings, transcripts and translations "
            "will be permanently deleted.")
        self.acknowledge.setChecked(False)
        self.acknowledge.toggled.connect(self._refresh)
        layout.addWidget(self.acknowledge)

        typed_row = QtWidgets.QHBoxLayout()
        typed_row.addWidget(QtWidgets.QLabel(f"Type {CONFIRM_WORD} to confirm:"))
        self.typed = QtWidgets.QLineEdit()
        self.typed.setPlaceholderText(CONFIRM_WORD)
        self.typed.textChanged.connect(self._refresh)
        typed_row.addWidget(self.typed, 1)
        layout.addLayout(typed_row)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QtWidgets.QHBoxLayout()
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.close)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        self.remove_button = QtWidgets.QPushButton("Remove BabelFishR")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self.perform)
        buttons.addWidget(self.remove_button)
        layout.addLayout(buttons)
        self._refresh()

    # -- behaviour ------------------------------------------------------
    def _refresh(self) -> None:
        ready = confirmation_ready(self.acknowledge.isChecked(), self.typed.text())
        self.remove_button.setEnabled(ready and not self._finished)

    def perform(self) -> None:
        """Delete, but only after re-checking both confirmations."""
        if not confirmation_ready(self.acknowledge.isChecked(), self.typed.text()):
            return
        if self._running_check() is not False:
            if not self._offer_to_quit():
                return

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            report = uninstall(self.plan, running_check=self._running_check)
        except UninstallRefused as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.warning(self, "Nothing was deleted", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never die silently mid-delete
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(
                self, "Removal did not finish",
                f"The removal stopped with an error:\n\n{exc}\n\n"
                f"Some items may still be on this computer.")
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        self._show_report(report)

    def _offer_to_quit(self) -> bool:
        """Ask BabelFishR to quit, then verify it really stopped."""
        answer = QtWidgets.QMessageBox.question(
            self, "BabelFishR is running",
            "BabelFishR is still open. It has to quit before it can be "
            "removed.\n\nQuit BabelFishR now?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel)
        if answer != QtWidgets.QMessageBox.Yes:
            self.status.setText("Nothing was deleted.")
            return False
        request_quit()
        deadline = QtCore.QTime.currentTime().addSecs(20)
        while QtCore.QTime.currentTime() < deadline:
            if self._running_check() is False:
                return True
            QtWidgets.QApplication.processEvents()
            QtCore.QThread.msleep(250)
        QtWidgets.QMessageBox.warning(
            self, "BabelFishR is still running",
            "BabelFishR did not stop, so nothing was deleted. Quit it from "
            "its own menu (or the Force Quit window) and try again.")
        return False

    def _show_report(self, report) -> None:
        self._finished = True
        self.detail.setPlainText(report.summary())
        self.acknowledge.setEnabled(False)
        self.typed.setEnabled(False)
        self.remove_button.setEnabled(False)
        self.cancel_button.setText("Close")
        if report.complete:
            self.warning.setStyleSheet("color: #1b5e20; font-weight: bold;")
            self.warning.setText("BabelFishR has been completely removed.")
            self.status.setText(
                "You can now drag this uninstaller to the Trash, or just "
                "eject the disk image.")
        else:
            self.warning.setText(
                "⚠️  BabelFishR was NOT completely removed. The items listed "
                "above are still on this computer.")
            self.status.setText(
                f"{len(report.leftovers())} item(s) could not be removed.")


def run(plan: Optional[UninstallPlan] = None) -> int:
    """Open the uninstaller window. Returns a process exit code."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setApplicationName("Uninstall BabelFishR")
    window = UninstallWindow(plan)
    window.show()
    window.raise_()
    window.activateWindow()
    return app.exec()
