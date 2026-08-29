"""Digital voice mode classifier (DMR, P25, D-STAR, NXDN, System Fusion).

What this does and does not do
------------------------------
It *identifies* a digital voice transmission from its baseband signature and
records it, so the operator's log says "16:04:12 - DMR, 462.6250, 8.2 s" rather
than "unintelligible noise".  It does **not** decode the speech: DMR, P25,
D-STAR, NXDN and Fusion all carry AMBE/IMBE vocoder frames, which are patented
and have no freely redistributable decoder.  If you need audio from those
systems, feed the recorded IQ/discriminator audio to a tool that is licensed
for it (DSD+, or an AMBE hardware dongle) - BabelFishR deliberately ships no
vocoder.

Classification uses three signatures that survive an FM discriminator:

* symbol rate, from the spacing of level transitions;
* how many discrete symbol levels the eye has (2 for GMSK, 4 for C4FM);
* TDMA burst periodicity - DMR keys 30 ms on / 30 ms off, giving a strong
  ~16.7 Hz amplitude modulation that FDMA modes do not have.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional

import numpy as np

from ..dsp.filters import hilbert_envelope, lowpass, moving_average
from ..models import DecodeResult
from .base import BaseDecoder, register


@dataclasses.dataclass
class Profile:
    name: str
    symbol_rate: float
    levels: int
    tdma: bool = False
    notes: str = ""


PROFILES: List[Profile] = [
    Profile("DMR / MOTOTRBO", 4800.0, 4, tdma=True, notes="2-slot TDMA, 12.5 kHz"),
    Profile("P25 Phase 1 (C4FM)", 4800.0, 4, notes="FDMA, 12.5 kHz"),
    Profile("System Fusion (C4FM)", 4800.0, 4, notes="Yaesu C4FM"),
    Profile("D-STAR", 4800.0, 2, notes="GMSK, 6.25 kHz equivalent"),
    Profile("NXDN 4800", 2400.0, 4, notes="C4FM, 6.25 kHz"),
    Profile("dPMR", 2400.0, 4, notes="C4FM, 6.25 kHz"),
]

DMR_BURST_HZ = 1.0 / 0.06  # 30 ms slot on, 30 ms off


class DigitalVoiceDecoder(BaseDecoder):
    id = "digital-voice"
    name = "Digital voice classifier"
    description = "Identifies DMR / P25 / D-STAR / NXDN bursts (no vocoder)"
    sample_rate = 24000

    min_confidence = 0.35

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        features = self.features(audio, sample_rate)
        if features is None:
            return []
        matches = self._match(features)
        if not matches:
            return []
        best, score = matches[0]
        alternatives = [m.name for m, _ in matches[1:3]]
        note = ("Identified only - BabelFishR ships no AMBE/IMBE vocoder, "
                "so the voice payload is recorded but not transcribed.")
        return [DecodeResult(
            decoder=self.id,
            label=f"Digital voice: {best.name} (not decodable to audio)",
            confidence=float(np.clip(score, 0.0, 1.0)),
            duration=len(audio) / float(sample_rate),
            data={"mode": best.name, "alternatives": alternatives,
                  "symbol_rate": round(features["symbol_rate"], 1),
                  "levels": features["levels"], "tdma": features["tdma"],
                  "tdma_strength": round(features["tdma_strength"], 3),
                  "flatness": round(features["flatness"], 3),
                  "note": note, "vocoder": "AMBE/IMBE (proprietary)"},
        )]

    def features(self, audio: np.ndarray, sample_rate: int) -> Optional[Dict[str, object]]:
        x = np.asarray(audio, dtype=np.float64)
        if x.size < sample_rate * 0.2:
            return None
        x = x - float(np.mean(x))
        peak = float(np.max(np.abs(x)))
        if peak < 1e-5:
            return None
        x = x / peak

        symbol_rate = self._symbol_rate(x, sample_rate)
        if symbol_rate <= 0:
            return None
        levels = self._level_count(x, sample_rate, symbol_rate)
        tdma_strength = self._tdma_strength(x, sample_rate)
        flatness = self._spectral_flatness(x)
        return {
            "symbol_rate": symbol_rate, "levels": levels,
            "tdma": tdma_strength > 0.25, "tdma_strength": tdma_strength,
            "flatness": flatness,
        }

    def _symbol_rate(self, x: np.ndarray, sample_rate: int) -> float:
        """Median transition spacing -> symbol rate (robust to missing edges)."""
        sign = np.sign(x)
        sign[sign == 0] = 1
        crossings = np.flatnonzero(np.diff(sign)) 
        if crossings.size < 20:
            return 0.0
        gaps = np.diff(crossings).astype(np.float64)
        gaps = gaps[gaps > 0]
        if gaps.size < 10:
            return 0.0
        # The shortest recurring gap is one symbol period.
        unit = float(np.percentile(gaps, 15))
        if unit <= 0:
            return 0.0
        return sample_rate / unit

    def _level_count(self, x: np.ndarray, sample_rate: int, symbol_rate: float) -> int:
        """Count eye-diagram modes: 2 (GMSK) vs 4 (C4FM)."""
        sps = max(2, int(round(sample_rate / max(symbol_rate, 1.0))))
        smoothed = moving_average(x, max(2, sps // 2))
        samples = smoothed[sps // 2::sps]
        if samples.size < 40:
            return 0
        hist, edges = np.histogram(samples, bins=41, range=(-1.0, 1.0))
        hist = moving_average(hist.astype(np.float64), 3)
        threshold = 0.25 * float(hist.max())
        peaks = 0
        for i in range(1, hist.size - 1):
            if hist[i] >= threshold and hist[i] >= hist[i - 1] and hist[i] > hist[i + 1]:
                peaks += 1
        return peaks

    def _tdma_strength(self, x: np.ndarray, sample_rate: int) -> float:
        """Energy at the DMR 16.7 Hz burst rate, relative to the envelope."""
        env = hilbert_envelope(x)
        env = lowpass(env, 200.0, sample_rate, numtaps=127)
        env = env - float(np.mean(env))
        if env.size < sample_rate // 4 or float(np.std(env)) < 1e-9:
            return 0.0
        spec = np.abs(np.fft.rfft(env * np.hanning(env.size)))
        freqs = np.fft.rfftfreq(env.size, 1.0 / sample_rate)
        band = (freqs > 5.0) & (freqs < 60.0)
        if not band.any():
            return 0.0
        target = (freqs > DMR_BURST_HZ - 2.0) & (freqs < DMR_BURST_HZ + 2.0)
        if not target.any():
            return 0.0
        return float(spec[target].max() / (spec[band].mean() * 8.0 + 1e-12))

    def _spectral_flatness(self, x: np.ndarray) -> float:
        """Geometric/arithmetic mean ratio: noise-like digital modes score high."""
        spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2 + 1e-20
        return float(np.exp(np.mean(np.log(spec))) / np.mean(spec))

    def _match(self, features: Dict[str, object]):
        rate = float(features["symbol_rate"])
        levels = int(features["levels"])
        scored = []
        for profile in PROFILES:
            rate_error = abs(rate - profile.symbol_rate) / profile.symbol_rate
            if rate_error > 0.45:
                continue
            score = (1.0 - rate_error) * 0.5
            if levels and abs(levels - profile.levels) <= 1:
                score += 0.25
            if profile.tdma and features["tdma"]:
                score += 0.25
            elif not profile.tdma and not features["tdma"]:
                score += 0.10
            if float(features["flatness"]) > 0.15:
                score += 0.10
            scored.append((profile, min(score, 0.95)))
        scored.sort(key=lambda kv: -kv[1])
        return [s for s in scored if s[1] >= self.min_confidence]


def synthesize(symbol_rate: float = 4800.0, levels: int = 4, duration: float = 1.0,
               sample_rate: int = 24000, tdma: bool = False,
               amplitude: float = 0.6, seed: int = 0) -> np.ndarray:
    """Render a synthetic C4FM/GMSK-like discriminator waveform for tests."""
    rng = np.random.default_rng(seed)
    n_symbols = int(duration * symbol_rate)
    if levels == 2:
        deviations = np.array([-1.0, 1.0])
    else:
        deviations = np.array([-1.0, -1.0 / 3.0, 1.0 / 3.0, 1.0])
    symbols = rng.choice(deviations, size=n_symbols)
    sps = sample_rate / symbol_rate
    idx = np.minimum((np.arange(int(n_symbols * sps)) / sps).astype(int), n_symbols - 1)
    wave = symbols[idx]
    # Root-raised-cosine-ish smoothing, as a real modulator would apply.
    wave = moving_average(wave, max(2, int(sps)))
    if tdma:
        t = np.arange(wave.size) / float(sample_rate)
        gate = (np.sin(2 * np.pi * DMR_BURST_HZ * t) > 0).astype(np.float64)
        gate = moving_average(gate, max(2, int(sample_rate * 0.002)))
        wave = wave * (0.15 + 0.85 * gate)
    peak = float(np.max(np.abs(wave))) or 1.0
    return amplitude * wave / peak


register(DigitalVoiceDecoder())
