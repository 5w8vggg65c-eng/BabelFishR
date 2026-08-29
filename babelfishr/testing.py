"""Synthetic radio-audio fixtures.

Used by the test suite and by ``babelfishr selftest`` to exercise the whole
pipeline without a radio.  These are *simulations*, not recordings: they
reproduce the gross statistics that matter to the detector (syllabic envelope,
spectral shape, squelch noise bursts) so segmentation logic can be tested
deterministically.  They are not a substitute for validating against off-air
audio.
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .dsp.filters import bandpass, lowpass, moving_average


def silence(seconds: float, sample_rate: int = 48_000,
            noise_dbfs: float = -60.0, seed: int = 0) -> np.ndarray:
    """An idle channel: never digital silence, always a little hiss."""
    rng = np.random.default_rng(seed)
    amplitude = 10 ** (noise_dbfs / 20.0)
    return rng.normal(0.0, amplitude, int(seconds * sample_rate))


def speech_like(seconds: float, sample_rate: int = 48_000, level_dbfs: float = -14.0,
                syllable_rate: float = 4.0, seed: int = 1,
                bandwidth: Tuple[float, float] = (300.0, 3000.0)) -> np.ndarray:
    """Band-limited, syllabically modulated noise - a stand-in for voice.

    Communications audio is band-limited to roughly 300-3000 Hz, and speech has
    a 3-5 Hz syllabic envelope. Both matter to the detector, so both are here.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * sample_rate)
    if n <= 0:
        return np.zeros(0)
    t = np.arange(n) / float(sample_rate)

    # Voiced excitation plus breath noise, shaped into formant-ish bands.
    excitation = rng.normal(0.0, 1.0, n)
    voice = np.zeros(n)
    for centre, weight in ((500.0, 1.0), (1200.0, 0.6), (2400.0, 0.3)):
        voice += weight * bandpass(excitation, centre * 0.75, centre * 1.25,
                                   sample_rate, numtaps=127)
    voice = bandpass(voice, bandwidth[0], bandwidth[1], sample_rate, numtaps=127)

    # Syllabic envelope with occasional short gaps, as between words.
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * syllable_rate * t + rng.uniform(0, 6))
    envelope *= 0.6 + 0.4 * np.sin(2 * np.pi * (syllable_rate / 3.1) * t)
    envelope = np.clip(envelope, 0.05, 1.0)

    signal = voice * envelope
    peak = float(np.max(np.abs(signal))) or 1.0
    return signal / peak * (10 ** (level_dbfs / 20.0))


def squelch_tail(sample_rate: int = 48_000, seconds: float = 0.18,
                 level_dbfs: float = -12.0, seed: int = 2) -> np.ndarray:
    """The burst of broadband noise an open-squelch radio emits on carrier drop."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sample_rate)
    noise = rng.normal(0.0, 1.0, n)
    # Bright, harsh, and decaying - the characteristic "kssh".
    noise = noise - lowpass(noise, 800.0, sample_rate, numtaps=63)
    envelope = np.linspace(1.0, 0.2, n) ** 1.5
    signal = noise * envelope
    peak = float(np.max(np.abs(signal))) or 1.0
    return signal / peak * (10 ** (level_dbfs / 20.0))


def static_burst(seconds: float, sample_rate: int = 48_000,
                 level_dbfs: float = -20.0, seed: int = 3) -> np.ndarray:
    """Broadband noise: a fade, picket-fencing, or an unsquelched channel."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sample_rate)
    noise = rng.normal(0.0, 1.0, n)
    peak = float(np.max(np.abs(noise))) or 1.0
    return noise / peak * (10 ** (level_dbfs / 20.0))


def tone(seconds: float, frequency: float = 1000.0, sample_rate: int = 48_000,
         level_dbfs: float = -12.0) -> np.ndarray:
    """A courtesy beep / roger tone."""
    t = np.arange(int(seconds * sample_rate)) / float(sample_rate)
    return (10 ** (level_dbfs / 20.0)) * np.sin(2 * np.pi * frequency * t)


def digital_burst(seconds: float, sample_rate: int = 48_000, symbol_rate: float = 4800.0,
                  levels: int = 4, level_dbfs: float = -14.0, seed: int = 4
                  ) -> np.ndarray:
    """A 4FSK/C4FM-shaped burst as it would appear after FM demodulation.

    Digital voice (DMR, P25, NXDN) leaves a discriminator as a multi-level
    baseband waveform clocked at a fixed symbol rate. This reproduces that
    gross shape - broadband, level-stationary, but highly regular in its zero
    crossings - which is exactly the combination the detector must not confuse
    with static.

    A simulation, not a real burst: it is shaped to exercise classification and
    routing, and proves nothing about decoding a real signal.
    """
    rng = np.random.default_rng(seed)
    n_symbols = max(1, int(seconds * symbol_rate))
    deviations = (np.array([-1.0, 1.0]) if levels == 2
                  else np.array([-1.0, -1.0 / 3.0, 1.0 / 3.0, 1.0]))
    symbols = rng.choice(deviations, size=n_symbols)
    samples_per_symbol = sample_rate / symbol_rate
    index = np.minimum((np.arange(int(n_symbols * samples_per_symbol))
                        / samples_per_symbol).astype(int), n_symbols - 1)
    wave = symbols[index]
    # Real modulators pulse-shape; a raw staircase is unrealistically sharp.
    wave = moving_average(wave, max(2, int(samples_per_symbol)))
    peak = float(np.max(np.abs(wave))) or 1.0
    return wave / peak * (10 ** (level_dbfs / 20.0))


@dataclasses.dataclass
class SimulatedTransmission:
    """Ground truth for one transmission placed in a fixture."""

    start: float
    duration: float
    kind: str = "voice"
    language: Optional[str] = None
    text: str = ""


@dataclasses.dataclass
class Fixture:
    audio: np.ndarray
    sample_rate: int
    transmissions: List[SimulatedTransmission]

    @property
    def duration(self) -> float:
        return self.audio.size / float(self.sample_rate)

    def write(self, path: str, bit_depth: int = 16) -> str:
        from .audio.wavefile import write_wav

        return write_wav(path, self.audio, self.sample_rate, bit_depth)


def build_fixture(spec: Sequence[dict], sample_rate: int = 48_000,
                  idle_noise_dbfs: float = -58.0, seed: int = 0) -> Fixture:
    """Assemble a fixture from a list of segment descriptions.

    Each entry is ``{"gap": seconds}`` for idle time, or a transmission such as
    ``{"kind": "voice", "duration": 3.0, "level_dbfs": -14, "tail": True}``.
    """
    parts: List[np.ndarray] = []
    events: List[SimulatedTransmission] = []
    position = 0.0
    counter = seed * 100

    for entry in spec:
        counter += 1
        if "gap" in entry:
            block = silence(entry["gap"], sample_rate, idle_noise_dbfs, seed=counter)
            parts.append(block)
            position += block.size / sample_rate
            continue

        kind = entry.get("kind", "voice")
        duration = float(entry.get("duration", 2.0))
        level = float(entry.get("level_dbfs", -14.0))
        if kind == "voice":
            body = speech_like(duration, sample_rate, level,
                               syllable_rate=entry.get("syllable_rate", 4.0),
                               seed=counter)
        elif kind == "static":
            body = static_burst(duration, sample_rate, level, seed=counter)
        elif kind == "digital":
            body = digital_burst(duration, sample_rate,
                                 entry.get("symbol_rate", 4800.0),
                                 entry.get("levels", 4), level, seed=counter)
        elif kind == "tone":
            body = tone(duration, entry.get("frequency", 1000.0), sample_rate, level)
        elif kind == "silence":
            body = silence(duration, sample_rate, idle_noise_dbfs, seed=counter)
        else:
            raise ValueError(f"unknown fixture kind: {kind}")

        if entry.get("clip"):
            body = np.clip(body * float(entry.get("clip_gain", 8.0)), -1.0, 1.0)

        start = position
        parts.append(body)
        position += body.size / sample_rate

        if entry.get("tail", False):
            tail = squelch_tail(sample_rate, entry.get("tail_seconds", 0.18),
                                entry.get("tail_level_dbfs", -12.0), seed=counter)
            parts.append(tail)
            position += tail.size / sample_rate

        events.append(SimulatedTransmission(
            start=start, duration=duration, kind=kind,
            language=entry.get("language"), text=entry.get("text", ""),
        ))

    audio = np.concatenate(parts) if parts else np.zeros(0)
    return Fixture(audio=audio, sample_rate=sample_rate, transmissions=events)


def standard_fixture(sample_rate: int = 48_000) -> Fixture:
    """The canonical multi-transmission fixture used across the test suite.

    Five real transmissions with squelch tails, separated by idle noise, plus a
    static burst and a brief crackle that must *not* be counted.
    """
    return build_fixture([
        {"gap": 1.5},
        {"kind": "voice", "duration": 3.0, "level_dbfs": -14, "tail": True,
         "language": "es", "text": "equipo uno en posicion"},
        {"gap": 2.0},
        {"kind": "voice", "duration": 1.2, "level_dbfs": -16, "tail": True,
         "language": "en", "text": "roger that"},
        {"gap": 1.8},
        {"kind": "static", "duration": 0.9, "level_dbfs": -24},
        {"gap": 1.5},
        {"kind": "voice", "duration": 4.5, "level_dbfs": -12, "tail": True,
         "language": "de", "text": "achtung strassensperre voraus"},
        {"gap": 2.2},
        {"kind": "voice", "duration": 0.06, "level_dbfs": -18},
        {"gap": 1.4},
        {"kind": "voice", "duration": 2.2, "level_dbfs": -20, "tail": True,
         "language": "fr", "text": "message recu"},
        {"gap": 1.6},
        {"kind": "voice", "duration": 2.6, "level_dbfs": -10, "clip": True,
         "tail": True, "language": "en", "text": "loud and overdriven"},
        {"gap": 1.5},
    ], sample_rate=sample_rate)


def gapped_transmission_fixture(sample_rate: int = 48_000) -> Fixture:
    """One transmission containing natural pauses - must stay a single event."""
    return build_fixture([
        {"gap": 1.5},
        {"kind": "voice", "duration": 1.4, "level_dbfs": -14},
        {"kind": "silence", "duration": 0.35},
        {"kind": "voice", "duration": 1.6, "level_dbfs": -14},
        {"kind": "silence", "duration": 0.4},
        {"kind": "voice", "duration": 1.3, "level_dbfs": -14, "tail": True},
        {"gap": 1.5},
    ], sample_rate=sample_rate)
