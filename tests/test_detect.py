"""Transmission detection: the behaviour that makes or breaks the product."""

from __future__ import annotations

import numpy as np
import pytest

from babelfishr.detect import (DetectorSettings, RadioActivityDetector,
                               detect_in_array, spectral_flatness)
from babelfishr.testing import (build_fixture, gapped_transmission_fixture,
                                speech_like, standard_fixture, static_burst,
                                squelch_tail, tone)

SR = 48_000


def test_counts_separated_transmissions():
    fixture = standard_fixture(SR)
    expected = [t for t in fixture.transmissions
                if t.kind == "voice" and t.duration > 0.1]
    detected = detect_in_array(fixture.audio, SR)
    assert len(detected) == len(expected)


def test_transmissions_are_in_chronological_order():
    detected = detect_in_array(standard_fixture(SR).audio, SR)
    offsets = [d.start_offset for d in detected]
    assert offsets == sorted(offsets)


def test_pre_roll_precedes_the_signal():
    """The recording must start before the first word, not on it."""
    fixture = build_fixture([{"gap": 2.0},
                             {"kind": "voice", "duration": 2.0, "level_dbfs": -14}],
                            sample_rate=SR)
    settings = DetectorSettings(pre_roll=0.30)
    detected = detect_in_array(fixture.audio, SR, settings)
    assert len(detected) == 1
    speech_start = fixture.transmissions[0].start
    assert detected[0].start_offset < speech_start
    # Pre-roll should be roughly what was asked for, not an arbitrary amount.
    assert speech_start - detected[0].start_offset == pytest.approx(0.30, abs=0.12)


def test_hang_time_keeps_one_transmission_whole():
    detected = detect_in_array(gapped_transmission_fixture(SR).audio, SR)
    assert len(detected) == 1
    assert detected[0].duration > 4.0


def test_short_hang_time_would_split_it():
    """Confirms the previous test passes because of hang time, not by luck."""
    settings = DetectorSettings(hang_time=0.05, post_roll=0.02, min_duration=0.2)
    detected = detect_in_array(gapped_transmission_fixture(SR).audio, SR, settings)
    assert len(detected) > 1


def test_squelch_tail_is_trimmed():
    fixture = build_fixture(
        [{"gap": 1.5}, {"kind": "voice", "duration": 2.0, "level_dbfs": -14,
                        "tail": True, "tail_seconds": 0.35}, {"gap": 1.5}],
        sample_rate=SR)
    detected = detect_in_array(fixture.audio, SR)
    assert len(detected) == 1
    assert detected[0].trimmed_tail > 0.0


def test_tail_trim_never_consumes_the_transmission():
    settings = DetectorSettings(max_tail_trim=0.6)
    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "voice", "duration": 3.0, "level_dbfs": -14,
                        "tail": True}, {"gap": 1.0}], sample_rate=SR)
    detected = detect_in_array(fixture.audio, SR, settings)
    assert detected[0].duration > 2.5


def test_static_is_classified_and_rejected():
    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "static", "duration": 4.0, "level_dbfs": -20},
         {"gap": 1.0}], sample_rate=SR)
    assert detect_in_array(fixture.audio, SR) == []

    kept = detect_in_array(fixture.audio, SR, DetectorSettings(reject_noise=False))
    assert len(kept) == 1
    assert kept[0].likely_noise
    assert not kept[0].worth_transcribing


def test_steady_tone_is_not_treated_as_speech():
    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "tone", "duration": 1.5, "level_dbfs": -14},
         {"gap": 1.0}], sample_rate=SR)
    detected = detect_in_array(fixture.audio, SR, DetectorSettings(reject_noise=False))
    assert len(detected) == 1
    assert detected[0].likely_tone
    assert not detected[0].worth_transcribing


def test_brief_crackle_is_ignored():
    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "voice", "duration": 0.05, "level_dbfs": -14},
         {"gap": 1.0}], sample_rate=SR)
    assert detect_in_array(fixture.audio, SR) == []


def test_short_but_real_transmission_is_kept():
    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "voice", "duration": 0.7, "level_dbfs": -14},
         {"gap": 1.0}], sample_rate=SR)
    assert len(detect_in_array(fixture.audio, SR)) == 1


def test_clipped_speech_is_detected_and_flagged():
    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "voice", "duration": 2.0, "level_dbfs": -10,
                        "clip": True}, {"gap": 1.0}], sample_rate=SR)
    detected = detect_in_array(fixture.audio, SR)
    assert len(detected) == 1
    assert detected[0].clipped


def test_speech_modulation_separates_it_from_static():
    """The two discriminators must stay far apart, not merely on the right side."""
    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "voice", "duration": 3.0, "level_dbfs": -14},
         {"gap": 1.5}, {"kind": "static", "duration": 3.0, "level_dbfs": -20},
         {"gap": 1.0}], sample_rate=SR)
    detected = detect_in_array(fixture.audio, SR, DetectorSettings(reject_noise=False))
    voice = [d for d in detected if not d.likely_noise]
    noise = [d for d in detected if d.likely_noise]
    assert voice and noise
    assert voice[0].modulation_db > noise[0].modulation_db + 3.0


def test_flatness_is_measured_in_the_speech_band():
    """Measured over the whole spectrum, band-limited audio all looks tonal."""
    assert spectral_flatness(speech_like(1.0, SR)[:2048], SR) < 0.2
    assert spectral_flatness(static_burst(1.0, SR)[:2048], SR) > 0.35
    assert spectral_flatness(tone(1.0, 1000.0, SR)[:2048], SR) < 0.01


def test_max_duration_truncates_a_stuck_transmitter():
    settings = DetectorSettings(max_duration=2.0, min_duration=0.3)
    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "voice", "duration": 8.0, "level_dbfs": -14}],
        sample_rate=SR)
    detected = detect_in_array(fixture.audio, SR, settings)
    assert len(detected) > 1
    assert any(d.truncated for d in detected)


def test_silence_produces_nothing():
    from babelfishr.testing import silence

    assert detect_in_array(silence(5.0, SR), SR) == []


def test_settings_validation_rejects_nonsense():
    with pytest.raises(ValueError):
        DetectorSettings(open_margin_db=2.0, close_margin_db=8.0).validate()
    with pytest.raises(ValueError):
        DetectorSettings(min_duration=0).validate()
