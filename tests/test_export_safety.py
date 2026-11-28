"""Session export never touches what it did not create.

A bundle is built in a private, hidden staging folder beside the requested
location and renamed into place whole. An occupied folder or ZIP name is
left exactly as it was and the bundle goes to "name (2)", whose path the
CLI prints and the window shows. JSON, CSV and Markdown are written from one
exported-audio mapping: included audio has its bundle-relative path, excluded
or missing audio has none (JSON null, CSV empty, no Markdown Audio line).
Odd ids cannot escape audio/ and never collide. A failure or a cancel part
way leaves no staging folder and no partial bundle, and a retry works. The
operator's save folder (a removable volume) never travels in a bundle.
"""

from __future__ import annotations

import csv
import datetime as dt
import errno
import io
import json
import os
import pathlib
import shutil
import zipfile

import pytest

from babelfishr import export
from babelfishr.models import ProcessingState, Session, Transmission

T0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
BUNDLE_FILES = {"session.json", "transcript.md", "transmissions.csv", "README.txt"}


def snapshot(root: pathlib.Path) -> dict:
    """Every path under *root* with its bytes (None for folders)."""
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def staging_left(parent: pathlib.Path) -> list:
    return [p.name for p in parent.iterdir() if p.name.startswith(".babelfishr-")]


def make_run(store, recordings: pathlib.Path, ids=("tx_a", "tx_b"),
             session_id="run1", conversation_id="", names=None):
    recordings.mkdir(parents=True, exist_ok=True)
    store.save_session(Session(id=session_id, conversation_id=conversation_id,
                               started_at=T0))
    txs = []
    for n, tx_id in enumerate(ids):
        name = (names or {}).get(tx_id, f"rec{n}.wav")
        wav = recordings / f"{n}" / name
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF" + tx_id.encode() + bytes(64))
        tx = Transmission(id=tx_id, session_id=session_id,
                          started_at=T0 + dt.timedelta(seconds=10 * n),
                          duration=2.0, state=ProcessingState.COMPLETE,
                          transcript=f"message {n}", audio_path=str(wav))
        store.save_transmission(tx)
        txs.append(tx)
    return txs


def read_bundle(path: pathlib.Path) -> dict:
    """{relative name: bytes} of a bundle folder or ZIP."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            return {name: zf.read(name) for name in zf.namelist()}
    return {str(p.relative_to(path).as_posix()): p.read_bytes()
            for p in path.rglob("*") if p.is_file()}


def csv_rows(files: dict) -> list:
    return list(csv.DictReader(io.StringIO(files["transmissions.csv"].decode())))


def run_cli(config, tmp_path, *argv):
    from babelfishr.cli import main

    settings = tmp_path / "cli.toml"
    config.save(str(settings))
    return main(["--config", str(settings), "export", "--format", "bundle", *argv])


# -- CLI: folder and --zip ----------------------------------------------------
def test_cli_folder_export_into_an_occupied_folder_goes_to_a_sibling(
        config, store, tmp_path, capsys):
    make_run(store, tmp_path / "rec")
    dest = tmp_path / "out" / "bundle"
    (dest / "audio").mkdir(parents=True)
    (dest / "operator notes.txt").write_text("keep me")
    (dest / "audio" / "old.wav").write_bytes(b"old")
    before = snapshot(dest)

    assert run_cli(config, tmp_path, "--session", "run1", "--output", str(dest)) == 0
    out = capsys.readouterr().out
    actual = tmp_path / "out" / "bundle (2)"
    assert f"Exported bundle to {actual}" in out
    assert snapshot(dest) == before, "the existing folder was changed"
    files = read_bundle(actual)
    assert set(files) == BUNDLE_FILES | {"audio/tx_a.wav", "audio/tx_b.wav"}
    assert staging_left(dest.parent) == []


def test_cli_zip_never_packages_or_deletes_an_existing_folder(
        config, store, tmp_path, capsys):
    make_run(store, tmp_path / "rec")
    dest = tmp_path / "out" / "bundle"
    dest.mkdir(parents=True)
    (dest / "private.txt").write_text("operator file")
    before = snapshot(dest)

    assert run_cli(config, tmp_path, "--session", "run1", "--output", str(dest),
                   "--zip") == 0
    zip_path = tmp_path / "out" / "bundle.zip"
    assert f"Exported bundle to {zip_path}" in capsys.readouterr().out
    assert snapshot(dest) == before, "the folder of the same name was touched"
    files = read_bundle(zip_path)
    assert set(files) == BUNDLE_FILES | {"audio/tx_a.wav", "audio/tx_b.wav"}
    assert staging_left(dest.parent) == []


def test_cli_zip_into_an_occupied_zip_name_goes_to_a_sibling(
        config, store, tmp_path, capsys):
    make_run(store, tmp_path / "rec")
    out = tmp_path / "out"
    out.mkdir()
    (out / "bundle.zip").write_bytes(b"not ours")
    assert run_cli(config, tmp_path, "--session", "run1", "--output",
                   str(out / "bundle.zip"), "--zip") == 0
    actual = out / "bundle (2).zip"
    assert f"Exported bundle to {actual}" in capsys.readouterr().out
    assert (out / "bundle.zip").read_bytes() == b"not ours"
    assert BUNDLE_FILES <= set(read_bundle(actual))
    assert sorted(p.name for p in out.iterdir()) == ["bundle (2).zip", "bundle.zip"]


def test_cli_export_failure_reports_and_leaves_nothing(config, store, tmp_path,
                                                       capsys, monkeypatch):
    make_run(store, tmp_path / "rec")
    out = tmp_path / "out"
    out.mkdir()

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export.shutil, "copy2", full)
    assert run_cli(config, tmp_path, "--session", "run1", "--output",
                   str(out / "bundle")) == 1
    assert "nothing was written" in capsys.readouterr().err
    assert list(out.iterdir()) == []


# -- one exported-audio mapping for JSON, CSV and Markdown ---------------------
@pytest.mark.parametrize("archive", [False, True])
def test_a_no_audio_bundle_has_no_audio_paths_anywhere(store, tmp_path, archive):
    txs = make_run(store, tmp_path / "rec")
    path = pathlib.Path(export.export_session(
        store, "run1", str(tmp_path / "b"), include_audio=False, archive=archive))
    files = read_bundle(path)
    assert set(files) == BUNDLE_FILES, "no audio folder content without audio"
    manifest = json.loads(files["session.json"])
    assert [e["audio_path"] for e in manifest["transmissions"]] == [None, None]
    assert manifest["audio_files"] == {}
    assert [r["audio_path"] for r in csv_rows(files)] == ["", ""]
    assert "**Audio:**" not in files["transcript.md"].decode()
    for tx in txs:
        for content in files.values():
            assert tx.audio_path.encode() not in content
            assert pathlib.Path(tx.audio_path).name.encode() not in content


def test_markdown_csv_and_json_follow_the_exported_audio(store, tmp_path):
    txs = make_run(store, tmp_path / "rec", ids=("tx_a", "tx_b", "tx_gone"))
    pathlib.Path(txs[2].audio_path).unlink()  # removed outside BabelFishR
    files = read_bundle(pathlib.Path(export.export_session(
        store, "run1", str(tmp_path / "b"))))
    manifest = json.loads(files["session.json"])
    by_id = {e["id"]: e["audio_path"] for e in manifest["transmissions"]}
    assert by_id == {"tx_a": "audio/tx_a.wav", "tx_b": "audio/tx_b.wav",
                     "tx_gone": None}
    assert {r["id"]: r["audio_path"] for r in csv_rows(files)} == {
        "tx_a": "audio/tx_a.wav", "tx_b": "audio/tx_b.wav", "tx_gone": ""}
    md = files["transcript.md"].decode()
    assert "**Audio:** `audio/tx_a.wav`" in md
    assert "**Audio:** `audio/tx_b.wav`" in md
    assert md.count("**Audio:**") == 2, "missing audio is not named"
    assert "rec2.wav" not in md


def test_ambiguous_names_are_collision_free_and_stay_inside_the_bundle(store,
                                                                       tmp_path):
    ids = ("../../escape", "a/b", "a_b", "A_B", "..")
    make_run(store, tmp_path / "rec", ids=ids,
             names={i: "same.wav" for i in ids})
    out = tmp_path / "out"
    path = pathlib.Path(export.export_session(store, "run1", str(out / "b")))
    files = read_bundle(path)
    audio = sorted(n for n in files if n.startswith("audio/"))
    assert len(audio) == len(ids)
    assert len({n.lower() for n in audio}) == len(ids), "names collide"
    assert all("/" not in n[len("audio/"):] and not n[len("audio/"):].startswith(".")
               for n in audio)
    manifest = json.loads(files["session.json"])
    mapped = [e["audio_path"] for e in manifest["transmissions"]]
    assert sorted(mapped) == audio
    assert sorted(r["audio_path"] for r in csv_rows(files)) == audio
    # Each mapped file holds that transmission's own recording.
    for entry in manifest["transmissions"]:
        assert entry["id"].encode() in files[entry["audio_path"]]
    # Nothing written outside the bundle.
    assert sorted(p.name for p in out.iterdir()) == ["b"]
    assert [q for q in tmp_path.rglob("escape*") if path not in q.parents] == []


# -- failure, cancel and retry --------------------------------------------------
@pytest.mark.parametrize("archive", [False, True])
def test_a_copy_failure_part_way_leaves_nothing_and_a_retry_works(
        store, tmp_path, monkeypatch, archive):
    make_run(store, tmp_path / "rec", ids=("tx_a", "tx_b", "tx_c"))
    out = tmp_path / "out"
    dest = out / "bundle"
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("existing")
    previous = pathlib.Path(export.export_session(store, "run1", str(out / "earlier"),
                                                  archive=archive))
    before = snapshot(out)

    real_copy = shutil.copy2
    calls = []

    def flaky(src, dst, *a, **k):
        calls.append(src)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(export.shutil, "copy2", flaky)
    with pytest.raises(OSError):
        export.export_session(store, "run1", str(dest), archive=archive)
    assert snapshot(out) == before, "a failed export changed something"
    assert staging_left(out) == []

    monkeypatch.setattr(export.shutil, "copy2", real_copy)
    result = pathlib.Path(export.export_session(store, "run1", str(dest),
                                                archive=archive))
    assert result.name == ("bundle.zip" if archive else "bundle (2)")
    assert len([n for n in read_bundle(result) if n.startswith("audio/")]) == 3
    assert (dest / "keep.txt").read_text() == "existing"
    assert read_bundle(previous), "the earlier export is intact"
    assert staging_left(out) == []


@pytest.mark.parametrize("archive", [False, True])
def test_a_failure_to_publish_leaves_nothing(store, tmp_path, monkeypatch, archive):
    make_run(store, tmp_path / "rec")
    out = tmp_path / "out"
    out.mkdir()

    def denied(*a, **k):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(export.os, "rename", denied)
    monkeypatch.setattr(export.os, "replace", denied)
    monkeypatch.setattr(export.os, "link", denied)
    with pytest.raises(PermissionError):
        export.export_session(store, "run1", str(out / "bundle"), archive=archive)
    assert list(out.iterdir()) == []


def test_cancelling_leaves_nothing(store, tmp_path):
    make_run(store, tmp_path / "rec", ids=("tx_a", "tx_b", "tx_c"))
    out = tmp_path / "out"
    out.mkdir()
    checks = []

    def cancelled():
        checks.append(1)
        return len(checks) >= 3  # after the first recording is copied

    with pytest.raises(export.ExportCancelled):
        export.export_session(store, "run1", str(out / "bundle"),
                              cancelled=cancelled)
    assert list(out.iterdir()) == []


# -- one clip -------------------------------------------------------------------
def test_clip_export_never_silently_replaces_a_file(store, tmp_path, monkeypatch):
    (tx,) = make_run(store, tmp_path / "rec", ids=("tx_a",))
    out = tmp_path / "out"
    out.mkdir()
    (out / "tx_a.wav").write_bytes(b"operator file")
    written = pathlib.Path(export.export_transmission_audio(tx, str(out)))
    assert written == out / "tx_a (2).wav"
    assert (out / "tx_a.wav").read_bytes() == b"operator file"
    assert written.read_bytes() == pathlib.Path(tx.audio_path).read_bytes()

    named = out / "chosen.wav"
    named.write_bytes(b"older")
    again = pathlib.Path(export.export_transmission_audio(tx, str(named)))
    assert again == out / "chosen (2).wav" and named.read_bytes() == b"older"

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export.shutil, "copy2", full)
    with pytest.raises(OSError):
        export.export_transmission_audio(tx, str(named), overwrite=True)
    assert named.read_bytes() == b"older", "a failed copy damaged the file"
    assert staging_left(out) == []


# -- the operator's save folder stays on this machine -----------------------------
def test_the_save_path_never_travels_in_a_bundle(app, fixture_wav, tmp_path):
    drive = tmp_path / "Volumes" / "FIELD DRIVE"
    drive.mkdir(parents=True)
    app.set_save_path(app.conversation_id, str(drive))
    app.select_engines()
    session = app.start_session(replay_path=fixture_wav, name="field")
    app.run_replay()
    app.stop_session()
    stored = app.store.get_session(session.id).save_path
    assert stored and str(drive) in stored, "the run did not use the save path"
    assert any(str(drive) in (t.audio_path or "") for t in app.transmissions())

    secrets = {str(drive), stored}
    for archive in (False, True):
        path = pathlib.Path(export.export_session(
            app.store, session.id, str(tmp_path / "out" / "b"), archive=archive,
            conversation_id=app.conversation_id))
        files = read_bundle(path)
        assert any(n.startswith("audio/") for n in files)
        for name, content in files.items():
            text = content.decode("utf-8", "replace")
            for secret in secrets:
                assert secret not in text, f"{secret} leaked into {name}"
                assert json.dumps(secret)[1:-1] not in text
        manifest = json.loads(files["session.json"])
        assert manifest["session"]["save_path"] == ""
    assert app.store.get_session(session.id).save_path == stored
    assert app.store.get_conversation(app.conversation_id).save_path


# -- the window ------------------------------------------------------------------
@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(qt_app, config, store, tmp_path):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    app = BabelFishRApp(config=config, store=store)
    win = MainWindow(app)
    yield win
    win.close()
    app.close()


def test_window_reexport_after_a_removal_is_a_fresh_exact_bundle(
        window, store, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    make_run(store, tmp_path / "rec", conversation_id=window.app.conversation_id)
    out = tmp_path / "exports"
    out.mkdir()
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(out)))
    shown = []
    monkeypatch.setattr(window.status, "showMessage",
                        lambda text, *a: shown.append(text))

    window._export_session()
    (first,) = [p for p in out.iterdir()]
    assert shown[-1] == f"Exported session bundle to {first}"
    first_before = snapshot(first)
    assert (first / "audio" / "tx_b.wav").exists()

    store.hide_transmission("tx_b")
    window._export_session()
    second = out / f"{first.name} (2)"
    assert shown[-1] == f"Exported session bundle to {second}"
    assert snapshot(first) == first_before, "the earlier bundle was changed"
    assert sorted(p.name for p in (second / "audio").iterdir()) == ["tx_a.wav"]
    manifest = json.loads((second / "session.json").read_text())
    assert [e["id"] for e in manifest["transmissions"]] == ["tx_a"]
    assert staging_left(out) == []


def test_window_export_failure_warns_and_leaves_nothing(window, store, tmp_path,
                                                        monkeypatch):
    from PySide6 import QtWidgets

    make_run(store, tmp_path / "rec", conversation_id=window.app.conversation_id)
    out = tmp_path / "exports"
    out.mkdir()
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(out)))
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a[2])))

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export.shutil, "copy2", full)
    window._export_session()
    assert warned and "could not be exported" in warned[0]
    assert list(out.iterdir()) == []


def test_window_clip_export_replaces_only_a_confirmed_file(window, store, tmp_path,
                                                          monkeypatch):
    from PySide6 import QtWidgets

    (tx,) = make_run(store, tmp_path / "rec", ids=("tx_a",),
                     conversation_id=window.app.conversation_id)
    chosen = tmp_path / "chosen.wav"
    chosen.write_bytes(b"older")
    # The save dialog's own overwrite confirmation was answered yes.
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(chosen), "")))
    shown = []
    monkeypatch.setattr(window.status, "showMessage",
                        lambda text, *a: shown.append(text))
    window._on_export_clip(tx.id)
    assert chosen.read_bytes() == pathlib.Path(tx.audio_path).read_bytes()
    assert shown[-1] == f"Exported {chosen}"
    assert staging_left(tmp_path) == []
