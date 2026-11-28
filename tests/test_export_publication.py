"""The last step of a standalone text or clip export: publication.

Inspector (Preview 13): a cancel during the final flush could still
replace the old file, and the staging file could be 0644 while the
destination was 0600. Director: text and clip saves replaced the chosen
name unconditionally, even when nothing was there when the operator
approved it, so a name claimed in between was replaced.

Now: staging is owner-only from creation; the intended mode is applied
only at publication; cancellation is checked after the flush, immediately
before publication; and only the very file the operator confirmed is
replaced - any other occupant of the name survives.
"""

from __future__ import annotations

import os
import pathlib
import stat

import pytest

from babelfishr import export
from test_export_safety import make_run


@pytest.fixture(autouse=True)
def umask_022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def leftovers(folder: pathlib.Path) -> list:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith("."))


class Flag:
    def __init__(self):
        self.set = False

    def __call__(self):
        return self.set


# -- text: both sides of publication -----------------------------------------
def test_cancel_during_the_final_flush_keeps_the_old_bytes(tmp_path, monkeypatch):
    target = tmp_path / "out.csv"
    target.write_text("old bytes")
    confirmed = export.confirmed_file(target)
    cancel = Flag()
    real_fsync = os.fsync

    def fsync_then_cancel(fd):
        real_fsync(fd)
        cancel.set = True              # pressed while the data was flushed

    monkeypatch.setattr(export.os, "fsync", fsync_then_cancel)
    with pytest.raises(export.ExportCancelled):
        export.write_text_export("new bytes", str(target), cancelled=cancel,
                                 confirmed=confirmed)
    assert target.read_text() == "old bytes"
    assert leftovers(tmp_path) == []


def test_cancel_after_publication_reports_the_published_file(tmp_path, monkeypatch):
    target = tmp_path / "out.csv"
    target.write_text("old bytes")
    confirmed = export.confirmed_file(target)
    cancel = Flag()
    real_replace = os.replace

    def replace_then_cancel(src, dst):
        real_replace(src, dst)
        cancel.set = True              # pressed just after publication

    monkeypatch.setattr(export.os, "replace", replace_then_cancel)
    written = export.write_text_export("new bytes", str(target), cancelled=cancel,
                                       confirmed=confirmed)
    assert written == str(target) and target.read_text() == "new bytes"
    assert leftovers(tmp_path) == []


# -- permissions ---------------------------------------------------------------
def _spy_staging_mode(monkeypatch, seen):
    real_fsync = os.fsync

    def fsync(fd):
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(export.os, "fsync", fsync)


def test_text_staging_is_owner_only_and_a_private_destination_stays_private(
        tmp_path, monkeypatch):
    target = tmp_path / "private.json"
    target.write_text("old")
    os.chmod(target, 0o600)
    seen = []
    _spy_staging_mode(monkeypatch, seen)
    export.write_text_export("{}", str(target),
                             confirmed=export.confirmed_file(target))
    assert seen == [0o600]
    assert mode(target) == 0o600 and target.read_text() == "{}"


def test_a_new_text_file_gets_the_ordinary_mode_only_at_publication(
        tmp_path, monkeypatch):
    seen = []
    _spy_staging_mode(monkeypatch, seen)
    written = pathlib.Path(export.write_text_export("x", str(tmp_path / "new.md")))
    assert seen == [0o600]             # while the data was written
    assert mode(written) == 0o644      # 0o666 less the umask, at publication
    assert leftovers(tmp_path) == []   # no staging, no mode probe


def test_clip_staging_is_owner_only_even_from_a_world_readable_recording(
        store, tmp_path, monkeypatch):
    (tx,) = make_run(store, tmp_path / "rec", ids=("tx_a",))
    os.chmod(tx.audio_path, 0o644)
    out = tmp_path / "out"
    out.mkdir()
    private = out / "clip.wav"
    private.write_bytes(b"older")
    os.chmod(private, 0o600)
    seen = []
    real_copyfile = export.shutil.copyfile

    def copyfile(src, dst, *a, **k):
        result = real_copyfile(src, dst, *a, **k)
        seen.append(mode(dst))
        return result

    monkeypatch.setattr(export.shutil, "copyfile", copyfile)
    export.export_transmission_audio(tx, str(private),
                                     confirmed=export.confirmed_file(private))
    assert seen == [0o600]
    assert mode(private) == 0o600
    assert private.read_bytes() == pathlib.Path(tx.audio_path).read_bytes()
    fresh = pathlib.Path(export.export_transmission_audio(tx, str(out / "new.wav")))
    assert mode(fresh) == 0o644
    assert leftovers(out) == []


def test_a_cancelled_clip_keeps_the_old_bytes(store, tmp_path, monkeypatch):
    (tx,) = make_run(store, tmp_path / "rec", ids=("tx_a",))
    out = tmp_path / "out"
    out.mkdir()
    chosen = out / "clip.wav"
    chosen.write_bytes(b"older")
    cancel = Flag()
    real_chmod = os.chmod

    def chmod_then_cancel(path, m, *a, **k):
        real_chmod(path, m, *a, **k)
        cancel.set = True              # after the final mode, before publication

    monkeypatch.setattr(export.os, "chmod", chmod_then_cancel)
    with pytest.raises(export.ExportCancelled):
        export.export_transmission_audio(tx, str(chosen), cancelled=cancel,
                                         confirmed=export.confirmed_file(chosen))
    assert chosen.read_bytes() == b"older"
    assert leftovers(out) == []


# -- late filename conflicts and confirmed overwrite -----------------------------
@pytest.mark.parametrize("kind", ["text", "clip"])
def test_a_name_claimed_after_the_dialog_survives(store, tmp_path, kind):
    (tx,) = make_run(store, tmp_path / "rec", ids=("tx_a",))
    out = tmp_path / "out"
    out.mkdir()
    chosen = out / ("export.csv" if kind == "text" else "clip.wav")
    confirmed = export.confirmed_file(chosen)          # nothing there: None
    assert confirmed is None
    chosen.write_bytes(b"claimed later")               # before the save runs
    if kind == "text":
        written = export.write_text_export("data", str(chosen), confirmed=confirmed)
    else:
        written = export.export_transmission_audio(tx, str(chosen),
                                                   confirmed=confirmed)
    assert chosen.read_bytes() == b"claimed later"
    assert pathlib.Path(written).name == chosen.stem + " (2)" + chosen.suffix
    assert leftovers(out) == []


@pytest.mark.parametrize("kind", ["text", "clip"])
def test_a_confirmed_file_swapped_for_another_is_not_replaced(store, tmp_path,
                                                              kind):
    (tx,) = make_run(store, tmp_path / "rec", ids=("tx_a",))
    out = tmp_path / "out"
    out.mkdir()
    chosen = out / ("export.csv" if kind == "text" else "clip.wav")
    chosen.write_bytes(b"confirmed")
    confirmed = export.confirmed_file(chosen)
    keep = out / "keep"
    keep.write_bytes(b"someone else's")
    os.replace(keep, chosen)                           # a different file now
    if kind == "text":
        written = export.write_text_export("data", str(chosen), confirmed=confirmed)
    else:
        written = export.export_transmission_audio(tx, str(chosen),
                                                   confirmed=confirmed)
    assert chosen.read_bytes() == b"someone else's"
    assert pathlib.Path(written) != chosen


@pytest.mark.parametrize("kind", ["text", "clip"])
def test_the_confirmed_file_is_still_replaced_in_place(store, tmp_path, kind):
    (tx,) = make_run(store, tmp_path / "rec", ids=("tx_a",))
    out = tmp_path / "out"
    out.mkdir()
    chosen = out / ("export.csv" if kind == "text" else "clip.wav")
    chosen.write_bytes(b"confirmed")
    os.chmod(chosen, 0o640)
    confirmed = export.confirmed_file(chosen)
    if kind == "text":
        written = export.write_text_export("data", str(chosen), confirmed=confirmed)
        assert chosen.read_text() == "data"
    else:
        written = export.export_transmission_audio(tx, str(chosen),
                                                   confirmed=confirmed)
        assert chosen.read_bytes() == pathlib.Path(tx.audio_path).read_bytes()
    assert written == str(chosen) and mode(chosen) == 0o640
    assert leftovers(out) == []


def test_cli_style_overwrite_replaces_the_file_present_at_the_start(tmp_path):
    target = tmp_path / "out.md"
    target.write_text("old")
    assert export.write_text_export("new", str(target), overwrite=True) == str(target)
    assert target.read_text() == "new"
    missing = tmp_path / "missing.md"
    assert export.write_text_export("x", str(missing), overwrite=True) == str(missing)
