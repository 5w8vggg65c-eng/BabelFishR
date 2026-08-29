"""Read stable device identities straight from CoreAudio.

PortAudio gives us an index and a name. Neither is stable: the index shifts
whenever anything is plugged in or removed, and two identical USB interfaces
have identical names. CoreAudio, underneath it, gives every device a UID that
survives replugging and reboots, and a transport type that says whether a
device is the built-in microphone or something the operator connected.

That distinction is the whole point of this module. An operator watching a
radio must never be handed the laptop's own microphone because a USB interface
came back on a different index.

Everything here is optional and defensive: on any non-macOS host, or if the
framework cannot be loaded or a property cannot be read, the callers fall back
to matching on name plus host API plus channel count. No exception from this
module ever reaches the audio path.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import dataclasses
import logging
import sys
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# --- CoreAudio constants (four-character codes) --------------------------
_SYSTEM_OBJECT = 1


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


_PROP_DEVICES = _fourcc("dev#")           # kAudioHardwarePropertyDevices
_PROP_DEVICE_UID = _fourcc("uid ")        # kAudioDevicePropertyDeviceUID
_PROP_NAME = _fourcc("lnam")              # kAudioObjectPropertyName
_PROP_STREAM_CONFIG = _fourcc("slay")     # kAudioDevicePropertyStreamConfiguration
_PROP_TRANSPORT = _fourcc("tran")         # kAudioDevicePropertyTransportType
_SCOPE_GLOBAL = _fourcc("glob")
_SCOPE_INPUT = _fourcc("inpt")
_ELEMENT_MAIN = 0

#: kAudioDeviceTransportType* -> the word we show the operator.
TRANSPORTS: Dict[int, str] = {
    _fourcc("bltn"): "builtin",
    _fourcc("usb "): "usb",
    _fourcc("blue"): "bluetooth",
    _fourcc("hdmi"): "hdmi",
    _fourcc("thun"): "thunderbolt",
    _fourcc("airp"): "airplay",
    _fourcc("virt"): "virtual",
    _fourcc("aggr"): "aggregate",
    _fourcc("pci "): "pci",
    _fourcc("fire"): "firewire",
    _fourcc("disp"): "displayport",
    _fourcc("unkn"): "unknown",
}

#: Transports that are physically part of the Mac, not something plugged in.
BUILTIN_TRANSPORTS = frozenset({"builtin"})

_CF_STRING_ENCODING_UTF8 = 0x08000100


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class _AudioBufferList(ctypes.Structure):
    _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                ("mBuffers", _AudioBuffer * 1)]


@dataclasses.dataclass(frozen=True)
class CoreAudioDevice:
    """One CoreAudio device that can capture."""

    uid: str
    name: str
    input_channels: int
    transport: str

    @property
    def is_builtin(self) -> bool:
        return self.transport in BUILTIN_TRANSPORTS


_LIBRARIES = None


def _libraries():
    """(CoreAudio, CoreFoundation) or None. Loaded once, never raises."""
    global _LIBRARIES
    if _LIBRARIES is not None:
        return _LIBRARIES or None
    if sys.platform != "darwin":
        _LIBRARIES = False
        return None
    try:
        core_audio = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        core_audio.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        core_audio.AudioObjectGetPropertyData.restype = ctypes.c_int32
        core_foundation.CFStringGetCString.restype = ctypes.c_bool
        core_foundation.CFRelease.restype = None
    except Exception as exc:  # noqa: BLE001 - any failure means "no CoreAudio"
        log.debug("CoreAudio unavailable: %s", exc)
        _LIBRARIES = False
        return None
    _LIBRARIES = (core_audio, core_foundation)
    return _LIBRARIES


def available() -> bool:
    return _libraries() is not None


def _address(selector: int, scope: int = _SCOPE_GLOBAL):
    return _AudioObjectPropertyAddress(selector, scope, _ELEMENT_MAIN)


def _property_size(core_audio, object_id: int, address) -> Optional[int]:
    size = ctypes.c_uint32(0)
    status = core_audio.AudioObjectGetPropertyDataSize(
        ctypes.c_uint32(object_id), ctypes.byref(address),
        ctypes.c_uint32(0), None, ctypes.byref(size))
    return None if status != 0 else int(size.value)


def _property_bytes(core_audio, object_id: int, address) -> Optional[bytes]:
    size = _property_size(core_audio, object_id, address)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    length = ctypes.c_uint32(size)
    status = core_audio.AudioObjectGetPropertyData(
        ctypes.c_uint32(object_id), ctypes.byref(address),
        ctypes.c_uint32(0), None, ctypes.byref(length), buffer)
    return None if status != 0 else buffer.raw[:length.value]


def _cfstring_property(core_audio, core_foundation, object_id: int,
                       selector: int) -> str:
    """Read a CFStringRef property and convert it, releasing the reference."""
    address = _address(selector)
    reference = ctypes.c_void_p()
    length = ctypes.c_uint32(ctypes.sizeof(reference))
    status = core_audio.AudioObjectGetPropertyData(
        ctypes.c_uint32(object_id), ctypes.byref(address),
        ctypes.c_uint32(0), None, ctypes.byref(length), ctypes.byref(reference))
    if status != 0 or not reference.value:
        return ""
    try:
        buffer = ctypes.create_string_buffer(1024)
        ok = core_foundation.CFStringGetCString(
            reference, buffer, ctypes.c_long(len(buffer)),
            ctypes.c_uint32(_CF_STRING_ENCODING_UTF8))
        return buffer.value.decode("utf-8", "replace") if ok else ""
    finally:
        try:
            core_foundation.CFRelease(reference)
        except Exception:  # noqa: BLE001
            pass


def _input_channels(core_audio, object_id: int) -> int:
    raw = _property_bytes(core_audio, object_id,
                          _address(_PROP_STREAM_CONFIG, _SCOPE_INPUT))
    if not raw or len(raw) < ctypes.sizeof(ctypes.c_uint32):
        return 0
    count = int.from_bytes(raw[:4], sys.byteorder)
    if count <= 0:
        return 0
    buffers_offset = _AudioBufferList.mBuffers.offset
    buffer_size = ctypes.sizeof(_AudioBuffer)
    channels = 0
    for i in range(count):
        start = buffers_offset + i * buffer_size
        if start + 4 > len(raw):
            break
        channels += int.from_bytes(raw[start:start + 4], sys.byteorder)
    return channels


def _transport(core_audio, object_id: int) -> str:
    raw = _property_bytes(core_audio, object_id, _address(_PROP_TRANSPORT))
    if not raw or len(raw) < 4:
        return ""
    value = int.from_bytes(raw[:4], sys.byteorder)
    return TRANSPORTS.get(value, "")


def list_input_devices() -> List[CoreAudioDevice]:
    """Every CoreAudio device with at least one input channel.

    Returns an empty list on any host or any failure, so callers can treat
    "CoreAudio told us nothing" and "we are not on a Mac" identically.
    """
    libraries = _libraries()
    if libraries is None:
        return []
    core_audio, core_foundation = libraries
    try:
        address = _address(_PROP_DEVICES)
        size = _property_size(core_audio, _SYSTEM_OBJECT, address)
        if not size:
            return []
        count = size // ctypes.sizeof(ctypes.c_uint32)
        ids = (ctypes.c_uint32 * count)()
        length = ctypes.c_uint32(size)
        status = core_audio.AudioObjectGetPropertyData(
            ctypes.c_uint32(_SYSTEM_OBJECT), ctypes.byref(address),
            ctypes.c_uint32(0), None, ctypes.byref(length), ids)
        if status != 0:
            return []

        devices: List[CoreAudioDevice] = []
        for object_id in ids:
            channels = _input_channels(core_audio, int(object_id))
            if channels <= 0:
                continue
            uid = _cfstring_property(core_audio, core_foundation,
                                     int(object_id), _PROP_DEVICE_UID)
            name = _cfstring_property(core_audio, core_foundation,
                                      int(object_id), _PROP_NAME)
            devices.append(CoreAudioDevice(
                uid=uid, name=name, input_channels=channels,
                transport=_transport(core_audio, int(object_id)) or "unknown"))
        return devices
    except Exception as exc:  # noqa: BLE001 - never break the audio path
        log.debug("could not enumerate CoreAudio devices: %s", exc)
        return []


def input_index() -> Dict[str, CoreAudioDevice]:
    """CoreAudio inputs keyed by name, for matching against PortAudio.

    A name that appears more than once is dropped rather than guessed at: two
    identical USB interfaces cannot be told apart this way, and picking one
    would be exactly the silent substitution this application must never make.
    """
    by_name: Dict[str, List[CoreAudioDevice]] = {}
    for device in list_input_devices():
        by_name.setdefault(device.name, []).append(device)
    return {name: found[0] for name, found in by_name.items() if len(found) == 1}


def probe() -> Dict[str, object]:
    """Exercise the CoreAudio ABI and report exactly what happened.

    Written to be run on a real Mac - including inside the shipped bundle, via
    ``BabelFishR --selftest-coreaudio`` - because everything else in this
    module is verified against a fake framework and that proves only that the
    Python is self-consistent.

    What it can establish: the frameworks load, the property selectors and the
    AudioObjectPropertyAddress layout are right (a size query for the device
    list returns status 0), and whatever devices the machine does have come
    back with a coherent shape.

    What it cannot establish: that audio flows. A hosted runner has no audio
    hardware, so ``device_count`` there is expected to be 0 and that is not a
    failure of this code. Only a Mac with an interface plugged into it can
    show that a device is real, and only listening to the result can show that
    the right one was opened.
    """
    report: Dict[str, object] = {
        "platform": sys.platform,
        "applicable": sys.platform == "darwin",
        "frameworks_loaded": False,
        "device_list_query": "not attempted",
        "device_count": 0,
        "devices": [],
        "errors": [],
        "ok": True,
    }
    if sys.platform != "darwin":
        report["device_list_query"] = "skipped: not macOS"
        return report

    libraries = _libraries()
    if libraries is None:
        report["errors"] = ["CoreAudio/CoreFoundation could not be loaded"]
        report["ok"] = False
        return report
    report["frameworks_loaded"] = True
    core_audio, _ = libraries

    try:
        address = _address(_PROP_DEVICES)
        size = _property_size(core_audio, _SYSTEM_OBJECT, address)
        if size is None:
            report["device_list_query"] = "failed: non-zero OSStatus"
            report["errors"] = [
                "AudioObjectGetPropertyDataSize(kAudioHardwarePropertyDevices) "
                "returned a non-zero status; the selector or the property "
                "address layout is wrong"]
            report["ok"] = False
            return report
        report["device_list_query"] = "ok"
        report["object_count"] = size // ctypes.sizeof(ctypes.c_uint32)
    except Exception as exc:  # noqa: BLE001
        report["device_list_query"] = f"raised: {exc}"
        report["errors"] = [f"{type(exc).__name__}: {exc}"]
        report["ok"] = False
        return report

    devices = list_input_devices()
    report["device_count"] = len(devices)
    report["devices"] = [
        {"uid": d.uid, "name": d.name, "channels": d.input_channels,
         "transport": d.transport, "builtin": d.is_builtin} for d in devices]

    # Anything the machine did report has to be coherent, or the struct
    # reading is wrong in a way that would silently corrupt identification.
    problems = []
    for device in devices:
        if device.input_channels <= 0:
            problems.append(f"{device.name!r} reported as an input with "
                            f"{device.input_channels} channels")
        if not device.name:
            problems.append(f"a device with uid {device.uid!r} has no name")
        if device.transport not in set(TRANSPORTS.values()) | {"unknown"}:
            problems.append(f"{device.name!r} has unrecognised transport "
                            f"{device.transport!r}")
    if problems:
        report["errors"] = problems
        report["ok"] = False
    return report


def format_probe(report: Optional[Dict[str, object]] = None) -> str:
    """The probe as plain text, for a build log or a diagnostic report."""
    report = probe() if report is None else report
    lines = [
        "CoreAudio probe",
        f"  platform          : {report['platform']}",
        f"  applicable        : {report['applicable']}",
        f"  frameworks loaded : {report['frameworks_loaded']}",
        f"  device list query : {report['device_list_query']}",
        f"  audio objects     : {report.get('object_count', 'n/a')}",
        f"  input devices     : {report['device_count']}",
    ]
    for device in report["devices"]:  # type: ignore[union-attr]
        lines.append(
            f"     {device['name']!r} uid={device['uid']!r} "
            f"{device['channels']} ch {device['transport']}"
            f"{' (built-in)' if device['builtin'] else ''}")
    for error in report["errors"]:  # type: ignore[union-attr]
        lines.append(f"  ERROR: {error}")
    if report["applicable"] and not report["device_count"]:
        lines.append("  note: no input devices. Expected on a hosted runner, "
                     "which has no audio hardware. This is not evidence that "
                     "audio capture works.")
    lines.append(f"  result            : {'ok' if report['ok'] else 'FAILED'}")
    return "\n".join(lines)


__all__ = ["CoreAudioDevice", "available", "list_input_devices", "input_index",
           "format_probe", "probe", "TRANSPORTS", "BUILTIN_TRANSPORTS"]
