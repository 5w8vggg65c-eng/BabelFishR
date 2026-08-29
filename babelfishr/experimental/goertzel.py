"""Goertzel single-bin DFT - the workhorse for tone signalling decoders.

Cheaper than an FFT when you only care about a handful of known frequencies,
which is exactly the case for CTCSS, DTMF, RTTY and selcall.
"""

from __future__ import annotations

from typing import Dict, Iterable, Sequence

import numpy as np


def goertzel_power(samples: np.ndarray, sample_rate: float, freq_hz: float) -> float:
    """Normalised power (0..~1 for a full-scale tone) at *freq_hz*."""
    n = samples.size
    if n == 0:
        return 0.0
    k = int(round(n * freq_hz / sample_rate))
    omega = 2.0 * np.pi * k / n
    coeff = 2.0 * np.cos(omega)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return float(power) / (n * n / 4.0)


def goertzel_bank(samples: np.ndarray, sample_rate: float,
                  freqs: Sequence[float]) -> np.ndarray:
    """Vectorised Goertzel over many frequencies at once.

    Rather than iterating the recurrence per frequency in Python, correlate
    against complex exponentials - identical result, far faster for a bank of
    38 CTCSS tones evaluated on every window.
    """
    x = np.asarray(samples, dtype=np.float64)
    n = x.size
    if n == 0 or not len(freqs):
        return np.zeros(len(freqs), dtype=np.float64)
    t = np.arange(n, dtype=np.float64) / sample_rate
    # (freqs, n) complex basis; windowing keeps close CTCSS tones separable.
    window = np.hanning(n)
    basis = np.exp(-2j * np.pi * np.asarray(freqs, dtype=np.float64)[:, None] * t[None, :])
    coherent_gain = window.sum()
    corr = basis @ (x * window)
    return (np.abs(corr) * 2.0 / max(coherent_gain, 1e-12)) ** 2


def tone_powers(samples: np.ndarray, sample_rate: float,
                freqs: Iterable[float]) -> Dict[float, float]:
    freqs = list(freqs)
    powers = goertzel_bank(samples, sample_rate, freqs)
    return {f: float(p) for f, p in zip(freqs, powers)}


def dominant_tone(samples: np.ndarray, sample_rate: float,
                  freqs: Sequence[float]) -> tuple:
    """Return ``(freq, power, ratio_to_runner_up)``."""
    powers = goertzel_bank(samples, sample_rate, freqs)
    if powers.size == 0:
        return (0.0, 0.0, 0.0)
    order = np.argsort(powers)[::-1]
    best = float(powers[order[0]])
    runner = float(powers[order[1]]) if powers.size > 1 else 0.0
    ratio = best / runner if runner > 1e-12 else float("inf")
    return (float(freqs[order[0]]), best, ratio)


def peak_frequency(samples: np.ndarray, sample_rate: float,
                   low_hz: float = 100.0, high_hz: float = 4000.0) -> tuple:
    """Coarse FFT peak with parabolic interpolation: ``(freq_hz, magnitude)``."""
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 32:
        return (0.0, 0.0)
    n = int(2 ** np.ceil(np.log2(x.size)))
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size), n=n))
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    if not mask.any():
        return (0.0, 0.0)
    idx = int(np.argmax(np.where(mask, spec, 0.0)))
    if 0 < idx < spec.size - 1:
        a, b, c = spec[idx - 1], spec[idx], spec[idx + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
    else:
        delta = 0.0
    bin_hz = sample_rate / n
    return (float((idx + delta) * bin_hz), float(spec[idx] * 2.0 / x.size))
