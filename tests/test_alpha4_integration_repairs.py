"""Three integration defects a code audit found in 17ffad82.

Each one is a seam: a helper that existed but was never called, a query that
was written per-Session but reached from a global one, and a value pinned for
the life of a capture that nothing ever unpinned. Unit tests of the pieces all
passed. These drive the production paths instead.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import (DEFAULT_CONVERSATION_NAME, ProcessingState,
                               Provenance, Transmission)
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.sources import SignalMetadata
from babelfishr.storage import Store
from babelfishr.testing import build_fixture

SR = 48_000
BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, times: int = 8) -> None:
    for _ in range(times):
        qt_app.processEvents()


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


# ---- 1. source metadata reaches the transmission through the real path --


class _MeasuringSource:
    """A replay source that also reports RF metadata, like a real SDR would.

    Wraps ReplayAudioSource rather than reimplementing capture, so the test
    runs through the production CaptureService end to end.
    """

    measures_rf = True

    def __init__(self, path: str, metadata, block_size: int = 2048):
        from babelfishr.audio.source import ReplayAudioSource

        self._inner = ReplayAudioSource(path, realtime=False,
                                        block_size=block_size)
        self._metadata = metadata
        self.metadata_calls = 0

    def metadata(self):
        self.metadata_calls += 1
        return self._metadata

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _capture_with_metadata(config, store, wav, metadata):
    app = mock_app(config, store)
    source = _MeasuringSource(wav, metadata,
                              block_size=config.audio.block_size)
    app.start_session(source=source, name="measured")
    app.run_replay()
    app.stop_session()
    captured = app.recent_transmissions()
    assert captured, "the replay produced no transmissions"
    return app, captured


def test_signal_source_metadata_reaches_the_transmission(config, store, wav):
    """The whole contract, through CaptureService, not through the helper.

    On 17ffad82 frequency, RSSI and modulation arrived and SNR and the raw
    record did not, because the capture path still had its own hand-written
    copy of three fields and never called apply_source_metadata().
    """
    metadata = SignalMetadata(
        tuned_frequency_hz=462_562_500.0, sample_rate_hz=2_400_000.0,
        rssi_dbm=-73.0, snr_db=14.5, modulation="NFM",
        squelch_code="127.3 Hz", talkgroup="1201", unit_id="4021",
        protocol="DMR", source="rtl-sdr", extra={"gain_index": 21},
        provenance=Provenance.SDR)
    app, captured = _capture_with_metadata(config, store, wav, metadata)

    for tx in captured:
        assert tx.frequency_mhz == pytest.approx(462.5625)
        assert tx.frequency_provenance is Provenance.SDR
        assert tx.rssi_dbm == -73.0
        assert tx.rssi_provenance is Provenance.SDR
        assert tx.modulation == "NFM"
        assert tx.modulation_provenance is Provenance.SDR
        # The two the old hand-written path dropped entirely.
        assert tx.snr_db == 14.5, "SNR never reached the transmission"
        assert tx.snr_provenance is Provenance.SDR
        assert tx.signal_metadata, "the raw source record was not kept"
        # And the extended optional fields, through the same real path.
        assert tx.squelch_code == "127.3 Hz"
        assert tx.talkgroup == "1201"
        assert tx.unit_id == "4021"
        assert tx.protocol == "DMR"
        assert tx.has_supplied_unit_id is True
        raw = tx.signal_metadata["rtl-sdr"]
        assert raw["snr_db"] == 14.5
        assert raw["extra"]["gain_index"] == 21
    app.close()


def test_the_raw_record_survives_a_database_round_trip(config, store, wav):
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0, snr_db=9.0,
                              source="rtl-sdr", extra={"gain_index": 21},
                              provenance=Provenance.SDR)
    app, captured = _capture_with_metadata(config, store, wav, metadata)
    tx_id = captured[0].id
    app.close()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    restored = reopened.get_transmission(tx_id)
    assert restored.snr_db == 9.0
    assert restored.signal_metadata["rtl-sdr"]["extra"]["gain_index"] == 21
    # And it really is JSON, not a repr that happens to look like one.
    assert json.loads(json.dumps(restored.signal_metadata))
    reopened.close()


def test_metadata_a_source_cannot_serialise_never_costs_a_recording(
        config, store, wav):
    class _Awkward:
        def __repr__(self):
            return "<a driver object>"

    metadata = SignalMetadata(tuned_frequency_hz=1e8, source="odd",
                              extra={"handle": _Awkward()},
                              provenance=Provenance.SDR)
    app, captured = _capture_with_metadata(config, store, wav, metadata)
    for tx in captured:
        assert pathlib.Path(tx.audio_path).exists(), "capture-first broken"
    assert "a driver object" in json.dumps(captured[0].signal_metadata)
    app.close()


def test_a_source_that_raises_never_stops_the_recording(config, store, wav):
    class _Broken(_MeasuringSource):
        def metadata(self):
            raise RuntimeError("the dongle fell out")

    app = mock_app(config, store)
    app.start_session(source=_Broken(wav, None,
                                     block_size=config.audio.block_size),
                      name="broken")
    app.run_replay()
    app.stop_session()

    captured = app.recent_transmissions()
    assert captured, "a metadata failure stopped capture"
    for tx in captured:
        assert pathlib.Path(tx.audio_path).exists()
        assert tx.frequency_mhz is None
        assert tx.signal_metadata == {}
    app.close()


def test_a_source_that_states_no_provenance_is_not_called_measured(config,
                                                                    store, wav):
    """A recorded replay is not a live receiver, and must not claim to be."""
    metadata = SignalMetadata(tuned_frequency_hz=462_562_500.0, snr_db=3.0,
                              source="recorded-iq",
                              provenance=Provenance.UNKNOWN)
    app, captured = _capture_with_metadata(config, store, wav, metadata)
    for tx in captured:
        assert tx.frequency_mhz == pytest.approx(462.5625)
        assert tx.frequency_provenance is Provenance.UNKNOWN
        assert tx.frequency_is_measured is False
        assert tx.signal_summary()[0]["measured"] == "no"
    app.close()


def test_ordinary_audio_still_produces_no_rf_metadata(config, store, wav):
    """A microphone carries none of this, so none of it may appear."""
    app = mock_app(config, store)
    app.start_session(replay_path=wav, name="microphone")
    app.run_replay()
    app.stop_session()
    for tx in app.recent_transmissions():
        assert tx.frequency_mhz is None
        assert tx.rssi_dbm is None
        assert tx.snr_db is None
        assert tx.squelch_code == "" and tx.talkgroup == ""
        assert tx.unit_id == "" and tx.protocol == ""
        assert tx.signal_metadata == {}
        assert tx.signal_summary() == []
    app.close()


# ---- 2. search and the review queue are scoped to the named Session -----


def _two_sessions(config, store, wav):
    """General and Ops, each with a run containing the same phrase."""
    app = mock_app(config, store)
    general = app.conversation_id
    app.start_session(replay_path=wav, name="general run")
    app.run_replay()
    app.stop_session()

    ops = app.create_conversation("Ops North")
    app.select_conversation(ops.id)
    app.start_session(replay_path=wav, name="ops run")
    app.run_replay()
    app.stop_session()

    phrase = "windmill"
    for conversation_id in (general, ops.id):
        for tx in app.recent_transmissions(conversation_id=conversation_id):
            tx.transcript = f"{phrase} sighted"
            tx.transcript_confidence = 0.2      # reviewable
            tx.state = ProcessingState.COMPLETE
            store.save_transmission(tx)
    return app, general, ops.id, phrase


def test_search_returns_only_the_selected_sessions_transmissions(config, store,
                                                                  wav):
    app, general, ops, phrase = _two_sessions(config, store, wav)
    in_general = {t.id for t in app.recent_transmissions(conversation_id=general)}
    in_ops = {t.id for t in app.recent_transmissions(conversation_id=ops)}
    assert in_general and in_ops and not in_general & in_ops

    app.select_conversation(general)
    found = {t.id for t in app.search(phrase)}
    assert found == in_general, "search leaked another Session's transmissions"

    app.select_conversation(ops)
    found = {t.id for t in app.search(phrase)}
    assert found == in_ops
    app.close()


def test_the_review_queue_is_scoped_to_the_selected_session(config, store, wav):
    app, general, ops, _ = _two_sessions(config, store, wav)
    in_general = {t.id for t in app.recent_transmissions(conversation_id=general)}
    in_ops = {t.id for t in app.recent_transmissions(conversation_id=ops)}

    app.select_conversation(general)
    queued = {t.id for t in app.review_queue()}
    assert queued, "nothing was reviewable, so this proves nothing"
    assert queued == in_general, "the review queue leaked another Session"

    app.select_conversation(ops)
    assert {t.id for t in app.review_queue()} == in_ops
    app.close()


def test_search_and_review_can_still_be_asked_for_everything(config, store, wav):
    app, general, ops, phrase = _two_sessions(config, store, wav)
    everywhere = {t.id for t in app.search(phrase, conversation_id=None)}
    both = ({t.id for t in app.recent_transmissions(conversation_id=general)}
            | {t.id for t in app.recent_transmissions(conversation_id=ops)})
    assert everywhere == both
    assert {t.id for t in app.review_queue(conversation_id=None)} == both
    app.close()


def test_the_window_search_and_review_stay_inside_the_open_tab(qt_app, config,
                                                               store, wav):
    from babelfishr.ui.main_window import MainWindow

    app, general, ops, phrase = _two_sessions(config, store, wav)
    in_general = {t.id for t in app.recent_transmissions(conversation_id=general)}
    app.select_conversation(general)

    window = MainWindow(app)
    pump(qt_app)
    window.timeline.set_transmissions(app.search(phrase))
    assert set(window.timeline._bubbles) == in_general

    window.timeline.set_transmissions(app.review_queue())
    assert set(window.timeline._bubbles) == in_general
    window.hide()
    app.close()


def test_normal_thread_filtering_is_unchanged(config, store, wav):
    app, general, ops, _ = _two_sessions(config, store, wav)
    app.select_conversation(general)
    general_ids = {t.id for t in app.recent_transmissions()}
    app.select_conversation(ops)
    ops_ids = {t.id for t in app.recent_transmissions()}
    assert general_ids and ops_ids and not general_ids & ops_ids
    app.close()


# ---- 3. the capture destination is cleared when monitoring stops --------


def test_switching_tabs_during_capture_still_does_not_redirect(config, store,
                                                                wav):
    app = mock_app(config, store)
    general = app.conversation_id
    ops = app.create_conversation("Ops North")

    app.start_session(replay_path=wav, name="live")
    assert app.capture_conversation_id == general
    app.select_conversation(ops.id)
    assert app.capture_conversation_id == general, (
        "switching tabs redirected the running capture")
    app.run_replay()
    app.stop_session()

    assert app.recent_transmissions(conversation_id=ops.id) == []
    assert app.recent_transmissions(conversation_id=general)
    app.close()


def test_the_capture_destination_is_empty_after_a_normal_stop(config, store,
                                                               wav):
    app = mock_app(config, store)
    app.start_session(replay_path=wav, name="live")
    assert app.capture_conversation_id
    app.run_replay()
    app.stop_session()
    assert app.capture_conversation_id == "", (
        "the destination still names a capture that has stopped")
    app.close()


def test_closing_the_app_clears_the_capture_destination(config, store, wav):
    app = mock_app(config, store)
    app.start_session(replay_path=wav, name="live")
    assert app.capture_conversation_id
    app.close()
    assert app.capture_conversation_id == ""


def test_a_failed_start_leaves_no_stale_destination(config, store):
    from babelfishr.audio.devices import DeviceIdentity, InputDeviceMissing

    app = mock_app(config, store)
    app.create_conversation("Ops North")

    def refuse(*args, **kwargs):
        raise InputDeviceMissing(DeviceIdentity(name="USB Audio CODEC"))

    app._build_source = refuse
    with pytest.raises(InputDeviceMissing):
        app.start_session(name="doomed")
    assert app.capture_conversation_id == "", (
        "a start that never completed left a destination behind")
    assert app.session is None
    assert app.pipeline is None
    app.close()


def test_a_failure_after_the_pin_also_clears_the_destination(config, store, wav):
    """The pin happens before the Session row is written, so the window
    between them has to be covered too."""
    app = mock_app(config, store)
    original = app.store.save_session

    def explode(session):
        raise RuntimeError("the database went away")

    app.store.save_session = explode
    with pytest.raises(RuntimeError):
        app.start_session(replay_path=wav, name="doomed")
    app.store.save_session = original
    assert app.capture_conversation_id == ""
    assert app.session is None
    app.close()


def test_the_recording_into_label_clears_when_monitoring_stops(qt_app, config,
                                                                store, wav):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    ops = app.create_conversation("Ops North")
    window = MainWindow(app)
    pump(qt_app)

    window._start_monitoring(replay_path=wav)
    pump(qt_app)
    # The operator wanders off to another tab while the watch runs.
    index = next(i for i in range(window.session_tabs.count())
                 if window.session_tabs.tabData(i) == ops.id)
    window.session_tabs.setCurrentIndex(index)
    pump(qt_app)
    assert "Recording into" in window.capture_tab_label.text(), (
        "the operator was not told where live traffic was going")

    window._stop_monitoring()
    pump(qt_app)
    assert window.capture_tab_label.text() == "", (
        "the window still says it is recording after monitoring stopped")
    window.hide()
    app.close()
