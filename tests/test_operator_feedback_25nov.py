"""Operator feedback after build 34 (2026-11-25), no audio or text needed:

- the calibrated threshold, once applied, survives a relaunch (it used to
  change only the running copy);
- the activity badge says what the work is: messages queued behind the one
  being processed, Finishing after Stop, and continuous audio (handed on in
  parts because it never went quiet for the message-ending time);
- exporting a message whose original recording file is gone says so (Play
  already reports it); the words are kept.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import threading
import time

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.models import ProcessingState, Transmission
from babelfishr.pipeline import PipelineState
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _window(config, store):
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    return app, MainWindow(app)


def test_an_applied_calibration_is_saved(qt_app, config, store, tmp_path):
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    before = Config.load(config.source_path)
    app, window = _window(config, store)
    try:
        window._apply_calibration(-31.5)
        again = Config.load(config.source_path)
        assert again.detector.mode == "fixed"
        assert again.detector.threshold_dbfs == -31.5
        # Nothing else changes: sensitivity margins, quiet, model choice.
        assert again.detector.hang_time == before.detector.hang_time
        assert again.detector.open_margin_db == before.detector.open_margin_db
        assert again.asr.model == before.asr.model
    finally:
        window.close()
        app.close()


class _Pipe:
    def __init__(self, n, automatic=None):
        self.visible_pending = n
        self.pending = n
        self.automatic_pending = n if automatic is None else automatic


def test_the_badge_counts_queued_messages_and_says_finishing(qt_app, config, store):
    app, window = _window(config, store)
    try:
        app.pipeline, app.standalone_pipeline = _Pipe(3), None
        app.capture = object()                     # monitoring
        assert window._badge_detail(PipelineState.TRANSCRIBING, "Transcribing") \
            == "Transcribing · 2 queued"
        app.capture = None                         # stopped, still working
        app.pipeline, app.standalone_pipeline = None, _Pipe(1)
        assert window._badge_detail(PipelineState.TRANSCRIBING, "Transcribing") == "Finishing"
        app.standalone_pipeline = _Pipe(4)
        assert window._badge_detail(PipelineState.TRANSLATING, "Translating") \
            == "Finishing · 3 queued"
    finally:
        app.pipeline = app.standalone_pipeline = None
        app.capture = None
        window.close()
        app.close()


def test_the_badge_says_continuous_while_parts_are_handed_on(qt_app, config, store):
    app, window = _window(config, store)

    class _Det:
        continuing = True

    class _Cap:
        detector = _Det()
        state = PipelineState.RECEIVING

    try:
        app.capture = _Cap()
        assert window._badge_detail(PipelineState.RECEIVING, "Audio active (est.)") \
            == "Audio active (est.) · continuous"
        _Det.continuing = False
        assert window._badge_detail(PipelineState.RECEIVING, "Audio active (est.)") \
            == "Audio active (est.)"
    finally:
        app.capture = None
        window.close()
        app.close()


def test_the_detector_reports_a_continuing_message():
    from babelfishr.audio.source import AudioBlock
    from babelfishr.detect import DetectorSettings, RadioActivityDetector
    from babelfishr.testing import speech_like

    sr = 48_000
    det = RadioActivityDetector(sr, DetectorSettings(mode="fixed", threshold_dbfs=-20.0))
    loud = speech_like(20.0, sr, level_dbfs=-14.0, seed=1)
    loud = loud / np.max(np.abs(loud)) * 0.7 + np.random.default_rng(2).normal(0, 0.07, loud.size)
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    seen = []
    for i in range(0, loud.size, 2048):
        out = det.push(AudioBlock(samples=loud[i:i + 2048], sample_rate=sr,
                                  timestamp=t0 + dt.timedelta(seconds=i / sr), offset=i / sr))
        seen.append((bool(out), det.continuing))
    assert not seen[0][1], "a fresh message is not a continuation"
    first_part = next(k for k, (out, _) in enumerate(seen) if out)
    assert all(cont for _, cont in seen[first_part + 1:]), "after a part, it continues"


def test_exporting_a_removed_recording_says_so_and_keeps_the_words(
        qt_app, config, store, tmp_path, monkeypatch):
    """Play already reports a missing file (tests/test_alpha5_playback.py);
    Export audio used to raise from the slot instead of saying so."""
    from PySide6 import QtWidgets

    from babelfishr.models import Session

    app, window = _window(config, store)
    try:
        store.save_session(Session(id="s1", conversation_id=app.conversation_id))
        tx = Transmission(session_id="s1", started_at=dt.datetime.now(dt.timezone.utc),
                          duration=2.0, state=ProcessingState.COMPLETE,
                          transcript="units responding",
                          audio_path=str(tmp_path / "removed.wav"))
        store.save_transmission(tx)
        warned, asked = [], []
        monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: warned.append(a)))
        monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (asked.append(a), ("", ""))[1]))
        window._on_export_clip(tx.id)
        assert warned and "no longer on disk" in warned[0][2]
        assert not asked, "a save dialog was offered for audio that is gone"
        assert store.get_transmission(tx.id).transcript == "units responding"
    finally:
        window.close()
        app.close()
