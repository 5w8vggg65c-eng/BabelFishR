"""A compact live magnitude spectrum of the input, for the visible meter.

The INPUT meter shows *what* is arriving as well as how loud it is: the
horizontal axis is audio frequency (log-spaced bands from ~60 Hz up to the
lower of 10 kHz and the input's Nyquist frequency), the vertical axis is the
band's magnitude in dBFS. A full-scale sine reads 0 dBFS in its band.

Where it runs: never on the PortAudio callback. The capture thread, which
already pulls every block off the source's queue to meter and detect it,
copies each block into a fixed-size ring (:meth:`SpectrumAnalyzer.feed`) and
computes a frame only when a level event is about to be published
(:meth:`SpectrumAnalyzer.frame`). Memory is fixed: one ``fft_size`` ring and
the current per-band smoothing state - no history is kept, and only the
latest frame travels on the latest level reading.
"""

from __future__ import annotations

import dataclasses
import functools
from typing import Optional, Tuple

import numpy as np

#: Bands across the axis. Enough to tell 300 Hz from 1 kHz from 3 kHz at a
#: glance, few enough to stay legible in a narrow pane.
DEFAULT_BANDS = 40
#: Lowest frequency shown: radio audio is high-passed well above this.
F_MIN_HZ = 60.0
#: Highest frequency shown, if the sample rate allows it.
F_MAX_HZ = 10_000.0
#: Samples analysed per frame (about 43 ms at 48 kHz).
FFT_SIZE = 2048
#: The floor every band rests on with no signal; also the axis bottom.
FLOOR_DBFS = -90.0
#: Bars fall at this rate once the sound in their band stops (they rise at
#: once): light smoothing that still reads as "live".
DECAY_DB_PER_S = 120.0
#: Peak caps hold this long, then fall at PEAK_DECAY_DB_PER_S.
PEAK_HOLD_S = 0.4
PEAK_DECAY_DB_PER_S = 90.0


@dataclasses.dataclass(frozen=True)
class SpectrumFrame:
    """One analysed moment. Immutable, small, safe to hand across threads."""

    #: Per band, dBFS (FLOOR_DBFS..0), smoothed.
    levels_dbfs: Tuple[float, ...]
    #: Per band, held peak dBFS.
    peaks_dbfs: Tuple[float, ...]
    #: Band edges in Hz: ``len(levels_dbfs) + 1`` ascending values.
    edges_hz: Tuple[float, ...]
    sample_rate: int

    @property
    def bands(self) -> int:
        return len(self.levels_dbfs)

    def fractions(self, floor_dbfs: float = FLOOR_DBFS) -> Tuple[float, ...]:
        """0..1 bar heights on a *floor_dbfs*..0 dBFS scale."""
        span = -float(floor_dbfs)
        return tuple(min(1.0, max(0.0, (level - floor_dbfs) / span))
                     for level in self.levels_dbfs)

    def peak_band(self) -> int:
        """Index of the loudest band."""
        return int(np.argmax(self.levels_dbfs))

    @property
    def silent(self) -> bool:
        return max(self.levels_dbfs, default=FLOOR_DBFS) <= FLOOR_DBFS + 1e-6


def band_edges(sample_rate: int, bands: int = DEFAULT_BANDS,
               f_min: float = F_MIN_HZ, f_max: float = F_MAX_HZ
               ) -> Tuple[float, ...]:
    """Log-spaced band edges from *f_min* to min(*f_max*, Nyquist)."""
    nyquist = float(sample_rate) / 2.0
    top = min(float(f_max), nyquist)
    bottom = min(float(f_min), top / 2.0)
    return tuple(float(x) for x in np.geomspace(bottom, top, int(bands) + 1))


@functools.lru_cache(maxsize=16)
def _plan(sample_rate: int, fft_size: int, bands: int):
    """Window, normalisation and per-band FFT bin ranges for one setup."""
    edges = band_edges(sample_rate, bands)
    window = np.blackman(fft_size)
    # Amplitude of a sine of peak A in its bin is A * sum(window) / 2.
    scale = 2.0 / float(np.sum(window))
    bin_hz = float(sample_rate) / fft_size
    last_bin = fft_size // 2
    ranges = []
    for low, high in zip(edges[:-1], edges[1:]):
        first = int(np.ceil(low / bin_hz))
        last = int(np.floor(high / bin_hz))
        if last < first:
            # A band narrower than one bin (the low end): its nearest bin.
            first = last = int(round(np.sqrt(low * high) / bin_hz))
        first = max(1, min(first, last_bin))
        last = max(first, min(last, last_bin))
        ranges.append((first, last + 1))
    starts = np.asarray([r[0] for r in ranges])
    stops = np.asarray([r[1] for r in ranges])
    return edges, window, scale, starts, stops


def band_levels(samples: np.ndarray, sample_rate: int,
                bands: int = DEFAULT_BANDS, fft_size: int = FFT_SIZE
                ) -> Tuple[np.ndarray, Tuple[float, ...]]:
    """Unsmoothed per-band dBFS of the latest *fft_size* samples.

    Blackman-windowed rFFT (low leakage, so a tone stays a narrow peak on
    a 90 dB scale); each band takes the strongest bin inside it, so a
    tone reads its own level whatever the band's width. Fewer samples than
    *fft_size* are zero-padded at the front (the oldest end).
    """
    data = np.asarray(samples, dtype=np.float64).ravel()[-fft_size:]
    edges, window, scale, starts, stops = _plan(int(sample_rate), int(fft_size),
                                                int(bands))
    frame = np.zeros(fft_size)
    if data.size:
        frame[-data.size:] = data
    magnitude = np.abs(np.fft.rfft(frame * window)) * scale
    # Strongest bin per band (a few dozen short slices).
    peaks = np.fromiter((magnitude[a:b].max() for a, b in zip(starts, stops)),
                        dtype=np.float64, count=len(starts))
    with np.errstate(divide="ignore"):
        levels = 20.0 * np.log10(np.maximum(peaks, 1e-12))
    return np.clip(levels, FLOOR_DBFS, 0.0), edges


def analyse(samples: np.ndarray, sample_rate: int, bands: int = DEFAULT_BANDS,
            fft_size: int = FFT_SIZE) -> SpectrumFrame:
    """A single, unsmoothed frame - the pure analysis, for tests and tools."""
    levels, edges = band_levels(samples, sample_rate, bands, fft_size)
    values = tuple(float(x) for x in levels)
    return SpectrumFrame(values, values, edges, int(sample_rate))


class SpectrumAnalyzer:
    """Keeps the latest ``fft_size`` samples and turns them into frames.

    Not thread-safe by design: one consumer thread (the capture thread)
    feeds it and asks for frames. Time is the *audio's* time - seconds of
    samples fed - so smoothing behaves the same in a faster-than-real-time
    replay as live, and never depends on the wall clock.
    """

    def __init__(self, bands: int = DEFAULT_BANDS, fft_size: int = FFT_SIZE):
        self.bands = int(bands)
        self.fft_size = int(fft_size)
        self._ring = np.zeros(self.fft_size)
        self._filled = 0
        self.sample_rate = 0
        self._samples_fed = 0
        self._levels = np.full(self.bands, FLOOR_DBFS)
        self._peaks = np.full(self.bands, FLOOR_DBFS)
        self._peak_age = np.zeros(self.bands)
        self._last_time: Optional[float] = None

    def reset(self) -> None:
        self._ring[:] = 0.0
        self._filled = 0
        self._samples_fed = 0
        self._levels[:] = FLOOR_DBFS
        self._peaks[:] = FLOOR_DBFS
        self._peak_age[:] = 0.0
        self._last_time = None

    @property
    def audio_time(self) -> float:
        return self._samples_fed / float(self.sample_rate or 1)

    def feed(self, samples: np.ndarray, sample_rate: int) -> None:
        """Copy a block into the ring. Cheap: no FFT here."""
        if int(sample_rate) != self.sample_rate:
            # A different input or rate: nothing measured before applies.
            self.sample_rate = int(sample_rate)
            self.reset()
        data = np.asarray(samples, dtype=np.float64).ravel()
        count = data.size
        if not count:
            return
        self._samples_fed += count
        if count >= self.fft_size:
            self._ring[:] = data[-self.fft_size:]
        else:
            self._ring[:-count] = self._ring[count:]
            self._ring[-count:] = data
        self._filled = min(self.fft_size, self._filled + count)

    def frame(self) -> Optional[SpectrumFrame]:
        """The current smoothed frame, or None before any audio arrived."""
        if not self.sample_rate or not self._filled:
            return None
        raw, edges = band_levels(self._ring[-self._filled:], self.sample_rate,
                                 self.bands, self.fft_size)
        now = self.audio_time
        elapsed = 0.0 if self._last_time is None else max(0.0, now - self._last_time)
        self._last_time = now
        # Rise at once, fall at DECAY_DB_PER_S.
        self._levels = np.maximum(raw, self._levels - DECAY_DB_PER_S * elapsed)
        self._levels = np.clip(self._levels, FLOOR_DBFS, 0.0)
        # Peak caps: hold, then fall.
        self._peak_age += elapsed
        falling = self._peak_age > PEAK_HOLD_S
        self._peaks = np.where(falling,
                               self._peaks - PEAK_DECAY_DB_PER_S * elapsed,
                               self._peaks)
        higher = self._levels >= self._peaks
        self._peaks = np.where(higher, self._levels, self._peaks)
        self._peak_age = np.where(higher, 0.0, self._peak_age)
        self._peaks = np.clip(self._peaks, FLOOR_DBFS, 0.0)
        return SpectrumFrame(tuple(float(x) for x in self._levels),
                             tuple(float(x) for x in self._peaks),
                             edges, self.sample_rate)


__all__ = ["SpectrumFrame", "SpectrumAnalyzer", "analyse", "band_edges",
           "band_levels", "DEFAULT_BANDS", "FFT_SIZE", "FLOOR_DBFS",
           "F_MIN_HZ", "F_MAX_HZ"]
