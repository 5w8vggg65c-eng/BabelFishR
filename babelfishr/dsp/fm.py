"""Demodulators used by the SDR path (complex IQ -> audio)."""

from __future__ import annotations

import numpy as np

from .filters import convolve, dc_block, design_fir_lowpass
from .resample import resample


def fm_demod(iq: np.ndarray, deviation_hz: float = 5000.0,
             sample_rate: float = 240_000.0) -> np.ndarray:
    """Quadrature (polar-discriminator) FM demodulation.

    The phase difference between consecutive IQ samples is proportional to
    instantaneous frequency; scaling by the deviation gives roughly unit-scale
    audio for a fully deviated carrier.
    """
    z = np.asarray(iq, dtype=np.complex128)
    if z.size < 2:
        return np.zeros(0, dtype=np.float64)
    product = z[1:] * np.conj(z[:-1])
    phase = np.angle(product)
    scale = sample_rate / (2.0 * np.pi * max(deviation_hz, 1.0))
    return (phase * scale).astype(np.float64)


def am_demod(iq: np.ndarray) -> np.ndarray:
    """Envelope detection for airband and CB AM."""
    env = np.abs(np.asarray(iq, dtype=np.complex128))
    return dc_block(env)


def ssb_demod(iq: np.ndarray, sample_rate: float, upper: bool = True) -> np.ndarray:
    """Weaver-free SSB: take the real part of the (frequency-shifted) analytic
    signal. Adequate for HF voice monitoring, not for contest-grade work."""
    z = np.asarray(iq, dtype=np.complex128)
    if z.size == 0:
        return np.zeros(0, dtype=np.float64)
    return (np.real(z) if upper else np.real(np.conj(z))).astype(np.float64)


def channelise(iq: np.ndarray, sample_rate: float, offset_hz: float,
               bandwidth_hz: float, audio_rate: int = 16_000) -> np.ndarray:
    """Mix a channel at *offset_hz* to baseband, filter it, demodulate NFM."""
    z = np.asarray(iq, dtype=np.complex128)
    if z.size == 0:
        return np.zeros(0, dtype=np.float64)
    t = np.arange(z.size, dtype=np.float64) / sample_rate
    mixed = z * np.exp(-2j * np.pi * offset_hz * t)
    kernel = design_fir_lowpass(bandwidth_hz / 2.0, sample_rate, 129)
    filtered = convolve(mixed.real, kernel) + 1j * convolve(mixed.imag, kernel)
    audio = fm_demod(filtered, deviation_hz=bandwidth_hz / 4.0, sample_rate=sample_rate)
    return resample(audio, sample_rate, audio_rate)


def power_spectrum(iq: np.ndarray, sample_rate: float, nfft: int = 4096):
    """Welch-ish averaged spectrum, returned as ``(freq_offsets_hz, dB)``.

    The scanner uses this to find which channels are actually busy before
    spending time demodulating them.
    """
    z = np.asarray(iq, dtype=np.complex128)
    if z.size < nfft:
        nfft = int(2 ** np.floor(np.log2(max(z.size, 2))))
        if nfft < 8:
            return np.zeros(0), np.zeros(0)
    window = np.hanning(nfft)
    n_blocks = max(1, z.size // nfft)
    acc = np.zeros(nfft, dtype=np.float64)
    for i in range(n_blocks):
        block = z[i * nfft:(i + 1) * nfft]
        if block.size < nfft:
            break
        acc += np.abs(np.fft.fftshift(np.fft.fft(block * window))) ** 2
    acc /= (n_blocks * (window.sum() ** 2))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / sample_rate))
    return freqs, 10.0 * np.log10(acc + 1e-20)
