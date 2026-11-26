"""Live input waveform in the same space as the saved recording display."""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..audio.envelope import WINDOW_SECONDS, WaveformFrame
from ..audio.meter import LevelReading
from . import theme
from .waveform import RecordingWaveform


DEFAULT_FLOOR_DBFS = -60.0
DEFAULT_GAIN_DB = 0.0
FLOOR_RANGE = (-90.0, -6.0)
GAIN_RANGE = (0.0, 30.0)


def display_magnitude(extrema, floor_dbfs: float = DEFAULT_FLOOR_DBFS,
                      gain_db: float = DEFAULT_GAIN_DB) -> np.ndarray:
    """0..1 drawing height for each sample peak: its level plus the display
    gain, on a scale from *floor_dbfs* (the centre line) to 0 dBFS (the
    edge). Peaks below the floor draw on the centre line. With the defaults
    (-60 dBFS, +0 dB) this is the original fixed scale. Display only."""
    level = 20.0 * np.log10(np.maximum(np.abs(np.asarray(extrema, dtype=np.float64)),
                                       1e-9)) + gain_db
    return np.clip((level - floor_dbfs) / (0.0 - floor_dbfs), 0.0, 1.0)


class LiveWaveform(QtWidgets.QWidget):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None, display=None,
                 save=None):
        super().__init__(parent)
        #: DisplayConfig (or None): where the floor and gain are remembered.
        self._display = display
        self._save = save
        self.floor_dbfs = float(getattr(display, "live_waveform_floor_dbfs",
                                        DEFAULT_FLOOR_DBFS))
        self.gain_db = float(getattr(display, "live_waveform_gain_db", DEFAULT_GAIN_DB))
        self._build_display_control()
        self.setObjectName("liveWaveform")
        self.setAccessibleName("Live input waveform")
        self.setAccessibleDescription(
            "The last five seconds of incoming audio. New audio enters on the right.")
        self._refresh_tooltip()
        self.setFixedHeight(RecordingWaveform.HEIGHT)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setMinimumWidth(160)
        self.frame: Optional[WaveformFrame] = None
        self._stale = QtCore.QTimer(self)
        self._stale.setInterval(1500)
        self._stale.setSingleShot(True)
        self._stale.timeout.connect(self.reset)

    # -- the display adjustment (rendering only) -----------------------------
    def _build_display_control(self) -> None:
        self.display_button = QtWidgets.QToolButton(self)
        self.display_button.setObjectName("waveformDisplay")
        self.display_button.setText("Display scale \u25be")
        self.display_button.setAccessibleName("Live waveform display scale")
        self.display_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(self.display_button)
        panel = QtWidgets.QWidget(menu)
        form = QtWidgets.QFormLayout(panel)
        form.setContentsMargins(10, 8, 10, 8)
        note = QtWidgets.QLabel(
            "Changes only how the live waveform is drawn - not the recording "
            "level, the start-recording threshold, the meter, the saved "
            "recording or transcription.")
        note.setWordWrap(True)
        note.setMaximumWidth(280)
        form.addRow(note)
        self.floor_box = QtWidgets.QDoubleSpinBox(panel)
        self.floor_box.setRange(*FLOOR_RANGE)
        self.floor_box.setDecimals(0)
        self.floor_box.setSingleStep(3.0)
        self.floor_box.setSuffix(" dBFS")
        self.floor_box.setAccessibleName("Display floor")
        self.floor_box.setToolTip("Peaks below this level are drawn on the centre "
                                  "line. Raise it above a steady noise band to "
                                  "reveal voice peaks.")
        self.gain_box = QtWidgets.QDoubleSpinBox(panel)
        self.gain_box.setRange(*GAIN_RANGE)
        self.gain_box.setDecimals(0)
        self.gain_box.setSingleStep(3.0)
        self.gain_box.setPrefix("+")
        self.gain_box.setSuffix(" dB")
        self.gain_box.setAccessibleName("Display gain")
        self.gain_box.setToolTip("Draws quiet input larger. Display only.")
        form.addRow("Display floor", self.floor_box)
        form.addRow("Display gain", self.gain_box)
        self.reset_display_button = QtWidgets.QPushButton("Reset to \u221260 dBFS, +0 dB", panel)
        self.reset_display_button.setAccessibleName("Reset the live waveform display")
        self.reset_display_button.clicked.connect(self.reset_display)
        form.addRow(self.reset_display_button)
        action = QtWidgets.QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        self.display_button.setMenu(menu)
        self._sync_boxes()
        self.floor_box.valueChanged.connect(lambda v: self.set_display(floor_dbfs=v))
        self.gain_box.valueChanged.connect(lambda v: self.set_display(gain_db=v))

    def _sync_boxes(self) -> None:
        for box, value in ((self.floor_box, self.floor_dbfs), (self.gain_box, self.gain_db)):
            blocked = box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(blocked)

    def set_display(self, floor_dbfs: Optional[float] = None,
                    gain_db: Optional[float] = None) -> None:
        """Change the drawing scale; remembered. Nothing else changes."""
        if floor_dbfs is not None:
            self.floor_dbfs = float(min(max(floor_dbfs, FLOOR_RANGE[0]), FLOOR_RANGE[1]))
        if gain_db is not None:
            self.gain_db = float(min(max(gain_db, GAIN_RANGE[0]), GAIN_RANGE[1]))
        self._sync_boxes()
        if self._display is not None:
            self._display.live_waveform_floor_dbfs = self.floor_dbfs
            self._display.live_waveform_gain_db = self.gain_db
            if self._save is not None:
                try:
                    self._save()
                except OSError:  # noqa: BLE001 - a display preference
                    pass
        self._refresh_tooltip()
        self.update()

    def reset_display(self) -> None:
        self.set_display(floor_dbfs=DEFAULT_FLOOR_DBFS, gain_db=DEFAULT_GAIN_DB)

    def is_default_display(self) -> bool:
        return (self.floor_dbfs, self.gain_db) == (DEFAULT_FLOOR_DBFS, DEFAULT_GAIN_DB)

    def scale_text(self) -> str:
        gain = f" \u00b7 gain +{self.gain_db:.0f} dB" if self.gain_db else ""
        return f"centre {self.floor_dbfs:.0f} dBFS \u2192 edge 0 dBFS{gain}"

    def _refresh_tooltip(self) -> None:
        self.setToolTip(
            "The upper and lower edges are the positive and negative sample "
            f"peaks. Scale: {self.scale_text()}. Display ▾ changes only how "
            "this is drawn.")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        hint = self.display_button.sizeHint()
        self.display_button.setGeometry(self.width() - hint.width() - 6, 1,
                                        hint.width(), 20)

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
            p.drawText(ruler, QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter,
                       self.scale_text())

            mid = area.top() + area.height() / 2
            p.setPen(QtGui.QColor(colours["border"]))
            p.drawLine(QtCore.QPointF(area.left(), mid), QtCore.QPointF(area.right(), mid))
            # A signed dB scale keeps speech visible at radio levels; the
            # operator's display floor and gain set it. Individual clips and
            # blocks are never stretched to fill the plot.
            extrema = self.frame.extrema
            magnitude = display_magnitude(extrema, self.floor_dbfs, self.gain_db)
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
