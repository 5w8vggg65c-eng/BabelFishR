"""CSV cells from the radio, the operator or the app are literal text.

Independent security inspection of Preview 12: to_csv preserved
formula-like text from notes, transcripts and translations, so a
spreadsheet opening the export could evaluate "=1+1" (or worse, a
link-building or DDE formula). Every cell now passes one boundary,
csv_cell(): text a spreadsheet could take for a formula gets a leading
apostrophe; app-typed measurements stay numbers, negative ones included;
JSON and the database keep the operator's text exactly.

Only harmless arithmetic and plain text are used here.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json

import pytest

from babelfishr import export
from babelfishr.models import ProcessingState, Session, Transmission
from test_export_safety import read_bundle

T0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)

TRICKY = {
    "notes": "=1+1",
    "transcript": "+2+3 copy that",
    "translation": "-4+5 recibido",
    "transcript_correction": "@SUM(1,2)",
    "translation_correction": "\t=6+7",
    "channel_name": "  =8+9",
    "profile_label": "\r=1+2",
}


def tricky_run(store, tmp_path):
    store.save_session(Session(id="run1", started_at=T0))
    wav = tmp_path / "rec" / "a.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF" + bytes(64))
    tricky = Transmission(
        id="tx_a", session_id="run1", started_at=T0, duration=2.5,
        state=ProcessingState.COMPLETE, audio_path=str(wav),
        frequency_mhz=-1.25, transcript_confidence=0.5,
        tags=["=tag"], **TRICKY)
    plain = Transmission(
        id="tx_b", session_id="run1", started_at=T0 + dt.timedelta(seconds=10),
        duration=1.0, state=ProcessingState.COMPLETE,
        transcript='radio check, "loud and clear"', notes="plain note, 42",
        frequency_mhz=146.52)
    store.save_transmission(tricky)
    store.save_transmission(plain)
    return tricky, plain


def rows(text):
    return {r["id"]: r for r in csv.DictReader(io.StringIO(text))}


def check_csv(text):
    table = rows(text)
    tricky, plain = table["tx_a"], table["tx_b"]
    for column, original in TRICKY.items():
        assert tricky[column] == "'" + original, column
    assert tricky["tags"] == "'=tag"
    # Measurements stay numbers, negative included; plain text is exact.
    assert tricky["frequency_mhz"] == "-1.25"
    assert tricky["duration"] == "2.5"
    assert tricky["transcript_confidence"] == "0.5"
    assert plain["frequency_mhz"] == "146.52"
    assert plain["transcript"] == 'radio check, "loud and clear"'
    assert plain["notes"] == "plain note, 42"
    # No cell a spreadsheet would evaluate.
    for row in table.values():
        for column, cell in row.items():
            if column in export.CSV_NUMERIC_COLUMNS:
                continue
            assert not cell.lstrip(" ")[:1] in ("=", "+", "-", "@"), (column, cell)
            assert not (cell[:1] and ord(cell[0]) < 32), (column, cell)
    # Standard quoting: the raw text quotes the comma and doubles the quote.
    assert '"radio check, ""loud and clear"""' in text


def run_cli(config, tmp_path, *argv):
    from babelfishr.cli import main

    settings = tmp_path / "cli.toml"
    config.save(str(settings))
    return main(["--config", str(settings), "export", "--session", "run1", *argv])


def test_standalone_csv_on_stdout_and_file(config, store, tmp_path, capsys):
    tricky_run(store, tmp_path)
    assert run_cli(config, tmp_path, "--format", "csv") == 0
    check_csv(capsys.readouterr().out)
    out = tmp_path / "out.csv"
    assert run_cli(config, tmp_path, "--format", "csv", "--output", str(out)) == 0
    with open(out, encoding="utf-8", newline="") as handle:
        check_csv(handle.read())


@pytest.mark.parametrize("archive", [False, True])
def test_bundle_csv(config, store, tmp_path, capsys, archive):
    tricky_run(store, tmp_path)
    dest = tmp_path / "bundle"
    argv = ["--format", "bundle", "--output", str(dest)] + (["--zip"] if archive else [])
    assert run_cli(config, tmp_path, *argv) == 0
    path = dest.with_suffix(".zip") if archive else dest
    files = read_bundle(path)
    check_csv(files["transmissions.csv"].decode("utf-8"))
    # JSON keeps the operator's text exactly.
    manifest = json.loads(files["session.json"])
    entry = next(t for t in manifest["transmissions"] if t["id"] == "tx_a")
    for column, original in TRICKY.items():
        assert entry[column] == original, column


def test_standalone_json_and_the_database_keep_the_text(config, store, tmp_path,
                                                        capsys):
    tricky_run(store, tmp_path)
    assert run_cli(config, tmp_path, "--format", "json") == 0
    entry = next(t for t in json.loads(capsys.readouterr().out)["transmissions"]
                 if t["id"] == "tx_a")
    stored = store.get_transmission("tx_a")
    for column, original in TRICKY.items():
        assert entry[column] == original
        assert getattr(stored, column) == original


@pytest.mark.parametrize("value,expected", [
    ("=1+1", "'=1+1"), (" =1+1", "' =1+1"), ("+1", "'+1"), ("-1", "'-1"),
    ("@A1", "'@A1"), ("\t1", "'\t1"), ("\r1", "'\r1"), ("\n=1", "'\n=1"),
    ("＝1+1", "'＝1+1"), ("plain", "plain"), ("", ""), ("1+1", "1+1"),
    ("a=b", "a=b"),
])
def test_text_cells(value, expected):
    assert export.csv_cell("notes", value) == expected


@pytest.mark.parametrize("value,expected", [
    (-2.5, -2.5), (0, 0), ("-2.5", "-2.5"), ("1e-3", "1e-3"), ("=1", "'=1"),
    ("-1+1", "'-1+1"), (None, ""),
])
def test_measurement_cells(value, expected):
    assert export.csv_cell("duration", value) == expected
