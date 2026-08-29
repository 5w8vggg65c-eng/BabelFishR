"""Audio input device discovery and stable device identity.

Wraps ``sounddevice`` (PortAudio) but never requires it at import time: the
device list, the CLI and the tests must all work on a machine with no audio
backend at all, which is exactly the situation in CI.

The important thing this module does is refuse to identify a device by its
PortAudio index. Indices are positions in a list that is rebuilt every time
anything is plugged in, unplugged or woken up, so the index that meant "USB
Audio CODEC" this morning can mean "MacBook Air Microphone" this afternoon.

A field operator watching a radio must never be handed the laptop's own
microphone because a USB interface came back on a different index. So a
selection the operator makes is persisted as a :class:`DeviceIdentity` - the
CoreAudio UID where macOS will give us one, otherwise the exact name plus host
API plus channel count plus transport - and restored with
:func:`resolve_identity`, which returns nothing at all rather than something
else.
"""

from __future__ import annotations

import dataclasses
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

from . import coreaudio

log = logging.getLogger(__name__)

#: Names macOS gives the microphone built into the machine. Used only as a
#: fallback: CoreAudio's transport type is authoritative when we can read it.
BUILTIN_NAME_HINTS = (
    "macbook pro microphone", "macbook air microphone", "macbook microphone",
    "built-in microphone", "built-in input", "internal microphone",
    "imac microphone", "mac mini microphone", "mac studio microphone",
    "display audio",
)

_IDENTITY_SCHEME = "babelfishr-input"
_IDENTITY_VERSION = "1"


class AudioBackendUnavailable(RuntimeError):
    """Raised when a real capture is attempted without a working PortAudio."""


class InputDeviceMissing(AudioBackendUnavailable):
    """The device the operator chose is not present.

    Deliberately its own exception type: everything above this must be able to
    tell "the radio interface is unplugged" apart from "there is no audio
    backend", because the first has a specific and recoverable answer and the
    second does not.
    """

    def __init__(self, identity: "DeviceIdentity", message: str = ""):
        self.identity = identity
        super().__init__(message or (
            f"the selected audio input is not connected: {identity.describe()}"))


@dataclasses.dataclass(frozen=True)
class DeviceIdentity:
    """A way to name one audio input that survives replugging and rebooting.

    ``uid`` is the CoreAudio device UID and is decisive on its own. When it is
    empty - a non-Mac host, or a device CoreAudio would not tell us about - the
    remaining fields are matched together, because none of them is sufficient
    alone. The PortAudio index is deliberately not part of this at all.
    """

    uid: str = ""
    name: str = ""
    host_api: str = ""
    channels: int = 0
    transport: str = ""
    label: str = ""
    """What the operator saw when they chose it. Only ever used in messages."""

    @property
    def empty(self) -> bool:
        return not (self.uid or self.name)

    @property
    def is_builtin(self) -> bool:
        if self.transport:
            return self.transport in coreaudio.BUILTIN_TRANSPORTS
        return _looks_builtin(self.name)

    @property
    def basis(self) -> str:
        """Which of the two identification schemes this identity relies on."""
        return "coreaudio-uid" if self.uid else "composite"

    def token(self) -> str:
        """A single string that can live in a settings file."""
        if self.empty:
            return ""
        fields = [("v", _IDENTITY_VERSION)]
        for key, value in (("uid", self.uid), ("name", self.name),
                           ("api", self.host_api), ("transport", self.transport),
                           ("label", self.label)):
            if value:
                fields.append((key, value))
        if self.channels:
            fields.append(("ch", str(self.channels)))
        return f"{_IDENTITY_SCHEME}:?" + urllib.parse.urlencode(fields)

    @classmethod
    def parse(cls, token: Optional[str]) -> "DeviceIdentity":
        """Read a token back. Anything unrecognisable yields an empty identity."""
        text = (token or "").strip()
        if not text:
            return cls()
        if not text.startswith(f"{_IDENTITY_SCHEME}:"):
            # A bare name from an older settings file. Honour it as a composite
            # identity rather than discarding the operator's choice.
            return cls(name=text, label=text)
        query = text.split("?", 1)[1] if "?" in text else ""
        fields = dict(urllib.parse.parse_qsl(query, keep_blank_values=False))
        try:
            channels = int(fields.get("ch", "0"))
        except ValueError:
            channels = 0
        return cls(uid=fields.get("uid", ""), name=fields.get("name", ""),
                   host_api=fields.get("api", ""), channels=channels,
                   transport=fields.get("transport", ""),
                   label=fields.get("label", ""))

    def describe(self) -> str:
        parts = [self.label or self.name or "(unnamed input)"]
        details = []
        if self.host_api:
            details.append(self.host_api)
        if self.channels:
            details.append(f"{self.channels} ch")
        if self.transport and self.transport != "unknown":
            details.append(self.transport)
        if details:
            parts.append("(" + ", ".join(details) + ")")
        if self.uid:
            parts.append(f"[uid {self.uid}]")
        return " ".join(parts)

    def matches(self, device: "AudioDevice") -> str:
        """``"uid"``, ``"composite"`` or ``""``. Never matches on index."""
        if self.uid and device.uid:
            return "uid" if self.uid == device.uid else ""
        if self.uid and not device.uid:
            # We recorded a UID and this device has none. Falling back to the
            # name here is how a different device gets picked up, so we do not.
            return ""
        if not self.name:
            return ""
        if self.name != device.name:
            return ""
        if self.host_api and device.host_api and self.host_api != device.host_api:
            return ""
        if self.channels and device.max_input_channels != self.channels:
            return ""
        if (self.transport and device.transport
                and self.transport != device.transport):
            return ""
        return "composite"


@dataclasses.dataclass(frozen=True)
class DeviceMatch:
    """The result of restoring a persisted selection."""

    device: "AudioDevice"
    basis: str
    """``"uid"`` or ``"composite"``."""

    ambiguous: bool = False
    """More than one connected device is indistinguishable from the identity."""


@dataclasses.dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    default_sample_rate: float
    host_api: str = ""
    is_default: bool = False
    uid: str = ""
    """CoreAudio device UID, when macOS gave us one."""

    transport: str = ""
    """``builtin``, ``usb``, ``bluetooth``, ``virtual``... when known."""

    @property
    def id(self) -> str:
        """Legacy selector. Contains the index, so never persist this."""
        return f"{self.index}:{self.name}"

    @property
    def usable(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_builtin(self) -> bool:
        """True for the microphone built into the Mac itself."""
        if self.transport:
            return self.transport in coreaudio.BUILTIN_TRANSPORTS
        return _looks_builtin(self.name)

    @property
    def is_external(self) -> bool:
        return not self.is_builtin

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            uid=self.uid, name=self.name, host_api=self.host_api,
            channels=self.max_input_channels, transport=self.transport,
            label=self.name)

    def describe(self) -> str:
        marker = " (system default)" if self.is_default else ""
        kind = f" - {self.transport}" if self.transport else ""
        return (f"[{self.index}] {self.name}{marker} - "
                f"{self.max_input_channels} ch @ {self.default_sample_rate:.0f} Hz"
                f"{f' - {self.host_api}' if self.host_api else ''}{kind}")

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["id"] = self.id
        d["identity"] = self.identity.token()
        d["is_builtin"] = self.is_builtin
        return d


def _looks_builtin(name: str) -> bool:
    lowered = (name or "").lower()
    return any(hint in lowered for hint in BUILTIN_NAME_HINTS)


def _sounddevice():
    try:
        import sounddevice  # type: ignore

        return sounddevice
    except Exception as exc:  # noqa: BLE001 - any import failure means "no backend"
        log.debug("sounddevice unavailable: %s", exc)
        return None


def backend_available() -> bool:
    return _sounddevice() is not None


def backend_status() -> str:
    sd = _sounddevice()
    if sd is None:
        return ("PortAudio backend unavailable: install the 'audio' extra "
                "(pip install 'babelfishr[audio]'). Replay mode still works.")
    try:
        return f"PortAudio {sd.get_portaudio_version()[1]}"
    except Exception as exc:  # noqa: BLE001
        return f"sounddevice present but PortAudio failed to initialise: {exc}"


def list_input_devices() -> List[AudioDevice]:
    """Every device that can capture. Empty list when no backend is present.

    CoreAudio is consulted for a UID and a transport type where it can be, and
    ignored entirely where it cannot. A device with no UID is still usable; it
    just falls back to composite identification.
    """
    sd = _sounddevice()
    if sd is None:
        return []
    try:
        raw = sd.query_devices()
        host_apis = sd.query_hostapis()
        try:
            default_input = sd.default.device[0]
        except Exception:  # noqa: BLE001
            default_input = None
    except Exception as exc:  # noqa: BLE001
        log.warning("could not query audio devices: %s", exc)
        return []

    native = coreaudio.input_index()

    devices: List[AudioDevice] = []
    for index, info in enumerate(raw):
        channels = int(info.get("max_input_channels", 0))
        if channels <= 0:
            continue
        api_index = int(info.get("hostapi", -1))
        api_name = ""
        if 0 <= api_index < len(host_apis):
            api_name = str(host_apis[api_index].get("name", ""))
        name = str(info.get("name", f"device {index}"))
        match = native.get(name)
        # Only trust the CoreAudio row when the channel count agrees; a
        # mismatch means we matched the wrong device by name.
        if match is not None and match.input_channels != channels:
            match = None
        devices.append(AudioDevice(
            index=index,
            name=name,
            max_input_channels=channels,
            default_sample_rate=float(info.get("default_samplerate", 48000.0)),
            host_api=api_name,
            is_default=(default_input is not None and index == default_input),
            uid=match.uid if match else "",
            transport=match.transport if match else "",
        ))
    return devices


def resolve_identity(identity: Optional[DeviceIdentity],
                     devices: Optional[List[AudioDevice]] = None
                     ) -> Optional[DeviceMatch]:
    """Find the device the operator actually chose, or nothing.

    This is the only function that may be used to restore a persisted
    selection. It never falls back to the system default, never falls back to
    the built-in microphone, and never matches on the PortAudio index - so a
    device that comes back on a different index is still found, and a
    *different* device that inherits the old index is not.
    """
    if identity is None or identity.empty:
        return None
    if devices is None:
        devices = list_input_devices()

    exact = [device for device in devices if identity.matches(device) == "uid"]
    if exact:
        return DeviceMatch(exact[0], "uid", ambiguous=len(exact) > 1)

    composite = [device for device in devices
                 if identity.matches(device) == "composite"]
    if not composite:
        return None
    # Several connected devices are genuinely indistinguishable from the
    # recorded identity (two identical USB interfaces, say). They are
    # interchangeable as far as anything we can observe goes, but the operator
    # is told, because they may not be interchangeable in the rack.
    return DeviceMatch(composite[0], "composite", ambiguous=len(composite) > 1)


def unique_labels(devices: List[AudioDevice]) -> Dict[int, str]:
    """Human-readable labels, disambiguated only where they have to be.

    Two identical USB interfaces produce two identical names. Showing the
    operator the same string twice and asking them to pick is worse than
    useless, so duplicates gain the host API, channel count and current index.
    """
    counts: Dict[str, int] = {}
    for device in devices:
        counts[device.name] = counts.get(device.name, 0) + 1

    labels: Dict[int, str] = {}
    for device in devices:
        label = device.name
        if counts[device.name] > 1:
            details = [part for part in (device.host_api,
                                         f"{device.max_input_channels} ch") if part]
            if device.uid:
                details.append(f"uid {device.uid}")
            else:
                details.append(f"currently input {device.index}")
            label = f"{device.name} ({', '.join(details)})"
        labels[device.index] = label
    return labels


def find_device(selector: Optional[str]) -> Optional[AudioDevice]:
    """Resolve a device by index, identity token, exact id, or name fragment.

    For interactive and command-line use, where the operator is naming a device
    right now and can see the result. Do NOT use it to restore a saved
    selection: an index-based or fragment-based selector can silently land on a
    different device later. :func:`resolve_identity` is the safe path.
    """
    devices = list_input_devices()
    if not devices:
        return None
    if selector is None or str(selector).strip() in ("", "default"):
        for device in devices:
            if device.is_default:
                return device
        return devices[0]

    selector = str(selector).strip()
    if selector.startswith(f"{_IDENTITY_SCHEME}:"):
        match = resolve_identity(DeviceIdentity.parse(selector), devices)
        return match.device if match else None
    if selector.isdigit():
        index = int(selector)
        for device in devices:
            if device.index == index:
                return device
        return None
    for device in devices:
        if device.id == selector or device.name == selector:
            return device
    lowered = selector.lower()
    for device in devices:
        if lowered in device.name.lower():
            return device
    return None


def default_input_device() -> Optional[AudioDevice]:
    """The system default input. Only ever returned when explicitly asked for."""
    return find_device(None)


__all__ = [
    "AudioBackendUnavailable", "AudioDevice", "DeviceIdentity", "DeviceMatch",
    "InputDeviceMissing", "backend_available", "backend_status",
    "default_input_device", "find_device", "list_input_devices",
    "resolve_identity", "unique_labels",
]
