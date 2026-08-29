"""Experimental signalling decoders - NOT part of the supported MVP.

Status
------
Everything in this package decodes digital signalling that may ride on
analogue receive audio (CTCSS, DCS, DTMF, CW, AFSK1200/AX.25/APRS, MDC-1200,
POCSAG, RTTY) or classifies digital voice bursts.  It was written and verified
against *synthetically generated* signals only.  None of it has been validated
against off-air recordings from a real radio.

Why it is quarantined
---------------------
BabelFishR's supported job is analogue voice: detect a transmission, record it,
transcribe it, translate it.  Signalling decoders are a different product with
a different validation burden, and a half-validated decoder in the receive path
is worse than no decoder - it produces confident-looking wrong metadata.

Nothing here is imported by the receive pipeline.  It is opt-in:

    from babelfishr.experimental import load_decoders
    decoders = load_decoders()          # raises unless explicitly enabled

Enable with ``BABELFISHR_EXPERIMENTAL=1`` in the environment, or
``experimental = true`` in the config file.

Before trusting any of this against real traffic
------------------------------------------------
Record known off-air samples, confirm the decode against the transmitting
radio's own display, and only then consider promoting a decoder out of here.
See ``docs/EXPERIMENTAL.md``.
"""

from __future__ import annotations

import os
from typing import Dict


class ExperimentalDisabled(RuntimeError):
    """Raised when experimental code is used without being enabled."""


def enabled(config=None) -> bool:
    """True when the operator has explicitly opted in."""
    if config is not None and getattr(config, "experimental", False):
        return True
    return os.environ.get("BABELFISHR_EXPERIMENTAL", "").strip().lower() in (
        "1", "true", "yes", "on")


def require_enabled(config=None) -> None:
    if not enabled(config):
        raise ExperimentalDisabled(
            "Experimental signalling decoders are disabled. They are validated "
            "against synthetic signals only. Set BABELFISHR_EXPERIMENTAL=1 to "
            "opt in."
        )


def load_decoders(config=None) -> Dict[str, object]:
    """Import and return the decoder registry, if experimental use is enabled."""
    require_enabled(config)
    from .decoders.base import available

    return available()


__all__ = ["ExperimentalDisabled", "enabled", "require_enabled", "load_decoders"]
