"""What the operator can see and do about the audio input, in the real widget.

These drive the actual InputPanel and MainWindow against a simulated device
list. They are still simulations: no Mac, no USB interface, no radio.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from babelfishr.app import BabelFishRApp  # noqa: E402
from babelfishr.audio import devices as devices_module  # noqa: E402
from babelfishr.audio.devices import resolve_identity  # noqa: E402
from babelfishr.config import Config  # noqa: E402
from babelfishr.ui import input_panel as panel_module  # noqa: E402

from test_audio_source_selection import (builtin_mic,  # noqa: E402
                                         radio_interface, second_interface)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def wired(monkeypatch):
    """Pretend a Mac with the given inputs attached."""
    state = {"devices": []}

    def present():
        return list(state["devices"])

    for module in (devices_module, panel_module):
        monkeypatch.setattr(module, "list_input_devices", present)
    monkeypatch.setattr(panel_module, "resolve_identity",
                        lambda identity, devices=None:
                        resolve_identity(identity, present()))
    import babelfishr.app as app_module

    monkeypatch.setattr(app_module, "resolve_identity",
                        lambda identity, devices=None:
                        resolve_identity(identity, present()))
    monkeypatch.setattr(panel_module, "backend_available", lambda: True)

    def set_devices(*devices):
        state["devices"] = list(devices)

    return set_devices


@pytest.fixture
def app(tmp_path):
    config = Config()
    config.database = str(tmp_path / "test.sqlite3")
    config.recording.directory = str(tmp_path / "recordings")
    config.asr.engine = "mock"
    config.translate.engine = "mock"
    config.source_path = str(tmp_path / "settings.toml")
    application = BabelFishRApp(config=config)
    yield application
    application.close()


def _panel(app):
    return panel_module.InputPanel(app)


def _select(panel, device):
    """Do what the operator does: pick the row for this device."""
    index = panel.device_box.findData({"kind": panel_module.DEVICE,
                                       "index": device.index})
    assert index > 0, "the device is not offered in the list"
    panel.device_box.setCurrentIndex(index)
    return index


# ---- nothing is chosen for the operator -------------------------------
def test_the_panel_opens_with_nothing_selected(qt_app, app, wired):
    """Even though the built-in mic is the macOS default."""
    wired(builtin_mic(), radio_interface())
    panel = _panel(app)

    assert panel.device_box.currentIndex() == 0
    assert panel.device_box.currentText() == panel_module.CHOOSE_TEXT
    assert panel.status_label.text() == "INPUT: none selected"
    ok, message = panel.ready_to_monitor()
    assert ok is False
    assert "will not pick one for you" in message


def test_the_system_default_is_labelled_but_not_selected(qt_app, app, wired):
    wired(builtin_mic(), radio_interface())
    panel = _panel(app)

    texts = [panel.device_box.itemText(i)
             for i in range(panel.device_box.count())]
    assert any("currently the system default" in text for text in texts), (
        "highlighting the system default is useful; selecting it is not")
    assert panel.device_box.currentIndex() == 0


def test_every_input_is_offered_by_a_human_readable_name(qt_app, app, wired):
    wired(builtin_mic(), radio_interface(), second_interface())
    panel = _panel(app)

    texts = [panel.device_box.itemText(i)
             for i in range(panel.device_box.count())]
    assert any("MacBook Air Microphone" in t and "built-in microphone" in t
               for t in texts)
    assert any("USB Audio CODEC" in t for t in texts)
    assert any("Scarlett Solo USB" in t for t in texts)
    # A visibly labelled, deliberate option - never a silent fallback.
    assert panel_module.SYSTEM_DEFAULT_TEXT in texts


# ---- choosing, and being told what was chosen --------------------------
def test_choosing_an_interface_persists_it_and_shows_it(qt_app, app, wired):
    radio = radio_interface()
    wired(builtin_mic(), radio)
    panel = _panel(app)
    _select(panel, radio)

    assert app.config.selected_input().uid == radio.uid
    assert app.config.audio.input.confirmed is True
    assert app.config.audio.input.locked is True
    assert panel.status_label.text() == "INPUT: USB Audio CODEC — CONNECTED"
    assert panel.ready_to_monitor()[0] is True


def test_choosing_the_builtin_microphone_is_allowed_and_unlocked(qt_app, app,
                                                                 wired):
    mic = builtin_mic()
    wired(mic, radio_interface())
    panel = _panel(app)
    _select(panel, mic)

    assert app.config.selected_input().is_builtin
    assert app.config.audio.input.locked is False
    assert panel.ready_to_monitor()[0] is True


def test_the_selection_is_restored_on_the_next_launch(qt_app, app, wired):
    radio = radio_interface()
    wired(builtin_mic(), radio)
    _select(_panel(app), radio)

    # A fresh panel, as a relaunch would build - and the device has moved.
    wired(builtin_mic(), second_interface(), radio_interface(index=6))
    restored = _panel(app)
    assert restored.status_label.text() == "INPUT: USB Audio CODEC — CONNECTED"
    assert restored.selected_device().index == 6


def test_a_missing_interface_leaves_nothing_selected_and_says_so(qt_app, app,
                                                                 wired):
    radio = radio_interface()
    wired(builtin_mic(), radio)
    _select(_panel(app), radio)

    wired(builtin_mic())  # unplugged
    panel = _panel(app)
    assert panel.device_box.currentIndex() == 0, (
        "the mic must not slide into the selected slot")
    assert "NOT CONNECTED" in panel.status_label.text()
    assert "USB Audio CODEC" in panel.status_label.text()
    assert panel.alert_label.isVisibleTo(panel)
    assert "RADIO INPUT DISCONNECTED" in panel.alert_label.text()

    ok, message = panel.ready_to_monitor()
    assert ok is False
    assert "not the MacBook microphone" in message


def test_a_disconnect_during_a_watch_is_shown_in_red(qt_app, app, wired):
    radio = radio_interface()
    wired(builtin_mic(), radio)
    panel = _panel(app)
    _select(panel, radio)

    panel.report_audio_status("disconnected", "input device stopped")
    assert "DISCONNECTED" in panel.status_label.text()
    assert "RADIO INPUT DISCONNECTED" in panel.alert_label.text()
    assert "will not accept audio from anything else" in panel.alert_label.text()

    wired(builtin_mic(), radio)
    panel.report_audio_status("reconnected", "resumed")
    assert panel.status_label.text() == "INPUT: USB Audio CODEC — CONNECTED"


# ---- locking and radio profiles ---------------------------------------
def test_the_lock_can_be_turned_off_deliberately(qt_app, app, wired):
    radio = radio_interface()
    wired(builtin_mic(), radio)
    panel = _panel(app)
    _select(panel, radio)

    assert panel.lock_check.isChecked() is True
    panel.lock_check.setChecked(False)
    assert app.config.audio.input.locked is False


def test_a_profile_selects_its_own_input_when_it_is_present(qt_app, app, wired):
    radio = radio_interface()
    wired(builtin_mic(), radio)
    panel = _panel(app)
    _select(panel, radio)
    panel.set_profile("prof_gmrs16")
    panel.profile_check.setChecked(True)
    assert app.config.preferred_input_for_profile("prof_gmrs16").uid == radio.uid

    # No profile selected, and the operator switches to the laptop mic for a
    # quick check. That must not rewrite what the profile is wired to.
    panel.set_profile(None)
    _select(panel, builtin_mic())
    assert app.config.selected_input().is_builtin
    assert app.config.preferred_input_for_profile("prof_gmrs16").uid == radio.uid

    # Selecting the profile again brings its own input back.
    panel.set_profile("prof_gmrs16")
    assert app.config.selected_input().uid == radio.uid
    assert panel.selected_device().uid == radio.uid


def test_a_profile_whose_input_is_absent_selects_nothing(qt_app, app, wired):
    radio = radio_interface()
    wired(builtin_mic(), radio)
    panel = _panel(app)
    _select(panel, radio)
    app.config.associate_profile_input("prof_gmrs16", radio)

    wired(builtin_mic())
    panel.refresh_devices()
    panel.set_profile("prof_gmrs16")

    assert app.config.has_confirmed_input() is False
    assert panel.device_box.currentIndex() == 0
    assert "not connected" in panel.alert_label.text()
    assert panel.ready_to_monitor()[0] is False


# ---- the controls freeze for the duration of a watch -------------------
def test_input_controls_are_disabled_while_monitoring(qt_app, app, wired):
    radio = radio_interface()
    wired(builtin_mic(), radio)
    panel = _panel(app)
    _select(panel, radio)

    panel.set_monitoring(True)
    for widget in (panel.device_box, panel.rescan_button, panel.lock_check,
                   panel.profile_check):
        assert widget.isEnabled() is False

    panel.set_monitoring(False)
    assert panel.device_box.isEnabled() is True


def test_the_window_refuses_to_start_and_offers_real_options(qt_app, app,
                                                             wired, monkeypatch):
    """Start with nothing chosen: no session, and a dialog with three ways out."""
    from babelfishr.ui.main_window import MainWindow

    wired(builtin_mic(), radio_interface())
    window = MainWindow(app)
    seen = {}

    def fake_resolve(message):
        seen["message"] = message

    monkeypatch.setattr(window, "_resolve_input_problem", fake_resolve)
    window._start_monitoring()

    assert app.session is None, "no watch may begin without a chosen input"
    assert "Choose an audio input first" in seen["message"]

    import inspect

    source = inspect.getsource(MainWindow._resolve_input_problem)
    for option in ("Rescan", "Choose Different Input", "Record Later"):
        assert option in source


def test_the_input_panel_is_outside_the_collapsible_setup_box(qt_app, app,
                                                              wired):
    """It has to stay visible when the operator collapses the setup panel."""
    from babelfishr.ui.main_window import MainWindow

    wired(builtin_mic(), radio_interface())
    window = MainWindow(app)
    window.setup_box.setChecked(False)
    qt_app.processEvents()

    assert window.input_panel.isVisibleTo(window.input_box)
    assert window.input_box.isVisibleTo(window)


def test_the_permission_wording_is_plain(qt_app):
    text = panel_module.PERMISSION_TEXT
    assert "macOS audio-input permission" in text
    # macOS calls all audio capture "microphone", including a USB interface,
    # so the wording has to make clear what the operator is really choosing.
    assert "USB audio interface" in text and "MacBook microphone" in text


def test_calibration_uses_the_selected_input_not_a_stale_widget(qt_app, app,
                                                                wired,
                                                                monkeypatch):
    """Calibrating a different device than you monitor is worse than useless.

    This also guards a real break: _calibrate still read a combo box that had
    moved into the input panel, so pressing Calibrate raised AttributeError.
    """
    from babelfishr.ui.main_window import MainWindow

    wired(builtin_mic(), radio_interface())
    window = MainWindow(app)
    seen = {}
    monkeypatch.setattr(window, "_resolve_input_problem",
                        lambda message: seen.setdefault("message", message))

    # Nothing chosen: calibration must refuse the same way monitoring does,
    # rather than falling back to a device.
    window._calibrate()
    assert "Choose an audio input first" in seen["message"]

    radio = radio_interface()
    _select(window.input_panel, radio)
    opened = {}

    class FakeSource:
        def __init__(self, **kwargs):
            opened.update(kwargs)

        def start(self):
            raise RuntimeError("stop here; the identity is what matters")

    monkeypatch.setattr("babelfishr.audio.source.LiveAudioSource", FakeSource)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    window._calibrate()

    assert opened["identity"].uid == radio.uid
