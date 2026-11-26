"""The BabelFishR main window."""

from __future__ import annotations

import logging
import pathlib
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..app import BabelFishRApp
from ..audio.devices import backend_available, backend_status
from ..audio.devices import (AmbiguousInputDevice, InputDeviceMissing,
                             InputNotSelected)
from ..audio.source import CLIP_THRESHOLD
from ..config import Config
from ..models import (ProcessingState, RadioProfile, SourceLanguageMode,
                      Transmission)
from ..modes import OperatingMode
from ..pipeline import PipelineState
from .input_panel import InputPanel


def _slug(text: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in text.strip())
    return safe.strip("-").lower() or "session"
from .timeline import FlowLayout, TimelineView
from .widgets import LevelMeterWidget

log = logging.getLogger(__name__)

#: State labels paired with a semantic colour NAME (resolved per appearance),
#: plus a symbol, so state never depends on colour alone.
#: LISTENING and RECEIVING come from the audio-level gate (detect.py), not
#: from the receiver: no input BabelFishR supports reports squelch, carrier
#: or RX state, so they are worded as what they are - armed and waiting,
#: and audio activity estimated from the level.
STATE_TEXT = {
    PipelineState.IDLE: ("Idle", "idle", "\u25cb"),
    PipelineState.LISTENING: ("Armed \u00b7 Listening", "listening", "\u25c9"),
    PipelineState.RECEIVING: ("Audio active (est.)", "receiving", "\u25cf"),
    PipelineState.TRANSCRIBING: ("Transcribing", "working", "\u25d4"),
    PipelineState.TRANSLATING: ("Translating", "working", "\u25d1"),
    PipelineState.COMPLETE: ("Complete", "ok", "\u2713"),
    PipelineState.ERROR: ("Error", "error", "\u26a0"),
}

STATE_TIPS = {
    PipelineState.LISTENING: (
        "Monitoring is armed. A message starts by itself when the audio level "
        "rises above the activity threshold and ends after it stays below it "
        "for the set seconds of quiet (End a message after). This is the audio "
        "feed, not the radio's squelch."),
    PipelineState.RECEIVING: (
        "Audio activity, estimated from the input level. BabelFishR receives no "
        "squelch, carrier or RX signal from this input, so this is not the "
        "receiver's own status: noise loud enough can look the same."),
}

COMMON_LANGUAGES = [
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
    ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"),
    ("uk", "Ukrainian"), ("ru", "Russian"), ("ar", "Arabic"), ("tr", "Turkish"),
    ("zh", "Chinese"), ("ja", "Japanese"), ("ko", "Korean"), ("hi", "Hindi"),
    ("sv", "Swedish"), ("no", "Norwegian"), ("da", "Danish"), ("fi", "Finnish"),
]


def _color_swatch(color: str, size: int = 12) -> QtGui.QIcon:
    """A small rounded square in the operator's colour; empty when unset."""
    if not color:
        return QtGui.QIcon()
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setBrush(QtGui.QColor(color))
    painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 60), 1))
    painter.drawRoundedRect(0.5, 0.5, size - 1, size - 1, 3, 3)
    painter.end()
    return QtGui.QIcon(pixmap)


def _ordinal(n: int) -> str:
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


class _KeyboardContextMenu(QtCore.QObject):
    """A keyboard context-menu request (Menu key, Shift+F10 where the
    platform turns it into one) on the Sessions list opens the selected
    Session's menu; a mouse request is left to the normal right-click path."""

    def __init__(self, open_menu, parent):
        super().__init__(parent)
        self._open_menu = open_menu

    def eventFilter(self, obj, event):  # noqa: N802 - Qt's name
        if (event.type() == QtCore.QEvent.ContextMenu
                and event.reason() == QtGui.QContextMenuEvent.Keyboard):
            event.accept()
            self._open_menu()
            return True
        return False


class _SpacePlayback(QtCore.QObject):
    """Space starts and stops playback - scoped to playback.

    Watches key events application-wide, for this window only, and takes a
    plain Space only when the keyboard focus is somewhere playback lives:
    the message list (not a text field or one of a card's own other
    buttons), the waveform, or the playback bar's other parts - its
    Bookmark and Add note buttons included, so focus that drifts onto them
    can never turn Space into a bookmark. A press acts once; its release
    and auto-repeats are swallowed. The transport's own buttons (Play
    selected, Pause/Resume, Stop, -5/+5 s), text fields (the inline
    editor, search, spin boxes) and every other control get Space as usual:
    a deliberately focused button does what its label says.
    """

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window
        self._swallowing = False
        self._watched = None
        # Never on the whole application: only the widget that has the
        # keyboard focus in this window (and the window itself, for when
        # nothing does). Geometry and paint events elsewhere never pass
        # through Python.
        window.installEventFilter(self)
        QtWidgets.QApplication.instance().focusChanged.connect(self._focus_moved)
        self._focus_moved(None, QtWidgets.QApplication.focusWidget())

    def _focus_moved(self, _old, new) -> None:
        watched = self._watched
        if watched is not None:
            try:
                watched.removeEventFilter(self)
            except RuntimeError:           # the widget is gone already
                pass
            self._watched = None
        if new is not None and new is not self.window:
            try:
                if new.window() is self.window:
                    new.installEventFilter(self)
                    self._watched = new
            except RuntimeError:
                pass

    def detach(self) -> None:
        try:
            QtWidgets.QApplication.instance().focusChanged.disconnect(self._focus_moved)
        except (RuntimeError, TypeError):
            pass
        self._focus_moved(None, None)
        self.window.removeEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt's name
        kind = event.type()
        if kind not in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease,
                        QtCore.QEvent.ShortcutOverride):
            return False
        if event.key() != QtCore.Qt.Key_Space:
            return False
        if event.modifiers() & ~QtCore.Qt.KeypadModifier:
            return False
        if kind == QtCore.QEvent.KeyRelease and self._swallowing:
            # The press acted, and may have moved focus (a button that became
            # disabled): its release is swallowed wherever it lands, so it
            # can never click whatever has focus now.
            if not event.isAutoRepeat():
                self._swallowing = False
            return True
        focus = QtWidgets.QApplication.focusWidget()
        if obj is not focus and not (focus is None and obj is self.window):
            return False                   # once, at the widget that has focus
        if not self.window.space_is_playback(focus):
            return False
        if kind == QtCore.QEvent.ShortcutOverride:
            event.accept()                 # ours, not a shortcut's
            return True
        if kind == QtCore.QEvent.KeyPress:
            self._swallowing = True
            if not event.isAutoRepeat():   # held down: acted on once
                self.window.toggle_playback_from_space()
        return True


class MainWindow(QtWidgets.QMainWindow):
    """Session header, input controls, live meter and the timeline."""

    def __init__(self, app: BabelFishRApp,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.app = app
        from ..extensions import product

        self.setWindowTitle(product().name)
        self.resize(1040, 820)
        self._apply_theme()

        self._state = PipelineState.IDLE
        # Set once the application has actually closed - engines and store
        # included. From then on no timer callback touches the store.
        self._shutdown_complete = False
        self._shutdown_error = ""
        # Set the moment the application hands its final cleanup to its own
        # thread: from then on the store may close at any instant, so no
        # timer callback or control here may read it.
        self._store_off_limits = False
        self._waiting_for_dialog = False
        # What the thread is showing: the Session's thread, a Search result
        # or the Review queue. Live events are admitted through the same
        # store query that opened the view, so what arrives cannot widen it.
        self._view_kind = "thread"
        self._view_query = ""
        self._readiness = None
        self._theming = False
        self._readiness_worker = None
        # One entry per thing that can be wrong, so clearing one cannot hide
        # another that is still true. See _warn.
        self._warnings: Dict[str, str] = {}
        from ..timefmt import set_display_zone

        zone_problem = set_display_zone(self.app.config.display.timezone,
                                        self.app.config.display.utc_zone)
        self._build_ui()
        if zone_problem:
            self._warn(zone_problem, source="time-zone")
        self._refresh_devices()
        self._refresh_profiles()
        self._report_engines()
        self._refresh_mode_badge()
        self._refresh_state_badge()
        self._refresh_install_model_button()
        self._space_filter = _SpacePlayback(self)
        self.app.restore_selected_conversation()
        self._refresh_session_tabs()
        self._reload_timeline()
        self._refresh_save_path_row()
        # After the selected Session is restored: its save path is checked.
        self._refresh_readiness(run_smoke_tests=True)
        # Only a separate product's own entry point registers anything here
        # (babelfishr.extensions); started as BabelFishR this is empty.
        from ..extensions import hooks

        for window_built in hooks("window_built"):
            window_built(self)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._drain_events)
        self._timer.start()
        # Quitting with work outstanding waits on the event loop, not by
        # blocking it: this timer retries the close until nothing can still
        # use the engines or the store.
        self._quit_timer = QtCore.QTimer(self)
        self._quit_timer.setInterval(250)
        self._quit_timer.timeout.connect(self._quit_tick)

    def _apply_theme(self) -> None:
        """Restyle from the live system palette (light/dark)."""
        from . import theme

        # setStyleSheet itself posts a palette/style change, so without this
        # guard changeEvent re-enters _apply_theme until the stack overflows.
        if getattr(self, "_theming", False):
            return
        self._theming = True
        try:
            # The field console in dark appearance is the window's own
            # palette, so natively drawn controls match it; in light
            # appearance this resets to the system palette.
            self.setPalette(theme.palette(self))
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
        """The v5 field console: identity row, teal status band, the in-window
        menu as navigation, then two panes - Sessions, input and local
        engines on the left; waveform, review list and the selected
        recording's actions on the right - over a footer status band.

        Every control is an existing one, re-homed; every label shows a real
        state. Nothing here is sample data.
        """
        from .live_waveform import LiveWaveform
        from .waveform import RecordingWaveform
        from .widgets import SessionListBar

        central = QtWidgets.QWidget()
        central.setObjectName("centralRoot")
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- identity row --------------------------------------------------
        self.title_bar = QtWidgets.QFrame()
        self.title_bar.setObjectName("titleBar")
        title_row = QtWidgets.QHBoxLayout(self.title_bar)
        title_row.setContentsMargins(14, 4, 14, 4)
        from ..extensions import product

        self.app_title = QtWidgets.QLabel(product().name)
        self.app_title.setObjectName("appTitle")
        title_row.addWidget(self.app_title)
        title_row.addStretch(1)
        #: Empty in the application. A preview or demonstration sets it so
        #: sample data can never be mistaken for real traffic.
        self.title_note = QtWidgets.QLabel("")
        self.title_note.setObjectName("titleNote")
        title_row.addWidget(self.title_note)
        root.addWidget(self.title_bar)

        # -- status band: operating mode | source | state ------------------
        self.status_band = QtWidgets.QFrame()
        self.status_band.setObjectName("statusBand")
        band = QtWidgets.QHBoxLayout(self.status_band)
        band.setContentsMargins(10, 4, 14, 4)
        band.setSpacing(12)
        self._build_header(band)
        root.addWidget(self.status_band)
        # Kept under its old name: the header row is now the status band.
        self.header_band = self.status_band

        # Navigation: the window's own menu bar, below the band as in the
        # approved layout. menuBar() returns this bar, so every menu, action
        # and shortcut is the same object as before.
        self._nav_bar = QtWidgets.QMenuBar(central)
        root.addWidget(self._nav_bar)

        # -- the two panes --------------------------------------------------
        panes = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        panes.setObjectName("paneSplitter")
        panes.setChildrenCollapsible(False)
        panes.setHandleWidth(1)
        root.addWidget(panes, 1)
        self.pane_splitter = panes

        left = QtWidgets.QFrame()
        left.setObjectName("leftPane")
        left.setMinimumWidth(240)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(4)
        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        left_scroll.setWidget(left)
        left_scroll.setMinimumWidth(250)
        self.sidebar = left_scroll
        self._sidebar_width = 320
        panes.addWidget(left_scroll)

        right = QtWidgets.QFrame()
        right.setObjectName("rightPane")
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 0)
        right_layout.setSpacing(6)
        panes.addWidget(right)
        panes.setStretchFactor(0, 0)
        panes.setStretchFactor(1, 1)
        panes.setSizes([320, 900])

        # -- left: SESSIONS --------------------------------------------------
        left_layout.addWidget(self._section_header("SESSIONS"))
        # Named Session threads. The database still records one Session per
        # monitoring run; this groups those runs into the continuous log an
        # operator actually thinks in.
        self.session_tabs = SessionListBar()
        self.session_tabs.setObjectName("sessionTabs")
        self.session_tabs.setAccessibleName("Sessions")
        self.session_tabs.setToolTip(
            "Named Sessions. Monitoring can be started and stopped many "
            "times inside one Session; the thread continues.\n\n"
            "To rename a Session: double-click it, right-click it, "
            "or select it and press Rename.\n\n"
            "Right-click a Session (or press Shift+F10 or the Menu key) for "
            "its colour, Clear Session History, Hide and Delete.")
        self.session_tabs.currentChanged.connect(self._on_session_tab)
        self.session_tabs.tabBarDoubleClicked.connect(self._rename_session_tab)
        self.session_tabs.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.session_tabs.customContextMenuRequested.connect(
            self._session_tab_menu)
        # The same context menu from the keyboard, for the selected Session.
        # Platforms route these keys differently: as a shortcut, or (macOS's
        # Menu key, some keyboards' Shift+F10) as a keyboard context-menu
        # event whose position is not over any tab. Both end in
        # _show_session_actions; right-click stays position-based.
        for keys in ("Shift+F10", QtCore.Qt.Key_Menu):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(keys), self.session_tabs)
            shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._show_session_actions)
        self._session_keyboard_menu = _KeyboardContextMenu(self._show_session_actions,
                                                           self.session_tabs)
        self.session_tabs.installEventFilter(self._session_keyboard_menu)
        left_layout.addWidget(self.session_tabs)

        session_buttons = FlowLayout(spacing=5)
        session_buttons.setContentsMargins(0, 2, 0, 2)
        self.new_session_button = QtWidgets.QToolButton()
        self.new_session_button.setText("+ New")
        self.new_session_button.setToolTip("Create a new named Session")
        self.new_session_button.setAccessibleName("New Session")
        self.new_session_button.clicked.connect(self._new_session_tab)
        session_buttons.addWidget(self.new_session_button)
        self.rename_session_button = QtWidgets.QToolButton()
        self.rename_session_button.setText("Rename…")
        self.rename_session_button.setToolTip(
            "Rename the selected Session. Its messages and recordings are "
            "kept; only the name changes.")
        self.rename_session_button.setAccessibleName("Rename Session")
        self.rename_session_button.clicked.connect(
            lambda: self._rename_session_tab(self.session_tabs.currentIndex()))
        session_buttons.addWidget(self.rename_session_button)
        left_layout.addLayout(session_buttons)
        # Only while a confirmed deletion still has files left on disk.
        self.cleanup_button = QtWidgets.QPushButton("Retry file cleanup\u2026")
        self.cleanup_button.setObjectName("paneLink")
        self.cleanup_button.setFlat(True)
        self.cleanup_button.setAccessibleName("Retry file cleanup")
        self.cleanup_button.clicked.connect(lambda: self._finish_unfinished_deletions())
        self.cleanup_button.hide()
        left_layout.addWidget(self.cleanup_button)
        # The concept's "Saved sessions" and "Search": the real View actions.
        self.hidden_sessions_link = self._pane_link("Hidden sessions",
                                                    checkable=True)
        self.search_link = self._pane_link("Search")
        self.search_link.setToolTip("Search this Session's original and "
                                    "translated text (Ctrl+F)")
        left_layout.addWidget(self.hidden_sessions_link)
        left_layout.addWidget(self.search_link)
        left_layout.addWidget(self._rule())

        # -- left: INPUT ------------------------------------------------------
        left_layout.addWidget(self._section_header("INPUT"))
        # Deliberately always visible: the operator must be able to see which
        # input is live at every moment of a watch.
        self.input_box = QtWidgets.QFrame()
        self.input_box.setObjectName("inputArea")
        self.input_panel = InputPanel(self.app)
        self.input_panel.selectionChanged.connect(self._on_input_selection)
        input_layout = QtWidgets.QVBoxLayout(self.input_box)
        input_layout.setContentsMargins(4, 2, 4, 2)
        input_layout.addWidget(self.input_panel)
        left_layout.addWidget(self.input_box)
        # The radio profile in use, when there is one (the source and target
        # languages are the editable controls themselves, not a summary).
        self.profile_line = QtWidgets.QLabel("")
        self.profile_line.setObjectName("paneLine")
        self.profile_line.setWordWrap(True)
        self.profile_line.setAccessibleName("Radio profile")
        self.profile_line.hide()
        left_layout.addWidget(self.profile_line)
        level_row = QtWidgets.QHBoxLayout()
        level_row.setContentsMargins(10, 2, 0, 2)
        # The input's own meter (above) is the visible one; this one is kept
        # for the calibration and capture code that feeds it.
        self.meter = LevelMeterWidget()
        self.meter.hide()
        level_row.addWidget(self.meter, 1)
        level_row.addStretch(1)
        self.clip_label = QtWidgets.QLabel("")
        self.clip_label.setWordWrap(True)
        self.clip_label.setAccessibleName("Clipped input samples")
        level_row.addWidget(self.clip_label)
        self.calibrate_button = QtWidgets.QPushButton("Calibrate")
        self.calibrate_button.setToolTip(
            "Listen to the idle channel for a few seconds and suggest a "
            "detection threshold.")
        self.calibrate_button.clicked.connect(self._calibrate)
        level_row.addWidget(self.calibrate_button)
        left_layout.addLayout(level_row)
        left_layout.addWidget(self._rule())

        # -- left: LOCAL ENGINES ---------------------------------------------
        from .local_engines import WhisperModelPicker

        left_layout.addWidget(self._section_header("LOCAL ENGINES"))
        self.speech_engine_line = QtWidgets.QLabel("Speech: …")
        self.speech_engine_line.setObjectName("engineLine")
        self.speech_engine_line.setWordWrap(True)
        self.translation_engine_line = QtWidgets.QLabel("Translation: …")
        self.translation_engine_line.setObjectName("engineLine")
        self.translation_engine_line.setWordWrap(True)
        left_layout.addWidget(self.speech_engine_line)
        model_row = QtWidgets.QHBoxLayout()
        self.whisper_model_box = WhisperModelPicker(self.app)
        model_label = QtWidgets.QLabel("Whisper model")
        model_label.setObjectName("engineLine")
        model_label.setBuddy(self.whisper_model_box)
        model_row.addWidget(model_label)
        model_row.addWidget(self.whisper_model_box, 1)
        # Install a speech model that is not here yet (Turbo among them) -
        # Online / Setup only, never while monitoring.
        self.install_model_button = QtWidgets.QToolButton()
        self.install_model_button.setText("Install a speech model\u2026")
        self.install_model_button.setAccessibleName("Install a Whisper speech model")
        self.install_model_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.install_model_menu = QtWidgets.QMenu(self.install_model_button)
        self.install_model_menu.aboutToShow.connect(self._fill_install_model_menu)
        self.install_model_button.setMenu(self.install_model_menu)
        self._install_worker = None
        left_layout.addLayout(model_row)
        install_row = QtWidgets.QHBoxLayout()
        install_row.addStretch(1)
        install_row.addWidget(self.install_model_button)
        left_layout.addLayout(install_row)
        self.whisper_model_box.currentIndexChanged.connect(self._change_whisper_model)
        self._build_language_controls(left_layout)
        left_layout.addWidget(self.translation_engine_line)
        self.ready_badge = QtWidgets.QLabel()
        self.ready_badge.setObjectName("readinessLink")
        self.ready_badge.setCursor(QtCore.Qt.PointingHandCursor)
        self.ready_badge.setToolTip("Field readiness - click for the full report")
        self.ready_badge.setAccessibleName("Field readiness")
        self.ready_badge.mousePressEvent = lambda event: self._show_readiness()
        left_layout.addWidget(self.ready_badge)
        left_layout.addStretch(1)
        # The narrowest the sidebar may become still fits its contents and
        # its vertical scroll bar: nothing is clipped at the right edge.
        self.sidebar.setMinimumWidth(max(
            250, left.minimumSizeHint().width()
            + self.sidebar.verticalScrollBar().sizeHint().width() + 2))

        # -- right: header, banners --------------------------------------------
        header = QtWidgets.QFrame()
        header.setObjectName("paneHeader")
        header_row = QtWidgets.QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        self.sidebar_toggle = QtWidgets.QToolButton()
        self.sidebar_toggle.setText("Sidebar")
        self.sidebar_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.sidebar_toggle.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.sidebar_toggle.setCheckable(True)
        self.sidebar_toggle.setChecked(True)
        self.sidebar_toggle.toggled.connect(self._set_sidebar_visible)
        self._set_sidebar_visible(True)
        header_row.addWidget(self.sidebar_toggle)
        heading = QtWidgets.QLabel("Transcript and translation")
        heading.setObjectName("paneHeading")
        header_row.addWidget(heading)
        header_row.addStretch(1)
        self.capture_tab_label = QtWidgets.QLabel("")
        self.capture_tab_label.setObjectName("sectionLabel")
        header_row.addWidget(self.capture_tab_label)
        right_layout.addWidget(header)

        self.warning_banner = QtWidgets.QLabel()
        self.warning_banner.setObjectName("warningBanner")
        self.warning_banner.setWordWrap(True)
        self.warning_banner.hide()
        right_layout.addWidget(self.warning_banner)

        self.privacy_banner = QtWidgets.QLabel()
        self.privacy_banner.setObjectName("privacyBanner")
        self.privacy_banner.setWordWrap(True)
        self.privacy_banner.hide()
        right_layout.addWidget(self.privacy_banner)

        # -- right: the selected recording's waveform ---------------------------
        self.waveform = RecordingWaveform()
        self.live_waveform = LiveWaveform(display=self.app.config.display,
                                          save=self.app.config.save)
        self.waveform_stack = QtWidgets.QStackedWidget()
        self.waveform_stack.addWidget(self.waveform)
        self.waveform_stack.addWidget(self.live_waveform)
        right_layout.addWidget(self.waveform_stack)

        # -- right: filters and search ------------------------------------------
        filter_row = QtWidgets.QHBoxLayout()
        filter_row.setSpacing(6)
        self.filter_group = QtWidgets.QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.filter_all = self._filter_button("All messages")
        self.filter_review = self._filter_button("Needs review")
        self.filter_bookmarked = self._filter_button("Bookmarked")
        self.filter_removed = self._filter_button("Removed")
        self.filter_removed.setToolTip(
            "Messages removed from this Session's thread - their recordings and "
            "text are kept - with Restore to put them back")
        for button in (self.filter_all, self.filter_review, self.filter_bookmarked,
                       self.filter_removed):
            self.filter_group.addButton(button)
            filter_row.addWidget(button)
        self.filter_all.setChecked(True)
        filter_row.addStretch(1)
        self.search_field = QtWidgets.QLineEdit()
        self.search_field.setObjectName("searchField")
        self.search_field.setPlaceholderText("Search...")
        self.search_field.setClearButtonEnabled(True)
        self.search_field.setAccessibleName("Search this Session")
        self.search_field.setMinimumWidth(148)
        self.search_field.returnPressed.connect(
            lambda: self._run_search(self.search_field.text()))
        # Emptying the field - its clear button, or deleting the text - ends
        # the search. It used to leave the filtered results on screen under
        # an empty field, with no filter selected.
        self.search_field.textChanged.connect(self._on_search_text_changed)
        filter_row.addWidget(self.search_field, 1)
        right_layout.addLayout(filter_row)

        # -- right: the review list ---------------------------------------------
        right_layout.addWidget(self._column_header())
        self.timeline = TimelineView()
        self.timeline.correctionRequested.connect(self._on_correction)
        self.timeline.approvalRequested.connect(self.app.approve)
        self.timeline.tagsChanged.connect(self._on_tags)
        self.timeline.bookmarkToggled.connect(self._on_bookmark)
        self.timeline.retryRequested.connect(self._on_retry)
        self.timeline.retryAsSpeechRequested.connect(self._on_retry_as_speech)
        self.timeline.exportRequested.connect(self._on_export_clip)
        self.timeline.noteChanged.connect(self._on_note)
        self.timeline.transcribeAnywayRequested.connect(self._on_transcribe_anyway)
        self.timeline.removeRequested.connect(self._on_remove_message)
        self.timeline.restoreRequested.connect(self._on_restore_message)
        self.timeline.selectionChanged.connect(self._on_selection_changed)
        self.timeline.selectedUpdated.connect(lambda _id: self._refresh_selected_panel())
        self._follow_playback(self.timeline.playback)
        self.timeline.playbackReplaced.connect(self._follow_playback)
        right_layout.addWidget(self.timeline, 1)

        # Session Options: the existing controls, in a dialog opened over the
        # transcript by the bottom button (see _show_session_options). It is a
        # separate window, so nothing in this layout moves or shrinks.
        self.setup_box = QtWidgets.QGroupBox("Session Options")
        self.setup_box.setObjectName("setupPanel")
        self.setup_box.setAccessibleName("Session Options")
        self.setup_content = QtWidgets.QWidget(self.setup_box)
        self.setup_content.setObjectName("setupPanelContent")
        self.setup_content.setLayout(self._build_controls())
        outer = QtWidgets.QVBoxLayout(self.setup_box)
        outer.setContentsMargins(10, 6, 10, 8)
        outer.addWidget(self.setup_content)
        from .session_options import SessionOptionsDialog

        self.session_options_dialog = SessionOptionsDialog(self.setup_box, self)
        self.session_options_dialog.finished.connect(self._session_options_closed)

        # The transport stays in one place while the thread scrolls or the
        # sidebar is collapsed. The trigger belongs to the live input.
        self.selected_panel = self._build_selected_panel()
        self.selected_panel.layout().insertWidget(1, self.input_panel.gate_controls)
        root.addWidget(self.selected_panel)

        # -- right: actions -------------------------------------------------------
        actions = QtWidgets.QFrame()
        actions.setObjectName("actionRow")
        action_row = QtWidgets.QHBoxLayout(actions)
        action_row.setContentsMargins(10, 5, 10, 6)
        action_row.setSpacing(12)
        self.open_recording_button = QtWidgets.QPushButton("Open recording")
        self.open_recording_button.setObjectName("primaryAction")
        self.open_recording_button.setToolTip(
            "Run a WAV recording through the pipeline (File ▸ Replay WAV file)")
        self.open_recording_button.clicked.connect(self._replay_file)
        action_row.addWidget(self.open_recording_button)
        self.start_button = QtWidgets.QPushButton("Start monitoring")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setMinimumWidth(190)
        self.start_button.setShortcut("Ctrl+R")
        self.start_button.setToolTip("Start or stop monitoring (Ctrl+R)")
        self.start_button.setAccessibleName("Start or stop monitoring")
        self.start_button.clicked.connect(self._toggle_monitoring)
        action_row.addWidget(self.start_button)
        self.session_options_button = QtWidgets.QPushButton("Session options")
        self.session_options_button.setObjectName("secondaryAction")
        self.session_options_button.setCheckable(True)
        self.session_options_button.setToolTip(
            "Radio profile, source language and translation language "
            "(opens over the transcript)")
        self.session_options_button.toggled.connect(self._show_session_options)
        action_row.addWidget(self.session_options_button)
        self.export_button = QtWidgets.QPushButton("Export")
        self.export_button.setObjectName("secondaryAction")
        self.export_button.setToolTip(
            "Export this Session (File ▸ Export session bundle)")
        self.export_button.clicked.connect(self._export_session)
        action_row.addWidget(self.export_button)
        action_row.addStretch(1)
        root.addWidget(actions)

        # -- footer status band ---------------------------------------------------
        self.status = self.statusBar()
        self.status.setSizeGripEnabled(True)
        self.counts_label = QtWidgets.QLabel("")
        self.counts_label.setObjectName("countsLine")
        self.counts_label.setAccessibleName("Message counts")
        self.status.addPermanentWidget(self.counts_label)
        self.status.showMessage(backend_status())
        self._build_menu()
        self._wire_pane_links()
        # Nothing is selected yet: the panel and waveform say so.
        self._refresh_selected_panel()
        self._refresh_cleanup_offer()

    def _set_sidebar_visible(self, visible: bool) -> None:
        if not visible:
            self._sidebar_width = self.pane_splitter.sizes()[0] or self._sidebar_width
        self.sidebar.setVisible(visible)
        if visible:
            available = max(self.pane_splitter.width(), self.width())
            self.pane_splitter.setSizes([self._sidebar_width,
                                         max(1, available - self._sidebar_width)])
        self.sidebar_toggle.setArrowType(QtCore.Qt.LeftArrow if visible else QtCore.Qt.RightArrow)
        action = "Hide sidebar" if visible else "Show sidebar"
        self.sidebar_toggle.setAccessibleName(action)
        self.sidebar_toggle.setToolTip(action)

    def menuBar(self) -> QtWidgets.QMenuBar:  # noqa: N802
        """The in-window navigation bar (see _build_ui), not a bar QMainWindow
        would put above the identity row."""
        bar = getattr(self, "_nav_bar", None)
        return bar if bar is not None else super().menuBar()

    # -- v5 construction helpers ---------------------------------------------
    def _section_header(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setObjectName("sectionHeader")
        label.setAccessibleName(text.title())
        return label

    def _rule(self) -> QtWidgets.QFrame:
        rule = QtWidgets.QFrame()
        rule.setObjectName("rule")
        rule.setFixedHeight(1)
        return rule

    def _pane_link(self, text: str, checkable: bool = False) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton(text)
        button.setObjectName("paneLink")
        button.setCheckable(checkable)
        button.setFlat(True)
        button.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                             QtWidgets.QSizePolicy.Fixed)
        return button

    def _filter_button(self, text: str) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton(text)
        button.setObjectName("filterButton")
        button.setCheckable(True)
        button.setAccessibleName(f"Show {text.lower()}")
        return button

    def _column_header(self) -> QtWidgets.QFrame:
        from .timeline import REVIEW_COLUMN_WIDTH, TIME_COLUMN_WIDTH

        frame = QtWidgets.QFrame()
        frame.setObjectName("columnHeader")
        row = QtWidgets.QHBoxLayout(frame)
        # The rows' own margins and spacing, so the headings sit over them.
        row.setContentsMargins(14, 4, 10, 4)
        row.setSpacing(16)
        from ..timefmt import heading, zone_label

        time = QtWidgets.QLabel(heading())
        time.setWordWrap(True)          # "TIME (Kathmandu)" wraps, never clips
        time.setToolTip(f"Times are shown in {zone_label()}")
        time.setFixedWidth(TIME_COLUMN_WIDTH)
        self.time_heading = time
        row.addWidget(time)
        # One MESSAGE column: each row shows the transcript at full width,
        # then the translation under it - no half-width translation column.
        message = QtWidgets.QHBoxLayout()
        message.setSpacing(6)
        self.original_heading = QtWidgets.QLabel("MESSAGE: TRANSCRIPT, THEN")
        message.addWidget(self.original_heading)
        self.translation_heading = QtWidgets.QLabel("TRANSLATION")
        message.addWidget(self.translation_heading)
        message.addStretch(1)
        row.addLayout(message, 1)
        review = QtWidgets.QLabel("REVIEW")
        review.setFixedWidth(REVIEW_COLUMN_WIDTH)
        review.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        row.addWidget(review)
        return frame

    def _build_selected_panel(self) -> QtWidgets.QFrame:
        """A persistent transport under the timeline, shared by all recordings."""
        from .waveform import PlaybackSeekSlider

        panel = QtWidgets.QFrame()
        panel.setObjectName("selectedAudio")
        panel.setAccessibleName("Audio playback")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(4)
        title_row = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("AUDIO PLAYBACK")
        title.setObjectName("selectedTitle")
        title_row.addWidget(title)
        self.selected_range = QtWidgets.QLabel("")
        self.selected_range.setObjectName("selectedTitle")
        self.selected_range.setAccessibleName("Selected recording time")
        title_row.addWidget(self.selected_range)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        self.playback_controls = QtWidgets.QWidget()
        controls = QtWidgets.QVBoxLayout(self.playback_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(3)
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        self.selected_play_button = QtWidgets.QPushButton(self.PLAY_SELECTED)
        self.selected_play_button.setObjectName("transportButton")
        self.selected_play_button.setAccessibleName("Play selected recording")
        self.selected_play_button.clicked.connect(self._play_selected)
        self.playback_pause_button = QtWidgets.QPushButton("Pause")
        self.playback_pause_button.setObjectName("transportButton")
        self.playback_pause_button.setAccessibleName("Pause or resume playback")
        self.playback_pause_button.clicked.connect(self._pause_playback)
        self.playback_stop_button = QtWidgets.QPushButton("Stop")
        self.playback_stop_button.setObjectName("transportButton")
        self.playback_stop_button.setAccessibleName("Stop playback")
        self.playback_stop_button.clicked.connect(self._stop_playback)
        self.playback_rewind_button = QtWidgets.QPushButton("−5 s")
        self.playback_rewind_button.setObjectName("transportButton")
        self.playback_rewind_button.setAccessibleName("Rewind five seconds")
        self.playback_rewind_button.clicked.connect(
            lambda: self.timeline.playback.rewind())
        self.playback_forward_button = QtWidgets.QPushButton("+5 s")
        self.playback_forward_button.setObjectName("transportButton")
        self.playback_forward_button.setAccessibleName("Forward five seconds")
        self.playback_forward_button.clicked.connect(
            lambda: self.sequence.skip_forward(5000))

        # Pause and Stop must stay under the pointer across short recordings.
        for button, longest in ((self.selected_play_button, self.PLAY_SELECTED),
                                (self.playback_pause_button, "Resume")):
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance(longest) + 36)
        for button in (self.selected_play_button, self.playback_pause_button,
                       self.playback_stop_button, self.playback_rewind_button,
                       self.playback_forward_button):
            buttons.addWidget(button)

        self.playback_seek = PlaybackSeekSlider()
        self.playback_seek.seekRequested.connect(self._seek_playback)
        buttons.addWidget(self.playback_seek, 1)
        self.playback_position_label = QtWidgets.QLabel("")
        self.playback_position_label.setAccessibleName("Playback position")
        buttons.addWidget(self.playback_position_label)

        self.selected_bookmark_button = QtWidgets.QPushButton("Bookmark")
        self.selected_bookmark_button.setObjectName("textAction")
        self.selected_bookmark_button.setCheckable(True)
        self.selected_bookmark_button.clicked.connect(self._bookmark_selected)
        buttons.addWidget(self.selected_bookmark_button)
        self.selected_note_button = QtWidgets.QPushButton("Add note")
        self.selected_note_button.setObjectName("textAction")
        self.selected_note_button.clicked.connect(self._note_selected)
        buttons.addWidget(self.selected_note_button)
        self.playback_line = QtWidgets.QLabel("")
        self.playback_line.setObjectName("selectedHint")
        self.playback_line.setAccessibleName("Playback")
        self.playback_line.setWordWrap(True)
        controls.addWidget(self.playback_line)
        controls.addLayout(buttons)
        self.selected_hint = QtWidgets.QLabel("")
        self.selected_hint.setObjectName("selectedHint")
        self.selected_hint.setWordWrap(True)
        layout.addWidget(self.selected_hint)
        # Keep the transport at the same bottom edge when status or review text wraps.
        layout.addWidget(self.playback_controls)
        self.selected_panel = panel
        self._playback_selection_ready = False
        self.timeline.editingChanged.connect(self._on_playback_editing_changed)
        return panel

    def _on_playback_editing_changed(self, tx_id) -> None:
        if tx_id is not None:
            self.sequence.cancel()  # Invalidate a queued advance, leave the current clip alone.
        elif getattr(self, "_followed_clip", None) is None:
            self._drop_transient_card()
        self._refresh_playback_bar()

    def _seek_playback(self, tx_id: str, path: str, position: int) -> None:
        self.timeline.playback.seek_to(tx_id, path, position)

    def _on_playback_position_for_panel(self, owner: str, position: int, duration: int) -> None:
        # The backend may deliver a final position change for a previous file.
        controller = self.timeline.playback
        if hasattr(self, "playback_seek") and owner == controller.owner:
            from .playback import format_clock

            self.playback_seek.display(owner, controller.path, position, duration,
                                       controller.controllable)
            self.playback_position_label.setText(format_clock(position, duration))

    def _follow_playback(self, controller) -> None:
        """The waveform marker and Selected Audio follow whichever playback
        controller the list is using - including one installed later."""
        previous = getattr(self, "_followed_playback", None)
        if previous is not None and previous is not controller:
            try:
                previous.changed.disconnect(self._on_playback_changed_for_panel)
                previous.positionChanged.disconnect(self._on_playback_position_for_panel)
            except (RuntimeError, TypeError):
                pass
        self._followed_playback = controller
        self.waveform.set_playback(controller)
        controller.changed.connect(self._on_playback_changed_for_panel)
        controller.positionChanged.connect(self._on_playback_position_for_panel)
        self._install_sequence(controller)

    # -- continuous playback ---------------------------------------------------
    def _install_sequence(self, controller) -> None:
        """One SequencePlayer per playback controller: Play on an original
        continues with each newer transmission of the same Session until
        Stop (see playback_sequence)."""
        from .playback_sequence import SequencePlayer

        old = getattr(self, "sequence", None)
        if old is not None:
            old.cancel()
        self._sequence_conversation = ""
        self.sequence = SequencePlayer(controller, self._next_original_after,
                                       self._sequence_live, parent=self)
        self.sequence.currentChanged.connect(self._on_sequence_current)
        self.sequence.stateChanged.connect(lambda _s: self._refresh_playback_bar())
        self.timeline.original_starter = self._start_sequence

    def _start_sequence(self, tx_id: str, path: str) -> bool:
        """The operator's Play on one original: it plays, then the newer ones."""
        from .timeline import original_present

        tx = self.app.store.get_transmission(tx_id)
        if tx is None or not tx.state.is_terminal or not original_present(tx):
            return False
        if self.timeline.editing_id is not None:
            self.sequence.cancel()
            return self.timeline.playback.play(tx.id, tx.audio_path)
        session = self.app.store.get_session(tx.session_id)
        self._sequence_conversation = (session.conversation_id if session else "") or ""
        started = self.sequence.start(tx_id, tx.audio_path)
        self._refresh_playback_bar()
        return started

    def _next_original_after(self, tx_id: str):
        """(id, path) of the next newer playable original in the Session
        being played, or None: hidden ones and missing files are skipped."""
        from .timeline import original_present

        if not self._sequence_conversation or self._store_gone():
            return None
        # Page by page to the end of the Session - a page boundary is not
        # the end. Bounded by the Session's own rows: only rows that name a
        # recording are read, and a missing file costs one stat.
        after = tx_id
        while True:
            rows = self.app.store.newer_in_conversation(
                self._sequence_conversation, after, limit=500, with_audio=True)
            if not rows:
                return None
            for row in rows:
                if original_present(row):
                    return row.id, row.audio_path
            after = rows[-1].id

    def _sequence_live(self) -> bool:
        """The Session being played is the one monitoring records into now."""
        return bool(self.app.capture is not None and self._sequence_conversation
                    and self.app.capture_conversation_id == self._sequence_conversation)

    def _on_sequence_current(self, tx_id) -> None:
        self._follow_playing_clip()
        self._refresh_playback_bar()

    # -- following what is heard ------------------------------------------------
    def _playing_original(self):
        """The transmission whose ORIGINAL is playing or paused, or None (a
        decoded copy or a preview is not followed)."""
        from .playback import PAUSED, PLAYING
        from .waveform import same_audio

        controller = self.timeline.playback
        owner = controller.owner
        if not owner or controller.state_for(owner) not in (PLAYING, PAUSED) \
                or self._store_gone():
            return None
        tx = self.app.store.get_transmission(owner)
        if tx is None or not same_audio(controller.path or "", tx.audio_path or ""):
            return None
        return tx

    def _follow_playing_clip(self) -> None:
        """The waveform shows the recording being heard, with its playhead;
        its card is shown, highlighted and brought into view at each new
        clip. In a filtered view a playing card outside the filter is shown
        while it plays (and said so), never hidden; the filter stays."""
        tx = self._playing_original()
        followed = getattr(self, "_followed_clip", None)
        if tx is None:
            if followed is not None:
                self._followed_clip = None
                self._drop_transient_card()
                self.timeline.mark_now_playing(None)
                self._show_selection_waveform()
            return
        if tx.id == followed and tx.id in self.timeline.order():
            return
        self._followed_clip = tx.id
        if tx.id not in self.timeline.order():
            self._drop_transient_card()
            if self._in_this_conversation(tx) and not tx.hidden:
                self.timeline.place(tx)
                self._transient_card = tx.id
        elif getattr(self, "_transient_card", None) not in (None, tx.id):
            self._drop_transient_card()
        if self.timeline.editing_id in (None, tx.id):
            self.timeline.select(tx.id)
        self.waveform.show_recording(tx.id, tx.audio_path)
        self.timeline.mark_now_playing(tx.id)

    def _refollow_after_rebuild(self) -> None:
        """After a view is rebuilt in the same Session, the playing card is
        shown (transiently if the view does not hold it) and highlighted."""
        self._transient_card = None
        self._followed_clip = None
        self._follow_playing_clip()
        if self._followed_clip is None:
            self.timeline.mark_now_playing(None, reveal=False)

    def _drop_transient_card(self) -> None:
        """Take away a card that was shown only because it was playing, if
        the view's own query does not hold it."""
        transient = getattr(self, "_transient_card", None)
        if transient is not None and self.timeline.editing_id == transient:
            return  # Keep the draft in this filter until the operator finishes editing.
        self._transient_card = None
        if not transient or transient not in self.timeline.order():
            return
        if self._view_kind == "thread":
            return                                # it belongs to the thread anyway
        if transient not in {t.id for t in self._view_results()}:
            self.timeline.remove(transient)

    def _show_selection_waveform(self) -> None:
        tx = self.timeline.selected_transmission()
        if tx is None:
            self.waveform.clear()
        else:
            self.waveform.show_recording(tx.id, tx.audio_path)

    def _stop_playback(self) -> None:
        """Stop playback: the clip and the continuation, waiting included.
        Separate from Stop monitoring."""
        self.sequence.stop()
        controller = self.timeline.playback
        if controller.owner is not None:
            controller.stop()
        self._follow_playing_clip()
        self.timeline.mark_now_playing(None)
        self._refresh_playback_bar()

    def space_is_playback(self, widget) -> bool:
        """Does a plain Space at *widget* belong to playback?"""
        if widget is None:
            return self.isActiveWindow()
        if widget.window() is not self:
            return False
        if isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox)):
            return False
        if isinstance(widget, (QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit)) \
                and not widget.isReadOnly():
            return False                   # typing: the inline editor, notes
        if isinstance(widget, QtWidgets.QComboBox) and widget.isEditable():
            return False
        if widget is self:
            return True
        if widget in (self.selected_play_button, self.playback_pause_button,
                      self.playback_stop_button, self.playback_rewind_button,
                      self.playback_forward_button):
            return False                   # a labelled control: its own action
        if self.selected_panel is widget or self.selected_panel.isAncestorOf(widget):
            return True                    # the rest of the playback bar
        if self.waveform is widget or self.waveform.isAncestorOf(widget):
            return True
        if self.timeline is widget or self.timeline.isAncestorOf(widget):
            if isinstance(widget, QtWidgets.QAbstractButton):
                # A card's own buttons keep Space (Approve?, its menu);
                # only its Play button is playback.
                return bool(widget.property("playbackControl"))
            return True
        return False

    def toggle_playback_from_space(self) -> None:
        """One Space: STOP whatever plays - the clip and the queue after it,
        paused, between clips or waiting for the next live recording - so
        neither a late end report nor a new recording can restart it; or,
        when nothing plays, play the selected message and those after it.
        Pause and Resume stay on their own button. Never anything else."""
        from .playback import PAUSED, PLAYING

        controller = self.timeline.playback
        owner = controller.owner
        state = controller.state_for(owner) if owner else ""
        sequence = self.sequence
        if (owner and state in (PLAYING, PAUSED)) or sequence.active \
                or sequence.waiting or sequence.paused_between_clips:
            self._stop_playback()
        else:
            self._play_selected()

    def _pause_playback(self) -> None:
        controller = self.timeline.playback
        if not controller.controllable:
            return
        if controller.owner is None:
            self.sequence.toggle_between_clips()
        else:
            controller.toggle(controller.owner, controller.path)
        self._refresh_playback_bar()

    def _refresh_playback_bar(self) -> None:
        """The playback bar's Pause/Stop and its one line of state."""
        from .playback import PAUSED, PLAYING, format_clock

        controller = self.timeline.playback
        owner = controller.owner
        state = controller.state_for(owner) if owner else ""
        sequence = getattr(self, "sequence", None)
        waiting = bool(sequence is not None and sequence.waiting)
        active = bool(owner and state in (PLAYING, PAUSED)) or waiting \
            or bool(sequence is not None and sequence.active)
        # Only for a playable, finished selection or while playback is
        # active; at idle with such a selection Pause and Stop stay in place,
        # disabled.
        self.playback_controls.setVisible(active or self._playback_selection_ready)
        self.playback_stop_button.setEnabled(active and controller.controllable)
        self.playback_pause_button.setEnabled(active and controller.controllable)
        held = bool(sequence and sequence.paused_between_clips)
        self.playback_pause_button.setText("Resume" if state == PAUSED or held else "Pause")
        can_seek = bool(owner and state in (PLAYING, PAUSED) and controller.controllable)
        for button in (self.playback_rewind_button, self.playback_forward_button):
            button.setEnabled(can_seek)
        if owner:
            self._on_playback_position_for_panel(owner, *controller.position_for(owner))
        else:
            tx = self.timeline.selected_transmission()
            duration = round(tx.duration * 1000) if tx and tx.duration else 0
            self.playback_seek.display(None, None, 0, duration, False)
            self.playback_position_label.setText(format_clock(0, duration))
        if held:
            text = "Paused between transmissions"
        elif waiting:
            text = "Waiting for the next transmission\u2026"
        elif owner and state in (PLAYING, PAUSED):
            tx = self.app.store.get_transmission(owner) if not self._store_gone() else None
            from ..timefmt import clock

            when = clock(tx.started_at) if tx is not None else ""
            verb = "Paused" if state == PAUSED else "Playing"
            more = (" \u00b7 continues with newer transmissions"
                    if sequence is not None and sequence.active else "")
            outside = (" \u00b7 shown here while it plays (not in this view)"
                       if getattr(self, "_transient_card", None) == owner else "")
            text = f"{verb} {when}{more}{outside}"
        else:
            text = ""
        self.playback_line.setText(text)

    def _on_playback_changed_for_panel(self, _affected) -> None:
        self._follow_playing_clip()
        self._refresh_selected_panel()

    def _wire_pane_links(self) -> None:
        """Left-pane links run the real View actions, nothing of their own."""
        self.hidden_sessions_link.setChecked(
            self.show_hidden_sessions_action.isChecked())
        self.hidden_sessions_link.toggled.connect(
            self.show_hidden_sessions_action.setChecked)
        self.show_hidden_sessions_action.toggled.connect(
            self.hidden_sessions_link.setChecked)
        self.search_link.clicked.connect(self._focus_search)
        self.filter_all.clicked.connect(
            lambda: self._filter_clicked("thread", self.show_all_action.trigger))
        self.filter_review.clicked.connect(
            lambda: self._filter_clicked("review", self.review_action.trigger))
        self.filter_bookmarked.clicked.connect(
            lambda: self._filter_clicked("bookmarked", self._show_bookmarked))
        self.filter_removed.clicked.connect(
            lambda: self._filter_clicked("removed", self._show_removed))

    def _filter_clicked(self, kind: str, show) -> None:
        """Pressing the filter already on screen changes nothing - rebuilding
        it would stop playback and drop the selection for no reason."""
        if self._view_kind != kind:
            show()
        self._sync_filters()

    def _build_header(self, row: QtWidgets.QHBoxLayout) -> None:
        """The teal status band: operating mode, source, monitoring state."""
        # A real control, not a label that happens to react to a click. It
        # looks pressable, it takes focus, it opens on Space or Return, and a
        # screen reader announces it as a button.
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
        # Offline Field Use's own readiness, right beside the mode: Checking...
        # while the checks run, then a red cross or a green tick. A button,
        # so it takes focus and opens the full report on Space or a click.
        self.offline_status = QtWidgets.QToolButton()
        self.offline_status.setObjectName("offlineStatus")
        self.offline_status.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.offline_status.setCursor(QtCore.Qt.PointingHandCursor)
        self.offline_status.clicked.connect(self._show_readiness)
        self.offline_status.setVisible(False)
        row.addWidget(self.offline_status)
        row.addStretch(1)

        self.source_label = QtWidgets.QLabel("SOURCE: …")
        self.source_label.setObjectName("sourceLabel")
        self.source_label.setAccessibleName("Source")
        row.addWidget(self.source_label)
        row.addStretch(1)

        self.state_badge = QtWidgets.QLabel("○ Idle")
        self.state_badge.setObjectName("stateBadge")
        self.state_badge.setAccessibleName("Current state")
        # Shown in capitals, as the band's other words; the text itself is
        # the state's ordinary name.
        font = self.state_badge.font()
        font.setCapitalization(QtGui.QFont.AllUppercase)
        self.state_badge.setFont(font)
        row.addWidget(self.state_badge)

    def _build_language_controls(self, layout) -> None:
        """Language detection, and directly below it the source language,
        under Local Engines: what the speech model is told about the
        language. Saved, restored, and locked while monitoring."""
        session = self.app.config.session
        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        self.source_mode_box = self._compact_combo(QtWidgets.QComboBox())
        self.source_mode_box.addItem("Detect automatically", "automatic")
        self.source_mode_box.addItem("Specified", "specified")
        self.source_mode_box.setAccessibleName("Language detection")
        self.source_language_box = self._compact_combo(QtWidgets.QComboBox())
        for code, name in COMMON_LANGUAGES:
            self.source_language_box.addItem(f"{name} ({code})", code)
        self.source_language_box.setAccessibleName("Source language")
        mode_index = self.source_mode_box.findData(session.source_language_mode)
        self.source_mode_box.setCurrentIndex(max(0, mode_index))
        language_index = self.source_language_box.findData(session.source_language)
        if language_index >= 0:
            self.source_language_box.setCurrentIndex(language_index)
        self.source_language_box.setEnabled(
            self.source_mode_box.currentData() == "specified")
        self.source_mode_box.currentIndexChanged.connect(self._on_source_mode)
        self.source_language_box.currentIndexChanged.connect(
            lambda _i: self._save_source_language())
        for row, (text, box) in enumerate((("Language detection", self.source_mode_box),
                                           ("Source language", self.source_language_box))):
            caption = QtWidgets.QLabel(text)
            caption.setObjectName("engineLine")
            caption.setBuddy(box)
            grid.addWidget(caption, row, 0)
            grid.addWidget(box, row, 1)
        layout.addLayout(grid)

    def _save_source_language(self) -> None:
        """Remember the language choice and recheck what it needs."""
        session = self.app.config.session
        mode = self.source_mode_box.currentData()
        language = self.source_language_box.currentData()
        if (session.source_language_mode, session.source_language) == (mode, language):
            return
        session.source_language_mode, session.source_language = mode, language
        try:
            self.app.config.save()
        except OSError as exc:  # noqa: BLE001 - shown; the running choice stands
            self.status.showMessage(f"Language choice not saved: {exc}", 8000)
        self._refresh_context_lines()
        self._refresh_readiness(run_smoke_tests=False)

    def _on_target_language(self, _index=None) -> None:
        code = self.target_language_box.currentData()
        translate = self.app.config.translate
        if not code or code == translate.target_language:
            return
        translate.target_language = code
        try:
            self.app.config.save()
        except OSError as exc:  # noqa: BLE001
            self.status.showMessage(f"Target language not saved: {exc}", 8000)
        self._refresh_context_lines()
        self._refresh_readiness(run_smoke_tests=False)

    # -- installing a speech model (Online / Setup) -------------------------
    def _install_model_problem(self) -> str:
        """Why a model cannot be installed now, or "" when it can."""
        if not self.app.mode.allows_downloads:
            return (f"{self.app.mode.title} does not download anything. Switch to "
                    f"Online / Setup to install a speech model.")
        if self.app.session is not None:
            return "Stop monitoring first: a model is not installed during a watch."
        if self._install_worker is not None:
            return "A speech model is being installed."
        return ""

    def _refresh_install_model_button(self) -> None:
        button = getattr(self, "install_model_button", None)
        if button is None:
            return
        problem = self._install_model_problem()
        button.setEnabled(not problem)
        button.setToolTip(problem or (
            "Download and verify a Whisper speech model (multilingual "
            "transcription; separate from translation language packs). The "
            "model in use does not change until you choose the new one."))

    def _installable_models(self):
        """(name, label, state) for each model not installed completely."""
        from ..preparation import MODEL_SIZES_MB
        from ..providers.whisper_local import MODEL_SIZES, ModelState, inspect_model_directory

        found = []
        for name in MODEL_SIZES:
            try:
                state, _ = inspect_model_directory(self.app.whisper_model_directory(name))
            except OSError:
                state = None
            if state is ModelState.COMPLETE:
                continue
            verb = "Repair" if state is ModelState.INCOMPLETE else "Install"
            size = MODEL_SIZES_MB.get(name, 0)
            title = name.replace("-", " ").capitalize()
            found.append((name, f"{verb} {title} (about {size} MB)", state))
        return found

    def _fill_install_model_menu(self) -> None:
        menu = self.install_model_menu
        menu.clear()
        models = self._installable_models()
        if not models:
            menu.addAction("Every Whisper model is installed").setEnabled(False)
            return
        for name, label, _state in models:
            action = menu.addAction(label)
            action.triggered.connect(lambda _c=False, n=name: self._install_model(n))

    def _install_model(self, name: str, confirm: bool = True) -> None:
        from .workers import install_model_job, run_in_background

        problem = self._install_model_problem()
        if problem:
            self.status.showMessage(problem, 10000)
            return
        from ..preparation import MODEL_SIZES_MB

        title = name.replace("-", " ").capitalize()
        if confirm and self._choose(
                f"Install {title}",
                f"Download the Whisper model {title} (about "
                f"{MODEL_SIZES_MB.get(name, 0)} MB)?",
                "It is downloaded into BabelFishR's own folder and checked by "
                "transcribing a short sample with downloads disabled. The "
                "model in use now stays selected; choose the new one in "
                "Whisper model afterwards. An interrupted download can be "
                "repaired later.",
                ["Download"]) != "Download":
            return
        self.status.showMessage(f"Installing Whisper {title}\u2026", 0)
        progress = QtWidgets.QProgressDialog(
            f"Installing Whisper {title}\u2026", "Cancel", 0, 0, self)
        progress.setWindowTitle("Install speech model")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        self._install_progress = progress

        def done(outcome, cancelled=False, error=""):
            self._install_worker = None
            progress.close()
            self.whisper_model_box.refresh_choices()
            self._refresh_install_model_button()
            if cancelled:
                text = (f"Installing {title} was cancelled. Anything partly "
                        f"downloaded shows as Incomplete and can be repaired.")
            elif error:
                text = f"{title} was not installed: {error}"
            else:
                _step, ok, detail = outcome
                text = (f"{title} is installed and verified offline. Choose it "
                        f"in Whisper model to use it." if ok
                        else f"{title} was not installed: {detail}")
            self.status.showMessage(text, 15000)
            QtWidgets.QMessageBox.information(self, "Install speech model", text)

        worker = run_in_background(
            install_model_job, self.app.config, name,
            on_finished=lambda result: done(result),
            on_failed=lambda message: done(None, error=message),
            on_cancelled=lambda: done(None, cancelled=True),
            on_message=lambda text: progress.setLabelText(text))
        self._install_worker = worker
        progress.canceled.connect(lambda: worker.token.cancel())
        self._refresh_install_model_button()

    # -- the Session's save path ------------------------------------------
    def _save_path_locked(self) -> bool:
        """Locked while this Session is recording (changes after Stop)."""
        return bool(self.app.save_path_locked(self.app.conversation_id))

    def _refresh_save_path_row(self) -> None:
        label = getattr(self, "save_path_label", None)
        if label is None:
            return
        # The last check's result (readiness, a choice, a Start): no disk
        # access while drawing the window.
        info = self.app.save_path_info(self.app.conversation_id, probe=False)
        if not info.is_custom:
            text = f"Standard location: {info.effective_dir}"
        elif info.state == "unchecked":
            text = f"{info.owned_dir}  (in {info.chosen}) \u2013 checking\u2026"
        elif info.available:
            text = f"{info.owned_dir}  (in {info.chosen})"
        else:
            text = f"\u2715 {info.problem()}"
        label.setText(text)
        safety = self.app.config.audio.safety_recording
        local = (f"the shared message index and settings ({self.app.config.paths().root})"
                 + (", and the rolling safety buffer of continuous audio (a "
                    "short-lived backup, kept on this computer on purpose)"
                    if getattr(safety, "enabled", False) else ""))
        label.setToolTip(
            "New recordings, per-message transcript files and exports of this "
            f"Session are saved here. On this computer stay {local}. Earlier "
            "recordings stay where they were saved.")
        locked = self._save_path_locked()
        why = "Locked while this Session is recording. Stop to change it." if locked else ""
        self.save_path_choose.setEnabled(not locked)
        self.save_path_reset.setEnabled(not locked and info.is_custom)
        self.save_path_choose.setToolTip(why or "Save this Session's recordings on a "
                                         "removable drive or any writable folder")
        self.save_path_reset.setToolTip(why or "Save new recordings in the standard "
                                        "location again")

    def _choose_save_path(self) -> None:
        info = self.app.save_path_info(self.app.conversation_id, probe=False)
        start = info.chosen if info.is_custom and info.available else str(pathlib.Path.home())
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose where this Session saves its recordings", start)
        if not folder or self._store_gone():
            return
        self._apply_save_path(folder)

    def _reset_save_path(self) -> None:
        self._apply_save_path(None)

    def _apply_save_path(self, folder) -> None:
        from ..savepath import SavePathError

        try:
            info = self.app.set_save_path(self.app.conversation_id, folder)
        except SavePathError as exc:
            QtWidgets.QMessageBox.warning(self, "Save path not changed", str(exc))
            self._refresh_save_path_row()
            return
        self._refresh_save_path_row()
        self._clear_warning("save-path")
        where = info.owned_dir if info.is_custom else info.effective_dir
        self.status.showMessage(f"New recordings of this Session are saved in {where}.",
                                10000)
        self._refresh_readiness(run_smoke_tests=False)

    def _on_save_path_event(self, payload: dict) -> None:
        state = payload.get("state", "")
        message = payload.get("message", "")
        if state == "fallback":
            # Never a green tick while recordings go somewhere else.
            self._warn(message, source="save-path")
            self.status.showMessage(message, 15000)
            if self.app.mode is OperatingMode.FIELD_OFFLINE:
                self._set_offline_status("fail", [message])
            self._refresh_save_path_row()
            self._refresh_readiness(run_smoke_tests=False)
        elif state in ("recovered", "restored"):
            self._clear_warning("save-path")
            self.status.showMessage(message, 10000)
            self._refresh_save_path_row()
            self._refresh_readiness(run_smoke_tests=False)
        elif state in ("transcript-failed", "transcript-remove-failed"):
            self._warn(message, source="portable-transcript")
        elif state == "transcript-saved":
            self._clear_warning("portable-transcript")
        elif state == "changed":
            self._refresh_save_path_row()

    def _export_directory(self) -> pathlib.Path:
        try:
            return pathlib.Path(self.app.default_export_dir(self.app.conversation_id))
        except Exception:  # noqa: BLE001 - a starting folder is a convenience
            return pathlib.Path.home()

    def _compact_combo(self, box: QtWidgets.QComboBox) -> QtWidgets.QComboBox:
        """A combo box whose longest item never sets the form's minimum
        width: it shrinks to a few characters and elides, and the full item
        is in its tooltip and popup."""
        box.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        box.setMinimumContentsLength(8)
        box.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                          QtWidgets.QSizePolicy.Fixed)
        # currentTextChanged also fires when items are first filled in.
        box.currentTextChanged.connect(
            lambda text, b=box: b.setToolTip(text) if not b.property(
                "fixedTip") else None)
        return box

    def _build_controls(self) -> QtWidgets.QFormLayout:
        """Session Options as one form that grows downwards.

        A label moves above its field when the window is too narrow for both
        on one line, long text wraps, and no combo box is wider than the
        space it is given, so the dialog never needs to scroll sideways.
        """
        form = QtWidgets.QFormLayout()
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.options_form = form

        def label(text: str) -> QtWidgets.QLabel:
            widget = QtWidgets.QLabel(text)
            widget.setWordWrap(True)
            return widget

        # The timestamp zone: the same setting as View > Show Times In, kept
        # in step both ways, and a display choice only - so, unlike the
        # options below it, it stays available while monitoring.
        from .. import timefmt

        self.time_zone_box = self._compact_combo(QtWidgets.QComboBox())
        self.time_zone_box.addItem("Local", timefmt.LOCAL)
        self.time_zone_box.addItem("UTC", timefmt.UTC)
        self.time_zone_box.setAccessibleName("Timestamp time zone")
        self.time_zone_box.setProperty("fixedTip", True)
        self.time_zone_box.setToolTip(
            "Show message times in this computer's local time or in UTC. "
            "Stored times and exports are not changed. Also in View \u203a "
            "Show Times In.")
        self.time_zone_box.setCurrentIndex(
            self.time_zone_box.findData(timefmt.display_zone()))
        self.time_zone_box.currentIndexChanged.connect(
            lambda _i: self._set_time_zone(self.time_zone_box.currentData()))
        zone_label = label("Timestamp time zone")
        zone_label.setBuddy(self.time_zone_box)
        form.addRow(zone_label, self.time_zone_box)

        # Under UTC: which offset or named region. Searchable: type part of
        # a city, a region or an offset ("Denver", "Kathmandu", "+05:45").
        self.utc_zone_box = self._compact_combo(QtWidgets.QComboBox())
        self.utc_zone_box.setEditable(True)
        self.utc_zone_box.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.utc_zone_box.setAccessibleName("UTC offset or region")
        self.utc_zone_box.lineEdit().setPlaceholderText("Type a city, region or offset")
        for text, key in timefmt.choices():
            self.utc_zone_box.addItem(text, key)
        completer = QtWidgets.QCompleter(self.utc_zone_box.model(), self.utc_zone_box)
        completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        completer.setFilterMode(QtCore.Qt.MatchContains)
        completer.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
        self.utc_zone_box.setCompleter(completer)
        completer.activated[str].connect(self._on_utc_zone_text)
        self.utc_zone_box.activated.connect(self._on_utc_zone_index)
        self.utc_zone_box.lineEdit().editingFinished.connect(
            lambda: self._on_utc_zone_text(self.utc_zone_box.currentText()))
        self.utc_zone_label = label("UTC offset or region")
        self.utc_zone_label.setBuddy(self.utc_zone_box)
        form.addRow(self.utc_zone_label, self.utc_zone_box)
        self._sync_utc_zone_box()

        self.profile_box = self._compact_combo(QtWidgets.QComboBox())
        self.profile_box.currentIndexChanged.connect(self._on_profile_changed)
        self.profile_button = QtWidgets.QToolButton()
        self.profile_button.setText("+")
        self.profile_button.setToolTip("Add a radio/channel profile")
        self.profile_button.clicked.connect(self._new_profile)
        profile_row = QtWidgets.QHBoxLayout()
        profile_row.setContentsMargins(0, 0, 0, 0)
        profile_row.addWidget(self.profile_box, 1)
        profile_row.addWidget(self.profile_button)
        form.addRow(label("Radio profile"), profile_row)

        self.target_language_box = self._compact_combo(QtWidgets.QComboBox())
        for code, name in COMMON_LANGUAGES:
            self.target_language_box.addItem(f"{name} ({code})", code)
        target = self.app.config.translate.target_language
        index = self.target_language_box.findData(target)
        if index >= 0:
            self.target_language_box.setCurrentIndex(index)
        self.target_language_box.currentIndexChanged.connect(self._on_target_language)
        form.addRow(label("Translate into"), self.target_language_box)

        # The quiet that ends a message (detector.hang_time), in seconds.
        quiet_label = label("End a message after")
        self.quiet_seconds_box = QtWidgets.QDoubleSpinBox()
        # The same bounds DetectorSettings.validate() accepts, so a saved
        # value is shown as what it is.
        self.quiet_seconds_box.setRange(0.1, 30.0)
        self.quiet_seconds_box.setSingleStep(0.5)
        self.quiet_seconds_box.setDecimals(1)
        self.quiet_seconds_box.setSuffix(" s of quiet")
        self.quiet_seconds_box.setValue(
            max(0.1, min(30.0, float(self.app.config.detector.hang_time))))
        self.quiet_seconds_box.setAccessibleName("Seconds of quiet that end a message")
        self.quiet_seconds_box.setToolTip(
            "A message ends when the input's audio level stays below the "
            "activity threshold this long; shorter pauses stay in the same "
            "message, and the next audio starts a new one. This is the audio "
            "level, not the radio's squelch. Sound that never goes quiet is "
            "still transcribed while it continues: it is handed on in parts of "
            "about 15 s, each cut at the quietest point in its last 3 s so a "
            "word is not split, and the next part carries straight on. A short "
            "transmission that ends in quiet is always kept whole.")
        quiet_label.setBuddy(self.quiet_seconds_box)
        self.quiet_seconds_box.valueChanged.connect(self._on_quiet_seconds)
        form.addRow(quiet_label, self.quiet_seconds_box)

        # Where this Session's recordings and portable transcripts go.
        save_label = label("Save path")
        self.save_path_label = QtWidgets.QLabel("")
        self.save_path_label.setObjectName("sectionLabel")
        self.save_path_label.setWordWrap(True)
        self.save_path_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.save_path_label.setAccessibleName("Save path in effect")
        self.save_path_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                           QtWidgets.QSizePolicy.Preferred)
        self.save_path_choose = QtWidgets.QPushButton("Choose folder\u2026")
        self.save_path_choose.setAccessibleName("Choose a save folder for this Session")
        self.save_path_choose.clicked.connect(self._choose_save_path)
        self.save_path_reset = QtWidgets.QPushButton("Use standard location")
        self.save_path_reset.setAccessibleName("Save this Session in the standard location")
        self.save_path_reset.clicked.connect(self._reset_save_path)
        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.save_path_choose)
        buttons.addWidget(self.save_path_reset)
        buttons.addStretch(1)
        save_column = QtWidgets.QVBoxLayout()
        save_column.setContentsMargins(0, 0, 0, 0)
        save_column.setSpacing(4)
        save_column.addWidget(self.save_path_label)
        save_column.addLayout(buttons)
        save_label.setBuddy(self.save_path_choose)
        form.addRow(save_label, save_column)

        # The operating mode lives in the header, next to readiness, and only
        # there. Two controls for one setting is two things to disagree.
        self.channel_label = QtWidgets.QLabel("")
        self.channel_label.hide()
        self.channel_label.setObjectName("sectionLabel")
        self.channel_label.setWordWrap(True)
        self.channel_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                         QtWidgets.QSizePolicy.Preferred)
        form.addRow(self.channel_label)
        return form

    def _sync_utc_zone_box(self) -> None:
        """Show the saved UTC choice; the row only while UTC is chosen."""
        from .. import timefmt

        box = getattr(self, "utc_zone_box", None)
        if box is None:
            return
        index = box.findData(timefmt.utc_zone())
        if index < 0:
            index = box.findData(timefmt.ZULU)
        box.blockSignals(True)
        box.setCurrentIndex(index)
        box.blockSignals(False)
        box.setToolTip(f"{box.currentText()}\nTimes are shown in "
                       f"{timefmt.zone_label()}.")
        self.options_form.setRowVisible(box, timefmt.is_utc())

    def _on_utc_zone_index(self, index: int) -> None:
        key = self.utc_zone_box.itemData(index)
        if key is not None:
            self._set_time_zone("utc", utc_zone=key)

    def _on_utc_zone_text(self, text: str) -> None:
        """Typed or picked from the search list: use it if it names an
        entry; otherwise put the current choice back."""
        index = self.utc_zone_box.findText(text, QtCore.Qt.MatchFixedString)
        if index >= 0:
            if self.utc_zone_box.itemData(index) != self.app.config.display.utc_zone \
                    or not self.app.config.display.timezone == "utc":
                self.utc_zone_box.setCurrentIndex(index)
                self._on_utc_zone_index(index)
            return
        self._sync_utc_zone_box()

    def _show_session_options(self, shown: bool) -> None:
        """Open the Session Options dialog over the transcript, or close it."""
        dialog = self.session_options_dialog
        if shown:
            dialog.open_over(self.timeline)
        elif dialog.isVisible():
            dialog.close()

    def _session_options_closed(self, _result: int = 0) -> None:
        # Closed by Close, Escape or the window's own button: the bottom
        # button shows that it is closed.
        self.session_options_button.blockSignals(True)
        self.session_options_button.setChecked(False)
        self.session_options_button.blockSignals(False)

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
        self._refresh_install_model_button()
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
        bar = self.menuBar()
        # Keep the menus inside the BabelFishR window, on every platform.
        #
        # On macOS a QMenuBar is normally handed to the system and drawn in
        # the bar along the top edge of the screen. On the first live bench
        # test the operator could not find Search or the Review queue there:
        # what he saw were desktop menus, and the application itself appeared
        # to offer none of these commands. Whether that was Cocoa activation,
        # the frozen bundle, or something else is not established - it has
        # not been reproduced on a physical Mac. What can be established is
        # that a menu drawn inside the window is where the operator is already
        # looking, and cannot depend on which application the system thinks
        # is frontmost. Qt documents this property as exactly that switch.
        # The same actions, the same handlers; only where they are drawn.
        # (Qt reports isNativeMenuBar() False on platforms with no native bar
        # regardless of this call, so a test of the property alone proves the
        # request was made, not that the Mac honours it.)
        bar.setNativeMenuBar(False)
        bar.setObjectName("mainMenuBar")

        file_menu = bar.addMenu("&File")
        file_menu.setObjectName("fileMenu")

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

        # The View and Tools actions are kept as attributes: they are the
        # commands the operator reaches through the visible menu, and a test
        # that clicks the menu needs to know which item it is aiming at.
        view_menu = bar.addMenu("&View")
        view_menu.setObjectName("viewMenu")
        self.search_action = QtGui.QAction("Search transmissions...", self)
        self.search_action.setShortcut("Ctrl+F")
        self.search_action.triggered.connect(self._search)
        view_menu.addAction(self.search_action)

        # Review queue, All messages and Removed are the filter buttons over
        # the thread; the menu no longer repeats them. These actions stay on
        # the window for their shortcuts and for older callers.
        self.review_action = QtGui.QAction("Review queue", self)
        self.review_action.triggered.connect(self._show_review_queue)
        self.addAction(self.review_action)

        self.show_all_action = QtGui.QAction("Show all transmissions", self)
        self.show_all_action.setShortcut("Ctrl+Shift+A")
        self.show_all_action.setShortcutContext(QtCore.Qt.WindowShortcut)
        self.show_all_action.setToolTip("Return to the full message thread")
        self.show_all_action.triggered.connect(self._reload_timeline)
        self.addAction(self.show_all_action)

        # Removed messages mixed into the thread (a compatibility switch;
        # the Removed filter is the visible place to find them).
        self.show_removed_action = QtGui.QAction("Show removed messages", self)
        self.show_removed_action.setCheckable(True)
        self.show_removed_action.toggled.connect(lambda _: self._reload_timeline())
        self.addAction(self.show_removed_action)
        view_menu.addSeparator()

        self.refresh_action = QtGui.QAction("Refresh", self)
        self.refresh_action.setShortcut(QtGui.QKeySequence.Refresh)
        self.refresh_action.setStatusTip(
            "Show this view as saved, including recordings kept but not "
            "processed after Stop")
        self.refresh_action.triggered.connect(self._refresh_current_view)
        view_menu.addAction(self.refresh_action)

        # Show Times In: Local (default) or UTC - display only; stored
        # instants and exports keep their UTC ISO-8601 values.
        from ..timefmt import LOCAL, UTC, zone_label

        times_menu = view_menu.addMenu("Show Times In")
        self.time_zone_group = QtGui.QActionGroup(self)
        self.time_zone_group.setExclusive(True)
        self.time_zone_actions = {}
        for zone, label in ((LOCAL, zone_label() if self._time_zone() == LOCAL
                             else "Local"), (UTC, "UTC")):
            action = QtGui.QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self._time_zone() == zone)
            action.triggered.connect(lambda _=False, z=zone: self._set_time_zone(z))
            self.time_zone_group.addAction(action)
            times_menu.addAction(action)
            self.time_zone_actions[zone] = action
        if self._time_zone() == UTC:
            self.time_zone_actions[UTC].setText(f"UTC \u2014 {zone_label()}")
        times_menu.addSeparator()
        self.utc_zone_action = times_menu.addAction("UTC Offset or Region\u2026")
        self.utc_zone_action.setToolTip(
            "Choose UTC/Zulu, a fixed offset or a named region (Session Options)")
        self.utc_zone_action.triggered.connect(self._choose_utc_zone)

        self.show_hidden_sessions_action = QtGui.QAction("Show hidden Sessions", self)
        self.show_hidden_sessions_action.setCheckable(True)
        self.show_hidden_sessions_action.setToolTip(
            "Also show Session tabs that were hidden, so they can be restored. "
            "Everything in them was kept.")
        self.show_hidden_sessions_action.toggled.connect(
            lambda _: self._refresh_session_tabs())
        view_menu.addAction(self.show_hidden_sessions_action)

        self._build_message_menu(bar)

        tools_menu = bar.addMenu("&Tools")
        tools_menu.setObjectName("toolsMenu")
        self.readiness_action = QtGui.QAction("Field readiness...", self)
        self.readiness_action.setShortcut("Ctrl+Shift+R")
        self.readiness_action.triggered.connect(self._show_readiness)
        tools_menu.addAction(self.readiness_action)

        self.assistant_action = QtGui.QAction("Setup assistant...", self)
        self.assistant_action.triggered.connect(self._show_assistant)
        tools_menu.addAction(self.assistant_action)

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

        self.finish_deletions_action = QtGui.QAction(
            "Retry file cleanup\u2026", self)
        self.finish_deletions_action.setToolTip(
            "Try again to remove recording files a permanent deletion could "
            "not delete the first time.")
        self.finish_deletions_action.triggered.connect(
            self._finish_unfinished_deletions)
        # Not a routine Tools item: offered only when a past confirmed
        # deletion still has files to remove (see _refresh_cleanup_offer).
        self.addAction(self.finish_deletions_action)

        self.reveal_logs_action = QtGui.QAction("Reveal Logs in Finder", self)
        self.reveal_logs_action.setToolTip("Open the folder containing the "
                                           "application log.")
        self.reveal_logs_action.triggered.connect(self._reveal_logs)
        tools_menu.addAction(self.reveal_logs_action)

        help_menu = bar.addMenu("&Help")
        help_menu.setObjectName("helpMenu")
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
                       "audio. Replaying a WAV file still works.",
                       source="audio-backend")
        else:
            self._clear_warning("audio-backend")

    def _on_input_selection(self) -> None:
        self.status.showMessage(self.input_panel.status_label.text(), 6000)
        # The selected input is part of readiness: never keep an old tick.
        self._refresh_readiness(run_smoke_tests=False)

    def _refresh_profiles(self) -> None:
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_box.addItem("No profile (no channel metadata)", None)
        for profile in self.app.profiles():
            self.profile_box.addItem(f"{profile.name} - {profile.label()}", profile.id)
        self.profile_box.blockSignals(False)
        self.profile_box.setToolTip(self.profile_box.currentText())
        self._on_profile_changed()

    def _report_engines(self) -> None:
        summary = self.app.select_engines()
        messages = list(summary.warnings)
        if messages:
            self._warn("\n".join(messages), source="engines")
        else:
            # The previous summary's warning is not this summary's warning.
            # Leaving it up is how the Record Only explanation stayed on
            # screen after the operator had gone back to Field Offline.
            self._clear_warning("engines")
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
        # LOCAL ENGINES: what select_engines actually chose, by name.
        from ..providers.whisper_local import FasterWhisperEngine

        speech = summary.transcription
        if isinstance(self.app.transcription, FasterWhisperEngine):
            device, precision = self.app.transcription.runtime_settings()
            speech += f" · {device.upper()} / {precision}"
        self.speech_engine_line.setText(f"Speech: {speech}")
        self.translation_engine_line.setText(f"Translation: {summary.translation}")
        self.whisper_model_box.refresh_choices()
        self._refresh_context_lines()

    def _change_whisper_model(self, index: int) -> None:
        from ..app import ModeChangeRefused, ProcessingBusy

        model = self.whisper_model_box.itemData(index)
        if not model or model == self.app.config.asr.model:
            return
        try:
            self.app.set_whisper_model(model)
        except (ModeChangeRefused, ProcessingBusy, ValueError, OSError) as exc:
            self.whisper_model_box.refresh_choices()
            self.status.showMessage(f"Whisper model unchanged. {exc}", 12000)
            return
        self._report_engines()
        self._refresh_readiness(run_smoke_tests=False)
        self.status.showMessage(f"Selected Whisper {model} for transcription.", 6000)

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

    #: Display order for the banner. Audio problems first: they decide whether
    #: anything is being recorded at all.
    _WARNING_SOURCES = ("audio-input", "audio-backend", "engines", "general")

    def _warn(self, text: str, source: str = "general") -> None:
        """Show a warning, remembering what it is about.

        Warnings are kept per source - the engines, the audio backend, the
        selected audio input - and the banner shows every current one. That
        is what lets one be cleared without hiding another that is still
        true. The banner used to be a single label that was only ever written
        to and never cleared, so the explanation of Record Only mode stayed on
        screen after the operator had left Record Only: the next engine
        summary had no warnings, and therefore never touched it.
        """
        self._warnings[source] = text
        self._render_warnings()

    def _clear_warning(self, source: str) -> None:
        """Retract one warning. Others stay exactly as they were."""
        if self._warnings.pop(source, None) is not None:
            self._render_warnings()

    def _render_warnings(self) -> None:
        ordered = [self._warnings[k] for k in self._WARNING_SOURCES
                   if self._warnings.get(k)]
        ordered += [v for k, v in self._warnings.items()
                    if k not in self._WARNING_SOURCES and v]
        if not ordered:
            self.warning_banner.clear()
            self.warning_banner.hide()
            return
        self.warning_banner.setText("\n".join("⚠ " + t for t in ordered))
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
        self._show_clipping()
        self._refresh_capture_tab_label()
        self.start_button.setText("Stop monitoring")
        self._set_controls_enabled(False)
        self.input_panel.set_monitoring(True, replay=bool(replay_path))
        self.live_waveform.reset()
        self.waveform_stack.setCurrentWidget(self.waveform if replay_path else self.live_waveform)
        self._refresh_context_lines()
        self._show_live_view()

    def _show_live_view(self) -> None:
        """After a successful Start: the Session being recorded, its live
        thread (no search, no filter), scrolled to the newest message at the
        top - so new transcriptions appear in view. Once only: scrolling
        afterwards is the operator's."""
        target = self.app._capture_conversation_id or self.app.conversation_id
        if target and target != self.app.conversation_id:
            self.app.select_conversation(target)
            self._persist_selected_session()
        for index in range(self.session_tabs.count()):
            if self.session_tabs.tabData(index) == target:
                if self.session_tabs.currentIndex() != index:
                    blocked = self.session_tabs.blockSignals(True)
                    self.session_tabs.setCurrentIndex(index)
                    self.session_tabs.blockSignals(blocked)
                break
        if self.search_field.text():
            blocked = self.search_field.blockSignals(True)
            self.search_field.clear()
            self.search_field.blockSignals(blocked)
        removed = getattr(self, "show_removed_action", None)
        if removed is not None and removed.isChecked():
            blocked = removed.blockSignals(True)
            removed.setChecked(False)
            removed.blockSignals(blocked)
        self._reload_timeline()
        self._refresh_capture_tab_label()
        self.timeline.scroll_to_top()
        # And again once the rebuilt list has its final size.
        QtCore.QTimer.singleShot(0, self.timeline.scroll_to_top)

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
            self.sidebar_toggle.setChecked(True)
            self.input_box.setVisible(True)
            self.input_panel.refresh_devices()
            self.input_panel.device_box.setFocus()
            self.input_panel.device_box.showPopup()
        elif clicked is later:
            self.status.showMessage(
                "Not monitoring. Nothing is being recorded.", 8000)

    def _stop_monitoring(self) -> None:
        # Returns at once. The operator's choice (2026-11-25): Stop cuts the
        # input off immediately and finishes everything accepted up to that
        # point - the running job, queued messages, and the message that was
        # still open - and each result lands in its own session as it
        # completes. No Retry is needed. Pressing Stop again is a no-op.
        session = self.app.stop_session()
        self.start_button.setText("Start monitoring")
        self._set_controls_enabled(True)
        self._refresh_capture_tab_label()
        # Inputs can be changed again, but only once the watch has stopped.
        self.input_panel.set_monitoring(False)
        permission = (self._readiness.get("Microphone permission")
                      if self._readiness is not None else None)
        if permission is not None and "not asked yet" in permission.detail:
            # macOS asked when this watch opened the microphone: read the
            # answer now rather than keep reporting "not asked yet".
            self._refresh_readiness(run_smoke_tests=False)
        self.waveform_stack.setCurrentWidget(self.waveform)
        self.live_waveform.reset()
        self._refresh_context_lines()
        outstanding = self.app.outstanding_work()
        if outstanding:
            # Work still being processed and, as one, a capture still
            # finishing its last recording off this thread.
            self._set_state(PipelineState.TRANSCRIBING if self._processing_active()
                            else PipelineState.IDLE)
            self.status.showMessage(
                f"Monitoring stopped. Finishing {outstanding} recording(s); "
                f"each appears in its session as it completes.", 12000)
        else:
            self._set_state(PipelineState.IDLE)
            if session is not None:
                self.status.showMessage("Monitoring stopped.", 8000)
        self.meter.reset()
        self._show_clipping()

    def _refresh_current_view(self) -> None:
        """View > Refresh: show the current view exactly as the store has it,
        including recordings a stopped run saved after Stop."""
        kind = self._view_kind
        if kind == "search" and self._view_query:
            self._run_search(self._view_query)
        elif kind == "review":
            self._show_review_queue()
        elif kind == "bookmarked":
            self._show_bookmarked()
        elif kind == "removed":
            self._show_removed()
        else:
            self._reload_timeline()

    def _time_zone(self) -> str:
        from ..timefmt import display_zone

        return display_zone()

    def _set_time_zone(self, zone: str, utc_zone: Optional[str] = None) -> None:
        """Show every time in *zone* (Local or UTC) - and, under UTC, in
        *utc_zone* (or the one already chosen) - at once, on messages
        already shown and on those still arriving; remembered."""
        from .. import timefmt

        display = self.app.config.display
        if utc_zone is not None:
            display.utc_zone = utc_zone
        problem = timefmt.set_display_zone(zone, display.utc_zone)
        display.timezone = timefmt.display_zone()
        try:
            self.app.config.save()
        except OSError:  # noqa: BLE001 - a preference is never worth a crash
            pass
        for key, action in self.time_zone_actions.items():
            action.setChecked(key == display.timezone)
        box = getattr(self, "time_zone_box", None)
        if box is not None and box.currentData() != display.timezone:
            box.blockSignals(True)
            box.setCurrentIndex(box.findData(display.timezone))
            box.blockSignals(False)
        self._sync_utc_zone_box()
        self.time_zone_actions[timefmt.LOCAL].setText(
            timefmt.zone_label() if not timefmt.is_utc() else "Local")
        self.time_zone_actions[timefmt.UTC].setText(
            f"UTC \u2014 {timefmt.zone_label()}" if timefmt.is_utc() else "UTC")
        self.time_heading.setText(timefmt.heading())
        self.time_heading.setToolTip(f"Times are shown in {timefmt.zone_label()}")
        self.timeline.redraw_times()
        self._refresh_selected_panel()
        if problem:
            self._warn(problem, source="time-zone")
        else:
            self._clear_warning("time-zone")
            self.status.showMessage(f"Times are shown in {timefmt.zone_label()}.", 6000)

    def _choose_utc_zone(self) -> None:
        """View > Show Times In > UTC Offset or Region...: the same picker,
        in Session Options."""
        if not self.session_options_button.isChecked():
            self.session_options_button.setChecked(True)
        else:
            self.session_options_dialog.raise_()
        if self.time_zone_box.currentData() != "utc":
            self._set_time_zone("utc")
        self.session_options_dialog.scroll.ensureWidgetVisible(self.utc_zone_box)
        self.utc_zone_box.setFocus(QtCore.Qt.OtherFocusReason)

    def _on_quiet_seconds(self, value: float) -> None:
        """Takes effect from the next Start (the running gate keeps its own
        copy); refused while monitoring by the disabled control."""
        self.app.config.detector.hang_time = float(value)
        try:
            self.app.config.save()
        except OSError:  # noqa: BLE001 - a preference is never worth a crash
            pass

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.whisper_model_box.refresh_availability()
        self._refresh_install_model_button()
        self._refresh_save_path_row()
        for widget in (self.profile_box, self.profile_button,
                       self.source_mode_box, self.target_language_box,
                       self.quiet_seconds_box,
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
        if not path or self._store_gone():
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
            self._apply_calibration(result.recommended_threshold_dbfs)

    def _apply_calibration(self, threshold_dbfs: float) -> None:
        """The operator chose the suggested threshold: use it and save it, so
        it is still in force after a relaunch (it used to change only the
        running copy). Nothing else in the settings changes."""
        self.app.config.detector.mode = "fixed"
        self.app.config.detector.threshold_dbfs = threshold_dbfs
        try:
            self.app.config.save()
            saved = ""
        except OSError as exc:  # noqa: BLE001 - say so; the running value stands
            saved = f" (not saved: {exc})"
        self.status.showMessage(
            f"Detection threshold set to {threshold_dbfs} dBFS{saved}", 8000)

    # -- events ----------------------------------------------------------
    def _show_clipping(self, count: int = 0, total: int = 0) -> None:
        if not count:
            self.clip_label.clear()
            self.clip_label.setToolTip("")
            return
        self.clip_label.setText(f"{count:,} clipped samples")
        share = f"{count / total:.2%} of all input samples" if total else "These samples"
        self.clip_label.setToolTip(
            f"{share} reached at least {CLIP_THRESHOLD:.1%} of full scale during "
            "this monitoring run. A single recording can contain many clipped "
            "samples. Lower the input gain if clipping persists.")

    def _drain_events(self) -> None:
        if self._shutdown_complete or self._store_off_limits:
            return                     # the store is closing or closed
        touched = set()             # ids whose row a filtered view must re-read
        badge_changed = False       # queue length or a continuing part changed
        for event in self.app.events.drain():
            if self._from_an_earlier_capture(event):
                continue               # an ended run's meter/activity: not this input
            if event.kind == "level":
                reading = event.payload
                if self.waveform_stack.currentWidget() is self.live_waveform:
                    self.live_waveform.feed(reading)
                self.meter.set_reading(reading.rms_fraction, reading.peak_fraction,
                                       reading.clipped, reading.clip_count)
                self.input_panel.set_reading(
                    reading.rms_fraction, reading.peak_fraction,
                    reading.clipped, reading.clip_count,
                    spectrum=getattr(reading, "spectrum", None),
                    rms_dbfs=getattr(reading, "rms_dbfs", None))
                self._show_clipping(reading.clip_count, reading.sample_count)
            elif event.kind == "state":
                self._set_state(event.payload)
            elif event.kind in ("transmission", "updated"):
                # Only into the thread it was actually filed under. An
                # operator reviewing history must not see live traffic
                # appear in the Session they are reading - and a Search or
                # Review view admits only what its own query admits.
                if self._admit(event.payload, arrival=event.kind == "transmission"):
                    touched.add(event.payload.id)
                badge_changed = True
                sequence = getattr(self, "sequence", None)
                if sequence is not None and sequence.waiting:
                    sequence.notify_new_recording()
            elif event.kind == "save-path":
                self._on_save_path_event(event.payload or {})
            elif event.kind == "audio-status":
                payload = event.payload or {}
                kind = payload.get("kind", "")
                message = payload.get("message", "")
                self.status.showMessage(f"Audio: {kind} - {message}", 8000)
                self.input_panel.report_audio_status(kind, message)
                # Receiver-path kinds (stream, decoder, tuning) come only from
                # an extension's receiver, which handles its own wording.
                from ..extensions import hooks

                for audio_status in hooks("audio_status"):
                    audio_status(self, kind, message)
                if kind == "audio-resumed":
                    self._clear_warning("audio-input")
                elif kind == "receiver-shutdown-failed":
                    self._warn(message, source="audio-input")
                elif kind in ("disconnected", "reconnect-failed"):
                    self._warn(
                        "The selected audio input stopped responding. "
                        "BabelFishR is waiting for that same device and will "
                        "not record from anything else. Transmissions already "
                        "captured are safe.", source="audio-input")
                elif kind in ("connected", "reconnected"):
                    # The device is back, or a fresh run opened it. The
                    # warning about it was true and is not any more; nothing
                    # else on the banner is touched.
                    self._clear_warning("audio-input")
            elif event.kind == "error":
                payload = event.payload or {}
                self.status.showMessage(
                    f"{payload.get('stage', 'processing')} error: "
                    f"{payload.get('message', '')}", 12000)
        # Once a second while anything is held: which recording is being
        # processed, for how long, and each waiting card's place in line.
        import time as _t

        now = _t.monotonic()
        if now - getattr(self, "_queue_tick", 0.0) >= 1.0:
            self._queue_tick = now
            self.whisper_model_box.refresh_availability()
            if any(p is not None and p.visible_pending
                   for p in (self.app.pipeline, self.app.standalone_pipeline)) \
                    or getattr(self, "_queue_notes", None):
                self._refresh_queue_positions()
                badge_changed = True
        sequence = getattr(self, "sequence", None)
        if sequence is not None and sequence.waiting and not self._sequence_live():
            sequence.notify_live_ended()      # monitoring of that Session ended
        if touched:
            # One query for the whole drained batch, not one per event.
            self._refresh_view(touched)
        self._refresh_context_lines()
        if badge_changed:
            self._refresh_state_badge()

    def _from_an_earlier_capture(self, event) -> bool:
        """A level reading or capture activity state (Listening, Receiving)
        from a run that is no longer the one capturing - its drain and final
        flush run after Stop, or after a quick restart. It says nothing about
        the input now, so it changes nothing on screen. Transcripts,
        translations and processing states of that run still land: they
        finish after Stop by design."""
        run = getattr(event, "run", None)
        if run is None:
            return False
        session = self.app.session
        if session is not None and session.id == run:
            return False
        if event.kind == "level":
            return True
        return event.kind == "state" and event.payload in (
            PipelineState.LISTENING, PipelineState.RECEIVING, PipelineState.IDLE)

    def _admit(self, tx, arrival: bool) -> bool:
        """Show a live transmission or update as the current view allows.

        The Session's own thread takes every arrival at the top and every
        update in place, as before - and a shown message that has just been
        removed from view or deleted leaves it. A Search or Review view is
        different: it is the answer to a question, so nothing is drawn from
        the event itself; the event only says the answer may have changed.
        Returns True when the view has to be refreshed against its query
        (see _refresh_view), which the drain does once for the whole batch.

        Two things this must not do, both seen before: add every same-
        Session arrival to whatever was on screen, so a Search for one word
        filled up with unrelated traffic; and judge by the event's record
        alone, so a record that had merely fallen out of a full result set
        could never come back, and one that had just been removed from view
        was ignored before the view could be reconciled.
        """
        if self.app.store.is_stale(tx):
            # Queued before Clear Session History changed this row: draw the
            # row as it is now, never the old text or recording path.
            fresh = self.app.store.get_transmission(tx.id)
            if fresh is not None:
                tx = fresh
        if self._view_kind == "thread":
            if self._belongs_here(tx):
                if arrival:
                    self.timeline.add(tx)
                else:
                    self.timeline.update(tx)
            elif tx.id in self.timeline.order():
                self.timeline.remove(tx.id)    # removed from view or deleted
            return False
        # A filtered view: anything shown, or anything of this Session's,
        # can change the answer - a record that now qualifies, one that no
        # longer does, or a replacement for one that left a full result.
        return tx.id in self.timeline.order() or self._in_this_conversation(tx)

    def _refresh_view(self, touched=()) -> None:
        """Reconcile a Search or Review view to its own query, completely.

        The very store query that opened the view is run again - the same
        scope, semantics and limit - and its complete bounded result is the
        membership: a record it holds that is not shown is placed where its
        time puts it, a shown one whose event arrived (*touched*) is updated
        to the row as the store has it, anything shown that it does not hold
        leaves, and the count in the status line follows. Widgets that stay
        are the same widgets, under the same viewport anchoring as every
        other growth; nothing is rebuilt to change membership. Never runs
        against a closing store.
        """
        if self._view_kind == "thread" or self._store_gone():
            return
        results = self._view_results()
        wanted = {t.id: t for t in results}
        shown = set(self.timeline.order())
        playing = getattr(self, "_transient_card", None)
        for tx_id in shown - set(wanted):
            if tx_id == playing:
                continue                           # shown while it plays (see _follow_playing_clip)
            self.timeline.remove(tx_id)            # stopped qualifying
        for row in results:
            if row.id not in shown:
                self.timeline.place(row)           # newly qualifying, or a replacement
            elif row.id in touched:
                self.timeline.update(row)          # the row as the store has it
        self._announce_view(len(results))

    def _view_results(self):
        if self._view_kind == "search":
            return self.app.search(self._view_query)
        if self._view_kind == "review":
            return self.app.review_queue()
        if self._view_kind == "bookmarked":
            return self._bookmarked_results()
        if self._view_kind == "removed":
            return self._removed_results()
        return []

    def _view_summary(self, count: int) -> str:
        if self._view_kind == "search":
            return f"{count} match(es) for {self._view_query!r}"
        if self._view_kind == "review":
            return f"{count} transmission(s) need review"
        if self._view_kind == "bookmarked":
            return f"{count} bookmarked transmission(s)"
        if self._view_kind == "removed":
            return f"{count} removed message(s) - Restore puts one back"
        return ""

    def _announce_view(self, count: int) -> None:
        if self._view_kind != "thread":
            self.status.showMessage(
                f"{self._view_summary(count)} - All, above the messages, "
                f"goes back", 10000)

    def _belongs_here(self, tx) -> bool:
        """Is this transmission part of the Session currently on screen?

        Also: not deleted (a worker's late result for a deleted message must
        not draw it back), and not removed from view unless removed messages
        are being shown.
        """
        if self.app.store.is_deleted(tx.id):
            return False
        if getattr(tx, "hidden", False) and not self._showing_removed():
            return False
        return self._in_this_conversation(tx)

    def _in_this_conversation(self, tx) -> bool:
        """Was this transmission filed under the Session on screen?"""
        session_id = getattr(tx, "session_id", "")
        if not session_id:
            return False
        viewing = self.app.conversation_id
        session = self.app.store.get_session(session_id)
        if session is None:
            return False
        return (session.conversation_id or viewing) == viewing

    def _set_state(self, state: str) -> None:
        self._state = self._truthful_state(state)
        self._refresh_state_badge()

    def _truthful_state(self, state: str) -> str:
        """Reconcile a reported state with whether anything is being captured.

        "Listening" is a claim about the microphone: that a capture is open
        and waiting for the next transmission. Two things used to make that
        claim when it was false. Processing a saved recording with no
        monitoring running ended by announcing Listening; and the last events
        of a run that had already been stopped were drained after the badge
        had been set to Idle, and overwrote it. The events themselves are
        still delivered - a transcript arriving late must still reach its
        bubble - but a capture state only lands when there is a capture.

        COMPLETE is how the processing pipeline says "finished": it does not
        know whether a microphone is open and does not pretend to. Finished
        means back to whatever the capture is doing, or Idle if there is none.

        The capture's *current* state is the reference, not the existence of
        a capture object. A capture that has been created but not started, or
        one whose run has ended, is Idle by its own account; and a stale
        Receiving from an earlier moment must not overwrite a run that is
        Listening now.
        """
        capture = self.app.capture
        # What the capture is doing right now - Idle when there is none, and
        # Idle for one that exists but has not started or has stopped, since
        # the service sets its own state on both. This is the truth a queued
        # capture event is checked against, rather than the event itself: an
        # event describes the moment it was published, which may be a run
        # that has since stopped, or a state the capture has since left.
        baseline = capture.state if capture is not None else PipelineState.IDLE
        working = (PipelineState.TRANSCRIBING, PipelineState.TRANSLATING)
        if state in (PipelineState.LISTENING, PipelineState.RECEIVING,
                     PipelineState.COMPLETE):
            resolved = baseline
        elif state in working:
            # Processing activity is shown whether or not a microphone is
            # open - but only while there is processing. A Transcribing that
            # arrives after the work has finished describes nothing current.
            resolved = state if self._processing_active() else baseline
        else:
            resolved = state
        if resolved == PipelineState.IDLE and self._processing_active():
            # No capture, but a processor still holds work - the run was
            # stopped with transcription under way, and it carries on. Idle
            # would be false; the stage last reported is what is happening.
            return self._state if self._state in working else PipelineState.TRANSCRIBING
        return resolved

    def _processing_active(self) -> bool:
        """Is there processing the window should show? Manual jobs and the
        current run's work - not an engine call abandoned at Stop, which
        finishes unseen (it still counts for ownership: deletion, engine
        changes and Quit use the pipelines' full accounting)."""
        for pipeline in (self.app.pipeline, self.app.standalone_pipeline):
            if pipeline is not None and pipeline.visible_pending:
                return True
        return False

    @staticmethod
    def _clock(when) -> str:
        from ..timefmt import bare_clock

        return bare_clock(when)

    def _queue_view(self):
        """([(running job, its row)], [waiting ids in order]) across the
        processors, from their own snapshots."""
        running, waiting = [], []
        for pipeline in (self.app.pipeline, self.app.standalone_pipeline):
            snap = getattr(pipeline, "queue_snapshot", None) if pipeline else None
            if snap is None:
                continue
            try:
                snapshot = snap()
            except Exception:  # noqa: BLE001 - feedback only
                continue
            for job in snapshot.running:
                tx = (self.app.store.get_transmission(job.tx_id)
                      if not self._store_gone() else None)
                running.append((job, tx))
            waiting += list(snapshot.waiting)
        return running, waiting

    def _refresh_queue_positions(self) -> None:
        """Each held card says where it is: being processed (and how long)
        or its place in line. Refreshed about once a second while work is
        held; nothing is touched when there is none."""
        running, waiting = self._queue_view()
        notes = {job.tx_id: f"{job.stage} \u00b7 {int(job.seconds)} s"
                 for job, _ in running}
        for place, tx_id in enumerate(waiting, start=1):
            notes[tx_id] = ("next in line" if place == 1
                            else f"{place}{_ordinal(place)} in line")
        previous = getattr(self, "_queue_notes", {})
        for tx_id in set(previous) | set(notes):
            bubble = self.timeline.bubble(tx_id)
            if bubble is not None:
                bubble.set_queue_note(notes.get(tx_id, ""))
        self._queue_notes = notes

    def _refresh_state_badge(self) -> None:
        state = getattr(self, "_state", PipelineState.IDLE)
        text, tone, symbol = STATE_TEXT.get(
            state, (str(state).title(), "idle", "\u25cb"))
        text = self._badge_detail(state, text)
        # Symbol plus word, so the state is never carried by colour alone.
        self.state_badge.setText(f"{symbol} {text}")
        if self.state_badge.property("tone") != tone:
            self.state_badge.setProperty("tone", tone)
            self.state_badge.style().unpolish(self.state_badge)
            self.state_badge.style().polish(self.state_badge)
            self.state_badge.update()
        self.state_badge.setAccessibleDescription(text)
        tip = STATE_TIPS.get(state, "")
        if state is PipelineState.RECEIVING:
            seconds = self.app.config.detector.segment_seconds
            tip += (f" Long activity is handed to speech recognition in parts of "
                    f"about {seconds:g} seconds while it continues." if seconds else
                    " Segmenting is off: long activity reaches speech recognition "
                    "only when it ends.")
        self.state_badge.setToolTip(tip)

    def _badge_detail(self, state, text: str) -> str:
        """Say what the work is: Finishing (monitoring stopped, accepted
        messages still being processed), how many messages wait behind the
        one being processed, and whether audio is arriving continuously
        (handed on in parts rather than ending in quiet)."""
        working = (PipelineState.TRANSCRIBING, PipelineState.TRANSLATING)
        if state in working:
            pipelines = [p for p in (self.app.pipeline, self.app.standalone_pipeline)
                         if p is not None]
            pending = sum(p.visible_pending for p in pipelines)
            automatic = sum(getattr(p, "automatic_pending", 0) for p in pipelines)
            if self.app.capture is None and automatic:
                # A stopped run's messages; a manual job alone stays
                # "Transcribing".
                text = "Finishing"
            running, waiting = self._queue_view()
            if running:
                # Which recording, read from the processor itself.
                job, tx = running[0]
                when = self._clock(tx.started_at) if tx is not None else ""
                text += f" {when} ({int(job.seconds)} s)" if when else ""
                if waiting:
                    text += f" \u00b7 {len(waiting)} waiting"
            elif pending > 1:
                text += f" \u00b7 {pending - 1} queued"
        else:
            if state is PipelineState.RECEIVING:
                capture = self.app.capture
                if capture is not None and getattr(capture.detector, "continuing", False):
                    text += " \u00b7 continuous"
            # While monitoring the badge shows the input's state; processing
            # runs alongside it and is said too, from the processor's queue.
            running, waiting = self._queue_view()
            if running:
                job, tx = running[0]
                when = self._clock(tx.started_at) if tx is not None else ""
                text += f" \u00b7 processing {when} ({int(job.seconds)} s)"
                if waiting:
                    text += f" \u00b7 {len(waiting)} waiting"
        return text

    def _refresh_mode_badge(self) -> None:
        """Label the operating-mode control and mark the current choice.

        The word "mode" is in the button itself, so the toolbar reads
        "Operating mode: Offline Field Use" next to a separate readiness chip -
        two different things, no longer two identical-looking labels.
        """
        mode = self.app.mode
        self.mode_button.setText(f"{mode.label}  \u25be")
        # Styling only, from the mode the application is actually in.
        self.mode_button.setProperty("mode", mode.value)
        self.mode_button.style().unpolish(self.mode_button)
        self.mode_button.style().polish(self.mode_button)
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
        if ok and not self._store_gone():
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

        offline = self.app.mode is OperatingMode.FIELD_OFFLINE
        if offline:
            # A tick means the smoke tests ran for this configuration, so in
            # Offline Field Use every check runs them (on the worker thread).
            run_smoke_tests = True
        self._set_offline_status("checking" if offline else "hidden")

        # Immediately, before the worker is even launched. The badge used to
        # start blank and only ever said "Checking" if a *finished* report
        # happened to contain a skipped smoke test - so during the seconds the
        # check actually takes, the toolbar said nothing at all.
        self.ready_badge.setText("\u2026 Checking")
        self.ready_badge.setStyleSheet(
            f"color: {theme.status_color('working', self)};")
        self.ready_badge.setAccessibleDescription("Checking field readiness")

        # A previous model's slower smoke test may finish after the newly
        # selected model has already reported its own readiness. The old
        # report no longer describes the configuration: forget it.
        self._readiness_request = request = object()
        self._readiness = None

        def finished(report):
            if request is self._readiness_request:
                self._render_readiness(report)

        def failed(message):
            if request is self._readiness_request:
                self.ready_badge.setText("\u26a0 Unknown")
                if self.app.mode is OperatingMode.FIELD_OFFLINE:
                    self._set_offline_status(
                        "fail", [f"The readiness check could not finish: {message}"])

        from ..readiness import microphone_authorization

        # Read here, on the GUI thread, where Qt's permission API answers
        # (without ever showing the system dialog).
        self._readiness_worker = run_in_background(
            readiness_job, self.app, run_smoke_tests,
            microphone_permission=microphone_authorization(),
            on_finished=finished, on_failed=failed)

    def _render_readiness(self, report) -> None:
        from . import theme
        from ..readiness import offline_field_problems

        self._readiness = report
        # The destination was just checked: show what it is now.
        self._refresh_save_path_row()
        if self.app.mode is OperatingMode.FIELD_OFFLINE:
            self._set_offline_status("fail" if offline_field_problems(report) else "ok",
                                     offline_field_problems(report))
        else:
            self._set_offline_status("hidden")
        # Wording only. The decision - which branch a report lands in - is the
        # report's, and is unchanged: a skipped or failed check is never
        # rendered as success.
        if report.field_ready:
            # The operator asked for "Ready", shown only when actually ready.
            # This branch is the only one that is.
            text, tone = "\u2713 Ready", "ok"
            detail = ("Ready: recording, local transcription and local "
                      "translation all work on this computer.")
        elif report.field_ready_unknown:
            # Prepared, but the smoke tests have not run in this process yet.
            # Alpha 3 showed "Record only" here, on a machine with a working
            # Whisper model and working Argos routes, and it stayed wrong
            # until the operator restarted. "Not tested" is not "unavailable".
            text, tone = "\u2026 Checking", "working"
            detail = "Checking field readiness."
        elif report.can_record:
            # "Partly ready", not "Record only". The badge describes what is
            # installed and working; Record Only is an operating mode the
            # operator chooses, and the same words in this chip read as
            # though they had chosen it. The readiness dialog already calls
            # this state "Partly ready - recording works", so the chip now
            # agrees with it.
            text, tone = "\u25d1 Partly ready", "working"
            words = {"yes": "works", "NO": "not available yet",
                     "not checked yet": "installed, not checked yet"}
            detail = ("Partly ready: recording works; transcription "
                      f"{words[report.stage_word('transcribe')]}, translation "
                      f"{words[report.stage_word('translate')]}. This describes "
                      "what is installed, not the operating mode.")
        else:
            text, tone = "\u26a0 Not ready", "error"
            detail = "Not ready: audio capture is not working."
        self.ready_badge.setText(text)
        self.ready_badge.setStyleSheet(f"color: {theme.status_color(tone, self)};")
        self.ready_badge.setAccessibleDescription(detail)
        self.ready_badge.setToolTip(
            f"{detail}\n\nField readiness - click for the full report.")

    OFFLINE_CHECKING = "Checking\u2026"

    def _set_offline_status(self, state: str, problems=()) -> None:
        """The indicator beside the mode control: "checking", "ok", "fail"
        or "hidden" (another mode is selected)."""
        button = getattr(self, "offline_status", None)
        if button is None:
            return
        button.setVisible(state != "hidden")
        button.setProperty("state", state)
        if state == "checking":
            button.setText(self.OFFLINE_CHECKING)
            name = "Offline Field Use readiness: checking"
            tip = "Checking whether Offline Field Use is ready on this computer\u2026"
        elif state == "ok":
            button.setText("\u2713")
            name = "Offline Field Use readiness: ready"
            tip = ("Ready for Offline Field Use: audio, storage, the selected "
                   "speech model and translation are present and ran locally "
                   "just now.")
        elif state == "fail":
            button.setText("\u2715")
            name = "Offline Field Use readiness: not ready"
            tip = "Not ready for Offline Field Use:\n" + "\n".join(
                f"\u2022 {line}" for line in problems)
        else:
            button.setText("")
            name, tip = "Offline Field Use readiness", ""
        button.setAccessibleName(name)
        button.setAccessibleDescription(tip)
        button.setToolTip(f"{tip}\n\nClick for the full report." if tip else "")
        button.style().unpolish(button)
        button.style().polish(button)

    def _show_readiness(self) -> None:
        from .readiness_dialog import ReadinessDialog

        # The report behind the mark; None while a newer check is running
        # (the dialog then runs the same check itself).
        ReadinessDialog(self.app, self, report=self._readiness).exec()
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

    def _on_retry(self, tx_id: str) -> None:
        if self.app.retry(tx_id):
            self.status.showMessage("Retrying the saved recording...", 8000)
            return
        tx = self.app.store.get_transmission(tx_id)
        QtWidgets.QMessageBox.information(
            self, "Retry",
            (self.app.processing_problem(tx_id)
             or "The recording could not be queued again.")
            + ("\n\nThe recording itself is safe."
               if tx is not None and tx.audio_path else ""))

    AUTOMATIC_LANGUAGE = "Automatic (detect the language)"

    def _on_retry_as_speech(self, tx_id: str) -> None:
        """Transcribe as speech...: one choice - Automatic or a language -
        and one job for this recording. For a recording skipped or held as
        probably not speech, or recognised in the wrong language; the
        Session's own source-language setting applies to new recordings."""
        tx = self.app.store.get_transmission(tx_id)
        if tx is None:
            return
        labels = [self.AUTOMATIC_LANGUAGE] + [f"{name} ({code})"
                                              for code, name in COMMON_LANGUAGES]
        codes = [None] + [code for code, _ in COMMON_LANGUAGES]
        current = 0
        if (tx.source_language_mode is SourceLanguageMode.SPECIFIED
                and tx.source_language in codes):
            current = codes.index(tx.source_language)
        label, ok = QtWidgets.QInputDialog.getItem(
            self, "Transcribe as speech",
            "This recording is speech. Transcribe it (and translate it) in:",
            labels, current, False)
        if not ok:
            return
        code = codes[labels.index(label)]
        if self.app.transcribe_as_speech(tx_id, code):
            self.status.showMessage(
                f"Transcribing the saved recording as speech ({label})...", 8000)
            return
        QtWidgets.QMessageBox.information(
            self, "Transcribe as speech",
            (self.app.processing_problem(tx_id)
             or "The recording could not be queued.")
            + "\n\nThe recording itself is safe.")

    def _on_export_clip(self, tx_id: str) -> None:
        from ..export import export_transmission_audio

        tx = self.app.store.get_transmission(tx_id)
        if tx is None or not tx.audio_path:
            QtWidgets.QMessageBox.warning(self, "Export", "No audio for this clip.")
            return
        if not pathlib.Path(tx.audio_path).exists():
            # Removed outside BabelFishR (e.g. a cleanup): say so plainly;
            # the transcript and translation are kept.
            QtWidgets.QMessageBox.warning(
                self, "Export",
                "The original recording is no longer on disk, so it cannot be "
                "exported. The transcript and translation are kept.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export clip", str(self._export_directory() / f"{tx.id}.wav"),
            "WAV files (*.wav)")
        if path and not self._store_gone():
            export_transmission_audio(tx, path)
            self.status.showMessage(f"Exported {path}", 6000)

    # -- data views ------------------------------------------------------
    # -- named Session tabs ----------------------------------------------
    def _refresh_session_tabs(self) -> None:
        """Rebuild the tab bar from the database, keeping the selection."""
        if self._store_gone():
            return
        conversations = self.app.conversations(
            include_hidden=self._showing_hidden_sessions())
        selected = self.app.conversation_id
        if selected not in {c.id for c in conversations}:
            # Viewing a Session whose tab is not shown (it was hidden while
            # selected, or on relaunch): fall back to General rather than
            # show a thread with no tab.
            selected = self.app.select_conversation(
                self.app.store.default_conversation().id)
            self._reload_timeline()
        blocked = self.session_tabs.blockSignals(True)
        while self.session_tabs.count():
            self.session_tabs.removeTab(0)
        for index, conversation in enumerate(conversations):
            label = conversation.name + (" (hidden)" if conversation.hidden else "")
            self.session_tabs.addTab(label)
            self.session_tabs.setTabData(index, conversation.id)
            # The operator's colour, as a swatch beside the name. A swatch
            # rather than a recoloured label: the name stays in the theme's
            # text colour and so stays readable, and the selected tab is
            # still marked the way every other selected tab is.
            self.session_tabs.setTabIcon(index, _color_swatch(conversation.color))
            self.session_tabs.setTabToolTip(
                index, f"Tab colour {conversation.color}"
                if conversation.color else "")
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
        if conversation_id != self.app.conversation_id:
            self._stop_playback()           # never carried into another Session
        self.app.select_conversation(conversation_id)
        self._persist_selected_session()
        self._reload_timeline()
        self._refresh_capture_tab_label()
        # Another Session may save somewhere else: recheck where.
        self._refresh_save_path_row()
        self._refresh_readiness(run_smoke_tests=False)

    def _new_session_tab(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(
            self, "New Session", "Name for this Session:")
        if not ok or not name.strip() or self._store_gone():
            return
        conversation = self.app.create_conversation(name)
        self.app.select_conversation(conversation.id)
        self._persist_selected_session()
        self._refresh_session_tabs()
        self._reload_timeline()

    def _session_tab_menu(self, position) -> None:
        index = self.session_tabs.tabAt(position)
        if index < 0:
            return
        self._exec_session_menu(self._build_session_tab_menu(index),
                                self.session_tabs.mapToGlobal(position))

    def _build_session_tab_menu(self, index: int) -> QtWidgets.QMenu:
        """The right-click menu for one tab. Built apart from being shown,
        so its contents can be checked without a blocking popup."""
        menu = QtWidgets.QMenu(self)
        rename = menu.addAction("Rename Session\u2026")
        rename.triggered.connect(lambda: self._rename_session_tab(index))
        color = menu.addAction("Tab colour\u2026")
        color.triggered.connect(lambda: self._color_session_tab(index))
        conversation_id = self.session_tabs.tabData(index)
        conversation = (self.app.store.get_conversation(conversation_id)
                        if conversation_id else None)
        reset = menu.addAction("Default tab colour")
        reset.setEnabled(bool(conversation and conversation.color))
        reset.triggered.connect(lambda: self._color_session_tab(index, reset=True))
        menu.addSeparator()
        clear = menu.addAction(self.CLEAR_HISTORY)
        clear.setEnabled(conversation is not None)
        clear.setToolTip("Delete this Session's transcripts, audio files, or "
                         "both. The Session and its tab stay.")
        clear.triggered.connect(lambda: self._clear_session_history(index))
        if conversation is not None and conversation.hidden:
            restore = menu.addAction("Restore Session")
            restore.triggered.connect(lambda: self._restore_session_tab(index))
        else:
            hide = menu.addAction(self.HIDE_SESSION_ACTION)
            hide.setToolTip("Hide this Session's tab. Everything in it is kept; "
                            "View \u203a Show hidden Sessions brings it back.")
            hide.triggered.connect(lambda: self._hide_session_tab(index))
            delete = menu.addAction(self.DELETE_SESSION_ACTION)
            delete.triggered.connect(lambda: self._delete_session_tab(index))
            if conversation is not None and conversation.is_default:
                # The permanent General tab: emptied with Clear Session
                # History, never hidden or deleted.
                for action in (hide, delete):
                    action.setEnabled(False)
                    action.setToolTip(self.app.GENERAL_REMOVAL_PENDING)
        return menu

    HIDE_SESSION_ACTION = "Hide Session"
    DELETE_SESSION_ACTION = "Delete Session\u2026"

    def _show_session_actions(self) -> None:
        index = self.session_tabs.currentIndex()
        if index < 0:
            return
        menu = self._build_session_tab_menu(index)
        rect = self.session_tabs.tabRect(index)
        self._exec_session_menu(menu, self.session_tabs.mapToGlobal(rect.bottomLeft()))

    def _exec_session_menu(self, menu: QtWidgets.QMenu, where: QtCore.QPoint) -> None:
        """Show a Session's context menu (one place, so every route - mouse,
        shortcut, keyboard context-menu event - behaves the same). Focus
        goes back to the Sessions list afterwards."""
        menu.exec(where)
        # A closed popup does not always hand activation back (seen on
        # macOS): without it no key reaches the list, and the next Menu key
        # or Shift+F10 does nothing. Take it back, unless a dialog the menu
        # opened (Delete Session..., a colour) is still up.
        if self.isVisible() and QtWidgets.QApplication.activeModalWidget() is None:
            self.activateWindow()
            self.raise_()
            self.session_tabs.setFocus(QtCore.Qt.PopupFocusReason)
            # Activation can land a moment later (macOS): focus the list
            # again once it has, unless the operator moved on meanwhile.
            QtCore.QTimer.singleShot(0, self._refocus_session_list)

    def _refocus_session_list(self) -> None:
        if (self.isActiveWindow() and QtWidgets.QApplication.activePopupWidget() is None
                and QtWidgets.QApplication.activeModalWidget() is None
                and QtWidgets.QApplication.focusWidget() in (None, self)):
            self.session_tabs.setFocus(QtCore.Qt.PopupFocusReason)

    def _hide_session_tab(self, index: int) -> None:
        """Hide Session: at once, nothing deleted, undoable."""
        conversation_id = self.session_tabs.tabData(index)
        if not conversation_id or self._store_gone():
            return
        problem = self.app.conversation_removal_problem(conversation_id)
        if problem:
            QtWidgets.QMessageBox.information(
                self, "This Session cannot be hidden yet", problem)
            return
        hidden = self.app.hide_conversation(conversation_id)
        if hidden is None:
            return
        self._refresh_session_tabs()
        self._reload_timeline()
        self.status.showMessage(
            f"\u201c{hidden.name}\u201d hidden. Everything in it is kept - View "
            f"\u203a Show hidden Sessions to restore it.", 10000)

    def _delete_session_tab(self, index: int) -> None:
        """Delete Session...: ONE named confirmation offering Hide Session,
        Delete or Cancel (the default)."""
        conversation_id = self.session_tabs.tabData(index)
        if not conversation_id or self._store_gone():
            return
        problem = self.app.conversation_removal_problem(conversation_id)
        if problem:
            QtWidgets.QMessageBox.information(
                self, "This Session cannot be deleted yet", problem)
            return
        conversation = self.app.store.get_conversation(conversation_id)
        if conversation is None:
            return
        inventory = self.app.conversation_removal_inventory(conversation_id)
        runs, messages = len(inventory.session_ids), len(inventory.transmission_ids)
        lines = [f"\u2022 {runs} monitoring run(s)",
                 f"\u2022 {messages} message(s) with their transcripts, "
                 f"translations, notes, tags and search entries",
                 f"\u2022 {len(inventory.owned)} recording file(s) in "
                 f"BabelFishR's Recordings folder"]
        if inventory.shared:
            lines.append(f"\u2022 NOT deleted: {len(inventory.shared)} file(s) "
                         f"another Session's message still uses")
        if inventory.external:
            lines.append(f"\u2022 NOT deleted: {len(inventory.external)} file(s) "
                         f"outside BabelFishR's Recordings folder")
        lines += ["", "Hide Session keeps all of it instead (View \u203a Show "
                      "hidden Sessions brings it back). Delete cannot be undone; "
                      "other Sessions and copies you exported are not touched."]
        choice = self._choose(
            f"Delete Session \u201c{conversation.name}\u201d",
            f"Delete the Session \u201c{conversation.name}\u201d permanently?",
            "\n".join(lines), [self.HIDE_SESSION_ACTION, self.DELETE_CHOICE],
            destructive=self.DELETE_CHOICE)
        if self._store_gone() or not choice:
            return                                   # Cancel: nothing changed
        if choice == self.HIDE_SESSION_ACTION:
            self._hide_session_tab(index)
            return
        if (self.timeline.playback.owner
                and self.timeline.playback.owner in inventory.transmission_ids):
            self.timeline.playback.stop()
        problem = self.app.conversation_removal_problem(conversation_id)
        if problem:
            QtWidgets.QMessageBox.information(
                self, "This Session cannot be deleted yet", problem)
            return
        report = self.app.delete_conversation_permanently(conversation_id)
        self._refresh_session_tabs()
        self._reload_timeline()
        self._refresh_cleanup_offer()
        if report is None:
            return
        if report.complete:
            self.status.showMessage(
                f"\u201c{report.name}\u201d deleted: {len(report.session_ids)} "
                f"run(s), {len(report.messages)} message(s), "
                f"{len(report.removed_files)} file(s).", 10000)
        else:
            failed = "\n".join(f"{p}\n    {why}" for p, why in report.failed)
            QtWidgets.QMessageBox.warning(
                self, "Deletion not finished",
                f"\u201c{report.name}\u201d is deleted, but {len(report.failed)} "
                f"file(s) could not be removed:\n\n{failed}\n\nThey are recorded; "
                f"Retry file cleanup\u2026 (shown under Sessions) tries again.")

    DELETE_CHOICE = "Delete"

    CLEAR_HISTORY = "Clear Session History\u2026"

    def _run_clear_dialog(self, dialog) -> bool:
        """Shown modally; tests substitute this (a modal box waits for a person)."""
        return dialog.exec() == QtWidgets.QDialog.Accepted

    def _clear_session_history(self, index: int) -> None:
        from ..app import ProcessingBusy
        from .clear_history import ClearHistoryDialog

        conversation_id = self.session_tabs.tabData(index)
        if not conversation_id or self._store_gone():
            return
        conversation = self.app.store.get_conversation(conversation_id)
        if conversation is None:
            return
        problem = self.app.history_clear_problem(conversation_id)
        if problem:
            QtWidgets.QMessageBox.information(
                self, "This Session's history cannot be cleared yet", problem)
            return
        inventory = self.app.history_inventory(conversation_id)
        dialog = ClearHistoryDialog(conversation.name, inventory, self)
        accepted = self._run_clear_dialog(dialog)
        scope = dialog.scope()
        dialog.deleteLater()
        if not accepted or self._store_gone():
            self.status.showMessage("Clear Session History cancelled. Nothing "
                                    "was changed.", 6000)
            return
        # Nothing may be playing a file while it is unlinked.
        owner = self.timeline.playback.owner
        if scope != "text" and owner and owner in inventory.transmission_ids:
            self.timeline.playback.stop()
        try:
            report = self.app.clear_conversation_history(conversation_id, scope)
        except ProcessingBusy as exc:
            QtWidgets.QMessageBox.information(
                self, "This Session's history cannot be cleared yet", str(exc))
            return
        self._after_history_clear(report)
        if report is None:
            return
        what = {"text": "transcripts", "audio": "audio files",
                "both": "messages and audio"}[scope]
        if report.complete:
            self.status.showMessage(
                f"\u201c{report.name}\u201d: {what} cleared "
                f"({len(report.changed) + len(report.deleted)} message(s), "
                f"{len(report.removed)} file(s) removed"
                + (f", {len(report.already_gone)} already missing"
                   if report.already_gone else "") + ").", 10000)
        else:
            failed = "\n".join(f"{p}\n    {why}" for p, why in report.failed)
            QtWidgets.QMessageBox.warning(
                self, "Clear not finished",
                f"{len(report.failed)} file(s) in \u201c{report.name}\u201d could "
                f"not be removed:\n\n{failed}\n\n"
                + ("They stay attached to their messages and can still be "
                   "played. Fix the cause and choose Clear Session History "
                   "again to retry." if scope == "audio" else
                   "They are recorded so you can try again with Retry file "
                   "cleanup\u2026 under the Session buttons."))

    def _after_history_clear(self, report) -> None:
        """Redraw from the store, so no bubble, waveform or selected panel
        keeps a cleared text or a removed recording."""
        if report is None or self._store_gone():
            return
        self._refresh_cleanup_offer()
        selected = self.timeline.selected_id
        if report.conversation_id == self.app.conversation_id:
            if self._view_kind == "thread":
                self._reload_timeline()
            else:
                self._refresh_view(set(report.changed))
            if selected in self.timeline.order():
                self.timeline.select(selected)   # the same message, as it is now
        self._on_selection_changed(self.timeline.selected_id)
        self._refresh_session_tabs()

    def _remove_session_tab(self, index: int) -> None:
        """Older entry point: the same single Delete Session confirmation,
        which also offers Hide Session."""
        self._delete_session_tab(index)

    def _restore_session_tab(self, index: int) -> None:
        conversation_id = self.session_tabs.tabData(index)
        if not conversation_id:
            return
        restored = self.app.restore_conversation(conversation_id)
        if restored is None:
            return
        self._refresh_session_tabs()
        self.status.showMessage(f"\u201c{restored.name}\u201d restored.", 6000)

    def _color_session_tab(self, index: int, reset: bool = False) -> None:
        """Let the operator pick a colour for one tab, or clear it.

        Saved against the Session's id: renaming, reordering and relaunching
        leave it where it was put. Cancel changes nothing.
        """
        conversation_id = self.session_tabs.tabData(index)
        if not conversation_id:
            return
        conversation = self.app.store.get_conversation(conversation_id)
        if conversation is None:
            return
        if reset:
            chosen = ""
        else:
            initial = QtGui.QColor(conversation.color) if conversation.color \
                else QtGui.QColor(self.palette().highlight().color())
            picked = QtWidgets.QColorDialog.getColor(
                initial, self, f"Tab colour for \u201c{conversation.name}\u201d")
            if not picked.isValid():
                self.status.showMessage(
                    f"Tab colour unchanged for \u201c{conversation.name}\u201d",
                    6000)
                return
            chosen = picked.name()          # #rrggbb
        try:
            self.app.set_conversation_color(conversation_id, chosen)
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            log.exception("colouring Session %s failed", conversation_id)
            QtWidgets.QMessageBox.warning(
                self, "Tab colour",
                f"The colour for \u201c{conversation.name}\u201d was not "
                f"saved: {exc}")
            return
        self._refresh_session_tabs()
        self.status.showMessage(
            f"Tab colour for \u201c{conversation.name}\u201d "
            f"{'reset to default' if reset else 'set to ' + chosen}", 6000)

    def _rename_session_tab(self, index: int) -> None:
        """Rename one Session. The Session itself is untouched.

        Only the name changes: same id, same messages, same recordings, and
        a capture pinned to this Session keeps filing into it. Every outcome
        is visible - cancel and a blank name say "unchanged", a save that did
        not happen says so - because a rename that silently did nothing is
        indistinguishable, to the operator, from a button that does not work.
        """
        conversation_id = self.session_tabs.tabData(index)
        if not conversation_id:
            return
        current = self.session_tabs.tabText(index)
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename Session", "Name for this Session:",
            QtWidgets.QLineEdit.Normal, current)
        name = (name or "").strip()
        if not ok or not name or name == current:
            self.status.showMessage(
                f"Session name unchanged: \u201c{current}\u201d", 6000)
            return
        if self._store_gone():
            return
        try:
            renamed = self.app.rename_conversation(conversation_id, name)
        except Exception as exc:  # noqa: BLE001 - report it, do not crash
            log.exception("renaming Session %s failed", conversation_id)
            renamed = None
            failure = str(exc)
        else:
            failure = "" if renamed is not None else "it no longer exists"
        if renamed is None or renamed.name != name:
            QtWidgets.QMessageBox.warning(
                self, "Rename Session",
                f"\u201c{current}\u201d was not renamed"
                f"{': ' + failure if failure else ''}.\n\n"
                f"Its messages and recordings are untouched.")
            self._refresh_session_tabs()
            return
        self._refresh_session_tabs()
        self.status.showMessage(
            f"Renamed \u201c{current}\u201d to \u201c{name}\u201d", 6000)

    def _persist_selected_session(self) -> None:
        """Remember the tab, so a relaunch opens where the operator left."""
        try:
            self.app.config.save()
        except OSError:  # noqa: BLE001 - a preference is never worth a crash
            pass

    def _reload_timeline(self) -> None:
        """Restore the selected Session's thread, newest transmission first."""
        if self._store_gone():
            return
        self._view_kind, self._view_query = "thread", ""
        self.timeline.set_transmissions(self.app.recent_transmissions(
            include_hidden=self._showing_removed()), keep_playback=True)
        self._refollow_after_rebuild()
        self._sync_filters()

    def _showing_removed(self) -> bool:
        action = getattr(self, "show_removed_action", None)
        return bool(action is not None and action.isChecked())

    def _showing_hidden_sessions(self) -> bool:
        action = getattr(self, "show_hidden_sessions_action", None)
        return bool(action is not None and action.isChecked())

    # -- v5: selection, selected audio, context lines -------------------------
    def _on_selection_changed(self, tx_id) -> None:
        """Show the selected message's own recording, or say why there is none."""
        tx = self.timeline.selected_transmission()
        if tx is None:
            if getattr(self, "_followed_clip", None) is None:
                self.waveform.clear()
        else:
            # Re-check the original on disk: a file removed or restored
            # outside BabelFishR shows on the card as soon as it is chosen.
            bubble = self.timeline.bubble(tx.id)
            if bubble is not None:
                bubble.refresh_pairing()
            # While an original is playing the waveform shows what is heard;
            # choosing a message only targets it (Play selected).
            if getattr(self, "_followed_clip", None) is None:
                self.waveform.show_recording(tx.id, tx.audio_path)
        self._refresh_selected_panel()
        self._refresh_message_menu()

    # -- the Message menu: the selected message's actions, from the menu bar --
    def _build_message_menu(self, bar) -> None:
        """Every action of a message's \u22ef menu, for the selected message,
        from the menu bar and the keyboard.

        The \u22ef button is the only other way in, and it is a pop-up a
        click must open: on a Mac under assistive or automated control it
        never opened, which left "Retry as speech in..." - the way to give a
        message its real language - out of reach. These run the very same
        handlers as the row's own menu.
        """
        # Not on the menu bar any more (each card's own menu is the one place
        # for these); kept as the selected-message actions behind the
        # keyboard shortcuts, which are attached to the window itself.
        self.message_menu = QtWidgets.QMenu("Message", self)
        self.message_menu.setObjectName("messageMenu")
        self.message_menu.aboutToShow.connect(self._refresh_message_menu)

        def add(text, slot, shortcut=None, tip=""):
            action = QtGui.QAction(text, self)
            if shortcut:
                action.setShortcut(shortcut)
            if tip:
                action.setStatusTip(tip)
            action.triggered.connect(slot)
            self.message_menu.addAction(action)
            if shortcut:
                action.setShortcutContext(QtCore.Qt.WindowShortcut)
                self.addAction(action)            # works with no menu shown
            return action

        self.message_play_action = add(
            "Play original recording", lambda: self._on_selected("play"))
        self.message_menu.addSeparator()
        self.message_edit_action = add(
            "Edit transcript and translation...", lambda: self._on_selected("edit"),
            "Ctrl+E")
        self.message_note_action = add(
            "Add or edit note...", lambda: self._on_selected("note"))
        self.message_tags_action = add(
            "Edit tags...", lambda: self._on_selected("tags"))
        self.message_bookmark_action = add(
            "Bookmark", lambda: self._on_selected("bookmark"), "Ctrl+B")
        self.message_bookmark_action.setCheckable(True)
        self.message_menu.addSeparator()
        self.message_retry_speech_action = add(
            "Transcribe as speech\u2026", lambda: self._on_selected("retry-speech"),
            "Ctrl+L",
            "Transcribe the selected message as speech - a language you choose, "
            "or Automatic - then translate it")
        # The same action: "Transcribe anyway" was a second name for it.
        self.message_transcribe_action = self.message_retry_speech_action
        self.message_retry_action = add(
            "Retry processing", lambda: self._on_selected("retry"))
        self.message_export_action = add(
            "Export audio...", lambda: self._on_selected("export"))
        self.message_menu.addSeparator()
        self.message_remove_action = add(
            "Remove message\u2026", lambda: self._on_selected("remove"))
        self.message_restore_action = add(
            "Restore to thread", lambda: self._on_selected("restore"))
        self._refresh_message_menu()

    def _selected_bubble(self):
        tx = self.timeline.selected_transmission()
        return self.timeline.bubble(tx.id) if tx is not None else None

    def _refresh_message_menu(self) -> None:
        """Enabled for a selected message; mirrors what its own \u22ef menu
        offers (Transcribe anyway, Remove/Restore) and its bookmark."""
        if not hasattr(self, "message_menu"):
            return
        bubble = self._selected_bubble()
        for action in self.message_menu.actions():
            if not action.isSeparator():
                action.setEnabled(bubble is not None)
        if bubble is None:
            self.message_bookmark_action.setChecked(False)
            return
        self.message_bookmark_action.setChecked(bool(bubble.tx.bookmarked))
        self.message_transcribe_action.setEnabled(bubble.transcribe_action.isVisible()
                                                  and bubble.transcribe_action.isEnabled())
        self.message_remove_action.setEnabled(bubble.remove_action.isVisible())
        self.message_restore_action.setEnabled(bubble.restore_action.isVisible())
        self.message_play_action.setEnabled(bubble.play_action.isEnabled())

    def _on_selected(self, what: str) -> None:
        bubble = self._selected_bubble()
        if bubble is None:
            self.status.showMessage("Select a message first.", 5000)
            return
        tx_id = bubble.tx.id
        actions = {
            "play": bubble.play_action.trigger,
            "edit": bubble._edit,
            "note": bubble._edit_note,
            "tags": bubble._edit_tags,
            "bookmark": bubble.bookmark_action.trigger,
            "transcribe": bubble.transcribe_action.trigger,
            "retry": lambda: self._on_retry(tx_id),
            "retry-speech": lambda: self._on_retry_as_speech(tx_id),
            "export": bubble._export,
            "remove": bubble.remove_action.trigger,
            "restore": bubble.restore_action.trigger,
        }
        actions[what]()
        self._refresh_message_menu()

    def _refresh_selected_panel(self) -> None:
        tx = self.timeline.selected_transmission()
        from .timeline import original_present

        has = tx is not None
        present = bool(tx and original_present(tx))
        self._playback_selection_ready = bool(tx and tx.state.is_terminal and present)
        self._refresh_playback_bar()
        for button in (self.selected_play_button, self.selected_bookmark_button,
                       self.selected_note_button):
            button.setEnabled(has)
        if not has:
            self.selected_range.setText("")
            self.selected_hint.setText("Select a transmission to play.")
            self.selected_bookmark_button.setChecked(False)
            self.selected_play_button.setText(self.PLAY_SELECTED)
            return
        from ..timefmt import span

        self.selected_range.setText(
            f"{span(tx.started_at, tx.ended_at)}  ({float(tx.duration or 0.0):.1f} s)")
        # One action per situation: Pause/Resume belongs to the bar's own
        # button. When the chosen row is the recording already playing (or
        # paused), Play says so and does nothing; any other playable row
        # can be started from here.
        current = self._original_state(tx)
        if current == "playing":
            self.selected_play_button.setText("Now playing")
            self.selected_play_button.setEnabled(False)
        elif current == "paused":
            self.selected_play_button.setText("Paused")
            self.selected_play_button.setEnabled(False)
        else:
            self.selected_play_button.setText(self.PLAY_SELECTED)
            self.selected_play_button.setEnabled(self._playback_selection_ready)
        blocked = self.selected_bookmark_button.blockSignals(True)
        self.selected_bookmark_button.setChecked(bool(tx.bookmarked))
        self.selected_bookmark_button.setText(
            "Bookmarked" if tx.bookmarked else "Bookmark")
        self.selected_bookmark_button.blockSignals(blocked)
        self.selected_note_button.setText("Edit note" if tx.notes else "Add note")
        if not present:
            hint = ("The original recording of this transmission is missing."
                    if tx.audio_path else "This transmission has no recording.")
        elif not tx.state.is_terminal:
            hint = "Playback will be available when this transmission finishes processing."
        elif tx.state is ProcessingState.FAILED:
            hint = "Processing failed. The original audio is kept; Retry is in the message's menu."
        elif tx.needs_review:
            hint = "Check uncertain wording against the original audio."
        else:
            hint = ""
        self.selected_hint.setText(hint)

    PLAY_SELECTED = "\u25b6 Play selected"

    def _play_selected(self) -> None:
        from .timeline import original_present

        tx = self.timeline.selected_transmission()
        if tx is None or not tx.state.is_terminal or not original_present(tx):
            return                      # no deliberate target: nothing plays, nothing waits
        # The same controller as the message's own Play button - but this is
        # the ORIGINAL: pause/resume only when the original itself is what is
        # playing. The message's decoded audio (same id, other file) is
        # replaced by the original, never paused by this button.
        if self._original_state(tx) in ("playing", "paused"):
            return                      # already the current one: Pause/Resume is the bar's
        # Starts it - and what follows it - replacing anything else playing,
        # including this message's decoded audio.
        self._start_sequence(tx.id, tx.audio_path)
        self._refresh_selected_panel()

    def _original_state(self, tx) -> str:
        """stopped / playing / paused - for this message's original file."""
        from .waveform import same_audio

        controller = self.timeline.playback
        state = controller.state_for(tx.id)
        if state != "stopped" and same_audio(controller.path, tx.audio_path):
            return state
        return "stopped"

    def _bookmark_selected(self) -> None:
        tx = self.timeline.selected_transmission()
        bubble = self.timeline.bubble(tx.id) if tx is not None else None
        if bubble is not None:
            # Exactly the message menu's Bookmark: the same action and signal.
            bubble.bookmark_action.setChecked(not tx.bookmarked)
            bubble._toggle_bookmark()
        self._refresh_selected_panel()

    def _note_selected(self) -> None:
        tx = self.timeline.selected_transmission()
        bubble = self.timeline.bubble(tx.id) if tx is not None else None
        if bubble is not None:
            bubble._edit_note()

    def _refresh_counts(self) -> None:
        """Footer: counts of what is on screen, and whether originals are kept."""
        shown = [self.timeline.bubble(tx_id).tx for tx_id in self.timeline.order()
                 if self.timeline.bubble(tx_id) is not None]
        review = sum(1 for tx in shown if tx.needs_review)
        parts = [f"{len(shown)} message{'s' if len(shown) != 1 else ''}",
                 f"{review} need{'s' if review == 1 else ''} review"]
        # The setting, not a claim about every old file: recordings can have
        # been removed since.
        if getattr(self.app.config.recording, "enabled", True):
            parts.append("Keep original audio: on")
        else:
            parts.append("Keep original audio: off")
        text = "  |  ".join(parts)
        if self.counts_label.text() != text:
            self.counts_label.setText(text)

    def _source_text(self) -> str:
        """What is really being read: the running session's own source when
        there is one (a recorded file during replay), else the selected input."""
        session = getattr(self.app, "session", None)
        device = getattr(session, "audio_device", "") if session is not None else ""
        if device.startswith("replay:"):
            name = pathlib.Path(device[len("replay:"):]).name
            capture = getattr(self.app, "capture", None)
            source = getattr(capture, "source", None)
            thread = getattr(capture, "_thread", None)
            ended = (capture is None or bool(getattr(source, "finished", False))
                     # A capture that failed mid-file has stopped reading too.
                     or (thread is not None and not thread.is_alive()))
            return f"SOURCE: RECORDED FILE \u00b7 {name}" + (" \u00b7 ended" if ended else "")
        status = self.input_panel.status_label.text().strip()
        source = status[len("INPUT:"):].strip() if status.upper().startswith("INPUT:") else status
        return f"SOURCE: {source or 'none selected'}"

    def _refresh_context_lines(self) -> None:
        """Band source, language and count lines, from the live state."""
        text = self._source_text()
        if self.source_label.toolTip() != text:
            self.source_label.setToolTip(text)
            self.source_label.setAccessibleDescription(text)
        # Long or non-ASCII file names are elided to the band's middle, never
        # pushing the mode or the state off it; the full text is the tooltip.
        room = max(160, self.status_band.width() // 2)
        shown = self.source_label.fontMetrics().elidedText(
            text, QtCore.Qt.ElideMiddle, room)
        if self.source_label.text() != shown:
            self.source_label.setText(shown)
        profile = (self.channel_label.text()
                   if self.profile_box.currentData() else "")
        if self.profile_line.text() != profile:
            self.profile_line.setText(profile)
        self.profile_line.setVisible(bool(profile))
        code = self.target_language_box.currentData() or ""
        heading = f"TRANSLATION / {code.upper()}" if code else "TRANSLATION"
        if self.translation_heading.text() != heading:
            self.translation_heading.setText(heading)
        self._refresh_counts()

    # -- removing messages ---------------------------------------------------
    def _choose(self, title: str, text: str, informative: str,
                options: List[str], destructive: Optional[str] = None,
                default: Optional[str] = None) -> str:
        """Ask the operator to pick one of *options*; "" means cancelled.

        One modal question, built here so every removal dialog reads the
        same way. Tests substitute this method: a modal box waits for a
        person, and what they need to prove is what each answer does.
        """
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setWindowTitle(title)
        box.setText(text)
        box.setInformativeText(informative)
        buttons = {}
        for label in options:
            role = (QtWidgets.QMessageBox.DestructiveRole
                    if label == destructive else QtWidgets.QMessageBox.ActionRole)
            buttons[box.addButton(label, role)] = label
        cancel = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(cancel if default is None else next(
            b for b, label in buttons.items() if label == default))
        box.setEscapeButton(cancel)
        box.exec()
        return buttons.get(box.clickedButton(), "")

    REMOVE_KEEP = "Remove from thread (keep the recording and data)"
    DELETE_FOREVER = "Delete permanently\u2026"
    CONFIRM_DELETE = "Delete permanently"

    def _on_remove_message(self, tx_id: str) -> None:
        """The operator asked to remove one message. Two very different
        outcomes are offered by name; Cancel changes nothing."""
        problem = self.app.removal_problem(tx_id)
        if problem:
            QtWidgets.QMessageBox.information(
                self, "This message cannot be removed yet", problem)
            return
        tx = self.app.store.get_transmission(tx_id)
        if tx is None:
            return
        from ..timefmt import stamp

        when = stamp(tx.started_at)
        choice = self._choose(
            "Remove message",
            f"Remove the message from {when}?",
            "Remove from thread keeps the recording and everything about "
            "the message; it disappears from this thread, search, review "
            "and exports, and can be brought back from the Removed "
            "filter above the messages.\n\nDelete permanently asks again, lists "
            "exactly what will be deleted, and cannot be undone.",
            [self.REMOVE_KEEP, self.DELETE_FOREVER],
            destructive=self.DELETE_FOREVER, default=self.REMOVE_KEEP)
        if self._store_gone():
            return
        if choice == self.REMOVE_KEEP:
            refreshed = self.app.remove_from_thread(tx_id)
            self._after_local_change(refreshed)
            self.status.showMessage(
                self._with_view_count(
                    "Message removed from the thread. Its recording and data "
                    "are kept - the Removed filter above the messages "
                    "restores it."), 10000)
        elif choice == self.DELETE_FOREVER:
            self._confirm_and_delete(tx)

    def _confirm_and_delete(self, tx) -> None:
        inventory = self.app.deletion_inventory(tx.id)
        if inventory is None:
            return
        lines = ["This will permanently delete:",
                 "\u2022 the message, its transcript, translation, notes, tags "
                 "and search entry"]
        if inventory.owned:
            lines.append(f"\u2022 {len(inventory.owned)} file(s) in BabelFishR's "
                         f"Recordings folder:")
            lines += [f"    {p}" for p in inventory.owned]
        else:
            lines.append("\u2022 no recording files (none of its files are in "
                         "BabelFishR's Recordings folder)")
        if inventory.shared:
            lines.append(f"\u2022 NOT deleted: {len(inventory.shared)} file(s) "
                         f"another message still uses")
        if inventory.external:
            lines.append(f"\u2022 NOT deleted: {len(inventory.external)} file(s) "
                         f"outside BabelFishR's Recordings folder (your own "
                         f"WAV files, exports, backups)")
        lines.append("")
        lines.append("Copies you exported or shared elsewhere are not touched. "
                     "This cannot be undone.")
        choice = self._choose("Delete permanently?",
                              "Delete this message and its recording for good?",
                              "\n".join(lines), [self.CONFIRM_DELETE],
                              destructive=self.CONFIRM_DELETE)
        if choice != self.CONFIRM_DELETE or self._store_gone():
            return
        # Nothing may be playing the file while it is unlinked.
        if self.timeline.playback.owner == tx.id:
            self.timeline.playback.stop()
        problem = self.app.removal_problem(tx.id)
        if problem:
            QtWidgets.QMessageBox.information(
                self, "This message cannot be deleted yet", problem)
            return
        report = self.app.delete_permanently(tx.id)
        self._refresh_cleanup_offer()
        self.timeline.remove(tx.id)
        self._refresh_view()           # a Search or Review view may owe a replacement
        if report is None:
            self.status.showMessage(
                self._with_view_count("That message was already gone."), 6000)
            return
        if report.complete:
            self.status.showMessage(self._with_view_count(
                f"Message deleted. {len(report.removed)} file(s) removed."), 8000)
        else:
            failed = "\n".join(f"{p}\n    {why}" for p, why in report.failed)
            QtWidgets.QMessageBox.warning(
                self, "Deletion not finished",
                f"The message is deleted, but {len(report.failed)} file(s) "
                f"could not be removed:\n\n{failed}\n\nThey are recorded so "
                f"you can try again with Retry file cleanup\u2026 under the "
                f"Session buttons.")
            self.status.showMessage(self._with_view_count(
                f"Message deleted; {len(report.failed)} file(s) still on disk."),
                10000)

    def _on_restore_message(self, tx_id: str) -> None:
        restored = self.app.restore_to_thread(tx_id)
        if restored is None:
            return
        self._after_local_change(restored)
        self.status.showMessage(
            self._with_view_count("Message restored to the thread."), 6000)

    def _after_local_change(self, tx) -> None:
        """The operator changed one message here (removed it from view, or
        put it back): show the consequence as the current view defines it.

        The thread shows or drops it by the removed-messages preference. A
        Search or Review view is reconciled to its own query instead - the
        store's answer, not the widget's - so a message removed from view
        leaves a Search that excludes removed messages even while the
        thread would still show it, and a restored one comes back if it
        qualifies. An earlier version updated the widgets directly and a
        removed message stayed in the Search view it no longer belonged to.
        """
        if tx is None:
            return
        if self._view_kind != "thread":
            self._refresh_view({tx.id})
        elif self._belongs_here(tx):
            self.timeline.update(tx)
        else:
            self.timeline.remove(tx.id)

    def _with_view_count(self, message: str) -> str:
        """Prefix an operator message with the filtered view's current count,
        so the status line stays truthful about the view it describes."""
        if self._view_kind == "thread":
            return message
        return f"{self._view_summary(self.timeline.count())}. {message}"

    def _refresh_cleanup_offer(self) -> None:
        """Retry file cleanup... is shown only while a past confirmed
        deletion still has files on disk it could not remove (read from the
        store, so it is offered again after a restart)."""
        if self._store_gone():
            return
        try:
            pending = self.app.leftover_deletions()
        except Exception:  # noqa: BLE001 - an offer is never worth a crash
            pending = {}
        count = sum(len(files) for files in pending.values())
        self.cleanup_button.setVisible(bool(count))
        self.finish_deletions_action.setEnabled(bool(count))
        self.cleanup_button.setToolTip(
            f"{count} file(s) from a confirmed deletion could not be removed "
            f"yet. Try again." if count else "")

    def _finish_unfinished_deletions(self) -> None:
        if self._store_gone():
            return
        before = self.app.leftover_deletions()
        if not before:
            self._refresh_cleanup_offer()
            QtWidgets.QMessageBox.information(
                self, "Retry file cleanup", "No files are waiting to be removed.")
            return
        count = sum(len(files) for files in before.values())
        choice = self._choose(
            "Retry file cleanup",
            f"Try again to remove {count} file(s) left by a deletion you "
            f"confirmed earlier?",
            "Each file is checked again first: one another message now uses is "
            "kept, and nothing outside BabelFishR's Recordings folder is touched.",
            [self.RETRY_CLEANUP])
        if choice != self.RETRY_CLEANUP or self._store_gone():
            return                                   # Cancel: nothing changed
        try:
            result = self.app.retry_leftover_deletions()
        finally:
            self._refresh_cleanup_offer()
        done = len(result.removed) + len(result.already_gone)
        summary = f"Removed {done} file(s)."
        if result.preserved_paths:
            kept = "\n".join(result.preserved_paths)
            summary += (f"\n\nKept {len(result.preserved_paths)} file(s) that "
                        f"another message now uses:\n{kept}")
        if result.still:
            remaining = "\n".join(p for files in result.still.values() for p in files)
            QtWidgets.QMessageBox.warning(
                self, "Retry file cleanup",
                f"{summary}\n\nStill could not remove:\n\n{remaining}\n\n"
                f"Retry file cleanup stays offered until they are gone.")
        else:
            QtWidgets.QMessageBox.information(
                self, "Retry file cleanup", f"{summary} All done.")

    RETRY_CLEANUP = "Retry file cleanup"

    def _search(self) -> None:
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Search", "Search original and translated text:")
        if not ok:
            return
        self._run_search(text)

    def _run_search(self, text: str) -> None:
        """Search this Session's original and translated text - the same
        store query View \u25b8 Search runs. An empty search shows everything."""
        if self._store_gone():
            return
        if not text.strip():
            self._reload_timeline()
            return
        results = self.app.search(text)
        self._view_kind, self._view_query = "search", text
        self.timeline.set_transmissions(results, keep_playback=True)
        self._refollow_after_rebuild()
        self._announce_view(len(results))
        self._sync_filters()

    def _on_search_text_changed(self, text: str) -> None:
        if not text.strip() and self._view_kind == "search":
            self._reload_timeline()

    def _show_bookmarked(self) -> None:
        """Only this Session's bookmarked messages - a store query, like
        Search and Review, and kept true as traffic arrives."""
        if self._store_gone():
            return
        results = self._bookmarked_results()
        self._view_kind, self._view_query = "bookmarked", ""
        self.timeline.set_transmissions(results, keep_playback=True)
        self._refollow_after_rebuild()
        self._announce_view(len(results))
        self._sync_filters()

    REMOVED_EMPTY = (
        "No removed messages in this Session.\n\n"
        "A message you remove from the thread (its \u22ef menu \u203a Remove "
        "message\u2026) appears here, with Restore to put it back. Its "
        "recording and text are kept.")

    def _show_removed(self) -> None:
        """This Session's removed-from-view messages - a store query like
        Bookmarked, kept true as messages are removed and restored."""
        if self._store_gone():
            return
        results = self._removed_results()
        self._view_kind, self._view_query = "removed", ""
        self.timeline.set_transmissions(results, keep_playback=True)
        self._refollow_after_rebuild()
        self._announce_view(len(results))
        self._sync_filters()

    def _removed_results(self):
        return self.app.store.hidden_transmissions(self.app.conversation_id)

    def _bookmarked_results(self):
        # Store.search never returns removed messages; nor does this view.
        return self.app.search("", bookmarked=True)

    def _focus_search(self) -> None:
        self.search_field.setFocus(QtCore.Qt.ShortcutFocusReason)
        self.search_field.selectAll()

    def _sync_filters(self) -> None:
        """The filter buttons say which view is on screen - none for Search."""
        buttons = {"thread": self.filter_all, "review": self.filter_review,
                   "bookmarked": self.filter_bookmarked,
                   "removed": self.filter_removed}
        self.timeline.set_empty_text(
            self.REMOVED_EMPTY if self._view_kind == "removed" else None)
        target = buttons.get(self._view_kind)
        self.filter_group.setExclusive(False)
        for button in buttons.values():
            button.setChecked(button is target)
        self.filter_group.setExclusive(True)
        if self._view_kind != "search" and self.search_field.text():
            blocked = self.search_field.blockSignals(True)
            self.search_field.clear()
            self.search_field.blockSignals(blocked)
        elif self._view_kind == "search":
            blocked = self.search_field.blockSignals(True)
            self.search_field.setText(self._view_query)
            self.search_field.blockSignals(blocked)
        self._refresh_counts()

    def _show_review_queue(self) -> None:
        if self._store_gone():
            return
        results = self.app.review_queue()
        self._view_kind, self._view_query = "review", ""
        self.timeline.set_transmissions(results, keep_playback=True)
        self._refollow_after_rebuild()
        self._announce_view(len(results))
        self._sync_filters()

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
            # No explanatory row: the Radio profile control says it.
            self.channel_label.setText("")
            self.channel_label.hide()
        else:
            self.channel_label.setText(
                f"Monitoring {profile.name}  ·  {profile.label()}  "
                f"(operator-supplied metadata)")
            self.channel_label.show()

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
        self.source_language_box.setEnabled(specified and self.app.capture is None)
        self._save_source_language()

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
        if not directory or self._store_gone():
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
            self, "Export", str(self._export_directory() / f"babelfishr_{session.id}.{fmt}"),
            filters[fmt])
        if not path or self._store_gone():
            return
        renderer = renderers[fmt]
        text = (renderer(transmissions, session) if fmt != "csv"
                else renderer(transmissions))
        pathlib.Path(path).write_text(text, encoding="utf-8")
        self.status.showMessage(f"Exported {path}", 8000)

    # -- shutdown --------------------------------------------------------
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self._shutdown_complete or self._finish_shutdown():
            # Nothing may continue playing, or start, after the window goes.
            sequence = getattr(self, "sequence", None)
            if sequence is not None:
                sequence.cancel()
            # Stop any waveform read; its worker leaves at the next chunk.
            self.waveform.shutdown()
            # Space is this window's playback key only while it exists.
            space = getattr(self, "_space_filter", None)
            if space is not None:
                space.detach()
            super().closeEvent(event)
            return
        # Work is still outstanding, a capture is still finishing, an
        # analysis is still saving, or the close itself failed. The window
        # stays, the event loop keeps turning, and the close is retried from
        # a timer until the application reports that nothing can still use
        # its engines or its store. A repeated Quit lands here again and
        # changes nothing.
        self._announce_quit_pending()
        self.start_button.setEnabled(False)
        if not self._quit_timer.isActive():
            self._quit_timer.start()
        event.ignore()

    def _announce_quit_pending(self) -> None:
        if self._shutdown_error:
            text = (f"Could not finish quitting: {self._shutdown_error}. "
                    f"Retrying - nothing still in use has been closed.")
        elif self._waiting_for_dialog:
            text = "Quitting once the open dialog is closed."
        elif self._store_off_limits:
            text = "Quitting - closing the engines and the database."
        else:
            parts = []
            outstanding = self.app.outstanding_work()
            if outstanding:
                parts.append(f"{outstanding} transmission(s) finish processing")
            for name in self.app.active_operations():
                parts.append(f"the {name} finishes")
            if self.app.session_end_pending():
                parts.append("the end of the run is recorded")
            text = ("Quitting once " + (" and ".join(parts) if parts else
                    "the previous run has finished shutting down")
                    + " - the window stays responsive meanwhile.")
        self.status.showMessage(text, 0)

    def _finish_shutdown(self) -> bool:
        """Close the application if nothing can still use it. True when done.

        Closing never blocks this thread: the application either closes in
        order now - capture settled, workers ended, analyses saved, engines
        closed, store closed - or reports that it cannot yet. Once it has, the
        event drain stops for good, so no timer callback can reach the closed
        store. A close that *fails* is not success: nothing is marked
        complete, the timers keep running so it is retried, and the operator
        is told what failed. The application closes each resource once, and
        only at the very end, so a retry cannot close something still in use.
        """
        try:
            done = self.app.close(wait=False, start_cleanup=False)
        except Exception as exc:  # noqa: BLE001 - report, keep, retry
            log.exception("closing the application failed")
            self._shutdown_error = f"{type(exc).__name__}: {exc}"
            return False
        self._waiting_for_dialog = False
        if not done and self.app.ready_for_cleanup and not self.app.cleaning:
            # Every other user of the store has finished. Before the store may
            # close, every route from this window to it is shut - menus and
            # their shortcuts, tool bars, the central controls, the event
            # drain - and any handler already past a dialog finds the door
            # closed too (_store_gone). Only then is the cleanup asked for;
            # a modal dialog still open is let finish first, since its
            # handler continues on this thread when it closes.
            self._shut_store_routes()
            if QtWidgets.QApplication.activeModalWidget() is None:
                self.app.start_final_cleanup()
            else:
                self._waiting_for_dialog = True
        elif self.app.cleaning and not self._store_off_limits:
            self._shut_store_routes()          # cleanup begun another way
        # A failure inside the cleanup thread, or in the end-of-run write, is
        # reported by the application rather than raised here; it is shown,
        # and the retry is automatic.
        self._shutdown_error = (self.app.cleanup_error or self.app.persistence_error
                                or getattr(self.app, "receiver_error", "")
                                or getattr(self.app, "capture_error", ""))
        if done:
            self._quit_timer.stop()
            self._timer.stop()
            self._shutdown_complete = True
        return done

    def _shut_store_routes(self) -> None:
        """Close every way this window can reach the store. Idempotent.

        Disabling controls is the visible half; the other half is that every
        handler which reads the store after a dialog checks _store_gone(),
        and the event drain returns at once. The window keeps repainting and
        the quit timer keeps polling; only the status bar still speaks.
        """
        if self._store_off_limits:
            return
        self._store_off_limits = True
        self._timer.stop()
        self.menuBar().setEnabled(False)
        for toolbar in self.findChildren(QtWidgets.QToolBar):
            toolbar.setEnabled(False)
        central = self.centralWidget()
        if central is not None:
            central.setEnabled(False)
        for action in self.findChildren(QtGui.QAction):
            action.setEnabled(False)           # menus, shortcuts, context menus

    def _store_gone(self) -> bool:
        """True once the database may be closing: the caller must not touch it.

        For handlers that were already past a dialog, a queued callback, or
        anything else that reaches this thread after the routes were shut.
        """
        if not self._store_off_limits:
            return False
        self.status.showMessage(
            "Quitting - the database is closing, so that was not done.", 8000)
        return True

    def _quit_tick(self) -> None:
        if self._finish_shutdown():
            self.close()
        else:
            self._announce_quit_pending()


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
