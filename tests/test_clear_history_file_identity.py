"""Clear Session History judges "the same file" by the filesystem, not the name.

Mac acceptance of f6acbc7: on a case-insensitive APFS volume Receiver.wav
and receiver.wav are one directory entry, but the sharing check compared
resolved path strings. With two Sessions referencing the two spellings,
the preview said nothing was shared, and Audio files only or Both on one
Session deleted the other Session's playable recording and called it
complete.

Sharing now uses device and inode for every existing file. A missing or
unreadable file falls back to a loose key (resolved, Unicode-normalised,
case-folded), which can only make a file look *more* shared. Names are never
folded for an existing file, so on a case-sensitive volume Receiver.wav and
receiver.wav stay two files.

Hard links give the same identity on every platform, so they stand in for
case and Unicode aliases on Linux. The real case/Unicode alias test runs
wherever the temporary folder is case- or normalization-insensitive (the
macOS runner, a Mac) and is skipped elsewhere. Synthetic files only.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import unicodedata

import pytest

from babelfishr.models import (AnalysisArtifact, AnalysisAttempt, ProcessingState,
                               Session, Transmission)

T0 = dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.timezone.utc)
FIELDS = ("original", "processed", "decoded")


def _write(path: pathlib.Path, data=b"RIFF synthetic") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _message(store, run, offset, path, field="original", text="words"):
    tx = Transmission(session_id=run, started_at=T0 + dt.timedelta(seconds=offset),
                      duration=1.0, state=ProcessingState.COMPLETE, transcript=text)
    if field == "original":
        tx.audio_path = path
    elif field == "processed":
        tx.processed_audio_path = path
    else:
        tx.analysis_attempts = [AnalysisAttempt(
            transmission_id=tx.id, engine="synthetic",
            artifacts=[AnalysisArtifact(kind="decoded-audio", path=path)])]
    store.save_transmission(tx)
    return tx


@pytest.fixture
def two_tabs(app):
    store = app.store
    target = store.create_conversation("Target")
    keeper = store.create_conversation("Keeper")
    store.save_session(Session(id="t-run", conversation_id=target.id))
    store.save_session(Session(id="k-run", conversation_id=keeper.id))
    rec = pathlib.Path(app.config.recording.directory)
    rec.mkdir(parents=True, exist_ok=True)
    return app, store, target, keeper, rec


def _referenced_paths(tx):
    paths = [tx.audio_path, tx.processed_audio_path]
    for attempt in tx.analysis_attempts:
        paths += [a.path for a in attempt.artifacts]
    return [p for p in paths if p]


def _clear_and_check_kept(app, store, target, keeper_tx, kept_path, scope):
    """The preview calls the file shared; the clear keeps it, playable, and
    the keeper row still refers to it; preview and result agree."""
    inventory = app.history_inventory(target.id)
    assert len(inventory.shared) == 1, inventory
    assert inventory.owned == [], inventory
    report = app.clear_conversation_history(target.id, scope)
    assert report.complete
    assert report.removed == [], report.removed
    assert pathlib.Path(kept_path).read_bytes() == b"RIFF synthetic"
    assert kept_path in _referenced_paths(store.get_transmission(keeper_tx.id))
    assert len(report.kept_shared) == len(inventory.shared)


# --- one file under two names, in two Sessions ------------------------------------

@pytest.mark.parametrize("scope", ["audio", "both"])
@pytest.mark.parametrize("target_field", FIELDS)
@pytest.mark.parametrize("keeper_field", FIELDS)
def test_a_hard_link_in_another_session_is_kept(two_tabs, scope, target_field,
                                                keeper_field):
    app, store, target, keeper, rec = two_tabs
    original = _write(rec / "Receiver.wav")
    alias = rec / "elsewhere" / "receiver-link.wav"
    alias.parent.mkdir()
    os.link(original, alias)
    assert os.path.realpath(original) != os.path.realpath(alias), \
        "the two names resolve differently: only identity can tell"
    _message(store, "t-run", 0, original, target_field)
    kept = _message(store, "k-run", 5, str(alias), keeper_field)
    _clear_and_check_kept(app, store, target, kept, str(alias), scope)


def _case_insensitive(folder: pathlib.Path) -> bool:
    probe = folder / "CaseProbe.tmp"
    probe.write_bytes(b"")
    try:
        return (folder / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def _normalization_insensitive(folder: pathlib.Path) -> bool:
    name = "Empfänger-probe.tmp"
    probe = folder / unicodedata.normalize("NFC", name)
    probe.write_bytes(b"")
    try:
        return (folder / unicodedata.normalize("NFD", name)).exists()
    finally:
        probe.unlink()


@pytest.mark.parametrize("scope", ["audio", "both"])
@pytest.mark.parametrize("target_field", FIELDS)
def test_a_case_alias_in_another_session_is_kept(two_tabs, scope, target_field):
    """The reported Mac case, verbatim: Receiver.wav and receiver.wav."""
    app, store, target, keeper, rec = two_tabs
    if not _case_insensitive(rec):
        pytest.skip("this volume is case-sensitive; the hard-link test covers it")
    upper = _write(rec / "Receiver.wav")
    lower = str(rec / "receiver.wav")
    assert os.path.samefile(upper, lower)
    _message(store, "t-run", 0, upper, target_field)
    kept = _message(store, "k-run", 5, lower)
    _clear_and_check_kept(app, store, target, kept, lower, scope)


@pytest.mark.parametrize("scope", ["audio", "both"])
def test_a_unicode_alias_in_another_session_is_kept(two_tabs, scope):
    app, store, target, keeper, rec = two_tabs
    if not _normalization_insensitive(rec):
        pytest.skip("this volume keeps NFC and NFD names apart")
    name = "Empfänger.wav"
    nfc = _write(rec / unicodedata.normalize("NFC", name))
    nfd = str(rec / unicodedata.normalize("NFD", name))
    assert nfc != nfd and os.path.samefile(nfc, nfd)
    _message(store, "t-run", 0, nfc)
    kept = _message(store, "k-run", 5, nfd)
    _clear_and_check_kept(app, store, target, kept, nfd, scope)


# --- different files that only look alike are not protected -------------------------

@pytest.mark.parametrize("scope", ["audio", "both"])
def test_the_same_filename_in_two_folders_is_two_files(two_tabs, scope):
    app, store, target, keeper, rec = two_tabs
    mine = _write(rec / "a" / "receiver.wav", b"RIFF target")
    theirs = _write(rec / "b" / "receiver.wav")
    _message(store, "t-run", 0, mine)
    kept = _message(store, "k-run", 5, theirs)
    inventory = app.history_inventory(target.id)
    assert inventory.owned == [mine] and inventory.shared == []
    report = app.clear_conversation_history(target.id, scope)
    assert report.complete and report.removed == [mine]
    assert not pathlib.Path(mine).exists()
    assert pathlib.Path(theirs).exists()
    assert store.get_transmission(kept.id).audio_path == theirs


@pytest.mark.parametrize("scope", ["audio", "both"])
def test_names_differing_in_case_stay_two_files_on_a_case_sensitive_volume(two_tabs, scope):
    app, store, target, keeper, rec = two_tabs
    if _case_insensitive(rec):
        pytest.skip("this volume is case-insensitive; the alias test covers it")
    mine = _write(rec / "Receiver.wav", b"RIFF target")
    theirs = _write(rec / "receiver.wav")
    assert not os.path.samefile(mine, theirs)
    _message(store, "t-run", 0, mine)
    _message(store, "k-run", 5, theirs)
    inventory = app.history_inventory(target.id)
    assert inventory.owned == [mine] and inventory.shared == [], \
        "names are not case-folded for existing files"
    report = app.clear_conversation_history(target.id, scope)
    assert report.removed == [mine] and pathlib.Path(theirs).exists()


# --- two names of one file inside the Session being cleared ---------------------------

@pytest.mark.parametrize("scope", ["audio", "both"])
def test_two_names_inside_the_cleared_session_both_go(two_tabs, scope):
    app, store, target, keeper, rec = two_tabs
    first = _write(rec / "one.wav")
    second = rec / "two.wav"
    os.link(first, second)
    _message(store, "t-run", 0, first)
    _message(store, "t-run", 5, str(second), "processed")
    inventory = app.history_inventory(target.id)
    assert len(inventory.owned) == 1 and inventory.shared == [], \
        "one file, counted once, owned by the Session being cleared"
    report = app.clear_conversation_history(target.id, scope)
    assert report.complete
    assert not pathlib.Path(first).exists() and not second.exists(), \
        "no orphaned name is left in the Recordings folder"
    assert report.kept_shared == []


# --- the identity key itself -----------------------------------------------------------

def test_missing_files_fall_back_to_a_loose_key_that_only_widens_sharing(tmp_path):
    from babelfishr.storage import _file_keys

    a = str(tmp_path / "Gone.wav")
    b = str(tmp_path / "gone.wav")
    assert _file_keys(a) == _file_keys(b), "missing: matched loosely, never narrower"
    present = _write(tmp_path / "Here.wav")
    assert _file_keys(present) != _file_keys(str(tmp_path / "Gone.wav"))
    assert next(iter(_file_keys(present))).startswith("file:")
    hopped = f"{tmp_path}/no-such-folder/../Here.wav"
    assert _file_keys(hopped) == _file_keys(present)
