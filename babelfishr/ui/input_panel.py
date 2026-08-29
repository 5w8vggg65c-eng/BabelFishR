"""Audio input selection, and the status line that must never lie about it.

The rule this whole module exists to enforce: *a field operator must never
believe BabelFishR is receiving radio traffic while it is actually recording
the surrounding room.*

Three things follow from that, and they are all here.

Nothing is selected for the operator. The window opens with no input chosen -
not the system default, not the first device in the list - and monitoring
cannot start until the operator has picked one and it is present.

The choice is remembered by stable identity, not by index, so the same
physical interface is found again after a replug or a reboot; and if it is
*not* found, nothing else is used in its place. The panel says so, in red,
rather than quietly recording the laptop microphone.

The active input is on screen the whole time it is being used, with a live
level meter beside it, because "which device am I actually listening to" is a
question the operator must never have to go looking for an answer to.
"""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtWidgets

from ..audio.devices import (AudioDevice, DeviceIdentity, backend_available,
                             list_input_devices, resolve_input, unique_labels)
from .widgets import LevelMeterWidget

#: Combo box entry kinds, stored as item data.
NONE = "none"
DEVICE = "device"
SYSTEM_DEFAULT = "system-default"

CHOOSE_TEXT = "— Choose an audio input —"
SYSTEM_DEFAULT_TEXT = ("Use the macOS system default input "
                       "(follows the system setting, and changes with it)")

PERMISSION_TEXT = (
    "BabelFishR needs macOS audio-input permission. You can then choose the "
    "MacBook microphone, a USB audio interface, or another connected input.")


class InputPanel(QtWidgets.QWidget):
    """Choose an input, see which one is live, and be told when it is gone."""

    selectionChanged = QtCore.Signal()

    def __init__(self, app, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.app = app
        self._devices = []
        self._loading = False
        self._monitoring = False
        self._profile_id: Optional[str] = None
        self._build()
        self.refresh_devices()

    # -- construction ----------------------------------------------------
    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        chooser = QtWidgets.QHBoxLayout()
        chooser.setSpacing(8)
        chooser.addWidget(QtWidgets.QLabel("Audio input"))

        self.device_box = QtWidgets.QComboBox()
        self.device_box.setMinimumWidth(340)
        self.device_box.setAccessibleName("Audio input device")
        self.device_box.setToolTip(
            "The input BabelFishR records from. Choose the MacBook microphone "
            "to test, or your radio interface for a watch.\n\n" + PERMISSION_TEXT)
        self.device_box.currentIndexChanged.connect(self._on_choice)
        chooser.addWidget(self.device_box, 1)

        self.rescan_button = QtWidgets.QToolButton()
        self.rescan_button.setText("↻")
        self.rescan_button.setToolTip("Rescan for connected audio inputs")
        self.rescan_button.setAccessibleName("Rescan audio inputs")
        self.rescan_button.clicked.connect(self.refresh_devices)
        chooser.addWidget(self.rescan_button)
        layout.addLayout(chooser)

        options = QtWidgets.QHBoxLayout()
        options.setSpacing(14)
        # There is deliberately no "lock input" checkbox. It used to be here,
        # ticked by default, and it changed nothing: capture resolved the saved
        # identity either way. A control that appears to protect the operator
        # and does not is worse than no control, so every explicitly chosen
        # device is now pinned to its identity, always, and the only way to
        # follow the system default is to choose that option by name.
        self.profile_check = QtWidgets.QCheckBox(
            "Remember this input for the selected radio profile")
        self.profile_check.setToolTip(
            "Selecting that profile will select this input again.")
        self.profile_check.toggled.connect(self._on_profile_link)
        options.addWidget(self.profile_check)
        options.addStretch(1)
        layout.addLayout(options)

        status = QtWidgets.QHBoxLayout()
        status.setSpacing(10)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("inputStatus")
        self.status_label.setAccessibleName("Selected audio input")
        self.status_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        status.addWidget(self.status_label)

        self.meter = LevelMeterWidget()
        self.meter.setToolTip("Live level on the selected input")
        self.meter.setAccessibleName("Input level")
        status.addWidget(self.meter, 1)
        layout.addLayout(status)

        self.alert_label = QtWidgets.QLabel()
        self.alert_label.setObjectName("inputAlert")
        self.alert_label.setWordWrap(True)
        self.alert_label.hide()
        layout.addWidget(self.alert_label)

    # -- device list -----------------------------------------------------
    def refresh_devices(self) -> None:
        """Rebuild the list without ever choosing on the operator's behalf.

        The system default is *labelled* as such, because that is useful
        information, but it is not selected. Selection only ever happens
        because the operator selected it, or because a previous, explicitly
        confirmed selection was found again by identity.
        """
        self._loading = True
        try:
            self.device_box.clear()
            self._devices = list_input_devices()
            self.device_box.addItem(CHOOSE_TEXT, {"kind": NONE})

            labels = unique_labels(self._devices)
            for device in self._devices:
                self.device_box.addItem(self._label_for(device, labels),
                                        {"kind": DEVICE, "index": device.index})
            self.device_box.addItem(SYSTEM_DEFAULT_TEXT,
                                    {"kind": SYSTEM_DEFAULT})
            self.device_box.setEnabled(bool(self._devices) and not self._monitoring)
            self._restore_selection()
        finally:
            self._loading = False
        self.refresh_status()

    def _label_for(self, device: AudioDevice, labels) -> str:
        kind = ("built-in microphone" if device.is_builtin
                else (f"{device.transport} input" if device.transport
                      else "external input"))
        label = f"{labels[device.index]}  —  {kind}"
        if device.is_default:
            label += "  —  currently the system default"
        return label

    def _restore_selection(self) -> None:
        """Re-select a previously confirmed input, by identity only."""
        selection = self.app.config.audio.input
        if not selection.confirmed:
            self.device_box.setCurrentIndex(0)
            return
        if selection.use_system_default:
            index = self.device_box.findData({"kind": SYSTEM_DEFAULT})
            self.device_box.setCurrentIndex(max(index, 0))
            return
        resolution = resolve_input(DeviceIdentity.parse(selection.identity),
                                   self._devices)
        if resolution.device is None:
            # Either the chosen device is not here, or several here are
            # indistinguishable from it. Both leave the box on "Choose an audio
            # input" so nothing can be started by accident; refresh_status says
            # which of the two situations it is.
            self.device_box.setCurrentIndex(0)
            return
        index = self.device_box.findData({"kind": DEVICE,
                                          "index": resolution.device.index})
        self.device_box.setCurrentIndex(max(index, 0))

    # -- operator actions ------------------------------------------------
    def _on_choice(self) -> None:
        if self._loading:
            return
        data = self.device_box.currentData() or {"kind": NONE}
        kind = data.get("kind")
        if kind == NONE:
            self.app.config.clear_input_selection()
        elif kind == SYSTEM_DEFAULT:
            self.app.config.record_system_default_input()
        else:
            device = self._device_at(data.get("index"))
            if device is None:
                self.refresh_devices()
                return
            self.app.config.record_input_selection(
                device, profile_id=(self._profile_id
                                    if self.profile_check.isChecked() else None))
        self.refresh_status()
        self.selectionChanged.emit()

    def _on_profile_link(self, checked: bool) -> None:
        if self._loading or not self._profile_id:
            return
        device = self.selected_device()
        if checked and device is not None:
            self.app.config.associate_profile_input(self._profile_id, device)
        elif not checked:
            if self.app.config.audio.profile_inputs.pop(self._profile_id, None):
                self.app.config.save()

    def _device_at(self, index) -> Optional[AudioDevice]:
        for device in self._devices:
            if device.index == index:
                return device
        return None

    # -- radio profiles --------------------------------------------------
    def set_profile(self, profile_id: Optional[str]) -> None:
        """Select the input this profile is wired to, if it is connected.

        If it is not connected, nothing is selected. Substituting a different
        device here would be the same failure as substituting one at capture
        time, only quieter.
        """
        self._profile_id = profile_id
        preferred = self.app.config.preferred_input_for_profile(profile_id)
        self._loading = True
        try:
            self.profile_check.setEnabled(bool(profile_id) and not self._monitoring)
            self.profile_check.setChecked(bool(profile_id) and not preferred.empty)
        finally:
            self._loading = False
        if profile_id is None or preferred.empty:
            self.refresh_status()
            return

        resolution = resolve_input(preferred, self._devices)
        if resolution.device is None:
            self._loading = True
            try:
                self.device_box.setCurrentIndex(0)
            finally:
                self._loading = False
            self.app.config.clear_input_selection()
            # refresh_status first: it repaints the status line and clears any
            # previous alert, so the message about *this* profile has to be
            # written after it, not before.
            self.refresh_status()
            if resolution.ambiguous:
                self._show_alert(
                    f"This radio profile expects <b>{preferred.describe()}</b>, "
                    f"and {len(resolution.candidates)} connected inputs are "
                    f"indistinguishable from it. BabelFishR cannot safely "
                    f"determine which one is carrying the radio, so it has "
                    f"selected none of them.")
            else:
                self._show_alert(
                    f"This radio profile expects <b>{preferred.describe()}</b>, "
                    f"which is not connected. No other input has been selected "
                    f"in its place. Connect it and press Rescan, or choose a "
                    f"different input deliberately.")
            return
        self.app.config.record_input_selection(resolution.device,
                                               profile_id=profile_id)
        self._loading = True
        try:
            index = self.device_box.findData({"kind": DEVICE,
                                              "index": resolution.device.index})
            self.device_box.setCurrentIndex(max(index, 0))
        finally:
            self._loading = False
        self.refresh_status()

    # -- state -----------------------------------------------------------
    def selected_device(self) -> Optional[AudioDevice]:
        data = self.device_box.currentData() or {}
        if data.get("kind") != DEVICE:
            return None
        return self._device_at(data.get("index"))

    def selected_identity(self) -> Optional[DeviceIdentity]:
        """The identity to capture from, or ``None`` for the system default."""
        status = self.app.input_status()
        if status["state"] == "system-default":
            return None
        identity = status["identity"]
        return None if identity.empty else identity

    def ready_to_monitor(self):
        """``(ok, message)``. Never returns ok for an unconfirmed input."""
        if not backend_available():
            return False, ("There is no working audio backend on this machine, "
                           "so nothing can be captured. Replaying a WAV file "
                           "still works.")
        status = self.app.input_status()
        state = status["state"]
        if state == "none":
            return False, ("Choose an audio input first. BabelFishR will not "
                           "pick one for you: the built-in microphone and a "
                           "radio interface are very different things to be "
                           "recording.\n\n" + PERMISSION_TEXT)
        if state == "missing":
            return False, (
                f"The selected audio input is not connected:\n\n"
                f"    {status['expected']}\n\n"
                f"BabelFishR will not substitute another input for it - not "
                f"the MacBook microphone, not the system default, and not "
                f"another interface that happens to be plugged in.")
        if state == "ambiguous":
            listed = "\n".join(f"    {name}" for name in status["candidates"])
            return False, (
                f"BabelFishR cannot safely determine which interface is "
                f"carrying the radio.\n\n"
                f"{len(status['candidates'])} connected inputs are "
                f"indistinguishable from the one you selected "
                f"({status['expected']}):\n\n{listed}\n\n"
                f"They report the same name, the same connection and the same "
                f"channel count, and macOS gives no unique identifier for "
                f"them, so nothing here can tell them apart - but only one of "
                f"them may have your radio plugged into it. BabelFishR will "
                f"not guess.\n\n"
                f"Disconnect the one you do not want and press Rescan.")
        return True, ""

    def refresh_status(self) -> None:
        """Repaint the always-visible INPUT line."""
        from . import theme

        status = self.app.input_status()
        state = status["state"]

        if state == "none":
            self._set_status("INPUT: none selected", "idle")
            self._show_alert("")
            self.meter.reset()
        elif state == "system-default":
            self._set_status("INPUT: macOS system default — SELECTED", "working")
            self._show_alert(
                "This follows the macOS system setting, so the device it "
                "means can change without warning. For a radio watch, select "
                "the interface itself.")
        elif state == "missing":
            self._set_status(
                f"INPUT: {status['expected']} — NOT CONNECTED", "error")
            self._show_alert(
                f"<b>RADIO INPUT DISCONNECTED.</b> {status['expected']} is not "
                f"connected. Nothing else has been selected in its place. "
                f"Reconnect it and press Rescan.")
            self.meter.reset()
        elif state == "ambiguous":
            self._set_status(
                f"INPUT: {status['expected']} — CANNOT IDENTIFY", "error")
            listed = "<br>".join(f"&nbsp;&nbsp;• {name}"
                                 for name in status["candidates"])
            self._show_alert(
                f"<b>BabelFishR cannot safely determine which interface is "
                f"carrying the radio.</b><br><br>"
                f"{len(status['candidates'])} connected inputs are "
                f"indistinguishable from the one you selected:<br>{listed}"
                f"<br><br>Only one of them may have your radio plugged into "
                f"it, and nothing macOS reports tells them apart. Nothing has "
                f"been selected and monitoring will not start. Disconnect the "
                f"one you do not want, then press Rescan.")
            self.meter.reset()
        else:
            device = status["device"]
            self._set_status(f"INPUT: {device.name} — CONNECTED", "listening")
            note = ""
            if device.is_builtin and self._monitoring:
                note = ("This is the microphone built into this Mac. It is "
                        "recording the room, not a radio.")
            self._show_alert(note)
        self.status_label.setStyleSheet(
            f"font-weight: 600; color: {theme.status_color(self._tone, self)};")

    def _set_status(self, text: str, tone: str) -> None:
        self._tone = tone
        self.status_label.setText(text)
        self.status_label.setAccessibleDescription(text)

    def _show_alert(self, html: str) -> None:
        from . import theme

        if not html:
            self.alert_label.hide()
            self.alert_label.setText("")
            return
        self.alert_label.setText(html)
        self.alert_label.setStyleSheet(
            f"color: {theme.status_color('error', self)}; font-weight: 600;")
        self.alert_label.show()

    def set_monitoring(self, monitoring: bool) -> None:
        """Freeze the input controls for the duration of a watch."""
        self._monitoring = bool(monitoring)
        for widget in (self.device_box, self.rescan_button,
                       self.profile_check):
            widget.setEnabled(not self._monitoring)
        if not self._monitoring:
            self.device_box.setEnabled(bool(self._devices))
            self.profile_check.setEnabled(bool(self._profile_id))
        self.refresh_status()

    def set_reading(self, rms: float, peak: float, clipped: bool,
                    clip_count: int) -> None:
        self.meter.set_reading(rms, peak, clipped, clip_count)

    def report_audio_status(self, kind: str, message: str) -> None:
        """React to the capture thread losing or regaining the device."""
        from . import theme

        if kind == "ambiguous-device":
            self._set_status("INPUT: CANNOT IDENTIFY", "error")
            self._show_alert(
                f"<b>BabelFishR cannot safely determine which interface is "
                f"carrying the radio.</b> {message}<br><br>Nothing is being "
                f"recorded. Recordings already captured are unaffected. "
                f"Disconnect the duplicate interface and press Rescan.")
            self.meter.reset()
        elif kind in ("disconnected", "reconnect-failed"):
            expected = (self.app.config.audio.input.label
                        or self.app.selected_input_identity().describe()
                        or "the selected input")
            self._set_status(f"INPUT: {expected} — DISCONNECTED", "error")
            self._show_alert(
                f"<b>RADIO INPUT DISCONNECTED.</b> {expected} stopped "
                f"responding. Recording of transmissions already captured is "
                f"unaffected. BabelFishR is waiting for <i>this</i> device to "
                f"come back and will not accept audio from anything else. "
                f"({message})")
            self.meter.reset()
        elif kind in ("connected", "reconnected"):
            self.refresh_status()
        self.status_label.setStyleSheet(
            f"font-weight: 600; color: {theme.status_color(self._tone, self)};")


__all__ = ["InputPanel", "PERMISSION_TEXT", "CHOOSE_TEXT",
           "SYSTEM_DEFAULT_TEXT"]
