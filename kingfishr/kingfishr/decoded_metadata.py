"""Promote what a digital decoder genuinely printed onto a transmission.

Moved from ``babelfishr/signal_metadata.py``. The rule is the same: a value
reaches a transmission only when the decoder supplied it, carrying
``Provenance.DSD``. BabelFishR keeps the data model (analysis attempts,
talkgroup / unit ID fields) and shows stored values read-only.
"""

from __future__ import annotations

from babelfishr.models import AnalysisAttempt, Provenance, Transmission

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
