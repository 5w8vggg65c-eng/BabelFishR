"""A recording a retained message still uses is never deleted - on the first
deletion, on a later retry, whatever the path looks like, whichever field
refers to it, and whichever Session the retained message is in.

Every destructive step here runs in a disposable temporary home on mock
engines. Only ``pathlib.Path.unlink`` is ever substituted, and only to make a
particular file refuse to go - the way a locked or read-only file would.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import threading
import time

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import AnalysisArtifact, AnalysisAttempt
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.testing import build_fixture

SR = 48_000
AWKWARD_NAME = 'decodé "quoted".wav'


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


def two_messages(config, store, wav):
    """Two recorded, processed messages in General; nothing else running."""
    app = mock_app(config, store)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    app.stop_session()
    txs = app.recent_transmissions()
    assert len(txs) >= 2
    for tx in txs:
        assert tx.audio_path and pathlib.Path(tx.audio_path).is_file()
    return app, txs[:2]


def refuse_unlink_of(monkeypatch, path: pathlib.Path):
    """Make one file refuse to go, as a locked file would. Returns the undo."""
    real = pathlib.Path.unlink

    def refusing(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", refusing)
    return lambda: monkeypatch.setattr(pathlib.Path, "unlink", real)


def awkward_artifact(store, txs, root: pathlib.Path) -> str:
    """A decoded-audio artifact whose path holds an accent and quotation marks,
    attached to every message in ``txs``. Its stored form is escaped JSON."""
    weird = root / AWKWARD_NAME
    shutil.copy(txs[0].audio_path, weird)
    for tx in txs:
        tx.analysis_attempts.append(AnalysisAttempt(
            transmission_id=tx.id, engine="dsd-neo", input_path=tx.audio_path,
            artifacts=[AnalysisArtifact(kind="decoded-audio", path=str(weird))]))
        store.save_transmission(tx)
    return str(weird)


# ---- retry re-evaluates sharing -------------------------------------------------------


def test_retry_preserves_a_file_a_retained_message_has_since_come_to_use(
        config, store, wav, monkeypatch):
    app, (a, b) = two_messages(config, store, wav)
    a_file = pathlib.Path(a.audio_path)
    data = a_file.read_bytes()

    undo = refuse_unlink_of(monkeypatch, a_file)
    report = app.delete_permanently(a.id)
    assert not report.complete
    assert store.leftover_deletions() == {a.id: [str(a_file)]}
    assert a_file.is_file()

    # Since then, a retained message has come to use the same file.
    keeper = store.get_transmission(b.id)
    keeper.processed_audio_path = str(a_file)
    store.save_transmission(keeper)
    undo()

    result = app.retry_leftover_deletions()
    assert result.preserved == {a.id: [str(a_file)]}
    assert result.still == {} and result.removed == []
    assert a_file.read_bytes() == data, "the retained message's recording was destroyed"
    assert store.leftover_deletions() == {}, "a preserved file is not a leftover"
    assert store.get_transmission(b.id).processed_audio_path == str(a_file)


def test_a_leftover_artifact_with_an_awkward_path_is_preserved_on_retry(
        config, store, wav, monkeypatch):
    app, (a, b) = two_messages(config, store, wav)
    root = pathlib.Path(app.owned_roots()[0])
    weird = pathlib.Path(awkward_artifact(store, [a], root))
    data = weird.read_bytes()

    undo = refuse_unlink_of(monkeypatch, weird)
    report = app.delete_permanently(a.id)
    assert store.leftover_deletions() == {a.id: [str(weird)]}
    assert not pathlib.Path(a.audio_path).exists() and weird.is_file()
    assert [p for p, _ in report.failed] == [str(weird)]

    keeper = store.get_transmission(b.id)
    keeper.analysis_attempts.append(AnalysisAttempt(
        transmission_id=keeper.id, engine="dsd-neo", input_path=keeper.audio_path,
        artifacts=[AnalysisArtifact(kind="decoded-audio", path=str(weird))]))
    store.save_transmission(keeper)
    undo()

    result = app.retry_leftover_deletions()
    assert result.preserved == {a.id: [str(weird)]}
    assert weird.read_bytes() == data
    assert store.leftover_deletions() == {}


# ---- the reference is read, not text-matched -----------------------------------------


def test_analysis_artifact_paths_with_accents_and_quotes_protect_the_file(
        config, store, wav):
    app, (a, b) = two_messages(config, store, wav)
    root = pathlib.Path(app.owned_roots()[0])
    weird = awkward_artifact(store, [a, b], root)

    # Why a text match over the stored JSON could not find it.
    stored = json.dumps(weird)
    assert weird not in stored
    assert "\\u00e9" in stored and '\\"' in stored

    inventory = app.deletion_inventory(a.id)
    assert weird in inventory.shared and weird not in inventory.owned
    assert b.id in inventory.reasons[weird]

    report = app.delete_permanently(a.id)
    assert report.complete
    assert pathlib.Path(weird).is_file(), "the artifact another message uses was deleted"
    assert not pathlib.Path(a.audio_path).exists(), "a's own recording should have gone"


def test_equivalent_spellings_of_the_same_file_count_as_the_same_file(config, store,
                                                                       wav):
    app, (a, b) = two_messages(config, store, wav)
    a_file = pathlib.Path(a.audio_path)
    # Built as strings: pathlib would collapse the "." itself.
    dotted = f"{a_file.parent}/./{a_file.name}"
    hopped = f"{a_file.parent}/elsewhere/../{a_file.name}"
    link = a_file.parent / "alias.wav"
    link.symlink_to(a_file)
    for spelling in (dotted, hopped, str(link)):
        assert os.path.realpath(spelling) == os.path.realpath(str(a_file))
        assert spelling != str(a_file)

    keeper = store.get_transmission(b.id)
    for spelling in (dotted, hopped, str(link)):
        keeper.processed_audio_path = spelling
        store.save_transmission(keeper)
        inventory = app.deletion_inventory(a.id)
        assert str(a_file) in inventory.shared, spelling
        assert str(a_file) not in inventory.owned, spelling

    keeper.processed_audio_path = ""
    store.save_transmission(keeper)
    assert str(a_file) in app.deletion_inventory(a.id).owned


def test_every_kind_of_reference_protects_including_from_another_session(
        config, store, wav):
    app, (a, b) = two_messages(config, store, wav)
    ops = app.create_conversation("Ops")
    app.select_conversation(ops.id)
    app.start_session(replay_path=wav, name="ops")
    app.run_replay()
    app.stop_session()
    c = app.recent_transmissions()[0]
    assert store.get_session(c.session_id).conversation_id == ops.id
    a_file = a.audio_path
    original_c_audio = c.audio_path

    def attempt(**kwargs):
        return AnalysisAttempt(transmission_id=c.id, engine="dsd-neo", **kwargs)

    cases = {
        "audio_path": lambda tx: setattr(tx, "audio_path", a_file),
        "processed_audio_path": lambda tx: setattr(tx, "processed_audio_path", a_file),
        "analysis input, derived": lambda tx: tx.analysis_attempts.append(
            attempt(input_path=a_file, input_is_derived=True)),
        "analysis input, original": lambda tx: tx.analysis_attempts.append(
            attempt(input_path=a_file, input_is_derived=False)),
        "analysis artifact": lambda tx: tx.analysis_attempts.append(
            attempt(input_path=original_c_audio,
                    artifacts=[AnalysisArtifact(kind="log", path=a_file)])),
    }
    for name, refer in cases.items():
        fresh = store.get_transmission(c.id)
        fresh.audio_path = original_c_audio
        fresh.processed_audio_path = ""
        fresh.analysis_attempts = []
        refer(fresh)
        store.save_transmission(fresh)
        inventory = app.deletion_inventory(a.id)
        assert a_file in inventory.shared, name
        assert c.id in inventory.reasons[a_file], name

    fresh = store.get_transmission(c.id)
    fresh.audio_path = original_c_audio
    fresh.processed_audio_path = ""
    fresh.analysis_attempts = []
    store.save_transmission(fresh)
    assert a_file in app.deletion_inventory(a.id).owned


def test_an_unreadable_analysis_record_protects_rather_than_permits(config, store,
                                                                    wav):
    app, (a, b) = two_messages(config, store, wav)
    with store._lock:
        store._conn.execute(
            "UPDATE transmissions SET analysis_attempts = ? WHERE id = ?",
            ("{this is not json", b.id))
        store._conn.commit()

    inventory = app.deletion_inventory(a.id)
    assert a.audio_path in inventory.shared
    assert "could not be read" in inventory.reasons[a.audio_path]
    report = app.delete_permanently(a.id)
    assert report is not None and store.get_transmission(a.id) is None
    assert pathlib.Path(a.audio_path).is_file(), (
        "a file was deleted on the strength of a record nobody could read")


# ---- what still deletes, and what is still left alone ---------------------------------


def test_an_unshared_owned_file_still_deletes(config, store, wav):
    app, (a, b) = two_messages(config, store, wav)
    inventory = app.deletion_inventory(a.id)
    assert inventory.owned == [a.audio_path] and not inventory.shared
    report = app.delete_permanently(a.id)
    assert report.complete and report.removed == [a.audio_path]
    assert not pathlib.Path(a.audio_path).exists()
    assert pathlib.Path(b.audio_path).is_file()
    assert store.leftover_deletions() == {}


def test_external_files_and_symlinks_are_left_alone_on_retry(config, store, wav,
                                                             tmp_path):
    app, (a, b) = two_messages(config, store, wav)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"RIFF outside")
    link = pathlib.Path(app.owned_roots()[0]) / "link.wav"
    link.symlink_to(pathlib.Path(b.audio_path))
    # A tombstone naming both, as one from before a move could.
    with store._lock:
        store._tombstone("gone-1", None, [str(outside), str(link)])
        store._conn.commit()

    result = app.retry_leftover_deletions()
    assert result.still == {"gone-1": [str(outside), str(link)]}
    assert result.removed == [] and result.preserved == {}
    assert outside.exists() and link.is_symlink()
    assert pathlib.Path(b.audio_path).is_file()


def test_a_save_that_races_the_deletion_waits_for_it(config, store, wav, monkeypatch):
    """The sharing decision and the unlink are one step under the store's
    lock: a save adding a reference lands after the deletion, never between."""
    app, (a, b) = two_messages(config, store, wav)
    a_file = pathlib.Path(a.audio_path)
    entered = threading.Event()
    times = {}
    real = pathlib.Path.unlink

    def slow(self, *args, **kwargs):
        if self == a_file:
            entered.set()
            time.sleep(0.4)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", slow)

    def late_reference():
        assert entered.wait(10.0)
        fresh = store.get_transmission(b.id)
        fresh.processed_audio_path = str(a_file)
        store.save_transmission(fresh)
        times["saved"] = time.monotonic()

    thread = threading.Thread(target=late_reference)
    thread.start()
    report = app.delete_permanently(a.id)
    times["deleted"] = time.monotonic()
    thread.join(10.0)
    assert "saved" in times
    assert times["saved"] >= times["deleted"], (
        "a save slipped in between the sharing decision and the unlink")
    assert report.complete and not a_file.exists()


# ---- through the window --------------------------------------------------------------


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
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    general = app.conversation_id
    ops = app.create_conversation("Ops")
    for conversation_id in (general, ops.id):
        app.select_conversation(conversation_id)
        app.start_session(replay_path=wav, name=f"run in {conversation_id}")
        app.run_replay()
        app.stop_session()
    app.select_conversation(general)
    window._refresh_session_tabs()
    window._reload_timeline()
    pump(qt_app)

    def messages(conversation_id):
        return [t for s in store.session_ids_for_conversation(conversation_id)
                for t in store.list_transmissions(session_id=s, limit=1000,
                                                  include_hidden=True)]

    return app, window, general, ops.id, messages


def test_the_window_keeps_a_shared_awkward_artifact_through_message_and_session_removal(
        qt_app, config, store, wav, monkeypatch):
    app, window, general, ops, messages = two_sessions_window(qt_app, config, store, wav)
    gen, ops_msgs = messages(general), messages(ops)
    assert len(gen) >= 2 and ops_msgs
    root = pathlib.Path(app.owned_roots()[0])
    victim, keeper, ops_msg = gen[0], gen[1], ops_msgs[0]
    weird = pathlib.Path(awkward_artifact(store, [victim, keeper, ops_msg], root))
    ops_files = [t.audio_path for t in ops_msgs]
    shown = silence_boxes(monkeypatch)

    # The message, for good, through its own bubble menu.
    window._reload_timeline()
    pump(qt_app)
    answers(monkeypatch, window, window.DELETE_FOREVER, window.CONFIRM_DELETE)
    window.timeline._bubbles[victim.id].remove_action.trigger()
    pump(qt_app)
    assert store.get_transmission(victim.id) is None
    assert not pathlib.Path(victim.audio_path).exists()
    assert weird.is_file(), "the artifact two other messages use went with the message"

    # The other Session, for good, through its tab menu.
    answers(monkeypatch, window, window.DELETE_SESSION, window.CONFIRM_DELETE_SESSION)
    menu_action(window, tab_index_for(window, ops), "Remove Session").trigger()
    pump(qt_app)
    assert store.get_conversation(ops) is None
    assert not any(pathlib.Path(f).exists() for f in ops_files), "Ops' own recordings stayed"
    assert weird.is_file(), "the artifact the retained message uses went with the Session"
    kept = store.get_transmission(keeper.id)
    assert kept.analysis_attempts[0].artifacts[0].path == str(weird)
    assert not shown["warning"], [w[2] for w in shown["warning"]]
    window.close()


def test_finish_unfinished_deletions_reports_what_it_kept(qt_app, config, store, wav,
                                                          monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app, (a, b) = two_messages(config, store, wav)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    a_file = pathlib.Path(a.audio_path)
    undo = refuse_unlink_of(monkeypatch, a_file)
    assert not app.delete_permanently(a.id).complete
    keeper = store.get_transmission(b.id)
    keeper.processed_audio_path = str(a_file)
    store.save_transmission(keeper)
    undo()
    shown = silence_boxes(monkeypatch)

    window.finish_deletions_action.trigger()
    pump(qt_app)
    assert shown["information"], shown
    text = shown["information"][-1][2]
    assert "Removed 0 file(s)" in text and "Kept 1 file(s)" in text and str(a_file) in text
    assert "All done" in text
    assert a_file.is_file()
    window.close()
