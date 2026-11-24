"""KingFishR's SDR sources over the BabelFishR signal-source contract.

Moved from ``babelfishr/sources.py`` (BabelFishR keeps the contract:
:class:`~babelfishr.sources.SignalSource` and
:class:`~babelfishr.sources.SignalMetadata`).

No SDR hardware provider is bundled. Claiming support for a dongle nobody has
tested against would be worse than claiming none: there is one honest
reference implementation over recorded IQ, and a real device driver is a
separate, testable piece of work.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

from babelfishr.audio.source import AudioBlock
from babelfishr.sources import SignalMetadata, SignalSource


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
        # No provenance: a recording being replayed is not a measurement
        # being taken, and the caller can say otherwise if it genuinely is.
        self._metadata = metadata or SignalMetadata(
            sample_rate_hz=float(sample_rate))
        self.block_size = int(block_size)
        self.name = name
        self._position = 0
        self._running = False
        self._finished = False
        import datetime as _dt

        from babelfishr.models import utcnow

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

        from babelfishr.audio.wavefile import read_wav

        sdr = config.sdr
        samples, rate = read_wav(str(pathlib.Path(sdr.recording_path).expanduser()))
        metadata = SignalMetadata(
            center_frequency_hz=getattr(sdr, "center_frequency_hz", None),
            tuned_frequency_hz=getattr(sdr, "tuned_frequency_hz", None),
            sample_rate_hz=float(rate),
            gain_db=getattr(sdr, "gain_db", None))
        return RecordedIQSource(samples, rate, metadata=metadata)
    return None
