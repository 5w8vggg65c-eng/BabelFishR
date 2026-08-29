"""Baudot RTTY decoder (45.45 baud / 170 Hz shift by default).

Still in daily use on HF, and occasionally on VHF for weather and NAVTEX-style
broadcasts.  Mark and space are recovered with the same correlator used by the
AFSK modem, then framed as 1 start bit, 5 data bits, 1.5 stop bits.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

from ..dsp.filters import moving_average
from ..dsp.goertzel import peak_frequency
from ..models import DecodeResult
from .afsk import correlate_tone
from .base import BaseDecoder, register

DEFAULT_BAUD = 45.45
DEFAULT_SHIFT = 170.0
DEFAULT_MARK = 2125.0

LETTERS = [
    "\0", "E", "\n", "A", " ", "S", "I", "U", "\r", "D", "R", "J", "N", "F",
    "C", "K", "T", "Z", "L", "W", "H", "Y", "P", "Q", "O", "B", "G", "\x0e",
    "M", "X", "V", "\x0f",
]
FIGURES = [
    "\0", "3", "\n", "-", " ", "'", "8", "7", "\r", "\x05", "4", "\x07", ",",
    "!", ":", "(", "5", "+", ")", "2", "$", "6", "0", "1", "9", "?", "&",
    "\x0e", ".", "/", ";", "\x0f",
]
LTRS_CODE = 0x1F
FIGS_CODE = 0x1B


def decode_baudot(codes: List[int]) -> str:
    """Baudot codes -> text, honouring LTRS/FIGS shift state."""
    out: List[str] = []
    figures = False
    for code in codes:
        code &= 0x1F
        if code == LTRS_CODE:
            figures = False
            continue
        if code == FIGS_CODE:
            figures = True
            continue
        ch = FIGURES[code] if figures else LETTERS[code]
        if ch and ch not in ("\0", "\x0e", "\x0f", "\x05", "\x07"):
            out.append(ch)
    return "".join(out)


def encode_baudot(text: str) -> List[int]:
    codes: List[int] = []
    figures = False
    for ch in text.upper():
        if ch in LETTERS and LETTERS.index(ch) not in (LTRS_CODE, FIGS_CODE):
            if figures and ch not in (" ", "\r", "\n"):
                codes.append(LTRS_CODE)
                figures = False
            codes.append(LETTERS.index(ch))
        elif ch in FIGURES:
            if not figures:
                codes.append(FIGS_CODE)
                figures = True
            codes.append(FIGURES.index(ch))
    return codes


class RttyDecoder(BaseDecoder):
    id = "rtty"
    name = "RTTY"
    description = "Baudot radioteletype, 45.45 baud / 170 Hz shift"
    sample_rate = 8000

    baud = DEFAULT_BAUD
    shift = DEFAULT_SHIFT
    min_printable = 8
    """Below this many printable characters we assume it was noise."""

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        best: Optional[Tuple[str, float, float]] = None
        for mark in self._candidate_marks(audio, sample_rate):
            # Only bother framing bits if the audio really is two-tone FSK at
            # this shift - otherwise speech decodes into convincing gibberish.
            if not self._fsk_present(audio, sample_rate, mark):
                continue
            for reverse in (False, True):
                space = mark + self.shift
                a, b = (space, mark) if reverse else (mark, space)
                text = self._decode_at(audio, sample_rate, a, b)
                score = self._score(text)
                if best is None or score > best[2]:
                    best = (text, mark, score)
        if best is None or best[2] <= 0:
            return []
        text, mark, score = best
        printable = sum(1 for c in text if c.isalnum() or c in " .,/-?")
        if printable < self.min_printable:
            return []
        return [DecodeResult(
            decoder=self.id, label=f"RTTY: {text.strip()[:120]}",
            confidence=float(np.clip(score, 0.0, 1.0)),
            duration=len(audio) / float(sample_rate),
            data={"text": text, "baud": self.baud, "shift": self.shift,
                  "mark_hz": round(mark, 1)},
        )]

    #: Fraction of in-band energy the mark+space pair must hold.
    min_tone_share = 0.45
    #: Each tone must hold at least this share on its own.
    min_single_share = 0.12

    def _fsk_present(self, audio: np.ndarray, sample_rate: int, mark: float) -> bool:
        x = np.asarray(audio, dtype=np.float64)
        if x.size < 512:
            return False
        n = int(2 ** np.ceil(np.log2(min(x.size, 32768))))
        spec = np.abs(np.fft.rfft(x[:n] * np.hanning(min(x.size, n)), n=n)) ** 2
        freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
        band = (freqs >= 300.0) & (freqs <= 3200.0)
        total = float(spec[band].sum())
        if total <= 1e-12:
            return False
        # A shift's worth of bandwidth around each tone (RTTY is narrow).
        half = max(self.baud * 1.5, 60.0)
        shares = []
        for tone in (mark, mark + self.shift):
            sel = (freqs >= tone - half) & (freqs <= tone + half)
            shares.append(float(spec[sel].sum()) / total)
        return (sum(shares) >= self.min_tone_share
                and min(shares) >= self.min_single_share)

    def _candidate_marks(self, audio: np.ndarray, sample_rate: int) -> List[float]:
        """Standard tone pairs plus whatever the strongest tone actually is."""
        marks = [DEFAULT_MARK, 1275.0, 1445.0]
        peak, _ = peak_frequency(audio, sample_rate, 400.0, 3000.0)
        if peak > 400.0:
            marks.append(peak)
            marks.append(peak - self.shift)
        return marks

    def _decode_at(self, audio: np.ndarray, sample_rate: int,
                   mark: float, space: float) -> str:
        sps = sample_rate / self.baud
        window = max(8, int(round(sps)))
        x = np.asarray(audio, dtype=np.float64)
        diff = correlate_tone(x, mark, sample_rate, window) - \
            correlate_tone(x, space, sample_rate, window)
        diff = moving_average(diff, max(2, window // 3))
        level = (diff > 0).astype(np.int8)

        codes: List[int] = []
        i = 0
        n = level.size
        limit = int(sps * 0.5)
        while i < n - int(sps * 7.5):
            # Hunt for a start bit: mark-to-space transition holding for a bit.
            if level[i] == 1 and level[min(i + limit, n - 1)] == 0:
                centre = i + sps * 1.5
                bits = []
                for k in range(5):
                    idx = int(round(centre + k * sps))
                    if idx >= n:
                        break
                    bits.append(int(level[idx]))
                if len(bits) == 5:
                    stop_idx = int(round(centre + 5 * sps))
                    if stop_idx < n and level[stop_idx] == 1:
                        codes.append(sum(b << j for j, b in enumerate(bits)))
                        i = int(round(centre + 6 * sps))
                        continue
            i += 1
        return decode_baudot(codes)

    def _score(self, text: str) -> float:
        if not text:
            return 0.0
        good = sum(1 for c in text if c.isalnum() or c in " .,:;/-?()+")
        return good / max(len(text), 1) * min(1.0, len(text) / 20.0)


def synthesize(text: str, sample_rate: int = 8000, baud: float = DEFAULT_BAUD,
               mark: float = DEFAULT_MARK, shift: float = DEFAULT_SHIFT,
               amplitude: float = 0.5, idle_bits: int = 10) -> np.ndarray:
    """Render text as RTTY audio (1 start, 5 data, 1.5 stop bits)."""
    space = mark + shift
    stream: List[Tuple[int, float]] = [(1, idle_bits)]  # idle = mark
    for code in encode_baudot(text):
        stream.append((0, 1.0))  # start bit (space)
        for j in range(5):
            stream.append(((code >> j) & 1, 1.0))
        stream.append((1, 1.5))  # stop
    stream.append((1, idle_bits))

    sps = sample_rate / baud
    freqs: List[np.ndarray] = []
    for level, length in stream:
        n = int(round(length * sps))
        freqs.append(np.full(n, mark if level else space))
    freq = np.concatenate(freqs)
    phase = 2 * np.pi * np.cumsum(freq) / sample_rate
    return amplitude * np.sin(phase)


register(RttyDecoder())
