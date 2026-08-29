"""Continuous safety recording.

Segmentation can be wrong.  A threshold set too high, an unusual squelch tail or
a burst of interference can mean a transmission is never cut out of the stream -
and if the only copy of the audio was the segment, that traffic is gone.

The safety recorder writes the raw stream to disk in fixed-length chunks
regardless of what the detector thinks, so an operator can always go back to the
tape.  It is off by default (it costs disk), and its retention is configurable.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import logging
import pathlib
import time
from typing import List, Optional

import numpy as np

from ..models import utcnow
from .source import AudioBlock
from .wavefile import write_wav

log = logging.getLogger(__name__)


@dataclasses.dataclass
class SafetyChunk:
    path: str
    started_at: _dt.datetime
    duration: float
    bytes: int


class SafetyRecorder:
    """Rolling chunked recorder with age- and size-based retention."""

    def __init__(self, directory: str, chunk_seconds: float = 300.0,
                 enabled: bool = False, retention_hours: Optional[float] = 24.0,
                 max_bytes: Optional[int] = None, bit_depth: int = 16,
                 session_id: str = ""):
        self.directory = pathlib.Path(directory)
        self.chunk_seconds = max(5.0, float(chunk_seconds))
        self.enabled = enabled
        self.retention_hours = retention_hours
        self.max_bytes = max_bytes
        self.bit_depth = bit_depth
        self.session_id = session_id
        self.chunks: List[SafetyChunk] = []

        self._buffer: List[np.ndarray] = []
        self._buffered = 0
        self._sample_rate = 0
        self._chunk_start: Optional[_dt.datetime] = None

    # -- writing ---------------------------------------------------------
    def feed(self, block: AudioBlock) -> Optional[SafetyChunk]:
        """Add a block; returns a chunk when one is completed."""
        if not self.enabled:
            return None
        if self._chunk_start is None:
            self._chunk_start = block.timestamp
        self._sample_rate = block.sample_rate
        self._buffer.append(np.asarray(block.samples, dtype=np.float64))
        self._buffered += block.samples.size
        if self._buffered >= self.chunk_seconds * block.sample_rate:
            return self.flush()
        return None

    def flush(self) -> Optional[SafetyChunk]:
        """Write whatever is buffered as a chunk."""
        if not self.enabled or not self._buffer or self._sample_rate <= 0:
            self._buffer, self._buffered = [], 0
            return None
        audio = np.concatenate(self._buffer)
        started = self._chunk_start or utcnow()
        self._buffer, self._buffered, self._chunk_start = [], 0, None

        stamp = started.strftime("%Y%m%dT%H%M%SZ")
        name = f"safety_{stamp}.wav"
        target = self.directory / (self.session_id or "unsorted") / name
        try:
            write_wav(str(target), audio, self._sample_rate, self.bit_depth)
        except OSError as exc:
            log.error("safety recording failed (%s); continuing without it", exc)
            return None

        chunk = SafetyChunk(
            path=str(target), started_at=started,
            duration=audio.size / float(self._sample_rate),
            bytes=target.stat().st_size if target.exists() else 0,
        )
        self.chunks.append(chunk)
        self.enforce_retention()
        return chunk

    def close(self) -> Optional[SafetyChunk]:
        return self.flush()

    # -- retention -------------------------------------------------------
    def enforce_retention(self) -> int:
        """Delete chunks past the age or size limit. Returns how many went."""
        removed = 0
        if self.retention_hours is not None and self.retention_hours > 0:
            cutoff = utcnow() - _dt.timedelta(hours=self.retention_hours)
            for chunk in list(self.chunks):
                if chunk.started_at < cutoff:
                    removed += self._remove(chunk)
        if self.max_bytes is not None and self.max_bytes > 0:
            total = sum(c.bytes for c in self.chunks)
            for chunk in sorted(self.chunks, key=lambda c: c.started_at):
                if total <= self.max_bytes:
                    break
                total -= chunk.bytes
                removed += self._remove(chunk)
        return removed

    def _remove(self, chunk: SafetyChunk) -> int:
        with contextlib.suppress(OSError):
            pathlib.Path(chunk.path).unlink()
        with contextlib.suppress(ValueError):
            self.chunks.remove(chunk)
        return 1

    def total_bytes(self) -> int:
        return sum(c.bytes for c in self.chunks)

    def describe(self) -> str:
        if not self.enabled:
            return "safety recording: off"
        retention = (f"{self.retention_hours:.0f} h" if self.retention_hours
                     else "unlimited")
        return (f"safety recording: on, {len(self.chunks)} chunk(s), "
                f"{self.total_bytes() / 1e6:.1f} MB, retention {retention}, "
                f"in {self.directory}")
