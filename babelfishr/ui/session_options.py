"""The Session Options dialog.

The bottom "Session options" button opens this window over the transcript,
centred on it. It is a separate, non-modal window, so the transcript, the
waveform and the input meters keep their size and place and stay live
underneath. Close, Escape or the button again close it; it can be resized,
and on a small screen it is fitted to the screen with its contents
scrolling, so every option stays reachable.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

#: Roomy by default; shrunk to fit the screen when that is smaller.
PREFERRED_SIZE = QtCore.QSize(780, 560)
MINIMUM_SIZE = QtCore.QSize(360, 260)


class SessionOptionsDialog(QtWidgets.QDialog):
    def __init__(self, content: QtWidgets.QWidget, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.setObjectName("sessionOptionsDialog")
        self.setWindowTitle("Session Options")
        self.setAccessibleName("Session Options")
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(MINIMUM_SIZE)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        self.close_button = self.buttons.button(QtWidgets.QDialogButtonBox.Close)
        layout.addWidget(self.buttons)
        self.resize(PREFERRED_SIZE)

    def open_over(self, anchor: QtWidgets.QWidget) -> None:
        """Show centred on *anchor* (the transcript), inside its screen."""
        top_left = anchor.mapToGlobal(QtCore.QPoint(0, 0))
        target = QtCore.QRect(top_left, anchor.size())
        screen = (QtGui.QGuiApplication.screenAt(target.center())
                  or anchor.screen() or QtGui.QGuiApplication.primaryScreen())
        available = screen.availableGeometry() if screen else QtCore.QRect(
            0, 0, PREFERRED_SIZE.width(), PREFERRED_SIZE.height())
        # Roomy, but never larger than the screen; the contents scroll.
        width = max(min(max(self.width(), PREFERRED_SIZE.width()),
                        available.width()), min(MINIMUM_SIZE.width(), available.width()))
        height = max(min(max(self.height(), PREFERRED_SIZE.height()),
                         available.height()), min(MINIMUM_SIZE.height(), available.height()))
        if self.isVisible():
            width, height = min(self.width(), available.width()), min(
                self.height(), available.height())
        self.resize(width, height)
        frame = self.frameGeometry()
        extra_w, extra_h = frame.width() - self.width(), frame.height() - self.height()
        rect = QtCore.QRect(0, 0, width + max(extra_w, 0), height + max(extra_h, 0))
        rect.moveCenter(target.center())
        # Clamp inside the available area so the title bar and Close are on screen.
        rect.moveLeft(max(available.left(), min(rect.left(), available.right() - rect.width() + 1)))
        rect.moveTop(max(available.top(), min(rect.top(), available.bottom() - rect.height() + 1)))
        self.move(rect.topLeft())
        self.show()
        self.raise_()
        self.activateWindow()
