"""Turning what a decoder or a signal source actually said into metadata.

The rule this module exists to enforce: a value reaches a transmission only
when something genuinely supplied it, and it arrives carrying where it came
from. Nothing here derives a frequency, a tone, a talkgroup or a unit ID from
ordinary microphone or line-level audio, because none of those are recoverable
from it - and a fabricated identifier on a radio log is worse than an empty
field, which is at least honestly empty.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, Optional

from .models import AnalysisAttempt, Provenance, Transmission

log = logging.getLogger(__name__)

#: DSD-neo metadata key -> (transmission field, provenance).
#: Only keys a decoder genuinely produces. ``color_code`` and ``nac`` are kept
#: in the raw record rather than promoted: they identify a system, not a
#: transmitter, and there is nowhere honest to show them yet.
_DECODED_FIELDS = {
    "talkgroup": ("talkgroup", "talkgroup_provenance"),
    "source_id": ("unit_id", "unit_id_provenance"),
}


def apply_decoded_metadata(tx: Transmission, attempt: AnalysisAttempt) -> bool:
    """Promote genuinely decoded identifiers onto the transmission.

    Returns True when anything was set. The raw metadata is always kept in
    ``signal_metadata`` under the attempt's engine name, so a claim in the
    interface can be traced back to what the decoder actually printed.
    """
    metadata = dict(attempt.metadata or {})
    if not metadata:
        return False

    raw = dict(tx.signal_metadata or {})
    raw[attempt.engine or "decoder"] = metadata
    tx.signal_metadata = raw

    changed = False
    for key, (field, provenance_field) in _DECODED_FIELDS.items():
        value = metadata.get(key)
        if value in (None, ""):
            continue
        setattr(tx, field, str(value))
        setattr(tx, provenance_field, Provenance.DSD)
        changed = True

    if attempt.protocol and not tx.protocol:
        # Only a protocol the decoder identified - never one inferred from a
        # content classification, which is a guess about the audio's shape.
        tx.protocol = attempt.protocol
        tx.protocol_provenance = Provenance.DSD
        changed = True
    return changed


def apply_source_metadata(tx: Transmission, metadata: Optional[Any]) -> bool:
    """Attach measurements a signal source reported for this transmission.

    ``metadata`` is whatever a :class:`SignalSource` produced. Values are
    copied only when the source actually reported them, and each is marked
    with the source's own provenance rather than assumed to be measured -
    a recorded-IQ replay is not a live receiver.
    """
    if metadata is None:
        return False
    values: Dict[str, Any] = (dict(metadata) if isinstance(metadata, dict)
                              else getattr(metadata, "to_dict", dict)())
    if not values:
        return False

    provenance = _provenance_of(values.get("provenance"))
    # Kept verbatim, and kept JSON-serialisable: this record goes into a TEXT
    # column and has to come back out unchanged. A driver handing back a numpy
    # scalar must not be what stops a transmission being written.
    raw = dict(tx.signal_metadata or {})
    raw[str(values.get("source") or "signal-source")] = _jsonable(values)
    tx.signal_metadata = raw

    changed = False
    for key, field, provenance_field in _NUMERIC_FIELDS:
        number = _as_measurement(values.get(key))
        if number is None:
            # Absent, malformed, non-numeric or non-finite. The raw report is
            # already kept above for diagnostics; what must not happen is a
            # value like NaN or a driver's own object being written to the
            # column and presented as a measurement.
            continue
        setattr(tx, field, number)
        setattr(tx, provenance_field, provenance)
        changed = True

    for key, field, provenance_field in _TEXT_FIELDS:
        value = values.get(key)
        if value in (None, ""):
            continue
        setattr(tx, field, str(value))
        setattr(tx, provenance_field, provenance)
        changed = True
    return changed


#: Measurements, normalised to native floats before they are persisted.
_NUMERIC_FIELDS = (
    ("frequency_mhz", "frequency_mhz", "frequency_provenance"),
    ("rssi_dbm", "rssi_dbm", "rssi_provenance"),
    ("snr_db", "snr_db", "snr_provenance"),
)

_TEXT_FIELDS = (
    ("modulation", "modulation", "modulation_provenance"),
    ("squelch_code", "squelch_code", "squelch_code_provenance"),
    ("talkgroup", "talkgroup", "talkgroup_provenance"),
    ("unit_id", "unit_id", "unit_id_provenance"),
    ("protocol", "protocol", "protocol_provenance"),
)


def _as_measurement(value: Any) -> Optional[float]:
    """A native Python float, or None if this is not a usable measurement.

    Normalisation belongs here, at the boundary, so every future driver gets
    the same protection without knowing it needs it. A NumPy ``float32`` binds
    to SQLite as a **blob**: the row is written, the value comes back as
    ``bytes``, and the first attempt to format it for the interface raises
    ``TypeError: unsupported format string passed to bytes.__format__``. The
    recording is fine; the metadata quietly ruins the bubble that displays it.

    Rejected outright: booleans (``True`` is not -73 dBm), anything without a
    numeric conversion, and NaN or infinity - none of which is a measurement,
    and all of which would render as one.
    """
    if value is None or isinstance(value, bool):
        return None
    if not hasattr(value, "__float__"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _jsonable(value: Any) -> Any:
    """Whatever a source reported, in a form json.dumps accepts.

    A numeric scalar is converted to a real number rather than stringified,
    so the raw diagnostic record stays faithful to what the driver reported.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_jsonable(v) for v in value]
        number = _as_measurement(value)
        return number if number is not None else str(value)


def _provenance_of(value: Any) -> Provenance:
    """A source that does not say where a value came from does not get to
    claim it was measured."""
    if isinstance(value, Provenance):
        return value
    try:
        return Provenance(str(value))
    except ValueError:
        return Provenance.UNKNOWN
