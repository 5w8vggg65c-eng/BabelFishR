"""Window exports run off the GUI thread.

Exporting a large named Session used to read the store, render and copy
every recording on the Qt main thread: the window froze, and meanwhile Stop
Monitoring and the live capture-loss indicator could not be handled. Here
each export is made deliberately slow (a gate inside the worker's copy or
render step) and, while it is held:

* Stop Monitoring works and the open message's tail is saved;
* capture loss arriving meanwhile is persisted and shown in the badge;
* the event loop keeps turning (a QTimer fires);

and once released the export holds exactly what was selected when Export
was chosen. Also: failures and results are reported on the GUI thread, a
cancelled export writes nothing, a second export is refused while one runs,
and closing the window during an export is safe.

Synthetic audio, mock engines and temp folders only.
"""

from __future__ import annotations

import errno
import json
import os
import pathlib
import threading
import time

import pytest

from babelfishr import export
from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.models import GAP_DRIVER_OVERFLOW, CaptureGap, ProcessingState
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.testing import build_fixture

from export_wait import finish_export


@pytest.fixture(autouse=True)
def replace_answered_yes(monkeypatch):
    """BabelFishR's own "Replace it?" question (asked after the save dialog,
    whose native overwrite prompt is off) is answered yes here; tests of
    the question itself are in test_export_off_gui_thread.py."""
    from babelfishr.ui.main_window import MainWindow

    asked = []
    monkeypatch.setattr(MainWindow, "_ask_replace",
                        lambda self, title, path: asked.append(path) or True)
    return asked


SR = 48_000
VOICE = {"kind": "voice", "duration": 1.5, "level_dbfs": -14}
QUIET = {"gap": 4.0}


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(qt_app, predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qt_app.processEvents()
    return predicate()


def _push(source, spec):
    audio = build_fixture(spec, sample_rate=SR).audio
    for start in range(0, audio.size, 4800):
        source.push(audio[start:start + 4800])


def _rows(store, session_id):
    return store.list_transmissions(session_id=session_id, limit=1000)


class Gate:
    """Holds the first call of a wrapped function until released, recording
    which thread it ran on. The wait is bounded so a failure cannot hang."""

    def __init__(self, function):
        self.function = function
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads = []
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.threads.append(threading.current_thread())
        if self.calls == 1:
            self.entered.set()
            self.release.wait(10.0)
        return self.function(*args, **kwargs)


@pytest.fixture
def window(qt_app, config, store, tmp_path):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    app = BabelFishRApp(config=config, store=store)
    app.transcription, app.translation = (MockTranscriptionEngine(),
                                          MockTranslationEngine())
    win = MainWindow(app)
    win.show()
    yield win
    win.close()
    app.close()


def _monitor(window, store, qt_app):
    """A running synthetic capture with one finished message and a second
    one still open (its tail is saved only by Stop)."""
    app = window.app
    source = CallbackAudioSource(SR, name="test")
    session = app.start_session(source=source, name="run", target_language="fr")
    app.begin_capture()
    _push(source, [{"gap": 0.5}, VOICE, QUIET])
    assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 1 and all(
        t.state is ProcessingState.COMPLETE for t in _rows(store, session.id)))
    _push(source, [VOICE])
    assert _pump(qt_app, lambda: source._queue.empty() and app.capture.detector.open)
    return session


def _on_gui_thread(qt_app):
    from PySide6 import QtCore
    return QtCore.QThread.currentThread() is qt_app.thread()


# -- the finding ------------------------------------------------------------------

def test_a_slow_bundle_export_leaves_stop_capture_loss_and_the_loop_working(
        window, store, qt_app, tmp_path, monkeypatch):
    from PySide6 import QtCore, QtWidgets

    session = _monitor(window, store, qt_app)
    (first,) = _rows(store, session.id)
    out = tmp_path / "exports"
    out.mkdir()
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(out)))
    gate = Gate(export.shutil.copy2)
    monkeypatch.setattr(export.shutil, "copy2", gate)
    shown = []
    real_show = window.status.showMessage
    monkeypatch.setattr(window.status, "showMessage", lambda text, *a: (
        shown.append((text, _on_gui_thread(qt_app))), real_show(text, *a)))

    started = time.monotonic()
    window._export_session()
    assert time.monotonic() - started < 5.0, "Export blocked the GUI thread"
    assert gate.entered.wait(10.0)
    assert gate.threads[0] is not threading.main_thread()
    try:
        # (3) the event loop keeps turning while the export is held.
        fired = []
        QtCore.QTimer.singleShot(10, lambda: fired.append(True))
        assert _pump(qt_app, lambda: fired)

        # (1) Stop Monitoring works; the open message's tail is saved.
        window._stop_monitoring()
        assert window.app.session is None
        assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2 and all(
            t.state is ProcessingState.COMPLETE for t in _rows(store, session.id)))
        tail = [t for t in _rows(store, session.id) if t.id != first.id][0]
        assert tail.audio_path and pathlib.Path(tail.audio_path).exists()

        # (2) capture loss recorded meanwhile is persisted and shown.
        badge = window.capture_loss_badge
        store.add_capture_gap(CaptureGap(session_id=session.id, frames=None,
                                         sample_rate=SR,
                                         reason=GAP_DRIVER_OVERFLOW, overflows=2))
        window.app.events.publish("audio-status", {
            "kind": "capture-overflow", "message": "input overflow"})
        assert _pump(qt_app, lambda: badge.isVisible())
        assert "overflow" in badge.text()
        assert store.capture_gaps(session.id)[0].overflows == 2
        assert window._export_job is not None, "the export was not held"
    finally:
        gate.release.set()
    finish_export(window)

    # The bundle: complete, and exactly what was there when Export was chosen.
    (bundle,) = list(out.iterdir())
    manifest = json.loads((bundle / "session.json").read_text())
    assert [e["id"] for e in manifest["transmissions"]] == [first.id]
    assert [p.name for p in (bundle / "audio").iterdir()] == [f"{first.id}.wav"]
    for name in ("transcript.md", "transmissions.csv", "README.txt"):
        assert (bundle / name).exists()
    assert (f"Exported session bundle to {bundle}", True) in shown
    assert not [p for p in out.iterdir() if p.name.startswith(".")]
    assert window.export_button.isEnabled()


def test_a_slow_text_export_holds_its_selection_and_reports_on_the_gui_thread(
        window, store, qt_app, tmp_path, monkeypatch):
    from PySide6 import QtCore, QtWidgets

    session = _monitor(window, store, qt_app)
    (first,) = _rows(store, session.id)
    chosen = tmp_path / "out.json"
    chosen.write_text("older export")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(chosen), "")))
    gate = Gate(export.portable_text)
    monkeypatch.setattr(export, "portable_text", gate)
    shown = []
    monkeypatch.setattr(window.status, "showMessage", lambda text, *a: shown.append(
        (text, _on_gui_thread(qt_app))))

    window._export_text("json")
    assert gate.entered.wait(10.0)
    assert gate.threads[0] is not threading.main_thread()
    try:
        fired = []
        QtCore.QTimer.singleShot(10, lambda: fired.append(True))
        assert _pump(qt_app, lambda: fired)
        window._stop_monitoring()
        assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2)
        assert chosen.read_text() == "older export"     # not yet replaced
    finally:
        gate.release.set()
    finish_export(window)
    payload = json.loads(chosen.read_text())
    ids = [t["id"] for t in payload["transmissions"]]
    assert ids == [first.id]
    assert shown[-1] == (f"Exported {chosen}", True)
    assert sorted(p.name for p in tmp_path.iterdir()
                  if p.name.startswith(".babelfishr")) == []


def test_a_failure_is_warned_on_the_gui_thread_and_the_old_file_kept(
        window, store, qt_app, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    session = _monitor(window, store, qt_app)
    window._stop_monitoring()
    assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2)
    chosen = tmp_path / "out.md"
    chosen.write_text("older export")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(chosen), "")))
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(
        lambda *a, **k: warned.append((a[2], _on_gui_thread(qt_app)))))
    worker_threads = []

    def full(*a, **k):
        worker_threads.append(threading.current_thread())
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export.os, "fsync", full)
    window._export_text("md")
    finish_export(window)
    assert worker_threads and worker_threads[0] is not threading.main_thread()
    ((text, on_gui),) = warned
    assert on_gui and "could not be written" in text and "No space left" in text
    assert chosen.read_text() == "older export"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    # An unexpected error is warned about too, not raised out of a slot.
    monkeypatch.setattr(export.os, "fsync", os.fsync)

    def broken(*a, **k):
        raise ValueError("bad render")

    monkeypatch.setattr(export, "portable_text", broken)
    window._export_text("md")
    finish_export(window)
    assert warned[-1][1] and "bad render" in warned[-1][0]
    assert chosen.read_text() == "older export"

    # And a retry after the failures works. (undo() also drops the
    # autouse "Replace it?" answer, so it is given again.)
    monkeypatch.undo()
    replace_answered_yes.__wrapped__(monkeypatch)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(chosen), "")))
    window._export_text("md")
    finish_export(window)
    assert chosen.read_text() != "older export"


@pytest.mark.parametrize("kind", ["bundle", "text"])
def test_cancel_writes_nothing_and_a_retry_works(window, store, qt_app, tmp_path,
                                                 monkeypatch, kind):
    from PySide6 import QtWidgets

    session = _monitor(window, store, qt_app)
    window._stop_monitoring()
    assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2)
    out = tmp_path / "exports"
    out.mkdir()
    chosen = out / "chosen.csv"
    if kind == "bundle":
        monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: str(out)))
        gate = Gate(export.shutil.copy2)
        monkeypatch.setattr(export.shutil, "copy2", gate)
        start = window._export_session
    else:
        chosen.write_text("older export")
        monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(chosen), "")))
        gate = Gate(export.portable_text)
        monkeypatch.setattr(export, "portable_text", gate)
        start = lambda: window._export_text("csv")  # noqa: E731
    before = sorted(p.name for p in out.iterdir())

    start()
    assert gate.entered.wait(10.0)
    button = window._export_cancel_button
    assert button.isVisible() and button.isEnabled()
    button.click()
    gate.release.set()
    finish_export(window)
    assert sorted(p.name for p in out.iterdir()) == before, "a cancel wrote"
    if kind == "text":
        assert chosen.read_text() == "older export"
    assert "cancelled" in window.status.currentMessage()
    assert not button.isVisible()

    start()                                    # retry: the gate is open now
    finish_export(window)
    if kind == "bundle":
        (bundle,) = [p for p in out.iterdir()]
        assert len(json.loads((bundle / "session.json").read_text())[
            "transmissions"]) == 2
    else:
        assert chosen.read_text() != "older export"
    assert not [p for p in out.iterdir() if p.name.startswith(".")]


def test_a_second_export_is_refused_while_one_runs(window, store, qt_app, tmp_path,
                                                  monkeypatch):
    from PySide6 import QtWidgets

    session = _monitor(window, store, qt_app)
    window._stop_monitoring()
    assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2)
    out = tmp_path / "exports"
    out.mkdir()
    asked = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: asked.append(a) or str(out)))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: asked.append(a) or ("", "")))
    gate = Gate(export.shutil.copy2)
    monkeypatch.setattr(export.shutil, "copy2", gate)

    window._export_session()
    assert gate.entered.wait(10.0)
    try:
        assert not window.export_button.isEnabled()
        assert not any(a.isEnabled() for a in window._export_actions)
        window._export_session()
        window._export_text("md")
        window._on_export_clip(_rows(store, session.id)[0].id)
        assert len(asked) == 1, "a second export was started"
        assert "still running" in window.status.currentMessage()
    finally:
        gate.release.set()
    finish_export(window)
    assert window.export_button.isEnabled()
    assert all(a.isEnabled() for a in window._export_actions)
    assert len(list(out.iterdir())) == 1


def test_the_clip_copy_runs_off_the_gui_thread(window, store, qt_app, tmp_path,
                                              monkeypatch):
    from PySide6 import QtWidgets

    session = _monitor(window, store, qt_app)
    window._stop_monitoring()
    assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2)
    tx = _rows(store, session.id)[0]
    chosen = tmp_path / "clip.wav"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(chosen), "")))
    gate = Gate(export.shutil.copyfile)
    monkeypatch.setattr(export.shutil, "copyfile", gate)
    shown = []
    monkeypatch.setattr(window.status, "showMessage", lambda text, *a: shown.append(
        (text, _on_gui_thread(qt_app))))
    window._on_export_clip(tx.id)
    assert gate.entered.wait(10.0)
    assert gate.threads[0] is not threading.main_thread()
    assert not chosen.exists()
    gate.release.set()
    finish_export(window)
    assert chosen.read_bytes() == pathlib.Path(tx.audio_path).read_bytes()
    assert shown[-1] == (f"Exported {chosen}", True)


def test_closing_during_an_export_cancels_it_and_closes_cleanly(
        window, store, qt_app, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    session = _monitor(window, store, qt_app)
    window._stop_monitoring()
    assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2)
    out = tmp_path / "exports"
    out.mkdir()
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(out)))
    gate = Gate(export.shutil.copy2)
    monkeypatch.setattr(export.shutil, "copy2", gate)
    window._export_session()
    assert gate.entered.wait(10.0)
    window.close()                      # the export is cancelled; close waits
    assert window._export_job is not None and window._export_job.token.cancelled
    assert window.isVisible() and not window._shutdown_complete
    gate.release.set()
    assert _pump(qt_app, lambda: window._shutdown_complete)
    assert _pump(qt_app, lambda: not window.isVisible())
    assert list(out.iterdir()) == [], "a cancelled bundle was published"


def test_a_close_that_stops_waiting_leaves_the_export_to_end_alone(
        window, store, qt_app, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    session = _monitor(window, store, qt_app)
    window._stop_monitoring()
    assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2)
    out = tmp_path / "exports"
    out.mkdir()
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(out)))
    gate = Gate(export.shutil.copy2)
    monkeypatch.setattr(export.shutil, "copy2", gate)
    monkeypatch.setattr(type(window), "EXPORT_CLOSE_WAIT_S", 0.2)
    window._export_session()
    assert gate.entered.wait(10.0)
    ended = []
    signals = window._export_job.worker.signals
    for signal in (signals.finished, signals.failed, signals.cancelled):
        signal.connect(lambda *a, name=signal: ended.append(a))
    window.close()
    # The export is still held, but the close goes ahead after the wait.
    assert _pump(qt_app, lambda: window._shutdown_complete)
    assert not gate.release.is_set()
    gate.release.set()
    assert _pump(qt_app, lambda: ended)
    # It stopped (cancelled) on its own connection and left nothing.
    assert list(out.iterdir()) == []


# -- the export's own read connection --------------------------------------------

def test_a_store_snapshot_is_frozen_read_only_and_separate(store, tmp_path):
    from babelfishr.models import Session
    from babelfishr.storage import Store

    conv = store.default_conversation().id
    before = store.save_session(Session(name="before", conversation_id=conv))
    snap = Store.open_snapshot(store.path, recordings_dir=str(store.recordings_dir))
    try:
        later = store.save_session(Session(name="later", conversation_id=conv))
        ids = set(snap.session_ids_for_conversation(conv))
        assert before.id in ids and later.id not in ids, "not a snapshot"
        assert later.id in store.session_ids_for_conversation(conv)
        with pytest.raises(Exception):
            snap.save_session(Session(name="no", conversation_id=conv))
        # Readable from another thread while the store keeps writing.
        seen = []
        worker = threading.Thread(target=lambda: seen.append(
            snap.get_session(before.id)))
        worker.start()
        worker.join(10.0)
        assert seen and seen[0].id == before.id
    finally:
        snap.close()
    assert store.get_session(later.id) is not None
    missing = tmp_path / "nowhere" / "db.sqlite3"
    with pytest.raises(Exception):
        Store.open_snapshot(str(missing))
    assert not missing.exists() and not missing.parent.exists()
    with pytest.raises(ValueError):
        Store.open_snapshot(":memory:")


# -- the single-clip audio export ------------------------------------------------

def _stopped_clip(window, store, qt_app, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    session = _monitor(window, store, qt_app)
    window._stop_monitoring()
    assert _pump(qt_app, lambda: len(_rows(store, session.id)) == 2)
    tx = _rows(store, session.id)[0]
    chosen = tmp_path / "clips" / "clip.wav"
    chosen.parent.mkdir()
    chosen.write_bytes(b"older clip")
    # The save dialog's own overwrite confirmation was answered yes.
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(chosen), "")))
    return session, tx, chosen


def test_a_clip_failure_warns_on_the_gui_thread_and_keeps_the_old_file(
        window, store, qt_app, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    _, tx, chosen = _stopped_clip(window, store, qt_app, tmp_path, monkeypatch)
    warned, threads = [], []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(
        lambda *a, **k: warned.append((a[2], _on_gui_thread(qt_app)))))

    def full(*a, **k):
        threads.append(threading.current_thread())
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export.shutil, "copyfile", full)
    window._on_export_clip(tx.id)
    finish_export(window)
    assert threads and threads[0] is not threading.main_thread()
    ((text, on_gui),) = warned
    assert on_gui and "could not be exported" in text and "No space left" in text
    assert chosen.read_bytes() == b"older clip"
    assert [p.name for p in chosen.parent.iterdir()] == [chosen.name]
    assert window.export_button.isEnabled()


def test_a_cancelled_clip_writes_nothing_and_a_retry_works(
        window, store, qt_app, tmp_path, monkeypatch):
    _, tx, chosen = _stopped_clip(window, store, qt_app, tmp_path, monkeypatch)
    gate = Gate(export.shutil.copyfile)
    monkeypatch.setattr(export.shutil, "copyfile", gate)
    window._on_export_clip(tx.id)
    assert gate.entered.wait(10.0)          # held mid-copy
    window._export_cancel_button.click()
    gate.release.set()
    finish_export(window)
    assert "cancelled" in window.status.currentMessage()
    assert chosen.read_bytes() == b"older clip"
    assert [p.name for p in chosen.parent.iterdir()] == [chosen.name]

    window._on_export_clip(tx.id)
    finish_export(window)
    assert chosen.read_bytes() == pathlib.Path(tx.audio_path).read_bytes()
    assert [p.name for p in chosen.parent.iterdir()] == [chosen.name]


@pytest.mark.parametrize("first", ["clip", "bundle", "text"])
def test_any_export_blocks_every_other_kind_while_it_runs(
        window, store, qt_app, tmp_path, monkeypatch, first):
    from PySide6 import QtWidgets

    _, tx, chosen = _stopped_clip(window, store, qt_app, tmp_path, monkeypatch)
    out = tmp_path / "exports"
    out.mkdir()
    asked = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: asked.append(a) or str(out)))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(
        lambda *a, **k: asked.append(a) or (str(chosen), "")))
    copier = "copyfile" if first == "clip" else "copy2"   # clip vs bundle audio
    gate = Gate(export.portable_text if first == "text"
                else getattr(export.shutil, copier))
    if first == "text":
        monkeypatch.setattr(export, "portable_text", gate)
    else:
        monkeypatch.setattr(export.shutil, copier, gate)
    starts = {"clip": lambda: window._on_export_clip(tx.id),
              "bundle": window._export_session,
              "text": lambda: window._export_text("md")}
    starts[first]()
    assert gate.entered.wait(10.0)
    try:
        for kind, start in starts.items():
            start()
            assert len(asked) == 1, f"a {kind} export started alongside a {first}"
            assert "still running" in window.status.currentMessage()
    finally:
        gate.release.set()
    finish_export(window)
    assert window.export_button.isEnabled()


def test_a_text_name_claimed_while_the_export_runs_survives(
        window, store, qt_app, tmp_path, monkeypatch):
    """The operator chose a new name; another file took it before the
    export finished. That file is kept and the export reports its own."""
    from PySide6 import QtWidgets

    _monitor(window, store, qt_app)
    chosen = tmp_path / "fresh.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(chosen), "")))
    gate = Gate(export.portable_text)
    monkeypatch.setattr(export, "portable_text", gate)
    window._export_text("csv")
    assert gate.entered.wait(10.0)
    chosen.write_text("claimed meanwhile")
    gate.release.set()
    finish_export(window)
    assert chosen.read_text() == "claimed meanwhile"
    sibling = tmp_path / "fresh (2).csv"
    assert sibling.is_file()
    assert str(sibling) in window.status.currentMessage()


def test_cancel_pressed_after_publication_reports_completion(
        window, store, qt_app, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    _monitor(window, store, qt_app)
    chosen = tmp_path / "out.md"
    chosen.write_text("older")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(chosen), "")))
    real_replace = os.replace
    pressed = threading.Event()

    def replace_then_cancel(src, dst):
        real_replace(src, dst)
        window._export_job.token.cancel()      # Cancel lands just after
        pressed.set()

    monkeypatch.setattr(export.os, "replace", replace_then_cancel)
    window._export_text("md")
    finish_export(window)
    assert pressed.is_set()
    assert chosen.read_text() != "older"
    assert f"Exported {chosen}" in window.status.currentMessage()


# -- explicit consent bound to the file that was there ---------------------------
def _real_question(monkeypatch, answer):
    """Restore BabelFishR's own replace question and answer it; record the
    native dialog's options and every question asked."""
    from PySide6 import QtWidgets

    from babelfishr.ui import main_window as mw

    monkeypatch.setattr(mw.MainWindow, "_ask_replace", _REAL_ASK_REPLACE)
    asked = []

    def question(parent, title, text, *a, **k):
        asked.append(text)
        return answer
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(question))
    return asked


def _dialog(monkeypatch, chosen, seen_options):
    from PySide6 import QtWidgets

    def native(*a, **k):
        seen_options.append(k.get("options"))
        return (str(chosen), "")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(native))


from babelfishr.ui.main_window import MainWindow as _MW  # noqa: E402
_REAL_ASK_REPLACE = _MW._ask_replace


@pytest.mark.parametrize("kind", ["text", "clip"])
def test_consent_is_asked_by_the_app_for_the_file_it_observed(
        window, store, qt_app, tmp_path, monkeypatch, kind):
    from PySide6 import QtWidgets

    _, tx, clip = _stopped_clip(window, store, qt_app, tmp_path, monkeypatch)
    chosen = clip if kind == "clip" else tmp_path / "out.md"
    chosen.write_bytes(b"older")
    options = []
    _dialog(monkeypatch, chosen, options)
    asked = _real_question(monkeypatch, QtWidgets.QMessageBox.Yes)
    start = (lambda: window._on_export_clip(tx.id)) if kind == "clip" else (
        lambda: window._export_text("md"))
    start()
    finish_export(window)
    # The native prompt was off; BabelFishR asked once, naming the file.
    assert options == [QtWidgets.QFileDialog.Option.DontConfirmOverwrite]
    assert len(asked) == 1 and chosen.name in asked[0]
    assert chosen.read_bytes() != b"older"
    assert [p.name for p in chosen.parent.iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize("kind", ["text", "clip"])
def test_declining_writes_nothing(window, store, qt_app, tmp_path, monkeypatch,
                                  kind):
    from PySide6 import QtWidgets

    _, tx, clip = _stopped_clip(window, store, qt_app, tmp_path, monkeypatch)
    chosen = clip if kind == "clip" else tmp_path / "out.md"
    chosen.write_bytes(b"older")
    _dialog(monkeypatch, chosen, [])
    _real_question(monkeypatch, QtWidgets.QMessageBox.No)
    before = sorted(p.name for p in chosen.parent.iterdir())
    (lambda: window._on_export_clip(tx.id))() if kind == "clip" else \
        window._export_text("md")
    assert window._export_job is None              # no export started
    assert chosen.read_bytes() == b"older"
    assert sorted(p.name for p in chosen.parent.iterdir()) == before


@pytest.mark.parametrize("kind", ["text", "clip"])
def test_consent_does_not_carry_over_to_a_file_swapped_in_later(
        window, store, qt_app, tmp_path, monkeypatch, kind):
    """The operator agreed to replace the file they saw; another file
    took its place before the export published: that one is kept."""
    from PySide6 import QtWidgets

    _, tx, clip = _stopped_clip(window, store, qt_app, tmp_path, monkeypatch)
    chosen = clip if kind == "clip" else tmp_path / "out.md"
    chosen.write_bytes(b"agreed to replace")
    _dialog(monkeypatch, chosen, [])
    _real_question(monkeypatch, QtWidgets.QMessageBox.Yes)
    if kind == "clip":
        gate = Gate(export.shutil.copyfile)
        monkeypatch.setattr(export.shutil, "copyfile", gate)
        window._on_export_clip(tx.id)
    else:
        gate = Gate(export.portable_text)
        monkeypatch.setattr(export, "portable_text", gate)
        window._export_text("md")
    assert gate.entered.wait(10.0)
    swap = chosen.with_name("swap")
    swap.write_bytes(b"someone else's")
    os.replace(swap, chosen)
    gate.release.set()
    finish_export(window)
    assert chosen.read_bytes() == b"someone else's"
    sibling = chosen.with_name(f"{chosen.stem} (2){chosen.suffix}")
    assert sibling.is_file()
    assert str(sibling) in window.status.currentMessage()


@pytest.mark.parametrize("kind", ["text", "clip"])
def test_a_new_name_asks_nothing(window, store, qt_app, tmp_path, monkeypatch,
                                 kind):
    from PySide6 import QtWidgets

    _, tx, clip = _stopped_clip(window, store, qt_app, tmp_path, monkeypatch)
    chosen = (clip.with_name("new.wav") if kind == "clip"
              else tmp_path / "new.md")
    _dialog(monkeypatch, chosen, [])
    asked = _real_question(monkeypatch, QtWidgets.QMessageBox.Yes)
    (lambda: window._on_export_clip(tx.id))() if kind == "clip" else \
        window._export_text("md")
    finish_export(window)
    assert asked == [] and chosen.is_file()
