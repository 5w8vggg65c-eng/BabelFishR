"""Filter primitives. numpy only - scipy is never required at runtime."""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np


def design_fir_lowpass(cutoff_hz: float, sample_rate: float, numtaps: int = 101) -> np.ndarray:
    """Windowed-sinc low-pass kernel (Hamming window, linear phase)."""
    if numtaps % 2 == 0:
        numtaps += 1
    fc = max(1e-6, min(cutoff_hz / sample_rate, 0.499))
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = 2 * fc * np.sinc(2 * fc * n)
    h *= np.hamming(numtaps)
    return (h / np.sum(h)).astype(np.float64)


def design_fir_highpass(cutoff_hz: float, sample_rate: float, numtaps: int = 101) -> np.ndarray:
    if numtaps % 2 == 0:
        numtaps += 1
    lp = design_fir_lowpass(cutoff_hz, sample_rate, numtaps)
    # Spectral inversion of the low-pass gives the complementary high-pass.
    hp = -lp
    hp[(numtaps - 1) // 2] += 1.0
    return hp


def design_fir_bandpass(low_hz: float, high_hz: float, sample_rate: float,
                        numtaps: int = 101) -> np.ndarray:
    if numtaps % 2 == 0:
        numtaps += 1
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    f1 = max(1e-6, min(low_hz / sample_rate, 0.499))
    f2 = max(1e-6, min(high_hz / sample_rate, 0.499))
    h = 2 * f2 * np.sinc(2 * f2 * n) - 2 * f1 * np.sinc(2 * f1 * n)
    h *= np.hamming(numtaps)
    peak = np.abs(np.sum(h * np.exp(-2j * np.pi * ((f1 + f2) / 2) * n)))
    if peak > 0:
        h = h / peak
    return h.astype(np.float64)


def convolve(x: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Zero-delay FIR filtering: same length out, group delay compensated."""
    if x.size == 0:
        return x
    return np.convolve(x, kernel, mode="same")


def lowpass(x: np.ndarray, cutoff_hz: float, sample_rate: float, numtaps: int = 101) -> np.ndarray:
    return convolve(x, design_fir_lowpass(cutoff_hz, sample_rate, numtaps))


def highpass(x: np.ndarray, cutoff_hz: float, sample_rate: float, numtaps: int = 101) -> np.ndarray:
    return convolve(x, design_fir_highpass(cutoff_hz, sample_rate, numtaps))


def bandpass(x: np.ndarray, low_hz: float, high_hz: float, sample_rate: float,
             numtaps: int = 101) -> np.ndarray:
    return convolve(x, design_fir_bandpass(low_hz, high_hz, sample_rate, numtaps))


def dc_block(x: np.ndarray, pole: float = 0.999) -> np.ndarray:
    """Single-pole DC blocker, y[n] = x[n] - x[n-1] + p*y[n-1]."""
    if x.size == 0:
        return x
    return biquad(x, b=(1.0, -1.0, 0.0), a=(1.0, -pole, 0.0))


def biquad(x: np.ndarray, b: Tuple[float, float, float],
           a: Tuple[float, float, float]) -> np.ndarray:
    """Direct-form-I biquad. Uses scipy.lfilter when present, else a loop."""
    try:  # pragma: no cover - exercised only when scipy is installed
        from scipy.signal import lfilter

        return np.asarray(lfilter(np.asarray(b), np.asarray(a), x), dtype=np.float64)
    except Exception:
        pass
    y = np.zeros_like(x, dtype=np.float64)
    x1 = x2 = y1 = y2 = 0.0
    a0, a1, a2 = a
    b0, b1, b2 = b
    for i, xn in enumerate(np.asarray(x, dtype=np.float64)):
        yn = (b0 * xn + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2) / a0
        x2, x1 = x1, xn
        y2, y1 = y1, yn
        y[i] = yn
    return y


def deemphasis(x: np.ndarray, sample_rate: float, tau: float = 750e-6) -> np.ndarray:
    """Apply an RC de-emphasis curve (750 us NA / 50 us EU broadcast).

    Discriminator-tapped audio is flat; speaker audio is already de-emphasised.
    """
    alpha = math.exp(-1.0 / (sample_rate * tau))
    return biquad(x, b=(1.0 - alpha, 0.0, 0.0), a=(1.0, -alpha, 0.0))


def normalise(x: np.ndarray, headroom_db: float = -1.0) -> np.ndarray:
    """Scale to a peak of ``headroom_db`` dBFS without changing the waveform."""
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak <= 0:
        return x
    target = 10 ** (headroom_db / 20.0)
    return (x * (target / peak)).astype(np.float64)


def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def dbfs(value: float) -> float:
    """Amplitude (0..1) to dBFS, floored at -120."""
    return 20.0 * math.log10(value) if value > 1e-6 else -120.0


def frame(x: np.ndarray, size: int, hop: int) -> np.ndarray:
    """Non-copying framing view, shape (frames, size)."""
    if x.size < size:
        return np.empty((0, size), dtype=x.dtype)
    n = 1 + (x.size - size) // hop
    strides = (x.strides[0] * hop, x.strides[0])
    return np.lib.stride_tricks.as_strided(x, shape=(n, size), strides=strides, writeable=False)


def moving_average(x: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or x.size == 0:
        return x
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(x, kernel, mode="same")


def hilbert_envelope(x: np.ndarray) -> np.ndarray:
    """Analytic-signal magnitude, computed by FFT (no scipy dependency)."""
    n = x.size
    if n == 0:
        return x
    spectrum = np.fft.fft(x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1
        h[1:n // 2] = 2
    else:
        h[0] = 1
        h[1:(n + 1) // 2] = 2
    return np.abs(np.fft.ifft(spectrum * h))
