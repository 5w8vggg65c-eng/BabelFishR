"""Decoder plug-in framework.

Every decoder receives the *whole* audio of one transmission (not a stream) and
returns zero or more :class:`~babelfishr.models.DecodeResult`.  Decoders must be
cheap enough to all run over every transmission, and must never raise: a broken
decoder degrades to "no decode", it does not take the receiver down.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Protocol

import numpy as np

from ..models import DecodeResult

log = logging.getLogger(__name__)


class Decoder(Protocol):
    id: str
    name: str

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        ...


_REGISTRY: Dict[str, "BaseDecoder"] = {}


class BaseDecoder:
    id: str = "base"
    name: str = "base"
    description: str = ""

    #: Preferred sample rate; the runner resamples if the input differs.
    sample_rate: Optional[int] = None

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:  # pragma: no cover
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} id={self.id}>"


def register(decoder: "BaseDecoder") -> "BaseDecoder":
    _REGISTRY[decoder.id] = decoder
    return decoder


def available() -> Dict[str, "BaseDecoder"]:
    _load_builtin()
    return dict(_REGISTRY)


def get(decoder_id: str) -> "BaseDecoder":
    _load_builtin()
    if decoder_id not in _REGISTRY:
        raise KeyError(f"unknown decoder {decoder_id!r}; known: {', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[decoder_id]


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import (afsk, ctcss, cw, dcs, digital_voice, dtmf,  # noqa: F401
                   mdc1200, pocsag, rtty)


def run_decoders(audio: np.ndarray, sample_rate: int,
                 enabled: Optional[Iterable[str]] = None,
                 min_confidence: float = 0.0,
                 on_error: Optional[Callable[[str, Exception], None]] = None,
                 ) -> List[DecodeResult]:
    """Run the enabled decoders over one transmission, newest-first by confidence."""
    from ..dsp.resample import resample

    decoders = available()
    ids = list(enabled) if enabled is not None else list(decoders)
    results: List[DecodeResult] = []
    cache: Dict[int, np.ndarray] = {int(sample_rate): np.asarray(audio, dtype=np.float64)}

    for did in ids:
        dec = decoders.get(did)
        if dec is None:
            log.warning("ignoring unknown decoder %r", did)
            continue
        rate = int(dec.sample_rate or sample_rate)
        if rate not in cache:
            cache[rate] = resample(cache[int(sample_rate)], sample_rate, rate)
        try:
            found = dec.decode(cache[rate], rate) or []
        except Exception as exc:  # noqa: BLE001 - decoders must never be fatal
            log.debug("decoder %s failed: %s", did, exc, exc_info=True)
            if on_error is not None:
                on_error(did, exc)
            continue
        results.extend(r for r in found if r.confidence >= min_confidence)

    results.sort(key=lambda r: (-r.confidence, r.offset))
    return results
