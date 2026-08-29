"""Signal-processing primitives shared by sources and decoders."""

from .filters import (bandpass, dbfs, dc_block, deemphasis, frame, highpass,
                      hilbert_envelope, lowpass, moving_average, normalise, rms)
from .goertzel import dominant_tone, goertzel_bank, goertzel_power, peak_frequency, tone_powers
from .resample import float_to_pcm16, pcm16_to_float, resample, to_mono
from .vad import Segment, Segmenter, segment_array

__all__ = [
    "bandpass", "dbfs", "dc_block", "deemphasis", "frame", "highpass",
    "hilbert_envelope", "lowpass", "moving_average", "normalise", "rms",
    "dominant_tone", "goertzel_bank", "goertzel_power", "peak_frequency", "tone_powers",
    "float_to_pcm16", "pcm16_to_float", "resample", "to_mono",
    "Segment", "Segmenter", "segment_array",
]
