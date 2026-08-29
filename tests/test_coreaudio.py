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
