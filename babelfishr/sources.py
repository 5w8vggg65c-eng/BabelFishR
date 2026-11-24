"""The signal-source contract: an audio source that also reports measured
RF metadata (:class:`SignalSource`, :class:`SignalMetadata`).

The default and fully supported input is the ordinary audio path: a radio's
accessory output into a computer audio input. Everything BabelFishR does works
on that path alone. This contract is what the pipeline reads metadata through
(:func:`babelfishr.signal_metadata.apply_source_metadata`), so a source that
measures frequency, RSSI or SNR records them with provenance, and a source
that measures nothing records nothing.

BabelFishR ships no RF source. The SDR sources (the recorded-IQ reference,
the SDR++ receiver stream) are part of KingFishR, which implements this
contract from its own package.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Any, Dict, List, Optional

from .audio.source import AudioSource
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

    # Channel access and digital identifiers, for a source that genuinely
    # decodes them. A source that does not leaves them empty; nothing here is
    # ever derived from the audio.
    squelch_code: str = ""
    """CTCSS/PL tone or DCS code. Channel access, not an identity."""

    talkgroup: str = ""
    unit_id: str = ""
    protocol: str = ""

    source: str = ""
    """What reported this, e.g. "rtl-sdr". Used as the key under which the
    raw record is kept on the transmission."""

    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)
    """Anything the source reports that this contract does not model."""

    provenance: Provenance = Provenance.UNKNOWN
    """Where these values came from, and it must be claimed, not assumed.

    The default used to be ``SDR``, so a source that said nothing about
    provenance had its values presented as *measured* - a recorded replay, a
    configured constant or a half-written driver would all have read as a
    live receiver's measurements. Measured status now requires an affirmative
    claim: a genuine live SDR passes ``Provenance.SDR``, a radio reporting its
    own tuning passes ``RADIO``, and anything that says nothing stays
    ``UNKNOWN`` and is rendered as unverified.
    """

    @property
    def frequency_mhz(self) -> Optional[float]:
        frequency = self.tuned_frequency_hz or self.center_frequency_hz
        return frequency / 1e6 if frequency is not None else None

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-serialisable record of exactly what the source reported.

        It is stored verbatim on the transmission and goes through SQLite and
        back, so every value in it has to survive ``json.dumps``.
        """
        d = dataclasses.asdict(self)
        d["provenance"] = _provenance_value(self.provenance)
        d["frequency_mhz"] = self.frequency_mhz
        d["extra"] = _jsonable(self.extra)
        return d


def _provenance_value(provenance: Any) -> str:
    """The provenance as a plain string, whatever the source put there."""
    if isinstance(provenance, Provenance):
        return provenance.value
    return str(provenance) if provenance else Provenance.UNKNOWN.value


def _jsonable(value: Any) -> Any:
    """Coerce a source's extra metadata into something json.dumps accepts.

    A driver is free to hand back a NumPy scalar or its own object; that must
    not be the thing that stops a transmission being written to the database.
    Shared with the promotion path so both agree on what a number is.
    """
    from .signal_metadata import _jsonable as coerce

    return coerce(value)


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
