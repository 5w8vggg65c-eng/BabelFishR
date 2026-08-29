"""Transmission detection: turning a continuous stream into discrete events.

This is not general-purpose voice activity detection.  Radio audio has
properties that break conversational VAD:

* **Squelch tails.** When the carrier drops, an open-squelch receiver dumps a
  burst of loud white noise.  Ordinary VAD hears "energy" and keeps the gate
  open, tacking a rasp onto the end of every recording.
* **Static and picket-fencing.** A mobile station in a fade produces bursts of
  broadband noise that look exactly like speech to an energy detector.
* **Signalling tones.** Roger beeps, courtesy tones and DTMF are real parts of a
  transmission, but they are not speech and should not be transcribed as such.
* **Clipped speech.** Overdriven radio audio flattens peaks, so peak-based
  measures saturate; the noise floor and RMS still carry the information.
* **Very short transmissions.** A two-word "go ahead" is a legitimate event, but
  a 60 ms squelch crackle is not.

The detector therefore tracks the channel noise floor, opens on a margin above
it, holds through the pauses inside speech, trims the squelch tail off the end,
and reports a *confidence* plus content flags so downstream stages can decide
whether transcription is even worth attempting.
"""

from __future__ import annotations

import abc
import dataclasses
import datetime as _dt
from collections import deque
from typing import Deque, List, Optional

import numpy as np

from .audio.source import AudioBlock
from .dsp.filters import dbfs, rms
from .models import utcnow


@dataclasses.dataclass
class DetectorSettings:
    """Everything an operator may need to tune for their radio and interface."""

    mode: str = "auto"
    """``auto`` tracks the noise floor; ``fixed`` uses ``threshold_dbfs``."""

    threshold_dbfs: float = -45.0
    open_margin_db: float = 8.0
    """dB above the tracked floor at which a transmission is declared."""

    close_margin_db: float = 4.0
    """Hysteresis, so a fading signal does not chatter the gate."""

    hang_time: float = 0.8
    """Seconds of quiet tolerated inside one transmission before it ends."""

    pre_roll: float = 0.30
    """Audio kept from *before* detection, so the first word is never clipped."""

    post_roll: float = 0.20
    """Audio kept after the signal drops."""

    min_duration: float = 0.30
    """Shorter events are discarded as crackle."""

    max_duration: float = 300.0
    """A stuck transmitter is split rather than filling the disk."""

    frame_ms: float = 20.0
    open_frames: int = 2
    """Consecutive loud frames required to open - rejects single clicks."""

    trim_squelch_tail: bool = True
    squelch_tail_flatness: float = 0.42
    """Spectral flatness above which a trailing frame is treated as noise."""

    max_tail_trim: float = 0.60
    """Never trim more than this - a tail-trimmer must not eat a transmission."""

    reject_noise: bool = True
    """Discard events classified as pure static rather than logging them."""

    noise_flatness: float = 0.25
    """In-band flatness above which an event *may* be static (see modulation)."""

    min_modulation_db: float = 3.0
    """Speech swings in level syllable to syllable; stationary noise does not.

    Standard deviation of frame levels, in dB, below which an event is
    considered stationary. Speech typically measures 6-15 dB, static 1-3 dB.
    """

    tone_flatness: float = 0.008
    """Below this, with no modulation, the event is a steady signalling tone."""

    def validate(self) -> "DetectorSettings":
        if self.open_margin_db <= self.close_margin_db:
            raise ValueError("open_margin_db must exceed close_margin_db")
        if self.pre_roll < 0 or self.post_roll < 0:
            raise ValueError("pre_roll and post_roll must be >= 0")
        if self.min_duration <= 0:
            raise ValueError("min_duration must be > 0")
        return self


@dataclasses.dataclass
class DetectedTransmission:
    """One detected event, with the audio and the evidence behind it."""

    audio: np.ndarray
    sample_rate: int
    started_at: _dt.datetime
    start_offset: float
    duration: float
    peak_dbfs: float
    rms_dbfs: float
    noise_floor_dbfs: float
    active_ratio: float
    confidence: float
    clipped: bool = False
    likely_noise: bool = False
    """Broadband and structureless: probably static, not a voice."""

    likely_tone: bool = False
    """Dominated by a steady tone: a beep or signalling, not speech."""

    modulation_db: float = 0.0
    """Spread of frame levels in dB - the syllabic signature of speech."""

    flatness: float = 0.0
    """Median in-band spectral flatness: ~0 tonal, ~0.5 broadband noise."""

    truncated: bool = False
    trimmed_tail: float = 0.0
    """Seconds of squelch tail removed from the end."""

    @property
    def snr_db(self) -> float:
        return self.peak_dbfs - self.noise_floor_dbfs

    @property
    def ended_at(self) -> _dt.datetime:
        return self.started_at + _dt.timedelta(seconds=self.duration)

    @property
    def worth_transcribing(self) -> bool:
        """Cheap gate so the ASR engine is not fed static or a courtesy beep."""
        return (not self.likely_noise and not self.likely_tone
                and self.duration >= 0.25)

    def to_dict(self) -> dict:
        d = {k: v for k, v in dataclasses.asdict(self).items() if k != "audio"}
        d["snr_db"] = self.snr_db
        d["samples"] = int(self.audio.size)
        return d


class TransmissionDetector(abc.ABC):
    """Interface for anything that cuts a stream into transmissions."""

    @abc.abstractmethod
    def push(self, block: AudioBlock) -> List[DetectedTransmission]:
        """Consume a block; return transmissions completed by it."""

    @abc.abstractmethod
    def flush(self) -> List[DetectedTransmission]:
        """End of stream: emit anything still open."""

    @abc.abstractmethod
    def reset(self) -> None:
        ...

    @property
    @abc.abstractmethod
    def open(self) -> bool:
        """True while a transmission is in progress."""


class NoiseFloorTracker:
    """Falls quickly toward quiet, rises slowly - never chases a talker up."""

    def __init__(self, fall_db_s: float = 24.0, rise_db_s: float = 1.5):
        self.value = -70.0
        self.fall_rate = fall_db_s
        self.rise_rate = rise_db_s
        self._initialised = False

    def update(self, level_dbfs: float, dt: float) -> float:
        if not self._initialised:
            self.value = level_dbfs
            self._initialised = True
        elif level_dbfs < self.value:
            self.value = max(level_dbfs, self.value - self.fall_rate * dt)
        else:
            self.value = min(level_dbfs, self.value + self.rise_rate * dt)
        return self.value

    def reset(self) -> None:
        self.value = -70.0
        self._initialised = False


#: Communications audio occupies roughly this band; everything outside it is
#: filter skirt, and including it makes every band-limited signal look tonal.
SPEECH_BAND = (200.0, 3800.0)


def spectral_flatness(frame: np.ndarray, sample_rate: int,
                      band: tuple = SPEECH_BAND) -> float:
    """Geometric/arithmetic mean of the power spectrum *within the audio band*.

    Near 1 for white noise (static, squelch tail), near 0 for a pure tone,
    typically 0.1-0.4 for speech.

    Restricting to the band matters: radio audio is band-limited to about
    300-3000 Hz, so measured across a 24 kHz spectrum the empty bins dominate
    the geometric mean and *everything* scores as a pure tone.
    """
    if frame.size < 32:
        return 0.0
    spectrum = np.abs(np.fft.rfft(frame * np.hanning(frame.size))) ** 2
    freqs = np.fft.rfftfreq(frame.size, 1.0 / sample_rate)
    selected = spectrum[(freqs >= band[0]) & (freqs <= band[1])]
    if selected.size < 4:
        return 0.0
    selected = selected + 1e-20
    return float(np.exp(np.mean(np.log(selected))) / np.mean(selected))


def high_frequency_ratio(frame: np.ndarray, sample_rate: int,
                         split_hz: float = 2000.0) -> float:
    """Share of energy above *split_hz*; squelch noise is markedly HF-heavy."""
    if frame.size < 32:
        return 0.0
    spectrum = np.abs(np.fft.rfft(frame * np.hanning(frame.size))) ** 2
    freqs = np.fft.rfftfreq(frame.size, 1.0 / sample_rate)
    total = float(spectrum.sum())
    if total <= 1e-20:
        return 0.0
    return float(spectrum[freqs >= split_hz].sum() / total)


class RadioActivityDetector(TransmissionDetector):
    """Energy gate with radio-specific content analysis."""

    def __init__(self, sample_rate: int, settings: Optional[DetectorSettings] = None):
        self.sample_rate = int(sample_rate)
        self.settings = (settings or DetectorSettings()).validate()
        self.floor = NoiseFloorTracker()

        self.frame_size = max(1, int(self.sample_rate * self.settings.frame_ms / 1000.0))
        self.frame_dt = self.frame_size / float(self.sample_rate)
        self._pre_roll: Deque[np.ndarray] = deque(
            maxlen=max(1, int(round(self.settings.pre_roll / self.frame_dt))))

        self._pending = np.zeros(0, dtype=np.float64)
        self._open = False
        self._consecutive = 0
        self._buffer: List[np.ndarray] = []
        self._frame_stats: List[dict] = []
        self._active_frames = 0
        self._total_frames = 0
        self._peak = 0.0
        self._clipped = False
        self._floor_at_open = -120.0
        self._samples_seen = 0
        self._pre_samples = 0
        self._start_offset = 0.0
        self._hang_remaining = 0.0
        self._last_active_samples = 0
        self.stream_start: Optional[_dt.datetime] = None

    # -- thresholds ------------------------------------------------------
    def open_threshold(self) -> float:
        if self.settings.mode == "fixed":
            return self.settings.threshold_dbfs
        return self.floor.value + self.settings.open_margin_db

    def close_threshold(self) -> float:
        if self.settings.mode == "fixed":
            return self.settings.threshold_dbfs - self.settings.close_margin_db
        return self.floor.value + self.settings.close_margin_db

    @property
    def open(self) -> bool:
        return self._open

    @property
    def noise_floor_dbfs(self) -> float:
        return self.floor.value

    def reset(self) -> None:
        self.__init__(self.sample_rate, self.settings)  # noqa: PLC2801

    # -- streaming -------------------------------------------------------
    def push(self, block: AudioBlock) -> List[DetectedTransmission]:
        if self.stream_start is None:
            self.stream_start = block.timestamp - _dt.timedelta(seconds=block.offset)
        samples = np.asarray(block.samples, dtype=np.float64).ravel()
        self._pending = (np.concatenate([self._pending, samples])
                         if self._pending.size else samples)

        out: List[DetectedTransmission] = []
        count = self._pending.size // self.frame_size
        for i in range(count):
            frame = self._pending[i * self.frame_size:(i + 1) * self.frame_size]
            found = self._process(frame)
            if found is not None:
                out.append(found)
        if count:
            self._pending = self._pending[count * self.frame_size:]
        return out

    def flush(self) -> List[DetectedTransmission]:
        out: List[DetectedTransmission] = []
        if self._open:
            if self._pending.size:
                self._buffer.append(self._pending.copy())
            found = self._close()
            if found is not None:
                out.append(found)
        self._pending = np.zeros(0, dtype=np.float64)
        return out

    # -- internals -------------------------------------------------------
    def _process(self, frame: np.ndarray) -> Optional[DetectedTransmission]:
        level = dbfs(rms(frame))
        self._samples_seen += frame.size
        completed: Optional[DetectedTransmission] = None

        if not self._open:
            self.floor.update(level, self.frame_dt)

        loud = level >= self.open_threshold()
        self._consecutive = self._consecutive + 1 if loud else 0

        if not self._open:
            self._pre_roll.append(frame.copy())
            if self._consecutive >= self.settings.open_frames:
                self._start()
            return None

        self._buffer.append(frame.copy())
        self._total_frames += 1
        self._peak = max(self._peak, float(np.max(np.abs(frame))))
        if np.any(np.abs(frame) >= 0.999):
            self._clipped = True
        self._frame_stats.append({
            "level": level,
            "flatness": spectral_flatness(frame, self.sample_rate),
            "hf_ratio": high_frequency_ratio(frame, self.sample_rate),
            "samples": int(sum(b.size for b in self._buffer)),
        })
        if loud:
            self._active_frames += 1

        if level < self.close_threshold():
            self._hang_remaining -= self.frame_dt
            if self._hang_remaining <= 0:
                completed = self._close()
        else:
            self._hang_remaining = self.settings.hang_time
            self._last_active_samples = sum(b.size for b in self._buffer)

        if completed is None and self._duration() >= self.settings.max_duration:
            completed = self._close(truncated=True)
        return completed

    def _duration(self) -> float:
        return sum(b.size for b in self._buffer) / float(self.sample_rate)

    def _start(self) -> None:
        self._open = True
        self._buffer = list(self._pre_roll)
        self._pre_samples = sum(b.size for b in self._buffer)
        self._start_offset = max(
            0.0, (self._samples_seen - self._pre_samples) / float(self.sample_rate))
        self._pre_roll.clear()
        self._frame_stats = []
        self._active_frames = self.settings.open_frames
        self._total_frames = len(self._buffer)
        self._peak = 0.0
        self._clipped = False
        self._floor_at_open = self.floor.value
        self._hang_remaining = self.settings.hang_time
        self._last_active_samples = self._pre_samples

    def _close(self, truncated: bool = False) -> Optional[DetectedTransmission]:
        audio = np.concatenate(self._buffer) if self._buffer else np.zeros(0)
        stats = list(self._frame_stats)
        pre_samples = self._pre_samples
        start_offset = self._start_offset
        peak, clipped = self._peak, self._clipped
        floor_at_open = self._floor_at_open
        active, total = self._active_frames, self._total_frames
        last_active = self._last_active_samples

        self._open = False
        self._buffer = []
        self._frame_stats = []
        self._consecutive = 0
        self._last_active_samples = 0

        # Cut the hang-time silence, keeping the configured post-roll.
        keep = min(audio.size,
                   last_active + int(self.settings.post_roll * self.sample_rate))
        audio = audio[:keep]

        trimmed = 0.0
        if self.settings.trim_squelch_tail:
            audio, trimmed = self._trim_tail(audio, stats)

        duration = audio.size / float(self.sample_rate)
        signal_duration = max(
            0.0, duration - pre_samples / float(self.sample_rate)
            - self.settings.post_roll)
        if signal_duration < self.settings.min_duration:
            return None

        (likely_noise, likely_tone, modulation, flatness,
         confidence) = self._classify(stats, active, total)
        if likely_noise and self.settings.reject_noise:
            # Broadband static with no speech structure: a real event on the
            # channel, but not one worth logging as a transmission by default.
            return None
        assert self.stream_start is not None
        return DetectedTransmission(
            audio=audio, sample_rate=self.sample_rate,
            started_at=self.stream_start + _dt.timedelta(seconds=start_offset),
            start_offset=start_offset, duration=duration,
            peak_dbfs=dbfs(peak), rms_dbfs=dbfs(rms(audio)),
            noise_floor_dbfs=floor_at_open,
            active_ratio=(active / total) if total else 0.0,
            confidence=confidence, clipped=clipped, likely_noise=likely_noise,
            likely_tone=likely_tone, modulation_db=modulation, flatness=flatness,
            truncated=truncated, trimmed_tail=trimmed,
        )

    def _trim_tail(self, audio: np.ndarray, stats: List[dict]):
        """Remove a trailing squelch-noise burst, if there is one.

        Walks back from the end while frames look like broadband noise, and
        stops at the first frame that looks like signal - so speech that simply
        ends loudly is never truncated.
        """
        if not stats or audio.size == 0:
            return (audio, 0.0)
        cutoff = None
        for stat in reversed(stats):
            if stat["samples"] > audio.size:
                continue
            noisy = (stat["flatness"] >= self.settings.squelch_tail_flatness
                     and stat["hf_ratio"] > 0.45)
            if not noisy:
                break
            cutoff = stat["samples"] - self.frame_size
        if cutoff is None or cutoff <= 0:
            return (audio, 0.0)
        # Bound the trim: a run of noisy frames must never swallow the event.
        floor_samples = max(
            int(self.settings.pre_roll * self.sample_rate) + self.frame_size,
            audio.size - int(self.settings.max_tail_trim * self.sample_rate))
        keep = max(floor_samples, cutoff)
        if keep >= audio.size:
            return (audio, 0.0)
        trimmed = (audio.size - keep) / float(self.sample_rate)
        return (audio[:keep], trimmed)

    def _classify(self, stats: List[dict], active: int, total: int):
        """Content flags plus an overall detection confidence.

        Two independent signatures are combined, because neither is sufficient
        alone: *spectral flatness* separates tonal from broadband, and
        *envelope modulation* separates speech (which swings in level from
        syllable to syllable) from stationary static that happens to sit in the
        same band.
        """
        if not stats:
            return (False, False, 0.0, 0.0, 0.4)
        flatness = np.asarray([s["flatness"] for s in stats])
        levels = np.asarray([s["level"] for s in stats])
        threshold = self.open_threshold()
        selected = levels >= threshold
        if not selected.any():
            selected = np.ones_like(levels, dtype=bool)

        median_flatness = float(np.median(flatness[selected]))
        modulation = float(np.std(levels[selected])) if selected.sum() > 1 else 0.0
        stationary = modulation < self.settings.min_modulation_db

        likely_noise = stationary and median_flatness > self.settings.noise_flatness
        likely_tone = stationary and median_flatness < self.settings.tone_flatness

        # Confidence rises with how much of the event was above threshold, how
        # far above the floor it sat, and how speech-like the envelope was.
        activity = (active / total) if total else 0.0
        margin = float(np.clip((float(np.max(levels)) - self.floor.value) / 30.0, 0, 1))
        speechiness = float(np.clip(modulation / 8.0, 0.0, 1.0))
        confidence = float(np.clip(0.2 * activity + 0.35 * margin
                                   + 0.45 * speechiness, 0.0, 1.0))
        if likely_noise or likely_tone:
            confidence *= 0.4
        return (likely_noise, likely_tone, round(modulation, 2),
                round(median_flatness, 4), round(confidence, 3))


def detect_in_array(audio: np.ndarray, sample_rate: int,
                    settings: Optional[DetectorSettings] = None,
                    block_size: int = 4096) -> List[DetectedTransmission]:
    """Run the detector over a complete array (used by replay and tests)."""
    from .audio.source import AudioBlock

    detector = RadioActivityDetector(sample_rate, settings)
    out: List[DetectedTransmission] = []
    start = utcnow()
    data = np.asarray(audio, dtype=np.float64)
    for i in range(0, data.size, block_size):
        chunk = data[i:i + block_size]
        offset = i / float(sample_rate)
        out.extend(detector.push(AudioBlock(
            samples=chunk, sample_rate=sample_rate,
            timestamp=start + _dt.timedelta(seconds=offset), offset=offset)))
    out.extend(detector.flush())
    return out
