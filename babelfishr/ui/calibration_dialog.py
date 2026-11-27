"""Calibrate: listen to the quiet input, live, and suggest a threshold.

Opens at once. The input is opened and measured on a worker thread, so the
window stays responsive; every block actually received is drawn as a live
waveform and advances the progress, so the time remaining is the real
listening still to do, not a clock. When the listen ends the existing
summary and recommended threshold are shown with Apply. The threshold in
force does not change unless the operator applies the result. The input is
released when the listen completes, is cancelled, or fails (a device that
cannot be opened, or one that delivers nothing).
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from PySide6 import QtCore, QtWidgets

from ..audio.envelope import RollingEnvelope
from ..audio.meter import CalibrationCancelled, calibrate
from . import theme
from .live_waveform import LiveWaveform

NOTE = "Note: Please be quiet during this process."


class _Relay(QtCore.QObject):
    """Carries the worker's news to the GUI thread (queued signals)."""

    frame = QtCore.Signal(object, float)        # WaveformFrame, collected seconds
    finished = QtCore.Signal(object)            # CalibrationResult
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()


class _Reading:
    def __init__(self, frame):
        self.waveform = frame


class CalibrationDialog(theme.ConsoleDialog):
    """*source_factory()* returns the selected input's (unstarted) source."""

    def __init__(self, source_factory: Callable[[], object], seconds: float = 5.0,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        theme.style_window(self)
        self.setWindowTitle("Calibrate input")
        self.setMinimumWidth(320)
        self.seconds = float(seconds)
        self.result = None
        self.applied = False
        self._source_factory = source_factory
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.source_released = threading.Event()

        layout = QtWidgets.QVBoxLayout(self)
        self.note = QtWidgets.QLabel(NOTE)
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.waveform = LiveWaveform()
        self.waveform.display_button.hide()      # a fixed scale here
        layout.addWidget(self.waveform)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Listening progress")
        layout.addWidget(self.progress)
        self.status = QtWidgets.QLabel("Opening the input…")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                  QtWidgets.QSizePolicy.Preferred)
        self.status.setAccessibleName("Calibration status")
        layout.addWidget(self.status)
        self.summary = QtWidgets.QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(150)
        self.summary.hide()
        layout.addWidget(self.summary)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.apply_button = QtWidgets.QPushButton("Use suggested threshold")
        self.apply_button.setObjectName("primaryAction")
        self.apply_button.hide()
        self.apply_button.clicked.connect(self._apply)
        self.close_button = QtWidgets.QPushButton("Cancel")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self._relay = _Relay(self)
        self._relay.frame.connect(self._on_frame)
        self._relay.finished.connect(self._on_finished)
        self._relay.failed.connect(self._on_failed)
        self._relay.cancelled.connect(lambda: self.status.setText("Calibration cancelled."))

    # -- the listen, on its own thread -----------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._listen, daemon=True,
                                        name="babelfishr-calibrate")
        self._thread.start()

    def _listen(self) -> None:
        relay, stop = self._relay, self._stop
        source = None
        try:
            source = self._source_factory()
            source.start()
            envelope = RollingEnvelope(source.sample_rate)

            def on_block(block, collected):
                envelope.feed(block.samples)
                frame = envelope.frame()
                if frame is not None and not stop.is_set():
                    relay.frame.emit(frame, collected)

            result = calibrate(source, seconds=self.seconds, on_block=on_block,
                               cancelled=stop.is_set)
        except CalibrationCancelled:
            self._emit(relay.cancelled)
            return
        except Exception as exc:  # noqa: BLE001 - a device or permission failure
            self._emit(relay.failed, f"The input could not be measured: {exc}")
            return
        finally:
            if source is not None:
                try:
                    source.stop()
                except Exception:  # noqa: BLE001 - releasing is best effort
                    pass
            self.source_released.set()
        self._emit(relay.finished, result)

    @staticmethod
    def _emit(signal, *args) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:              # the dialog is gone
            pass

    # -- on the GUI thread ---------------------------------------------------
    def _on_frame(self, frame, collected: float) -> None:
        self.waveform.feed(_Reading(frame))
        done = min(1.0, collected / self.seconds) if self.seconds else 1.0
        self.progress.setValue(int(done * 1000))
        left = max(0.0, self.seconds - collected)
        self.status.setText(f"Listening… {left:.1f} s remaining")

    def _on_finished(self, result) -> None:
        self.result = result
        self.progress.setValue(1000)
        self.summary.setPlainText(result.summary())
        self.summary.show()
        usable = bool(result.ok)
        self.status.setText(
            f"Suggested detection threshold: {result.recommended_threshold_dbfs:.1f} dBFS. "
            "Nothing changes unless you apply it." if usable else
            "No usable measurement: " + "; ".join(result.warnings or ["see below"])
            + ". The threshold in force is unchanged.")
        self.apply_button.setVisible(usable)
        self.close_button.setText("Close")

    def _on_failed(self, message: str) -> None:
        self.status.setText(message + " The threshold in force is unchanged.")
        self.close_button.setText("Close")

    def _apply(self) -> None:
        self.applied = True
        self.accept()

    def done(self, code: int) -> None:  # noqa: D401 - Qt's name
        # Closing (Cancel, Close, Escape) while listening stops the listen;
        # the worker releases the input at its next block.
        self._stop.set()
        super().done(code)
