"""Removing a whole Session tab: hide it with everything kept, or delete it.

The choice is the operator's at the moment of removal, offered by name; the
policy is not decided in code. General is refused with the reason, because
whether it may ever be removed is a decision that has not been made. Every
destructive test runs in a disposable temporary home.
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest

from babelfishr.app import BabelFishRApp
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


def tab_index_for(window, conversation_id):
    for index in range(window.session_tabs.count()):
        if window.session_tabs.tabData(index) == conversation_id:
            return index
    return None


def menu_action(window, index, prefix):
    menu = window._build_session_tab_menu(index)
    found = [a for a in menu.actions() if a.text().startswith(prefix)]
    assert found, [a.text() for a in menu.actions()]
    return found[0]


def two_sessions_window(qt_app, config, store, wav):
    """General and Ops, each with two runs and messages on disk."""
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    general = app.conversation_id
    ops = app.create_conversation("Ops")
    for conversation_id in (general, ops.id):
        app.select_conversation(conversation_id)
        for run in range(2):
            app.start_session(replay_path=wav, name=f"run {run}")
            app.run_replay()
            app.stop_session()
    window._refresh_session_tabs()
    pump(qt_app)

    def snapshot(conversation_id):
        sessions = store.session_ids_for_conversation(conversation_id)
        txs = [t for s in sessions for t in store.list_transmissions(
            session_id=s, limit=1000, include_hidden=True)]
        files = [t.audio_path for t in txs]
        assert sessions and txs and all(pathlib.Path(f).is_file() for f in files)
        return sessions, [t.id for t in txs], files

    return app, window, general, ops.id, snapshot


# ---- General ------------------------------------------------------------------


def test_general_cannot_be_removed_and_says_why(qt_app, config, store, wav,
                                                monkeypatch):
    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    action = menu_action(window, tab_index_for(window, general), "Remove Session")
    assert not action.isEnabled()
    assert "decision" in action.toolTip()
    assert "decision" in app.conversation_removal_problem(general)
    with pytest.raises(ValueError):
        store.hide_conversation(general, hidden=True)
    with pytest.raises(ValueError):
        store.delete_conversation_permanently(general, app.owned_roots())
    assert store.default_conversation().id == general
    window.close()


# ---- cancel and refusals ------------------------------------------------------------


def test_cancel_changes_nothing(qt_app, config, store, wav, monkeypatch):
    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    before = snapshot(ops)
    answers(monkeypatch, window, "")
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    answers(monkeypatch, window, window.DELETE_SESSION, "")
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    assert snapshot(ops) == before
    assert store.get_conversation(ops).hidden is False
    assert tab_index_for(window, ops) is not None
    window.close()


def test_a_session_being_recorded_into_is_refused(qt_app, config, store, wav,
                                                  monkeypatch):
    from babelfishr.audio.source import CallbackAudioSource

    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    app.select_conversation(ops)
    app.start_session(source=CallbackAudioSource(SR), name="live")
    app.begin_capture()
    assert app.capture_conversation_id == ops
    shown = silence_boxes(monkeypatch)
    asked = answers(monkeypatch, window, window.HIDE_SESSION)
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    assert not asked
    assert shown["information"] and "Stop monitoring" in shown["information"][0][2]
    assert store.get_conversation(ops).hidden is False
    assert app.capture is not None and app.capture_conversation_id == ops
    app.stop_session()
    window.close()


# ---- hide, keep, restore ------------------------------------------------------------


def test_hiding_keeps_every_run_message_and_file_and_is_restorable(
        qt_app, config, store, wav, monkeypatch):
    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    before = snapshot(ops)
    app.select_conversation(ops)
    window._refresh_session_tabs()
    window._reload_timeline()
    pump(qt_app)
    assert window.timeline.count() == len(before[1])

    asked = answers(monkeypatch, window, window.HIDE_SESSION)
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    assert asked and "2 monitoring run(s)" in asked[0][2]
    assert tab_index_for(window, ops) is None, "the tab is still shown"
    assert store.get_conversation(ops).hidden is True
    assert snapshot(ops) == before, "hiding changed the data"
    # Viewing moved to General, whose thread is on screen.
    assert app.conversation_id == general
    assert window.session_tabs.tabData(window.session_tabs.currentIndex()) == general
    assert "kept" in window.status.currentMessage()

    window.show_hidden_sessions_action.setChecked(True)
    pump(qt_app)
    index = tab_index_for(window, ops)
    assert index is not None and "(hidden)" in window.session_tabs.tabText(index)
    menu_action(window, index, "Restore Session").trigger()
    pump(qt_app)
    assert store.get_conversation(ops).hidden is False
    window.show_hidden_sessions_action.setChecked(False)
    pump(qt_app)
    index = tab_index_for(window, ops)
    assert index is not None and "(hidden)" not in window.session_tabs.tabText(index)
    window.close()


def test_a_hidden_session_stays_hidden_after_relaunch(qt_app, config, store, wav,
                                                      monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    before = snapshot(ops)
    answers(monkeypatch, window, window.HIDE_SESSION)
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    window.close()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    app2 = mock_app(config, reopened)
    window2 = MainWindow(app2)
    pump(qt_app)
    assert tab_index_for(window2, ops) is None
    assert reopened.get_conversation(ops).hidden is True
    assert reopened.session_ids_for_conversation(ops) == before[0]
    window2.close()


# ---- permanent deletion ---------------------------------------------------------------


def test_permanent_deletion_removes_runs_messages_and_files_of_that_session_only(
        qt_app, config, store, wav, monkeypatch):
    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    ops_sessions, ops_txs, ops_files = snapshot(ops)
    gen_before = snapshot(general)
    inventory = app.conversation_removal_inventory(ops)
    assert set(inventory.session_ids) == set(ops_sessions)
    assert set(inventory.transmission_ids) == set(ops_txs)
    assert set(inventory.owned) == set(ops_files)

    asked = answers(monkeypatch, window, window.DELETE_SESSION,
                    window.CONFIRM_DELETE_SESSION)
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)

    scope = asked[1][2]
    assert "2 monitoring run(s)" in scope and f"{len(ops_txs)} message(s)" in scope
    assert f"{len(ops_files)} recording file(s)" in scope
    assert store.get_conversation(ops) is None
    for session_id in ops_sessions:
        assert store.get_session(session_id) is None
    for tx_id in ops_txs:
        assert store.get_transmission(tx_id) is None and store.is_deleted(tx_id)
    assert not any(pathlib.Path(f).exists() for f in ops_files)
    assert tab_index_for(window, ops) is None
    assert "deleted" in window.status.currentMessage()
    # General is exactly as it was.
    assert snapshot(general) == gen_before
    assert store.default_conversation().id == general
    window.close()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    assert reopened.get_conversation(ops) is None
    assert [c.id for c in reopened.list_conversations(include_hidden=True)] == [general]
    reopened.close()


def test_deleting_the_viewed_session_moves_to_general_and_stops_its_playback(
        qt_app, config, store, wav, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "playback_tests", pathlib.Path(__file__).with_name("test_alpha5_playback.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from babelfishr.ui.playback import PlaybackController

    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    _, ops_txs, ops_files = snapshot(ops)
    app.select_conversation(ops)
    window._refresh_session_tabs()
    window._reload_timeline()
    backend = module.make_scripted_backend()
    controller = PlaybackController(backend, parent=window.timeline)
    window.timeline.playback.changed.disconnect(window.timeline._on_playback_changed)
    window.timeline.playback = controller
    window.timeline._player = controller
    controller.changed.connect(window.timeline._on_playback_changed)
    controller.play(ops_txs[0], ops_files[0])
    assert controller.owner == ops_txs[0]

    answers(monkeypatch, window, window.DELETE_SESSION, window.CONFIRM_DELETE_SESSION)
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    assert controller.owner is None and ("stop",) in backend.calls
    assert app.conversation_id == general
    assert window.session_tabs.tabData(window.session_tabs.currentIndex()) == general
    assert window.timeline.count() == len(snapshot(general)[1])
    window.close()


def test_a_file_shared_with_another_session_survives_deletion(qt_app, config,
                                                              store, wav,
                                                              monkeypatch):
    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    _, ops_txs, ops_files = snapshot(ops)
    _, gen_txs, _ = snapshot(general)
    keeper = store.get_transmission(gen_txs[0])
    keeper.processed_audio_path = ops_files[0]        # General uses an Ops file
    store.save_transmission(keeper)

    inventory = app.conversation_removal_inventory(ops)
    assert ops_files[0] in inventory.shared and ops_files[0] not in inventory.owned
    asked = answers(monkeypatch, window, window.DELETE_SESSION,
                    window.CONFIRM_DELETE_SESSION)
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    assert "NOT deleted: 1 file(s) another Session" in asked[1][2]
    assert pathlib.Path(ops_files[0]).is_file(), "a shared file was deleted"
    assert not any(pathlib.Path(f).exists() for f in ops_files[1:])
    assert store.get_transmission(gen_txs[0]).processed_audio_path == ops_files[0]
    window.close()


def test_an_unremovable_file_is_reported_and_left_for_retry(qt_app, config, store,
                                                            wav, monkeypatch):
    app, window, general, ops, snapshot = two_sessions_window(qt_app, config,
                                                              store, wav)
    _, ops_txs, ops_files = snapshot(ops)
    stuck = pathlib.Path(ops_files[0])
    real_unlink = pathlib.Path.unlink

    def refusing(self, *a, **k):
        if self == stuck:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", refusing)
    shown = silence_boxes(monkeypatch)
    answers(monkeypatch, window, window.DELETE_SESSION, window.CONFIRM_DELETE_SESSION)
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    assert store.get_conversation(ops) is None
    assert stuck.is_file()
    assert shown["warning"] and str(stuck) in shown["warning"][0][2]
    assert store.leftover_deletions() == {ops_txs[0]: [str(stuck)]}
    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    assert app.retry_leftover_deletions().still == {}
    assert not stuck.exists()
    window.close()


# ---- migration -------------------------------------------------------------------------


def test_an_existing_database_shows_every_session_after_migration(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "colors_tests", pathlib.Path(__file__).with_name("test_alpha5_session_colors.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = str(tmp_path / "run21.sqlite3")
    module._schema_4_database(database, "")
    store = Store(database, recordings_dir=str(tmp_path))
    assert "hidden" in store._columns("conversations")
    assert [c.id for c in store.list_conversations()] == ["conv_gen", "conv_ops"]
    assert all(c.hidden is False for c in store.list_conversations())
    store.close()
