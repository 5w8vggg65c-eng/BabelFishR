"""Tests moved from shared BabelFishR test files with the code they cover
(the recorded-IQ source, decoded identifiers, and digital analysis during
Quit): test_alpha4_metadata_and_startup.py,
test_alpha4_thread_and_sessions.py and test_alpha5_shutdown.py.
"""

from __future__ import annotations

import threading

import numpy as np

from babelfishr.models import (AnalysisAttempt, AnalysisOutcome, ProcessingState,
                               Provenance, Transmission)
from kingfishr.digital import analyze_digital

from test_alpha5_shutdown import (VOICE_OPEN, cleanup, heartbeat,  # noqa: F401
                                  pump, pump_until, qt_app, wav,
                                  window_on_held_capture,
                                  window_with_a_saved_message)


def test_a_recorded_iq_source_does_not_claim_to_be_a_receiver():
    """Replaying a recording is not taking a measurement."""
    from kingfishr.sources import RecordedIQSource

    samples = np.sin(2 * np.pi * 1000 * np.arange(8000) / 8000)
    source = RecordedIQSource(samples, 8000)
    assert source.metadata().provenance is Provenance.UNKNOWN


def test_decoded_identifiers_are_materialised_with_dsd_provenance():
    from kingfishr.decoded_metadata import apply_decoded_metadata

    transmission = Transmission(session_id="s")
    attempt = AnalysisAttempt(
        engine="dsd-neo", engine_version="1.2.3", protocol="DMR",
        outcome=AnalysisOutcome.PROTOCOL_IDENTIFIED,
        metadata={"talkgroup": "1201", "source_id": "4021",
                  "color_code": "1", "nac": "293"})
    assert apply_decoded_metadata(transmission, attempt) is True

    assert transmission.talkgroup == "1201"
    assert transmission.talkgroup_provenance is Provenance.DSD
    assert transmission.unit_id == "4021"
    assert transmission.unit_id_provenance is Provenance.DSD
    assert transmission.protocol == "DMR"
    assert transmission.protocol_provenance is Provenance.DSD
    assert transmission.has_supplied_unit_id is True
    # The raw output is kept whole, including the keys not promoted.
    assert transmission.signal_metadata["dsd-neo"]["nac"] == "293"
    assert transmission.signal_metadata["dsd-neo"]["color_code"] == "1"


class FakeAnalyser:
    """Stands in for dsd-neo at the production seam (from_config), which is
    consulted several times per run (availability, then the analysis
    itself), so the gates are shared by every instance. Blocks inside
    analyse() until released, then returns a real attempt."""

    entered = threading.Event()
    release = threading.Event()
    configured = True
    executable = "fake-dsd"

    def available(self):
        return True

    def unavailable_reason(self):
        return ""

    def version(self):
        return "0"

    def analyse(self, request):
        FakeAnalyser.entered.set()
        assert FakeAnalyser.release.wait(60.0)
        return AnalysisAttempt(transmission_id=request.transmission.id,
                               engine="fake-analyser", engine_version="0")


def install_fake_analyser(monkeypatch):
    from kingfishr.analysis.dsd import DsdNeoAnalyser

    FakeAnalyser.entered = threading.Event()
    FakeAnalyser.release = threading.Event()
    monkeypatch.setattr(DsdNeoAnalyser, "from_config",
                        classmethod(lambda cls, config: FakeAnalyser()))


def test_a_running_digital_analysis_saves_before_the_store_closes(qt_app, config, store,
                                                                  wav, monkeypatch):
    app, window, tx = window_with_a_saved_message(qt_app, config, store, wav)
    install_fake_analyser(monkeypatch)
    order = []
    real_save, real_close = store.save_transmission, store.close
    store.save_transmission = lambda t: (order.append("save"), real_save(t))[1]
    store.close = lambda: (order.append("store-close"), real_close())[1]
    timer, ticks = heartbeat(qt_app)
    try:
        window._on_analyze_digital(tx.id, "")              # the real window path
        assert FakeAnalyser.entered.wait(10.0), "the analysis never started"
        assert app.active_operations() == {"digital analysis": 1}

        assert window.close() is False, "Quit was accepted with an analysis still running"
        before = len(ticks)
        pump(qt_app, 40)
        assert len(ticks) >= before + 3
        assert "digital analysis" in window.status.currentMessage()
        assert "store-close" not in order and store.get_transmission(tx.id) is not None

        FakeAnalyser.release.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
        assert order.index("save") < order.index("store-close"), order
        from babelfishr.storage import Store
        again = Store(config.database, recordings_dir=config.recording.directory)
        saved = again.get_transmission(tx.id)
        again.close()
        assert len(saved.analysis_attempts) == 1
        assert saved.analysis_attempts[0].engine == "fake-analyser"
    finally:
        timer.stop()
        FakeAnalyser.release.set()
        pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0) or window.close()


def test_digital_analysis_is_refused_while_quit_is_pending(qt_app, config, store, wav,
                                                          monkeypatch):
    """The analysis half of test_new_work_is_refused_while_quit_is_pending."""
    app, window, source = window_on_held_capture(qt_app, config, store, VOICE_OPEN)
    install_fake_analyser(monkeypatch)
    timer, _ = heartbeat(qt_app)
    try:
        window._stop_monitoring()
        assert window.close() is False
        assert app.closing
        from babelfishr.models import Session
        store.save_session(Session(id="old", conversation_id=app.conversation_id))
        store.save_transmission(Transmission(id="old-tx", session_id="old", audio_path=wav,
                                             state=ProcessingState.SKIPPED))
        assert analyze_digital(app, "old-tx") is None
        assert not FakeAnalyser.entered.is_set()
        window._on_analyze_digital("old-tx", "")
        assert "not started" in window.status.currentMessage()
        pump(qt_app, 10)
        assert not FakeAnalyser.entered.is_set()
        source.released.set()
        assert pump_until(qt_app, lambda: not window.isVisible(), timeout=20.0)
    finally:
        cleanup(qt_app, window, source, timer)
