"""The Receiver menu, receiver line and digital-analysis handlers.

Moved from ``babelfishr/ui/main_window.py``. Installed on a BabelFishR main
window by the ``window_built`` hook when KingFishR is started from its own
entry point; the method bodies are unchanged and still run as the window's
own methods.
"""

from __future__ import annotations

import pathlib
import types

from PySide6 import QtGui, QtWidgets


class ReceiverWindowMixin:
    """Methods bound onto the main window by :func:`install`."""

    def _refresh_sdr_label(self) -> None:
        from ..receiver import receiver_status
        from ..sources import sdr_status

        try:
            receiver = receiver_status(self.app.config)
        except Exception:  # noqa: BLE001 - a label, never a crash
            receiver = {"sdrpp": "", "sdrpp_running": False, "dsd_neo": "",
                        "dsd_neo_version": "", "problems": []}
        parts = []
        if receiver["sdrpp_running"]:
            parts.append("SDR++ running")
        elif receiver["sdrpp"]:
            parts.append("SDR++ installed")
        else:
            parts.append("SDR++ not installed")
        parts.append(f"DSD-neo {receiver['dsd_neo_version']}" if receiver["dsd_neo"]
                     else "DSD-neo not installed")
        settings = self.app.config.receiver
        if settings.frequency_hz:
            parts.append(f"{settings.frequency_hz / 1e6:.4f} MHz {settings.mode}"
                         + (f" · digital {settings.digital_protocol}"
                            if settings.digital else " · analog"))
        text = "Receiver: " + " · ".join(parts)
        legacy = sdr_status(self.app.config)
        if legacy["configured"]:
            text += (f" · recorded IQ: {legacy['detail']}" if legacy["available"]
                     else f" · recorded IQ unavailable: {legacy['reason'][:40]}")
        self.sdr_label.setText(text)
        if hasattr(self, "receiver_line"):
            self.receiver_line.setText(text)

    def _receiver_blocked(self) -> bool:
        """Quitting, or the store already shut: no receiver work begins."""
        if self.app.closing or self._store_off_limits:
            self.status.showMessage("BabelFishR is quitting; the receiver is not "
                                    "started now.", 6000)
            return True
        return False

    def _choose_sdrpp(self) -> None:
        if self._receiver_blocked():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose the SDR++ application", "/Applications",
            "Applications and programs (*.app sdrpp);;All files (*)")
        if self._store_gone() or not path:
            return
        self.app.config.receiver.sdrpp_path = path
        self._save_receiver_settings()
        self._refresh_sdr_label()
        self.input_panel.refresh_devices()

    def _choose_dsd(self) -> None:
        if self._receiver_blocked():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose the dsd-neo program", str(pathlib.Path.home()),
            "Programs (dsd-neo dsd-neo* *);;All files (*)")
        if self._store_gone() or not path:
            return
        self.app.config.analysis.dsd_path = path
        self._save_receiver_settings()
        self._refresh_sdr_label()

    def _save_receiver_settings(self) -> None:
        try:
            self.app.config.save()
        except OSError:  # noqa: BLE001 - a preference is never worth a crash
            pass

    def _open_receiver(self) -> None:
        """Start SDR++ (configured for BabelFishR) or show that it is running.
        Nothing is recorded until Start monitoring."""
        from ..receiver import ReceiverError

        if self._receiver_blocked():
            return
        try:
            self.app.receiver.ensure_sdrpp()
        except ReceiverError as exc:
            QtWidgets.QMessageBox.warning(self, "The receiver is not available", str(exc))
            self._refresh_sdr_label()
            return
        if self._store_gone():
            return
        changes = self.app.receiver.configuration_changes
        note = (" SDR++'s settings were adjusted for BabelFishR: " + "; ".join(changes)
                if changes else "")
        self.status.showMessage("SDR++ is running. Tune it in its own window or with "
                                "Receiver > Tune receiver." + note, 12000)
        self._refresh_sdr_label()

    def _tune_receiver(self) -> None:
        from ..analysis.dsd import PRESETS
        from ..receiver import SDRPP, ReceiverError

        if self._receiver_blocked():
            return
        settings = self.app.config.receiver
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Tune receiver")
        form = QtWidgets.QFormLayout(dialog)
        frequency = QtWidgets.QDoubleSpinBox()
        frequency.setDecimals(5)
        frequency.setRange(0.1, 2000.0)
        frequency.setSuffix(" MHz")
        frequency.setValue((settings.frequency_hz or 462.5625e6) / 1e6)
        form.addRow("Frequency", frequency)
        mode = QtWidgets.QComboBox()
        for name in SDRPP.rigctl_modes:
            mode.addItem(name)
        mode.setCurrentText(settings.mode or "FM")
        form.addRow("Mode", mode)
        width = QtWidgets.QComboBox()
        width.setObjectName("filterWidth")
        labels = {SDRPP.analog_bandwidth_hz: "12.5 kHz (analog voice, the usual channel)",
                  15000: "15 kHz",
                  SDRPP.digital_voice_bandwidth_hz: "20 kHz (digital voice: the width that "
                                                    "decoded DMR through SDR++ here)",
                  25000: "25 kHz (wide)"}
        for hz in SDRPP.bandwidth_choices_hz:
            width.addItem(labels.get(hz, f"{hz / 1000:g} kHz"), hz)
        current = settings.bandwidth_hz or SDRPP.analog_bandwidth_hz
        if width.findData(current) < 0:
            width.addItem(f"{current / 1000:g} kHz", current)
        width.setCurrentIndex(width.findData(current))
        form.addRow("Filter width", width)
        digital = QtWidgets.QCheckBox("Digital voice: decode with DSD-neo")
        digital.setChecked(settings.digital)
        form.addRow(digital)

        def digital_toggled(checked: bool) -> None:
            # Observed on SDR++ (contract.py): at 12.5 kHz its FM output of
            # a DMR signal clipped and dsd-neo decoded no voice; at 20 kHz
            # it decoded. The default follows the tick; a width the operator
            # picked deliberately is left alone.
            wanted = SDRPP.digital_voice_bandwidth_hz if checked else SDRPP.analog_bandwidth_hz
            other = SDRPP.analog_bandwidth_hz if checked else SDRPP.digital_voice_bandwidth_hz
            if width.currentData() == other:
                width.setCurrentIndex(width.findData(wanted))
        digital.toggled.connect(digital_toggled)
        protocol = QtWidgets.QComboBox()
        for preset in PRESETS:
            protocol.addItem(preset.label, preset.id)
        index = protocol.findData(settings.digital_protocol)
        protocol.setCurrentIndex(max(index, 0))
        form.addRow("Protocol", protocol)
        slot = QtWidgets.QComboBox()
        slot.addItem("Slot 1", 1)
        slot.addItem("Slot 2", 2)
        slot.setCurrentIndex(0 if settings.digital_slot != 2 else 1)
        form.addRow("TDMA slot", slot)
        note = QtWidgets.QLabel(
            "SDR++ is asked to tune and then read back. What it confirms is "
            "recorded on each message; a confirmed frequency is not proof that "
            "speech is arriving. One slot is transcribed at a time. Tuning done "
            "in the SDR++ window itself is respected: Start monitoring reads "
            "what SDR++ is tuned to.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        self.tune_dialog = dialog
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        if self._receiver_blocked():
            return                                    # Quit arrived under the dialog
        settings.frequency_hz = float(frequency.value()) * 1e6
        settings.mode = mode.currentText()
        settings.bandwidth_hz = int(width.currentData())
        settings.digital = digital.isChecked()
        settings.digital_protocol = protocol.currentData()
        settings.digital_slot = int(slot.currentData())
        try:
            self.app.config.save()
        except OSError:  # noqa: BLE001 - a preference is never worth a crash
            pass
        self._refresh_sdr_label()
        receiver = self.app.receiver
        if receiver.status()["sdrpp_running"]:
            try:
                result = receiver.tune()
            except ReceiverError as exc:
                receiver.pending_tune = True
                QtWidgets.QMessageBox.warning(self, "SDR++ did not tune", str(exc))
                return
            self.status.showMessage(
                f"SDR++ confirms {result['confirmed_hz'] / 1e6:.4f} MHz "
                f"{result['mode']}.", 10000)
        else:
            # Applied when BabelFishR next starts SDR++. An SDR++ the operator
            # opens and tunes themselves keeps their tuning.
            receiver.pending_tune = True
            self.status.showMessage("Saved. Applied when BabelFishR starts SDR++; an "
                                    "SDR++ you open yourself keeps its own tuning.", 8000)

    def _show_receiver_status(self) -> None:
        if self.app.closing:
            return
        status = self.app.receiver.status()
        usb = {True: "yes", False: "no", None: "still checking" if status.get("usb_probed") is False
               else "cannot tell here"}[status["rtl_sdr_present"]]
        lines = [
            f"SDR++ software: {status['sdrpp'] or 'not found'}",
            f"SDR++ running: {'yes' if status['sdrpp_running'] else 'no'}",
            f"Control connection (rigctl): {'connected' if status['control_connected'] else 'not connected'}",
            f"Audio arriving from the receiver: {'yes' if status['audio_receiving'] else 'no'}",
            f"DSD-neo: {status['dsd_neo'] or 'not found'} {status['dsd_neo_version']}",
            f"RTL-SDR on USB: {usb}",
            f"SDR++ settings folder: {status['root']}",
        ]
        tuning = status["tuning"]
        if tuning["confirmed_hz"]:
            when = tuning["confirmed_at"].astimezone().strftime("%H:%M:%S") if tuning["confirmed_at"] else ""
            lines.append(f"SDR++ last reported: {tuning['confirmed_hz'] / 1e6:.4f} MHz "
                         f"{tuning['mode']} (read at {when})")
        if tuning["requested_hz"]:
            lines.append(f"Last asked for: {tuning['requested_hz'] / 1e6:.4f} MHz")
        lines.append("Radio start: " + ("requested (SDR++ does not confirm it; arriving "
                                       "audio does)" if tuning["radio_start_requested"] else "not requested"))
        if status["problems"]:
            lines.append("")
            lines.extend(status["problems"])
        QtWidgets.QMessageBox.information(self, "Receiver status", "\n".join(lines))

    def _on_analyze_digital(self, tx_id: str, protocol: str) -> None:
        if self.app.closing:
            self.status.showMessage(
                "Quitting - digital analysis is not started.", 8000)
            return
        from ..digital import analyser as find_analyser

        analyser = find_analyser(self.app.config)
        if analyser is None:
            from .analysis_dialog import show_dsd_missing

            show_dsd_missing(self, self.app)
            return
        # dsd-neo can run for seconds - automatic hunting alone rotates for
        # about six - so it must not block the interface.
        from babelfishr.ui.workers import run_in_background

        from ..digital import analysis_job

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


def install(window) -> None:
    """Add the Receiver menu (before Tools), the receiver lines and the
    digital-analysis wiring to *window*."""
    for name, value in vars(ReceiverWindowMixin).items():
        if callable(value) and not name.startswith("__"):
            setattr(window, name, types.MethodType(value, window))
    window._analysis_worker = None

    # The receiver line under the radio profile, and the label in the
    # session grid, as before the split.
    window.receiver_line = QtWidgets.QLabel("")
    window.receiver_line.setObjectName("mutedLine")
    window.receiver_line.setWordWrap(True)
    window.receiver_line.setContentsMargins(10, 0, 10, 0)
    pane = window.profile_line.parentWidget().layout()
    pane.insertWidget(pane.indexOf(window.profile_line) + 1, window.receiver_line)
    window.sdr_label = QtWidgets.QLabel()
    window.sdr_label.setObjectName("sectionLabel")
    grid = window.channel_label.parentWidget().layout()
    if isinstance(grid, QtWidgets.QGridLayout):
        grid.addWidget(window.sdr_label, 2, 0, 1, 6)

    bar = window.menuBar()
    tools = window.findChild(QtWidgets.QMenu, "toolsMenu")
    _build_receiver_menu(window, bar)
    receiver_menu = window._receiver_menu
    if tools is not None:
        bar.insertMenu(tools.menuAction(), receiver_menu)
    window.timeline.analyzeDigitalRequested.connect(window._on_analyze_digital)
    window._refresh_sdr_label()


def _build_receiver_menu(self, bar) -> None:
    menu_owner = QtWidgets.QMenu("&Receiver", self)
    receiver_menu = menu_owner
    self.open_receiver_action = QtGui.QAction("Open receiver window (SDR++)", self)
    self.open_receiver_action.setStatusTip(
        "Start or show SDR++, the receiver window, configured to feed BabelFishR")
    self.open_receiver_action.triggered.connect(self._open_receiver)
    receiver_menu.addAction(self.open_receiver_action)
    self.tune_receiver_action = QtGui.QAction("Tune receiver...", self)
    self.tune_receiver_action.setStatusTip(
        "Frequency, mode and digital decoding for the SDR receiver")
    self.tune_receiver_action.triggered.connect(self._tune_receiver)
    receiver_menu.addAction(self.tune_receiver_action)
    self.receiver_status_action = QtGui.QAction("Receiver status...", self)
    self.receiver_status_action.triggered.connect(self._show_receiver_status)
    receiver_menu.addAction(self.receiver_status_action)
    receiver_menu.addSeparator()
    self.choose_sdrpp_action = QtGui.QAction("Choose SDR++ application...", self)
    self.choose_sdrpp_action.setStatusTip(
        "Point BabelFishR at SDR++ if it is not in Applications")
    self.choose_sdrpp_action.triggered.connect(self._choose_sdrpp)
    receiver_menu.addAction(self.choose_sdrpp_action)
    self.choose_dsd_action = QtGui.QAction("Choose DSD-neo program...", self)
    self.choose_dsd_action.setStatusTip(
        "Point BabelFishR at the dsd-neo program for digital voice")
    self.choose_dsd_action.triggered.connect(self._choose_dsd)
    receiver_menu.addAction(self.choose_dsd_action)
    self._receiver_menu = receiver_menu


def audio_status(window, kind: str, message: str) -> None:
    """Receiver-path wording for audio-status events (was in the window's
    event loop)."""
    if kind in ("decoder-exited", "stream-error", "receiver-lost"):
        window._warn(
            f"The receiver path stopped: {message} Nothing else is "
            f"being recorded in its place. Transmissions already "
            f"captured are safe. Press Stop, check SDR++, then start "
            f"again.", source="audio-input")
    elif kind == "upstream-lost":
        window._warn(
            f"DSD-neo has lost the audio from SDR++ ({message}). It keeps "
            f"trying to reconnect; nothing else is recorded in its place. "
            f"Check that SDR++ is running and its Network sink is on.",
            source="audio-input")
    elif kind == "upstream-restored":
        window._clear_warning("audio-input")
    elif kind == "audio-dropped":
        window._warn(f"Receiver audio is arriving faster than it can be handled; "
                   f"the oldest buffered audio is being dropped. {message}",
                   source="audio-input")
    elif kind == "tuned":
        window._refresh_sdr_label()
