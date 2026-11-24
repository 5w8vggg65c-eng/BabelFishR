"""Evenly loud voiced speech is not a steady tone.

Field report (run 30, actual Mac): a public French speech fixture (clean,
evenly loud, digital silence between words; peak -3 dBFS) was classified
TONE, so automatic processing skipped it ("a steady tone cannot contain
speech") and it never reached speech recognition. The same happens at
a862cf1, before this branch: the tone rule asked only for a stationary
level among the loud frames and low spectral flatness. Voiced speech is
harmonic (low flatness), and evenly loud speech can sit under the 3 dB
modulation limit. A signalling tone also holds one frequency; speech pitch
and formants move. The rule now requires that too.
"""

from __future__ import annotations

import hashlib
import pathlib
import struct

import numpy as np
import pytest

from babelfishr.detect import (ContentClass, DetectorSettings, detect_in_array,
                               dominant_frequency)
from babelfishr.testing import bandpass

SR = 48_000
GATE = DetectorSettings(mode="fixed", threshold_dbfs=-20.0, hang_time=0.8,
                        pre_roll=0.5, post_roll=0.5)


def voiced(seconds, seed, f0=(110.0, 170.0)):
    """Synthetic voiced speech: a glottal pulse train with moving pitch,
    shaped by vowel formants that change each syllable, at even (TTS-like)
    loudness, with digital silence between words, peak -3 dBFS."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    pitch = f0[0] + (f0[1] - f0[0]) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t
                                                          + rng.uniform(0, 6)))
    phase = np.cumsum(pitch) / SR
    source = sum(np.sin(2 * np.pi * k * phase) / k for k in range(1, 30))
    vowels = [(700, 1200, 2600), (300, 2300, 3000), (500, 900, 2500), (400, 1900, 2600)]
    syllable = int(0.22 * SR)
    out = np.zeros(n)
    for i, start in enumerate(range(0, n, syllable)):
        seg = source[start:start + syllable]
        formants = vowels[(i + seed) % len(vowels)]
        shaped = sum(bandpass(seg, f * 0.85, f * 1.15, SR, numtaps=127) / (j + 1)
                     for j, f in enumerate(formants))
        out[start:start + seg.size] = shaped * np.sin(np.linspace(0, np.pi, seg.size)) ** 0.3
    for gap in np.arange(0.66, seconds, 0.66):                 # word gaps: zeros
        a = int(gap * SR)
        out[a:a + int(0.08 * SR)] = 0.0
    out = out / np.max(np.abs(out)) * 10 ** (-3 / 20)
    return np.concatenate([np.zeros(int(0.7 * SR)), out, np.zeros(int(0.7 * SR))])


def tone(seconds, *freqs, noise=0.0):
    t = np.arange(int(seconds * SR)) / SR
    x = sum(np.sin(2 * np.pi * f * t) for f in freqs)
    if noise:
        x = x + np.random.default_rng(1).normal(0, noise, x.size)
    x = x / np.max(np.abs(x)) * 10 ** (-6 / 20)
    return np.concatenate([np.zeros(int(0.7 * SR)), x, np.zeros(int(0.7 * SR))])


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5])
def test_evenly_loud_voiced_speech_is_never_a_tone_and_is_transcribed(seed):
    found = detect_in_array(voiced(3.2, seed), SR, GATE)
    assert len(found) == 1
    event = found[0]
    assert event.content_class is not ContentClass.TONE, (event.modulation_db,
                                                          event.flatness)
    assert event.should_auto_transcribe(GATE)


PURE_TONES = {
    "1000 Hz": tone(2.0, 1000),
    "1025 Hz, between FFT bins": tone(2.0, 1025),
    "1750 Hz tone burst": tone(1.5, 1750),
    "440 Hz": tone(1.0, 440),
    "DTMF 697 + 1209 Hz": tone(1.0, 697, 1209),
    "courtesy beep 0.4 s": tone(0.4, 1200),
    "1000 Hz with weak noise": tone(2.0, 1000, noise=0.003),
}


@pytest.mark.parametrize("name", PURE_TONES)
def test_real_steady_tones_are_still_tones(name):
    found = detect_in_array(PURE_TONES[name], SR, GATE)
    assert len(found) == 1
    assert found[0].content_class is ContentClass.TONE
    assert not found[0].should_auto_transcribe(GATE)       # auto_process_tone off


def test_dominant_frequency_resolves_between_bins():
    """20 ms frames have 50 Hz bins; the estimate must be finer than that."""
    t = np.arange(960) / SR
    for f in (125.0, 1025.0, 1750.0):
        assert abs(dominant_frequency(np.sin(2 * np.pi * f * t), SR) - f) < 5.0


# ---- the actual field fixture ---------------------------------------------------

FRENCH = (pathlib.Path(__file__).parent / "fixtures" / "public"
          / "uberi_speech_recognition" / "french.aiff")
FRENCH_SHA256 = "2bdb86e8d511a6f8e784188edc1bea6a504c703ab8ac5826246fece8b7e44582"


def read_aiff_pcm16(path):
    """Mono 16-bit big-endian PCM AIFF -> (float samples, rate), without the
    aifc module (removed in Python 3.13)."""
    data = path.read_bytes()
    assert data[:4] == b"FORM" and data[8:12] == b"AIFF"
    pos, comm, ssnd = 12, None, None
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if chunk == b"COMM":
            comm = body
        elif chunk == b"SSND":
            ssnd = body
        pos += 8 + size + (size & 1)
    channels, frames, bits = struct.unpack(">hIh", comm[:8])
    assert (channels, bits) == (1, 16)
    exponent = struct.unpack(">H", comm[8:10])[0] & 0x7FFF
    mantissa = struct.unpack(">Q", comm[10:18])[0]
    rate = mantissa * 2.0 ** (exponent - 16383 - 63)
    offset = struct.unpack(">I", ssnd[:4])[0]
    pcm = np.frombuffer(ssnd[8 + offset:8 + offset + frames * 2], dtype=">i2")
    return pcm.astype(np.float64) / 32768.0, rate


def test_the_public_french_fixture_reaches_speech_recognition():
    """The clip the actual Mac skipped as a tone (runs 28 and 30: TONE,
    confidence 0.209, 3.02 s), replayed as the field test did: 48 kHz, 1 s
    of digital silence each side, fixed -20 dBFS gate, rolls 0.5 s."""
    assert hashlib.sha256(FRENCH.read_bytes()).hexdigest() == FRENCH_SHA256
    speech, rate = read_aiff_pcm16(FRENCH)
    assert (rate, speech.size) == (44100.0, 111695)
    resampled = np.interp(np.arange(int(round(speech.size * SR / rate))) * rate / SR,
                          np.arange(speech.size), speech)
    audio = np.concatenate([np.zeros(SR), resampled, np.zeros(SR)])
    found = detect_in_array(audio, SR, GATE)
    assert len(found) == 1
    event = found[0]
    assert event.duration == pytest.approx(3.02, abs=0.05)
    assert event.content_class is not ContentClass.TONE
    assert event.should_auto_transcribe(GATE), "it never reaches speech recognition"
