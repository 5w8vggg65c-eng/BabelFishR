"""Positive acoustic evidence that a captured recording is radio static.

Static here means stationary broadband noise: a hiss whose spectrum is flat
across the speech band, whose samples are Gaussian-like, and whose level
stays steady from start to end. Every condition must hold, so the test is
conservative by construction:

* pure silence fails (no level),
* a steady tone fails (a peaked spectrum: flatness near 0),
* a digital signal fails (bounded symbol levels: kurtosis well under 3),
* speech, and speech mixed with static, fail (syllables make the level swing
  and the spectrum peaky),
* impulsive crackle fails (kurtosis far above 3).

It uses the recording itself, never the speech engine's silence probability.
Whether the card says "Static" also needs the absence of credible speech
(Transmission.is_static): audible speech or an operator's correction wins.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .detect import amplitude_kurtosis, dbfs, rms, spectral_flatness

FRAME_SECONDS = 0.05
#: Below this (dBFS, median frame level) it is not called static: a quiet
#: room or a noise floor is not a static burst.
MIN_LEVEL_DBFS = -50.0
#: Flatness is measured inside the band any radio passes (a receiver's
#: 300-3400 Hz audio filter must not make its own static look peaked).
FLATNESS_BAND = (400.0, 3000.0)
#: White noise scores about 0.56 (Hann-windowed periodogram); speech 0.1-0.4.
MIN_FLATNESS = 0.45
#: Gaussian noise has kurtosis 3; digital symbols sit near 1.4-1.9.
KURTOSIS_RANGE = (2.5, 4.0)
#: Stationary: the 10th-90th percentile spread of frame levels, in dB.
MAX_LEVEL_SPREAD_DB = 6.0
MIN_SECONDS = 0.5
#: Frames this far below the burst's loud level are its quiet surroundings.
ACTIVE_RANGE_DB = 20.0
VERSION = 1


def static_evidence(audio, sample_rate: int) -> Optional[dict]:
    """The measurements and a verdict (``"static"`` or ``"not static"``),
    or None when the recording is too short to judge."""
    x = np.asarray(audio, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    rate = int(sample_rate)
    if rate <= 0 or x.size < MIN_SECONDS * rate:
        return None
    size = max(64, int(FRAME_SECONDS * rate))
    frames = [x[i:i + size] for i in range(0, x.size - size + 1, size)]
    all_levels = np.array([dbfs(rms(f)) for f in frames])
    # The burst itself, not the quiet pre- and post-roll around it: frames
    # within 20 dB of its loud level. Speech keeps its quieter syllables in
    # this range, so its swings still show.
    keep = all_levels >= np.percentile(all_levels, 90) - ACTIVE_RANGE_DB
    if keep.sum() * size < MIN_SECONDS * rate:
        return None
    frames = [f for f, k in zip(frames, keep) if k]
    levels = all_levels[keep]
    level = float(np.median(levels))
    flatness = float(np.median([spectral_flatness(f, rate, FLATNESS_BAND)
                                for f in frames]))
    kurtosis = float(np.median([amplitude_kurtosis(f) for f in frames]))
    spread = float(np.percentile(levels, 90) - np.percentile(levels, 10))
    reasons = []
    if level < MIN_LEVEL_DBFS:
        reasons.append("silence")
    if flatness < MIN_FLATNESS:
        reasons.append("not broadband (peaked spectrum: speech, a tone or a signal)")
    if not KURTOSIS_RANGE[0] <= kurtosis <= KURTOSIS_RANGE[1]:
        reasons.append("not noise-like (sample distribution)")
    if spread > MAX_LEVEL_SPREAD_DB:
        reasons.append("not stationary (the level swings)")
    return {"verdict": "not static" if reasons else "static",
            "reasons": reasons, "level_dbfs": round(level, 1),
            "flatness": round(flatness, 3), "kurtosis": round(kurtosis, 2),
            "level_spread_db": round(spread, 1), "version": VERSION}
