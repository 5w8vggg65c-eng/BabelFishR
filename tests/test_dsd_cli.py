"""DSD-neo CLI correctness against the documented flag set.

Validated against
https://github.com/arancormonk/dsd-neo/blob/main/docs/cli.md

An earlier version collapsed distinctions upstream makes (DMR dual-slot vs
single-slot mono; NXDN48 vs NXDN96), omitted M17/ProVoice/EDACS entirely, never
passed -fa for an unknown protocol, matched P25 Phase 1 before Phase 2 so a
Phase 2 decode was mislabelled, reused one derived input path across attempts,
and declared VOICE_DECODED for any output file larger than a WAV header.

These tests drive a stub reproducing the documented interface. Real dsd-neo
behaviour remains UNVERIFIED until tested with the actual binary and
independently identified recordings.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from babelfishr.analysis.base import AnalysisRequest
from babelfishr.analysis.dsd import (AUTO_FLAG, AUTO_ROTATION_SECONDS, PRESETS,
                                     PRESETS_BY_ID, DsdNeoAnalyser,
                                     resolve_preset)
from babelfishr.audio.wavefile import write_wav
from babelfishr.models import AnalysisOutcome, Transmission

pytestmark = pytest.mark.unit

STUB = str(pathlib.Path(__file__).parent / "stubs" / "fake_dsd.py")

#: Exactly as documented upstream.
DOCUMENTED_FLAGS = {
    "auto": "-fa",
    "dmr-dual": "-fs",
    "dmr-mono": "-fr",
    "p25p1": "-f1",
    "p25p2": "-f2",
    "dstar": "-fd",
    "nxdn48": "-fi",
    "nxdn96": "-fn",
    "x2tdma": "-fx",
    "ysf": "-fy",
    "m17": "-fz",
    "provoice": "-fp",
    "dpmr": "-fm",
    "edacs": "-fh",
    "edacs-esk": "-fH",
    "edacs-ea": "-fe",
    "edacs-ea-esk": "-fE",
}


@pytest.fixture
def analyser():
    return DsdNeoAnalyser(executable=STUB)


@pytest.fixture
def recording(tmp_path):
    path = tmp_path / "tx.wav"
    write_wav(str(path), 0.3 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000),
              8000)
    return str(path)


@pytest.fixture
def long_recording(tmp_path):
    """Longer than the automatic rotation, so no short-recording warning."""
    path = tmp_path / "long.wav"
    seconds = int(AUTO_ROTATION_SECONDS) + 2
    write_wav(str(path),
              0.3 * np.sin(2 * np.pi * 440 * np.arange(8000 * seconds) / 8000),
              8000)
    return str(path)


def _run(analyser, recording, scenario, monkeypatch, **kwargs):
    monkeypatch.setenv("BABELFISHR_FAKE_DSD_SCENARIO", scenario)
    return analyser.analyse(AnalysisRequest(
        transmission=Transmission(audio_path=recording), **kwargs))


# ---- flag mapping -------------------------------------------------------
@pytest.mark.parametrize("preset_id,flag", sorted(DOCUMENTED_FLAGS.items()))
def test_every_preset_uses_the_documented_flag(preset_id, flag):
    assert preset_id in PRESETS_BY_ID, f"{preset_id} preset is missing"
    assert PRESETS_BY_ID[preset_id].flag == flag


def test_dmr_dual_and_mono_are_separate_presets():
    """-fs (dual slot) and -fr (single slot mono) are different decoders."""
    assert PRESETS_BY_ID["dmr-dual"].flag == "-fs"
    assert PRESETS_BY_ID["dmr-mono"].flag == "-fr"
    assert PRESETS_BY_ID["dmr-dual"].flag != PRESETS_BY_ID["dmr-mono"].flag


def test_nxdn48_and_nxdn96_are_separate_presets():
    assert PRESETS_BY_ID["nxdn48"].flag == "-fi"
    assert PRESETS_BY_ID["nxdn96"].flag == "-fn"


def test_no_preset_reuses_a_flag():
    flags = [preset.flag for preset in PRESETS]
    assert len(flags) == len(set(flags)), "two presets share a flag"


def test_unknown_protocol_resolves_to_auto():
    for name in ("", "unknown", "something-else", None or ""):
        assert resolve_preset(name).flag == AUTO_FLAG


# ---- command construction ----------------------------------------------
def test_unknown_protocol_passes_fa_explicitly(analyser, recording, monkeypatch,
                                               tmp_path):
    trace = tmp_path / "trace.txt"
    monkeypatch.setenv("BABELFISHR_FAKE_DSD_TRACE", str(trace))
    attempt = _run(analyser, recording, "nothing", monkeypatch)
    assert AUTO_FLAG in attempt.command
    assert AUTO_FLAG in trace.read_text()


@pytest.mark.parametrize("preset_id,flag", sorted(DOCUMENTED_FLAGS.items()))
def test_selected_preset_reaches_the_binary(analyser, recording, monkeypatch,
                                            tmp_path, preset_id, flag):
    trace = tmp_path / f"trace-{preset_id}.txt"
    monkeypatch.setenv("BABELFISHR_FAKE_DSD_TRACE", str(trace))
    attempt = _run(analyser, recording, "nothing", monkeypatch,
                   protocol=preset_id)
    assert flag in attempt.command
    assert flag in trace.read_text()


def test_command_declares_the_sample_rate(analyser, recording, monkeypatch):
    attempt = _run(analyser, recording, "nothing", monkeypatch)
    assert "-s" in attempt.command
    assert "48000" in attempt.command


def test_preset_is_recorded_on_the_attempt(analyser, recording, monkeypatch):
    attempt = _run(analyser, recording, "nothing", monkeypatch,
                   protocol="p25p2")
    assert attempt.options["preset"] == "p25p2"
    assert attempt.options["flag"] == "-f2"


# ---- parsing order ------------------------------------------------------
def test_phase_2_is_not_mislabelled_as_phase_1(analyser, recording, monkeypatch):
    """The original defect: the Phase 1 pattern matched 'P25 Phase 2' first."""
    attempt = _run(analyser, recording, "p25p2-voice", monkeypatch)
    assert attempt.protocol == "P25 Phase 2"


def test_phase_1_still_recognised(analyser, recording, monkeypatch):
    attempt = _run(analyser, recording, "p25-metadata", monkeypatch)
    assert attempt.protocol == "P25 Phase 1"


def test_nxdn48_is_not_reported_as_generic_nxdn(analyser, recording,
                                                monkeypatch):
    attempt = _run(analyser, recording, "candidate", monkeypatch)
    assert attempt.protocol == "NXDN48"


# ---- decode validation --------------------------------------------------
def test_silent_output_is_not_a_voice_decode(analyser, recording, monkeypatch):
    """A file bigger than its header is not evidence of a decode."""
    attempt = _run(analyser, recording, "silent-output", monkeypatch)
    assert attempt.outcome is not AnalysisOutcome.VOICE_DECODED
    assert attempt.decoded_audio is None
    assert "silent" in str(attempt.metadata)


def test_real_audio_with_corroboration_is_a_voice_decode(analyser, recording,
                                                         monkeypatch):
    attempt = _run(analyser, recording, "dmr-voice", monkeypatch)
    assert attempt.outcome is AnalysisOutcome.VOICE_DECODED
    assert attempt.decoded_audio
    assert pathlib.Path(attempt.decoded_audio).exists()


def test_decoded_audio_measurement_is_recorded(analyser, recording, monkeypatch):
    attempt = _run(analyser, recording, "dmr-voice", monkeypatch)
    assert "rms" in attempt.metadata.get("decoded_audio", "")


# ---- unique derived inputs ---------------------------------------------
def test_derived_inputs_are_unique_per_attempt(analyser, recording, monkeypatch):
    """Concurrent or repeated analyses must not share a derived input."""
    first = _run(analyser, recording, "nothing", monkeypatch)
    second = _run(analyser, recording, "nothing", monkeypatch)
    assert first.input_is_derived and second.input_is_derived
    assert first.input_path != second.input_path
    assert pathlib.Path(first.input_path).exists()
    assert pathlib.Path(second.input_path).exists()


def test_derived_input_names_include_the_attempt_id(analyser, recording,
                                                    monkeypatch):
    attempt = _run(analyser, recording, "nothing", monkeypatch)
    assert attempt.id in pathlib.Path(attempt.input_path).name


# ---- automatic hunting caveat ------------------------------------------
def test_short_recording_gets_an_auto_hunt_warning(analyser, recording,
                                                   monkeypatch):
    """A 1 s clip can finish before auto hunting reaches the right profile."""
    attempt = _run(analyser, recording, "nothing", monkeypatch)
    warning = attempt.metadata.get("auto_hunt_warning", "")
    assert "profile" in warning
    assert str(int(AUTO_ROTATION_SECONDS)) in warning


def test_long_recording_gets_no_auto_hunt_warning(analyser, long_recording,
                                                  monkeypatch):
    attempt = _run(analyser, long_recording, "nothing", monkeypatch)
    assert "auto_hunt_warning" not in attempt.metadata


def test_specific_preset_gets_no_auto_hunt_warning(analyser, recording,
                                                   monkeypatch):
    attempt = _run(analyser, recording, "nothing", monkeypatch,
                   protocol="dmr-dual")
    assert "auto_hunt_warning" not in attempt.metadata


# ---- invariants ---------------------------------------------------------
def test_original_survives_every_preset(analyser, recording, monkeypatch):
    import hashlib

    before = hashlib.sha256(pathlib.Path(recording).read_bytes()).hexdigest()
    for preset_id in DOCUMENTED_FLAGS:
        _run(analyser, recording, "nothing", monkeypatch, protocol=preset_id)
    after = hashlib.sha256(pathlib.Path(recording).read_bytes()).hexdigest()
    assert after == before


def test_encrypted_stays_metadata_only(analyser, recording, monkeypatch):
    attempt = _run(analyser, recording, "encrypted", monkeypatch)
    assert attempt.outcome is AnalysisOutcome.ENCRYPTED_OR_UNSUPPORTED
    assert attempt.decoded_audio is None
    assert not attempt.outcome.is_success


def test_dsd_works_without_an_sdr(analyser, recording, monkeypatch, tmp_path):
    """The path is radio audio -> WAV -> DSD; no SDR is involved anywhere."""
    from babelfishr.config import Config
    from babelfishr.sources import build_signal_source, sdr_status

    config = Config()
    assert not sdr_status(config)["configured"]
    assert build_signal_source(config) is None

    attempt = _run(analyser, recording, "dmr-voice", monkeypatch)
    assert attempt.outcome is AnalysisOutcome.VOICE_DECODED
    # The only input is the recording captured from ordinary radio audio.
    assert attempt.input_path.endswith(".wav")
    assert "-i" in attempt.command
