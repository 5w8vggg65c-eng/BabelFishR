"""A bounded, signed amplitude envelope of the most recent live audio."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


WINDOW_SECONDS = 5
BUCKETS_PER_SECOND = 100


@dataclass(frozen=True)
class WaveformFrame:
    """Oldest to newest (minimum, maximum) pairs, in original sample units."""

    extrema: np.ndarray
    seconds_per_bucket: float


class RollingEnvelope:
    """Reduce PCM on the capture thread. Only completed buckets reach Qt."""

    def __init__(self, sample_rate: int):
        if sample_rate <= 0:
            raise ValueError("The waveform needs a positive sample rate")
        self.sample_rate = sample_rate
        self._width = max(1, round(sample_rate / BUCKETS_PER_SECOND))
        # Round up so the visible window spans five seconds at any input rate.
        capacity = (WINDOW_SECONDS * sample_rate + self._width - 1) // self._width
        self._history = np.empty((capacity, 2), dtype=np.float32)
        self._used = 0
        self._tail = np.empty(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> None:
        data = np.asarray(samples).ravel()
        if not data.size:
            return
        width = self._width
        capacity = len(self._history)
        count, remainder = divmod(self._tail.size + data.size, width)
        if count > capacity:
            # Even a single oversized block costs only one window of work.
            # Preserve the bucket boundary shared with the preceding block.
            count = capacity
            data = data[-(count * width + remainder):]
        elif self._tail.size:
            data = np.concatenate((self._tail, data))
        split = count * width
        self._tail = data[split:].copy()
        if count:
            buckets = data[:split].reshape(count, width)
            self._history[:-count] = self._history[count:]
            self._history[-count:, 0] = buckets.min(axis=1)
            self._history[-count:, 1] = buckets.max(axis=1)
            self._used = min(capacity, self._used + count)

    def frame(self) -> Optional[WaveformFrame]:
        if not self._used:
            return None
        # The capture thread can immediately reuse its history. A queued
        # reading is a snapshot and survives missing intermediate UI updates.
        extrema = self._history[-self._used:].copy()
        extrema.setflags(write=False)
        return WaveformFrame(extrema, self._width / self.sample_rate)
