"""In-place progress for a speech-model download, sized to its window.

Shows real progress: bytes actually received against the total the Hub
reported for the files (a determinate bar and a percentage), or - when no
total could be verified - the bytes received so far with a busy bar. The
preparation and the offline verification are named as their own phases,
and "installed" is said only by the caller once the install succeeded.
Every line wraps or elides inside the window: a long model folder or file
name can never make the window (or its bar) wider than the screen, which
is what a one-line progress label did before.
"""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets


def human_bytes(count: Optional[int]) -> str:
    if count is None:
        return "?"
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000.0
    return f"{size:.1f} GB"


class ElidedLabel(QtWidgets.QLabel):
    """One line, elided in the middle to whatever width it is given; the
    full text is its tooltip and accessible description."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.setMinimumWidth(1)
        self._full = ""
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full)
        self.setAccessibleDescription(self._full)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = max(10, self.width())
        super().setText(self.fontMetrics().elidedText(self._full, QtCore.Qt.ElideMiddle,
                                                      width))

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        hint = super().sizeHint()
        return QtCore.QSize(10, hint.height())

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(1, super().minimumSizeHint().height())


class DownloadProgressPanel(QtWidgets.QFrame):
    """Bar, what is happening, which file; a Cancel when cancellable."""

    cancelRequested = QtCore.Signal()

    PHASES = {"resolving": "Finding the model's files…",
              "downloading": "Downloading",
              "verifying": "Verifying the model works offline…",
              "done": "Downloaded and verified"}

    def __init__(self, parent=None, cancellable: bool = True):
        super().__init__(parent)
        self.setObjectName("downloadPanel")
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.setMinimumWidth(1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.bar.setMinimumWidth(1)
        self.bar.setAccessibleName("Download progress")
        layout.addWidget(self.bar)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                  QtWidgets.QSizePolicy.Preferred)
        self.status.setMinimumWidth(1)
        self.status.setAccessibleName("Download status")
        layout.addWidget(self.status)
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.file = ElidedLabel("")
        self.file.setObjectName("sectionLabel")
        self.file.setAccessibleName("File being downloaded")
        row.addWidget(self.file, 1)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setAccessibleName("Cancel the download")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setVisible(cancellable)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.last = None

    # -- updates -------------------------------------------------------------
    def start(self, title: str) -> None:
        self.show()
        self.bar.setRange(0, 0)                  # busy until the first figures
        self.status.setText(title)
        self.file.set_full_text("")
        self.cancel_button.setEnabled(True)

    def set_progress(self, progress) -> None:
        self.last = progress
        phase = getattr(progress, "phase", "downloading")
        fraction = getattr(progress, "fraction", None)
        done = getattr(progress, "bytes_done", 0) or 0
        total = getattr(progress, "bytes_total", None)
        if phase == "downloading":
            if fraction is not None and total:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
                text = (f"Downloading — {human_bytes(done)} of {human_bytes(total)} "
                        f"({fraction * 100:.0f}%)")
            else:
                self.bar.setRange(0, 0)          # unknown total: busy, real bytes
                text = f"Downloading — {human_bytes(done)} received (total not known)"
            files_total = getattr(progress, "files_total", None)
            if files_total:
                text += (f" · file {min(files_total, progress.files_done + 1)} "
                         f"of {files_total}")
        elif phase == "verifying":
            self.bar.setRange(0, 0)
            text = self.PHASES["verifying"]
        elif phase == "done":
            self.bar.setRange(0, 1000)
            self.bar.setValue(1000)
            text = self.PHASES["done"]
        else:
            self.bar.setRange(0, 0)
            text = self.PHASES.get(phase, phase)
        self.status.setText(text)
        self.file.set_full_text(getattr(progress, "file", "") or "")
        self.cancel_button.setEnabled(phase in ("resolving", "downloading"))

    def set_message(self, text: str) -> None:
        """A step's own words (e.g. from the preparation log)."""
        if self.last is None:
            self.status.setText(text)

    def finish(self, text: str, ok: bool) -> None:
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000 if ok else 0)
        self.status.setText(text)
        self.cancel_button.setEnabled(False)

    def _cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.status.setText("Cancelling…")
        self.cancelRequested.emit()
