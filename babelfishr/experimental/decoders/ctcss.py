"""CTCSS / PL sub-audible tone squelch decoder.

CTCSS puts a continuous 67-254 Hz tone under the voice.  Knowing it tells you
which repeater or talkgroup a transmission belongs to, and lets the operator
gate recording to just their own group.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from ...dsp.filters import lowpass, rms
from ..goertzel import goertzel_bank
from ..results import DecodeResult
from .base import BaseDecoder, register

#: The 50 standard CTCSS tones in Hz (EIA 38 plus the common extensions).
CTCSS_TONES: List[float] = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
    171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5,
    203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
]

#: Motorola "PL" designators for the standard tones.
PL_NAMES = {
    67.0: "XZ", 69.3: "WZ", 71.9: "XA", 74.4: "WA", 77.0: "XB", 79.7: "WB",
    82.5: "YZ", 85.4: "YA", 88.5: "YB", 91.5: "ZZ", 94.8: "ZA", 97.4: "ZB",
    100.0: "1Z", 103.5: "1A", 107.2: "1B", 110.9: "2Z", 114.8: "2A",
    118.8: "2B", 123.0: "3Z", 127.3: "3A", 131.8: "3B", 136.5: "4Z",
    141.3: "4A", 146.2: "4B", 151.4: "5Z", 156.7: "5A", 162.2: "5B",
    167.9: "6Z", 173.8: "6A", 179.9: "6B", 186.2: "7Z", 192.8: "7A",
    203.5: "M1", 210.7: "M2", 218.1: "M3", 225.7: "M4", 233.6: "M5",
    241.8: "M6", 250.3: "M7",
}


class CtcssDecoder(BaseDecoder):
    id = "ctcss"
    name = "CTCSS / PL tone"
    description = "Continuous sub-audible tone squelch (67-254 Hz)"
    sample_rate = 8000

    #: A tone must dominate for at least this fraction of the transmission.
    min_presence = 0.55
    #: Ratio between the winning tone and the next best before we believe it.
    min_ratio = 2.5
    window_seconds = 0.5

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        tone, presence, ratio, level = self.analyse(audio, sample_rate)
        if tone is None:
            return []
        confidence = float(np.clip(presence * min(ratio / self.min_ratio, 1.0), 0.0, 1.0))
        name = PL_NAMES.get(tone)
        label = f"CTCSS {tone:.1f} Hz" + (f" (PL {name})" if name else "")
        return [DecodeResult(
            decoder=self.id, label=label, confidence=confidence,
            duration=len(audio) / float(sample_rate),
            data={"tone_hz": tone, "pl_name": name, "presence": round(presence, 3),
                  "ratio": round(min(ratio, 999.0), 2), "level_dbfs": round(level, 1)},
        )]

    def analyse(self, audio: np.ndarray, sample_rate: int):
        """Return ``(tone_hz|None, presence, ratio, level_dbfs)``."""
        x = np.asarray(audio, dtype=np.float64)
        if x.size < sample_rate * 0.4:
            return (None, 0.0, 0.0, -120.0)
        # Keep only the sub-audible band; voice energy above 300 Hz would
        # otherwise swamp a tone that sits 10-20 dB below the audio.
        sub = lowpass(x, 300.0, sample_rate, numtaps=255)

        win = int(self.window_seconds * sample_rate)
        hop = win // 2
        votes: dict = {}
        levels: List[float] = []
        n_windows = 0
        for start in range(0, max(1, sub.size - win + 1), hop):
            block = sub[start:start + win]
            if block.size < win:
                break
            n_windows += 1
            powers = goertzel_bank(block, sample_rate, CTCSS_TONES)
            order = np.argsort(powers)[::-1]
            best_i = int(order[0])
            best = float(powers[best_i])
            # Compare against the best tone that is not an adjacent bin.
            runner = 0.0
            for idx in order[1:]:
                if abs(int(idx) - best_i) > 1:
                    runner = float(powers[int(idx)])
                    break
            if best <= 1e-9:
                continue
            ratio = best / runner if runner > 1e-12 else 999.0
            if ratio >= self.min_ratio:
                tone = CTCSS_TONES[best_i]
                entry = votes.setdefault(tone, [0, 0.0])
                entry[0] += 1
                entry[1] += ratio
                levels.append(float(np.sqrt(best)))

        if not votes or n_windows == 0:
            return (None, 0.0, 0.0, -120.0)
        tone, (count, ratio_sum) = max(votes.items(), key=lambda kv: kv[1][0])
        presence = count / float(n_windows)
        if presence < self.min_presence:
            return (None, presence, 0.0, -120.0)
        level = 20.0 * np.log10(max(float(np.mean(levels)), 1e-6)) if levels else -120.0
        return (tone, presence, ratio_sum / count, level)


def nearest_tone(freq_hz: float, tolerance: float = 1.5) -> Optional[float]:
    """Snap a measured frequency to the standard tone table."""
    best = min(CTCSS_TONES, key=lambda t: abs(t - freq_hz))
    return best if abs(best - freq_hz) <= tolerance else None


def synthesize(tone_hz: float, duration: float, sample_rate: int = 8000,
               amplitude: float = 0.15) -> np.ndarray:
    """Generate a CTCSS tone (used by tests and ``babelfishr selftest``)."""
    t = np.arange(int(duration * sample_rate)) / float(sample_rate)
    return amplitude * np.sin(2 * np.pi * tone_hz * t)


register(CtcssDecoder())
