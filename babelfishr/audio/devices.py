"""Audio input device discovery.

Wraps ``sounddevice`` (PortAudio) but never requires it at import time: the
device list, the CLI and the tests must all work on a machine with no audio
backend at all, which is exactly the situation in CI.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, List, Optional

log = logging.getLogger(__name__)


class AudioBackendUnavailable(RuntimeError):
    """Raised when a real capture is attempted without a working PortAudio."""


@dataclasses.dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    default_sample_rate: float
    host_api: str = ""
    is_default: bool = False

    @property
    def id(self) -> str:
        return f"{self.index}:{self.name}"

    @property
    def usable(self) -> bool:
        return self.max_input_channels > 0

    def describe(self) -> str:
        marker = " (default)" if self.is_default else ""
        return (f"[{self.index}] {self.name}{marker} - "
                f"{self.max_input_channels} ch @ {self.default_sample_rate:.0f} Hz"
                f"{f' - {self.host_api}' if self.host_api else ''}")

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["id"] = self.id
        return d


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
    """Every device that can capture. Empty list when no backend is present."""
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

    devices: List[AudioDevice] = []
    for index, info in enumerate(raw):
        if int(info.get("max_input_channels", 0)) <= 0:
            continue
        api_index = int(info.get("hostapi", -1))
        api_name = ""
        if 0 <= api_index < len(host_apis):
            api_name = str(host_apis[api_index].get("name", ""))
        devices.append(AudioDevice(
            index=index,
            name=str(info.get("name", f"device {index}")),
            max_input_channels=int(info.get("max_input_channels", 0)),
            default_sample_rate=float(info.get("default_samplerate", 48000.0)),
            host_api=api_name,
            is_default=(default_input is not None and index == default_input),
        ))
    return devices


def find_device(selector: Optional[str]) -> Optional[AudioDevice]:
    """Resolve a device by index, exact id, or case-insensitive name fragment."""
    devices = list_input_devices()
    if not devices:
        return None
    if selector is None or str(selector).strip() in ("", "default"):
        for device in devices:
            if device.is_default:
                return device
        return devices[0]

    selector = str(selector).strip()
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
    return find_device(None)
