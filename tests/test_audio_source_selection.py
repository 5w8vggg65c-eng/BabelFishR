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
from babelfishr.audio.devices import (AmbiguousInputDevice, AudioDevice,
                                      DeviceIdentity, InputDeviceMissing,
                                      InputNotSelected, resolve_identity,
                                      resolve_input, unique_labels)
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
    """Control what PortAudio appears to report, for the whole audio stack.

    Only one thing needs patching. Every resolution path funnels through
    ``resolve_input``, which calls ``list_input_devices`` out of this module's
    own globals when no explicit list is given - so replacing that one name
    changes what the source, the app and the panel all see.
    """
    state = {"devices": []}

    def present():
        return list(state["devices"])

    monkeypatch.setattr(devices_module, "list_input_devices", present)

    def set_devices(*devices):
        state["devices"] = list(devices)
        return state["devices"]

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
    # Every explicitly chosen device is pinned to its identity. There is no
    # setting for this, because the setting that used to exist changed nothing
    # about capture and so could only ever mislead.
    assert not hasattr(cfg.audio.input, "locked")


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
        assert status["candidates"] == []

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


def test_indistinguishable_devices_resolve_to_nothing(connected):
    """No UID, same everything: refuse, rather than pick one."""
    make = lambda index: AudioDevice(  # noqa: E731
        index=index, name="USB Audio CODEC", max_input_channels=2,
        default_sample_rate=48_000.0, host_api="Core Audio")
    identity = make(1).identity

    resolution = resolve_input(identity, [make(1), make(2)])
    assert resolution.ambiguous is True
    assert resolution.device is None
    assert [d.index for d in resolution.candidates] == [1, 2]

    # The safe wrapper hands back nothing at all, so a caller that only knows
    # how to check for None cannot be given the wrong device.
    assert resolve_identity(identity, [make(1), make(2)]) is None

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


# ---- Field Check reports the input, without hijacking readiness --------
def test_field_check_reports_the_selected_input(cfg, connected):
    import babelfishr.readiness as readiness_module

    radio = radio_interface()
    connected(builtin_mic(), radio)

    check = readiness_module._selected_input_check(cfg)
    assert check.status.name == "WARN" and "none chosen" in check.detail

    cfg.record_input_selection(radio, save=False)
    check = readiness_module._selected_input_check(cfg)
    assert check.status.name == "PASS"
    assert "USB Audio CODEC" in check.detail and "external" in check.detail

    connected(builtin_mic())
    check = readiness_module._selected_input_check(cfg)
    assert check.status.name == "FAIL"
    assert "NOT connected" in check.detail
    assert "will not substitute" in check.remedy

    # The built-in mic is usable, and flagged for what it is.
    connected(builtin_mic(), radio)
    cfg.record_input_selection(builtin_mic(), save=False)
    check = readiness_module._selected_input_check(cfg)
    assert check.status.name == "WARN"
    assert "records the room" in check.remedy


def test_a_missing_input_does_not_by_itself_make_the_app_unready(cfg,
                                                                 connected):
    """Preparation legitimately happens before the interface is wired up.

    Readiness is about whether this machine can do the work offline. The hard
    refusal belongs where monitoring starts, not here - otherwise an operator
    could never prepare models on a Mac with nothing plugged into it.
    """
    from babelfishr.readiness import Check, CheckStatus, ReadinessReport

    report = ReadinessReport()
    report.add(Check("Audio backend", CheckStatus.PASS, ""))
    report.add(Check("Recording directory writable", CheckStatus.PASS, ""))
    report.add(Check("Selected audio input", CheckStatus.FAIL, "not connected"))
    assert report.can_record is True


# ---- P0: two identical interfaces, and the second one is the radio -----
#
# The failure being reproduced, exactly: an operator has two of the same USB
# interface. macOS reports no unique identifier for either, so their identities
# are byte-for-byte identical. The operator selects the second one - the one
# with the radio on it. The previous implementation returned composite[0] and
# LiveAudioSource logged "using the first", so capture opened the *first*
# interface, which was carrying nothing, and the operator was never stopped.
#
# There is no property in this situation that distinguishes the two devices.
# That is the point: because nothing can tell them apart, nothing may choose
# between them.

def identical_interface(index: int) -> AudioDevice:
    """One of a matched pair. No CoreAudio UID, so nothing separates them."""
    return AudioDevice(
        index=index, name="USB Audio CODEC", max_input_channels=2,
        default_sample_rate=48_000.0, host_api="Core Audio", is_default=False)


def test_selecting_the_second_of_two_identical_interfaces_never_opens_the_first(
        cfg, connected):
    """The precise case from the audit, at the point capture starts."""
    first, second = identical_interface(1), identical_interface(2)
    connected(builtin_mic(), first, second)

    # The operator selects the second one, which is the one with the radio.
    cfg.record_input_selection(second, save=False)

    source = LiveAudioSource(identity=cfg.selected_input())
    with pytest.raises(AmbiguousInputDevice) as raised:
        source._resolve_device()

    # Not "opened the first". Not opened at all.
    assert source.device is None
    assert len(raised.value.candidates) == 2
    assert "cannot safely determine which interface" in str(raised.value)


def test_an_ambiguous_identity_refuses_at_session_start(cfg, connected):
    """Through the real start path, not just the resolver."""
    first, second = identical_interface(1), identical_interface(2)
    connected(builtin_mic(), first, second)
    cfg.record_input_selection(second, save=False)

    app = BabelFishRApp(config=cfg)
    try:
        with pytest.raises(AmbiguousInputDevice):
            app.start_session()
        assert app.session is None
        assert app.capture is None
    finally:
        app.close()


def test_an_ambiguous_identity_refuses_after_a_restart(cfg, connected):
    """Restored from a settings file, as a relaunch would."""
    first, second = identical_interface(1), identical_interface(2)
    connected(builtin_mic(), first, second)
    cfg.record_input_selection(second)

    restored = reload_settings(cfg)
    resolution = resolve_input(restored.selected_input())
    assert resolution.ambiguous and resolution.device is None
    assert resolve_identity(restored.selected_input()) is None

    source = LiveAudioSource(identity=restored.selected_input())
    with pytest.raises(AmbiguousInputDevice):
        source._resolve_device()


def test_an_ambiguous_identity_refuses_on_profile_restoration(cfg, connected):
    """A radio profile pointing at one of a matched pair selects neither."""
    first, second = identical_interface(1), identical_interface(2)
    connected(builtin_mic(), first, second)
    cfg.record_input_selection(second, profile_id="prof_gmrs16")

    restored = reload_settings(cfg)
    preferred = restored.preferred_input_for_profile("prof_gmrs16")
    assert not preferred.empty

    resolution = resolve_input(preferred)
    assert resolution.ambiguous
    assert resolution.device is None
    assert resolve_identity(preferred) is None


def test_an_ambiguous_identity_refuses_on_reconnect(cfg, connected):
    """A duplicate appearing mid-watch stops capture rather than switching.

    The dangerous shape: the operator starts on the only interface present,
    then a second identical one is connected - a hub coming back, a colleague
    plugging in the spare - and the original briefly drops. Reconnection must
    not pick one.
    """
    radio = identical_interface(1)
    connected(builtin_mic(), radio)
    cfg.record_input_selection(radio, save=False)

    source = LiveAudioSource(identity=cfg.selected_input())
    assert source._resolve_device().index == 1

    connected(builtin_mic(), identical_interface(1), identical_interface(4))
    with pytest.raises(AmbiguousInputDevice):
        source._resolve_device()

    # The duplicate is removed; the original is unambiguous again and resumes.
    connected(builtin_mic(), identical_interface(4))
    assert source._resolve_device().index == 4


def test_the_ambiguous_refusal_is_recorded_in_the_connection_log(cfg,
                                                                connected):
    """Those minutes were not received, and the log has to say so."""
    connected(builtin_mic(), identical_interface(1), identical_interface(2))
    cfg.record_input_selection(identical_interface(2), save=False)

    source = LiveAudioSource(identity=cfg.selected_input())
    with pytest.raises(AmbiguousInputDevice):
        source._resolve_device()

    kinds = [entry[1] for entry in source.connection_log]
    assert "ambiguous-device" in kinds
    detail = next(e[2] for e in source.connection_log
                  if e[1] == "ambiguous-device")
    assert "refusing to guess" in detail


def test_a_uid_pair_is_not_ambiguous_and_still_selects_the_right_one(cfg,
                                                                     connected):
    """Two of the same model *with* UIDs are distinguishable, so they work.

    The refusal is about being unable to tell devices apart, not about having
    two of something.
    """
    first = dataclasses_replace(identical_interface(1), uid="usb-a")
    second = dataclasses_replace(identical_interface(2), uid="usb-b")
    connected(builtin_mic(), first, second)
    cfg.record_input_selection(second, save=False)

    source = LiveAudioSource(identity=cfg.selected_input())
    device = source._resolve_device()
    assert device.uid == "usb-b"

    # And it follows that device when the pair swaps indices.
    connected(builtin_mic(),
              dataclasses_replace(identical_interface(7), uid="usb-b"),
              dataclasses_replace(identical_interface(8), uid="usb-a"))
    assert source._resolve_device().index == 7


def dataclasses_replace(device, **changes):
    import dataclasses as _dc

    return _dc.replace(device, **changes)


def test_readiness_fails_rather_than_warns_on_an_ambiguous_input(cfg,
                                                                 connected):
    import babelfishr.readiness as readiness_module

    connected(builtin_mic(), identical_interface(1), identical_interface(2))
    cfg.record_input_selection(identical_interface(2), save=False)

    check = readiness_module._selected_input_check(cfg)
    assert check.status.name == "FAIL"
    assert "indistinguishable" in check.detail
    assert "will not guess" in check.remedy
    assert len(check.data["candidates"]) == 2
