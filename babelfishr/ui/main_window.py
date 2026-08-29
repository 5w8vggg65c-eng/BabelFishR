"""The BabelFishR main window."""

from __future__ import annotations

import pathlib
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..app import BabelFishRApp
from ..audio.devices import backend_available, backend_status
from ..config import Config
from ..models import (ProcessingState, RadioProfile, SourceLanguageMode,
                      Transmission)
from ..modes import OperatingMode
from ..pipeline import PipelineState
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
        self._build_ui()
        self._refresh_devices()
        self._refresh_profiles()
        self._report_engines()
        self._refresh_mode_badge()
        self._refresh_state_badge()
        self._refresh_readiness()
        self._refresh_sdr_label()

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

        grid.addWidget(QtWidgets.QLabel("Input"), 0, 0)
        self.device_box = QtWidgets.QComboBox()
        self.device_box.setMinimumWidth(230)
        grid.addWidget(self.device_box, 0, 1)

        self.refresh_button = QtWidgets.QToolButton()
        self.refresh_button.setText("↻")
        self.refresh_button.setToolTip("Rescan audio devices")
        self.refresh_button.clicked.connect(self._refresh_devices)
        grid.addWidget(self.refresh_button, 0, 2)

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

        show_all = QtGui.QAction("Show current session", self)
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

        help_menu = self.menuBar().addMenu("&Help")
        where = QtGui.QAction("Where are my recordings?", self)
        where.triggered.connect(self._show_storage_location)
        help_menu.addAction(where)

        engines = QtGui.QAction("Engine status", self)
        engines.triggered.connect(self._show_engine_status)
        help_menu.addAction(engines)

    # -- population ------------------------------------------------------
    def _refresh_devices(self) -> None:
        self.device_box.clear()
        devices = self.app.devices()
        if not devices:
            self.device_box.addItem("No audio input devices found", None)
            self.device_box.setEnabled(False)
            if not backend_available():
                self._warn("No audio backend. Install the audio extra "
                           "(pip install 'babelfishr[audio]') to capture live "
                           "audio. Replaying a WAV file still works.")
        else:
            self.device_box.setEnabled(True)
            for device in devices:
                self.device_box.addItem(device.describe(), device.index)
                if device.is_default:
                    self.device_box.setCurrentIndex(self.device_box.count() - 1)

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
        device = self.device_box.currentData()
        mode = self.source_mode_box.currentData()
        try:
            self.app.start_session(
                device=None if device is None else str(device),
                replay_path=replay_path, realtime_replay=bool(replay_path),
                profile_id=self.profile_box.currentData(),
                target_language=self.target_language_box.currentData(),
                source_language=(self.source_language_box.currentData()
                                 if mode == "specified" else None),
                source_language_mode=mode,
            )
            self.app.begin_capture()
        except Exception as exc:  # noqa: BLE001 - surface, never crash
            QtWidgets.QMessageBox.critical(self, "Could not start monitoring", str(exc))
            self.app.stop_session()
            return
        self.timeline.clear()
        self.meter.reset()
        self.start_button.setText("Stop monitoring")
        self._set_controls_enabled(False)

    def _stop_monitoring(self) -> None:
        self.app.stop_session()
        self.start_button.setText("Start monitoring")
        self._set_controls_enabled(True)
        self._set_state(PipelineState.IDLE)
        self.meter.reset()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.device_box, self.profile_box, self.profile_button,
                       self.source_mode_box, self.target_language_box,
                       self.refresh_button, self.calibrate_button):
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

        device = self.device_box.currentData()
        try:
            source = LiveAudioSource(device=None if device is None else str(device),
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
                self.status.showMessage(
                    f"Audio: {payload.get('kind')} - {payload.get('message')}", 8000)
                if payload.get("kind") in ("disconnected", "reconnect-failed"):
                    self._warn(f"Audio device problem: {payload.get('message')}")
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

    def _refresh_readiness(self, run_smoke_tests: bool = False) -> None:
        from . import theme

        report = self.app.readiness(run_smoke_tests=run_smoke_tests)
        self._readiness = report
        if report.field_ready:
            text, tone = "\u2713 Field ready", "ok"
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
        if not self.app.transcribe_anyway(tx_id):
            QtWidgets.QMessageBox.information(
                self, "Transcribe anyway",
                "Forcing transcription needs a running session with a "
                "transcription engine available.\n\n"
                "The recording is safe either way.")

    def _on_analyze_digital(self, tx_id: str, protocol: str) -> None:
        analyser = self.app.analyser()
        if analyser is None:
            from .analysis_dialog import show_dsd_missing

            show_dsd_missing(self, self.app)
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            attempt = self.app.analyze_digital(tx_id, protocol=protocol)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if attempt is None:
            return
        self.status.showMessage(
            f"Digital analysis: {attempt.summary()} "
            f"({attempt.runtime_seconds:.1f}s) - the recording is unchanged",
            12000)

    def _on_retry(self, tx_id: str) -> None:
        if not self.app.retry(tx_id):
            QtWidgets.QMessageBox.information(
                self, "Retry",
                "Retry needs a running session. Start monitoring, then retry.")

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
        self.timeline.set_transmissions(self.app.transmissions())

    def _search(self) -> None:
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Search", "Search original and translated text:")
        if not ok:
            return
        results = self.app.search(text)
        self.timeline.set_transmissions(results)
        self.status.showMessage(
            f"{len(results)} match(es) for {text!r} - View > Show current session "
            f"to go back", 10000)

    def _show_review_queue(self) -> None:
        results = self.app.review_queue()
        self.timeline.set_transmissions(results)
        self.status.showMessage(
            f"{len(results)} transmission(s) need review", 10000)

    def _show_assistant(self) -> None:
        from .setup_assistant import SetupAssistant

        SetupAssistant(self.app, self).exec()
        self._report_engines()
        self._refresh_mode_badge()
        self._refresh_readiness()

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
