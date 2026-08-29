"""Bell 202 AFSK1200 modem: the physical layer under APRS and packet radio.

1200 baud, 1200 Hz mark / 2200 Hz space, NRZI, HDLC framed.  This is the most
common "digital transmission on an analogue radio" in the VHF/UHF world, and it
demodulates perfectly well from ordinary speaker audio.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

from ..dsp.filters import bandpass, moving_average
from ..models import DecodeResult
from . import hdlc
from .base import BaseDecoder, register

MARK_HZ = 1200.0
SPACE_HZ = 2200.0
BAUD = 1200.0


def correlate_tone(x: np.ndarray, freq: float, sample_rate: float,
                   window: int) -> np.ndarray:
    """Sliding-window magnitude of *x* at *freq* (a one-bin DFT per sample)."""
    n = np.arange(window, dtype=np.float64)
    kernel = np.exp(-2j * np.pi * freq * n / sample_rate)
    real = np.convolve(x, kernel.real, mode="same")
    imag = np.convolve(x, kernel.imag, mode="same")
    return np.hypot(real, imag)


def demodulate(audio: np.ndarray, sample_rate: int, mark: float = MARK_HZ,
               space: float = SPACE_HZ, baud: float = BAUD) -> np.ndarray:
    """AFSK -> a baseband NRZ waveform (positive = mark)."""
    x = np.asarray(audio, dtype=np.float64)
    if x.size < 32:
        return np.zeros(0, dtype=np.float64)
    sps = sample_rate / baud
    window = max(4, int(round(sps)))
    # Restrict to the modem passband first: this rejects CTCSS underneath and
    # any hum, both of which would bias the correlator.
    filtered = bandpass(x, min(mark, space) - 500.0, max(mark, space) + 500.0,
                        sample_rate, numtaps=97)
    diff = correlate_tone(filtered, mark, sample_rate, window) - \
        correlate_tone(filtered, space, sample_rate, window)
    # Smooth over half a bit to knock down the tone-rate ripple.
    return moving_average(diff, max(2, window // 2))


def recover_bits(nrz: np.ndarray, sample_rate: int, baud: float = BAUD,
                 gain: float = 0.35) -> np.ndarray:
    """Digital PLL clock recovery -> one hard symbol per bit period."""
    if nrz.size == 0:
        return np.zeros(0, dtype=np.int8)
    sps = sample_rate / baud
    sliced = (nrz > 0).astype(np.int8)
    out: List[int] = []
    phase = sps / 2.0
    previous = sliced[0]
    for level in sliced:
        phase -= 1.0
        if phase <= 0.0:
            out.append(int(level))
            phase += sps
        if level != previous:
            # An edge tells us where the bit boundary is; steer the sampling
            # instant toward the middle of the next bit.
            phase += (sps / 2.0 - phase) * gain
            previous = level
    return np.array(out, dtype=np.int8)


def decode_frames(audio: np.ndarray, sample_rate: int, baud: float = BAUD,
                  mark: float = MARK_HZ, space: float = SPACE_HZ
                  ) -> List[Tuple[bytes, bool]]:
    """Return ``(frame_bytes, fcs_ok)`` for every HDLC frame found.

    Both signal polarities are tried, because whether a radio inverts the
    discriminator output varies by model and by where you tap the audio.
    """
    nrz = demodulate(audio, sample_rate, mark, space, baud)
    seen: dict = {}
    for polarity in (1.0, -1.0):
        symbols = recover_bits(nrz * polarity, sample_rate, baud)
        bits = hdlc.nrzi_decode(symbols)
        for _, frame in hdlc.find_frames(bits):
            ok = hdlc.fcs_ok(frame)
            if frame not in seen or ok:
                seen[frame] = ok
    return sorted(seen.items(), key=lambda kv: (not kv[1], len(kv[0])))


class Afsk1200Decoder(BaseDecoder):
    id = "afsk1200"
    name = "AFSK1200 / AX.25"
    description = "Bell 202 packet radio: APRS, packet BBS, KISS links"
    sample_rate = 9600  # exactly 8 samples per bit

    keep_bad_fcs = False

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        from .aprs import describe_payload

        out: List[DecodeResult] = []
        for frame, ok in decode_frames(audio, sample_rate):
            if not ok and not self.keep_bad_fcs:
                continue
            parsed = hdlc.parse_ax25(frame)
            if parsed is None:
                continue
            data = parsed.to_dict()
            data["raw_hex"] = frame.hex()
            aprs = describe_payload(parsed.info)
            if aprs:
                data["aprs"] = aprs
            label = f"APRS {parsed.summary()}" if aprs else f"AX.25 {parsed.summary()}"
            out.append(DecodeResult(
                decoder=self.id, label=label,
                confidence=1.0 if ok else 0.4,
                duration=len(audio) / float(sample_rate), data=data,
            ))
        return out


def synthesize(frame: bytes, sample_rate: int = 9600, baud: float = BAUD,
               amplitude: float = 0.5, mark: float = MARK_HZ,
               space: float = SPACE_HZ, flags_before: int = 16) -> np.ndarray:
    """Modulate an HDLC frame as continuous-phase AFSK (tests / selftest)."""
    levels = hdlc.frame_to_bits(frame, flags_before=flags_before)
    sps = sample_rate / baud
    n = int(round(len(levels) * sps))
    idx = np.minimum((np.arange(n) / sps).astype(int), len(levels) - 1)
    freqs = np.where(np.array(levels, dtype=np.int8)[idx] == 1, mark, space)
    # Continuous phase: integrate instantaneous frequency, never jump.
    phase = 2 * np.pi * np.cumsum(freqs) / sample_rate
    return amplitude * np.sin(phase)


register(Afsk1200Decoder())
