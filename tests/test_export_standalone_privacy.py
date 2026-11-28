"""Standalone Markdown/JSON/CSV exports get the bundle's portable projection.

Inspector finding: File > Export transcript/data and ``babelfishr export
--format md|json|csv`` (file or stdout) rendered the raw rows, so the
Session's save folder, the absolute recording and processed-audio paths, the
operator's home folder and path-bearing diagnostics (save-path fallback
metadata, analysis inputs/artifacts/stderr, error messages) travelled in a
file meant to be shared. Bundles already hid them.

Now every route uses export.portable_projection: a standalone file names a
recording by its plain file name (it has no audio folder), a bundle keeps its
bundle-relative ``audio/<file>``; diagnostics that are a path say
"[local folder]"; operator text is kept word for word, except that a literal
local folder path in it reads "[local folder]". Files are written through a
private temporary file and published: the window replaces the file its save
dialog confirmed, the CLI never replaces one unless --overwrite is given (an
occupied name gets "name (2)"). A failure leaves the old file and no
temporary file, with a warning box or a stderr error and exit status 1.
"""

from __future__ import annotations

import csv
import datetime as dt
import errno
import io
import json
import os
import pathlib
import tempfile

import pytest

from babelfishr import export
from babelfishr.models import (AnalysisArtifact, AnalysisAttempt, AnalysisOutcome,
                               ErrorInfo, ProcessingState, Session, Transmission)

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


T0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
TRANSCRIPT = "Mayday relay, vessel Blue Heron, position five two north"
TRANSLATION = "Mayday relais, Schiff Blue Heron, Position fünf zwei Nord"
NOTE_WORDS = "Coast guard acknowledged; folder of evidence is"

@pytest.fixture
def operator(tmp_path, monkeypatch):
    """A fake home folder holding the recordings, a separate save-path
    volume and an analysis work folder; the system temp folder is moved out
    of the way so only the export's own redaction can hide these."""
    home = tmp_path / "Users" / "opuser"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    drive = tmp_path / "Volumes" / "FIELD DRIVE"
    recordings = home / "BabelFishR" / "Recordings"
    work = tmp_path / "work" / "analysis-7"
    for folder in (drive / "BabelFishR" / "run1", recordings, work):
        folder.mkdir(parents=True)
    return {"home": home, "drive": drive, "recordings": recordings, "work": work}


def secrets_of(operator) -> list:
    values = [str(v) for v in operator.values()]
    values += [json.dumps(v)[1:-1] for v in values]
    return values + ["opuser", "FIELD DRIVE", "analysis-7"]


def assert_private(text: str, operator, where: str = "") -> None:
    for secret in secrets_of(operator):
        assert secret not in text, f"{secret!r} leaked {where}"


def make_rich_run(store, operator, conversation_id="", session_id="run1"):
    """One complete transmission with every kind of path-bearing field, one
    failed transmission whose error names its recording."""
    drive, recordings, work = operator["drive"], operator["recordings"], operator["work"]
    store.save_session(Session(
        id=session_id, conversation_id=conversation_id, started_at=T0,
        save_path=str(drive), name="harbour watch",
        notes=f"Saved on {drive}/BabelFishR during the storm"))
    audio = drive / "BabelFishR" / "run1" / "tx_a.wav"
    audio.write_bytes(b"RIFF" + bytes(64))
    processed = recordings / "tx_a.processed.wav"
    processed.write_bytes(b"RIFF" + bytes(64))
    decoded = work / "decoded.wav"
    tx = Transmission(
        id="tx_a", session_id=session_id, started_at=T0, duration=3.0,
        state=ProcessingState.COMPLETE, transcript=TRANSCRIPT,
        translation=TRANSLATION, source_language="en", target_language="de",
        audio_path=str(audio), processed_audio_path=str(processed),
        notes=f"{NOTE_WORDS} {operator['home']}/Evidence/heron",
        signal_metadata={
            "save_path_fallback": {
                "chosen": str(drive), "intended": str(drive / "BabelFishR"),
                "used": str(recordings), "reason": "the drive was removed"},
            "portable_transcript": {"state": "failed",
                                    "path": str(drive / "BabelFishR" / "run1"
                                                / "tx_a.json"),
                                    "reason": f"could not write {drive}"},
        },
        analysis_attempts=[AnalysisAttempt(
            transmission_id="tx_a", engine="dsd-neo",
            input_path=str(work / "derived.wav"), input_is_derived=True,
            command=["dsd-neo", "-i", str(work / "derived.wav"), "-w",
                     str(decoded)],
            options={"log_dir": str(work)},
            outcome=AnalysisOutcome.PROTOCOL_IDENTIFIED, protocol="DMR",
            metadata={"talkgroup": "2501", "wav": str(decoded)},
            artifacts=[AnalysisArtifact("decoded-audio", str(decoded)),
                       AnalysisArtifact("log", str(work / "dsd.log"),
                                        f"log in {work}")],
            stdout="sync", stderr=f"reading {work / 'derived.wav'}",
            error="")])
    store.save_transmission(tx)
    failed = Transmission(
        id="tx_b", session_id=session_id, started_at=T0 + dt.timedelta(seconds=9),
        duration=1.0, state=ProcessingState.FAILED,
        audio_path=str(drive / "BabelFishR" / "run1" / "tx_b.wav"),
        error=ErrorInfo(stage="transcription",
                        message=f"could not read {drive}/BabelFishR/run1/tx_b.wav"))
    store.save_transmission(failed)
    return tx, failed


def check_standalone(fmt: str, text: str, operator) -> None:
    """Projected, with the transcript kept and plain audio file names."""
    assert_private(text, operator, f"into the {fmt} export")
    if fmt == "json":
        data = json.loads(text)
        by_id = {t["id"]: t for t in data["transmissions"]}
        a = by_id["tx_a"]
        assert a["transcript"] == TRANSCRIPT and a["translation"] == TRANSLATION
        assert a["audio_path"] == "tx_a.wav"
        assert a["processed_audio_path"] is None
        assert a["notes"] == f"{NOTE_WORDS} {export.LOCAL_FOLDER}/Evidence/heron"
        fallback = a["signal_metadata"]["save_path_fallback"]
        assert fallback == {"chosen": export.LOCAL_FOLDER,
                            "intended": export.LOCAL_FOLDER,
                            "used": export.LOCAL_FOLDER,
                            "reason": "the drive was removed"}
        attempt = a["analysis_attempts"][0]
        assert attempt["input_path"] == "derived.wav"
        assert [x["path"] for x in attempt["artifacts"]] == ["decoded.wav", "dsd.log"]
        assert attempt["command"] == ["dsd-neo", "-i", "derived.wav", "-w",
                                      "decoded.wav"]
        assert attempt["metadata"] == {"talkgroup": "2501",
                                       "wav": export.LOCAL_FOLDER}
        assert by_id["tx_b"]["audio_path"] == "tx_b.wav"
        assert "tx_b.wav" in by_id["tx_b"]["error"]["message"]
        assert data["session"]["save_path"] == ""
        assert data["session"]["notes"].startswith(
            f"Saved on {export.LOCAL_FOLDER}")
    elif fmt == "csv":
        rows = {r["id"]: r for r in csv.DictReader(io.StringIO(text))}
        assert rows["tx_a"]["transcript"] == TRANSCRIPT
        assert rows["tx_a"]["translation"] == TRANSLATION
        assert rows["tx_a"]["audio_path"] == "tx_a.wav"
        assert rows["tx_b"]["audio_path"] == "tx_b.wav"
        assert rows["tx_a"]["notes"].startswith(NOTE_WORDS)
    else:
        assert TRANSCRIPT in text and TRANSLATION in text
        assert "**Audio:** `tx_a.wav`" in text
        assert f"**Notes:** {NOTE_WORDS} {export.LOCAL_FOLDER}/Evidence/heron" in text
        assert "**Error (transcription):** could not read" in text


def temp_left(folder: pathlib.Path) -> list:
    return [p.name for p in folder.iterdir() if p.name.startswith(".babelfishr-")]


def run_cli(config, tmp_path, *argv):
    from babelfishr.cli import main

    settings = tmp_path / "cli.toml"
    config.save(str(settings))
    return main(["--config", str(settings), "export", *argv])


FORMATS = ["md", "json", "csv"]


# -- CLI ------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", FORMATS)
def test_cli_stdout_is_projected(config, store, tmp_path, operator, capsys, fmt):
    make_rich_run(store, operator)
    assert run_cli(config, tmp_path, "--session", "run1", "--format", fmt) == 0
    check_standalone(fmt, capsys.readouterr().out, operator)


@pytest.mark.parametrize("fmt", FORMATS)
def test_cli_output_file_is_projected(config, store, tmp_path, operator, capsys,
                                      fmt):
    make_rich_run(store, operator)
    out = tmp_path / "out"
    out.mkdir()
    target = out / f"log.{fmt}"
    assert run_cli(config, tmp_path, "--session", "run1", "--format", fmt,
                   "--output", str(target)) == 0
    assert f"Exported 2 transmission(s) to {target}" in capsys.readouterr().out
    check_standalone(fmt, target.read_text(encoding="utf-8"), operator)
    assert temp_left(out) == []


def test_cli_output_never_replaces_a_file_without_overwrite(config, store, tmp_path,
                                                            operator, capsys):
    make_rich_run(store, operator)
    out = tmp_path / "out"
    out.mkdir()
    target = out / "log.md"
    target.write_text("operator's own log")
    assert run_cli(config, tmp_path, "--session", "run1", "--output",
                   str(target)) == 0
    sibling = out / "log (2).md"
    assert f"to {sibling}" in capsys.readouterr().out
    assert target.read_text() == "operator's own log"
    assert TRANSCRIPT in sibling.read_text(encoding="utf-8")

    assert run_cli(config, tmp_path, "--session", "run1", "--output",
                   str(target), "--overwrite") == 0
    assert f"to {target}" in capsys.readouterr().out
    check_standalone("md", target.read_text(encoding="utf-8"), operator)
    assert sorted(p.name for p in out.iterdir()) == ["log (2).md", "log.md"]


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("fmt", FORMATS)
def test_cli_write_failure_keeps_the_old_file(config, store, tmp_path, operator,
                                              capsys, monkeypatch, fmt, overwrite):
    make_rich_run(store, operator)
    out = tmp_path / "out"
    out.mkdir()
    target = out / f"log.{fmt}"
    target.write_text("previous export")

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export.os, "fsync", full)
    argv = ["--session", "run1", "--format", fmt, "--output", str(target)]
    assert run_cli(config, tmp_path, *argv, *(["--overwrite"] if overwrite
                                              else [])) == 1
    err = capsys.readouterr().err
    assert "Export failed" in err and "No space left on device" in err
    assert str(target) in err
    assert target.read_text() == "previous export"
    assert sorted(p.name for p in out.iterdir()) == [target.name]


def test_cli_replace_denied_keeps_the_old_file(config, store, tmp_path, operator,
                                               capsys, monkeypatch):
    make_rich_run(store, operator)
    out = tmp_path / "out"
    out.mkdir()
    target = out / "log.csv"
    target.write_text("previous export")

    real_replace = os.replace

    def denied(src, dst, *a, **k):
        if pathlib.Path(dst) == target:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(export.os, "replace", denied)
    assert run_cli(config, tmp_path, "--session", "run1", "--format", "csv",
                   "--output", str(target), "--overwrite") == 1
    assert "Permission denied" in capsys.readouterr().err
    assert target.read_text() == "previous export"
    assert temp_left(out) == []


def test_cli_help_documents_the_no_clobber_rule(capsys):
    from babelfishr.cli import main

    with pytest.raises(SystemExit):
        main(["export", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--overwrite" in text and "name (2)" in text


# -- legacy rows -----------------------------------------------------------------
@pytest.mark.parametrize("fmt", FORMATS)
def test_legacy_session_with_deleted_audio_exports(config, store, tmp_path,
                                                   operator, capsys, fmt):
    """A Session saved before save paths/Sessions existed (those columns
    empty) whose recordings were deleted: a no-audio standalone export."""
    recordings = operator["recordings"]
    store.save_session(Session(id="old", started_at=T0))
    store._conn.execute("UPDATE sessions SET save_path = NULL, "
                        "conversation_id = NULL WHERE id = 'old'")
    store._conn.commit()
    legacy = Transmission.from_dict({
        "id": "tx_old", "session_id": "old", "started_at": "2024-03-01T10:00:00Z",
        "duration": 2.5, "state": "complete", "transcript": "Old message kept",
        "audio_path": str(recordings / "gone" / "tx_old.wav")})
    store.save_transmission(legacy)
    assert not pathlib.Path(legacy.audio_path).exists()

    target = tmp_path / f"old.{fmt}"
    assert run_cli(config, tmp_path, "--session", "old", "--format", fmt,
                   "--output", str(target)) == 0
    text = target.read_text(encoding="utf-8")
    assert "Old message kept" in text
    assert_private(text, operator)
    # No audio travels; the record still names the recording it came from.
    assert "tx_old.wav" in text
    assert run_cli(config, tmp_path, "--session", "old", "--format", fmt) == 0
    stdout = capsys.readouterr().out
    assert "Old message kept" in stdout
    assert_private(stdout, operator)


def test_projection_handles_windows_paths_and_missing_fields(store):
    tx = Transmission(id="w", audio_path="C:\\Users\\opuser\\Rec\\w.wav",
                      signal_metadata={"save_path_fallback": {
                          "chosen": "E:\\FIELD DRIVE", "reason": "removed"}})
    text = export.portable_text("json", store, [tx], None)
    assert "opuser" not in text and "FIELD DRIVE" not in text
    entry = json.loads(text)["transmissions"][0]
    assert entry["audio_path"] == "w.wav"
    assert entry["signal_metadata"]["save_path_fallback"]["chosen"] == \
        export.LOCAL_FOLDER
    bare = Transmission(id="n", audio_path=None)
    assert json.loads(export.portable_text("json", store, [bare]))[
        "transmissions"][0]["audio_path"] is None
    row = next(csv.DictReader(io.StringIO(export.portable_text("csv", store,
                                                               [bare]))))
    assert row["audio_path"] == ""
    assert "**Audio:**" not in export.portable_text("md", store, [bare])


# -- bundles keep their own references --------------------------------------------
@pytest.mark.parametrize("archive", [False, True])
def test_bundle_keeps_bundle_relative_audio_and_hides_diagnostics(
        store, tmp_path, operator, archive):
    from test_export_safety import read_bundle

    make_rich_run(store, operator)
    path = pathlib.Path(export.export_session(store, "run1", str(tmp_path / "b"),
                                              archive=archive))
    files = read_bundle(path)
    for name, content in files.items():
        if not name.startswith("audio/"):
            assert_private(content.decode("utf-8"), operator, f"into {name}")
    manifest = json.loads(files["session.json"])
    by_id = {e["id"]: e for e in manifest["transmissions"]}
    assert by_id["tx_a"]["audio_path"] == "audio/tx_a.wav"
    assert by_id["tx_b"]["audio_path"] is None       # its recording is missing
    assert by_id["tx_a"]["processed_audio_path"] is None
    assert by_id["tx_a"]["transcript"] == TRANSCRIPT
    assert by_id["tx_a"]["analysis_attempts"][0]["input_path"] == "derived.wav"
    assert manifest["audio_files"] == {"tx_a": "audio/tx_a.wav"}
    md = files["transcript.md"].decode()
    assert "**Audio:** `audio/tx_a.wav`" in md and md.count("**Audio:**") == 1
    assert TRANSCRIPT in md
    rows = {r["id"]: r for r in csv.DictReader(io.StringIO(
        files["transmissions.csv"].decode()))}
    assert rows["tx_a"]["audio_path"] == "audio/tx_a.wav"
    assert rows["tx_b"]["audio_path"] == ""


def test_exports_never_change_the_stored_rows(store, operator):
    make_rich_run(store, operator)
    before = [t.to_dict() for t in store.list_transmissions(session_id="run1")]
    session = store.get_session("run1")
    for fmt in FORMATS:
        export.portable_text(fmt, store, store.list_transmissions(session_id="run1"),
                             session)
    assert [t.to_dict() for t in store.list_transmissions(session_id="run1")] == before
    assert store.get_session("run1").save_path == str(operator["drive"])


# -- the window -------------------------------------------------------------------
@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(qt_app, config, store, tmp_path, operator):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "apphome")
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    app = BabelFishRApp(config=config, store=store)
    win = MainWindow(app)
    yield win
    win.close()
    app.close()


def choose_file(monkeypatch, path: pathlib.Path):
    from PySide6 import QtWidgets

    # The save dialog's own overwrite confirmation was answered yes.
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))


@pytest.mark.parametrize("fmt", FORMATS)
def test_window_export_is_projected_and_replaces_the_confirmed_file(
        window, store, tmp_path, operator, monkeypatch, fmt):
    make_rich_run(store, operator, conversation_id=window.app.conversation_id)
    out = tmp_path / "exports"
    out.mkdir()
    chosen = out / f"chosen.{fmt}"
    chosen.write_text("older export")
    choose_file(monkeypatch, chosen)
    shown = []
    monkeypatch.setattr(window.status, "showMessage",
                        lambda text, *a: shown.append(text))
    window._export_text(fmt)
    finish_export(window)
    assert shown[-1] == f"Exported {chosen}"
    check_standalone(fmt, chosen.read_text(encoding="utf-8"), operator)
    assert sorted(p.name for p in out.iterdir()) == [chosen.name]


@pytest.mark.parametrize("fmt", FORMATS)
def test_window_export_failure_warns_and_keeps_the_old_file(
        window, store, tmp_path, operator, monkeypatch, fmt):
    from PySide6 import QtWidgets

    make_rich_run(store, operator, conversation_id=window.app.conversation_id)
    out = tmp_path / "exports"
    out.mkdir()
    chosen = out / f"chosen.{fmt}"
    chosen.write_text("older export")
    choose_file(monkeypatch, chosen)
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a[2])))

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export.os, "fsync", full)
    window._export_text(fmt)
    finish_export(window)
    assert warned and "could not be written" in warned[0]
    assert "No space left on device" in warned[0]
    assert chosen.read_text() == "older export"
    assert sorted(p.name for p in out.iterdir()) == [chosen.name]
