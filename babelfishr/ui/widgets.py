"""Small custom widgets: level meter, live input spectrum, compact waveform."""

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


def _format_hz(hz: float) -> str:
    return f"{hz / 1000:g}k" if hz >= 1000 else f"{hz:g}"


class SpectrumWidget(QtWidgets.QWidget):
    """The visible INPUT meter: a live, orange audio-frequency spectrum.

    Horizontal axis: audio frequency, log-spaced bands (60 Hz up to the
    lower of 10 kHz and the input's Nyquist frequency), with 100 / 1k / 4k
    ticks. Vertical axis: each band's magnitude, -90..0 dBFS, with faint
    -20 / -40 / -60 dBFS rules. A held peak cap sits above each bar.

    The gain information the bar meter carried is kept beside it: a slim
    level column on the right (RMS fill, peak-hold tick and the same -40 /
    -20 / -12 / -6 dBFS marks on a -60..0 scale), the clipping lamp at its
    top, a "CLIP n" count, and the peak/RMS readout in dBFS.

    It clears itself to flat when told (Stop, a change of input, a lost
    device) and also when readings stop arriving, so a stalled input can
    never leave a frozen spectrum that looks live.
    """

    #: Readings older than this are stale: the spectrum falls flat.
    STALE_MS = 700
    LEVEL_COLUMN = 12
    AXIS_HEIGHT = 12

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setMinimumHeight(56)
        self.setMinimumWidth(180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Fixed)
        self._rms = 0.0
        self._peak = 0.0
        self._clipped = False
        self._clip_count = 0
        self._levels: tuple = ()
        self._peaks: tuple = ()
        self._edges: tuple = ()
        self._sample_rate = 0
        self._stale = QtCore.QTimer(self)
        self._stale.setSingleShot(True)
        self._stale.setInterval(self.STALE_MS)
        self._stale.timeout.connect(self._went_stale)
        self.setToolTip(
            "Live input spectrum: frequency across, level up (dBFS). The "
            "column on the right is the overall level - aim for speech peaks "
            "around -12 dBFS; red means the input clipped.")

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(240, 64)

    # -- feeding -----------------------------------------------------------
    def set_reading(self, rms_fraction: float, peak_fraction: float,
                    clipped: bool, clip_count: int = 0, spectrum=None) -> None:
        self._rms = max(0.0, min(1.0, float(rms_fraction)))
        self._peak = max(0.0, min(1.0, float(peak_fraction)))
        self._clipped = bool(clipped)
        self._clip_count = int(clip_count)
        if spectrum is not None:
            self._set_frame(spectrum)
        self._stale.start()
        self.update()

    def set_spectrum(self, spectrum) -> None:
        self._set_frame(spectrum)
        self._stale.start()
        self.update()

    def _set_frame(self, frame) -> None:
        self._levels = tuple(frame.levels_dbfs)
        self._peaks = tuple(frame.peaks_dbfs)
        self._edges = tuple(frame.edges_hz)
        self._sample_rate = int(frame.sample_rate)

    def clear_spectrum(self) -> None:
        """Flat: no bars, no caps. The band layout (axis) is kept."""
        self._levels = self._peaks = ()
        self.update()

    def reset(self) -> None:
        """Everything flat: Stop, a different input, a lost device."""
        self._stale.stop()
        self._rms = self._peak = 0.0
        self._clipped = False
        self._clip_count = 0
        self._levels = self._peaks = ()
        self._edges = ()
        self._sample_rate = 0
        self.update()

    def _went_stale(self) -> None:
        self._rms = self._peak = 0.0
        self._clipped = False
        self.clear_spectrum()

    # -- inspection (tests, accessibility) -------------------------------
    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def edges_hz(self) -> tuple:
        return self._edges

    def bar_fractions(self) -> tuple:
        """0..1 height of every band as drawn; () when flat."""
        from ..audio.spectrum import FLOOR_DBFS

        span = -FLOOR_DBFS
        return tuple(min(1.0, max(0.0, (v - FLOOR_DBFS) / span))
                     for v in self._levels)

    @property
    def flat(self) -> bool:
        return not any(self.bar_fractions())

    # -- drawing ---------------------------------------------------------
    def _x_for_hz(self, hz: float, left: float, width: float) -> float:
        low, high = self._edges[0], self._edges[-1]
        return left + width * (np.log(hz / low) / np.log(high / low))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        from ..audio.spectrum import FLOOR_DBFS
        from . import theme

        t = theme.tokens(self)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.fillRect(rect, QtGui.QColor(t["surface"]))
        painter.setPen(QtGui.QColor(t["border"]))
        painter.drawRect(rect)

        font = QtGui.QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.78))
        painter.setFont(font)
        metrics = QtGui.QFontMetrics(font)
        muted = QtGui.QColor(t["text_muted"])

        column = self.LEVEL_COLUMN
        show_axis = rect.height() >= 48
        axis = self.AXIS_HEIGHT if show_axis else 0
        plot = QtCore.QRectF(rect.left() + 2, rect.top() + 2,
                             rect.width() - column - 6, rect.height() - axis - 3)
        span = -FLOOR_DBFS

        def y_for(db: float) -> float:
            fraction = min(1.0, max(0.0, (db - FLOOR_DBFS) / span))
            return plot.bottom() - fraction * plot.height()

        # Magnitude rules.
        painter.setPen(QtGui.QColor(t["border"]))
        for db in (-20, -40, -60):
            y = y_for(db)
            painter.drawLine(QtCore.QPointF(plot.left(), y),
                             QtCore.QPointF(plot.right(), y))
        if plot.height() >= 36:
            painter.setPen(muted)
            for db in (-20, -60):
                painter.drawText(QtCore.QPointF(plot.left() + 1,
                                                y_for(db) - 1), f"{db}")

        # Bars.
        bars = len(self._levels)
        if bars:
            orange = QtGui.QColor(t["spectrum"])
            cap = QtGui.QColor(t["spectrum_peak"])
            step = plot.width() / bars
            gap = 1.0 if step >= 3 else 0.0
            for index, level in enumerate(self._levels):
                x = plot.left() + index * step
                top = y_for(level)
                if top < plot.bottom() - 0.5:
                    painter.fillRect(QtCore.QRectF(x, top, step - gap,
                                                   plot.bottom() - top), orange)
                if index < len(self._peaks) and self._peaks[index] > FLOOR_DBFS + 0.5:
                    py = y_for(self._peaks[index])
                    painter.fillRect(QtCore.QRectF(x, py - 1, step - gap, 2), cap)

        # Frequency axis.
        if show_axis and len(self._edges) > 1:
            painter.setPen(muted)
            baseline = rect.bottom() - 2
            last_right = -1e9
            ticks = [hz for hz in (100, 1000, 4000) if
                     self._edges[0] <= hz <= self._edges[-1]]
            ticks.append(self._edges[-1])
            for hz in ticks:
                x = self._x_for_hz(hz, plot.left(), plot.width())
                label = _format_hz(round(hz / 100) * 100 if hz >= 1000 else hz)
                width = metrics.horizontalAdvance(label)
                left = min(x - width / 2, plot.right() - width)
                left = max(left, plot.left())
                if left <= last_right + 4:
                    continue
                painter.drawLine(QtCore.QPointF(x, plot.bottom()),
                                 QtCore.QPointF(x, plot.bottom() + 2))
                painter.drawText(QtCore.QPointF(left, baseline), label)
                last_right = left + width
        elif show_axis:
            painter.setPen(muted)
            painter.drawText(QtCore.QRectF(plot.left(), plot.bottom(),
                                           plot.width(), axis),
                             QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                             "frequency (no input)")

        # Gain readout and clipping, top right of the plot.
        if self._rms > 0 or self._peak > 0 or self._clip_count:
            def db_text(fraction: float) -> str:
                return "<-60" if fraction <= 0 else f"{fraction * 60 - 60:.0f}"

            text = f"pk {db_text(self._peak)}  rms {db_text(self._rms)} dBFS"
            painter.setPen(QtGui.QColor(t["text"]))
            box = QtCore.QRectF(plot.left(), plot.top(), plot.width() - 2,
                                metrics.height())
            painter.drawText(box, QtCore.Qt.AlignRight | QtCore.Qt.AlignTop, text)
            if self._clip_count:
                painter.setPen(QtGui.QColor(t["error"]))
                box.translate(0, metrics.height())
                painter.drawText(box, QtCore.Qt.AlignRight | QtCore.Qt.AlignTop,
                                 f"CLIP {self._clip_count}")

        # Level column: -60..0 dBFS bottom to top, the old bar meter's job.
        col = QtCore.QRectF(rect.right() - column - 1, rect.top() + 2,
                            column, rect.height() - 4)
        painter.setPen(QtGui.QColor(t["border"]))
        painter.drawRect(col)
        lamp = 5.0
        meter = QtCore.QRectF(col.left() + 1, col.top() + lamp + 1,
                              col.width() - 1, col.height() - lamp - 1)
        if self._rms > 0:
            gradient = QtGui.QLinearGradient(0, meter.bottom(), 0, meter.top())
            gradient.setColorAt(0.0, QtGui.QColor(t["ok"]))
            gradient.setColorAt(0.75, QtGui.QColor(t["warning"]))
            gradient.setColorAt(0.95, QtGui.QColor(t["error"]))
            height = meter.height() * self._rms
            painter.fillRect(QtCore.QRectF(meter.left(), meter.bottom() - height,
                                           meter.width(), height),
                             QtGui.QBrush(gradient))
        painter.setPen(muted)
        for db in (-40, -20, -12, -6):
            y = meter.bottom() - meter.height() * ((db + 60) / 60.0)
            painter.drawLine(QtCore.QPointF(meter.left(), y),
                             QtCore.QPointF(meter.left() + 3, y))
        if self._peak > 0:
            y = meter.bottom() - meter.height() * self._peak
            painter.setPen(QtGui.QPen(QtGui.QColor(t["text"]), 2))
            painter.drawLine(QtCore.QPointF(meter.left(), y),
                             QtCore.QPointF(meter.right(), y))
        lamp_colour = (QtGui.QColor(t["error"]) if self._clipped
                       else QtGui.QColor(t["surface_raised"]))
        painter.fillRect(QtCore.QRectF(col.left() + 1, col.top() + 1,
                                       col.width() - 1, lamp), lamp_colour)
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


class SessionListBar(QtWidgets.QTabBar):
    """The named Sessions as a vertical list, one row each.

    Still a QTabBar - current index, tab data, icons, tooltips, double-click
    and the context menu behave exactly as the horizontal tab row did - but
    laid out down the left pane and drawn as rows: horizontal text, the
    Session's colour swatch, the selected row filled with the accent and
    marked with a chevron. Keyboard: Up/Down (and Left/Right) move between
    Sessions, as on any tab bar.
    """

    ROW_PADDING = 7

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setShape(QtWidgets.QTabBar.RoundedWest)
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        self.setElideMode(QtCore.Qt.ElideRight)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Maximum)

    def tabSizeHint(self, index: int) -> QtCore.QSize:  # noqa: N802
        height = self.fontMetrics().height() + 2 * self.ROW_PADDING
        return QtCore.QSize(max(self.width(), 160), height)

    def minimumTabSizeHint(self, index: int) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(120, self.tabSizeHint(index).height())

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        # Exactly the rows: no empty band below the last Session.
        rows = sum(self.tabSizeHint(i).height() for i in range(self.count()))
        return QtCore.QSize(max(self.width(), 160), max(rows, 1))

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(120, self.sizeHint().height())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Row width follows the pane; ask for a relayout of the rows.
        self.updateGeometry()
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        from . import theme

        t = theme.tokens(self)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
        metrics = self.fontMetrics()
        for index in range(self.count()):
            rect = self.tabRect(index)
            rect.setLeft(0)
            rect.setRight(self.width() - 1)
            selected = index == self.currentIndex()
            if selected:
                painter.fillRect(rect, QtGui.QColor(t["accent"]))
                colour = QtGui.QColor(t["on_accent"])
            else:
                colour = QtGui.QColor(t["text"])
            if self.hasFocus() and selected:
                painter.setPen(QtGui.QPen(QtGui.QColor(t["text"]), 1,
                                          QtCore.Qt.DotLine))
                painter.drawRect(rect.adjusted(0, 0, -1, -1))
            x = rect.left() + 10
            icon = self.tabIcon(index)
            if not icon.isNull():
                size = min(12, rect.height() - 6)
                icon.paint(painter, QtCore.QRect(
                    x, rect.center().y() - size // 2, size, size))
                x += size + 8
            chevron = "›" if selected else ""
            chevron_width = metrics.horizontalAdvance(" >") if selected else 0
            text_rect = QtCore.QRect(x, rect.top(), rect.right() - x - 8 - chevron_width,
                                     rect.height())
            painter.setPen(colour)
            painter.drawText(text_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                             metrics.elidedText(self.tabText(index),
                                                QtCore.Qt.ElideRight, text_rect.width()))
            if chevron:
                painter.drawText(QtCore.QRect(rect.right() - 8 - chevron_width, rect.top(),
                                              chevron_width, rect.height()),
                                 QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight, ">")
        painter.end()
