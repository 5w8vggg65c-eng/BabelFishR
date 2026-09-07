"""Removing one message: from view with its data kept, or permanently.

Every destructive test works in a disposable temporary home: the database,
the Recordings folder and every file are created under pytest's tmp_path and
nothing else. The modal question is substituted through MainWindow._choose,
which is the one seam every removal dialog goes through; what each answer
does is what these tests prove.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import time

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import (AnalysisArtifact, AnalysisAttempt,
                               ProcessingState)
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.storage import Store
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
    return build_fixture([{"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
                          {"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -12},
                          {"gap": 1.0}], sample_rate=SR).write(
        str(tmp_path / "two.wav"))


def mock_app(config, store) -> BabelFishRApp:
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


def pump(qt_app, rounds: int = 20) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.004)


def answers(monkeypatch, window, *labels):
    """Script MainWindow._choose: each call returns the next label."""
    queue = list(labels)
    asked = []

    def fake(title, text, informative, options, destructive=None, default=None):
        asked.append((title, options, informative))
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(window, "_choose", fake)
    return asked


def silence_boxes(monkeypatch):
    from PySide6 import QtWidgets

    shown = {"information": [], "warning": []}
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown["information"].append(a)))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown["warning"].append(a)))
    return shown


def captured_window(qt_app, config, store, wav):
    """A window on General with two captured, transcribed messages."""
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    window._stop_monitoring()
    pump(qt_app, 40)
    txs = app.recent_transmissions()
    assert len(txs) >= 2, "the fixture must yield at least two messages"
    for tx in txs:
        assert tx.audio_path and pathlib.Path(tx.audio_path).is_file()
        assert tx.id in window.timeline._bubbles
    return app, window, txs


def add_derived_files(store, tx, recordings: pathlib.Path):
    """Give a message a processed copy and a decoded analysis artifact, both
    inside the Recordings folder, so a deletion has a known inventory."""
    processed = recordings / f"{tx.id}.processed.wav"
    shutil.copy(tx.audio_path, processed)
    decoded = recordings / f"{tx.id}.an_1.decoded.wav"
    shutil.copy(tx.audio_path, decoded)
    tx.processed_audio_path = str(processed)
    tx.analysis_attempts.append(AnalysisAttempt(
        transmission_id=tx.id, engine="dsd-neo", input_path=tx.audio_path,
        artifacts=[AnalysisArtifact(kind="decoded-audio", path=str(decoded))]))
    store.save_transmission(tx)
    return [str(processed), str(decoded)]


# ---- cancel ---------------------------------------------------------------------


def test_cancel_at_either_question_changes_nothing(qt_app, config, store, wav,
                                                   monkeypatch):
    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    asked = answers(monkeypatch, window, "")            # cancel the first box
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert asked and window.REMOVE_KEEP in asked[0][1]
    assert store.get_transmission(target.id) is not None
    assert store.get_transmission(target.id).hidden is False
    assert pathlib.Path(target.audio_path).is_file()
    assert target.id in window.timeline._bubbles

    answers(monkeypatch, window, window.DELETE_FOREVER, "")   # cancel the second
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert store.get_transmission(target.id) is not None
    assert pathlib.Path(target.audio_path).is_file()
    assert not store.is_deleted(target.id)
    window.close()


# ---- remove from view, keep data -----------------------------------------------


def test_remove_from_thread_keeps_everything_and_is_restorable(
        qt_app, config, store, wav, monkeypatch):
    app, window, txs = captured_window(qt_app, config, store, wav)
    target, other = txs[0], txs[1]
    target.transcript = "purple giraffe seventeen"
    target.transcript_confidence = 0.2
    store.save_transmission(target)

    answers(monkeypatch, window, window.REMOVE_KEEP)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)

    # Out of view, out of search, review and export; still stored, file kept.
    assert target.id not in window.timeline._bubbles
    assert other.id in window.timeline._bubbles
    assert store.get_transmission(target.id).hidden is True
    assert pathlib.Path(target.audio_path).is_file()
    assert target.id not in {t.id for t in app.recent_transmissions()}
    assert app.search("giraffe") == []
    assert target.id not in {t.id for t in app.review_queue()}
    from babelfishr.export import export_session

    export_session(store, target.session_id, str(config.paths().logs / "exp"),
                   include_audio=False, conversation_id=app.conversation_id)
    manifest = json.loads((config.paths().logs / "exp" / "session.json").read_text())
    assert target.id not in {t["id"] for t in manifest["transmissions"]}
    assert other.id in {t["id"] for t in manifest["transmissions"]}
    assert "kept" in window.status.currentMessage()

    # The way back: View > Show removed messages, then Restore.
    window.show_removed_action.setChecked(True)
    pump(qt_app)
    assert target.id in window.timeline._bubbles
    bubble = window.timeline._bubbles[target.id]
    assert "Removed from thread" in bubble.status_label.text()
    assert bubble.restore_action.isVisible() and not bubble.remove_action.isVisible()
    bubble.restore_action.trigger()
    pump(qt_app)
    assert store.get_transmission(target.id).hidden is False
    window.show_removed_action.setChecked(False)
    pump(qt_app)
    assert target.id in window.timeline._bubbles
    assert [t.id for t in app.search("giraffe")] == [target.id]
    window.close()


def test_a_removed_message_stays_removed_after_relaunch(qt_app, config, store,
                                                        wav, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    answers(monkeypatch, window, window.REMOVE_KEEP)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    window.close()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    app2 = mock_app(config, reopened)
    window2 = MainWindow(app2)
    pump(qt_app)
    assert target.id not in window2.timeline._bubbles
    assert reopened.get_transmission(target.id).hidden is True
    window2.show_removed_action.setChecked(True)
    pump(qt_app)
    assert target.id in window2.timeline._bubbles
    window2.close()


# ---- permanent deletion ---------------------------------------------------------


def test_permanent_deletion_removes_the_row_the_index_and_its_own_files(
        qt_app, config, store, wav, monkeypatch):
    app, window, txs = captured_window(qt_app, config, store, wav)
    target, other = txs[0], txs[1]
    recordings = pathlib.Path(config.recording.directory)
    derived = add_derived_files(store, target, recordings)
    target = store.get_transmission(target.id)
    target.transcript = "purple giraffe seventeen"
    store.save_transmission(target)
    assert app.search("giraffe"), "the search index must hold it first"
    files = [target.audio_path, *derived]
    assert all(pathlib.Path(f).is_file() for f in files)

    inventory = app.deletion_inventory(target.id)
    assert set(inventory.owned) == set(files), inventory
    assert not inventory.external and not inventory.shared

    asked = answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)

    assert len(asked) == 2
    scope = asked[1][2]
    assert "3 file(s)" in scope and target.audio_path in scope
    assert "cannot be undone" in scope
    assert store.get_transmission(target.id) is None
    assert store.is_deleted(target.id)
    assert app.search("giraffe") == []
    assert not any(pathlib.Path(f).exists() for f in files), "a file survived"
    assert target.id not in window.timeline._bubbles
    assert "deleted" in window.status.currentMessage()
    # The other message in the same Session is untouched.
    kept = store.get_transmission(other.id)
    assert kept is not None and pathlib.Path(kept.audio_path).is_file()
    assert other.id in window.timeline._bubbles
    window.close()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    assert reopened.get_transmission(target.id) is None
    assert reopened.is_deleted(target.id)
    assert reopened.get_transmission(other.id) is not None
    reopened.close()


def test_a_late_worker_result_cannot_resurrect_a_deleted_message(
        qt_app, config, store, wav, monkeypatch):
    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    stale_copy = store.get_transmission(target.id)      # what a worker holds
    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert store.get_transmission(target.id) is None

    stale_copy.transcript = "late words"
    stale_copy.state = ProcessingState.COMPLETE
    store.save_transmission(stale_copy)                   # the upsert path
    assert store.get_transmission(target.id) is None, "the row came back"
    app.events.publish("updated", stale_copy)
    window._drain_events()
    assert target.id not in window.timeline._bubbles, "the bubble came back"
    window.close()


def test_a_message_being_processed_is_refused_not_hidden(qt_app, config, store,
                                                         wav, monkeypatch):
    import threading

    import babelfishr.providers as providers

    class Blocking(MockTranscriptionEngine):
        id = "test-blocking-rm"
        name = "blocking"

        def __init__(self):
            super().__init__()
            self.release = threading.Event()
            self.started = threading.Event()

        def transcribe(self, audio, sr, *, language=None, vocabulary=None):
            self.started.set()
            self.release.wait(30)
            return super().transcribe(audio, sr, language=language,
                                      vocabulary=vocabulary)

    made = []
    real = providers._transcription_factories

    def factories(config=None, mode=None):
        t = real(config, mode)
        t["test-blocking-rm"] = lambda: made.append(Blocking()) or made[-1]
        return t

    monkeypatch.setattr(providers, "_transcription_factories", factories)
    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    app.config.asr.engine = "test-blocking-rm"
    assert app.transcribe_anyway(target.id)
    engine = app.standalone_pipeline.transcription
    assert engine.started.wait(10)
    assert app.standalone_pipeline.is_in_flight(target.id)

    shown = silence_boxes(monkeypatch)
    asked = answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert not asked, "the choice was offered for a message still in flight"
    assert shown["information"] and "still being processed" in shown["information"][0][2]
    assert store.get_transmission(target.id) is not None
    assert target.id in window.timeline._bubbles

    engine.release.set()
    assert app.standalone_pipeline.wait_until_idle(30)
    assert not app.standalone_pipeline.is_in_flight(target.id)
    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert store.get_transmission(target.id) is None
    window.close()


# ---- file ownership ------------------------------------------------------------


def test_files_outside_the_recordings_folder_are_never_deleted(
        qt_app, config, store, wav, monkeypatch, tmp_path):
    app, window, txs = captured_window(qt_app, config, store, wav)
    target = store.get_transmission(txs[0].id)
    elsewhere = tmp_path / "operators-own" / "original.wav"
    elsewhere.parent.mkdir()
    shutil.copy(target.audio_path, elsewhere)
    target.audio_path = str(elsewhere)
    store.save_transmission(target)

    inventory = app.deletion_inventory(target.id)
    assert str(elsewhere) in inventory.external and not inventory.owned
    asked = answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert "NOT deleted: 1 file(s) outside" in asked[1][2]
    assert store.get_transmission(target.id) is None
    assert elsewhere.is_file(), "an external file was deleted"
    window.close()


def test_a_file_another_message_still_uses_is_kept(qt_app, config, store, wav,
                                                   monkeypatch):
    app, window, txs = captured_window(qt_app, config, store, wav)
    a, b = store.get_transmission(txs[0].id), store.get_transmission(txs[1].id)
    b.processed_audio_path = a.audio_path        # b also uses a's recording
    store.save_transmission(b)

    inventory = app.deletion_inventory(a.id)
    assert a.audio_path in inventory.shared and a.audio_path not in inventory.owned
    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[a.id].remove_action.trigger()
    pump(qt_app)
    assert store.get_transmission(a.id) is None
    assert pathlib.Path(a.audio_path).is_file(), "a shared file was deleted"
    assert store.get_transmission(b.id).processed_audio_path == a.audio_path
    window.close()


def test_a_symlink_inside_recordings_is_not_followed(qt_app, config, store, wav,
                                                     monkeypatch, tmp_path):
    app, window, txs = captured_window(qt_app, config, store, wav)
    target = store.get_transmission(txs[0].id)
    real_file = tmp_path / "unrelated" / "precious.wav"
    real_file.parent.mkdir()
    shutil.copy(target.audio_path, real_file)
    link = pathlib.Path(config.recording.directory) / "link.wav"
    try:
        link.symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    target.audio_path = str(link)
    store.save_transmission(target)

    inventory = app.deletion_inventory(target.id)
    assert str(link) in inventory.external, inventory
    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert real_file.is_file(), "the link's target was deleted"
    assert link.is_symlink(), "the link itself was removed"
    window.close()


def test_an_already_missing_file_is_reported_gone_not_failed(qt_app, config,
                                                             store, wav,
                                                             monkeypatch):
    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    pathlib.Path(target.audio_path).unlink()
    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert store.get_transmission(target.id) is None
    assert store.leftover_deletions() == {}
    assert "deleted" in window.status.currentMessage()
    window.close()


def test_a_file_that_will_not_delete_is_recorded_and_retried(qt_app, config,
                                                             store, wav,
                                                             monkeypatch):
    """Partial filesystem failure: the row goes, the leftover is named, the
    deletion is not called complete, and Tools > Finish unfinished deletions
    removes it once the obstacle is gone."""
    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    stuck = pathlib.Path(target.audio_path)
    real_unlink = pathlib.Path.unlink

    def refusing_unlink(self, *args, **kwargs):
        if self == stuck:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", refusing_unlink)
    shown = silence_boxes(monkeypatch)
    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)

    assert store.get_transmission(target.id) is None
    assert stuck.is_file(), "the test needs the file to have survived"
    assert shown["warning"] and "could not be removed" in shown["warning"][0][2]
    assert str(stuck) in shown["warning"][0][2]
    assert store.leftover_deletions() == {target.id: [str(stuck)]}
    assert "still on disk" in window.status.currentMessage()

    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    window.finish_deletions_action.trigger()
    pump(qt_app)
    assert not stuck.exists()
    assert store.leftover_deletions() == {}
    assert shown["information"] and "All done" in shown["information"][-1][2]
    window.close()


# ---- coordination with playback and capture --------------------------------------


def _scripted_controller(view):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "playback_tests", pathlib.Path(__file__).with_name("test_alpha5_playback.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = module.make_scripted_backend()
    from babelfishr.ui.playback import PlaybackController

    controller = PlaybackController(backend, parent=view)
    view.playback.changed.disconnect(view._on_playback_changed)
    view.playback = controller
    view._player = controller
    controller.changed.connect(view._on_playback_changed)
    return backend, controller


def test_playback_of_the_message_stops_before_it_is_deleted(qt_app, config, store,
                                                            wav, monkeypatch):
    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    backend, controller = _scripted_controller(window.timeline)
    window._reload_timeline()
    pump(qt_app)
    backend.durations[target.audio_path] = 9000
    controller.play(target.id, target.audio_path)
    assert controller.owner == target.id

    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert ("stop",) in backend.calls
    assert controller.owner is None
    assert store.get_transmission(target.id) is None
    assert not pathlib.Path(target.audio_path).exists()
    window.close()


def test_deleting_during_monitoring_leaves_the_capture_pinned(qt_app, config,
                                                              store, wav,
                                                              monkeypatch):
    from babelfishr.audio.source import CallbackAudioSource

    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    app.start_session(source=CallbackAudioSource(SR), name="live")
    app.begin_capture()
    pinned = app.capture_conversation_id
    assert pinned == app.conversation_id
    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[target.id].remove_action.trigger()
    pump(qt_app)
    assert store.get_transmission(target.id) is None
    assert app.capture is not None and app.capture_conversation_id == pinned
    assert app.capture._running
    app.stop_session()
    window.close()


# ---- the real control path -------------------------------------------------------


def test_the_menu_item_is_reached_through_the_bubble_menu_button(qt_app, config,
                                                                 store, wav,
                                                                 monkeypatch):
    """Two real clicks: the bubble's ... button, then Remove message."""
    from PySide6 import QtCore
    from PySide6.QtTest import QTest

    app, window, txs = captured_window(qt_app, config, store, wav)
    target = txs[0]
    bubble = window.timeline._bubbles[target.id]
    asked = answers(monkeypatch, window, window.REMOVE_KEEP)
    menu = bubble.menu_button.menu()
    opened = []

    def click_item():
        # Runs inside the popup's own event loop, which the instant-popup
        # button starts when clicked: the menu is up, so click its item.
        opened.append(menu.isVisible())
        QTest.mouseClick(menu, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                         menu.actionGeometry(bubble.remove_action).center())

    QtCore.QTimer.singleShot(50, click_item)
    QTest.mouseClick(bubble.menu_button, QtCore.Qt.LeftButton)
    pump(qt_app)
    assert opened == [True], "the ... menu did not open"
    assert asked, "clicking Remove message did not ask the question"
    assert store.get_transmission(target.id).hidden is True
    window.close()


# ---- migration -----------------------------------------------------------------------


def test_an_existing_database_gains_the_hidden_flag_with_every_message_visible(
        tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "colors_tests", pathlib.Path(__file__).with_name("test_alpha5_session_colors.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = str(tmp_path / "run21.sqlite3")
    module._schema_4_database(database, "")
    store = Store(database, recordings_dir=str(tmp_path))
    assert "hidden" in store._columns("transmissions")
    assert "deleted_transmissions" in {
        r["name"] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for conv in ("conv_gen", "conv_ops"):
        (tx,) = store.conversation_transmissions(conv, limit=10)
        assert tx.hidden is False
    assert store.leftover_deletions() == {}
    store.close()
