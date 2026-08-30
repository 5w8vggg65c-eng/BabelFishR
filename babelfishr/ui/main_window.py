"""The BabelFishR main window."""

from __future__ import annotations

import pathlib
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..app import BabelFishRApp
from ..audio.devices import backend_available, backend_status
from ..audio.devices import (AmbiguousInputDevice, InputDeviceMissing,
                             InputNotSelected)
from ..config import Config
from ..models import (ProcessingState, RadioProfile, SourceLanguageMode,
                      Transmission)
from ..modes import OperatingMode
from ..pipeline import PipelineState
from .input_panel import InputPanel
from .timeline import TimelineView
from .widgets import LevelMeterWidget

#: State labels paired with a semantic colour NAME (resolved per appearance),
#: plus a symbol, so state never depends on colour alone.
STATE_TEXT = {
    PipelineState.IDLE: ("Idle", "idle", "\u25cb"),
    PipelineState.LISTENING: ("Listening", "listening", "\u25c9"),
    PipelineState.RECEIVING: ("Receiving", "receiving", "\u25cf"),
    PipelineState.TRANSCRIBING: ("Transcribing", "working", "\u25d4"),
    PipelineState.TRANSLATING: ("Translating", "working", "\u25d1"),
    PipelineState.COMPLETE: ("Complete", "ok", "\u2713"),
    PipelineState.ERROR: ("Error", "error", "\u26a0"),
}

COMMON_LANGUAGES = [
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
    ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"),
    ("uk", "Ukrainian"), ("ru", "Russian"), ("ar", "Arabic"), ("tr", "Turkish"),
    ("zh", "Chinese"), ("ja", "Japanese"), ("ko", "Korean"), ("hi", "Hindi"),
    ("sv", "Swedish"), ("no", "Norwegian"), ("da", "Danish"), ("fi", "Finnish"),
]


class MainWindow(QtWidgets.QMainWindow):
    """Session header, input controls, live meter and the timeline."""

    def __init__(self, app: BabelFishRApp,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("BabelFishR")
        self.resize(1040, 820)
        self._apply_theme()

        self._state = PipelineState.IDLE
        self._readiness = None
        self._theming = False
        self._readiness_worker = None
        self._analysis_worker = None
        self._build_ui()
        self._refresh_devices()
        self._refresh_profiles()
        self._report_engines()
        self._refresh_mode_badge()
        self._refresh_state_badge()
        self._refresh_readiness(run_smoke_tests=True)
        self._refresh_sdr_label()
        self._reload_timeline()

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._drain_events)
        self._timer.start()

    def _apply_theme(self) -> None:
        """Restyle from the live system palette (light/dark)."""
        from . import theme

        # setStyleSheet itself posts a palette/style change, so without this
        # guard changeEvent re-enters _apply_theme until the stack overflows.
        if getattr(self, "_theming", False):
            return
        self._theming = True
        try:
            self.setStyleSheet(theme.stylesheet(self))
        finally:
            self._theming = False

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        """Follow the operator's system appearance when it changes."""
        if (event.type() in (QtCore.QEvent.PaletteChange,
                             QtCore.QEvent.ApplicationPaletteChange)
                and not getattr(self, "_theming", False)):
            self._apply_theme()
            self._refresh_state_badge()
        super().changeEvent(event)

    # -- construction ----------------------------------------------------
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        root.addLayout(self._build_header())

        # Deliberately outside the collapsible panel below: the operator must
        # be able to see which input is live at every moment of a watch,
        # without expanding anything to find out.
        self.input_box = QtWidgets.QGroupBox("Audio input")
        self.input_box.setObjectName("inputPanel")
        self.input_panel = InputPanel(self.app)
        self.input_panel.selectionChanged.connect(self._on_input_selection)
        input_layout = QtWidgets.QVBoxLayout(self.input_box)
        input_layout.setContentsMargins(10, 6, 10, 8)
        input_layout.addWidget(self.input_panel)
        root.addWidget(self.input_box)

        self.setup_box = QtWidgets.QGroupBox("Session setup")
        self.setup_box.setObjectName("setupPanel")
        self.setup_box.setCheckable(True)
        self.setup_box.setChecked(True)
        self.setup_box.setToolTip("Collapse to give the timeline more room")
        self.setup_box.setLayout(self._build_controls())
        self.setup_box.toggled.connect(self._toggle_setup_panel)
        root.addWidget(self.setup_box)

        self.warning_banner = QtWidgets.QLabel()
        self.warning_banner.setObjectName("warningBanner")
        self.warning_banner.setWordWrap(True)
        self.warning_banner.hide()
        root.addWidget(self.warning_banner)

        self.privacy_banner = QtWidgets.QLabel()
        self.privacy_banner.setObjectName("privacyBanner")
        self.privacy_banner.setWordWrap(True)
        self.privacy_banner.hide()
        root.addWidget(self.privacy_banner)

        self.timeline = TimelineView()
        self.timeline.correctionRequested.connect(self._on_correction)
        self.timeline.tagsChanged.connect(self._on_tags)
        self.timeline.bookmarkToggled.connect(self._on_bookmark)
        self.timeline.retryRequested.connect(self._on_retry)
        self.timeline.exportRequested.connect(self._on_export_clip)
        self.timeline.noteChanged.connect(self._on_note)
        self.timeline.transcribeAnywayRequested.connect(self._on_transcribe_anyway)
        self.timeline.analyzeDigitalRequested.connect(self._on_analyze_digital)
        root.addWidget(self.timeline, 1)

        self.status = self.statusBar()
        self.status.showMessage(backend_status())
        self._build_menu()

    def _build_header(self) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)

        self.start_button = QtWidgets.QPushButton("Start monitoring")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setMinimumWidth(170)
        self.start_button.setDefault(True)
        self.start_button.setShortcut("Ctrl+R")
        self.start_button.setToolTip("Start or stop monitoring (Ctrl+R)")
        self.start_button.setAccessibleName("Start or stop monitoring")
        self.start_button.clicked.connect(self._toggle_monitoring)
        row.addWidget(self.start_button)

        self.state_badge = QtWidgets.QLabel("\u25cb Idle")
        self.state_badge.setObjectName("stateBadge")
        self.state_badge.setAccessibleName("Current state")
        row.addWidget(self.state_badge)

        self.mode_badge = QtWidgets.QLabel()
        self.mode_badge.setObjectName("modeBadge")
        self.mode_badge.setAccessibleName("Operating mode")
        self.mode_badge.setCursor(QtCore.Qt.PointingHandCursor)
        self.mode_badge.setToolTip("Operating mode - click to change")
        self.mode_badge.mousePressEvent = lambda event: self._choose_mode()
        row.addWidget(self.mode_badge)

        self.ready_badge = QtWidgets.QLabel()
        self.ready_badge.setObjectName("chip")
        self.ready_badge.setCursor(QtCore.Qt.PointingHandCursor)
        self.ready_badge.setToolTip("Field readiness - click for the full report")
        self.ready_badge.setAccessibleName("Field readiness")
        self.ready_badge.mousePressEvent = lambda event: self._show_readiness()
        row.addWidget(self.ready_badge)

        self.meter = LevelMeterWidget()
        row.addWidget(self.meter, 1)

        self.clip_label = QtWidgets.QLabel("")
        row.addWidget(self.clip_label)

        self.calibrate_button = QtWidgets.QPushButton("Calibrate")
        self.calibrate_button.setToolTip(
            "Listen to the idle channel for a few seconds and suggest a "
            "detection threshold.")
        self.calibrate_button.clicked.connect(self._calibrate)
        row.addWidget(self.calibrate_button)
        return row

    def _build_controls(self) -> QtWidgets.QGridLayout:
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        grid.addWidget(QtWidgets.QLabel("Radio profile"), 0, 3)
        self.profile_box = QtWidgets.QComboBox()
        self.profile_box.setMinimumWidth(200)
        self.profile_box.currentIndexChanged.connect(self._on_profile_changed)
        grid.addWidget(self.profile_box, 0, 4)

        self.profile_button = QtWidgets.QToolButton()
        self.profile_button.setText("+")
        self.profile_button.setToolTip("Add a radio/channel profile")
        self.profile_button.clicked.connect(self._new_profile)
        grid.addWidget(self.profile_button, 0, 5)

        grid.addWidget(QtWidgets.QLabel("Source language"), 1, 0)
        self.source_mode_box = QtWidgets.QComboBox()
        self.source_mode_box.addItem("Detect automatically", "automatic")
        self.source_mode_box.addItem("Specified", "specified")
        self.source_mode_box.currentIndexChanged.connect(self._on_source_mode)
        grid.addWidget(self.source_mode_box, 1, 1)

        self.source_language_box = QtWidgets.QComboBox()
        for code, name in COMMON_LANGUAGES:
            self.source_language_box.addItem(f"{name} ({code})", code)
        self.source_language_box.setEnabled(False)
        grid.addWidget(self.source_language_box, 1, 2, 1, 2)

        grid.addWidget(QtWidgets.QLabel("Translate into"), 1, 4)
        self.target_language_box = QtWidgets.QComboBox()
        for code, name in COMMON_LANGUAGES:
            self.target_language_box.addItem(f"{name} ({code})", code)
        target = self.app.config.translate.target_language
        index = self.target_language_box.findData(target)
        if index >= 0:
            self.target_language_box.setCurrentIndex(index)
        grid.addWidget(self.target_language_box, 1, 5)

        grid.addWidget(QtWidgets.QLabel("Processing"), 2, 0)
        self.mode_box = QtWidgets.QComboBox()
        for mode in OperatingMode:
            self.mode_box.addItem(mode.label, mode.value)
        index = self.mode_box.findData(self.app.config.mode)
        if index >= 0:
            self.mode_box.setCurrentIndex(index)
        self.mode_box.currentIndexChanged.connect(self._on_mode_box)
        grid.addWidget(self.mode_box, 2, 1)

        self.sdr_label = QtWidgets.QLabel()
        self.sdr_label.setObjectName("sectionLabel")
        grid.addWidget(self.sdr_label, 2, 2, 1, 4)

        self.channel_label = QtWidgets.QLabel("No profile selected")
        self.channel_label.setObjectName("sectionLabel")
        self.channel_label.setWordWrap(True)
        grid.addWidget(self.channel_label, 3, 0, 1, 6)
        return grid

    def _toggle_setup_panel(self, expanded: bool) -> None:
        for child in self.setup_box.findChildren(QtWidgets.QWidget):
            child.setVisible(expanded)

    def _on_mode_box(self) -> None:
        value = self.mode_box.currentData()
        if value and value != self.app.config.mode:
            self.app.set_mode(value)
            self._report_engines()
            self._refresh_mode_badge()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        replay = QtGui.QAction("Replay WAV file...", self)
        replay.triggered.connect(self._replay_file)
        file_menu.addAction(replay)

        file_menu.addSeparator()
        export_session = QtGui.QAction("Export session bundle...", self)
        export_session.triggered.connect(self._export_session)
        file_menu.addAction(export_session)

        export_md = QtGui.QAction("Export transcript (Markdown)...", self)
        export_md.triggered.connect(lambda: self._export_text("md"))
        file_menu.addAction(export_md)

        export_json = QtGui.QAction("Export data (JSON)...", self)
        export_json.triggered.connect(lambda: self._export_text("json"))
        file_menu.addAction(export_json)

        export_csv = QtGui.QAction("Export data (CSV)...", self)
        export_csv.triggered.connect(lambda: self._export_text("csv"))
        file_menu.addAction(export_csv)

        view_menu = self.menuBar().addMenu("&View")
        search = QtGui.QAction("Search transmissions...", self)
        search.setShortcut("Ctrl+F")
        search.triggered.connect(self._search)
        view_menu.addAction(search)

        review = QtGui.QAction("Review queue", self)
        review.triggered.connect(self._show_review_queue)
        view_menu.addAction(review)

        show_all = QtGui.QAction("Show all transmissions", self)
        show_all.setShortcut("Ctrl+Shift+A")
        show_all.setToolTip("Return to the full message thread")
        show_all.triggered.connect(self._reload_timeline)
        view_menu.addAction(show_all)

        tools_menu = self.menuBar().addMenu("&Tools")
        readiness = QtGui.QAction("Field readiness...", self)
        readiness.setShortcut("Ctrl+Shift+R")
        readiness.triggered.connect(self._show_readiness)
        tools_menu.addAction(readiness)

        assistant = QtGui.QAction("Setup assistant...", self)
        assistant.triggered.connect(self._show_assistant)
        tools_menu.addAction(assistant)

        # Diagnostics live on the main window, not only inside the setup
        # assistant. An operator whose first run failed has already closed that
        # dialog by the time they need to describe the problem to somebody, and
        # they should not have to reopen a wizard - or find a log directory by
        # hand - to do it.
        tools_menu.addSeparator()
        self.copy_diagnostics_action = QtGui.QAction(
            "Copy Diagnostic Report", self)
        self.copy_diagnostics_action.setToolTip(
            "Copy a complete description of this installation to the "
            "clipboard. Nothing is sent anywhere.")
        self.copy_diagnostics_action.triggered.connect(
            self._copy_diagnostic_report)
        tools_menu.addAction(self.copy_diagnostics_action)

        self.reveal_logs_action = QtGui.QAction("Reveal Logs in Finder", self)
        self.reveal_logs_action.setToolTip("Open the folder containing the "
                                           "application log.")
        self.reveal_logs_action.triggered.connect(self._reveal_logs)
        tools_menu.addAction(self.reveal_logs_action)

        help_menu = self.menuBar().addMenu("&Help")
        where = QtGui.QAction("Where are my recordings?", self)
        where.triggered.connect(self._show_storage_location)
        help_menu.addAction(where)

        engines = QtGui.QAction("Engine status", self)
        engines.triggered.connect(self._show_engine_status)
        help_menu.addAction(engines)

    # -- population ------------------------------------------------------
    def _refresh_devices(self) -> None:
        """Rescan inputs. Selection is the input panel's business, not ours."""
        self.input_panel.refresh_devices()
        if not backend_available():
            self._warn("No audio backend. Install the audio extra "
                       "(pip install 'babelfishr[audio]') to capture live "
                       "audio. Replaying a WAV file still works.")

    def _on_input_selection(self) -> None:
        self.status.showMessage(self.input_panel.status_label.text(), 6000)

    def _refresh_sdr_label(self) -> None:
        from ..sources import sdr_status

        status = sdr_status(self.app.config)
        if not status["configured"]:
            self.sdr_label.setText("SDR: not configured (optional)")
        elif status["available"]:
            self.sdr_label.setText(f"SDR: {status['detail']}")
        else:
            self.sdr_label.setText(f"SDR: unavailable - {status['reason'][:60]}")

    def _refresh_profiles(self) -> None:
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_box.addItem("No profile (no channel metadata)", None)
        for profile in self.app.profiles():
            self.profile_box.addItem(f"{profile.name} - {profile.label()}", profile.id)
        self.profile_box.blockSignals(False)
        self._on_profile_changed()

    def _report_engines(self) -> None:
        summary = self.app.select_engines()
        messages = list(summary.warnings)
        if messages:
            self._warn("\n".join(messages))
        if summary.privacy_notices:
            self.privacy_banner.setText(
                "⬆ Data leaves this computer: " + "  ".join(summary.privacy_notices))
            self.privacy_banner.show()
        else:
            self.privacy_banner.setText(
                "🔒 All processing runs on this computer. Nothing is uploaded.")
            self.privacy_banner.show()
        self.status.showMessage(
            f"Transcription: {summary.transcription}  |  "
            f"Translation: {summary.translation}  |  {backend_status()}")

    # -- diagnostics -----------------------------------------------------
    def diagnostic_report_path(self) -> pathlib.Path:
        """Where Copy Diagnostic Report leaves a copy on disk."""
        return self.app.config.paths().logs / "diagnostic-report.txt"

    def logs_directory(self) -> pathlib.Path:
        return self.app.config.paths().logs

    def _copy_diagnostic_report(self) -> None:
        from ..diagnostics import diagnostic_report

        try:
            text = diagnostic_report(self.app.config, readiness=self._readiness)
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not crash
            QtWidgets.QMessageBox.warning(
                self, "Diagnostic report",
                f"The report could not be assembled: {exc}")
            return

        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

        saved = ""
        try:
            path = self.diagnostic_report_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            saved = f"\n\nAlso saved to:\n{path}"
        except Exception as exc:  # noqa: BLE001
            saved = f"\n\n(It could not be saved to a file: {exc})"
        QtWidgets.QMessageBox.information(
            self, "Diagnostic report copied",
            "The report is on the clipboard. Paste it into a message to "
            "whoever is helping you. Nothing was sent anywhere." + saved)

    def _reveal_logs(self) -> None:
        """Open the log folder in the platform's file manager."""
        directory = self.logs_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "Logs", f"The log folder could not be created: {exc}")
            return
        url = QtCore.QUrl.fromLocalFile(str(directory))
        if not QtGui.QDesktopServices.openUrl(url):
            # Never leave the operator with nothing: if the file manager will
            # not open, at least tell them the path they can copy.
            QtWidgets.QMessageBox.information(
                self, "Logs",
                f"The log folder could not be opened automatically.\n\n"
                f"It is at:\n{directory}")

    def _warn(self, text: str) -> None:
        self.warning_banner.setText("⚠ " + text)
        self.warning_banner.show()

    # -- monitoring ------------------------------------------------------
    def _toggle_monitoring(self) -> None:
        if self.app.session is not None:
            self._stop_monitoring()
        else:
            self._start_monitoring()

    def _start_monitoring(self, replay_path: Optional[str] = None) -> None:
        identity = None
        if not replay_path:
            ok, message = self.input_panel.ready_to_monitor()
            if not ok:
                self._resolve_input_problem(message)
                return
            identity = self.input_panel.selected_identity()

        mode = self.source_mode_box.currentData()
        try:
            self.app.start_session(
                identity=identity,
                replay_path=replay_path, realtime_replay=bool(replay_path),
                profile_id=self.profile_box.currentData(),
                target_language=self.target_language_box.currentData(),
                source_language=(self.source_language_box.currentData()
                                 if mode == "specified" else None),
                source_language_mode=mode,
            )
            self.app.begin_capture()
        except (AmbiguousInputDevice, InputDeviceMissing,
                InputNotSelected) as exc:
            self.app.stop_session()
            self._resolve_input_problem(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface, never crash
            QtWidgets.QMessageBox.critical(self, "Could not start monitoring", str(exc))
            self.app.stop_session()
            return
        # The timeline is deliberately NOT cleared. Stopping and restarting
        # monitoring is a pause in one continuous radio watch, not a new
        # document. Clearing it here made every earlier transmission look
        # lost, even though the WAVs and the database rows were untouched.
        self.meter.reset()
        self.start_button.setText("Stop monitoring")
        self._set_controls_enabled(False)
        self.input_panel.set_monitoring(True)

    def _resolve_input_problem(self, message: str) -> None:
        """Refuse to start, say exactly why, and offer the real options.

        Deliberately three named actions rather than a bare OK: the operator
        needs to be able to act on this without being tempted to press start
        again and hope.
        """
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("BabelFishR is not receiving anything yet")
        box.setText("Monitoring was not started.")
        box.setInformativeText(message)
        rescan = box.addButton("Rescan", QtWidgets.QMessageBox.ActionRole)
        choose = box.addButton("Choose Different Input",
                               QtWidgets.QMessageBox.ActionRole)
        later = box.addButton("Record Later", QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(rescan)
        box.exec()

        clicked = box.clickedButton()
        if clicked is rescan:
            self.input_panel.refresh_devices()
            ok, _ = self.input_panel.ready_to_monitor()
            if ok:
                self._start_monitoring()
        elif clicked is choose:
            self.input_box.setVisible(True)
            self.input_panel.refresh_devices()
            self.input_panel.device_box.setFocus()
            self.input_panel.device_box.showPopup()
        elif clicked is later:
            self.status.showMessage(
                "Not monitoring. Nothing is being recorded.", 8000)

    def _stop_monitoring(self) -> None:
        self.app.stop_session()
        self.start_button.setText("Start monitoring")
        self._set_controls_enabled(True)
        # Inputs can be changed again, but only once the watch has stopped.
        self.input_panel.set_monitoring(False)
        self._set_state(PipelineState.IDLE)
        self.meter.reset()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.profile_box, self.profile_button,
                       self.source_mode_box, self.target_language_box,
                       self.calibrate_button):
            widget.setEnabled(enabled)
        self.source_language_box.setEnabled(
            enabled and self.source_mode_box.currentData() == "specified")

    def _replay_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Replay a WAV recording", "", "WAV files (*.wav)")
        if not path:
            return
        if self.app.session is not None:
            self._stop_monitoring()
        self._start_monitoring(replay_path=path)

    def _calibrate(self) -> None:
        from ..audio.meter import calibrate
        from ..audio.source import LiveAudioSource

        # Calibrate the input that will actually be monitored. A threshold
        # measured on a different device is worse than no threshold at all.
        ok, message = self.input_panel.ready_to_monitor()
        if not ok:
            self._resolve_input_problem(message)
            return
        try:
            source = LiveAudioSource(
                identity=self.input_panel.selected_identity(),
                sample_rate=self.app.config.audio.sample_rate,
                block_size=self.app.config.audio.block_size,
                reconnect=False)
            source.start()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Calibration", str(exc))
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            result = calibrate(source, seconds=5.0)
        finally:
            source.stop()
            QtWidgets.QApplication.restoreOverrideCursor()

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Input calibration")
        box.setText("<pre>" + result.summary() + "</pre>")
        box.setStandardButtons(QtWidgets.QMessageBox.Apply | QtWidgets.QMessageBox.Close)
        box.button(QtWidgets.QMessageBox.Apply).setText("Use suggested threshold")
        if box.exec() == QtWidgets.QMessageBox.Apply:
            self.app.config.detector.mode = "fixed"
            self.app.config.detector.threshold_dbfs = result.recommended_threshold_dbfs
            self.status.showMessage(
                f"Detection threshold set to {result.recommended_threshold_dbfs} dBFS",
                8000)

    # -- events ----------------------------------------------------------
    def _drain_events(self) -> None:
        for event in self.app.events.drain():
            if event.kind == "level":
                reading = event.payload
                self.meter.set_reading(reading.rms_fraction, reading.peak_fraction,
                                       reading.clipped, reading.clip_count)
                self.input_panel.set_reading(
                    reading.rms_fraction, reading.peak_fraction,
                    reading.clipped, reading.clip_count)
                self.clip_label.setText(
                    f"⚠ clipping ({reading.clip_count})" if reading.clip_count else "")
            elif event.kind == "state":
                self._set_state(event.payload)
            elif event.kind == "transmission":
                self.timeline.add(event.payload)
            elif event.kind == "updated":
                self.timeline.update(event.payload)
            elif event.kind == "audio-status":
                payload = event.payload or {}
                kind = payload.get("kind", "")
                message = payload.get("message", "")
                self.status.showMessage(f"Audio: {kind} - {message}", 8000)
                self.input_panel.report_audio_status(kind, message)
                if kind in ("disconnected", "reconnect-failed"):
                    self._warn(
                        "The selected audio input stopped responding. "
                        "BabelFishR is waiting for that same device and will "
                        "not record from anything else. Transmissions already "
                        "captured are safe.")
            elif event.kind == "error":
                payload = event.payload or {}
                self.status.showMessage(
                    f"{payload.get('stage', 'processing')} error: "
                    f"{payload.get('message', '')}", 12000)

    def _set_state(self, state: str) -> None:
        self._state = state
        self._refresh_state_badge()

    def _refresh_state_badge(self) -> None:
        from . import theme

        state = getattr(self, "_state", PipelineState.IDLE)
        text, tone, symbol = STATE_TEXT.get(
            state, (str(state).title(), "idle", "\u25cb"))
        # Symbol plus word, so the state is never carried by colour alone.
        self.state_badge.setText(f"{symbol} {text}")
        self.state_badge.setStyleSheet(f"color: {theme.status_color(tone, self)};")
        self.state_badge.setAccessibleDescription(text)

    def _refresh_mode_badge(self) -> None:
        mode = self.app.mode
        self.mode_badge.setText(mode.label)
        self.mode_badge.setToolTip(mode.describe() + "\n\nClick to change.")
        index = self.mode_box.findData(mode.value)
        if index >= 0 and self.mode_box.currentIndex() != index:
            self.mode_box.blockSignals(True)
            self.mode_box.setCurrentIndex(index)
            self.mode_box.blockSignals(False)

    def _choose_mode(self) -> None:
        modes = [m.label for m in OperatingMode]
        current = list(OperatingMode).index(self.app.mode)
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "Operating mode", "Mode:", modes, current, False)
        if ok:
            self.app.set_mode(list(OperatingMode)[modes.index(choice)].value)
            self._report_engines()
            self._refresh_mode_badge()

    def _refresh_readiness(self, run_smoke_tests: bool = True) -> None:
        """Refresh the toolbar badge without blocking the interface.

        Smoke tests are on by default. They are the only thing that can
        honestly distinguish "prepared and working" from "prepared but
        untested", and they run on a worker thread, so the cost is a badge
        that reads "Checking" for a few seconds rather than one that lies.
        """
        from .workers import readiness_job, run_in_background

        self._readiness_worker = run_in_background(
            readiness_job, self.app, run_smoke_tests,
            on_finished=self._render_readiness,
            on_failed=lambda message: self.ready_badge.setText("\u26a0 Unknown"))

    def _render_readiness(self, report) -> None:
        from . import theme

        self._readiness = report
        if report.field_ready:
            text, tone = "\u2713 Field ready", "ok"
        elif report.field_ready_unknown:
            # Prepared, but the smoke tests have not run in this process yet.
            # Alpha 3 showed "Record only" here, on a machine with a working
            # Whisper model and working Argos routes, and it stayed wrong
            # until the operator restarted. "Not tested" is not "unavailable".
            text, tone = "\u2026 Checking", "working"
        elif report.can_record:
            text, tone = "\u25d1 Record only", "working"
        else:
            text, tone = "\u26a0 Not ready", "error"
        self.ready_badge.setText(text)
        self.ready_badge.setStyleSheet(f"color: {theme.status_color(tone, self)};")
        self.ready_badge.setAccessibleDescription(text)

    def _show_readiness(self) -> None:
        from .readiness_dialog import ReadinessDialog

        ReadinessDialog(self.app, self).exec()
        self._refresh_readiness()

    # -- transmission actions --------------------------------------------
    def _on_correction(self, tx_id: str, transcript: str, translation: str) -> None:
        self.app.correct(tx_id, transcript=transcript, translation=translation)

    def _on_tags(self, tx_id: str, tags: List[str]) -> None:
        self.app.set_tags(tx_id, tags)

    def _on_bookmark(self, tx_id: str, value: bool) -> None:
        self.app.bookmark(tx_id, value)

    def _on_note(self, tx_id: str, note: str) -> None:
        self.app.correct(tx_id, notes=note)

    def _on_transcribe_anyway(self, tx_id: str) -> None:
        # A WAV on disk does not need a microphone. This works with monitoring
        # stopped, and after quitting and reopening the application.
        if self.app.transcribe_anyway(tx_id):
            self.status.showMessage("Transcribing the saved recording...", 8000)
            return
        QtWidgets.QMessageBox.information(
            self, "Transcribe anyway",
            (self.app.processing_problem(tx_id)
             or "The recording could not be queued for transcription.")
            + "\n\nThe recording itself is safe.")

    def _on_analyze_digital(self, tx_id: str, protocol: str) -> None:
        analyser = self.app.analyser()
        if analyser is None:
            from .analysis_dialog import show_dsd_missing

            show_dsd_missing(self, self.app)
            return
        # dsd-neo can run for seconds - automatic hunting alone rotates for
        # about six - so it must not block the interface.
        from .workers import analysis_job, run_in_background

        self.status.showMessage("Running digital analysis...", 0)
        self._analysis_worker = run_in_background(
            analysis_job, self.app, tx_id, protocol,
            on_finished=self._analysis_finished,
            on_failed=lambda message: self.status.showMessage(
                f"Digital analysis failed: {message}", 12000))

    def _analysis_finished(self, attempt) -> None:
        if attempt is None:
            self.status.showMessage("Digital analysis produced no attempt", 8000)
            return
        message = (f"Digital analysis: {attempt.summary()} "
                   f"({attempt.runtime_seconds:.1f}s) - the recording is "
                   f"unchanged")
        warning = attempt.metadata.get("auto_hunt_warning")
        if warning:
            message += "  |  " + str(warning)
        self.status.showMessage(message, 15000)

    def _on_retry(self, tx_id: str) -> None:
        if self.app.retry(tx_id):
            self.status.showMessage("Retrying the saved recording...", 8000)
            return
        QtWidgets.QMessageBox.information(
            self, "Retry",
            (self.app.processing_problem(tx_id)
             or "The recording could not be queued again.")
            + "\n\nThe recording itself is safe.")

    def _on_export_clip(self, tx_id: str) -> None:
        from ..export import export_transmission_audio

        tx = self.app.store.get_transmission(tx_id)
        if tx is None or not tx.audio_path:
            QtWidgets.QMessageBox.warning(self, "Export", "No audio for this clip.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export clip", f"{tx.id}.wav", "WAV files (*.wav)")
        if path:
            export_transmission_audio(tx, path)
            self.status.showMessage(f"Exported {path}", 6000)

    # -- data views ------------------------------------------------------
    def _reload_timeline(self) -> None:
        """Restore the full message thread, newest traffic last."""
        self.timeline.set_transmissions(self.app.recent_transmissions())

    def _search(self) -> None:
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Search", "Search original and translated text:")
        if not ok:
            return
        results = self.app.search(text)
        self.timeline.set_transmissions(results)
        self.status.showMessage(
            f"{len(results)} match(es) for {text!r} - View > Show all "
            f"transmissions to go back", 10000)

    def _show_review_queue(self) -> None:
        results = self.app.review_queue()
        self.timeline.set_transmissions(results)
        self.status.showMessage(
            f"{len(results)} transmission(s) need review - View > Show all "
            f"transmissions to go back", 10000)

    def _show_assistant(self) -> None:
        from .setup_assistant import SetupAssistant

        SetupAssistant(self.app, self).exec()
        self.refresh_after_setup()

    def refresh_after_setup(self) -> None:
        """Re-read everything preparation can have changed.

        One method, called from both the manually opened assistant and the
        one that opens itself on first run. The automatic path used to skip
        this entirely, so an operator who completed preparation was left
        looking at the pre-setup mode, engines and readiness until they quit
        and reopened the application.
        """
        self.app.select_engines()
        self._refresh_devices()
        self._report_engines()
        self._refresh_mode_badge()
        self._refresh_state_badge()
        self._refresh_readiness(run_smoke_tests=True)
        self._reload_timeline()

    def _show_storage_location(self) -> None:
        stats = self.app.store.stats()
        recordings = pathlib.Path(self.app.config.recording.directory).resolve()
        QtWidgets.QMessageBox.information(
            self, "Storage",
            f"Recordings: {recordings}\n"
            f"Database:  {pathlib.Path(stats['database']).resolve()}\n\n"
            f"{stats['transmissions']} transmission(s) across "
            f"{stats['sessions']} session(s).\n\n"
            "Everything is stored locally on this computer.")

    def _show_engine_status(self) -> None:
        from ..providers import (transcription_engine_status,
                                 translation_engine_status)

        lines = ["Transcription engines:"]
        for status in transcription_engine_status(self.app.config):
            mark = "available" if status.available else "unavailable"
            lines.append(f"  {status.name}: {mark}")
            if status.reason:
                lines.append(f"      {status.reason.splitlines()[0]}")
        lines.append("")
        lines.append("Translation engines:")
        for status in translation_engine_status(self.app.config):
            mark = "available" if status.available else "unavailable"
            lines.append(f"  {status.name}: {mark}  [{status.privacy}]")
            if status.reason:
                lines.append(f"      {status.reason.splitlines()[0]}")
        QtWidgets.QMessageBox.information(self, "Engine status", "\n".join(lines))

    # -- profiles --------------------------------------------------------
    def _on_profile_changed(self) -> None:
        profile_id = self.profile_box.currentData()
        profile = self.app.use_profile(profile_id) if profile_id else None
        # A profile can remember which interface it is wired to. Selecting it
        # selects that input when it is present - and says so plainly when it
        # is not, rather than leaving the previous device in place.
        if getattr(self, "input_panel", None) is not None:
            self.input_panel.set_profile(profile_id)
        if profile is None:
            self.channel_label.setText(
                "No profile selected - transmissions will have no channel or "
                "frequency metadata. BabelFishR cannot determine these from audio.")
        else:
            self.channel_label.setText(
                f"Monitoring {profile.name}  ·  {profile.label()}  "
                f"(operator-supplied metadata)")

    def _new_profile(self) -> None:
        dialog = ProfileDialog(self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        profile = dialog.profile()
        self.app.save_profile(profile)
        self._refresh_profiles()
        index = self.profile_box.findData(profile.id)
        if index >= 0:
            self.profile_box.setCurrentIndex(index)

    def _on_source_mode(self) -> None:
        specified = self.source_mode_box.currentData() == "specified"
        self.source_language_box.setEnabled(specified)

    # -- exports ---------------------------------------------------------
    def _export_session(self) -> None:
        session = self.app.session or (self.app.store.list_sessions(1) or [None])[0]
        if session is None:
            QtWidgets.QMessageBox.information(self, "Export", "No session to export.")
            return
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose a folder for the session bundle")
        if not directory:
            return
        from ..export import export_session

        target = pathlib.Path(directory) / f"babelfishr_{session.id}"
        path = export_session(self.app.store, session.id, str(target))
        self.status.showMessage(f"Exported session bundle to {path}", 10000)

    def _export_text(self, fmt: str) -> None:
        session = self.app.session or (self.app.store.list_sessions(1) or [None])[0]
        if session is None:
            QtWidgets.QMessageBox.information(self, "Export", "No session to export.")
            return
        from .. import export as export_module

        transmissions = self.app.store.list_transmissions(
            session_id=session.id, limit=100_000)
        renderers = {"md": export_module.to_markdown, "json": export_module.to_json,
                     "csv": export_module.to_csv}
        filters = {"md": "Markdown (*.md)", "json": "JSON (*.json)",
                   "csv": "CSV (*.csv)"}
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export", f"babelfishr_{session.id}.{fmt}", filters[fmt])
        if not path:
            return
        renderer = renderers[fmt]
        text = (renderer(transmissions, session) if fmt != "csv"
                else renderer(transmissions))
        pathlib.Path(path).write_text(text, encoding="utf-8")
        self.status.showMessage(f"Exported {path}", 8000)

    # -- shutdown --------------------------------------------------------
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        try:
            self.app.close()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


class ProfileDialog(QtWidgets.QDialog):
    """Create a radio/channel profile - the only source of channel metadata."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("New radio profile")
        layout = QtWidgets.QFormLayout(self)

        self.name = QtWidgets.QLineEdit("My radio")
        self.make = QtWidgets.QLineEdit()
        self.model = QtWidgets.QLineEdit()
        self.channel = QtWidgets.QLineEdit()
        self.frequency = QtWidgets.QLineEdit()
        self.frequency.setPlaceholderText("e.g. 462.5750")
        self.mode = QtWidgets.QLineEdit()
        self.mode.setPlaceholderText("e.g. NFM")

        layout.addRow("Profile name", self.name)
        layout.addRow("Radio make", self.make)
        layout.addRow("Radio model", self.model)
        layout.addRow("Channel name", self.channel)
        layout.addRow("Frequency (MHz)", self.frequency)
        layout.addRow("Mode", self.mode)

        note = QtWidgets.QLabel(
            "These values label your recordings. BabelFishR receives audio only "
            "and cannot measure frequency, channel or mode from it.")
        note.setWordWrap(True)
        note.setObjectName("bubbleHeader")
        layout.addRow(note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def profile(self) -> RadioProfile:
        try:
            frequency = float(self.frequency.text().strip()) if \
                self.frequency.text().strip() else None
        except ValueError:
            frequency = None
        return RadioProfile(
            name=self.name.text().strip() or "Unnamed radio",
            radio_make=self.make.text().strip(),
            radio_model=self.model.text().strip(),
            channel_name=self.channel.text().strip(),
            frequency_mhz=frequency, mode=self.mode.text().strip())
