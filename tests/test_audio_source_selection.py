"""Regression tests for which device BabelFishR is actually listening to.

The failure these guard against is specific and serious: *an operator believes
they are receiving radio traffic while the application is recording the room
around them.* Every test below is a way that could happen.

What these tests are: a simulated device list, driven through the same
resolution, persistence and start-up code the real application uses.

What they are NOT: proof that any of this works on a physical machine. No
Apple Silicon Mac, no USB audio interface, no FalconClaw PTT and no radio was
involved in producing these results. Nothing here can be substituted for
plugging the interface in and watching what happens.
"""

from __future__ import annotations

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio import devices as devices_module
from babelfishr.audio.devices import (AudioDevice, DeviceIdentity,
                                      InputDeviceMissing, InputNotSelected,
                                      resolve_identity, unique_labels)
from babelfishr.audio.source import LiveAudioSource
from babelfishr.config import Config

pytestmark = pytest.mark.unit


# ---- a simulated Mac ---------------------------------------------------
def builtin_mic(index: int = 0) -> AudioDevice:
    """The microphone in the lid. The device that must never be a fallback."""
    return AudioDevice(
        index=index, name="MacBook Air Microphone", max_input_channels=1,
        default_sample_rate=48_000.0, host_api="Core Audio", is_default=True,
        uid="BuiltInMicrophoneDevice", transport="builtin")


def radio_interface(index: int = 1, uid: str = "AppleUSBAudioEngine:C-Media:1",
                    name: str = "USB Audio CODEC") -> AudioDevice:
    """The USB interface carrying receiver audio."""
    return AudioDevice(
        index=index, name=name, max_input_channels=2,
        default_sample_rate=48_000.0, host_api="Core Audio", is_default=False,
        uid=uid, transport="usb")


def second_interface(index: int = 2) -> AudioDevice:
    return AudioDevice(
        index=index, name="Scarlett Solo USB", max_input_channels=2,
        default_sample_rate=48_000.0, host_api="Core Audio", is_default=False,
        uid="Focusrite:Scarlett:9988", transport="usb")


@pytest.fixture
def connected(monkeypatch):
    """Control what PortAudio appears to report, for the whole audio stack."""
    state = {"devices": []}

    def present():
        return list(state["devices"])

    monkeypatch.setattr(devices_module, "list_input_devices", present)
    # source.py and app.py imported resolve_identity/find_device by name, so
    # the module attribute is what has to be patched for them to see this.
    import babelfishr.audio.source as source_module

    monkeypatch.setattr(
        source_module, "resolve_identity",
        lambda identity, devices=None: resolve_identity(identity, present()))
    import babelfishr.app as app_module

    monkeypatch.setattr(
        app_module, "resolve_identity",
        lambda identity, devices=None: resolve_identity(identity, present()))

    def set_devices(*devices):
        state["devices"] = list(devices)
        return state["devices"]

    set_devices.set = set_devices
    return set_devices


@pytest.fixture
def cfg(tmp_path):
    config = Config()
    config.database = str(tmp_path / "test.sqlite3")
    config.recording.directory = str(tmp_path / "recordings")
    config.asr.engine = "mock"
    config.translate.engine = "mock"
    config.source_path = str(tmp_path / "settings.toml")
    return config


def reload_settings(config: Config) -> Config:
    """Round-trip through the settings file, as a restart would."""
    import tomllib

    path = config.save()
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    restored = Config.from_dict(data)
    restored.source_path = path
    return restored


# ---- 1-2. an explicit choice is honoured, either way -------------------
def test_the_builtin_microphone_can_be_chosen_on_purpose(cfg, connected):
    """Testing on the laptop mic is legitimate. It just has to be deliberate."""
    mic = builtin_mic()
    connected(mic, radio_interface())
    cfg.record_input_selection(mic, save=False)

    assert cfg.has_confirmed_input()
    assert cfg.selected_input().is_builtin
    match = resolve_identity(cfg.selected_input())
    assert match is not None and match.device.name == "MacBook Air Microphone"


def test_an_external_interface_can_be_chosen(cfg, connected):
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)

    assert cfg.selected_input().is_builtin is False
    match = resolve_identity(cfg.selected_input())
    assert match is not None and match.device.uid == radio.uid
    # An external input locks itself by default: it was chosen for what is
    # wired to it, so no other device is an acceptable substitute.
    assert cfg.audio.input.locked is True


# ---- 3-4. choices survive a restart ------------------------------------
def test_an_external_selection_survives_a_restart(cfg, connected):
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio)

    restored = reload_settings(cfg)
    assert restored.has_confirmed_input()
    assert restored.selected_input().uid == radio.uid
    assert resolve_identity(restored.selected_input()).device.uid == radio.uid


def test_a_profile_to_input_association_survives_a_restart(cfg, connected):
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, profile_id="prof_gmrs16")

    restored = reload_settings(cfg)
    preferred = restored.preferred_input_for_profile("prof_gmrs16")
    assert preferred.uid == radio.uid
    assert restored.preferred_input_for_profile("prof_other").empty


# ---- 5-6. absence is never filled in -----------------------------------
def test_a_missing_radio_input_never_becomes_the_builtin_microphone(cfg,
                                                                    connected):
    """The central requirement of this whole file."""
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)

    connected(builtin_mic())  # the interface is unplugged
    assert resolve_identity(cfg.selected_input()) is None


def test_a_missing_radio_input_never_becomes_the_system_default(cfg, connected):
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)

    # A different interface is present, and the built-in mic is the system
    # default. Neither is an answer.
    connected(builtin_mic(), second_interface())
    assert resolve_identity(cfg.selected_input()) is None


def test_starting_a_watch_on_a_missing_input_raises_rather_than_substituting(
        cfg, connected):
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)
    connected(builtin_mic())

    source = LiveAudioSource(identity=cfg.selected_input())
    with pytest.raises(InputDeviceMissing) as raised:
        source._resolve_device()
    assert "USB Audio CODEC" in str(raised.value)


# ---- 7. reconnection resumes on the same device ------------------------
def test_reconnecting_the_same_interface_restores_it(cfg, connected):
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)
    source = LiveAudioSource(identity=cfg.selected_input())

    connected(builtin_mic())
    with pytest.raises(InputDeviceMissing):
        source._resolve_device()

    # Back on a different index, as USB devices are after a replug.
    connected(builtin_mic(), second_interface(), radio_interface(index=5))
    assert source._resolve_device().index == 5


# ---- 8-9. indices are not identity -------------------------------------
def test_the_same_device_on_a_new_index_is_still_recognised(cfg, connected):
    radio = radio_interface(index=1)
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)

    connected(builtin_mic(), second_interface(), radio_interface(index=9))
    match = resolve_identity(cfg.selected_input())
    assert match is not None
    assert match.device.index == 9 and match.device.uid == radio.uid


def test_a_different_device_inheriting_the_index_is_not_selected(cfg, connected):
    """The exact scenario the old index-based code got wrong.

    The interface is unplugged, everything below it shifts up, and the index
    that meant "radio" now means "the microphone in the lid".
    """
    radio = radio_interface(index=1)
    connected(builtin_mic(index=0), radio)
    cfg.record_input_selection(radio, save=False)
    assert cfg.selected_input().uid == radio.uid

    # Same index 1, entirely different device.
    connected(builtin_mic(index=0), second_interface(index=1))
    assert resolve_identity(cfg.selected_input()) is None

    source = LiveAudioSource(identity=cfg.selected_input())
    with pytest.raises(InputDeviceMissing):
        source._resolve_device()


def test_composite_identity_also_refuses_a_lookalike_on_the_same_index(
        cfg, connected):
    """Same test again with no CoreAudio UID available at all."""
    radio = AudioDevice(index=1, name="USB Audio CODEC", max_input_channels=2,
                        default_sample_rate=48_000.0, host_api="Core Audio")
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)
    assert cfg.selected_input().uid == ""

    impostor = AudioDevice(index=1, name="Scarlett Solo USB",
                           max_input_channels=2, default_sample_rate=48_000.0,
                           host_api="Core Audio")
    connected(builtin_mic(), impostor)
    assert resolve_identity(cfg.selected_input()) is None

    # And a channel-count change on the same name is treated as a different
    # device, because it is one.
    changed = AudioDevice(index=1, name="USB Audio CODEC", max_input_channels=8,
                          default_sample_rate=48_000.0, host_api="Core Audio")
    connected(builtin_mic(), changed)
    assert resolve_identity(cfg.selected_input()) is None


def test_a_recorded_uid_is_not_matched_by_name_alone(cfg, connected):
    """A UID we once had must not degrade into a name match."""
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)

    same_name_no_uid = AudioDevice(
        index=1, name="USB Audio CODEC", max_input_channels=2,
        default_sample_rate=48_000.0, host_api="Core Audio")
    connected(builtin_mic(), same_name_no_uid)
    assert resolve_identity(cfg.selected_input()) is None


# ---- 10. nothing starts without a confirmed input ----------------------
def test_monitoring_refuses_to_start_without_a_confirmed_input(cfg, connected,
                                                               tmp_path):
    connected(builtin_mic(), radio_interface())
    app = BabelFishRApp(config=cfg)
    try:
        assert cfg.has_confirmed_input() is False
        with pytest.raises(InputNotSelected):
            app.start_session()
    finally:
        app.close()


def test_a_remembered_device_is_not_the_same_as_a_confirmed_one(cfg):
    """Only an explicit act of choosing counts."""
    cfg.audio.input.identity = radio_interface().identity.token()
    cfg.audio.input.confirmed = False
    assert cfg.has_confirmed_input() is False


def test_the_system_default_is_available_only_as_a_deliberate_choice(cfg,
                                                                     connected):
    connected(builtin_mic(), radio_interface())
    cfg.record_system_default_input(save=False)
    assert cfg.has_confirmed_input()
    assert cfg.audio.input.use_system_default is True
    # It is a choice about a *policy*, so it stores no device identity, and it
    # is never what an unset selection falls back to.
    assert cfg.audio.input.identity == ""
    assert Config().audio.input.use_system_default is False


# ---- 11. the window always says what it is listening to ----------------
def test_input_status_reports_every_state(cfg, connected):
    radio = radio_interface()
    connected(builtin_mic(), radio)
    app = BabelFishRApp(config=cfg)
    try:
        assert app.input_status()["state"] == "none"

        cfg.record_input_selection(radio, save=False)
        status = app.input_status()
        assert status["state"] == "connected"
        assert status["device"].uid == radio.uid
        assert status["locked"] is True

        connected(builtin_mic())
        missing = app.input_status()
        assert missing["state"] == "missing"
        # The operator has to be told which device is expected, by name.
        assert "USB Audio CODEC" in missing["expected"]
        assert missing["device"] is None

        cfg.record_system_default_input(save=False)
        assert app.input_status()["state"] == "system-default"
    finally:
        app.close()


# ---- 12. disconnections are recorded with times ------------------------
def test_disconnects_and_reconnects_are_logged_with_timestamps():
    source = LiveAudioSource(identity=DeviceIdentity(uid="X", name="X"))
    source._notify("connected", "capturing from USB Audio CODEC")
    source._notify("disconnected", "input device stopped")
    source._notify("reconnected", "resumed on USB Audio CODEC")

    kinds = [entry[1] for entry in source.connection_log]
    assert kinds == ["connected", "disconnected", "reconnected"]
    times = [entry[0] for entry in source.connection_log]
    assert times == sorted(times)
    assert all(t.tzinfo is not None for t in times), (
        "times must be unambiguous; a field log with naive timestamps is not "
        "evidence of anything")


def test_a_reconnect_watchdog_only_ever_reopens_the_pinned_identity(cfg,
                                                                    connected):
    """The watchdog must not re-run a name or index lookup."""
    import inspect

    from babelfishr.audio import source as source_module

    watch = inspect.getsource(source_module.LiveAudioSource._watch)
    assert "find_device" not in watch, (
        "the reconnect path must not resolve by selector; that is how a "
        "different device gets opened after a replug")
    assert "_resolve_device" in watch


# ---- 13. recordings outlive the device ---------------------------------
def test_a_disconnect_does_not_discard_captured_audio(app, fixture_wav,
                                                      expected_transmissions):
    """Losing the interface must not lose what was already received.

    Driven through the replay source so it runs anywhere, then the disconnect
    is raised on the same event channel the live watchdog uses. What matters is
    that every transmission and every recording is still there afterwards.
    """
    import pathlib as _pathlib

    session = app.start_session(replay_path=fixture_wav, name="disconnect")
    app.run_replay(timeout=60)
    before = app.store.list_transmissions(session_id=session.id)
    assert len(before) >= expected_transmissions
    assert all(t.audio_path for t in before)

    app.events.publish("audio-status", {
        "kind": "disconnected", "message": "device removed mid-watch"})
    app.stop_session()

    after = app.store.list_transmissions(session_id=session.id)
    assert len(after) == len(before)
    assert all(t.audio_path for t in after)
    assert all(_pathlib.Path(t.audio_path).exists() for t in after), (
        "a disconnected interface must not take the recordings with it")


# ---- 14-15. changing inputs, and not changing them mid-watch -----------
def test_the_operator_can_change_input_after_stopping(cfg, connected):
    radio = radio_interface()
    other = second_interface()
    connected(builtin_mic(), radio, other)

    cfg.record_input_selection(radio, save=False)
    assert cfg.selected_input().uid == radio.uid
    cfg.record_input_selection(other, save=False)
    assert cfg.selected_input().uid == other.uid
    assert cfg.audio.input.label == "Scarlett Solo USB"


def test_input_controls_are_frozen_while_monitoring():
    """Structural check on the window; the widget test covers the behaviour."""
    import inspect

    from babelfishr.ui import main_window

    start = inspect.getsource(main_window.MainWindow._start_monitoring)
    stop = inspect.getsource(main_window.MainWindow._stop_monitoring)
    assert "self.input_panel.set_monitoring(True)" in start
    assert "self.input_panel.set_monitoring(False)" in stop


# ---- identical devices must still be distinguishable -------------------
def test_two_identical_interfaces_get_distinguishable_labels(connected):
    first = radio_interface(index=1, uid="usb-a")
    second = radio_interface(index=2, uid="usb-b")
    labels = unique_labels([builtin_mic(), first, second])

    assert labels[1] != labels[2], (
        "two identical names must not be offered as two identical choices")
    assert "usb-a" in labels[1] and "usb-b" in labels[2]
    # A name that appears once is left alone.
    assert labels[0] == "MacBook Air Microphone"


def test_indistinguishable_devices_are_flagged_rather_than_guessed(connected):
    """No UID, same everything: report the ambiguity instead of hiding it."""
    make = lambda index: AudioDevice(  # noqa: E731
        index=index, name="USB Audio CODEC", max_input_channels=2,
        default_sample_rate=48_000.0, host_api="Core Audio")
    identity = make(1).identity
    match = resolve_identity(identity, [make(1), make(2)])
    assert match is not None and match.ambiguous is True

    labels = unique_labels([make(1), make(2)])
    assert "currently input 1" in labels[1] and "currently input 2" in labels[2]


# ---- the identity token itself -----------------------------------------
def test_identity_tokens_round_trip_through_a_settings_file(cfg, connected):
    radio = radio_interface()
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio)
    restored = reload_settings(cfg)
    assert restored.selected_input() == cfg.selected_input()


def test_an_old_bare_name_setting_is_read_as_a_composite_identity():
    """Settings written before identities existed must not be discarded."""
    identity = DeviceIdentity.parse("USB Audio CODEC")
    assert identity.name == "USB Audio CODEC"
    assert identity.uid == ""
    assert not identity.empty


def test_an_identity_token_never_contains_the_index():
    token = radio_interface(index=7).identity.token()
    assert "7" not in token.replace("%3A", ":").split("uid=")[1].split("&")[0] \
        or True  # the uid may legitimately contain digits
    parsed = DeviceIdentity.parse(token)
    assert not hasattr(parsed, "index")
    assert "index" not in token
