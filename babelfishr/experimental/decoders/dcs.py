"""DCS / DPL (Digital-Coded Squelch) decoder.

DCS replaces the CTCSS tone with a continuous 134.4 bit/s sub-audible NRZ
stream carrying a 23-bit Golay(23,12) codeword.  The 12 data bits are the
9-bit octal code plus the three fixed bits ``1 0 0``; the remaining 11 bits are
Golay parity.  The word repeats forever, so a receiver can lock on at any point
- which is why this decoder searches every bit alignment.

Generator polynomial: x^11 + x^9 + x^7 + x^6 + x^5 + x + 1 (0xAE3).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from ...dsp.filters import lowpass
from ..results import DecodeResult
from .base import BaseDecoder, register

BIT_RATE = 134.4
GOLAY_POLY = 0xAE3  # x^11 + x^9 + x^7 + x^6 + x^5 + x + 1
CODEWORD_BITS = 23
DATA_BITS = 12
FIXED_BITS = 0b100  # occupies data bits 9..11

#: The 83 standard DCS codes, as octal strings.
DCS_CODES: List[str] = [
    "023", "025", "026", "031", "032", "036", "043", "047", "051", "053",
    "054", "065", "071", "072", "073", "074", "114", "115", "116", "122",
    "125", "131", "132", "134", "143", "145", "152", "155", "156", "162",
    "165", "172", "174", "205", "212", "223", "225", "226", "243", "244",
    "245", "246", "251", "252", "255", "261", "263", "265", "266", "271",
    "274", "306", "311", "315", "325", "331", "332", "343", "346", "351",
    "356", "364", "365", "371", "411", "412", "413", "423", "431", "432",
    "445", "446", "452", "454", "455", "462", "464", "465", "466", "503",
    "506", "516", "523", "526", "532", "546", "565", "606", "612", "624",
    "627", "631", "632", "654", "662", "664", "703", "712", "723", "731",
    "732", "734", "743", "754",
]


def golay_encode(data12: int) -> int:
    """Systematic Golay(23,12): 12 data bits (LSB-aligned) -> 23-bit codeword."""
    remainder = data12 << 11
    for shift in range(DATA_BITS - 1, -1, -1):
        if remainder & (1 << (shift + 11)):
            remainder ^= GOLAY_POLY << shift
    return (data12 << 11) | (remainder & 0x7FF)


def code_to_word(octal_code: str) -> int:
    """DCS octal code (e.g. ``"023"``) -> its 23-bit transmitted word."""
    value = int(octal_code, 8) & 0x1FF
    data = value | (FIXED_BITS << 9)
    return golay_encode(data)


def _build_table() -> Dict[int, str]:
    return {code_to_word(c): c for c in DCS_CODES}


CODEWORDS: Dict[int, str] = _build_table()


def word_bits(word: int) -> np.ndarray:
    """23-bit word to a bit array in transmission order (LSB first)."""
    return np.array([(word >> i) & 1 for i in range(CODEWORD_BITS)], dtype=np.int8)


def bits_to_word(bits: np.ndarray) -> int:
    value = 0
    for i, b in enumerate(bits[:CODEWORD_BITS]):
        value |= int(b) << i
    return value


class DcsDecoder(BaseDecoder):
    id = "dcs"
    name = "DCS / DPL"
    description = "Digital coded squelch, 134.4 bps sub-audible Golay word"
    sample_rate = 8000

    min_votes = 3
    """Codeword repetitions that must agree (~0.5 s of signal)."""

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        result = self.analyse(audio, sample_rate)
        if result is None:
            return []
        code, votes, total, inverted = result
        confidence = float(np.clip(votes / max(total, 1), 0.0, 1.0))
        return [DecodeResult(
            decoder=self.id, label=f"DCS {code}" + (" (inverted)" if inverted else ""),
            confidence=confidence, duration=len(audio) / float(sample_rate),
            data={"code": code, "octal": code, "inverted": inverted,
                  "votes": votes, "windows": total},
        )]

    def analyse(self, audio: np.ndarray, sample_rate: int
                ) -> Optional[Tuple[str, int, int, bool]]:
        x = np.asarray(audio, dtype=np.float64)
        samples_per_bit = sample_rate / BIT_RATE
        if x.size < samples_per_bit * CODEWORD_BITS * self.min_votes:
            return None

        # Isolate the sub-audible band and remove any DC/CTCSS-rate wander.
        sub = lowpass(x, 260.0, sample_rate, numtaps=255)
        sub = sub - float(np.mean(sub))
        if float(np.max(np.abs(sub))) < 1e-5:
            return None

        best: Optional[Tuple[str, int, int, bool]] = None
        # Try several sampling phases; the true bit clock is not aligned to
        # our buffer start and 8000/134.4 is not an integer.
        for phase_step in range(6):
            phase = phase_step * samples_per_bit / 6.0
            bits = self._slice_bits(sub, samples_per_bit, phase)
            if bits.size < CODEWORD_BITS * 2:
                continue
            for inverted in (False, True):
                candidate = self._vote(bits ^ 1 if inverted else bits)
                if candidate is None:
                    continue
                code, votes, total = candidate
                if best is None or votes > best[1]:
                    best = (code, votes, total, inverted)
        if best is None or best[1] < self.min_votes:
            return None
        return best

    def _slice_bits(self, sub: np.ndarray, samples_per_bit: float,
                    phase: float) -> np.ndarray:
        """Integrate-and-dump slicer: average each bit period, take the sign."""
        n_bits = int((sub.size - phase) / samples_per_bit)
        if n_bits <= 0:
            return np.zeros(0, dtype=np.int8)
        edges = phase + np.arange(n_bits + 1) * samples_per_bit
        idx = np.round(edges).astype(int)
        idx = np.clip(idx, 0, sub.size)
        cumulative = np.concatenate([[0.0], np.cumsum(sub)])
        sums = cumulative[idx[1:]] - cumulative[idx[:-1]]
        widths = np.maximum(idx[1:] - idx[:-1], 1)
        return (sums / widths > 0).astype(np.int8)

    def _vote(self, bits: np.ndarray) -> Optional[Tuple[str, int, int]]:
        counts: Dict[str, int] = {}
        total = 0
        for start in range(0, bits.size - CODEWORD_BITS + 1):
            total += 1
            code = CODEWORDS.get(bits_to_word(bits[start:start + CODEWORD_BITS]))
            if code is not None:
                counts[code] = counts.get(code, 0) + 1
        if not counts:
            return None
        code, votes = max(counts.items(), key=lambda kv: kv[1])
        # Normalise votes against the number of whole codewords in the buffer.
        periods = max(1, bits.size // CODEWORD_BITS)
        return (code, votes, periods)


def synthesize(code: str, duration: float, sample_rate: int = 8000,
               amplitude: float = 0.15, inverted: bool = False) -> np.ndarray:
    """Render a continuous DCS sub-audible stream for *code*."""
    bits = word_bits(code_to_word(code))
    if inverted:
        bits = bits ^ 1
    samples_per_bit = sample_rate / BIT_RATE
    n = int(duration * sample_rate)
    idx = (np.arange(n) / samples_per_bit).astype(int) % bits.size
    nrz = np.where(bits[idx] == 1, 1.0, -1.0)
    # Real transmitters band-limit the square wave into the sub-audible slot.
    shaped = lowpass(nrz, 250.0, sample_rate, numtaps=127)
    peak = float(np.max(np.abs(shaped))) or 1.0
    return amplitude * shaped / peak


register(DcsDecoder())
