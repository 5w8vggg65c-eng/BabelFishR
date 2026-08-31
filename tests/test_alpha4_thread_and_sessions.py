"""Regressions for the newest-first thread, named Sessions and honest RF data.

The scrolling tests drive a real QScrollArea with enough bubbles to overflow
its viewport and measure pixel positions, because list order alone says
nothing about whether the text under the operator's eyes moved. The migration
tests build a genuine schema-3 database from the real schema-3 DDL, checked in
at tests/fixtures/schema_3.sql, rather than a hand-written approximation of it.
That fixture is a file, not a Git lookup: these tests must run from a source
tree alone - a shallow clone, an exported archive, a directory with no .git.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.models import (AnalysisAttempt, AnalysisOutcome,
                               DEFAULT_CONVERSATION_NAME, ProcessingState,
                               Provenance, Session, Transmission)
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.storage import Store
from babelfishr.testing import build_fixture

ROOT = pathlib.Path(__file__).resolve().parent.parent
SR = 48_000
BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, times: int = 10) -> None:
    for _ in range(times):
        qt_app.processEvents()


def tx(index: int, text: str = "", **kwargs) -> Transmission:
    kwargs.setdefault("target_language", "en")
    kwargs.setdefault("source_language", "en")
    return Transmission(id=f"tx_{index:03d}", session_id="s",
                        started_at=BASE + dt.timedelta(minutes=index),
                        duration=2.0, state=ProcessingState.COMPLETE,
                        transcript=text or f"message {index} " + "wrap " * 8,
                        **kwargs)


def overflowing_view(qt_app, count: int = 30):
    """A real scroll area with more bubbles than fit in it."""
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    view.resize(600, 400)
    view.show()
    view.set_transmissions([tx(i) for i in range(count)])
    pump(qt_app)
    assert view.verticalScrollBar().maximum() > 0, "the viewport did not overflow"
    return view


# ---- A. newest first ----------------------------------------------------


def test_the_thread_is_newest_first(qt_app):
    view = overflowing_view(qt_app)
    assert view.order()[0] == "tx_029"
    assert view.order()[-1] == "tx_000"
    bubbles = view._bubbles
    assert bubbles["tx_029"].y() < bubbles["tx_000"].y(), (
        "the newest bubble is not physically above the oldest")
    view.hide()


def test_a_new_transmission_is_inserted_at_the_top(qt_app):
    view = overflowing_view(qt_app)
    view.add(tx(99, "new traffic"))
    pump(qt_app)
    assert view.order()[0] == "tx_099"
    assert view._bubbles["tx_099"].y() < view._bubbles["tx_029"].y()
    view.hide()


def test_the_thread_opens_at_the_newest_transmission(qt_app):
    view = overflowing_view(qt_app)
    bar = view.verticalScrollBar()
    assert bar.value() == bar.minimum(), "the thread did not open at the top"
    view.hide()


def _viewport_y(view, tx_id: str) -> int:
    """Where a bubble sits relative to the top of the visible area."""
    return view._bubbles[tx_id].y() - view.verticalScrollBar().value()


def test_nothing_scrolls_to_the_bottom_by_itself(qt_app):
    view = overflowing_view(qt_app)
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    pump(qt_app)
    anchor_id, _ = view._anchor()
    before = _viewport_y(view, anchor_id)
    for index in range(90, 95):
        view.add(tx(index, "more traffic"))
        pump(qt_app)
    assert bar.value() != bar.maximum(), "the view followed new traffic downwards"
    assert bar.value() != bar.minimum(), "the view snapped back to the top"
    # Five arrivals in a row, and the message being read has not moved a
    # pixel. The old version of this line ended in "or True", which asserted
    # nothing at all.
    assert _viewport_y(view, anchor_id) == before
    view.hide()


def test_inserting_above_the_viewport_does_not_move_what_is_being_read(qt_app):
    """The one that matters: an arriving transmission must not shove the
    message the operator is reading down the screen."""
    view = overflowing_view(qt_app)
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    pump(qt_app)

    anchor = view._anchor()
    assert anchor is not None, "the test is not scrolled away from the top"
    anchor_id, _ = anchor
    before = _viewport_y(view, anchor_id)

    view.add(tx(99, "traffic arriving while history is being read"))
    pump(qt_app)

    assert view.order()[0] == "tx_099"
    assert _viewport_y(view, anchor_id) == before, (
        "the bubble under the operator's eyes moved when a new one arrived")
    view.hide()


def test_a_bubble_growing_above_the_viewport_preserves_the_anchor(qt_app):
    """Captured to Transcribing to Complete, and a translation appearing,
    all change the height of a bubble above what is being read."""
    view = overflowing_view(qt_app)
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    pump(qt_app)
    anchor_id, _ = view._anchor()
    above = view.order()[1]
    assert view._bubbles[above].y() < view._bubbles[anchor_id].y()

    for state, transcript, translation in (
            (ProcessingState.TRANSCRIBING, "", ""),
            (ProcessingState.TRANSLATING, "hola " * 30, ""),
            (ProcessingState.COMPLETE, "hola " * 30, "hello " * 30)):
        before = _viewport_y(view, anchor_id)
        height_before = view._bubbles[above].height()
        grown = tx(int(above.split("_")[1]))
        grown.id = above
        grown.state = state
        grown.transcript = transcript
        grown.translation = translation
        grown.source_language = "es"
        view.update(grown)
        pump(qt_app)
        assert _viewport_y(view, anchor_id) == before, (
            f"the anchor moved when a bubble above it changed to {state.value}")
        if translation:
            assert view._bubbles[above].height() != height_before, (
                "the bubble did not actually change height, so nothing was "
                "being compensated for")
    view.hide()


def test_at_the_top_a_new_bubble_simply_appears(qt_app):
    view = overflowing_view(qt_app)
    bar = view.verticalScrollBar()
    assert bar.value() == 0
    view.add(tx(99, "new"))
    pump(qt_app)
    assert view.order()[0] == "tx_099"
    assert bar.value() == 0, "the view left the top when it did not need to"
    view.hide()


# ---- B. named Session tabs ----------------------------------------------


def app_with_mocks(config, store) -> BabelFishRApp:
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


@pytest.fixture
def wav(tmp_path) -> str:
    return build_fixture([{"gap": 1.2},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
                          {"gap": 1.2}], sample_rate=SR).write(
        str(tmp_path / "voice.wav"))


def test_general_exists_and_is_the_default(config, store):
    app = app_with_mocks(config, store)
    conversations = app.conversations()
    assert [c.name for c in conversations] == [DEFAULT_CONVERSATION_NAME]
    assert conversations[0].is_default
    assert app.conversation_id == conversations[0].id
    app.close()


def test_creating_and_renaming_a_session_survives_a_relaunch(config, store):
    app = app_with_mocks(config, store)
    ops = app.create_conversation("Ops North")
    app.select_conversation(ops.id)
    app.rename_conversation(ops.id, "Ops North East")
    app.close()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    relaunched = BabelFishRApp(config=config, store=reopened)
    names = [c.name for c in relaunched.conversations()]
    assert names == [DEFAULT_CONVERSATION_NAME, "Ops North East"]
    assert relaunched.restore_selected_conversation() == ops.id, (
        "the last selected Session was not restored")
    relaunched.close()


def test_transmissions_are_filtered_by_the_selected_session(config, store, wav):
    app = app_with_mocks(config, store)
    general = app.conversation_id
    app.start_session(replay_path=wav, name="one")
    app.run_replay()
    app.stop_session()
    in_general = [t.id for t in app.recent_transmissions()]
    assert in_general

    ops = app.create_conversation("Ops North")
    app.select_conversation(ops.id)
    assert app.recent_transmissions() == [], (
        "a new Session opened showing another Session's transmissions")

    app.start_session(replay_path=wav, name="two")
    app.run_replay()
    app.stop_session()
    in_ops = [t.id for t in app.recent_transmissions()]
    assert in_ops
    assert not set(in_ops) & set(in_general), "the two threads share rows"

    app.select_conversation(general)
    assert [t.id for t in app.recent_transmissions()] == in_general
    app.close()


def test_repeated_runs_in_one_session_form_one_thread(config, store, wav):
    """Start, stop, start again - the operator sees one continuous log."""
    app = app_with_mocks(config, store)
    for name in ("first", "second", "third"):
        app.start_session(replay_path=wav, name=name)
        app.run_replay()
        app.stop_session()

    thread = app.recent_transmissions()
    sessions = {t.session_id for t in thread}
    assert len(sessions) == 3, "the runs were not separate Sessions underneath"
    assert len(thread) >= 3, "the thread lost transmissions between runs"
    assert thread == sorted(thread, key=lambda t: t.started_at), (
        "the thread is not in time order")
    app.close()


def test_switching_tabs_while_monitoring_never_refiles_live_traffic(
        config, store, wav):
    app = app_with_mocks(config, store)
    general = app.conversation_id
    ops = app.create_conversation("Ops North")

    app.start_session(replay_path=wav, name="live")
    assert app.capture_conversation_id == general
    # The operator wanders off to read another Session mid-watch.
    app.select_conversation(ops.id)
    assert app.capture_conversation_id == general, (
        "switching tabs redirected the running capture")
    app.run_replay()
    app.stop_session()

    assert app.recent_transmissions(conversation_id=ops.id) == [], (
        "live traffic was misfiled into the Session merely being viewed")
    assert app.recent_transmissions(conversation_id=general), (
        "live traffic did not reach the Session monitoring started under")
    app.close()


def test_only_one_capture_service_and_pipeline_exist(config, store, wav):
    app = app_with_mocks(config, store)
    app.create_conversation("Ops North")
    app.start_session(replay_path=wav, name="one")
    assert app.capture is not None and app.pipeline is not None
    with pytest.raises(RuntimeError):
        app.start_session(replay_path=wav, name="two")
    app.stop_session()
    assert app.capture is None and app.pipeline is None
    app.close()


def test_the_tab_bar_reflects_and_filters_the_threads(qt_app, config, store, wav):
    from babelfishr.ui.main_window import MainWindow

    app = app_with_mocks(config, store)
    app.start_session(replay_path=wav, name="general run")
    app.run_replay()
    app.stop_session()
    general_ids = {t.id for t in app.recent_transmissions()}
    assert general_ids

    ops = app.create_conversation("Ops North")
    window = MainWindow(app)
    pump(qt_app)
    labels = [window.session_tabs.tabText(i)
              for i in range(window.session_tabs.count())]
    assert labels == [DEFAULT_CONVERSATION_NAME, "Ops North"]
    assert set(window.timeline._bubbles) == general_ids

    index = labels.index("Ops North")
    window.session_tabs.setCurrentIndex(index)
    pump(qt_app)
    assert app.conversation_id == ops.id
    assert window.timeline.count() == 0, (
        "another Session's transmissions are showing in this tab")

    window.session_tabs.setCurrentIndex(0)
    pump(qt_app)
    assert set(window.timeline._bubbles) == general_ids
    window.hide()


def test_an_event_for_another_session_does_not_enter_this_thread(
        qt_app, config, store, wav):
    from babelfishr.ui.main_window import MainWindow

    app = app_with_mocks(config, store)
    general = app.conversation_id
    ops = app.create_conversation("Ops North")
    window = MainWindow(app)
    pump(qt_app)

    session = Session(name="elsewhere", conversation_id=ops.id)
    store.save_session(session)
    stray = Transmission(session_id=session.id, started_at=BASE, duration=1.0,
                         transcript="not for this tab",
                         state=ProcessingState.COMPLETE)
    store.save_transmission(stray)

    assert app.conversation_id == general
    assert window._belongs_here(stray) is False
    app.events.publish("transmission", stray)
    window._drain_events()
    pump(qt_app)
    assert stray.id not in window.timeline._bubbles, (
        "a transmission from another Session appeared in the wrong thread")
    window.hide()


def test_exporting_a_session_covers_every_run_inside_it(config, store, wav,
                                                        tmp_path):
    from babelfishr.export import export_session

    app = app_with_mocks(config, store)
    conversation_id = app.conversation_id
    for name in ("first", "second"):
        app.start_session(replay_path=wav, name=name)
        app.run_replay()
        app.stop_session()
    expected = {t.id for t in app.recent_transmissions()}
    runs = store.session_ids_for_conversation(conversation_id)
    assert len(runs) == 2

    target = tmp_path / "bundle"
    path = export_session(store, runs[0], str(target),
                          conversation_id=conversation_id)
    manifest = json.loads((pathlib.Path(path) / "session.json").read_text())
    exported = {entry["id"] for entry in manifest["transmissions"]}
    assert exported == expected, (
        "the export covered one monitoring run, not the whole Session")
    started = [entry["started_at"] for entry in manifest["transmissions"]]
    assert started == sorted(started), "the export is not chronological"
    app.close()


# ---- B. migration from the deployed schema 3 ----------------------------


#: The real schema-3 DDL, checked in rather than read out of Git.
#:
#: This used to be `git show 7a42cfc:babelfishr/storage.py` at run time, which
#: made the migration tests depend on repository history they cannot count on
#: having: actions/checkout leaves a one-commit shallow clone, so `git show`
#: exited 128 and both tests errored before their bodies ran. The file carries
#: its own provenance - it is byte-identical to the `_SCHEMA` literal at both
#: v0.3.0-alpha.3 (c9299e0) and the final schema-3 commit 7a42cfc.
SCHEMA_3_DDL = ROOT / "tests" / "fixtures" / "schema_3.sql"


def schema_3_ddl() -> str:
    """The DDL out of the fixture, with its provenance header removed.

    The header is the leading run of "--" lines and nothing else, so what this
    returns is the deployed DDL byte for byte - which is what lets the digest
    below pin it.
    """
    lines = SCHEMA_3_DDL.read_text().split("\n")
    first = next(i for i, line in enumerate(lines)
                 if not line.startswith("--"))
    return "\n".join(lines[first:])


def schema_3_database(path: str) -> None:
    """A real alpha 3 database, built from the deployed schema-3 DDL.

    Reads a file, runs no subprocess and needs no Git: a migration has to be
    testable from a source tree alone, which is exactly the situation the
    packaging runner is in.
    """
    ddl = schema_3_ddl()
    # The migration is only meaningful against the schema it has to open, so
    # fail loudly rather than quietly upgrading an already-current database.
    assert "conversation_id" not in ddl, (
        "the fixture is not schema 3 - it already has the schema-4 columns")
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) "
                 "VALUES ('schema_version', '3')")
    for run in range(3):
        session_id = f"sess_{run}"
        conn.execute(
            "INSERT INTO sessions (id, name, started_at, target_language) "
            "VALUES (?,?,?,?)",
            (session_id, f"run {run}", "2026-01-01T00:00:00+00:00", "en"))
        for index in range(2):
            conn.execute(
                """INSERT INTO transmissions
                       (id, session_id, started_at, duration, audio_path,
                        transcript, translation, transcript_correction,
                        translation_correction, notes, tags, bookmarked,
                        frequency_mhz, frequency_provenance, state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"tx_{run}{index}", session_id,
                 f"2026-01-01T00:{run * 10 + index:02d}:00+00:00", 2.5,
                 f"/recordings/{run}-{index}.wav", f"transcript {run}{index}",
                 f"translation {run}{index}", f"corrected {run}{index}",
                 f"fixed {run}{index}", f"note {run}{index}", '["alpha","bravo"]',
                 1, 462.575, "radio-profile-default", "complete"))
    conn.commit()
    conn.close()


def test_an_alpha_3_database_upgrades_without_losing_anything(tmp_path):
    database = str(tmp_path / "alpha3.sqlite3")
    schema_3_database(database)

    before = sqlite3.connect(database)
    before.row_factory = sqlite3.Row
    original = {r["id"]: dict(r) for r in
                before.execute("SELECT * FROM transmissions")}
    assert "conversation_id" not in {
        r[1] for r in before.execute("PRAGMA table_info(sessions)")}
    before.close()

    store = Store(database, recordings_dir=str(tmp_path))
    assert store.schema_version == 4

    conversations = store.list_conversations()
    assert [c.name for c in conversations] == [DEFAULT_CONVERSATION_NAME]
    general = conversations[0]
    assert general.is_default
    assert sorted(store.session_ids_for_conversation(general.id)) == [
        "sess_0", "sess_1", "sess_2"], "old runs were not filed under General"

    restored = store.conversation_transmissions(general.id, limit=1000)
    assert len(restored) == len(original) == 6
    for transmission in restored:
        was = original[transmission.id]
        assert transmission.audio_path == was["audio_path"]
        assert transmission.transcript == was["transcript"]
        assert transmission.translation == was["translation"]
        assert transmission.transcript_correction == was["transcript_correction"]
        assert transmission.translation_correction == was["translation_correction"]
        assert transmission.notes == was["notes"]
        assert transmission.tags == ["alpha", "bravo"]
        assert transmission.bookmarked is True
        assert transmission.frequency_mhz == was["frequency_mhz"]
        assert transmission.frequency_provenance is Provenance.PROFILE
        # And the columns this schema adds are present and empty, not absent.
        assert transmission.unit_id == ""
        assert transmission.signal_metadata == {}
    store.close()


def test_the_migration_is_idempotent(tmp_path):
    database = str(tmp_path / "alpha3.sqlite3")
    schema_3_database(database)

    first = Store(database, recordings_dir=str(tmp_path))
    general = first.default_conversation()
    counts = (len(first.list_conversations()),
              len(first.session_ids_for_conversation(general.id)),
              len(first.conversation_transmissions(general.id, limit=1000)))
    first.close()

    for _ in range(3):
        again = Store(database, recordings_dir=str(tmp_path))
        assert (len(again.list_conversations()),
                len(again.session_ids_for_conversation(general.id)),
                len(again.conversation_transmissions(general.id, limit=1000))
                ) == counts, "reopening the database changed it"
        assert again.default_conversation().id == general.id
        again.close()


def test_the_schema_3_baseline_is_the_real_deployed_ddl():
    """The fixture is the shipped schema 3, not a reconstruction from schema 4.

    Pinned by digest because a migration tested against a hand-written
    approximation of its own starting point proves nothing. This is the exact
    `_SCHEMA` literal from babelfishr/storage.py at v0.3.0-alpha.3 (c9299e0)
    and at the final schema-3 commit 7a42cfc, where it is byte-identical.
    """
    import hashlib

    ddl = schema_3_ddl()
    assert hashlib.sha256(ddl.encode()).hexdigest() == (
        "af7dc8a1b94a78cdeace5f4a7519d32ed32a945dece44d7de39a1a6b0522d4da"), (
        "the schema-3 baseline has been altered; it must stay the DDL that "
        "was actually deployed")
    assert len(ddl) == 3787
    # Schema 3's shape, positively: the tables exist, and the schema-4
    # additions the migration is supposed to make do not.
    for table in ("meta", "profiles", "sessions", "transmissions"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert "conversation_id" not in ddl
    assert "conversations" not in ddl


def test_the_migration_tests_do_not_depend_on_repository_history():
    """No Git, no subprocess: these must run from a source tree alone.

    They did not. `git show 7a42cfc:babelfishr/storage.py` at run time worked
    in a full clone and exited 128 in the one-commit shallow clone
    actions/checkout leaves, so both migration tests errored before their
    bodies ran and the packaging build never started.
    """
    import ast

    tree = ast.parse(pathlib.Path(__file__).read_text())
    imported = {alias.name.split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0]
                 for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "subprocess" not in imported, (
        "this module shells out again; the migration baseline must be a file")
    assert "git" not in imported

    assert SCHEMA_3_DDL.is_file(), "the baseline fixture is not checked in"
    # Resolved from this file, so it works in any tree the tests are run from.
    assert SCHEMA_3_DDL.parent.parent == pathlib.Path(__file__).resolve().parent
    # And it really is loadable without a database engine's help or a clone.
    assert schema_3_ddl().lstrip().startswith("CREATE TABLE")


# ---- C. honest RF and transmitter metadata ------------------------------


def test_absent_metadata_shows_nothing_at_all():
    plain = Transmission(session_id="s")
    assert plain.signal_summary() == []
    assert plain.has_supplied_unit_id is False


def test_an_entered_frequency_is_never_presented_as_measured():
    entered = Transmission(session_id="s", frequency_mhz=462.575,
                           frequency_provenance=Provenance.PROFILE)
    entry = entered.signal_summary()[0]
    assert entry["measured"] == "no"
    assert "profile" in entry["display"]
    assert entered.frequency_is_measured is False

    measured = Transmission(session_id="s", frequency_mhz=462.575,
                            frequency_provenance=Provenance.SDR)
    assert measured.signal_summary()[0]["measured"] == "yes"
    assert measured.frequency_is_measured is True


def test_a_squelch_tone_is_not_treated_as_an_identity():
    """CTCSS and DCS are channel access, shared by every radio set to them."""
    toned = Transmission(session_id="s", squelch_code="127.3 Hz",
                         squelch_code_provenance=Provenance.DSD)
    assert toned.has_supplied_unit_id is False
    entry = toned.signal_summary()[0]
    assert entry["label"] == "squelch"
    for forbidden in ("user", "speaker", "operator", "person", "identity"):
        assert forbidden not in entry["display"].lower()


def test_an_unprovided_unit_id_is_never_an_identification():
    for provenance in (Provenance.UNKNOWN, Provenance.INFERRED,
                       Provenance.PROFILE):
        guessed = Transmission(session_id="s", unit_id="4021",
                               unit_id_provenance=provenance)
        assert guessed.has_supplied_unit_id is False, (
            f"a {provenance.value} unit ID was treated as identified")
    for provenance in (Provenance.SDR, Provenance.DSD, Provenance.RADIO,
                       Provenance.OPERATOR):
        supplied = Transmission(session_id="s", unit_id="4021",
                                unit_id_provenance=provenance)
        assert supplied.has_supplied_unit_id is True


def test_decoded_identifiers_are_materialised_with_dsd_provenance():
    from babelfishr.signal_metadata import apply_decoded_metadata

    transmission = Transmission(session_id="s")
    attempt = AnalysisAttempt(
        engine="dsd-neo", engine_version="1.2.3", protocol="DMR",
        outcome=AnalysisOutcome.PROTOCOL_IDENTIFIED,
        metadata={"talkgroup": "1201", "source_id": "4021",
                  "color_code": "1", "nac": "293"})
    assert apply_decoded_metadata(transmission, attempt) is True

    assert transmission.talkgroup == "1201"
    assert transmission.talkgroup_provenance is Provenance.DSD
    assert transmission.unit_id == "4021"
    assert transmission.unit_id_provenance is Provenance.DSD
    assert transmission.protocol == "DMR"
    assert transmission.protocol_provenance is Provenance.DSD
    assert transmission.has_supplied_unit_id is True
    # The raw output is kept whole, including the keys not promoted.
    assert transmission.signal_metadata["dsd-neo"]["nac"] == "293"
    assert transmission.signal_metadata["dsd-neo"]["color_code"] == "1"


def test_nothing_is_invented_from_ordinary_audio(config, store, wav):
    """A microphone carries none of this, so none of it may appear."""
    app = app_with_mocks(config, store)
    app.start_session(replay_path=wav, name="microphone")
    app.run_replay()
    app.stop_session()

    for transmission in app.recent_transmissions():
        assert transmission.frequency_mhz is None
        assert transmission.rssi_dbm is None
        assert transmission.snr_db is None
        assert transmission.squelch_code == ""
        assert transmission.talkgroup == ""
        assert transmission.unit_id == ""
        assert transmission.protocol == ""
        assert transmission.signal_summary() == []
        assert transmission.has_supplied_unit_id is False
    app.close()


def test_a_source_that_states_no_provenance_is_not_believed():
    from babelfishr.signal_metadata import apply_source_metadata

    transmission = Transmission(session_id="s")
    apply_source_metadata(transmission, {"source": "recorded-iq",
                                         "frequency_mhz": 462.5625})
    assert transmission.frequency_provenance is Provenance.UNKNOWN
    assert transmission.frequency_is_measured is False
    assert transmission.signal_summary()[0]["measured"] == "no"


def test_supplied_metadata_appears_in_the_bubble_and_absent_does_not(qt_app):
    from babelfishr.ui.timeline import TimelineView

    view = TimelineView()
    rich = view.add(Transmission(
        session_id="s", transcript="units responding",
        state=ProcessingState.COMPLETE, target_language="en",
        source_language="en", talkgroup="1201",
        talkgroup_provenance=Provenance.DSD, unit_id="4021",
        unit_id_provenance=Provenance.DSD, rssi_dbm=-73.0,
        rssi_provenance=Provenance.SDR))
    header = rich.header.text()
    assert "talkgroup 1201 (decoded)" in header
    assert "unit 4021 (decoded)" in header
    assert "RSSI -73 dBm" in header
    assert rich.original_label.text() == "units responding"

    plain = view.add(Transmission(
        id="tx_plain", session_id="s", transcript="hello",
        state=ProcessingState.COMPLETE, target_language="en",
        source_language="en"))
    for absent in ("talkgroup", "unit", "RSSI", "SNR", "squelch", "protocol"):
        assert absent not in plain.header.text()


# ---- D. one obvious operating-mode control ------------------------------


def test_there_is_exactly_one_operating_mode_selector(qt_app, config, store):
    from PySide6 import QtWidgets

    from babelfishr.modes import OperatingMode
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(app_with_mocks(config, store))
    pump(qt_app)

    labels = {m.label for m in OperatingMode}
    offenders = []
    for combo in window.findChildren(QtWidgets.QComboBox):
        items = {combo.itemText(i) for i in range(combo.count())}
        if labels <= items:
            offenders.append(combo.objectName() or "unnamed combo")
    assert not offenders, (
        f"a second operating-mode selector exists: {offenders}")
    assert not hasattr(window, "mode_box")

    assert isinstance(window.mode_button, QtWidgets.QToolButton)
    assert window.mode_button.menu() is not None
    menu_labels = {a.text() for a in window.mode_button.menu().actions()}
    assert menu_labels == labels
    window.hide()


def test_the_mode_control_is_interactive_and_keyboard_reachable(qt_app, config,
                                                                 store):
    from PySide6 import QtCore, QtWidgets

    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(app_with_mocks(config, store))
    pump(qt_app)
    button = window.mode_button
    assert isinstance(button, QtWidgets.QToolButton), (
        "the operating mode is still a label pretending to be a control")
    assert button.focusPolicy() != QtCore.Qt.NoFocus
    assert "Operating mode" in button.text()
    assert button.accessibleName()
    # Readiness stays a separate control, and is not the same widget.
    assert window.ready_badge is not button
    assert "readiness" in window.ready_badge.toolTip().lower()
    window.hide()


# ---- E. the collapsible panel -------------------------------------------


def test_collapsing_and_expanding_opens_no_popups_and_keeps_values(qt_app,
                                                                    config,
                                                                    store):
    from PySide6 import QtWidgets

    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(app_with_mocks(config, store))
    window.resize(1000, 800)
    window.show()
    pump(qt_app)

    assert window.setup_box.title() == "Session Options"
    combos = window.setup_box.findChildren(QtWidgets.QComboBox)
    assert combos, "the panel has no combo boxes, so this proves nothing"
    values = [c.currentIndex() for c in combos]

    for _ in range(4):
        window.setup_box.setChecked(False)
        pump(qt_app)
        assert not window.setup_content.isVisible()
        window.setup_box.setChecked(True)
        pump(qt_app)
        assert window.setup_content.isVisible()

        for combo in combos:
            view = combo.view()
            assert not view.isVisible(), (
                f"{combo.objectName() or 'a combo box'} popup opened during a "
                f"collapse/expand cycle")
            assert not combo.view().window().isVisible() or \
                combo.view().window() is window.window()
        for area in window.setup_box.findChildren(QtWidgets.QAbstractScrollArea):
            for bar in (area.verticalScrollBar(), area.horizontalScrollBar()):
                assert not bar.isVisible(), "a stray scrollbar became visible"

    assert [c.currentIndex() for c in combos] == values, (
        "a control lost its value across collapse/expand")
    window.hide()


def test_the_panel_toggle_does_not_walk_every_descendant():
    """The defect was the implementation, so the implementation is checked.

    findChildren(QWidget) reaches a combo box's popup view and a scroll
    area's scrollbars; calling setVisible on those is what produced the
    dangling dropdowns. The behavioural test above is the real guard - this
    one names the cause so a future edit cannot reintroduce it quietly.
    """
    import ast
    import inspect

    from babelfishr.ui.main_window import MainWindow

    source = inspect.getsource(MainWindow._toggle_setup_panel)
    tree = ast.parse(source.lstrip())
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)}
    assert "findChildren" not in calls
