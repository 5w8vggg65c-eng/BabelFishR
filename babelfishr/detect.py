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
and reports a *confidence* plus a content classification.

Capture first, classify second
------------------------------
Classification NEVER decides whether an event is preserved.  The detector emits
every event that crosses the activity threshold and lasts long enough to be
real; the classification rides along as advice about whether *automatic* speech
recognition is worth attempting.  A misclassified digital burst or a burst of
static that turned out to matter must still be on disk afterwards, because the
operator cannot re-receive it.
"""

from __future__ import annotations

import abc
import dataclasses
import enum
import datetime as _dt
from collections import deque
from typing import Deque, List, Optional

import numpy as np

from .audio.source import AudioBlock
from .dsp.filters import dbfs, rms
from .models import utcnow


class ContentClass(str, enum.Enum):
    """What the detector thinks an event contains.

    Advice for downstream stages, never a reason to discard the recording.
    """

    SPEECH = "speech"
    NOISE = "noise"
    """Broadband and stationary: static, a fade, an open squelch."""

    TONE = "tone"
    """A steady tone: courtesy beep, roger beep, carrier with no modulation."""

    DIGITAL_SUSPECTED = "digital-suspected"
    """Legacy only: never produced any more.

    Versions before 2026-11-24 guessed "possibly digital" from the sample
    distribution of level-steady audio. The guess was unreliable and could
    label ordinary voice as digital (false positives), and ordinary audio
    from a microphone or interface cannot tell analogue from digital radio
    anyway. Kept so older rows load
    unchanged; the window shows no badge for it (timeline.UNBADGED_CLASSES)."""

    UNKNOWN = "unknown"

    @property
    def auto_transcribe_by_default(self) -> bool:
        return self is ContentClass.SPEECH or self is ContentClass.UNKNOWN

    @property
    def worth_digital_analysis(self) -> bool:
        return self in (ContentClass.DIGITAL_SUSPECTED, ContentClass.NOISE)


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

    hang_time: float = 3.0
    """End a message after this many seconds of quiet.

    "Quiet" is the input's audio level staying below the close threshold -
    the level the microphone or interface delivers, not a radio's squelch,
    carrier or RX state, which nothing here can see. Pauses shorter than
    this stay inside one message, so a speaker drawing breath is not split.
    3 s (was 0.8 s, a radio-burst value): monitoring a microphone or a
    receiver's audio, a message is then one utterance, and the next audio
    starts the next message - until Stop. Continuous audio that is never
    quiet this long is still handed on in parts (segment_seconds).
    Settings files that already hold a value keep it."""

    pre_roll: float = 0.50
    """Audio kept from *before* detection, so the first word is never clipped.

    0.5 s (was 0.3): measured on synthetic speech through a -20 dBFS gate, a
    soft opening syllable 0.25 s long kept 60% at 0.3 s and all of it at 0.5."""

    post_roll: float = 0.50
    """Audio kept after the signal drops.

    0.5 s (was 0.2): a closing syllable 12 dB below the speech, 0.4 s long,
    kept 35% at 0.2 s and all of it at 0.5. Only the rest of the hang-time
    quiet is cut."""

    min_duration: float = 0.30
    """Shorter events are discarded as crackle."""

    max_duration: float = 300.0
    """A stuck transmitter is split rather than filling the disk."""

    segment_seconds: float = 15.0
    """Processing latency for continuous audio.

    A radio burst ends when the channel goes quiet; continuous audio - a
    microphone in a room, an open carrier - may never be quiet for
    hang_time, and used to reach speech recognition only at Stop. While the
    gate stays open past this length the recording is cut into consecutive
    parts at the quietest frame in the last ``segment_search_seconds``, each
    handed on as soon as it is cut. Nothing is dropped, trimmed or repeated
    at a cut: the parts join sample for sample, and each part records that
    it continues (signal_metadata["segment"]). 0 disables segmenting.
    Bursts shorter than this are unaffected. While segmenting is on and
    shorter than max_duration, no part reaches max_duration, so a stuck
    carrier becomes a chain of parts rather than one truncated recording;
    max_duration applies when segmenting is off or not shorter."""

    segment_search_seconds: float = 3.0
    """How far back from the segment length to look for the quietest frame."""

    frame_ms: float = 20.0
    open_frames: int = 2
    """Consecutive loud frames required to open - rejects single clicks."""

    trim_squelch_tail: bool = True
    squelch_tail_flatness: float = 0.42
    """Spectral flatness above which a trailing frame is treated as noise."""

    max_tail_trim: float = 0.60
    """Never trim more than this - a tail-trimmer must not eat a transmission."""

    # -- two distinct thresholds, deliberately separated -----------------
    #
    # `min_duration` and the open threshold decide whether something is an
    # EVENT AT ALL, and therefore whether it is recorded. Nothing below is
    # allowed to veto recording.
    #
    # `auto_process_*` decide only whether the ASR engine is invoked
    # automatically. The operator can always override per transmission.

    auto_process_speech: bool = True
    auto_process_unknown: bool = True
    auto_process_tone: bool = False
    """A steady single-frequency carrier or courtesy beep. The one genuinely
    trivial class: it cannot carry speech, so it is the only classification
    still allowed to keep an ASR call from happening by itself."""

    # There is deliberately no auto_process_digital and no auto_process_noise.
    # A real Mac classified ordinary speech through the MacBook microphone as
    # a digital burst, and the setting that used to exist here then stopped
    # Whisper from ever seeing it - so the operator got no transcript, and
    # Argos got nothing to translate. Static is the same story with a
    # different label: radio static frequently has a weak voice under it.
    #
    # Removing the settings, rather than changing their defaults, is the
    # point. Alpha 3 wrote `auto_process_noise = false` into every
    # settings.toml it saved, so a new default would have fixed nothing for
    # anyone upgrading. Both keys are dropped on load - see RETIRED_OPTIONS.

    noise_flatness: float = 0.25
    """In-band flatness above which an event *may* be static (see modulation)."""

    min_modulation_db: float = 3.0
    """Speech swings in level syllable to syllable; stationary noise does not.

    Standard deviation of frame levels, in dB, below which an event is
    considered stationary. Speech typically measures 6-15 dB, static 1-3 dB.
    """

    tone_flatness: float = 0.008
    """Below this, with no modulation, the event may be a steady signalling
    tone - if its dominant frequency also holds (tone_steady_share)."""

    tone_steady_share: float = 0.8
    """Share of the loud frames whose dominant frequency must sit at the
    event's median one for it to count as a steady tone. Evenly loud, voiced
    speech (TTS, a close microphone) is also harmonic and level-stationary,
    and was called a tone and never transcribed; its pitch and formants move
    from frame to frame, a signalling tone's frequency does not."""

    def validate(self) -> "DetectorSettings":
        if self.open_margin_db <= self.close_margin_db:
            raise ValueError("open_margin_db must exceed close_margin_db")
        if self.pre_roll < 0 or self.post_roll < 0:
            raise ValueError("pre_roll and post_roll must be >= 0")
        if self.min_duration <= 0:
            raise ValueError("min_duration must be > 0")
        if not 0.1 <= self.hang_time <= 30.0:
            raise ValueError("hang_time (quiet that ends a message) must be "
                             "between 0.1 and 30 seconds")
        if self.segment_seconds and self.segment_seconds < 2.0:
            raise ValueError("segment_seconds must be 0 (off) or at least 2 seconds")
        if self.segment_search_seconds < 0:
            raise ValueError("segment_search_seconds must be >= 0")
        if self.segment_seconds and self.segment_search_seconds >= self.segment_seconds:
            raise ValueError("segment_search_seconds must be shorter than segment_seconds")
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
    content_class: "ContentClass" = None  # type: ignore[assignment]
    """What the detector thinks this is. Advice only - see module docstring."""

    likely_noise: bool = False
    """Broadband and structureless: probably static, not a voice."""

    likely_tone: bool = False
    """Dominated by a steady tone: a beep or signalling, not speech."""

    likely_digital: bool = False
    """Broadband but strongly structured: a candidate for digital analysis."""

    modulation_db: float = 0.0
    """Spread of frame levels in dB - the syllabic signature of speech."""

    flatness: float = 0.0
    """Median in-band spectral flatness: ~0 tonal, ~0.5 broadband noise."""

    kurtosis: float = 3.0
    """Sample-distribution kurtosis: ~3 static, <2 discrete symbols, >4 speech."""

    crest: float = 0.0
    """Peak-to-RMS ratio, corroborating the kurtosis reading."""

    truncated: bool = False
    trimmed_tail: float = 0.0
    """Seconds of squelch tail removed from the end."""

    continued: bool = False
    """Cut while the audio went on: the next part starts at the very next
    sample (see DetectorSettings.segment_seconds)."""

    continues_previous: bool = False
    """This part starts exactly where the previous part was cut."""

    @property
    def snr_db(self) -> float:
        return self.peak_dbfs - self.noise_floor_dbfs

    @property
    def ended_at(self) -> _dt.datetime:
        return self.started_at + _dt.timedelta(seconds=self.duration)

    def should_auto_transcribe(self, settings: "DetectorSettings") -> bool:
        """Whether to spend an ASR call automatically.

        This gates *processing*, never persistence: the recording exists on
        disk and in the database regardless of what this returns, and the
        operator can always force transcription from the timeline.

        The classifier is a heuristic over a few seconds of audio, and a real
        Mac proved it wrong in the way that costs most: ordinary speech
        through the built-in microphone came out as DIGITAL_SUSPECTED. So a
        classification no longer decides on its own that speech recognition
        is not worth trying. Only a steady tone - which cannot contain
        speech - and an event too short to hold a word are routed away, and
        even those are kept on disk and can be forced.
        """
        if self.duration < 0.25:
            return False
        content = self.content_class or ContentClass.UNKNOWN
        if content in (ContentClass.DIGITAL_SUSPECTED, ContentClass.NOISE):
            # Not a lookup, and not settings-dependent: no configuration may
            # reintroduce the veto, including one persisted by alpha 3.
            return True
        return {
            ContentClass.SPEECH: settings.auto_process_speech,
            ContentClass.UNKNOWN: settings.auto_process_unknown,
            ContentClass.TONE: settings.auto_process_tone,
        }.get(content, True)

    @property
    def worth_digital_analysis(self) -> bool:
        cls = self.content_class or ContentClass.UNKNOWN
        return cls.worth_digital_analysis

    def __post_init__(self) -> None:
        if self.content_class is None:
            self.content_class = ContentClass.UNKNOWN

    def to_dict(self) -> dict:
        d = {k: v for k, v in dataclasses.asdict(self).items() if k != "audio"}
        d["snr_db"] = self.snr_db
        d["samples"] = int(self.audio.size)
        d["content_class"] = (self.content_class or ContentClass.UNKNOWN).value
        return d


@dataclasses.dataclass
class Verdict:
    """The detector's read on one event's content."""

    content_class: ContentClass
    confidence: float
    likely_noise: bool = False
    likely_tone: bool = False
    likely_digital: bool = False
    modulation_db: float = 0.0
    flatness: float = 0.0
    kurtosis: float = 3.0
    crest: float = 0.0


class TransmissionDetector(abc.ABC):
    """Interface for anything that cuts a stream into transmissions."""

    @abc.abstractmethod
    def push(self, block: AudioBlock) -> List[DetectedTransmission]:
        """Consume a block; return transmissions completed by it."""

    @abc.abstractmethod
    def flush(self, cutoff: bool = False) -> List[DetectedTransmission]:
        """End of stream: emit anything still open. ``cutoff`` is the
        operator's Stop: an open burst keeps every sample through the cutoff
        (no hang wait, no tail trim, no minimum duration)."""

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


def dominant_frequency(frame: np.ndarray, sample_rate: int) -> float:
    """Frequency (Hz) of the strongest component of *frame*, DC excluded,
    refined between FFT bins by parabolic interpolation on the log
    magnitude: a 20 ms frame has 50 Hz bins, too coarse to tell a steady
    tone from a voice whose pitch moves between 100 and 150 Hz."""
    if frame.size < 32:
        return 0.0
    spectrum = np.abs(np.fft.rfft(frame * np.hanning(frame.size)))
    spectrum[0] = 0.0
    k = int(np.argmax(spectrum))
    offset = 0.0
    if 0 < k < spectrum.size - 1:
        a, b, c = np.log(spectrum[k - 1:k + 2] + 1e-12)
        denominator = a - 2.0 * b + c
        if denominator < 0:
            offset = float(np.clip(0.5 * (a - c) / denominator, -0.5, 0.5))
    return (k + offset) * sample_rate / frame.size


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


def amplitude_kurtosis(frame: np.ndarray) -> float:
    """Kurtosis of the sample distribution - the digital/static discriminator.

    Static is band-limited Gaussian noise, so its kurtosis sits at about 3.0.
    A digitally modulated baseband signal spends its time near a small set of
    discrete symbol levels, giving a flat-topped, sub-Gaussian distribution
    with kurtosis well below 2. Speech, with its long quiet stretches and short
    loud peaks, is super-Gaussian and lands above 4.

    Measured on synthetic fixtures: speech 4.4, static 3.0, 4FSK 1.9, 2FSK 1.4.
    """
    x = np.asarray(frame, dtype=np.float64)
    if x.size < 64:
        return 3.0
    x = x - float(np.mean(x))
    deviation = float(np.std(x))
    if deviation < 1e-12:
        return 3.0
    return float(np.mean((x / deviation) ** 4))


def crest_factor(frame: np.ndarray) -> float:
    """Peak-to-RMS ratio. Corroborates kurtosis: bounded symbols crest low."""
    x = np.asarray(frame, dtype=np.float64)
    if x.size == 0:
        return 0.0
    rms_value = float(np.sqrt(np.mean(np.square(x))))
    if rms_value < 1e-12:
        return 0.0
    return float(np.max(np.abs(x)) / rms_value)


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
        self.discontinuities = 0
        self._pre_samples = 0
        self._start_offset = 0.0
        self._hang_remaining = 0.0
        self._last_active_samples = 0
        self._continues_previous = False
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
    #: How far a block's timestamp may sit from where the sample count says
    #: it should be before the stream is taken to have a break in it.
    DISCONTINUITY_SECONDS = 0.25

    def push(self, block: AudioBlock) -> List[DetectedTransmission]:
        out: List[DetectedTransmission] = []
        ahead = (self._samples_seen + self._pending.size) / float(self.sample_rate)
        if self.stream_start is None:
            # The first block (or the first after a reset) fixes the origin:
            # the sample position where it begins is its timestamp.
            self.stream_start = block.timestamp - _dt.timedelta(seconds=ahead)
        else:
            expected = self.stream_start + _dt.timedelta(seconds=ahead)
            gap = (block.timestamp - expected).total_seconds()
            if abs(gap) > self.DISCONTINUITY_SECONDS:
                # A break in the stream (audio missing, or a clock that
                # jumped). Audio already buffered keeps the time it really
                # has: what is open closes here, ending where the audio
                # ends; the pre-roll from before the break is not attached
                # to anything after it; then the origin is fixed afresh at
                # this block, so what follows carries its own time.
                out.extend(self.flush())
                self._pre_roll.clear()
                self._consecutive = 0
                ahead = self._samples_seen / float(self.sample_rate)
                self.stream_start = block.timestamp - _dt.timedelta(seconds=ahead)
                self.discontinuities += 1
        samples = np.asarray(block.samples, dtype=np.float64).ravel()
        self._pending = (np.concatenate([self._pending, samples])
                         if self._pending.size else samples)

        count = self._pending.size // self.frame_size
        for i in range(count):
            frame = self._pending[i * self.frame_size:(i + 1) * self.frame_size]
            found = self._process(frame)
            if found is not None:
                out.append(found)
        if count:
            self._pending = self._pending[count * self.frame_size:]
        return out

    def flush(self, cutoff: bool = False) -> List[DetectedTransmission]:
        out: List[DetectedTransmission] = []
        if self._open:
            if self._pending.size:
                self._buffer.append(self._pending.copy())
            if cutoff:
                # The operator pressed Stop with this burst still open: every
                # sample up to the cutoff is kept - the partial last frame
                # too - without waiting for the hang time, trimming back to
                # the post-roll or a squelch tail, or dropping a burst
                # shorter than min_duration. The gate had already opened on
                # it; the input ended because the operator ended it.
                found = self._close(cutoff=True)
            else:
                # The end of a file or a break in the stream ends the audio,
                # not the transmitter: a recording already handed on in parts
                # keeps its last samples rather than losing them to
                # squelch-tail trimming.
                found = self._close(keep_tail=self._continues_previous)
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
            "kurtosis": amplitude_kurtosis(frame),
            "crest": crest_factor(frame),
            "peak_hz": dominant_frequency(frame, self.sample_rate),
            "samples": int(sum(b.size for b in self._buffer)),
            "loud": bool(loud),
        })
        if loud:
            self._active_frames += 1

        holding = level >= self.close_threshold()
        if not holding:
            self._hang_remaining -= self.frame_dt
            if self._hang_remaining <= 0:
                completed = self._close()
        else:
            self._hang_remaining = self.settings.hang_time
            self._last_active_samples = sum(b.size for b in self._buffer)

        # Processing segments only below the archival cap: a max_duration
        # shorter than segment_seconds still closes as a truncated stuck
        # transmitter, exactly as before.
        # Cut only on a frame that holds the gate open (at or above the close
        # threshold, not just the open one - room noise between the two keeps
        # the gate open and must still be cut): the part left open then
        # holds signal, so a cut never strands a silent remainder as a
        # message of its own. While the gate is only counting down its hang,
        # it closes by itself. Latency is at most segment_seconds + hang_time.
        if (completed is None and holding and self._open and self.settings.segment_seconds
                and self.settings.segment_seconds < self.settings.max_duration
                and self._duration() >= self.settings.segment_seconds):
            completed = self._cut_segment()
        if completed is None and self._duration() >= self.settings.max_duration:
            completed = self._close(truncated=True)
        return completed

    def _cut_segment(self) -> Optional[DetectedTransmission]:
        """Hand on what has been heard so far, and keep listening.

        The cut falls at the end of the quietest frame in the last
        ``segment_search_seconds`` (a pause, if there is one), never inside the
        pre-roll. The part before it is emitted untrimmed; the part after it
        stays in the open gate as the start of the next part, with its time
        offset advanced by exactly the samples handed on.
        """
        audio = np.concatenate(self._buffer) if self._buffer else np.zeros(0)
        stats = self._frame_stats
        total = audio.size
        earliest = max(self._pre_samples + self.frame_size,
                       total - int(self.settings.segment_search_seconds * self.sample_rate))
        candidates = [i for i, st in enumerate(stats) if earliest <= st["samples"] <= total]
        if not candidates:
            return None
        quietest = min(candidates, key=lambda i: (stats[i]["level"], -i))
        cut = int(stats[quietest]["samples"])
        part, rest = audio[:cut], audio[cut:]
        part_stats, rest_stats = stats[:quietest + 1], stats[quietest + 1:]

        verdict = self._classify(part_stats,
                                 sum(1 for st in part_stats if st.get("loud")),
                                 len(part_stats))
        assert self.stream_start is not None
        start_offset = self._start_offset
        part_peak = float(np.max(np.abs(part))) if part.size else 0.0
        found = DetectedTransmission(
            audio=part, sample_rate=self.sample_rate,
            started_at=self.stream_start + _dt.timedelta(seconds=start_offset),
            start_offset=start_offset, duration=part.size / float(self.sample_rate),
            peak_dbfs=dbfs(part_peak), rms_dbfs=dbfs(rms(part)),
            noise_floor_dbfs=self._floor_at_open,
            active_ratio=(sum(1 for st in part_stats if st.get("loud"))
                          / len(part_stats)) if part_stats else 0.0,
            confidence=verdict.confidence,
            clipped=bool(part.size and np.any(np.abs(part) >= 0.999)),
            content_class=verdict.content_class,
            likely_noise=verdict.likely_noise, likely_tone=verdict.likely_tone,
            likely_digital=verdict.likely_digital,
            modulation_db=verdict.modulation_db, flatness=verdict.flatness,
            kurtosis=verdict.kurtosis, crest=verdict.crest,
            truncated=False, trimmed_tail=0.0,
            continued=True, continues_previous=self._continues_previous,
        )

        # The remainder is the start of the next part, gate still open.
        self._buffer = [rest] if rest.size else []
        self._frame_stats = [dict(st, samples=int(st["samples"]) - cut) for st in rest_stats]
        self._active_frames = sum(1 for st in self._frame_stats if st.get("loud"))
        self._total_frames = len(self._frame_stats)
        self._peak = float(np.max(np.abs(rest))) if rest.size else 0.0
        self._clipped = bool(rest.size and np.any(np.abs(rest) >= 0.999))
        self._pre_samples = 0
        self._start_offset = start_offset + cut / float(self.sample_rate)
        self._last_active_samples = max(0, self._last_active_samples - cut)
        self._continues_previous = True
        return found

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
        self._continues_previous = False

    def _close(self, truncated: bool = False, keep_tail: bool = False,
               cutoff: bool = False) -> Optional[DetectedTransmission]:
        audio = np.concatenate(self._buffer) if self._buffer else np.zeros(0)
        stats = list(self._frame_stats)
        pre_samples = self._pre_samples
        start_offset = self._start_offset
        peak, clipped = self._peak, self._clipped
        floor_at_open = self._floor_at_open
        active, total = self._active_frames, self._total_frames
        last_active = self._last_active_samples
        continues_previous = self._continues_previous
        self._continues_previous = False

        self._open = False
        self._buffer = []
        self._frame_stats = []
        self._consecutive = 0
        self._last_active_samples = 0

        trimmed = 0.0
        if not cutoff:
            # Cut the hang-time silence, keeping the configured post-roll.
            keep = min(audio.size,
                       last_active + int(self.settings.post_roll * self.sample_rate))
            audio = audio[:keep]
            if self.settings.trim_squelch_tail and not keep_tail:
                audio, trimmed = self._trim_tail(audio, stats)

        duration = audio.size / float(self.sample_rate)
        # The signal runs from the end of the pre-roll to the last frame that
        # held the gate open - measured directly, not as "duration minus the
        # rolls", which miscounts once the squelch-tail trim has removed
        # part of the post-roll (a real 0.7 s burst then looked like crackle).
        # The frames that opened the gate are the last of the pre-roll; they
        # are signal too.
        signal_start = max(0, pre_samples - self.settings.open_frames * self.frame_size)
        signal_duration = max(
            0.0, (min(last_active, audio.size) - signal_start) / float(self.sample_rate))
        if (signal_duration < self.settings.min_duration and not continues_previous
                and not cutoff):
            # Crackle. A short last part of a segmented recording is not
            # crackle - it is the end of audio already partly handed on, and
            # dropping it would leave a hole in the recording.
            return None
        if audio.size == 0:
            return None

        verdict = self._classify(stats, active, total)
        # No classification veto: an event that crossed the activity threshold
        # and lasted long enough is real, and the operator cannot re-receive
        # it. Classification only advises whether to spend an ASR call.
        assert self.stream_start is not None
        return DetectedTransmission(
            audio=audio, sample_rate=self.sample_rate,
            started_at=self.stream_start + _dt.timedelta(seconds=start_offset),
            start_offset=start_offset, duration=duration,
            peak_dbfs=dbfs(peak), rms_dbfs=dbfs(rms(audio)),
            noise_floor_dbfs=floor_at_open,
            active_ratio=(active / total) if total else 0.0,
            confidence=verdict.confidence, clipped=clipped,
            content_class=verdict.content_class,
            likely_noise=verdict.likely_noise, likely_tone=verdict.likely_tone,
            likely_digital=verdict.likely_digital,
            modulation_db=verdict.modulation_db, flatness=verdict.flatness,
            kurtosis=verdict.kurtosis, crest=verdict.crest,
            truncated=truncated, trimmed_tail=trimmed,
            continues_previous=continues_previous,
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

    def _classify(self, stats: List[dict], active: int, total: int) -> "Verdict":
        """Content classification plus an overall detection confidence.

        Three independent signatures, because no one of them is sufficient:

        * *spectral flatness* separates tonal from broadband;
        * *envelope modulation* separates speech (which swings in level from
          syllable to syllable) from stationary noise in the same band;
        Kurtosis and crest factor are measured and recorded, but no longer
        used to guess "digital": BabelFishR receives ordinary audio from a
        microphone or interface and cannot tell analogue from digital radio
        (the old guess was unreliable and gave false positives).
        """
        if not stats:
            return Verdict(content_class=ContentClass.UNKNOWN, confidence=0.4)

        flatness = np.asarray([s["flatness"] for s in stats])
        levels = np.asarray([s["level"] for s in stats])
        kurtosis = np.asarray([s.get("kurtosis", 3.0) for s in stats])
        crest = np.asarray([s.get("crest", 0.0) for s in stats])
        threshold = self.open_threshold()
        selected = levels >= threshold
        if not selected.any():
            selected = np.ones_like(levels, dtype=bool)

        median_flatness = float(np.median(flatness[selected]))
        modulation = float(np.std(levels[selected])) if selected.sum() > 1 else 0.0
        median_kurtosis = float(np.median(kurtosis[selected]))
        median_crest = float(np.median(crest[selected]))
        stationary = modulation < self.settings.min_modulation_db

        # A signalling tone holds one frequency; level-stationary voiced
        # speech is harmonic too, but its pitch and formants move.
        steady = True
        peaks = [s.get("peak_hz") for s, keep in zip(stats, selected) if keep]
        if peaks and all(p is not None for p in peaks):
            peaks = np.asarray(peaks, dtype=float)
            centre = float(np.median(peaks))
            tolerance = max(10.0, 0.02 * centre)
            steady = float(np.mean(np.abs(peaks - centre) <= tolerance)) \
                >= self.settings.tone_steady_share
        likely_tone = (stationary and median_flatness < self.settings.tone_flatness
                       and steady)
        # No "digital" guess (see above): likely_digital stays False so the
        # recorded field keeps its shape for older readers.
        likely_digital = False
        likely_noise = (stationary and not likely_tone
                        and median_flatness > self.settings.noise_flatness)

        if likely_noise:
            content_class = ContentClass.NOISE
        elif likely_tone:
            content_class = ContentClass.TONE
        elif modulation >= self.settings.min_modulation_db:
            content_class = ContentClass.SPEECH
        else:
            content_class = ContentClass.UNKNOWN

        activity = (active / total) if total else 0.0
        margin = float(np.clip((float(np.max(levels)) - self.floor.value) / 30.0, 0, 1))
        speechiness = float(np.clip(modulation / 8.0, 0.0, 1.0))
        confidence = float(np.clip(0.2 * activity + 0.35 * margin
                                   + 0.45 * speechiness, 0.0, 1.0))
        if content_class is not ContentClass.SPEECH:
            confidence *= 0.4
        return Verdict(
            content_class=content_class, confidence=round(confidence, 3),
            likely_noise=likely_noise, likely_tone=likely_tone,
            likely_digital=likely_digital, modulation_db=round(modulation, 2),
            flatness=round(median_flatness, 4),
            kurtosis=round(median_kurtosis, 3), crest=round(median_crest, 3),
        )


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
