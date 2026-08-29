"""Squelch / voice-activity segmentation.

A radio channel is silent (or hissing) most of the time.  This module turns a
continuous stream into discrete *transmissions*: it tracks the channel noise
floor, opens on signal, holds through the short gaps inside speech, and closes
after a configurable hangtime.  That segmentation is what makes "record every
transmission on this band" possible without recording the squelch hiss.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from collections import deque
from typing import Deque, List, Optional

import numpy as np

from .filters import dbfs, rms


@dataclasses.dataclass
class Segment:
    """One squelch-open period, ready to be decoded and recorded."""

    audio: np.ndarray
    sample_rate: int
    start_time: _dt.datetime
    start_offset: float
    """Seconds since the stream started."""

    duration: float
    peak_dbfs: float
    noise_floor_dbfs: float
    active_ratio: float
    """Fraction of frames that were above the open threshold."""

    truncated: bool = False
    """True when the segment hit ``max_duration`` and was cut."""

    @property
    def snr_db(self) -> float:
        return self.peak_dbfs - self.noise_floor_dbfs


class NoiseFloorTracker:
    """Asymmetric tracker: falls fast toward quiet, rises slowly.

    Squelch that keys open on a rising noise floor is worse than useless on a
    busy band, so the rise is deliberately sluggish (and only happens while the
    gate is closed).
    """

    def __init__(self, initial_dbfs: float = -70.0,
                 fall_rate_db_s: float = 24.0, rise_rate_db_s: float = 1.5):
        self.value = initial_dbfs
        self.fall_rate = fall_rate_db_s
        self.rise_rate = rise_rate_db_s
        self._initialised = False

    def update(self, level_dbfs: float, dt: float) -> float:
        if not self._initialised:
            self.value = level_dbfs
            self._initialised = True
            return self.value
        if level_dbfs < self.value:
            self.value = max(level_dbfs, self.value - self.fall_rate * dt)
        else:
            self.value = min(level_dbfs, self.value + self.rise_rate * dt)
        return self.value


class Segmenter:
    """Streaming squelch. Feed it audio blocks, get back complete segments."""

    def __init__(self, sample_rate: int, *, mode: str = "auto",
                 threshold_dbfs: float = -45.0, open_margin_db: float = 8.0,
                 close_margin_db: float = 4.0, hangtime: float = 0.6,
                 min_duration: float = 0.35, max_duration: float = 300.0,
                 pre_roll: float = 0.25, frame_ms: float = 20.0,
                 open_frames: int = 2, tail_pad: float = 0.15):
        self.sample_rate = int(sample_rate)
        self.mode = mode
        self.threshold_dbfs = threshold_dbfs
        self.open_margin_db = open_margin_db
        self.close_margin_db = close_margin_db
        self.hangtime = hangtime
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.frame_size = max(1, int(sample_rate * frame_ms / 1000.0))
        self.frame_dt = self.frame_size / float(sample_rate)
        self.open_frames = max(1, open_frames)
        self.tail_pad = max(0.0, tail_pad)

        self.floor = NoiseFloorTracker()
        self._pending = np.zeros(0, dtype=np.float64)
        self._pre_roll: Deque[np.ndarray] = deque(
            maxlen=max(1, int(round(pre_roll / self.frame_dt))))
        self._open = False
        self._consecutive_open = 0
        self._buffer: List[np.ndarray] = []
        self._active_frames = 0
        self._total_frames = 0
        self._peak = 0.0
        self._floor_at_open = -120.0
        self._samples_seen = 0
        self._seg_start_offset = 0.0
        self._hang_remaining = 0.0
        self._last_active_samples = 0
        """Samples buffered as of the last frame above the close threshold."""
        self.start_wallclock: Optional[_dt.datetime] = None

    # -- thresholds ------------------------------------------------------
    def open_threshold(self) -> float:
        if self.mode == "fixed":
            return self.threshold_dbfs
        return self.floor.value + self.open_margin_db

    def close_threshold(self) -> float:
        if self.mode == "fixed":
            return self.threshold_dbfs - self.close_margin_db
        return self.floor.value + self.close_margin_db

    # -- streaming API ---------------------------------------------------
    def push(self, block: np.ndarray, timestamp: Optional[_dt.datetime] = None) -> List[Segment]:
        """Consume an audio block; return any segments that completed."""
        if self.start_wallclock is None:
            self.start_wallclock = timestamp or _dt.datetime.now(_dt.timezone.utc)
        block = np.asarray(block, dtype=np.float64).ravel()
        self._pending = np.concatenate([self._pending, block]) if self._pending.size else block

        out: List[Segment] = []
        n_frames = self._pending.size // self.frame_size
        for i in range(n_frames):
            frame = self._pending[i * self.frame_size:(i + 1) * self.frame_size]
            seg = self._process_frame(frame)
            if seg is not None:
                out.append(seg)
        if n_frames:
            self._pending = self._pending[n_frames * self.frame_size:]
        return out

    def flush(self) -> List[Segment]:
        """End of stream: emit whatever is still open."""
        out: List[Segment] = []
        if self._pending.size and self._open:
            self._buffer.append(self._pending.copy())
        self._pending = np.zeros(0, dtype=np.float64)
        if self._open:
            seg = self._close_segment()
            if seg is not None:
                out.append(seg)
        return out

    # -- internals -------------------------------------------------------
    def _process_frame(self, frame: np.ndarray) -> Optional[Segment]:
        level = dbfs(rms(frame))
        self._samples_seen += frame.size
        completed: Optional[Segment] = None

        if not self._open:
            self.floor.update(level, self.frame_dt)

        if level >= self.open_threshold():
            self._consecutive_open += 1
        else:
            self._consecutive_open = 0

        if not self._open:
            self._pre_roll.append(frame.copy())
            if self._consecutive_open >= self.open_frames:
                self._start_segment()
            return None

        # Gate is open: accumulate.
        self._buffer.append(frame.copy())
        self._total_frames += 1
        self._peak = max(self._peak, float(np.max(np.abs(frame))) if frame.size else 0.0)
        if level >= self.open_threshold():
            self._active_frames += 1

        if level < self.close_threshold():
            self._hang_remaining -= self.frame_dt
            if self._hang_remaining <= 0:
                completed = self._close_segment()
        else:
            self._hang_remaining = self.hangtime
            self._last_active_samples = sum(b.size for b in self._buffer)

        if completed is None and self._buffered_duration() >= self.max_duration:
            completed = self._close_segment(truncated=True)
        return completed

    def _buffered_duration(self) -> float:
        return sum(b.size for b in self._buffer) / float(self.sample_rate)

    def _start_segment(self) -> None:
        self._open = True
        self._buffer = list(self._pre_roll)
        pre_samples = sum(b.size for b in self._buffer)
        self._seg_start_offset = max(
            0.0, (self._samples_seen - pre_samples) / float(self.sample_rate))
        self._pre_roll.clear()
        self._active_frames = self.open_frames
        self._total_frames = len(self._buffer)
        self._last_active_samples = pre_samples
        self._peak = 0.0
        self._floor_at_open = self.floor.value
        self._hang_remaining = self.hangtime

    def _close_segment(self, truncated: bool = False) -> Optional[Segment]:
        audio = np.concatenate(self._buffer) if self._buffer else np.zeros(0)
        pre_samples = self.frame_size * len(self._pre_roll)
        # Trim the hangtime tail: keep signal plus a short pad, so a 100 ms
        # click stays a 100 ms segment instead of inheriting the whole hangtime.
        keep = min(audio.size,
                   self._last_active_samples + int(self.tail_pad * self.sample_rate))
        audio = audio[:keep]
        self._open = False
        self._buffer = []
        self._consecutive_open = 0
        self._last_active_samples = 0
        duration = audio.size / float(self.sample_rate)
        # min_duration judges the signal itself, excluding pre-roll and pad.
        signal_duration = max(0.0, duration - pre_samples / float(self.sample_rate)
                              - self.tail_pad)
        if signal_duration < self.min_duration:
            return None
        assert self.start_wallclock is not None
        return Segment(
            audio=audio,
            sample_rate=self.sample_rate,
            start_time=self.start_wallclock + _dt.timedelta(seconds=self._seg_start_offset),
            start_offset=self._seg_start_offset,
            duration=duration,
            peak_dbfs=dbfs(self._peak),
            noise_floor_dbfs=self._floor_at_open,
            active_ratio=(self._active_frames / self._total_frames) if self._total_frames else 0.0,
            truncated=truncated,
        )


def segment_array(audio: np.ndarray, sample_rate: int, **kwargs) -> List[Segment]:
    """Convenience: run the segmenter over a complete array (file replay)."""
    seg = Segmenter(sample_rate, **kwargs)
    out = seg.push(np.asarray(audio, dtype=np.float64))
    out.extend(seg.flush())
    return out
