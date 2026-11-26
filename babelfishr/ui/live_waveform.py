"""Live input waveform in the same space as the saved recording display."""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..audio.envelope import WINDOW_SECONDS, WaveformFrame
from ..audio.meter import LevelReading
from . import theme
from .waveform import RecordingWaveform


class LiveWaveform(QtWidgets.QWidget):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setObjectName("liveWaveform")
        self.setAccessibleName("Live input waveform")
        self.setAccessibleDescription(
            "The last five seconds of incoming audio. New audio enters on the right.")
        self.setToolTip(
            "The upper and lower edges are the actual positive and negative "
            "sample levels. The fixed scale runs from −60 dBFS at the centre "
            "to full scale at the edges.")
        self.setFixedHeight(RecordingWaveform.HEIGHT)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setMinimumWidth(160)
        self.frame: Optional[WaveformFrame] = None
        self._stale = QtCore.QTimer(self)
        self._stale.setInterval(1500)
        self._stale.setSingleShot(True)
        self._stale.timeout.connect(self.reset)

    def feed(self, reading: LevelReading) -> None:
        frame = getattr(reading, "waveform", None)
        if frame is not None:
            self.frame = frame
            self._stale.start()
            self.update()

    def reset(self) -> None:
        self._stale.stop()
        self.frame = None
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        colours = theme.tokens(self)
        p = QtGui.QPainter(self)
        try:
            p.fillRect(self.rect(), QtGui.QColor(colours["surface"]))
            p.setPen(QtGui.QColor(colours["border"]))
            p.drawRect(self.rect().adjusted(0, 0, -1, -1))
            area = self.rect().adjusted(8, 22, -8, -22)
            p.setPen(QtGui.QColor(colours["text_muted"]))
            p.drawText(self.rect().adjusted(8, 2, -8, 0),
                       QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft, "LIVE INPUT WAVEFORM")
            if self.frame is None:
                p.drawText(area, QtCore.Qt.AlignCenter, "Waiting for input…")
                return
            ruler = self.rect().adjusted(8, area.bottom() + 2, -8, -2)
            p.drawText(ruler, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                       f"−{WINDOW_SECONDS} s")
            p.drawText(ruler, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, "Now")

            mid = area.top() + area.height() / 2
            p.setPen(QtGui.QColor(colours["border"]))
            p.drawLine(QtCore.QPointF(area.left(), mid), QtCore.QPointF(area.right(), mid))
            # A fixed signed dB scale keeps speech visible at radio levels.
            # Individual clips and blocks are never stretched to fill the plot.
            extrema = self.frame.extrema
            magnitude = np.clip(1 + np.log10(np.maximum(np.abs(extrema), 0.001)) / 3, 0, 1)
            y = mid - np.sign(extrema) * magnitude * (area.height() / 2 - 1)
            step = area.width() * self.frame.seconds_per_bucket / WINDOW_SECONDS
            start = area.right() - (len(y) - 0.5) * step
            p.setClipRect(area)
            pen = QtGui.QPen(QtGui.QColor(colours["accent"]))
            pen.setWidthF(max(1.0, step))
            p.setPen(pen)
            lines = []
            for index, (bottom, top) in enumerate(y):
                x = start + index * step
                lines.append(QtCore.QLineF(x, float(top), x, float(bottom)))
            p.drawLines(lines)
        finally:
            p.end()
