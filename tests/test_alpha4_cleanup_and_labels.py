"""Two defects found against 122ae9ed, both where a claim outran the code.

The first is a docstring that said "every step here is inside its own guard"
over two operations sharing one try block. The second is a data model that
learned to distinguish six kinds of provenance and a bubble that still asked
one boolean question of it. Both are only visible from outside the function
that got them wrong, which is why these drive start_session and
TransmissionBubble rather than the helpers underneath.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState, Provenance, Transmission
from babelfishr.pipeline import ProcessingPipeline
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.sources import SignalMetadata
from babelfishr.testing import build_fixture

SR = 48_000


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


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


def fail_worker_start(monkeypatch, message="worker startup failed") -> None:
    """Break ProcessingPipeline.start, which runs after the row is written."""
    def boom(self, session=None):
        raise RuntimeError(message)

    monkeypatch.setattr(ProcessingPipeline, "start", boom)


def open_rows(store) -> list:
    return [dict(r) for r in store._conn.execute(
        "SELECT id, name, ended_at FROM sessions WHERE ended_at IS NULL")]


def break_cleanup_note(store, monkeypatch) -> list:
    """Make the *cleanup* save_session fail, and watch close_session.

    Only the cleanup save is broken: the initial one must still succeed, or
    there would be no row to leave open and nothing to prove.
    """
    real_save = store.save_session
    real_close = store.close_session
    attempted: list = []

    def failing_save(session):
        if session.ended_at is not None:
            raise sqlite3.OperationalError("note update failed")
        return real_save(session)

    def watched_close(session_id, ended_at=None):
        attempted.append(session_id)
        return real_close(session_id, ended_at)

    monkeypatch.setattr(store, "save_session", failing_save)
    monkeypatch.setattr(store, "close_session", watched_close)
    return attempted


# ---- 1. the two cleanup operations are genuinely independent ------------


def test_a_failed_note_still_closes_the_abandoned_run(config, store, wav,
                                                       monkeypatch):
    """The reproduction, end to end.

    Initial save succeeds, workers fail, the cleanup note fails - and
    close_session must still be attempted. Sharing one try block meant it
    never was, and the run stayed open with nothing able to close it.
    """
    app = mock_app(config, store)
    fail_worker_start(monkeypatch)
    closed = break_cleanup_note(store, monkeypatch)

    with pytest.raises(RuntimeError) as excinfo:
        app.start_session(replay_path=wav, name="doomed")

    assert "worker startup failed" in str(excinfo.value), (
        "the cleanup's error replaced the operator's real one")
    assert closed, "close_session() was never attempted"
    assert open_rows(store) == [], "the run is still open"
    assert app.session is None
    assert app.capture is None
    assert app.pipeline is None
    assert app.capture_conversation_id == ""
    app.close()


def test_a_failed_close_still_leaves_no_open_run(config, store, wav,
                                                  monkeypatch):
    """The inverse: closing fails, saving the session with ended_at succeeds."""
    app = mock_app(config, store)
    fail_worker_start(monkeypatch)

    def refuse(session_id, ended_at=None):
        raise sqlite3.OperationalError("close failed")

    monkeypatch.setattr(store, "close_session", refuse)

    with pytest.raises(RuntimeError) as excinfo:
        app.start_session(replay_path=wav, name="doomed")
    assert "worker startup failed" in str(excinfo.value)
    assert open_rows(store) == [], (
        "close_session failed and the save did not cover for it")
    app.close()


def test_both_cleanup_operations_failing_still_reraises_the_real_error(
        config, store, wav, monkeypatch):
    app = mock_app(config, store)
    fail_worker_start(monkeypatch, message="the dongle fell out")

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("the database is locked")

    # Only the cleanup save is broken: the initial save that creates the row
    # has to succeed, or there is no open row for cleanup to close and the
    # test proves nothing about cleanup at all.
    real_save = store.save_session

    def refuse_cleanup_save(session):
        if session.ended_at is not None:
            raise sqlite3.OperationalError("the database is locked")
        return real_save(session)

    monkeypatch.setattr(store, "close_session", refuse)
    monkeypatch.setattr(store, "save_session", refuse_cleanup_save)

    with pytest.raises(RuntimeError) as excinfo:
        app.start_session(replay_path=wav, name="doomed")
    assert "the dongle fell out" in str(excinfo.value)
    assert app.session is None and app.pipeline is None
    assert app.capture_conversation_id == ""
    app.close()


def test_a_valid_run_works_after_a_cleanup_whose_note_failed(config, store,
                                                              wav, monkeypatch):
    app = mock_app(config, store)
    fail_worker_start(monkeypatch)
    break_cleanup_note(store, monkeypatch)
    with pytest.raises(RuntimeError):
        app.start_session(replay_path=wav, name="doomed")

    monkeypatch.undo()
    app.start_session(replay_path=wav, name="good")
    app.run_replay()
    app.stop_session()

    assert app.recent_transmissions(), "the recovered run captured nothing"
    assert open_rows(store) == []
    assert app.capture_conversation_id == ""
    app.close()


def test_the_two_cleanup_operations_are_in_separate_guards():
    """Names the cause, since the behavioural tests above are the real guard.

    A single try around both was what the previous docstring claimed was two
    independent guards, so the claim is now checked rather than trusted.
    """
    import ast
    import inspect

    from babelfishr.app import BabelFishRApp

    source = inspect.getsource(BabelFishRApp._abandon_failed_start)
    tree = ast.parse(source.lstrip())

    for handler in [n for n in ast.walk(tree) if isinstance(n, ast.Try)]:
        called = {node.func.attr for node in ast.walk(handler)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        assert not {"save_session", "close_session"} <= called, (
            "save_session and close_session share one try block, so a failure "
            "in the first skips the second")


# ---- 2. the bubble tells the truth about where a frequency came from ----


def bubble_for(view, provenance, index: int):
    transmission = Transmission(
        id=f"tx_{index}", session_id="s", duration=1.0,
        frequency_mhz=462.5625, frequency_provenance=provenance,
        transcript="all units", state=ProcessingState.COMPLETE,
        source_language="en", target_language="en")
    return view.add(transmission)


def suffix_of(bubble) -> str:
    header = bubble.header.text()
    assert "462.5625 MHz" in header, header
    return header.split("462.5625 MHz")[1].split("  ·")[0].strip()


@pytest.mark.parametrize("provenance,expected", [
    (Provenance.SDR, ""),
    (Provenance.RADIO, ""),
    (Provenance.OPERATOR, "(entered)"),
    (Provenance.PROFILE, "(entered)"),
    (Provenance.INFERRED, "(inferred)"),
    (Provenance.DSD, "(decoded)"),
    (Provenance.UNKNOWN, "(unverified)"),
])
def test_the_bubble_labels_a_frequency_by_its_provenance(qt_app, provenance,
                                                          expected):
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    assert suffix_of(bubble_for(view, provenance, 0)) == expected


def test_unknown_inferred_and_decoded_are_never_called_entered(qt_app):
    """The defect: one boolean made every non-measured origin "entered"."""
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    for index, provenance in enumerate((Provenance.UNKNOWN,
                                        Provenance.INFERRED, Provenance.DSD)):
        suffix = suffix_of(bubble_for(view, provenance, index))
        assert "entered" not in suffix, (
            f"a {provenance.value} frequency is labelled as operator-entered")


def test_measured_frequencies_carry_no_warning_suffix(qt_app):
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    for index, provenance in enumerate((Provenance.SDR, Provenance.RADIO)):
        suffix = suffix_of(bubble_for(view, provenance, index))
        assert suffix == ""
        assert "entered" not in suffix and "unverified" not in suffix


def test_an_unrecognised_provenance_falls_back_to_unverified(qt_app):
    """Never blank by default: a gap must not read as a measurement."""
    from babelfishr.ui.timeline import _frequency_suffix

    assert _frequency_suffix(None) == " (unverified)"
    assert _frequency_suffix("not-a-provenance") == " (unverified)"


def test_the_bubble_agrees_with_the_data_model(qt_app):
    """Both say "unverified" for UNKNOWN; they disagreed before."""
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    transmission = Transmission(
        id="tx_agree", session_id="s", duration=1.0, frequency_mhz=462.5625,
        frequency_provenance=Provenance.UNKNOWN, transcript="all units",
        state=ProcessingState.COMPLETE, source_language="en",
        target_language="en")
    bubble = view.add(transmission)
    summary = transmission.signal_summary()[0]
    assert "unverified" in summary["display"]
    assert "unverified" in bubble.header.text()
    assert summary["measured"] == "no"


def test_a_source_with_no_provenance_ends_up_labelled_unverified(
        qt_app, config, store, wav):
    """The whole path: driver reports a frequency, makes no claim about it,
    and the bubble the operator reads says so."""
    from babelfishr.audio.source import ReplayAudioSource
    from babelfishr.ui.timeline import TimelineView

    class _SilentSource:
        measures_rf = True

        def __init__(self, path, block_size):
            self._inner = ReplayAudioSource(path, realtime=False,
                                            block_size=block_size)

        def metadata(self):
            # No provenance argument: the source makes no claim.
            return SignalMetadata(tuned_frequency_hz=462_562_500.0,
                                  source="mystery-driver")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    app = mock_app(config, store)
    app.start_session(source=_SilentSource(wav, config.audio.block_size),
                      name="silent")
    app.run_replay()
    app.stop_session()

    captured = app.recent_transmissions()
    assert captured
    view = TimelineView()
    for transmission in captured:
        assert transmission.frequency_provenance is Provenance.UNKNOWN
        assert transmission.frequency_is_measured is False
        header = view.add(transmission).header.text()
        assert "(unverified)" in header
        assert "(entered)" not in header
    app.close()


def test_a_profile_frequency_still_reads_as_entered(qt_app, config, store, wav):
    """Compatibility: an operator-declared channel is still "entered"."""
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    transmission = Transmission(
        id="tx_profile", session_id="s", duration=1.0, frequency_mhz=462.5625,
        frequency_provenance=Provenance.PROFILE, transcript="all units",
        state=ProcessingState.COMPLETE, source_language="en",
        target_language="en")
    assert "(entered)" in view.add(transmission).header.text()
