"""DSD-neo integration and metadata provenance.

The DSD tests drive a stub binary (``tests/stubs/fake_dsd.py``) that reproduces
dsd-neo's *interface* - argument shape, stdout, exit status, optional decoded
WAV. That proves BabelFishR drives an external decoder correctly and handles
every outcome. It proves nothing about decoding real digital traffic, which
needs the real tool and independently identified recordings.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys

import numpy as np
import pytest

from babelfishr.analysis.base import AnalysisRequest
from babelfishr.analysis.dsd import DsdNeoAnalyser
from babelfishr.audio.wavefile import read_wav, write_wav
from babelfishr.models import (AnalysisAttempt, AnalysisOutcome, Provenance,
                               Transmission)

STUB = str(pathlib.Path(__file__).parent / "stubs" / "fake_dsd.py")


@pytest.fixture
def recording(tmp_path):
    """An 8 kHz clip, so DSD's 48 kHz requirement forces a derived copy."""
    path = tmp_path / "tx.wav"
    write_wav(str(path), 0.3 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000),
              8000)
    return str(path)


@pytest.fixture
def analyser():
    return DsdNeoAnalyser(executable=STUB)


def _run(analyser, recording, scenario, monkeypatch, **kwargs):
    monkeypatch.setenv("BABELFISHR_FAKE_DSD_SCENARIO", scenario)
    tx = Transmission(audio_path=recording)
    return analyser.analyse(AnalysisRequest(transmission=tx, **kwargs))


# ---- availability ------------------------------------------------------
def test_absent_dsd_does_not_break_anything():
    engine = DsdNeoAnalyser(executable="/nonexistent/dsd-neo")
    assert not engine.available()
    assert "optional" in engine.unavailable_reason() or "no such" in \
        engine.unavailable_reason()


def test_absent_dsd_returns_a_failed_attempt_not_an_exception(recording):
    engine = DsdNeoAnalyser(executable="/nonexistent/dsd-neo")
    attempt = engine.analyse(AnalysisRequest(
        transmission=Transmission(audio_path=recording)))
    assert attempt.outcome is AnalysisOutcome.ANALYSIS_FAILED
    assert attempt.error


def test_version_is_captured(analyser):
    assert analyser.available()
    assert "1.2.3-fake" in analyser.version()


# ---- outcomes ----------------------------------------------------------
@pytest.mark.parametrize("scenario,expected", [
    ("dmr-voice", AnalysisOutcome.VOICE_DECODED),
    ("p25-metadata", AnalysisOutcome.PROTOCOL_IDENTIFIED),
    ("encrypted", AnalysisOutcome.ENCRYPTED_OR_UNSUPPORTED),
    ("nothing", AnalysisOutcome.NO_RESULT),
    ("crash", AnalysisOutcome.ANALYSIS_FAILED),
])
def test_outcome_taxonomy(analyser, recording, monkeypatch, scenario, expected):
    assert _run(analyser, recording, scenario, monkeypatch).outcome is expected


def test_no_result_is_distinct_from_failure(analyser, recording, monkeypatch):
    """'No usable decode from this input' is not 'the recording was lost'."""
    nothing = _run(analyser, recording, "nothing", monkeypatch)
    crashed = _run(analyser, recording, "crash", monkeypatch)
    assert nothing.outcome is not crashed.outcome
    assert "no usable decode" in nothing.outcome.label
    assert pathlib.Path(recording).exists()


def test_protocol_and_metadata_are_extracted(analyser, recording, monkeypatch):
    attempt = _run(analyser, recording, "dmr-voice", monkeypatch)
    assert attempt.protocol == "DMR"
    assert attempt.metadata["talkgroup"] == "2501"
    assert attempt.metadata["source_id"] == "1234567"


def test_encryption_is_reported_never_attacked(analyser, recording, monkeypatch):
    attempt = _run(analyser, recording, "encrypted", monkeypatch)
    assert attempt.outcome is AnalysisOutcome.ENCRYPTED_OR_UNSUPPORTED
    assert not attempt.outcome.is_success


def test_decoded_audio_becomes_a_separate_artifact(analyser, recording,
                                                   monkeypatch):
    attempt = _run(analyser, recording, "dmr-voice", monkeypatch)
    decoded = attempt.decoded_audio
    assert decoded and pathlib.Path(decoded).exists()
    assert decoded != recording
    samples, rate = read_wav(decoded)
    assert samples.size > 0 and rate > 0


def test_stdout_stderr_and_command_are_captured(analyser, recording, monkeypatch):
    attempt = _run(analyser, recording, "dmr-voice", monkeypatch)
    assert attempt.command and STUB in attempt.command[0]
    assert attempt.stdout
    assert attempt.exit_status == 0
    assert attempt.runtime_seconds >= 0


# ---- the original is sacred -------------------------------------------
def test_original_recording_is_never_modified(analyser, recording, monkeypatch):
    before = hashlib.sha256(pathlib.Path(recording).read_bytes()).hexdigest()
    for scenario in ("dmr-voice", "encrypted", "crash", "nothing"):
        _run(analyser, recording, scenario, monkeypatch)
    after = hashlib.sha256(pathlib.Path(recording).read_bytes()).hexdigest()
    assert after == before


def test_conversion_writes_a_separate_derived_file(analyser, recording,
                                                   monkeypatch):
    attempt = _run(analyser, recording, "nothing", monkeypatch)
    assert attempt.input_is_derived
    assert attempt.input_path != recording
    _, rate = read_wav(attempt.input_path)
    assert rate == 48_000
    derived = [a for a in attempt.artifacts if a.kind == "derived-input"]
    assert derived, "the derived input must be recorded as its own artifact"


def test_matching_rate_needs_no_derived_copy(tmp_path, analyser, monkeypatch):
    path = tmp_path / "48k.wav"
    write_wav(str(path), np.zeros(48_000), 48_000)
    attempt = _run(analyser, str(path), "nothing", monkeypatch)
    assert not attempt.input_is_derived
    assert attempt.input_path == str(path)


def test_missing_recording_fails_cleanly(analyser, tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_FAKE_DSD_SCENARIO", "nothing")
    attempt = analyser.analyse(AnalysisRequest(
        transmission=Transmission(audio_path=str(tmp_path / "gone.wav"))))
    assert attempt.outcome is AnalysisOutcome.ANALYSIS_FAILED
    assert "missing" in attempt.error


# ---- reruns ------------------------------------------------------------
def test_rerun_does_not_inherit_a_previous_decode(analyser, recording,
                                                  monkeypatch):
    """A stale decoded file must never be reported as a fresh success."""
    first = _run(analyser, recording, "dmr-voice", monkeypatch)
    assert first.outcome is AnalysisOutcome.VOICE_DECODED
    second = _run(analyser, recording, "nothing", monkeypatch)
    assert second.outcome is AnalysisOutcome.NO_RESULT
    assert second.decoded_audio is None


def test_protocol_specific_rerun_passes_the_flag(analyser, recording,
                                                 monkeypatch):
    attempt = _run(analyser, recording, "nothing", monkeypatch, protocol="DMR")
    assert attempt.requested_protocol == "DMR"
    assert "-fr" in attempt.command


def test_attempts_accumulate_on_the_transmission(config, store, recording,
                                                 monkeypatch):
    from babelfishr.app import BabelFishRApp
    from babelfishr.models import Session

    monkeypatch.setenv("BABELFISHR_FAKE_DSD_SCENARIO", "dmr-voice")
    config.analysis.dsd_path = STUB
    app = BabelFishRApp(config=config, store=store)
    session = store.save_session(Session())
    tx = Transmission(session_id=session.id, audio_path=recording)
    store.save_transmission(tx)

    first = app.analyze_digital(tx.id)
    second = app.analyze_digital(tx.id, protocol="P25 Phase 1")
    assert first and second
    reloaded = store.get_transmission(tx.id)
    assert len(reloaded.analysis_attempts) == 2
    assert reloaded.analysis_attempts[1].attempt_number == 2
    assert reloaded.latest_analysis.requested_protocol == "P25 Phase 1"
    assert reloaded.decoded_audio_path


def test_analysis_failure_does_not_damage_transcript(config, store, recording,
                                                     monkeypatch):
    from babelfishr.app import BabelFishRApp
    from babelfishr.models import Session

    monkeypatch.setenv("BABELFISHR_FAKE_DSD_SCENARIO", "crash")
    config.analysis.dsd_path = STUB
    app = BabelFishRApp(config=config, store=store)
    session = store.save_session(Session())
    tx = Transmission(session_id=session.id, audio_path=recording,
                      transcript="already transcribed", translation="translated")
    store.save_transmission(tx)

    app.analyze_digital(tx.id)
    reloaded = store.get_transmission(tx.id)
    assert reloaded.transcript == "already transcribed"
    assert reloaded.translation == "translated"
    assert pathlib.Path(reloaded.audio_path).exists()


# ---- provenance --------------------------------------------------------
def test_operator_frequency_is_never_reported_as_measured():
    tx = Transmission(frequency_mhz=462.575,
                      frequency_provenance=Provenance.OPERATOR)
    assert not tx.frequency_is_measured
    assert tx.frequency_provenance.label == "entered by operator"


def test_sdr_frequency_is_measured():
    tx = Transmission(frequency_mhz=462.575, frequency_provenance=Provenance.SDR)
    assert tx.frequency_is_measured


def test_profile_metadata_is_stamped_as_profile_derived(app, fixture_wav):
    from babelfishr.models import RadioProfile

    profile = app.save_profile(RadioProfile(
        name="UV-5R", channel_name="GMRS 16", frequency_mhz=462.5750))
    app.select_engines()
    app.start_session(replay_path=fixture_wav, profile_id=profile.id,
                      name="provenance")
    app.run_replay()
    for tx in app.transmissions():
        assert tx.frequency_provenance is Provenance.PROFILE
        assert not tx.frequency_is_measured, (
            "a profile value must never read as a measurement")


def test_audio_path_reports_no_rssi(app, fixture_wav):
    """An audio cable cannot measure signal strength, so none is invented."""
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="no-rssi")
    app.run_replay()
    for tx in app.transmissions():
        assert tx.rssi_dbm is None
        assert tx.rssi_provenance is Provenance.UNKNOWN


def test_provenance_round_trips_through_storage(store):
    from babelfishr.models import Session

    session = store.save_session(Session())
    tx = Transmission(session_id=session.id, frequency_mhz=145.5,
                      frequency_provenance=Provenance.SDR, rssi_dbm=-92.0,
                      rssi_provenance=Provenance.SDR)
    store.save_transmission(tx)
    loaded = store.get_transmission(tx.id)
    assert loaded.frequency_provenance is Provenance.SDR
    assert loaded.rssi_provenance is Provenance.SDR
    assert loaded.frequency_is_measured


def test_provenance_appears_in_exports(store):
    import json

    from babelfishr import export
    from babelfishr.models import Session

    session = store.save_session(Session())
    tx = Transmission(session_id=session.id, frequency_mhz=145.5,
                      frequency_provenance=Provenance.OPERATOR)
    store.save_transmission(tx)
    payload = json.loads(export.to_json(store.list_transmissions(), session))
    entry = payload["transmissions"][0]
    assert entry["frequency_provenance"] == "operator-entered"
    assert entry["frequency_is_measured"] is False


def test_analysis_attempts_round_trip_through_storage(store):
    from babelfishr.models import Session

    session = store.save_session(Session())
    tx = Transmission(session_id=session.id)
    tx.analysis_attempts.append(AnalysisAttempt(
        engine="dsd-neo", engine_version="1.2.3",
        outcome=AnalysisOutcome.PROTOCOL_IDENTIFIED, protocol="DMR",
        metadata={"talkgroup": "2501"}, stdout="sync", exit_status=0))
    store.save_transmission(tx)
    loaded = store.get_transmission(tx.id)
    assert len(loaded.analysis_attempts) == 1
    attempt = loaded.analysis_attempts[0]
    assert attempt.outcome is AnalysisOutcome.PROTOCOL_IDENTIFIED
    assert attempt.metadata["talkgroup"] == "2501"
    assert attempt.engine_version == "1.2.3"


# ---- SDR ---------------------------------------------------------------
def test_no_sdr_configured_by_default(config):
    from babelfishr.sources import build_signal_source, sdr_status

    status = sdr_status(config)
    assert not status["configured"]
    assert build_signal_source(config) is None


def test_untested_sdr_driver_is_refused_honestly(config):
    from babelfishr.sources import sdr_status

    config.sdr.driver = "rtlsdr"
    status = sdr_status(config)
    assert status["configured"] and not status["available"]
    assert "not bundled" in status["reason"]


def test_sdr_absence_does_not_affect_the_audio_pipeline(app, fixture_wav):
    app.config.sdr.driver = "rtlsdr"  # configured but unavailable
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="no-sdr")
    app.run_replay()
    assert len(app.transmissions()) == 5


def test_recorded_iq_source_satisfies_the_signal_interface(tmp_path):
    from babelfishr.sources import RecordedIQSource, SignalMetadata

    samples = np.sin(2 * np.pi * 1000 * np.arange(8000) / 8000)
    source = RecordedIQSource(
        samples, 8000,
        metadata=SignalMetadata(tuned_frequency_hz=462_575_000.0, rssi_dbm=-80.0))
    source.start()
    blocks = list(source.blocks())
    assert sum(b.samples.size for b in blocks) == samples.size
    metadata = source.metadata()
    assert metadata.frequency_mhz == pytest.approx(462.575)
    assert metadata.provenance is Provenance.SDR


def test_signal_source_metadata_reaches_the_transmission(config, store, tmp_path):
    """A measured frequency must arrive stamped as SDR-measured."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.sources import RecordedIQSource, SignalMetadata
    from babelfishr.testing import build_fixture

    fixture = build_fixture(
        [{"gap": 1.0}, {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
         {"gap": 1.0}], sample_rate=48_000)
    source = RecordedIQSource(
        fixture.audio, 48_000,
        metadata=SignalMetadata(tuned_frequency_hz=462_575_000.0, rssi_dbm=-75.0,
                                modulation="NFM"))

    app = BabelFishRApp(config=config, store=store)
    app.select_engines()
    app.start_session(source=source, name="sdr")
    app.run_replay()

    transmissions = app.transmissions()
    assert transmissions
    tx = transmissions[0]
    assert tx.frequency_mhz == pytest.approx(462.575)
    assert tx.frequency_provenance is Provenance.SDR
    assert tx.frequency_is_measured
    assert tx.rssi_dbm == pytest.approx(-75.0)
    assert tx.modulation == "NFM"
    app.stop_session()
