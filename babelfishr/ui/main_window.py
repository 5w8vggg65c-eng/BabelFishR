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


def _slug(text: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in text.strip())
    return safe.strip("-").lower() or "session"
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
        self.app.restore_selected_conversation()
        self._refresh_session_tabs()
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

        self.setup_box = QtWidgets.QGroupBox("Session Options")
        self.setup_box.setObjectName("setupPanel")
        self.setup_box.setCheckable(True)
        self.setup_box.setChecked(True)
        self.setup_box.setToolTip("Collapse Session Options to give the "
                                  "message thread more room")
        self.setup_box.setAccessibleName("Session Options")
        # One child widget holds every control, and collapsing hides that one
        # widget. The previous version walked findChildren(QWidget) and called
        # setVisible on all of them - which reaches inside the combo boxes to
        # their popup views and inside the scroll areas to their scrollbars,
        # so expanding again made dropdowns and stray scrollbars appear.
        self.setup_content = QtWidgets.QWidget(self.setup_box)
        self.setup_content.setObjectName("setupPanelContent")
        self.setup_content.setLayout(self._build_controls())
        outer = QtWidgets.QVBoxLayout(self.setup_box)
        outer.setContentsMargins(10, 6, 10, 8)
        outer.addWidget(self.setup_content)
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

        # Named Session threads. The database still records one Session per
        # monitoring run; this groups those runs into the continuous log an
        # operator actually thinks in.
        self.session_tabs = QtWidgets.QTabBar()
        self.session_tabs.setObjectName("sessionTabs")
        self.session_tabs.setExpanding(False)
        self.session_tabs.setDrawBase(True)
        self.session_tabs.setAccessibleName("Sessions")
        self.session_tabs.setToolTip(
            "Named Sessions. Monitoring can be started and stopped many "
            "times inside one Session; the thread continues.")
        self.session_tabs.currentChanged.connect(self._on_session_tab)
        self.session_tabs.tabBarDoubleClicked.connect(self._rename_session_tab)

        tab_row = QtWidgets.QHBoxLayout()
        tab_row.setSpacing(6)
        tab_row.addWidget(self.session_tabs)
        self.new_session_button = QtWidgets.QToolButton()
        self.new_session_button.setText("+")
        self.new_session_button.setToolTip("Create a new named Session")
        self.new_session_button.setAccessibleName("New Session")
        self.new_session_button.clicked.connect(self._new_session_tab)
        tab_row.addWidget(self.new_session_button)
        self.rename_session_button = QtWidgets.QToolButton()
        self.rename_session_button.setText("Rename")
        self.rename_session_button.setToolTip("Rename the selected Session")
        self.rename_session_button.setAccessibleName("Rename Session")
        self.rename_session_button.clicked.connect(
            lambda: self._rename_session_tab(self.session_tabs.currentIndex()))
        tab_row.addWidget(self.rename_session_button)
        tab_row.addStretch(1)
        self.capture_tab_label = QtWidgets.QLabel("")
        self.capture_tab_label.setObjectName("sectionLabel")
        tab_row.addWidget(self.capture_tab_label)
        root.addLayout(tab_row)

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

        # A real control, not a label that happens to react to a click. It
        # looks pressable, it takes focus, it opens on Space or Return, and a
        # screen reader announces it as a button - none of which a QLabel with
        # a mousePressEvent does.
        self.mode_button = QtWidgets.QToolButton()
        self.mode_button.setObjectName("modeButton")
        self.mode_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.mode_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.mode_button.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.mode_button.setAccessibleName("Operating mode")
        self.mode_menu = QtWidgets.QMenu(self.mode_button)
        self.mode_actions = {}
        group = QtGui.QActionGroup(self.mode_menu)
        group.setExclusive(True)
        for mode in OperatingMode:
            action = self.mode_menu.addAction(mode.label)
            action.setCheckable(True)
            action.setToolTip(mode.describe())
            action.setData(mode.value)
            action.triggered.connect(
                lambda checked=False, value=mode.value: self._apply_mode(value))
            group.addAction(action)
            self.mode_actions[mode.value] = action
        self.mode_button.setMenu(self.mode_menu)
        row.addWidget(self.mode_button)

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

        # The operating mode lives in the header, next to readiness, and only
        # there. Two controls for one setting is two things to disagree.
        self.sdr_label = QtWidgets.QLabel()
        self.sdr_label.setObjectName("sectionLabel")
        grid.addWidget(self.sdr_label, 2, 0, 1, 6)

        self.channel_label = QtWidgets.QLabel("No profile selected")
        self.channel_label.setObjectName("sectionLabel")
        self.channel_label.setWordWrap(True)
        grid.addWidget(self.channel_label, 3, 0, 1, 6)
        return grid

    def _toggle_setup_panel(self, expanded: bool) -> None:
        """Collapse to the title bar by hiding exactly one widget.

        Never findChildren(QWidget): that set includes each combo box's popup
        view and each scroll area's scrollbars and viewport, and showing those
        directly is what left dropdowns hanging open and scrollbars floating
        after a collapse-expand cycle.
        """
        self.setup_content.setVisible(expanded)
        self.setup_box.setFlat(not expanded)

    def _apply_mode(self, value: str) -> bool:  # noqa: D401
        """Change the mode, or explain why nothing changed.

        The app layer is the guard: it refuses before mutating anything, so a
        refusal here means the configuration, the engines and the badge are
        all still consistent with each other. The combo box is put back to
        match, because a control showing a mode the application is not in is
        the same lie the badge used to tell.
        """
        from ..app import ModeChangeRefused

        try:
            self.app.set_mode(value)
        except ModeChangeRefused as exc:
            QtWidgets.QMessageBox.information(
                self, "The processing mode was not changed", str(exc))
            self._sync_mode_box()
            return False
        self._report_engines()
        self._refresh_mode_badge()
        self._refresh_readiness(run_smoke_tests=True)
        return True

    def _sync_mode_box(self) -> None:
        """Make the control agree with the mode the application is actually in."""
        current = self.app.config.mode
        for value, action in self.mode_actions.items():
            blocked = action.blockSignals(True)
            action.setChecked(value == current)
            action.blockSignals(blocked)

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
        self._refresh_capture_tab_label()
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
        self._refresh_capture_tab_label()
        # Inputs can be changed again, but only once the watch has stopped.
        self.input_panel.set_monitoring(False)
        self._set_state(PipelineState.IDLE)
        self.meter.reset()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.profile_box, self.profile_button,
                       self.source_mode_box, self.target_language_box,
                       self.calibrate_button,
                       # The processing mode decides which engines are loaded.
                       # Changing it under a live capture is refused by the
                       # app layer anyway; greying it out means the operator
                       # is not invited to try. The guard stays: this is the
                       # courtesy, not the enforcement.
                       self.mode_button):
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
                # Only into the thread it was actually filed under. An
                # operator reviewing history must not see live traffic
                # appear in the Session they are reading.
                if self._belongs_here(event.payload):
                    self.timeline.add(event.payload)
            elif event.kind == "updated":
                if self._belongs_here(event.payload):
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

    def _belongs_here(self, tx) -> bool:
        """Is this transmission part of the Session currently on screen?"""
        session_id = getattr(tx, "session_id", "")
        if not session_id:
            return False
        viewing = self.app.conversation_id
        session = self.app.store.get_session(session_id)
        if session is None:
            return False
        return (session.conversation_id or viewing) == viewing

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
        """Label the operating-mode control and mark the current choice.

        The word "mode" is in the button itself, so the toolbar reads
        "Operating mode: Field Offline" next to a separate readiness chip -
        two different things, no longer two identical-looking labels.
        """
        mode = self.app.mode
        self.mode_button.setText(f"Operating mode: {mode.label}  \u25be")
        self.mode_button.setToolTip(
            f"{mode.describe()}\n\nChoose the operating mode. Disabled while "
            f"monitoring is running.")
        self.mode_button.setAccessibleDescription(
            f"Operating mode, currently {mode.label}")
        self._sync_mode_box()

    def _choose_mode(self) -> None:
        modes = [m.label for m in OperatingMode]
        current = list(OperatingMode).index(self.app.mode)
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "Operating mode", "Mode:", modes, current, False)
        if ok:
            self._apply_mode(list(OperatingMode)[modes.index(choice)].value)

    def _refresh_readiness(self, run_smoke_tests: bool = True) -> None:
        """Refresh the toolbar badge without blocking the interface.

        Smoke tests are on by default. They are the only thing that can
        honestly distinguish "prepared and working" from "prepared but
        untested", and they run on a worker thread, so the cost is a badge
        that reads "Checking" for a few seconds rather than one that lies.
        """
        from . import theme
        from .workers import readiness_job, run_in_background

        # Immediately, before the worker is even launched. The badge used to
        # start blank and only ever said "Checking" if a *finished* report
        # happened to contain a skipped smoke test - so during the seconds the
        # check actually takes, the toolbar said nothing at all.
        self.ready_badge.setText("\u2026 Checking")
        self.ready_badge.setStyleSheet(
            f"color: {theme.status_color('working', self)};")
        self.ready_badge.setAccessibleDescription("Checking field readiness")

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
    # -- named Session tabs ----------------------------------------------
    def _refresh_session_tabs(self) -> None:
        """Rebuild the tab bar from the database, keeping the selection."""
        conversations = self.app.conversations()
        selected = self.app.conversation_id
        blocked = self.session_tabs.blockSignals(True)
        while self.session_tabs.count():
            self.session_tabs.removeTab(0)
        for index, conversation in enumerate(conversations):
            self.session_tabs.addTab(conversation.name)
            self.session_tabs.setTabData(index, conversation.id)
            if conversation.id == selected:
                self.session_tabs.setCurrentIndex(index)
        self.session_tabs.blockSignals(blocked)
        self._refresh_capture_tab_label()

    def _refresh_capture_tab_label(self) -> None:
        """Say plainly where live traffic is going when it is not here.

        An operator reading an older Session while a watch runs must never be
        left wondering why nothing is arriving, or worse, assume the traffic
        is being filed where they are looking.
        """
        capture = self.app.capture_conversation_id
        if not capture or capture == self.app.conversation_id:
            self.capture_tab_label.setText("")
            return
        conversation = self.app.store.get_conversation(capture)
        name = conversation.name if conversation else "another Session"
        self.capture_tab_label.setText(
            f"\u25cf Recording into \u201c{name}\u201d")

    def _on_session_tab(self, index: int) -> None:
        conversation_id = self.session_tabs.tabData(index)
        if not conversation_id:
            return
        self.app.select_conversation(conversation_id)
        self._persist_selected_session()
        self._reload_timeline()
        self._refresh_capture_tab_label()

    def _new_session_tab(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(
            self, "New Session", "Name for this Session:")
        if not ok or not name.strip():
            return
        conversation = self.app.create_conversation(name)
        self.app.select_conversation(conversation.id)
        self._persist_selected_session()
        self._refresh_session_tabs()
        self._reload_timeline()

    def _rename_session_tab(self, index: int) -> None:
        conversation_id = self.session_tabs.tabData(index)
        if not conversation_id:
            return
        current = self.session_tabs.tabText(index)
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename Session", "Name for this Session:",
            QtWidgets.QLineEdit.Normal, current)
        if not ok or not name.strip():
            return
        self.app.rename_conversation(conversation_id, name)
        self._refresh_session_tabs()

    def _persist_selected_session(self) -> None:
        """Remember the tab, so a relaunch opens where the operator left."""
        try:
            self.app.config.save()
        except OSError:  # noqa: BLE001 - a preference is never worth a crash
            pass

    def _reload_timeline(self) -> None:
        """Restore the selected Session's thread, newest transmission first."""
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
        self._refresh_session_tabs()
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
        """Export the whole named Session, not the last monitoring run.

        Taking store.list_sessions(1) was the old shortcut, and it quietly
        assumed the newest low-level capture run was what the operator meant
        by "this Session". After a stop and a restart it would have exported
        the tail of the thread and nothing else.
        """
        conversation_id = self.app.conversation_id
        session_ids = self.app.store.session_ids_for_conversation(conversation_id)
        session = self.app.session
        if session is None:
            session = next((s for s in (self.app.store.get_session(i)
                                        for i in session_ids) if s), None)
        if session is None:
            QtWidgets.QMessageBox.information(
                self, "Export",
                "This Session has no monitoring runs to export yet.")
            return
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose a folder for the session bundle")
        if not directory:
            return
        from ..export import export_session

        conversation = self.app.store.get_conversation(conversation_id)
        label = _slug(conversation.name if conversation else session.id)
        target = pathlib.Path(directory) / f"babelfishr_{label}"
        path = export_session(self.app.store, session.id, str(target),
                              conversation_id=conversation_id)
        self.status.showMessage(f"Exported session bundle to {path}", 10000)

    def _export_text(self, fmt: str) -> None:
        """Export the selected named Session, in chronological order.

        A transcript is a record of what was received and when, so it reads
        oldest-first regardless of how the thread is displayed.
        """
        conversation_id = self.app.conversation_id
        session_ids = self.app.store.session_ids_for_conversation(conversation_id)
        session = self.app.session
        if session is None:
            session = next((s for s in (self.app.store.get_session(i)
                                        for i in session_ids) if s), None)
        if session is None:
            QtWidgets.QMessageBox.information(
                self, "Export",
                "This Session has no monitoring runs to export yet.")
            return
        from .. import export as export_module

        transmissions = []
        for other in session_ids:
            transmissions += self.app.store.list_transmissions(
                session_id=other, limit=100_000)
        transmissions.sort(key=lambda t: (t.started_at, t.id))
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
