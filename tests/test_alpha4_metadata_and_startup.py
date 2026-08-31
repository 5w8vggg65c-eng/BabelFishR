"""Three defects found against d323ccc, all at boundaries.

Two are about a value crossing from a driver into the database and out again;
the third is about a monitoring run that was written down and then abandoned.
Every test drives the production path — CaptureService through a wrapper that
reports metadata the way a driver would, and start_session itself — because
each defect lived in the joins rather than in any one function.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import sqlite3

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState, Provenance, Transmission
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.signal_metadata import apply_source_metadata
from babelfishr.sources import SignalMetadata
from babelfishr.storage import Store
from babelfishr.testing import build_fixture

SR = 48_000


@pytest.fixture
def wav(tmp_path) -> str:
    return build_fixture([{"gap": 1.2},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
                          {"gap": 1.2}], sample_rate=SR).write(
        str(tmp_path / "voice.wav"))


def mock_app(config, store) -> BabelFishRApp:
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


class _MeasuringSource:
    """A replay source that also reports RF metadata, as a driver would.

    Wraps the production ReplayAudioSource so capture runs through the real
    CaptureService rather than a stand-in for it.
    """

    measures_rf = True

    def __init__(self, path: str, metadata, block_size: int = 2048):
        from babelfishr.audio.source import ReplayAudioSource

        self._inner = ReplayAudioSource(path, realtime=False,
                                        block_size=block_size)
        self._metadata = metadata

    def metadata(self):
        return self._metadata

    def __getattr__(self, name):
        return getattr(self._inner, name)


def capture_with(config, store, wav, metadata):
    app = mock_app(config, store)
    app.start_session(source=_MeasuringSource(wav, metadata,
                                              block_size=config.audio.block_size),
                      name="measured")
    app.run_replay()
    app.stop_session()
    captured = app.recent_transmissions()
    assert captured, "the replay produced no transmissions"
    return app, captured


# ---- 1. measured status requires an affirmative claim -------------------


def test_omitted_provenance_is_unknown_and_not_measured(config, store, wav):
    """The default was SDR, so a source that said nothing read as measured."""
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0, rssi_dbm=-73.0)
    assert metadata.provenance is Provenance.UNKNOWN

    app, captured = capture_with(config, store, wav, metadata)
    for tx in captured:
        assert tx.frequency_mhz == pytest.approx(462.5625)
        assert tx.frequency_provenance is Provenance.UNKNOWN
        assert tx.frequency_is_measured is False
        assert tx.rssi_provenance is Provenance.UNKNOWN
        entries = {e["label"]: e for e in tx.signal_summary()}
        assert entries["frequency"]["measured"] == "no"
        assert "unverified" in entries["frequency"]["display"]
    app.close()


def test_an_explicit_sdr_claim_is_still_measured(config, store, wav):
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0, rssi_dbm=-73.0,
                              snr_db=14.0, provenance=Provenance.SDR)
    app, captured = capture_with(config, store, wav, metadata)
    for tx in captured:
        assert tx.frequency_provenance is Provenance.SDR
        assert tx.frequency_is_measured is True
        assert tx.rssi_provenance is Provenance.SDR
        assert tx.snr_provenance is Provenance.SDR
        assert tx.signal_summary()[0]["measured"] == "yes"
    app.close()


def test_a_radio_reported_claim_is_measured_and_labelled_as_the_radio():
    """RADIO keeps its existing meaning; only the default changed."""
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0,
                              provenance=Provenance.RADIO)
    tx = Transmission(session_id="s")
    apply_source_metadata(tx, metadata)
    assert tx.frequency_provenance is Provenance.RADIO
    assert tx.frequency_is_measured is True


@pytest.mark.parametrize("claimed", ["", "totally-bogus", "SDR", None, 7])
def test_an_invalid_provenance_stays_unknown(claimed, config, store, wav):
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0,
                              provenance=claimed)
    app, captured = capture_with(config, store, wav, metadata)
    for tx in captured:
        assert tx.frequency_provenance is Provenance.UNKNOWN, (
            f"provenance {claimed!r} was accepted")
        assert tx.frequency_is_measured is False
    app.close()


def test_a_recorded_iq_source_does_not_claim_to_be_a_receiver():
    """Replaying a recording is not taking a measurement."""
    from babelfishr.sources import RecordedIQSource

    samples = np.sin(2 * np.pi * 1000 * np.arange(8000) / 8000)
    source = RecordedIQSource(samples, 8000)
    assert source.metadata().provenance is Provenance.UNKNOWN


# ---- 2. source scalars must not corrupt persisted measurements ----------


@pytest.mark.parametrize("scalar", [np.float32, np.float64])
def test_numpy_measurements_survive_capture_storage_and_rendering(
        scalar, config, store, wav):
    """The whole path: driver scalar to SQLite to reload to the bubble.

    A NumPy float32 binds to SQLite as a blob. The row was written, the value
    came back as bytes, and signal_summary() raised
    TypeError: unsupported format string passed to bytes.__format__.
    """
    metadata = SignalMetadata(tuned_frequency_hz=scalar(462_562_500.0),
                              rssi_dbm=scalar(-73.5), snr_db=scalar(14.25),
                              source="rtl-sdr", provenance=Provenance.SDR)
    app, captured = capture_with(config, store, wav, metadata)
    tx_id = captured[0].id

    for tx in captured:
        assert type(tx.rssi_dbm) is float
        assert type(tx.snr_db) is float
        assert type(tx.frequency_mhz) is float

    database = config.database
    app.close()

    reopened = Store(database, recordings_dir=config.recording.directory)
    stored = reopened._conn.execute(
        "SELECT typeof(frequency_mhz), typeof(rssi_dbm), typeof(snr_db) "
        "FROM transmissions WHERE id = ?", (tx_id,)).fetchone()
    assert set(tuple(stored)) == {"real"}, (
        f"SQLite stored the measurements as {tuple(stored)}, not real numbers")

    restored = reopened.get_transmission(tx_id)
    assert type(restored.rssi_dbm) is float
    assert type(restored.snr_db) is float
    assert type(restored.frequency_mhz) is float
    assert restored.rssi_dbm == pytest.approx(-73.5, abs=1e-4)
    assert restored.snr_db == pytest.approx(14.25, abs=1e-4)
    assert restored.frequency_mhz == pytest.approx(462.5625, abs=1e-4)
    assert restored.rssi_provenance is Provenance.SDR
    assert restored.snr_provenance is Provenance.SDR

    # The point is that it renders at all: before the fix this raised
    # TypeError on bytes. The exact rounding is Python's, not ours.
    rendered = {e["label"]: e["display"] for e in restored.signal_summary()}
    assert "RSSI" in rendered and "SNR" in rendered
    assert rendered["RSSI"] == f"RSSI {-73.5:.0f} dBm"
    assert rendered["SNR"] == f"SNR {14.25:.0f} dB"
    reopened.close()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                 True, False, "quite loud", None,
                                 np.float32("nan")])
def test_a_malformed_measurement_is_not_promoted_and_costs_no_recording(
        bad, config, store, wav):
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0, rssi_dbm=bad,
                              source="odd", provenance=Provenance.SDR)
    app, captured = capture_with(config, store, wav, metadata)
    for tx in captured:
        assert pathlib.Path(tx.audio_path).exists(), "capture-first broken"
        assert tx.rssi_dbm is None, f"{bad!r} was promoted as a measurement"
        assert tx.rssi_provenance is Provenance.UNKNOWN
        # The frequency alongside it still arrived, and the raw report is kept.
        assert tx.frequency_mhz == pytest.approx(462.5625)
        assert "odd" in tx.signal_metadata
        assert json.loads(json.dumps(tx.signal_metadata)) is not None
        assert "RSSI" not in {e["label"] for e in tx.signal_summary()}
    app.close()


def test_the_raw_report_stays_json_safe_and_keeps_numbers_as_numbers(
        config, store, wav):
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0,
                              rssi_dbm=np.float32(-73.5), source="rtl-sdr",
                              extra={"gain_index": np.int32(21),
                                     "handle": object()},
                              provenance=Provenance.SDR)
    app, captured = capture_with(config, store, wav, metadata)
    database = config.database
    tx_id = captured[0].id
    app.close()

    reopened = Store(database, recordings_dir=config.recording.directory)
    raw = reopened.get_transmission(tx_id).signal_metadata["rtl-sdr"]
    assert raw["rssi_dbm"] == pytest.approx(-73.5, abs=1e-4)
    assert isinstance(raw["rssi_dbm"], float), "a number became a string"
    assert raw["extra"]["gain_index"] == 21
    assert isinstance(raw["extra"]["handle"], str), (
        "an unserialisable object was not coerced")
    reopened.close()


def test_ordinary_replay_audio_still_invents_no_rf_metadata(config, store, wav):
    app = mock_app(config, store)
    app.start_session(replay_path=wav, name="microphone")
    app.run_replay()
    app.stop_session()
    for tx in app.recent_transmissions():
        assert tx.frequency_mhz is None and tx.rssi_dbm is None
        assert tx.snr_db is None and tx.squelch_code == ""
        assert tx.talkgroup == "" and tx.unit_id == "" and tx.protocol == ""
        assert tx.signal_metadata == {}
        assert tx.signal_summary() == []
    app.close()


# ---- 3. a start that failed must not read as a run in progress ----------


def open_session_rows(store) -> list:
    return [dict(r) for r in store._conn.execute(
        "SELECT id, name, ended_at FROM sessions WHERE ended_at IS NULL")]


def fail_after_save_session(app, monkeypatch, message="worker start failed"):
    """Break ProcessingPipeline.start, which runs after the row is written."""
    from babelfishr.pipeline import ProcessingPipeline

    def boom(self, session=None):
        raise RuntimeError(message)

    monkeypatch.setattr(ProcessingPipeline, "start", boom)


def test_a_failure_after_save_session_leaves_no_open_run(config, store, wav,
                                                          monkeypatch):
    app = mock_app(config, store)
    fail_after_save_session(app, monkeypatch)

    with pytest.raises(RuntimeError):
        app.start_session(replay_path=wav, name="doomed")

    assert app.capture_conversation_id == ""
    assert app.session is None
    assert app.capture is None
    assert app.pipeline is None
    assert open_session_rows(store) == [], (
        "the abandoned run is still open, and nothing can close it")
    app.close()


def test_the_cleanup_does_not_mask_the_original_failure(config, store, wav,
                                                        monkeypatch):
    app = mock_app(config, store)
    fail_after_save_session(app, monkeypatch, message="the dongle fell out")

    with pytest.raises(RuntimeError) as excinfo:
        app.start_session(replay_path=wav, name="doomed")
    assert "the dongle fell out" in str(excinfo.value), (
        "the operator got the cleanup's error instead of the real one")
    app.close()


def test_a_cleanup_that_itself_fails_still_reraises_the_real_error(
        config, store, wav, monkeypatch):
    app = mock_app(config, store)
    fail_after_save_session(app, monkeypatch, message="the real problem")

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("the database is locked")

    monkeypatch.setattr(app.store, "close_session", refuse)
    with pytest.raises(RuntimeError) as excinfo:
        app.start_session(replay_path=wav, name="doomed")
    assert "the real problem" in str(excinfo.value)
    assert app.session is None and app.pipeline is None
    app.close()


def test_a_valid_run_starts_and_stops_after_a_failed_one(config, store, wav,
                                                          monkeypatch):
    app = mock_app(config, store)
    fail_after_save_session(app, monkeypatch)
    with pytest.raises(RuntimeError):
        app.start_session(replay_path=wav, name="doomed")

    monkeypatch.undo()
    session = app.start_session(replay_path=wav, name="good")
    assert session is not None
    app.run_replay()
    app.stop_session()

    assert app.recent_transmissions(), "the recovered run captured nothing"
    assert open_session_rows(store) == []
    app.close()


def test_the_abandoned_run_is_closed_and_says_why(config, store, wav,
                                                   monkeypatch):
    """Implementation choice, not a stated requirement: the row is kept and
    closed rather than deleted, so the attempt stays in the audit history."""
    app = mock_app(config, store)
    fail_after_save_session(app, monkeypatch, message="worker start failed")
    with pytest.raises(RuntimeError):
        app.start_session(replay_path=wav, name="doomed")

    rows = [dict(r) for r in store._conn.execute(
        "SELECT name, ended_at, notes FROM sessions")]
    assert len(rows) == 1, "the failed run was deleted rather than closed"
    assert rows[0]["ended_at"] is not None
    assert "failed to start" in rows[0]["notes"]

    # And it holds nothing, so it cannot appear as an operator Session.
    assert app.recent_transmissions() == []
    app.close()


def test_a_failure_before_the_row_is_written_creates_no_row(config, store,
                                                             monkeypatch):
    from babelfishr.audio.devices import DeviceIdentity, InputDeviceMissing

    app = mock_app(config, store)

    def refuse(*args, **kwargs):
        raise InputDeviceMissing(DeviceIdentity(name="USB Audio CODEC"))

    monkeypatch.setattr(app, "_build_source", refuse)
    with pytest.raises(InputDeviceMissing):
        app.start_session(name="doomed")

    assert app.capture_conversation_id == ""
    assert app.session is None and app.pipeline is None
    assert [dict(r) for r in store._conn.execute("SELECT id FROM sessions")] == []
    app.close()


# ---- 9. the d323ccc repairs are still in place --------------------------


def test_the_metadata_helper_is_still_wired_into_capture(config, store, wav):
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0, snr_db=9.5,
                              squelch_code="127.3 Hz", talkgroup="1201",
                              unit_id="4021", protocol="DMR", source="rtl-sdr",
                              provenance=Provenance.SDR)
    app, captured = capture_with(config, store, wav, metadata)
    for tx in captured:
        assert tx.snr_db == pytest.approx(9.5)
        assert tx.signal_metadata, "the raw record is not being kept"
        assert tx.squelch_code == "127.3 Hz" and tx.talkgroup == "1201"
        assert tx.unit_id == "4021" and tx.protocol == "DMR"
        assert tx.has_supplied_unit_id is True
    app.close()


def test_search_and_review_are_still_scoped_to_the_named_session(config, store,
                                                                 wav):
    app = mock_app(config, store)
    general = app.conversation_id
    app.start_session(replay_path=wav, name="general")
    app.run_replay()
    app.stop_session()

    ops = app.create_conversation("Ops North")
    app.select_conversation(ops.id)
    app.start_session(replay_path=wav, name="ops")
    app.run_replay()
    app.stop_session()

    for conversation_id in (general, ops.id):
        for tx in app.recent_transmissions(conversation_id=conversation_id):
            tx.transcript = "windmill sighted"
            tx.transcript_confidence = 0.2
            tx.state = ProcessingState.COMPLETE
            store.save_transmission(tx)

    in_general = {t.id for t in app.recent_transmissions(conversation_id=general)}
    in_ops = {t.id for t in app.recent_transmissions(conversation_id=ops.id)}
    assert in_general and in_ops and not in_general & in_ops

    app.select_conversation(general)
    assert {t.id for t in app.search("windmill")} == in_general
    assert {t.id for t in app.review_queue()} == in_general
    app.select_conversation(ops.id)
    assert {t.id for t in app.search("windmill")} == in_ops
    assert {t.id for t in app.review_queue()} == in_ops
    app.close()


def test_the_capture_destination_is_still_cleared_on_stop(config, store, wav):
    app = mock_app(config, store)
    app.start_session(replay_path=wav, name="live")
    assert app.capture_conversation_id
    app.run_replay()
    app.stop_session()
    assert app.capture_conversation_id == ""
    app.close()
