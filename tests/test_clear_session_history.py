"""Clear Session History: Transcripts only, Audio files only, or Both.

Right-click a Session tab (General included) and choose Clear Session
History. One dialog picks the scope, shows what is lost and what is kept,
and needs a deliberate confirmation. The Session itself (tab, name, colour,
identity) stays and can be used again.

Each scope covers every message in every run of that Session, including
messages removed from view. Other Sessions, files outside the Recordings
folder and files another Session still uses are kept. Anything that may
still write to the Session refuses the clear: live capture, a stopped run
still saving its last message or its end marker, saved-but-unhanded
messages, running and queued jobs. A copy of a row loaded before the clear,
whether in a worker or a queued window event, can neither be saved nor
drawn.

Synthetic records and files in temporary folders only; nothing needs a
network.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import types

import pytest

from babelfishr.app import BabelFishRApp, ProcessingBusy
from babelfishr.models import ProcessingState, Session, Transmission
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine

T0 = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.timezone.utc)


def _file(path: pathlib.Path, data=b"RIFF synthetic") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


class World:
    """Two named Sessions: Ops (two runs, a removed-from-view message,
    corrections, an earlier correction, notes, a bookmark, a shared file,
    an outside file, a missing file) and Other (one run, one message)."""

    def __init__(self, app: BabelFishRApp, tmp: pathlib.Path):
        self.app, self.store = app, app.store
        rec = pathlib.Path(app.config.recording.directory)
        self.ops = self.store.create_conversation("Ops")
        self.store.set_conversation_color(self.ops.id, "#aa3300")
        self.other = self.store.create_conversation("Other")
        for sid, conv in (("run-a", self.ops), ("run-b", self.ops),
                          ("run-o", self.other)):
            self.store.save_session(Session(id=sid, conversation_id=conv.id))
        self.shared = _file(rec / "shared.wav")
        self.outside = _file(tmp / "elsewhere" / "mine.wav")
        self.a1 = self._tx("run-a", 0, "alpha bravo", "ALPHA BRAVO",
                           audio=_file(rec / "a1.wav"),
                           processed=_file(rec / "a1.proc.wav"),
                           transcript_correction="alpha charlie",
                           notes="keep this note", bookmarked=True,
                           signal_metadata={"superseded_corrections": [
                               {"transcript": "alpha delta"}], "keep": 1})
        self.a2 = self._tx("run-a", 10, "hidden words", "HIDDEN", hidden=True,
                           audio=_file(rec / "a2.wav"))
        self.b1 = self._tx("run-b", 20, "bravo run two", "BRAVO",
                           audio=self.shared, tags=["urgent"])
        self.b2 = self._tx("run-b", 30, "outside file", "OUTSIDE",
                           audio=self.outside)
        self.b3 = self._tx("run-b", 40, "file gone", "GONE",
                           audio=str(rec / "missing.wav"))
        self.o1 = self._tx("run-o", 50, "other session text", "OTHER",
                           audio=self.shared)
        self.o_own = _file(rec / "o-own.wav")
        self.o2 = self._tx("run-o", 60, "other own", "OWN", audio=self.o_own)

    def _tx(self, run, offset, transcript, translation, audio=None,
            processed=None, **extra):
        tx = Transmission(session_id=run, started_at=T0 + dt.timedelta(seconds=offset),
                          duration=2.0, state=ProcessingState.COMPLETE,
                          transcript=transcript, translation=translation,
                          source_language="de", target_language="en",
                          audio_path=audio, processed_audio_path=processed,
                          transcript_segments=[{"start": 0.0, "end": 1.0,
                                                "text": transcript}], **extra)
        self.store.save_transmission(tx)
        return tx

    def ops_ids(self):
        return [self.a1.id, self.a2.id, self.b1.id, self.b2.id, self.b3.id]

    def row(self, tx):
        return self.store.get_transmission(tx.id)

    def found(self, word, conversation):
        return {t.id for t in self.store.search(word, conversation_id=conversation.id)}


@pytest.fixture
def world(app, tmp_path):
    return World(app, tmp_path)


def _untouched_other(world):
    o1, o2 = world.row(world.o1), world.row(world.o2)
    assert o1.transcript == "other session text" and o1.audio_path == world.shared
    assert o2.audio_path == world.o_own and pathlib.Path(world.o_own).exists()
    assert world.found("other", world.other) == {world.o1.id, world.o2.id}


def _session_kept(world):
    conv = world.store.get_conversation(world.ops.id)
    assert conv is not None and conv.name == "Ops" and conv.color == "#aa3300"
    assert set(world.store.session_ids_for_conversation(world.ops.id)) == {"run-a", "run-b"}


# --- Transcripts only -----------------------------------------------------------

def test_text_only_purges_text_and_keeps_rows_audio_notes_and_bookmarks(world):
    report = world.app.clear_conversation_history(world.ops.id, "text")
    assert report.complete and set(report.changed) == set(world.ops_ids())
    for tx in (world.a1, world.a2, world.b1, world.b2, world.b3):
        row = world.row(tx)
        assert row is not None, "rows are kept"
        assert row.transcript == row.translation == ""
        assert row.transcript_correction is None and row.translation_correction is None
        assert row.transcript_segments == []
        assert "superseded_corrections" not in row.signal_metadata
        assert "text" in row.signal_metadata["history_cleared"]
        assert row.history_generation == 1
    a1 = world.row(world.a1)
    assert a1.notes == "keep this note" and a1.bookmarked
    assert a1.signal_metadata["keep"] == 1
    assert world.row(world.a2).hidden, "a removed-from-view message stays removed"
    assert world.row(world.b1).tags == ["urgent"]
    # Playable audio stays.
    assert pathlib.Path(a1.audio_path).exists() and pathlib.Path(a1.processed_audio_path).exists()
    # Search: the text is gone, the note still finds its message.
    for word in ("alpha", "charlie", "bravo", "hidden", "ALPHA"):
        assert world.found(word, world.ops) == set(), word
    assert world.found("note", world.ops) == {world.a1.id}
    _untouched_other(world)
    _session_kept(world)


# --- Audio files only -------------------------------------------------------------

def test_audio_only_removes_owned_audio_and_keeps_text_and_outside_files(world):
    a1_files = [world.a1.audio_path, world.a1.processed_audio_path]
    report = world.app.clear_conversation_history(world.ops.id, "audio")
    assert report.complete
    for path in a1_files + [world.a2.audio_path]:
        assert not pathlib.Path(path).exists() and path in report.removed
    assert world.outside in report.kept_external and pathlib.Path(world.outside).exists()
    assert world.shared in report.kept_shared and pathlib.Path(world.shared).exists()
    assert world.b3.audio_path in report.already_gone
    for tx in (world.a1, world.a2, world.b1, world.b2, world.b3):
        row = world.row(tx)
        assert row.audio_path is None and row.processed_audio_path is None
        assert row.transcript and row.translation, "text is kept"
        assert "audio" in row.signal_metadata["history_cleared"]
    a1 = world.row(world.a1)
    assert a1.transcript_correction == "alpha charlie" and a1.notes == "keep this note"
    assert a1.signal_metadata["superseded_corrections"]
    assert world.found("alpha", world.ops) == {world.a1.id}
    _untouched_other(world)          # o1 still plays the shared file
    _session_kept(world)


def test_audio_shared_only_inside_the_session_is_removed(world):
    """Two Ops messages using one file: both are being cleared, so it goes."""
    rec = pathlib.Path(world.app.config.recording.directory)
    both = _file(rec / "both.wav")
    for tx in (world.a1, world.b2):
        row = world.row(tx)
        row.audio_path = both
        world.store.save_transmission(row)
    report = world.app.clear_conversation_history(world.ops.id, "audio")
    assert both in report.removed and not pathlib.Path(both).exists()


def test_a_file_that_will_not_go_stays_playable_and_a_retry_finishes(world, monkeypatch):
    target = world.a1.audio_path
    real_unlink = pathlib.Path.unlink

    def stubborn(self, *a, **k):
        if str(self) == target:
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", stubborn)
    report = world.app.clear_conversation_history(world.ops.id, "audio")
    assert not report.complete and report.failed[0][0] == target
    assert "PermissionError" in report.failed[0][1]
    row = world.row(world.a1)
    assert row.audio_path == target and pathlib.Path(target).exists(), \
        "truthful: still on disk, so still on the row"
    assert row.processed_audio_path is None
    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    again = world.app.clear_conversation_history(world.ops.id, "audio")
    assert again.complete and target in again.removed
    assert world.row(world.a1).audio_path is None


# --- Both -----------------------------------------------------------------------------

def test_both_deletes_every_message_and_its_audio_and_keeps_the_session(world):
    owned = [world.a1.audio_path, world.a1.processed_audio_path, world.a2.audio_path]
    report = world.app.clear_conversation_history(world.ops.id, "both")
    assert report.complete and set(report.deleted) == set(world.ops_ids())
    assert world.app.recent_transmissions(conversation_id=world.ops.id,
                                          include_hidden=True) == []
    for tx_id in world.ops_ids():
        assert world.store.is_deleted(tx_id), "a tombstone stops late writers"
    for path in owned:
        assert not pathlib.Path(path).exists()
    assert pathlib.Path(world.shared).exists() and pathlib.Path(world.outside).exists()
    assert world.found("note", world.ops) == set()
    _untouched_other(world)
    _session_kept(world)
    # Reusable: a new run files into the same Session and shows.
    world.store.save_session(Session(id="run-c", conversation_id=world.ops.id))
    fresh = Transmission(session_id="run-c", started_at=T0, duration=1.0,
                         state=ProcessingState.COMPLETE, transcript="new day")
    world.store.save_transmission(fresh)
    assert [t.id for t in world.app.recent_transmissions(
        conversation_id=world.ops.id)] == [fresh.id]


def test_general_can_be_cleared_and_stays(app, tmp_path):
    general = app.store.default_conversation()
    app.store.save_session(Session(id="g-run", conversation_id=general.id))
    tx = Transmission(session_id="g-run", started_at=T0, duration=1.0,
                      state=ProcessingState.COMPLETE, transcript="general words",
                      audio_path=_file(pathlib.Path(app.config.recording.directory) / "g.wav"))
    app.store.save_transmission(tx)
    report = app.clear_conversation_history(general.id, "both")
    assert report.deleted == [tx.id]
    assert app.store.default_conversation().id == general.id


def test_an_unknown_scope_changes_nothing(world):
    with pytest.raises(ValueError):
        world.app.clear_conversation_history(world.ops.id, "everything")
    assert world.row(world.a1).transcript == "alpha bravo"


# --- late writers ------------------------------------------------------------------

def test_a_copy_loaded_before_a_text_clear_cannot_be_saved(world):
    stale = world.row(world.a1)                  # a worker's copy
    world.app.clear_conversation_history(world.ops.id, "text")
    stale.translation = "LATE RESULT"
    world.store.save_transmission(stale)
    row = world.row(world.a1)
    assert row.transcript == "" and row.translation == ""
    assert world.store.is_stale(stale)
    # A copy read after the clear (Retry, a correction) saves normally.
    fresh = world.row(world.a1)
    fresh.transcript = "re-transcribed"
    world.store.save_transmission(fresh)
    assert world.row(world.a1).transcript == "re-transcribed"
    assert world.found("transcribed", world.ops) == {world.a1.id}


def test_a_copy_loaded_before_an_audio_clear_cannot_restore_the_path(world):
    stale = world.row(world.a1)
    world.app.clear_conversation_history(world.ops.id, "audio")
    world.store.save_transmission(stale)
    assert world.row(world.a1).audio_path is None


def test_the_app_correct_path_after_a_clear_works(world):
    world.app.clear_conversation_history(world.ops.id, "text")
    world.app.correct(world.a1.id, transcript="operator words")
    assert world.row(world.a1).transcript_correction == "operator words"


# --- refused while anything may still write ------------------------------------------

def _refused(world, needle):
    before = world.row(world.a1).transcript
    problem = world.app.history_clear_problem(world.ops.id)
    assert needle in problem, problem
    with pytest.raises(ProcessingBusy):
        world.app.clear_conversation_history(world.ops.id, "both")
    assert world.row(world.a1).transcript == before, "nothing changed"
    # Other Sessions are not blocked by it.
    assert world.app.history_clear_problem(world.other.id) == ""


def test_refused_while_monitoring_records_into_it(world):
    world.app.capture = object()
    world.app._capture_conversation_id = world.ops.id
    try:
        _refused(world, "Monitoring is recording")
    finally:
        world.app.capture = None
        world.app._capture_conversation_id = ""


def test_refused_while_a_stopped_capture_still_saves_its_last_message(world):
    """The gap right after Stop: the final flush has no row yet."""
    world.app._lingering_capture = types.SimpleNamespace(
        session=types.SimpleNamespace(id="run-b"), settled=False, unprocessed=[])
    try:
        _refused(world, "still being saved")
    finally:
        world.app._lingering_capture = None


def test_refused_while_the_end_of_a_run_is_being_recorded(world):
    world.app._session_ends.append(types.SimpleNamespace(session_id="run-a", done=False))
    try:
        _refused(world, "end of the last monitoring run")
    finally:
        world.app._session_ends.clear()


def test_refused_while_a_message_waits_for_a_processor(world):
    world.app._unprocessed.append(world.b1.id)
    try:
        _refused(world, "waiting to be processed")
    finally:
        world.app._unprocessed.clear()


@pytest.mark.parametrize("hidden_row", [False, True])
def test_refused_while_a_message_is_running_or_queued(world, hidden_row):
    busy = world.a2.id if hidden_row else world.b3.id
    world.app.standalone_pipeline = types.SimpleNamespace(
        is_in_flight=lambda tx_id: tx_id == busy, pending=1, accepting=True,
        finished=False, shutting_down=False)
    try:
        _refused(world, "still being processed or is queued")
    finally:
        world.app.standalone_pipeline = None


# --- the window ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(qt_app, world):
    from babelfishr.ui.main_window import MainWindow

    world.app.transcription = MockTranscriptionEngine()
    world.app.translation = MockTranslationEngine()
    world.app.select_conversation(world.ops.id)
    win = MainWindow(world.app)
    win._refresh_session_tabs()
    win._reload_timeline()
    yield win
    win.close()


def _tab(window, conversation_id):
    for i in range(window.session_tabs.count()):
        if window.session_tabs.tabData(i) == conversation_id:
            return i
    raise AssertionError("no tab")


def _answer(monkeypatch, window, scope=None, confirm=True):
    """Stand in for the operator at the modal dialog."""
    seen = {}

    def run(dialog):
        seen["dialog"] = dialog
        seen["clear_enabled_before"] = dialog.clear_button.isEnabled()
        seen["text"] = {key: None for key in ("text", "audio", "both")}
        for key, button in dialog.scope_buttons.items():
            button.setChecked(True)
            seen["text"][key] = dialog.summary.text()
        if scope is None:
            return False                         # Cancel
        dialog.scope_buttons[scope].setChecked(True)
        dialog.confirm.setChecked(confirm)
        seen["clear_enabled_after"] = dialog.clear_button.isEnabled()
        return confirm
    monkeypatch.setattr(window, "_run_clear_dialog", run)
    return seen


def test_every_tab_offers_it_general_included(window, world):
    for conv in (world.ops.id, world.app.store.default_conversation().id):
        menu = window._build_session_tab_menu(_tab(window, conv))
        action = [a for a in menu.actions() if a.text() == "Clear Session History…"]
        assert action and action[0].isEnabled()


def test_cancel_changes_nothing(window, world, monkeypatch):
    seen = _answer(monkeypatch, window, scope=None)
    window._clear_session_history(_tab(window, world.ops.id))
    assert seen["clear_enabled_before"] is False, "Clear needs the confirmation"
    assert seen["dialog"].cancel_button.isDefault()
    assert world.row(world.a1).transcript == "alpha bravo"
    assert pathlib.Path(world.a1.audio_path).exists()


def test_the_dialog_shows_what_each_scope_loses_and_keeps(window, world, monkeypatch):
    seen = _answer(monkeypatch, window, scope=None)
    window._clear_session_history(_tab(window, world.ops.id))
    text, audio, both = (seen["text"][k] for k in ("text", "audio", "both"))
    assert "Recognised and translated text of 5 messages (1 of them removed from view)" in text
    assert "stay playable" in text and "Notes (1)" in text
    assert "3 audio files" in audio and "1 file already missing" in audio
    assert "transcripts, translations" in audio and "transcripts, translations" in audio
    assert "another Session still uses" in audio and "outside the Recordings" in audio
    assert "All 5 messages" in both and "Deleted permanently" in both
    for summary in (text, audio, both):
        assert "“Ops” tab" in summary and "Every other Session" in summary


def test_changing_the_scope_asks_for_the_confirmation_again(window, world):
    from babelfishr.ui.clear_history import ClearHistoryDialog

    dialog = ClearHistoryDialog("Ops", world.app.history_inventory(world.ops.id), window)
    dialog.confirm.setChecked(True)
    assert dialog.clear_button.isEnabled()
    dialog.scope_buttons["both"].setChecked(True)
    assert not dialog.confirm.isChecked() and not dialog.clear_button.isEnabled()
    dialog.deleteLater()


def test_text_clear_from_the_window_redraws_the_thread(window, world, monkeypatch):
    window.timeline.select(world.a1.id)
    _answer(monkeypatch, window, scope="text")
    window._clear_session_history(_tab(window, world.ops.id))
    bubble = window.timeline.bubble(world.a1.id)
    assert bubble.original_label.isHidden()
    assert "cleared" in bubble.notes_label.text()
    assert bubble.play_action.isEnabled(), "audio stays playable"
    assert window.session_tabs.tabText(_tab(window, world.ops.id)).startswith("Ops")


def test_audio_clear_from_the_window_leaves_no_stale_play_or_waveform(window, world,
                                                                     monkeypatch):
    window.timeline.select(world.a1.id)
    _answer(monkeypatch, window, scope="audio")
    window._clear_session_history(_tab(window, world.ops.id))
    bubble = window.timeline.bubble(world.a1.id)
    assert not bubble.play_action.isEnabled()
    assert window.timeline.selected_transmission().audio_path is None
    assert not window.selected_play_button.isEnabled()
    assert "Recording removed" in bubble.notes_label.text()
    assert not bubble.retry_action.isEnabled()
    assert not world.app.retry(world.a1.id), "nothing to process"
    assert world.row(world.a1).state is ProcessingState.COMPLETE


def test_both_from_the_window_empties_the_thread(window, world, monkeypatch):
    _answer(monkeypatch, window, scope="both")
    window._clear_session_history(_tab(window, world.ops.id))
    assert window.timeline.order() == []
    assert window.session_tabs.tabText(_tab(window, world.ops.id)).startswith("Ops")


def test_a_refusal_is_explained_and_nothing_is_asked(window, world, monkeypatch):
    from PySide6 import QtWidgets

    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: told.append(a[2]))
    asked = _answer(monkeypatch, window, scope="both")
    world.app._unprocessed.append(world.b1.id)
    try:
        window._clear_session_history(_tab(window, world.ops.id))
    finally:
        world.app._unprocessed.clear()
    assert told and "waiting to be processed" in told[0]
    assert "dialog" not in asked
    assert world.row(world.b1) is not None


# --- stale queued window events (the Build 37 _admit edge) -------------------------

@pytest.mark.parametrize("scope", ["text", "audio"])
@pytest.mark.parametrize("show_removed", [False, True])
def test_an_update_queued_before_a_clear_is_not_drawn(window, world, scope, show_removed):
    window.show_removed_action.setChecked(show_removed)
    window._reload_timeline()
    # Completed updates queued before the clear, for a visible and a hidden row.
    for tx in (world.a1, world.a2):
        world.app.events.publish("updated", world.row(tx))
    world.app.clear_conversation_history(world.ops.id, scope)
    window._drain_events()
    for tx in (world.a1, world.a2):
        row = world.row(tx)
        if tx is world.a2 and not show_removed:
            assert tx.id not in window.timeline.order()
            continue
        shown = window.timeline.bubble(tx.id).tx
        assert shown.history_generation == row.history_generation
        assert shown.transcript == row.transcript
        assert shown.audio_path == row.audio_path
        bubble = window.timeline.bubble(tx.id)
        if scope == "text":
            assert bubble.original_label.isHidden()
        else:
            assert not bubble.play_action.isEnabled()
