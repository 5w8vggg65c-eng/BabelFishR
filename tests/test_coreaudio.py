"""The CoreAudio ctypes layer, driven against a fake framework.

This code reads the device UID and transport type that everything else depends
on for stable identification. It cannot run at all off macOS, and it has never
run on macOS in this project, so the parts that *can* be tested anywhere - the
constants, the structure layout, the failure behaviour and the name-collision
rule - are tested here rather than assumed.

Reading real CoreAudio remains unvalidated. See docs/MACOS_VALIDATION.md 3a.
"""

from __future__ import annotations

import ctypes

import pytest

from babelfishr.audio import coreaudio

pytestmark = pytest.mark.unit


def test_four_character_codes_match_the_apple_constants():
    """Wrong by one byte and every property read returns nothing."""
    assert coreaudio._fourcc("dev#") == 0x64657623   # kAudioHardwarePropertyDevices
    assert coreaudio._fourcc("uid ") == 0x75696420   # kAudioDevicePropertyDeviceUID
    assert coreaudio._fourcc("lnam") == 0x6C6E616D   # kAudioObjectPropertyName
    assert coreaudio._fourcc("slay") == 0x736C6179   # StreamConfiguration
    assert coreaudio._fourcc("tran") == 0x7472616E   # TransportType
    assert coreaudio._fourcc("glob") == 0x676C6F62
    assert coreaudio._fourcc("inpt") == 0x696E7074


def test_the_transport_table_distinguishes_builtin_from_everything_else():
    """This is what decides 'the laptop mic' from 'something you plugged in'."""
    assert coreaudio.TRANSPORTS[coreaudio._fourcc("bltn")] == "builtin"
    assert coreaudio.TRANSPORTS[coreaudio._fourcc("usb ")] == "usb"
    assert coreaudio.TRANSPORTS[coreaudio._fourcc("blue")] == "bluetooth"
    assert coreaudio.BUILTIN_TRANSPORTS == frozenset({"builtin"})
    for code, name in coreaudio.TRANSPORTS.items():
        if name != "builtin":
            assert name not in coreaudio.BUILTIN_TRANSPORTS


def test_the_audio_buffer_list_layout_matches_the_c_struct():
    """Padding here would make every channel count wrong."""
    assert ctypes.sizeof(coreaudio._AudioBuffer) == 16
    assert coreaudio._AudioBufferList.mNumberBuffers.offset == 0
    assert coreaudio._AudioBufferList.mBuffers.offset == 8


def test_input_channels_sums_every_buffer(monkeypatch):
    """A two-buffer mono-each device is a two-channel input, not a one."""
    def fake_bytes(core_audio, object_id, address):
        buffers = 2
        raw = bytearray(8 + buffers * 16)
        raw[0:4] = buffers.to_bytes(4, "little")
        raw[8:12] = (1).to_bytes(4, "little")
        raw[24:28] = (1).to_bytes(4, "little")
        return bytes(raw)

    monkeypatch.setattr(coreaudio, "_property_bytes", fake_bytes)
    monkeypatch.setattr("sys.byteorder", "little", raising=False)
    assert coreaudio._input_channels(None, 42) == 2


def test_input_channels_is_zero_when_the_property_cannot_be_read(monkeypatch):
    monkeypatch.setattr(coreaudio, "_property_bytes",
                        lambda *args: None)
    assert coreaudio._input_channels(None, 42) == 0


def test_everything_degrades_to_nothing_off_macos():
    """No exception may ever escape this module into the audio path."""
    if coreaudio.available():
        pytest.skip("this host has CoreAudio; the fallback cannot be observed")
    assert coreaudio.list_input_devices() == []
    assert coreaudio.input_index() == {}


def test_a_failure_inside_enumeration_returns_an_empty_list(monkeypatch):
    monkeypatch.setattr(coreaudio, "_libraries",
                        lambda: (object(), object()))
    monkeypatch.setattr(coreaudio, "_property_size",
                        lambda *args: 1 / 0)  # raises
    assert coreaudio.list_input_devices() == []


def test_duplicate_names_are_dropped_rather_than_guessed_at(monkeypatch):
    """Two identical interfaces cannot be told apart by name.

    Picking one would be exactly the silent substitution this application must
    never make, so neither is offered a UID and both fall back to composite
    identification - which reports the ambiguity.
    """
    devices = [
        coreaudio.CoreAudioDevice("uid-a", "USB Audio CODEC", 2, "usb"),
        coreaudio.CoreAudioDevice("uid-b", "USB Audio CODEC", 2, "usb"),
        coreaudio.CoreAudioDevice("uid-mic", "MacBook Air Microphone", 1,
                                  "builtin"),
    ]
    monkeypatch.setattr(coreaudio, "list_input_devices", lambda: devices)
    index = coreaudio.input_index()
    assert "USB Audio CODEC" not in index
    assert index["MacBook Air Microphone"].uid == "uid-mic"


def test_a_coreaudio_row_is_ignored_when_the_channel_count_disagrees(
        monkeypatch):
    """Matching PortAudio to CoreAudio by name alone can match the wrong row."""
    from babelfishr.audio import devices as devices_module

    class FakeSounddevice:
        @staticmethod
        def query_devices():
            return [{"name": "USB Audio CODEC", "max_input_channels": 2,
                     "default_samplerate": 48000.0, "hostapi": 0}]

        @staticmethod
        def query_hostapis():
            return [{"name": "Core Audio"}]

        class default:
            device = (0, 0)

    monkeypatch.setattr(devices_module, "_sounddevice",
                        lambda: FakeSounddevice)
    monkeypatch.setattr(devices_module.coreaudio, "input_index", lambda: {
        "USB Audio CODEC": coreaudio.CoreAudioDevice(
            "uid-wrong", "USB Audio CODEC", 8, "usb")})

    device = devices_module.list_input_devices()[0]
    assert device.uid == "", "a channel-count mismatch means the wrong device"
    assert device.transport == ""

    monkeypatch.setattr(devices_module.coreaudio, "input_index", lambda: {
        "USB Audio CODEC": coreaudio.CoreAudioDevice(
            "uid-right", "USB Audio CODEC", 2, "usb")})
    device = devices_module.list_input_devices()[0]
    assert device.uid == "uid-right" and device.transport == "usb"
    assert device.is_builtin is False


# ---- the probe ---------------------------------------------------------
def test_the_probe_is_honest_about_not_being_applicable_off_macos():
    report = coreaudio.probe()
    if coreaudio.available():
        pytest.skip("this host has CoreAudio; covered by the macOS test below")
    assert report["applicable"] is False
    assert report["ok"] is True, (
        "not being on a Mac is not a failure of this code")
    assert report["device_count"] == 0
    assert "not macOS" in report["device_list_query"]


def test_the_probe_reports_a_framework_that_will_not_load(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(coreaudio, "_libraries", lambda: None)
    report = coreaudio.probe()
    assert report["ok"] is False
    assert report["frameworks_loaded"] is False
    assert "could not be loaded" in report["errors"][0]


def test_the_probe_fails_when_the_property_selector_is_wrong(monkeypatch):
    """A non-zero OSStatus means the selector or the struct layout is wrong."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(coreaudio, "_libraries", lambda: (object(), object()))
    monkeypatch.setattr(coreaudio, "_property_size", lambda *args: None)
    report = coreaudio.probe()
    assert report["ok"] is False
    assert "failed" in report["device_list_query"]
    assert "AudioObjectGetPropertyDataSize" in report["errors"][0]


def test_the_probe_rejects_an_incoherent_device(monkeypatch):
    """Bad struct reading would silently corrupt device identification."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(coreaudio, "_libraries", lambda: (object(), object()))
    monkeypatch.setattr(coreaudio, "_property_size", lambda *args: 8)
    monkeypatch.setattr(coreaudio, "list_input_devices", lambda: [
        coreaudio.CoreAudioDevice("uid", "", 2, "usb")])
    report = coreaudio.probe()
    assert report["ok"] is False
    assert "has no name" in report["errors"][0]


def test_the_formatted_probe_never_implies_hardware_was_tested(monkeypatch):
    monkeypatch.setattr(coreaudio, "probe", lambda: {
        "platform": "darwin", "applicable": True, "frameworks_loaded": True,
        "device_list_query": "ok", "object_count": 0, "device_count": 0,
        "devices": [], "errors": [], "ok": True})
    text = coreaudio.format_probe()
    assert "no audio hardware" in text
    assert "not evidence that audio capture works" in text


# ---- a CoreAudio call must never be able to hang the application ------
def test_a_blocked_coreaudio_call_gives_up_instead_of_hanging():
    """Every call here goes through the HAL to coreaudiod over Mach IPC.

    If that daemon is absent, wedged or still starting - the normal state of a
    machine with no audio hardware - a call can block rather than fail. Device
    enumeration runs on the GUI thread, so a block there freezes the window
    an operator is watching a radio through.
    """
    import time

    started = time.monotonic()
    with pytest.raises(coreaudio.CoreAudioTimeout):
        coreaudio._with_timeout(lambda: time.sleep(30), timeout=0.2,
                                what="test call")
    assert time.monotonic() - started < 5.0


def test_enumeration_falls_back_to_nothing_when_coreaudio_will_not_answer(
        monkeypatch):
    """Losing the UID is a handled degradation. Hanging is not."""
    def blocked():
        import time

        time.sleep(30)

    monkeypatch.setattr(coreaudio, "_list_input_devices", blocked)
    monkeypatch.setattr(coreaudio, "CALL_TIMEOUT_SECONDS", 0.2)
    assert coreaudio.list_input_devices() == []
    assert coreaudio.input_index() == {}


def test_the_probe_reports_a_timeout_without_calling_it_a_failure(monkeypatch):
    """A daemon that will not answer is not a defect in this code.

    Failing the build on it would make a green build depend on the mood of a
    daemon on a machine with no sound card - so it is reported, loudly, and
    the consequence is spelled out.
    """
    import time

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(coreaudio, "_libraries", lambda: (object(), object()))
    monkeypatch.setattr(coreaudio, "_property_size",
                        lambda *a: time.sleep(30))
    monkeypatch.setattr(coreaudio, "CALL_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    report = coreaudio.probe()
    assert time.monotonic() - started < 5.0
    assert report["timed_out"] is True
    assert report["ok"] is True
    assert "timed out" in report["device_list_query"]
    text = coreaudio.format_probe(report)
    assert "cannot tell two identical interfaces apart" in text


@pytest.mark.skipif(not coreaudio.available(),
                    reason="needs a real macOS host with CoreAudio")
def test_the_real_coreaudio_abi_works_on_this_machine():
    """Runs on the macOS build runner, and on any developer's Mac.

    This is the only test in the project that touches the real CoreAudio ABI.
    It proves the frameworks load and the selectors and property-address layout
    are right. It deliberately does NOT require any device to exist: a hosted
    runner has no audio hardware, and asserting otherwise would turn an honest
    'nothing is plugged in' into a red build.
    """
    report = coreaudio.probe()
    assert report["applicable"] is True
    assert report["frameworks_loaded"] is True, coreaudio.format_probe(report)
    assert report["errors"] == [], coreaudio.format_probe(report)
    assert report["ok"] is True
    if report["timed_out"]:
        pytest.skip("coreaudiod did not answer on this host; the ABI could "
                    "not be exercised. See the probe output in the build log.")
    assert report["device_list_query"] == "ok", coreaudio.format_probe(report)

    # Whatever devices do exist must be coherent enough to identify.
    for device in coreaudio.list_input_devices():
        assert device.name
        assert device.input_channels > 0
        assert device.transport
