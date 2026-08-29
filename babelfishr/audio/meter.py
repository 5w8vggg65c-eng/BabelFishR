"""Level metering and the input calibration workflow.

Radio audio arrives at wildly different levels depending on the radio's volume
knob, the accessory port's output level and the interface's input gain.  Getting
that right is the single most common cause of "it didn't hear anything" or "it
recorded static all night", so the meter and the calibration step are treated as
first-class features rather than debug output.
"""

from __future__ import annotations

import dataclasses
import time
from typing import List, Optional

import numpy as np

from ..dsp.filters import dbfs, rms
from .source import CLIP_THRESHOLD, AudioBlock, AudioSource

#: Below this the input is almost certainly not connected to anything.
SILENT_DBFS = -75.0
#: A comfortable idle noise floor for radio audio.
GOOD_FLOOR_RANGE = (-60.0, -30.0)
#: Speech peaks should land here: loud enough to detect, clear of the ceiling.
GOOD_PEAK_RANGE = (-24.0, -3.0)


@dataclasses.dataclass
class LevelReading:
    rms_dbfs: float
    peak_dbfs: float
    peak_hold_dbfs: float
    clipped: bool
    clip_count: int

    @property
    def rms_fraction(self) -> float:
        """0..1 position on a -60..0 dBFS meter, for drawing a bar."""
        return float(np.clip((self.rms_dbfs + 60.0) / 60.0, 0.0, 1.0))

    @property
    def peak_fraction(self) -> float:
        return float(np.clip((self.peak_hold_dbfs + 60.0) / 60.0, 0.0, 1.0))


class LevelMeter:
    """Peak/RMS meter with peak-hold decay and a clipping counter."""

    def __init__(self, hold_seconds: float = 1.5, decay_db_per_second: float = 20.0):
        self.hold_seconds = hold_seconds
        self.decay = decay_db_per_second
        self._peak_hold = -120.0
        self._last_update: Optional[float] = None
        self.clip_count = 0
        self.last: LevelReading = LevelReading(-120.0, -120.0, -120.0, False, 0)

    def reset(self) -> None:
        self._peak_hold = -120.0
        self._last_update = None
        self.clip_count = 0

    def update(self, block: AudioBlock, now: Optional[float] = None) -> LevelReading:
        now = time.monotonic() if now is None else now
        peak_db = block.peak_dbfs
        if self._last_update is not None:
            elapsed = max(0.0, now - self._last_update)
            self._peak_hold -= self.decay * elapsed
        self._last_update = now
        self._peak_hold = max(self._peak_hold, peak_db)

        clipped_now = block.clipped_samples
        self.clip_count += clipped_now
        self.last = LevelReading(
            rms_dbfs=block.rms_dbfs, peak_dbfs=peak_db,
            peak_hold_dbfs=self._peak_hold, clipped=clipped_now > 0,
            clip_count=self.clip_count,
        )
        return self.last


@dataclasses.dataclass
class CalibrationResult:
    """What a short listen to an idle channel tells us about the input."""

    seconds: float
    noise_floor_dbfs: float
    median_dbfs: float
    peak_dbfs: float
    clip_count: int
    recommended_threshold_dbfs: float
    recommended_open_margin_db: float
    warnings: List[str] = dataclasses.field(default_factory=list)
    ok: bool = True

    def summary(self) -> str:
        lines = [
            f"Listened for {self.seconds:.1f} s",
            f"  noise floor : {self.noise_floor_dbfs:6.1f} dBFS",
            f"  median level: {self.median_dbfs:6.1f} dBFS",
            f"  peak        : {self.peak_dbfs:6.1f} dBFS",
            f"  clipped samples: {self.clip_count}",
            f"  suggested detection threshold: "
            f"{self.recommended_threshold_dbfs:.1f} dBFS "
            f"({self.recommended_open_margin_db:.0f} dB above the floor)",
        ]
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        if self.ok and not self.warnings:
            lines.append("  input levels look healthy")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def calibrate(source: AudioSource, seconds: float = 5.0,
              frame_ms: float = 20.0) -> CalibrationResult:
    """Measure an idle channel and recommend a detection threshold.

    Run this with the radio on, squelch open or closed, but nobody talking.
    """
    frames: List[float] = []
    clip_count = 0
    peak = 0.0
    collected = 0.0
    started = time.monotonic()

    while collected < seconds:
        block = source.read(timeout=1.0)
        if block is None:
            if source.finished or (time.monotonic() - started) > seconds * 3 + 2:
                break
            continue
        collected += block.duration
        clip_count += block.clipped_samples
        peak = max(peak, block.peak)
        size = max(1, int(block.sample_rate * frame_ms / 1000.0))
        for start in range(0, max(1, block.samples.size - size + 1), size):
            frames.append(dbfs(rms(block.samples[start:start + size])))

    if not frames:
        return CalibrationResult(
            seconds=collected, noise_floor_dbfs=-120.0, median_dbfs=-120.0,
            peak_dbfs=-120.0, clip_count=0, recommended_threshold_dbfs=-45.0,
            recommended_open_margin_db=8.0, ok=False,
            warnings=["no audio was captured - is the right input selected?"],
        )

    levels = np.asarray(frames)
    floor = float(np.percentile(levels, 20))
    median = float(np.median(levels))
    peak_db = dbfs(peak)

    warnings: List[str] = []
    ok = True
    if peak_db < SILENT_DBFS:
        warnings.append(
            "input is silent - check the cable, the radio's volume and that the "
            "correct device is selected")
        ok = False
    elif floor > GOOD_FLOOR_RANGE[1]:
        warnings.append(
            "noise floor is high - turn the radio's volume or the interface gain "
            "down, or close the squelch")
    elif floor < GOOD_FLOOR_RANGE[0] and peak_db < GOOD_PEAK_RANGE[0]:
        warnings.append(
            "signal is very quiet - turn the radio's volume or the interface gain "
            "up so speech peaks land near -12 dBFS")
    if clip_count > 0 or peak_db > -0.5:
        warnings.append(
            f"clipping detected ({clip_count} samples) - reduce the input gain")
        ok = False

    # Open the gate a healthy margin above the measured floor, but never so low
    # that ordinary hiss keys it, nor so high that quiet traffic is missed.
    margin = 10.0 if floor < -55.0 else 8.0
    threshold = float(np.clip(floor + margin, -70.0, -20.0))
    return CalibrationResult(
        seconds=collected, noise_floor_dbfs=floor, median_dbfs=median,
        peak_dbfs=peak_db, clip_count=clip_count,
        recommended_threshold_dbfs=round(threshold, 1),
        recommended_open_margin_db=margin, warnings=warnings, ok=ok,
    )
