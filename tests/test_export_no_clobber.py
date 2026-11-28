"""Publishing an export never replaces anything, even under a race.

Inspector's source review of export._publish: the folder path checked the
name and then did an ordinary rename (on POSIX that silently replaces an
empty folder), and the no-hard-link file fallback checked the name and then
did a replacing rename. Between the check and the rename, another export
(or the operator) can take the name. Every name-taking step must now be
exclusive: an atomic no-replace rename where available, else a hard link,
an exclusive mkdir or an O_EXCL create of the name.

These tests make the race deterministic by letting the existence check
always answer "free" and creating the name just before the publish.
"""

from __future__ import annotations

import errno
import os
import pathlib
import threading

import pytest

from babelfishr import export
from test_export_safety import make_run, read_bundle, staging_left, BUNDLE_FILES


def _unsupported(*_a, **_k):
    raise export._Unsupported("simulated FAT/exFAT volume")


def _no_links(*_a, **_k):
    raise OSError(errno.EPERM, "Operation not permitted (no hard links)")


@pytest.fixture(params=["native", "fallback"])
def rename_path(request, monkeypatch):
    """Run each test with the OS no-replace rename and with the portable
    fallback a volume without it (or without hard links) gets."""
    if request.param == "fallback":
        monkeypatch.setattr(export, "_rename_noreplace", _unsupported)
        monkeypatch.setattr(export.os, "link", _no_links)
    return request.param


def _race(monkeypatch, make_taken):
    """The name check says free; the name is taken right after it."""
    real = os.path.lexists
    fired = []

    def lexists(path):
        if not fired:
            fired.append(path)
            make_taken(pathlib.Path(path))
            return False
        return real(path)

    monkeypatch.setattr(export.os.path, "lexists", lexists)
    return fired


def test_an_empty_folder_taken_after_the_check_is_not_replaced(
        store, tmp_path, monkeypatch, rename_path):
    make_run(store, tmp_path / "rec")
    out = tmp_path / "out"
    out.mkdir()
    _race(monkeypatch, lambda p: p.mkdir())
    path = pathlib.Path(export.export_session(store, "run1", str(out / "bundle")))
    assert path.name == "bundle (2)"
    assert (out / "bundle").is_dir() and list((out / "bundle").iterdir()) == []
    assert BUNDLE_FILES <= set(read_bundle(path))
    assert staging_left(out) == []


def test_a_zip_name_taken_after_the_check_is_not_replaced(
        store, tmp_path, monkeypatch, rename_path):
    make_run(store, tmp_path / "rec")
    out = tmp_path / "out"
    out.mkdir()
    _race(monkeypatch, lambda p: p.write_bytes(b"operator's own file"))
    path = pathlib.Path(export.export_session(store, "run1", str(out / "bundle"),
                                              archive=True))
    assert path.name == "bundle (2).zip"
    assert (out / "bundle.zip").read_bytes() == b"operator's own file"
    assert BUNDLE_FILES <= set(read_bundle(path))
    assert staging_left(out) == []


def test_a_clip_name_taken_after_the_check_is_not_replaced(
        store, tmp_path, monkeypatch, rename_path):
    tx = make_run(store, tmp_path / "rec")[0]
    out = tmp_path / "out"
    out.mkdir()
    _race(monkeypatch, lambda p: p.write_bytes(b"keep"))
    path = pathlib.Path(export.export_transmission_audio(tx, str(out / "clip.wav")))
    assert path.name == "clip (2).wav"
    assert (out / "clip.wav").read_bytes() == b"keep"
    assert path.read_bytes() == pathlib.Path(tx.audio_path).read_bytes()
    assert [p.name for p in out.iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize("archive", [False, True])
def test_concurrent_exports_each_get_their_own_complete_bundle(
        store, tmp_path, monkeypatch, rename_path, archive):
    make_run(store, tmp_path / "rec")
    out = tmp_path / "out"
    out.mkdir()
    (out / ("bundle.zip" if archive else "bundle")).mkdir()  # operator's
    marker = out / ("bundle.zip" if archive else "bundle") / "notes.txt"
    marker.write_text("operator")
    # Every export sees every name as free, so they all collide on the
    # same names and only the exclusive steps keep them apart.
    monkeypatch.setattr(export.os.path, "lexists", lambda _p: False)
    barrier = threading.Barrier(6)
    results, errors = [], []

    from babelfishr.storage import Store

    def run():
        own = Store(store.path, recordings_dir=str(store.recordings_dir))
        try:
            barrier.wait()
            results.append(export.export_session(own, "run1", str(out / "bundle"),
                                                 archive=archive))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert len(set(results)) == 6
    for result in results:
        files = read_bundle(pathlib.Path(result))
        assert BUNDLE_FILES <= set(files)
        assert {f for f in files if f.startswith("audio/")} == {
            "audio/tx_a.wav", "audio/tx_b.wav"}
    assert marker.read_text() == "operator"
    assert sorted(p.name for p in marker.parent.iterdir()) == ["notes.txt"]
    assert staging_left(out) == []


def test_a_failed_move_into_the_claimed_folder_is_undone(store, tmp_path,
                                                         monkeypatch):
    make_run(store, tmp_path / "rec")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(export, "_rename_noreplace", _unsupported)
    real = os.rename
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 3:
            raise OSError(errno.EIO, "Input/output error")
        return real(src, dst)

    monkeypatch.setattr(export.os, "rename", flaky)
    with pytest.raises(OSError):
        export.export_session(store, "run1", str(out / "bundle"))
    assert list(out.iterdir()) == []          # claimed folder removed, staging too
    monkeypatch.setattr(export.os, "rename", real)
    path = export.export_session(store, "run1", str(out / "bundle"))
    assert pathlib.Path(path).name == "bundle"


def test_the_native_rename_refuses_an_existing_empty_folder(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "a").write_text("a")
    dst.mkdir()
    try:
        export._rename_noreplace(src, dst)
    except export._Unsupported:
        pytest.skip("no atomic no-replace rename on this OS or volume")
    except FileExistsError:
        pass
    else:
        pytest.fail("an existing folder was replaced")
    assert list(dst.iterdir()) == [] and (src / "a").exists()


def test_publish_never_uses_a_replacing_rename_onto_the_final_name(
        store, tmp_path, monkeypatch, rename_path):
    """Source-level guard: os.replace / os.rename may only target names
    this export created (its staging or a folder it just claimed)."""
    make_run(store, tmp_path / "rec")
    out = tmp_path / "out"
    out.mkdir()
    targets = []
    for name in ("rename", "replace"):
        real = getattr(os, name)

        def spy(src, dst, _real=real):
            targets.append(pathlib.Path(dst))
            return _real(src, dst)

        monkeypatch.setattr(export.os, name, spy)
    for archive in (False, True):
        path = pathlib.Path(export.export_session(store, "run1", str(out / "b"),
                                                  archive=archive))
        for target in targets:
            # Only entries inside the claimed folder, or (fallback ZIP) the
            # O_EXCL placeholder this export created.
            assert target.parent == path or (archive and target == path), target
        targets.clear()
