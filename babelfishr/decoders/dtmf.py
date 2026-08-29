"""DTMF decoder (ITU-T Q.23 tone pairs).

DTMF is everywhere on analogue radio: repeater control, autopatch, selective
calling, IRLP/EchoLink node commands, and phone-patch dialling.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from ..dsp.filters import rms
from ..dsp.goertzel import goertzel_bank
from ..models import DecodeResult
from .base import BaseDecoder, register

LOW_TONES = [697.0, 770.0, 852.0, 941.0]
HIGH_TONES = [1209.0, 1336.0, 1477.0, 1633.0]
KEYPAD = [
    ["1", "2", "3", "A"],
    ["4", "5", "6", "B"],
    ["7", "8", "9", "C"],
    ["*", "0", "#", "D"],
]
#: Second-harmonic frequencies, checked to reject speech that happens to have
#: energy at a DTMF pair (real DTMF is a pure sine pair with little harmonic).
_HARMONICS = [f * 2 for f in LOW_TONES + HIGH_TONES]


class DtmfDecoder(BaseDecoder):
    id = "dtmf"
    name = "DTMF"
    description = "Touch-tone keypad signalling (Q.23)"
    sample_rate = 8000

    window_ms = 25.0
    hop_ms = 10.0
    min_digit_ms = 40.0
    #: Allowed level difference between the row and column tone (dB).
    max_twist_db = 10.0
    #: Pair energy must be this many times the mean of the non-selected tones.
    min_dominance = 4.0
    min_level = 0.01

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        digits = self.detect(audio, sample_rate)
        if not digits:
            return []
        text = "".join(d["digit"] for d in digits)
        confidence = float(np.clip(np.mean([d["confidence"] for d in digits]), 0.0, 1.0))
        return [DecodeResult(
            decoder=self.id, label=f"DTMF: {text}", confidence=confidence,
            offset=digits[0]["start"],
            duration=digits[-1]["start"] + digits[-1]["duration"] - digits[0]["start"],
            data={"digits": text, "count": len(digits), "timing": digits},
        )]

    def detect(self, audio: np.ndarray, sample_rate: int) -> List[dict]:
        x = np.asarray(audio, dtype=np.float64)
        win = int(self.window_ms * sample_rate / 1000.0)
        hop = int(self.hop_ms * sample_rate / 1000.0)
        if x.size < win:
            return []

        frames: List[Optional[str]] = []
        strengths: List[float] = []
        for start in range(0, x.size - win + 1, hop):
            block = x[start:start + win]
            digit, strength = self._classify(block, sample_rate)
            frames.append(digit)
            strengths.append(strength)

        # Group runs of identical digits into keypresses.
        out: List[dict] = []
        i = 0
        min_frames = max(1, int(self.min_digit_ms / self.hop_ms))
        while i < len(frames):
            if frames[i] is None:
                i += 1
                continue
            j = i
            while j < len(frames) and frames[j] == frames[i]:
                j += 1
            run = j - i
            if run >= min_frames:
                out.append({
                    "digit": frames[i],
                    "start": i * hop / float(sample_rate),
                    "duration": (run * hop + win) / float(sample_rate),
                    "confidence": float(np.clip(np.mean(strengths[i:j]), 0.0, 1.0)),
                })
            i = j
        return out

    def _classify(self, block: np.ndarray, sample_rate: int):
        if rms(block) < self.min_level:
            return (None, 0.0)
        powers = goertzel_bank(block, sample_rate, LOW_TONES + HIGH_TONES + _HARMONICS)
        low = powers[:4]
        high = powers[4:8]
        harm = powers[8:16]

        li = int(np.argmax(low))
        hi = int(np.argmax(high))
        lp, hp = float(low[li]), float(high[hi])
        if lp <= 1e-9 or hp <= 1e-9:
            return (None, 0.0)

        twist = 10.0 * np.log10(lp / hp)
        if abs(twist) > self.max_twist_db:
            return (None, 0.0)

        others = [float(v) for k, v in enumerate(low) if k != li]
        others += [float(v) for k, v in enumerate(high) if k != hi]
        background = max(float(np.mean(others)), 1e-12)
        pair = min(lp, hp)
        if pair / background < self.min_dominance:
            return (None, 0.0)

        # Reject voice: genuine DTMF has very little second-harmonic energy.
        if float(harm[li]) > 0.25 * lp or float(harm[4 + hi]) > 0.25 * hp:
            return (None, 0.0)

        strength = float(np.clip(np.log10(pair / background) / 2.0, 0.0, 1.0))
        return (KEYPAD[li][hi], strength)


def synthesize(digits: str, sample_rate: int = 8000, tone_ms: float = 100.0,
               gap_ms: float = 60.0, amplitude: float = 0.4) -> np.ndarray:
    """Render a DTMF string to audio (tests and ``selftest``)."""
    lookup = {KEYPAD[r][c]: (LOW_TONES[r], HIGH_TONES[c])
              for r in range(4) for c in range(4)}
    parts: List[np.ndarray] = []
    gap = np.zeros(int(gap_ms * sample_rate / 1000.0))
    n = int(tone_ms * sample_rate / 1000.0)
    t = np.arange(n) / float(sample_rate)
    # Short raised-cosine edges avoid the spectral splatter of hard switching.
    edge = max(1, int(0.005 * sample_rate))
    ramp = np.ones(n)
    ramp[:edge] = np.linspace(0, 1, edge)
    ramp[-edge:] = np.linspace(1, 0, edge)
    for ch in digits:
        if ch not in lookup:
            parts.append(gap)
            continue
        f1, f2 = lookup[ch]
        tone = amplitude * (np.sin(2 * np.pi * f1 * t) + np.sin(2 * np.pi * f2 * t)) / 2.0
        parts.append(tone * ramp)
        parts.append(gap)
    return np.concatenate(parts) if parts else np.zeros(0)


register(DtmfDecoder())
