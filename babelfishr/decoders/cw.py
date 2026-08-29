"""Morse (CW) decoder with automatic speed tracking."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from ..dsp.filters import bandpass, hilbert_envelope, moving_average
from ..dsp.goertzel import peak_frequency
from ..models import DecodeResult
from .base import BaseDecoder, register

MORSE: Dict[str, str] = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F",
    "--.": "G", "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L",
    "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", "-..-.": "/", "-....-": "-",
    "-.--.": "(", "-.--.-": ")", ".----.": "'", "---...": ":", "-.-.-.": ";",
    "-...-": "=", ".-.-.": "+", ".--.-.": "@", "...-.-": "<SK>",
    "-.-.-": "<KA>", "...-.": "<SN>", "........": "<ERR>",
}
REVERSE_MORSE = {v: k for k, v in MORSE.items() if len(v) == 1}


class CwDecoder(BaseDecoder):
    id = "cw"
    name = "CW / Morse"
    description = "Morse code with adaptive speed estimation"
    sample_rate = 8000

    min_elements = 6
    min_tone_hz = 250.0
    max_tone_hz = 1800.0

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        result = self.analyse(audio, sample_rate)
        if result is None:
            return []
        text, wpm, tone, quality = result
        stripped = text.strip()
        # Speech and noise can key a threshold detector into plausible-looking
        # element runs; a low quality score is the tell.
        if len(stripped) < 2 or quality < 0.25:
            return []
        return [DecodeResult(
            decoder=self.id, label=f"CW ({wpm:.0f} wpm): {stripped[:120]}",
            confidence=float(np.clip(quality, 0.0, 1.0)),
            duration=len(audio) / float(sample_rate),
            data={"text": stripped, "wpm": round(wpm, 1), "tone_hz": round(tone, 1)},
        )]

    def analyse(self, audio: np.ndarray, sample_rate: int
                ) -> Optional[Tuple[str, float, float, float]]:
        x = np.asarray(audio, dtype=np.float64)
        if x.size < sample_rate * 0.3:
            return None
        tone, _ = peak_frequency(x, sample_rate, self.min_tone_hz, self.max_tone_hz)
        if tone < self.min_tone_hz:
            return None

        filtered = bandpass(x, max(80.0, tone - 200.0), tone + 200.0, sample_rate, 201)
        envelope = moving_average(hilbert_envelope(filtered), max(2, int(sample_rate * 0.004)))
        if float(np.max(envelope)) <= 1e-6:
            return None

        keyed = self._key(envelope)
        runs = self._runs(keyed, sample_rate)
        marks = [d for state, d in runs if state]
        if len(marks) < self.min_elements:
            return None

        dit = self._estimate_dit(marks)
        if dit <= 0:
            return None
        text = self._to_text(runs, dit)
        wpm = 1.2 / dit if dit > 0 else 0.0
        quality = self._quality(text, marks, dit)
        return (text, wpm, tone, quality)

    def _key(self, envelope: np.ndarray) -> np.ndarray:
        """Split the envelope into key-down / key-up using its own histogram."""
        hi = float(np.percentile(envelope, 90))
        lo = float(np.percentile(envelope, 10))
        if hi <= lo * 1.5:
            return np.zeros_like(envelope, dtype=np.int8)
        threshold = lo + 0.4 * (hi - lo)
        return (envelope > threshold).astype(np.int8)

    def _runs(self, keyed: np.ndarray, sample_rate: int) -> List[Tuple[bool, float]]:
        if keyed.size == 0:
            return []
        edges = np.flatnonzero(np.diff(keyed)) + 1
        bounds = np.concatenate([[0], edges, [keyed.size]])
        out: List[Tuple[bool, float]] = []
        for start, end in zip(bounds[:-1], bounds[1:]):
            out.append((bool(keyed[start]), (end - start) / float(sample_rate)))
        # Drop leading/trailing silence, it carries no timing information.
        while out and not out[0][0]:
            out.pop(0)
        while out and not out[-1][0]:
            out.pop()
        return out

    def _estimate_dit(self, marks: List[float]) -> float:
        """Two-class split of mark lengths into dits and dahs."""
        arr = np.sort(np.asarray(marks))
        if arr.size == 0:
            return 0.0
        # A dah is nominally 3 dits: split where the ratio jumps the most.
        best_split, best_gap = 0, 0.0
        for i in range(1, arr.size):
            gap = arr[i] / max(arr[i - 1], 1e-6)
            if gap > best_gap:
                best_gap, best_split = gap, i
        if best_gap < 1.8:  # all one class - assume they are all dits
            return float(np.median(arr))
        dits = arr[:best_split]
        return float(np.median(dits)) if dits.size else float(arr[0])

    def _to_text(self, runs: List[Tuple[bool, float]], dit: float) -> str:
        symbols: List[str] = []
        letters: List[str] = []
        words: List[str] = []
        for state, duration in runs:
            units = duration / dit
            if state:
                symbols.append("." if units < 2.0 else "-")
                continue
            if units < 2.0:
                continue  # inter-element gap
            letters.append(MORSE.get("".join(symbols), "?") if symbols else "")
            symbols = []
            if units >= 5.0:
                words.append("".join(letters))
                letters = []
        if symbols:
            letters.append(MORSE.get("".join(symbols), "?"))
        if letters:
            words.append("".join(letters))
        return " ".join(w for w in words if w)

    def _quality(self, text: str, marks: List[float], dit: float) -> float:
        if not text:
            return 0.0
        unknown = text.count("?")
        ratio = 1.0 - unknown / max(len(text.replace(" ", "")), 1)
        # Consistent element lengths are the other half of "this is really CW".
        units = np.asarray(marks) / max(dit, 1e-6)
        tightness = float(np.mean(np.minimum(
            np.abs(units - np.round(np.clip(units, 1, 3))), 1.0)))
        return float(np.clip(ratio * (1.0 - tightness), 0.0, 1.0))


def synthesize(text: str, wpm: float = 18.0, tone_hz: float = 700.0,
               sample_rate: int = 8000, amplitude: float = 0.5) -> np.ndarray:
    """Render text as CW audio with raised-cosine keying edges."""
    dit = 1.2 / wpm
    parts: List[np.ndarray] = []

    def tone(duration: float, on: bool) -> None:
        n = int(round(duration * sample_rate))
        if n <= 0:
            return
        if not on:
            parts.append(np.zeros(n))
            return
        t = np.arange(n) / float(sample_rate)
        wave = amplitude * np.sin(2 * np.pi * tone_hz * t)
        edge = max(1, int(0.004 * sample_rate))
        if n > 2 * edge:
            ramp = np.ones(n)
            ramp[:edge] = np.sin(np.linspace(0, np.pi / 2, edge)) ** 2
            ramp[-edge:] = np.cos(np.linspace(0, np.pi / 2, edge)) ** 2
            wave *= ramp
        parts.append(wave)

    tone(dit * 3, False)
    for word in text.upper().split():
        for ch in word:
            code = REVERSE_MORSE.get(ch)
            if code is None:
                continue
            for i, element in enumerate(code):
                if i:
                    tone(dit, False)
                tone(dit if element == "." else dit * 3, True)
            tone(dit * 3, False)  # inter-character gap
        tone(dit * 4, False)  # makes the inter-word gap 7 dits total
    tone(dit * 3, False)
    return np.concatenate(parts) if parts else np.zeros(0)


register(CwDecoder())
