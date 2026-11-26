"""A named Session's own save path (removable drive or any writable folder).

Every test uses temporary folders only, real WAV files and real JSON
sidecars. ``<tmp>/drive`` stands in for a removable drive; BabelFishR may
only ever write inside ``<tmp>/drive/BabelFishR``.
"""

from __future__ import annotations

import errno
import json
import os
import pathlib
import shutil
import time

import numpy as np
import pytest

from babelfishr import savepath
from babelfishr.app import BabelFishRApp
from babelfishr.audio.source import CallbackAudioSource
from babelfishr.audio.wavefile import read_wav
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.readiness import CheckStatus
from babelfishr.savepath import SavePathError
from babelfishr.storage import Store
from babelfishr.testing import build_fixture, speech_like

SR = 48_000


# -- helpers --------------------------------------------------------------------------
def mock_app(config, store) -> BabelFishRApp:
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    app.microphone_route = lambda: ""
    return app


@pytest.fixture
def drive(tmp_path) -> pathlib.Path:
    d = tmp_path / "drive"
    d.mkdir()
    return d


@pytest.fixture
def wav(tmp_path) -> str:
    return build_fixture([{"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
                          {"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -12},
                          {"gap": 1.0}], sample_rate=SR).write(
        str(tmp_path / "replay-source" / "two.wav"))


@pytest.fixture
def app(config, store):
    application = mock_app(config, store)
    yield application
    application.stop_session()
    application.wait_for_session_ends(10)
    application.wait_for_portable_transcripts(10)


def replay(app, wav) -> list:
    session = app.start_session(replay_path=wav, name="replay", target_language="en")
    app.run_replay(timeout=60)
    app.stop_session()
    assert app.wait_for_session_ends(10)
    assert app.wait_for_portable_transcripts(10)
    return app.store.list_transmissions(session_id=session.id, limit=100)


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def voice(seconds, seed=1):
    return speech_like(seconds, SR, level_dbfs=-14.0, seed=seed)


def quiet(seconds):
    return np.zeros(int(seconds * SR))


def feed(source, audio):
    for i in range(0, audio.size, 4800):
        source.push(audio[i:i + 4800])


def sidecar_for(tx) -> pathlib.Path:
    wav = pathlib.Path(tx.audio_path)
    parts = wav.parts
    i = parts.index("recordings")
    owned = pathlib.Path(*parts[:i])
    return owned / "transcripts" / pathlib.Path(*parts[i + 1:]).with_suffix(".json")


def load(path) -> dict:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def collect(app):
    seen = []
    app.events.subscribe(lambda e: seen.append(e) if e.kind == "save-path" else None)
    return seen


# -- choosing ---------------------------------------------------------------------------
def test_choice_persists_across_relaunch(config, store, drive):
    app = mock_app(config, store)
    conv = app.create_conversation("Field")
    assert app.save_path_info(conv.id).state == "standard"

    info = app.set_save_path(conv.id, str(drive))
    assert info.state == "ok" and info.is_custom and info.reason == ""
    assert info.owned_dir == drive / "BabelFishR"
    assert info.effective_dir == drive / "BabelFishR" / "recordings"
    assert info.standard_dir == pathlib.Path(config.recording.directory)
    # Only the owned area is created in the chosen folder, and the probe
    # file is gone again.
    assert [p.name for p in drive.iterdir()] == ["BabelFishR"]
    assert list((drive / "BabelFishR").iterdir()) == []
    # Other Sessions keep the standard destination.
    general = app.store.default_conversation().id
    assert app.save_path_info(general).state == "standard"

    store.close()
    again = Store(config.database, recordings_dir=config.recording.directory)
    try:
        app2 = mock_app(config, again)
        info2 = app2.save_path_info(conv.id)
        assert info2.chosen == str(drive) and info2.state == "ok"
        assert again.get_conversation(conv.id).save_path == str(drive)
        assert app2.save_path_info(general).chosen == ""
    finally:
        again.close()


def test_unavailable_folder_is_refused_when_chosen(app, tmp_path):
    conv = app.create_conversation("Field")
    missing = tmp_path / "Volumes" / "USBSTICK"
    with pytest.raises(SavePathError) as exc:
        app.set_save_path(conv.id, str(missing))
    assert savepath.NOT_FOUND in str(exc.value)
    assert isinstance(exc.value, ValueError)
    assert not (tmp_path / "Volumes").exists()        # no phantom mount path
    assert app.store.get_conversation(conv.id).save_path == ""

    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(SavePathError, match="not a folder"):
        app.set_save_path(conv.id, str(a_file))
    with pytest.raises(SavePathError):
        app.set_save_path(conv.id, "relative/folder")
    assert app.store.get_conversation(conv.id).save_path == ""


def test_read_only_folder_is_refused(app, tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    folder.chmod(0o555)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("running with privileges that ignore folder permissions")
        conv = app.create_conversation("Field")
        with pytest.raises(SavePathError, match="read-only"):
            app.set_save_path(conv.id, str(folder))
        assert list(folder.iterdir()) == []
        assert app.store.get_conversation(conv.id).save_path == ""
    finally:
        folder.chmod(0o755)


def test_write_errors_map_to_concrete_reasons(drive, monkeypatch):
    def refuse(code):
        def fake(*args, **kwargs):
            raise OSError(code, os.strerror(code))
        return fake

    monkeypatch.setattr(savepath.tempfile, "mkstemp", refuse(errno.EROFS))
    assert savepath.preflight(str(drive)) == savepath.READ_ONLY
    monkeypatch.setattr(savepath.tempfile, "mkstemp", refuse(errno.ENOSPC))
    assert savepath.preflight(str(drive)) == savepath.FULL
    monkeypatch.undo()
    assert savepath.preflight(str(drive)) == ""
    (drive / "BabelFishR").rmdir()
    (drive / "BabelFishR").write_text("in the way")
    assert savepath.preflight(str(drive)) == savepath.OWNED_NOT_A_FOLDER


def test_reset_to_standard_is_always_allowed(app, drive):
    conv = app.create_conversation("Field")
    app.set_save_path(conv.id, str(drive))
    shutil.rmtree(drive)                       # the drive is gone
    assert app.save_path_info(conv.id).state == "unavailable"
    assert app.save_path_info(conv.id).reason == savepath.NOT_FOUND
    info = app.set_save_path(conv.id, None)
    assert info.state == "standard" and not info.is_custom
    assert app.set_save_path(conv.id, "").state == "standard"
    assert not drive.exists()


# -- recording onto the chosen folder ----------------------------------------------------
def test_replay_writes_recordings_and_portable_transcripts_on_the_drive(app, config, drive, wav):
    conv = app.create_conversation("Field")
    app.select_conversation(conv.id)
    app.set_save_path(conv.id, str(drive))
    txs = replay(app, wav)
    assert len(txs) == 2

    owned = drive / "BabelFishR"
    for tx in txs:
        path = pathlib.Path(tx.audio_path)
        assert path.is_relative_to(owned / "recordings")
        samples, rate = read_wav(str(path))
        assert rate == SR and samples.size > 0
        sidecar = sidecar_for(tx)
        assert sidecar.is_relative_to(owned / "transcripts")
        data = load(sidecar)
        assert data["id"] == tx.id
        assert data["audio_file"] == path.relative_to(owned).as_posix()
        assert data["transcript"] == tx.transcript and tx.transcript
        assert data["translation"] == tx.translation
        assert data["target_language"] == "en"
        assert data["session"] == "Field"
        assert data["engines"]["transcription"] == tx.transcription_engine
        assert data["duration"] > 0 and data["started_at"] and data["ended_at"]
        assert data["reviewed"] is False and data["approved"] is False
    # The run is pinned to the folder, and nothing went to the standard one.
    run = app.store.get_session(txs[0].session_id)
    assert run.save_path == str(drive)
    assert not list(pathlib.Path(config.recording.directory).rglob("*.wav"))
    # No temporary files are left behind.
    assert not [p for p in owned.rglob("*") if p.name.startswith(".")]
    assert app.portable.failures == {}


def test_edits_and_approval_update_the_portable_transcript(app, drive, wav):
    conv = app.create_conversation("Field")
    app.select_conversation(conv.id)
    app.set_save_path(conv.id, str(drive))
    first, second = replay(app, wav)

    app.correct(first.id, transcript="corrected words", translation="fixed")
    app.correct(first.id, notes="checked against the log")
    assert app.wait_for_portable_transcripts(10)
    data = load(sidecar_for(first))
    assert data["transcript_correction"] == "corrected words"
    assert data["translation_correction"] == "fixed"
    assert data["final_transcript"] == "corrected words"
    assert data["notes"] == "checked against the log"
    assert data["reviewed"] is True
    assert data["transcript"] == first.transcript        # original kept

    low = app.store.get_transmission(second.id)
    low.transcript_confidence = 0.3                 # needs review
    app.store.save_transmission(low)
    assert app.approve(second.id) is not None
    app.set_tags(second.id, ["priority"])
    assert app.wait_for_portable_transcripts(10)
    data = load(sidecar_for(second))
    assert data["approved"] is True and data["reviewed"] is True
    assert data["tags"] == ["priority"]

    # A retry re-processes and rewrites the sidecar too.
    before = load(sidecar_for(second))["updated_at"]
    time.sleep(1.1)
    assert app.retry(second.id)
    assert wait_for(lambda: not app.is_manually_processing(second.id))
    app.standalone_pipeline.wait_until_idle(20)
    assert app.wait_for_portable_transcripts(10)
    assert load(sidecar_for(second))["updated_at"] != before


def test_change_and_restore_keep_old_recordings_where_they_are(app, config, drive, wav):
    conv = app.create_conversation("Field")
    app.select_conversation(conv.id)
    app.set_save_path(conv.id, str(drive))
    on_drive = replay(app, wav)

    app.set_save_path(conv.id, None)
    local = replay(app, wav)
    standard = pathlib.Path(config.recording.directory)
    for tx in local:
        assert pathlib.Path(tx.audio_path).is_relative_to(standard)
        assert app.store.get_session(tx.session_id).save_path == ""
    # Earlier recordings were not moved and still play from the drive.
    for tx in on_drive:
        reread = app.store.get_transmission(tx.id)
        assert reread.audio_path == tx.audio_path
        assert pathlib.Path(tx.audio_path).is_relative_to(drive / "BabelFishR")
        assert read_wav(tx.audio_path)[0].size > 0
        assert app.processing_problem(tx.id) == ""
    # Standard runs write no portable transcripts.
    assert not (standard / "transcripts").exists()
    # And back to the drive.
    assert app.set_save_path(conv.id, str(drive)).state == "ok"
    again = replay(app, wav)
    assert all(pathlib.Path(t.audio_path).is_relative_to(drive) for t in again)


def test_start_is_refused_when_the_destination_is_unavailable(app, tmp_path, drive, wav):
    conv = app.create_conversation("Field")
    app.select_conversation(conv.id)
    app.set_save_path(conv.id, str(drive))
    shutil.rmtree(drive)
    with pytest.raises(SavePathError) as exc:
        app.start_session(replay_path=wav, name="replay")
    message = str(exc.value)
    assert "not found (drive ejected or folder removed)" in message
    assert str(drive / "BabelFishR") in message
    assert app.capture is None and app.session is None
    assert app.store.list_sessions() == []           # nothing was started
    assert not drive.exists()                        # and nothing recreated
    # The standard destination always works.
    app.set_save_path(conv.id, None)
    app.start_session(replay_path=wav, name="replay")
    app.run_replay(timeout=60)


def test_destination_is_locked_while_this_session_records(app, drive, tmp_path):
    conv = app.create_conversation("Field")
    other = app.create_conversation("Other")
    app.select_conversation(conv.id)
    source = CallbackAudioSource(SR, name="mic")
    app.start_session(source=source, name="mic")
    app.begin_capture()
    try:
        with pytest.raises(SavePathError, match="until you stop monitoring"):
            app.set_save_path(conv.id, str(drive))
        with pytest.raises(SavePathError):
            app.set_save_path(conv.id, None)
        assert app.save_path_locked(conv.id)
        # Another Session's destination can still be chosen.
        assert app.set_save_path(other.id, str(drive)).state == "ok"
    finally:
        app.stop_session()
        app.wait_for_session_ends(10)
    assert app.set_save_path(conv.id, str(drive)).state == "ok"


def test_ejection_mid_watch_falls_back_to_local_and_says_so(app, config, drive):
    conv = app.create_conversation("Field")
    app.select_conversation(conv.id)
    app.set_save_path(conv.id, str(drive))
    events = collect(app)
    source = CallbackAudioSource(SR, name="mic")
    session = app.start_session(source=source, name="mic")
    app.begin_capture()
    try:
        feed(source, np.concatenate([quiet(1.0), voice(1.5), quiet(2.0)]))
        assert wait_for(lambda: len(app.store.list_transmissions(session.id)) == 1)
        ejected = drive.with_name("drive-ejected")
        drive.rename(ejected)                          # the drive goes away
        feed(source, np.concatenate([voice(1.5, seed=3), quiet(2.0)]))
        assert wait_for(lambda: len(app.store.list_transmissions(session.id)) == 2)
    finally:
        app.stop_session()
        app.wait_for_session_ends(10)
    app.wait_for_processing(20)
    if app.standalone_pipeline is not None:
        app.standalone_pipeline.wait_until_idle(20)
    assert app.wait_for_portable_transcripts(10)

    first, second = sorted(app.store.list_transmissions(session.id, limit=10),
                           key=lambda t: t.started_at)
    assert pathlib.Path(first.audio_path).is_relative_to(drive / "BabelFishR")
    standard = pathlib.Path(config.recording.directory)
    assert pathlib.Path(second.audio_path).is_relative_to(standard)
    assert read_wav(second.audio_path)[0].size > 0
    fallback = second.signal_metadata["save_path_fallback"]
    assert fallback["reason"] == savepath.NOT_FOUND
    assert fallback["used"] == str(pathlib.Path(second.audio_path).parent)
    assert "save_path_fallback" not in first.signal_metadata
    # Reported, with where the audio really went.
    states = [e.payload for e in events if e.payload["state"] == "fallback"]
    assert len(states) == 1
    assert states[0]["path"] == str(standard)
    assert states[0]["reason"] == savepath.NOT_FOUND
    assert states[0]["transmission"] == second.id
    # The portable transcript could not be written, and that is recorded.
    assert second.id in app.portable.failures
    assert "could not be saved" in app.portable_transcript_problem(second.id)
    assert app.store.get_transmission(second.id).signal_metadata[
        "portable_transcript"]["state"] == "failed"
    assert any(e.payload["state"] == "transcript-failed"
               and e.payload["transmission"] == second.id for e in events)
    # The ejected drive was not recreated as a phantom folder.
    assert not drive.exists()
    # The first message's sidecar is on the (ejected) drive.
    moved = pathlib.Path(str(sidecar_for(first)).replace(str(drive), str(ejected)))
    assert load(moved)["id"] == first.id


def test_queued_run_keeps_its_own_path_after_the_session_changes(app, tmp_path, drive):
    second_drive = tmp_path / "drive2"
    second_drive.mkdir()
    conv = app.create_conversation("Field")
    app.select_conversation(conv.id)
    app.set_save_path(conv.id, str(drive))
    source = CallbackAudioSource(SR, name="mic")
    session = app.start_session(source=source, name="mic")
    app.begin_capture()
    # A message still open at Stop: it is flushed after the Stop, by which
    # time the Session already points somewhere else.
    feed(source, np.concatenate([quiet(1.0), voice(1.5)]))
    time.sleep(0.5)
    app.stop_session()
    app.set_save_path(conv.id, str(second_drive))
    app.wait_for_session_ends(10)
    assert wait_for(lambda: app.store.list_transmissions(session.id))
    app._reap()
    if app.standalone_pipeline is not None:
        app.standalone_pipeline.wait_until_idle(20)
    (tx,) = app.store.list_transmissions(session.id)
    assert pathlib.Path(tx.audio_path).is_relative_to(drive / "BabelFishR")
    assert app.store.get_session(session.id).save_path == str(drive)
    # Later edits of that older run's message go to its own drive too.
    app.correct(tx.id, notes="edited later")
    assert app.wait_for_portable_transcripts(10)
    assert load(sidecar_for(tx))["notes"] == "edited later"
    assert not (second_drive / "BabelFishR" / "transcripts").exists()
    assert not (second_drive / "BabelFishR" / "recordings").exists()


# -- deletion, readiness, exports -------------------------------------------------------
def test_deletion_ownership_stops_at_the_babelfishr_folder(app, drive, wav):
    conv = app.create_conversation("Field")
    app.select_conversation(conv.id)
    app.set_save_path(conv.id, str(drive))
    sibling = drive / "holiday.wav"
    sibling.write_bytes(b"not ours")
    first, second = replay(app, wav)

    roots = app.owned_roots()
    assert str(drive / "BabelFishR") in roots
    assert str(drive) not in roots

    # A message whose recording is the operator's own file in the chosen
    # folder (a replay source, say) never has it deleted.
    tx = app.store.get_transmission(second.id)
    tx.audio_path = str(sibling)
    app.store.save_transmission(tx)
    inventory = app.deletion_inventory(second.id)
    assert inventory.external == [str(sibling)] and inventory.owned == []

    first_wav, first_json = pathlib.Path(first.audio_path), sidecar_for(first)
    report = app.delete_permanently(first.id)
    assert report.removed == [str(first_wav)]
    assert app.wait_for_portable_transcripts(10)
    assert not first_wav.exists() and not first_json.exists()
    app.delete_permanently(second.id)
    assert app.wait_for_portable_transcripts(10)
    assert sibling.read_bytes() == b"not ours"
    assert drive.is_dir() and (drive / "BabelFishR").is_dir()


def test_readiness_checks_the_selected_sessions_actual_destination(app, drive):
    conv = app.create_conversation("Field")
    app.select_conversation(conv.id)
    app.set_save_path(conv.id, str(drive))
    check = app.readiness(run_smoke_tests=False).get("Recording directory writable")
    assert check.status is CheckStatus.PASS
    assert str(drive / "BabelFishR") in check.detail

    shutil.rmtree(drive)
    report = app.readiness(run_smoke_tests=False)
    check = report.get("Recording directory writable")
    assert check.status is CheckStatus.FAIL
    assert check.detail == (f"Save path {drive / 'BabelFishR'}: "
                            f"not found (drive ejected or folder removed)")
    assert not report.can_record
    assert not drive.exists()
    # Another Session on the standard destination is unaffected.
    app.select_conversation(app.store.default_conversation().id)
    check = app.readiness(run_smoke_tests=False).get("Recording directory writable")
    assert check.status is CheckStatus.PASS


def test_default_export_dir_follows_the_save_path(app, drive):
    conv = app.create_conversation("Field")
    standard = app.default_export_dir(conv.id)
    assert standard in (pathlib.Path.home() / "Documents", pathlib.Path.home())
    app.set_save_path(conv.id, str(drive))
    assert app.default_export_dir(conv.id) == drive / "BabelFishR" / "exports"
    assert (drive / "BabelFishR" / "exports").is_dir()
    shutil.rmtree(drive)
    assert app.default_export_dir(conv.id) == standard
    assert not drive.exists()
