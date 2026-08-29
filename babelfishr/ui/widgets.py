"""Small custom widgets: level meter and compact waveform."""

from __future__ import annotations

import pathlib
from typing import List, Optional

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets


class LevelMeterWidget(QtWidgets.QWidget):
    """Horizontal RMS bar with peak-hold and a clipping indicator.

    Setting input gain is the most common cause of a failed monitoring session,
    so the meter shows the same information an audio engineer would want: where
    the signal sits, where it peaked, and whether it ever hit the ceiling.
    """

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setMinimumHeight(22)
        self.setMinimumWidth(180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Fixed)
        self._rms = 0.0
        self._peak = 0.0
        self._clipped = False
        self._clip_count = 0
        self.setToolTip("Input level. Aim for speech peaks around -12 dBFS.")

    def set_reading(self, rms_fraction: float, peak_fraction: float,
                    clipped: bool, clip_count: int = 0) -> None:
        self._rms = max(0.0, min(1.0, float(rms_fraction)))
        self._peak = max(0.0, min(1.0, float(peak_fraction)))
        self._clipped = clipped
        self._clip_count = clip_count
        self.update()

    def reset(self) -> None:
        self._rms = self._peak = 0.0
        self._clipped = False
        self._clip_count = 0
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
        rect = self.rect().adjusted(0, 0, -1, -1)

        painter.fillRect(rect, QtGui.QColor("#1e1f22"))
        painter.setPen(QtGui.QColor("#3a3d42"))
        painter.drawRect(rect)

        # -60..0 dBFS mapped across the width; colour by zone, not by value, so
        # the bar reads the same way every time.
        width = rect.width() - 2
        height = rect.height() - 2
        bar = int(width * self._rms)
        gradient = QtGui.QLinearGradient(rect.left(), 0, rect.right(), 0)
        gradient.setColorAt(0.0, QtGui.QColor("#3fb950"))
        gradient.setColorAt(0.75, QtGui.QColor("#d29922"))
        gradient.setColorAt(0.95, QtGui.QColor("#f85149"))
        painter.fillRect(rect.left() + 1, rect.top() + 1, bar, height,
                         QtGui.QBrush(gradient))

        if self._peak > 0:
            x = rect.left() + 1 + int(width * self._peak)
            painter.setPen(QtGui.QPen(QtGui.QColor("#e6edf3"), 2))
            painter.drawLine(x, rect.top() + 1, x, rect.bottom() - 1)

        # Scale marks at -40, -20, -12, -6 dBFS.
        painter.setPen(QtGui.QColor("#484f58"))
        for db in (-40, -20, -12, -6):
            x = rect.left() + 1 + int(width * ((db + 60) / 60.0))
            painter.drawLine(x, rect.bottom() - 4, x, rect.bottom() - 1)

        if self._clipped:
            painter.fillRect(rect.right() - 10, rect.top() + 1, 9, height,
                             QtGui.QColor("#f85149"))
        painter.end()


class WaveformWidget(QtWidgets.QWidget):
    """Compact min/max envelope of a clip, drawn from peaks."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 height: int = 34, buckets: int = 220):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Fixed)
        self.buckets = buckets
        self._peaks: Optional[np.ndarray] = None
        self._progress = 0.0

    def set_audio(self, samples: np.ndarray) -> None:
        data = np.asarray(samples, dtype=np.float64).ravel()
        if data.size == 0:
            self._peaks = None
        else:
            count = min(self.buckets, max(1, data.size))
            usable = (data.size // count) * count
            if usable <= 0:
                self._peaks = np.abs(data)[:count]
            else:
                reshaped = np.abs(data[:usable]).reshape(count, -1)
                self._peaks = reshaped.max(axis=1)
            peak = float(self._peaks.max()) or 1.0
            self._peaks = self._peaks / peak
        self.update()

    def load_file(self, path: str) -> bool:
        try:
            from ..audio.wavefile import read_wav

            samples, _ = read_wav(path)
        except Exception:  # noqa: BLE001 - a missing clip is not fatal to the UI
            self._peaks = None
            self.update()
            return False
        self.set_audio(samples)
        return True

    def set_progress(self, fraction: float) -> None:
        self._progress = max(0.0, min(1.0, float(fraction)))
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor(0, 0, 0, 0))
        if self._peaks is None or self._peaks.size == 0:
            painter.setPen(QtGui.QColor("#6e7681"))
            painter.drawText(rect, QtCore.Qt.AlignCenter, "no waveform")
            painter.end()
            return

        middle = rect.center().y()
        count = self._peaks.size
        step = rect.width() / float(count)
        played_until = rect.left() + rect.width() * self._progress
        for index, value in enumerate(self._peaks):
            x = rect.left() + index * step
            amplitude = max(1.0, value * (rect.height() / 2 - 2))
            colour = QtGui.QColor("#58a6ff") if x <= played_until else QtGui.QColor("#8b949e")
            painter.setPen(QtGui.QPen(colour, max(1.0, step * 0.8)))
            painter.drawLine(QtCore.QPointF(x, middle - amplitude),
                             QtCore.QPointF(x, middle + amplitude))
        painter.end()


class TagEditor(QtWidgets.QDialog):
    """Trivial comma-separated tag editor."""

    def __init__(self, tags: List[str], parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Edit tags")
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Comma-separated tags:"))
        self.field = QtWidgets.QLineEdit(", ".join(tags))
        layout.addWidget(self.field)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def tags(self) -> List[str]:
        return [t.strip() for t in self.field.text().split(",") if t.strip()]
