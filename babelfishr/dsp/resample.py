"""Sample-rate conversion and channel folding."""

from __future__ import annotations

from fractions import Fraction

import numpy as np

from .filters import convolve, design_fir_lowpass


def to_mono(x: np.ndarray) -> np.ndarray:
    """Average interleaved/2-D multichannel audio down to one channel."""
    a = np.asarray(x)
    if a.ndim == 1:
        return a.astype(np.float64, copy=False)
    return a.mean(axis=1).astype(np.float64)


def resample(x: np.ndarray, src_rate: float, dst_rate: float,
             max_denominator: int = 1000) -> np.ndarray:
    """Rational resampling by upsample -> anti-alias filter -> decimate.

    ASR wants 16 kHz, most decoders want 8 kHz, and soundcards hand us 44.1 or
    48 kHz, so this runs on every transmission.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0 or abs(src_rate - dst_rate) < 1e-9:
        return x
    ratio = Fraction(dst_rate / src_rate).limit_denominator(max_denominator)
    up, down = ratio.numerator, ratio.denominator
    if up == 0:
        return np.zeros(0, dtype=np.float64)

    if up > 1:
        upsampled = np.zeros(x.size * up, dtype=np.float64)
        upsampled[::up] = x * up
    else:
        upsampled = x

    intermediate_rate = src_rate * up
    cutoff = 0.45 * min(src_rate, dst_rate)
    taps = _taps_for(intermediate_rate, cutoff)
    filtered = convolve(upsampled, design_fir_lowpass(cutoff, intermediate_rate, taps))
    return filtered[::down] if down > 1 else filtered


def _taps_for(rate: float, cutoff: float) -> int:
    """Enough taps for a sharp transition without silly cost on long buffers."""
    taps = int(rate / max(cutoff, 1.0) * 8) | 1
    return int(np.clip(taps, 31, 511))


def resample_poly_int(x: np.ndarray, up: int, down: int) -> np.ndarray:
    """Integer-ratio variant used by the SDR path where rates are exact."""
    return resample(x, float(down), float(up))


def decimate(x: np.ndarray, factor: int) -> np.ndarray:
    if factor <= 1:
        return x
    taps = _taps_for(1.0, 0.5 / factor)
    return convolve(x, design_fir_lowpass(0.45 / factor, 1.0, taps))[::factor]


def float_to_pcm16(x: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def pcm16_to_float(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float64) / 32768.0
