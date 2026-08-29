"""Optional SDR input as a first-class extension point.

The default and fully supported input is the ordinary audio path: a radio's
accessory output into a computer audio input.  Everything BabelFishR does works
on that path alone, and nothing here is required.

An SDR, when present, can supply what an audio cable physically cannot:

* the tuned and measured centre frequency, as a *measured* value with SDR
  provenance rather than an operator-typed label;
* sample rate, gain settings, and RSSI/SNR where the device reports them;
* baseband or discriminator audio, which is a far better input to a digital
  decoder than filtered accessory audio;
* IQ recordings and spectrum data;
* scanning across configured channels.

:class:`SignalSource` extends :class:`~babelfishr.audio.source.AudioSource`
with that measured metadata, so the pipeline consumes both through the same
interface and losing the SDR degrades to the ordinary path rather than
breaking it.

No SDR hardware provider is bundled. Claiming support for a dongle nobody has
tested against would be worse than claiming none: the interface is here, with
one honest reference implementation over recorded IQ, and a real device driver
is a separate, testable piece of work.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .audio.source import AudioBlock, AudioSource
from .models import Provenance


@dataclasses.dataclass
class SignalMetadata:
    """What a signal source can measure. Every field may be absent.

    Absent means absent: a source that cannot measure RSSI leaves it None
    rather than inventing one, and the pipeline records provenance accordingly.
    """

    center_frequency_hz: Optional[float] = None
    tuned_frequency_hz: Optional[float] = None
    sample_rate_hz: Optional[float] = None
    gain_db: Optional[float] = None
    rssi_dbm: Optional[float] = None
    snr_db: Optional[float] = None
    modulation: str = ""
    provenance: Provenance = Provenance.SDR

    @property
    def frequency_mhz(self) -> Optional[float]:
        frequency = self.tuned_frequency_hz or self.center_frequency_hz
        return frequency / 1e6 if frequency is not None else None

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["provenance"] = self.provenance.value
        d["frequency_mhz"] = self.frequency_mhz
        return d


class SignalSource(AudioSource):
    """An audio source that also reports measured RF metadata."""

    #: Whether this source measures RF parameters at all.
    measures_rf: bool = True

    @abc.abstractmethod
    def metadata(self) -> SignalMetadata:
        """Current measured parameters. Called when a transmission is captured."""

    def tune(self, frequency_hz: float) -> bool:
        """Retune, where the device supports it. False when it does not."""
        return False

    def channels(self) -> List[float]:
        """Frequencies this source is configured to scan, if any."""
        return []

    def supports_iq(self) -> bool:
        return False


class RecordedIQSource(SignalSource):
    """Reference SignalSource over a recorded IQ or baseband file.

    Exists so the SignalSource contract is exercised by real code and real
    tests rather than existing only as an abstract class. It is not a device
    driver: it replays a file and reports the metadata that accompanied it.
    """

    measures_rf = True

    def __init__(self, samples: np.ndarray, sample_rate: int,
                 metadata: Optional[SignalMetadata] = None,
                 block_size: int = 2048, name: str = "recorded-iq"):
        self._samples = np.asarray(samples, dtype=np.float64).ravel()
        self.sample_rate = int(sample_rate)
        self._metadata = metadata or SignalMetadata(
            sample_rate_hz=float(sample_rate))
        self.block_size = int(block_size)
        self.name = name
        self._position = 0
        self._running = False
        self._finished = False
        import datetime as _dt

        from .models import utcnow

        self._dt = _dt
        self._start_time = utcnow()

    def start(self) -> None:
        self._running = True
        self._finished = False
        self._position = 0

    def stop(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def finished(self) -> bool:
        return self._finished

    def metadata(self) -> SignalMetadata:
        return self._metadata

    def read(self, timeout: float = 1.0) -> Optional[AudioBlock]:
        if not self._running:
            return None
        if self._position >= self._samples.size:
            self._finished = True
            self._running = False
            return None
        chunk = self._samples[self._position:self._position + self.block_size]
        offset = self._position / float(self.sample_rate)
        self._position += chunk.size
        return AudioBlock(
            samples=chunk, sample_rate=self.sample_rate,
            timestamp=self._start_time + self._dt.timedelta(seconds=offset),
            offset=offset)


def sdr_status(config) -> Dict[str, Any]:
    """Whether an SDR is configured and usable. Never raises."""
    sdr = getattr(config, "sdr", None)
    driver = (getattr(sdr, "driver", "") or "").strip()
    if not driver or driver == "none":
        return {"configured": False, "available": False,
                "reason": "no SDR configured", "detail": "",
                "driver": ""}

    if driver == "recorded-iq":
        path = getattr(sdr, "recording_path", "") or ""
        import pathlib

        exists = bool(path) and pathlib.Path(path).expanduser().exists()
        return {
            "configured": True, "available": exists, "driver": driver,
            "reason": "" if exists else f"recording not found: {path!r}",
            "detail": f"recorded IQ from {path}" if exists else "",
        }

    # Any other driver name is a device integration that is not bundled.
    return {
        "configured": True, "available": False, "driver": driver,
        "reason": (f"SDR driver {driver!r} is not bundled with BabelFishR. "
                   f"No device driver has been tested, so none is claimed. "
                   f"The ordinary audio input path is unaffected."),
        "detail": "",
    }


def build_signal_source(config) -> Optional[SignalSource]:
    """Construct the configured SDR source, or None. Never raises on absence."""
    status = sdr_status(config)
    if not status["available"]:
        return None
    if status["driver"] == "recorded-iq":
        import pathlib

        from .audio.wavefile import read_wav

        sdr = config.sdr
        samples, rate = read_wav(str(pathlib.Path(sdr.recording_path).expanduser()))
        metadata = SignalMetadata(
            center_frequency_hz=getattr(sdr, "center_frequency_hz", None),
            tuned_frequency_hz=getattr(sdr, "tuned_frequency_hz", None),
            sample_rate_hz=float(rate),
            gain_db=getattr(sdr, "gain_db", None))
        return RecordedIQSource(samples, rate, metadata=metadata)
    return None
