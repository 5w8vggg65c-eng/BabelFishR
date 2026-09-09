"""The search index is maintained by rowid and rewritten only when the
searchable text changes (F6).

Before: every accepted save deleted the message's index entry and inserted it
again - found by scanning the whole index for an UNINDEXED id - so a message
that was saved as captured, transcribing, translating and complete rewrote an
unchanged entry four times, and every retention or permanent deletion scanned
the index once more. Here the index's own rowid is remembered per message in
transmissions_fts_map; the text the index actually holds is compared with
what is being saved; and a message with nothing searchable has no entry.

Temporary databases only. FTS5 is compiled into this Python's SQLite; the
one test that exercises the fallback disables it deliberately.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import time

import pytest

from babelfishr.models import ProcessingState, Session, Transmission
import babelfishr.storage as storage
from babelfishr.storage import SCHEMA_VERSION, Store

FTS_LAYOUT = getattr(storage, "FTS_LAYOUT", 2)   # tolerant, so the file collects against the old code too

T0 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)

OLD_FTS_DDL = ("CREATE VIRTUAL TABLE IF NOT EXISTS transmissions_fts USING fts5 ("
               "id UNINDEXED, transcript, translation, correction, notes, tags, "
               "tokenize = 'unicode61')")


def record(i, session_id="s", **fields) -> Transmission:
    base = dict(id=f"tx{i}", session_id=session_id, started_at=T0 + dt.timedelta(seconds=i),
                duration=2.0, audio_path=f"/rec/{i}.wav", state=ProcessingState.CAPTURED,
                target_language="en", source_language="en")
    base.update(fields)
    return Transmission(**base)


class Trace:
    """Top-level statements against the index (not FTS5's own shadow-table
    traffic, which is quoted 'main'.'transmissions_fts_...')."""

    def __init__(self, store):
        self.statements = []
        store._conn.set_trace_callback(self.statements.append)

    def index_writes(self):
        return [s for s in self.statements
                if s.startswith(("INSERT INTO transmissions_fts ", "DELETE FROM transmissions_fts ",
                                 "UPDATE transmissions_fts ", "INSERT INTO transmissions_fts("))]

    def index_reads(self):
        return [s for s in self.statements
                if s.startswith("SELECT") and " FROM transmissions_fts " in s]

    def clear(self):
        self.statements.clear()


def index_rows(store):
    return store._conn.execute(
        "SELECT rowid, id FROM transmissions_fts ORDER BY rowid").fetchall()


def mapping(store):
    return {r["id"]: r["fts_rowid"] for r in
            store._conn.execute("SELECT id, fts_rowid FROM transmissions_fts_map")}


def found(store, word):
    return sorted(t.id for t in store.search(word, conversation_id=None))


@pytest.fixture
def s(store):
    store.save_session(Session(id="s", conversation_id=store.default_conversation().id))
    return store


# ---- unnecessary writes --------------------------------------------------------------

def test_state_only_saves_persist_without_rewriting_the_index_entry(s):
    tx = record(1, transcript="pineapple on the ridge")
    s.save_transmission(tx)
    assert found(s, "pineapple") == ["tx1"]
    entry = index_rows(s)
    trace = Trace(s)
    writes_before = s._writes
    for state in (ProcessingState.TRANSCRIBING, ProcessingState.TRANSLATING,
                  ProcessingState.COMPLETE):
        tx.state = state
        tx.transcript_confidence = 0.8
        tx.processed_audio_path = f"/rec/1.{state.value}.wav"
        s.save_transmission(tx)
    assert trace.index_writes() == [], trace.index_writes()
    # Persisted all the same: the state, the confidence and the path are current.
    loaded = s.get_transmission("tx1")
    assert loaded.state is ProcessingState.COMPLETE
    assert loaded.transcript_confidence == 0.8
    assert loaded.processed_audio_path == "/rec/1.complete.wav"
    assert index_rows(s) == entry and found(s, "pineapple") == ["tx1"]
    # The retained-file-reference bookkeeping still moves on every save.
    assert s._writes == writes_before + 3


def test_mutating_and_resaving_the_same_instance_updates_the_entry(s):
    tx = record(2, transcript="first words")
    s.save_transmission(tx)
    rowid = mapping(s)["tx2"]
    tx.transcript = "second words entirely"           # the same object, mutated
    trace = Trace(s)
    s.save_transmission(tx)
    assert [w.split()[0] for w in trace.index_writes()] == ["UPDATE"]
    assert found(s, "second") == ["tx2"] and found(s, "first") == []
    assert mapping(s)["tx2"] == rowid, "the entry was rewritten under a new rowid"
    assert len(index_rows(s)) == 1


def test_every_searchable_field_can_be_added_changed_and_cleared(s):
    tx = record(3)                                     # nothing searchable yet
    s.save_transmission(tx)
    assert index_rows(s) == [] and mapping(s) == {}, "an empty message has no entry"

    steps = [
        ("transcript", "kestrel", "kestrel"),
        ("translation", "falcon", "falcon"),
        ("transcript_correction", "osprey", "osprey"),
        ("translation_correction", "harrier", "harrier"),
        ("notes", "buzzard", "buzzard"),
        ("tags", ["merlin", "hobby"], "hobby"),
    ]
    for field, value, word in steps:
        setattr(tx, field, value)
        s.save_transmission(tx)
        assert found(s, word) == ["tx3"], f"{field} added but not found"
    assert len(index_rows(s)) == 1
    for field, value, word in steps:
        setattr(tx, field, [] if field == "tags" else ("" if field in ("transcript", "translation", "notes") else None))
        s.save_transmission(tx)
        assert found(s, word) == [], f"{field} cleared but still found"
    assert index_rows(s) == [] and mapping(s) == {}, "a message with nothing left to search keeps no entry"

    # Notes or tags alone are enough to be found - no transcript required.
    tx.tags = ["only-a-tag"]
    s.save_transmission(tx)
    assert found(s, "only-a-tag") == ["tx3"]
    tx.tags = []
    tx.notes = "only a note"
    s.save_transmission(tx)
    assert found(s, "note") == ["tx3"] and found(s, "only-a-tag") == []
    # Replacing a value: the old word goes, the new one comes.
    tx.notes = "replacement note"
    s.save_transmission(tx)
    assert found(s, "replacement") == ["tx3"] and found(s, "only") == []


# ---- the lookup ---------------------------------------------------------------------

def test_index_maintenance_addresses_entries_by_rowid_not_by_scanning_for_the_id(s):
    for i in range(10, 40):
        s.save_transmission(record(i, transcript=f"word{i}"))
    trace = Trace(s)
    tx = s.get_transmission("tx20")
    tx.state = ProcessingState.COMPLETE
    s.save_transmission(tx)                            # unchanged text: read only
    tx.transcript = "changed"
    s.save_transmission(tx)                            # update in place
    s.delete_transmission("tx21")                      # retention deletion
    s.delete_transmission_permanently("tx22", [str(s.recordings_dir)])
    statements = [x for x in trace.index_reads() + trace.index_writes()
                  if "WHERE" in x]
    assert statements, "no index statement was traced"
    for statement in statements:                       # traced with values bound in
        assert "WHERE rowid = " in statement, statement
        plan = " | ".join(r[3] for r in s._conn.execute("EXPLAIN QUERY PLAN " + statement))
        # FTS5 reports a rowid-equality constraint as 'INDEX 0:=' (the '='
        # is the constraint); a scan for the UNINDEXED id shows bare 'INDEX 0:'.
        assert "INDEX 0:=" in plan, (statement, plan)
    assert not any(" WHERE id = " in x for x in statements)
    assert s._conn.execute("SELECT count(*) FROM transmissions_fts").fetchone()[0] == 28
    assert len(mapping(s)) == 28


def test_index_lookups_do_not_grow_with_the_history(s):
    """Coarse: the per-save work at 20 messages and at 4,000 fully indexed
    messages is the same order of magnitude. A scan of the index would grow
    two hundred fold."""
    def cost(n, first):
        for i in range(first, first + n):
            s.save_transmission(record(i, transcript=f"history entry {i}"))
        tx = s.get_transmission(f"tx{first}")
        s._conn.execute("BEGIN")
        best = float("inf")
        for _ in range(5):
            started = time.perf_counter()
            for k in range(20):
                tx.state = ProcessingState.COMPLETE if k % 2 else ProcessingState.TRANSLATING
                s._conn.execute("UPDATE transmissions SET state = ? WHERE id = ?",
                                (tx.state.value, tx.id))
                s._index_fts(tx)
                s._unindex_fts(f"tx{first + 1 + k}") if k == 0 and False else None
            best = min(best, time.perf_counter() - started)
        s._conn.execute("COMMIT")
        return best

    small = cost(20, 100)
    large = cost(4000, 1000)
    assert large < small * 8 + 0.01, f"index work grew with history: {small:.4f}s -> {large:.4f}s"


# ---- deletion ------------------------------------------------------------------------

def test_deletion_and_retention_remove_the_entry_and_a_late_save_cannot_bring_it_back(s):
    for i in (50, 51, 52):
        s.save_transmission(record(i, transcript=f"marker{i}"))
    late = s.get_transmission("tx50")
    s.delete_transmission("tx50")                                    # retention
    s.delete_transmission_permanently("tx51", [str(s.recordings_dir)])
    assert [r["id"] for r in index_rows(s)] == ["tx52"]
    assert set(mapping(s)) == {"tx52"}
    assert found(s, "marker50") == [] and found(s, "marker51") == []
    late.transcript = "marker50 again from a late worker"
    s.save_transmission(late)                                        # tombstoned: refused
    assert s.get_transmission("tx50") is None
    assert [r["id"] for r in index_rows(s)] == ["tx52"]
    assert found(s, "marker50") == [] and found(s, "again") == []
    # The message with nothing searchable had no entry; deleting it is fine too.
    s.save_transmission(record(53))
    s.delete_transmission("tx53")
    assert set(mapping(s)) == {"tx52"}


# ---- the upgrade -------------------------------------------------------------------

def layout_1_database(path: str, n: int = 6):
    """A schema-5 database exactly as the previous build left it: an index
    with one row per message and no map - plus one duplicated entry and one
    orphan, which the old delete-by-id path could leave behind."""
    from babelfishr.storage import _SCHEMA, _ADDED_COLUMNS, _POST_MIGRATION_SCHEMA

    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for table, column, ddl in _ADDED_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError:
            pass
    conn.executescript(_POST_MIGRATION_SCHEMA)
    conn.execute(OLD_FTS_DDL)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '5')")
    conn.execute("INSERT INTO conversations (id, name, created_at, is_default, position, notes) "
                 "VALUES ('conv', 'General', '2026-01-01T00:00:00+00:00', 1, 0, '')")
    conn.execute("INSERT INTO sessions (id, name, started_at, target_language, conversation_id) "
                 "VALUES ('s', 's', '2026-01-01T00:00:00+00:00', 'en', 'conv')")
    for i in range(n):
        conn.execute(
            "INSERT INTO transmissions (id, session_id, started_at, duration, audio_path, "
            "transcript, translation, notes, tags, state, hidden) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (f"old{i}", "s", f"2026-01-01T00:{i:02d}:00+00:00", 2.0, f"/rec/{i}.wav",
             f"kept transcript {i} lantern", f"kept translation {i}", "", json.dumps(["k%d" % i]),
             "complete"))
        conn.execute("INSERT INTO transmissions_fts (id, transcript, translation, correction, notes, tags) "
                     "VALUES (?,?,?,?,?,?)", (f"old{i}", f"kept transcript {i} lantern",
                                              f"kept translation {i}", "", "", "k%d" % i))
    # A stale duplicate for old1 and an orphan for a message that is gone.
    conn.execute("INSERT INTO transmissions_fts (id, transcript, translation, correction, notes, tags) "
                 "VALUES ('old1', 'stale duplicate text', '', '', '', '')")
    conn.execute("INSERT INTO transmissions_fts (id, transcript, translation, correction, notes, tags) "
                 "VALUES ('gone', 'orphan phantom', '', '', '', '')")
    # A message saved while the index was unavailable: never indexed.
    conn.execute(
        "INSERT INTO transmissions (id, session_id, started_at, duration, audio_path, transcript, "
        "translation, notes, tags, state, hidden) VALUES ('unindexed', 's', "
        "'2026-01-01T01:00:00+00:00', 2.0, '/rec/u.wav', 'never indexed beacon', '', '', '[]', 'complete', 0)")
    conn.commit()
    conn.close()


def test_an_existing_populated_database_upgrades_with_its_index_preserved(tmp_path):
    database = str(tmp_path / "layout1.sqlite3")
    layout_1_database(database)
    before = sqlite3.connect(database)
    original = {r[0]: tuple(r) for r in before.execute("SELECT * FROM transmissions")}
    before.close()

    store = Store(database, recordings_dir=str(tmp_path))
    assert store.schema_version == SCHEMA_VERSION
    assert store._conn.execute(
        "SELECT value FROM meta WHERE key = 'fts_layout'").fetchone()[0] == str(FTS_LAYOUT)
    # Every message intact, byte for byte.
    now = {r[0]: tuple(r) for r in store._conn.execute("SELECT * FROM transmissions")}
    assert now == original
    # Search results preserved; the orphan and the stale duplicate are gone;
    # the never-indexed message is found now.
    assert found(store, "lantern") == [f"old{i}" for i in range(6)]
    assert found(store, "phantom") == [] and found(store, "stale") == []
    assert found(store, "beacon") == ["unindexed"]
    rows = index_rows(store)
    assert sorted(r["id"] for r in rows) == sorted(f"old{i}" for i in range(6)) + ["unindexed"]
    assert mapping(store) == {r["id"]: r["rowid"] for r in rows}
    # Adopted, not rebuilt: the six original rows keep their rowids.
    assert min(r["rowid"] for r in rows if r["id"].startswith("old")) == 1
    store.close()

    # Reopening does no index work at all.
    again = Store(database, recordings_dir=str(tmp_path))
    trace = Trace(again)
    again.close()
    third = Store(database, recordings_dir=str(tmp_path))
    trace = Trace(third)
    third._migrate()
    assert trace.index_writes() == [], trace.index_writes()
    assert found(third, "lantern") == [f"old{i}" for i in range(6)]
    third.close()


def test_the_schema_3_fixture_gains_a_searchable_index(tmp_path):
    from test_alpha4_thread_and_sessions import schema_3_database

    database = str(tmp_path / "alpha3.sqlite3")
    schema_3_database(database)
    store = Store(database, recordings_dir=str(tmp_path))
    assert store.schema_version == SCHEMA_VERSION
    assert found(store, "transcript") == sorted(f"tx_{run}{index}" for run in range(3) for index in range(2))
    assert found(store, "bravo") == sorted(f"tx_{run}{index}" for run in range(3) for index in range(2))
    assert len(mapping(store)) == 6
    store.close()


def test_a_failed_index_upgrade_claims_nothing_and_recovers_on_the_next_start(tmp_path, monkeypatch):
    database = str(tmp_path / "layout1.sqlite3")
    layout_1_database(database)

    real = Store._reconcile_fts

    def half_way(self):
        real(self)                                     # the map is written ...
        raise sqlite3.OperationalError("disk I/O error (simulated)")   # ... then it fails

    monkeypatch.setattr(Store, "_reconcile_fts", half_way)
    store = Store(database, recordings_dir=str(tmp_path))
    assert store.fts_enabled is False, "a half-built index was put into service"
    assert store._conn.execute("SELECT value FROM meta WHERE key = 'fts_layout'").fetchone() is None
    assert store._conn.execute("SELECT count(*) FROM transmissions_fts_map").fetchone()[0] == 0
    assert store._conn.execute("SELECT count(*) FROM transmissions").fetchone()[0] == 7
    assert store.schema_version == SCHEMA_VERSION      # the rest of the migration stands
    # Search still works, through LIKE, and saving still persists.
    assert found(store, "lantern") == [f"old{i}" for i in range(6)]
    tx = store.get_transmission("old0")
    tx.notes = "written while the index was off"
    store.save_transmission(tx)
    assert store.get_transmission("old0").notes == "written while the index was off"
    store.close()

    monkeypatch.setattr(Store, "_reconcile_fts", real)
    again = Store(database, recordings_dir=str(tmp_path))
    assert again.fts_enabled is True
    assert again._conn.execute("SELECT value FROM meta WHERE key = 'fts_layout'").fetchone()[0] == str(FTS_LAYOUT)
    assert found(again, "lantern") == [f"old{i}" for i in range(6)]
    assert found(again, "written") == ["old0"]        # the note saved meanwhile is indexed now
    again.close()


def test_an_index_rewritten_by_an_older_build_is_reconciled_on_open(tmp_path):
    database = str(tmp_path / "mixed.sqlite3")
    store = Store(database, recordings_dir=str(tmp_path))
    store.save_session(Session(id="s", conversation_id=store.default_conversation().id))
    for i in (70, 71):
        store.save_transmission(record(i, transcript=f"granite {i}"))
    store.close()
    # An older build opens the database and saves tx70 its way: delete by id, insert anew.
    conn = sqlite3.connect(database)
    conn.execute("DELETE FROM transmissions_fts WHERE id = 'tx70'")
    conn.execute("INSERT INTO transmissions_fts (id, transcript, translation, correction, notes, tags) "
                 "VALUES ('tx70', 'granite 70 older build', '', '', '', '')")
    conn.execute("UPDATE transmissions SET transcript = 'granite 70 older build' WHERE id = 'tx70'")
    conn.commit()
    conn.close()

    again = Store(database, recordings_dir=str(tmp_path))
    rows = index_rows(again)
    assert sorted(r["id"] for r in rows) == ["tx70", "tx71"]
    assert mapping(again) == {r["id"]: r["rowid"] for r in rows}
    assert found(again, "older") == ["tx70"] and found(again, "granite") == ["tx70", "tx71"]
    tx = again.get_transmission("tx70")
    tx.transcript = "granite 70 newest"
    again.save_transmission(tx)
    assert found(again, "newest") == ["tx70"] and found(again, "older") == []
    assert len(index_rows(again)) == 2
    again.close()


def test_rebuilding_the_index_recreates_it_from_the_messages(s):
    for i in (80, 81):
        s.save_transmission(record(i, transcript=f"basalt {i}"))
    s.save_transmission(record(82))                    # nothing searchable
    s._conn.execute("INSERT INTO transmissions_fts (id, transcript, translation, correction, notes, tags) "
                    "VALUES ('rogue', 'rogue entry', '', '', '', '')")
    assert s.rebuild_search_index() == 2
    assert sorted(r["id"] for r in index_rows(s)) == ["tx80", "tx81"]
    assert found(s, "basalt") == ["tx80", "tx81"] and found(s, "rogue") == []


# ---- without FTS5 -----------------------------------------------------------------------

def test_operation_without_fts5_is_unchanged(tmp_path, monkeypatch):
    # A SQLite without FTS5 fails the CREATE VIRTUAL TABLE with "no such
    # module"; the same failure, produced by asking for a module that is not there.
    monkeypatch.setattr(storage, "_FTS_SCHEMA",
                        storage._FTS_SCHEMA.replace("USING fts5", "USING fts5_absent"))
    store = Store(str(tmp_path / "nofts.sqlite3"), recordings_dir=str(tmp_path))
    assert store.fts_enabled is False
    tables = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master")}
    assert "transmissions_fts" not in tables and "transmissions_fts_map" not in tables
    store.save_session(Session(id="s", conversation_id=store.default_conversation().id))
    tx = record(90, transcript="obsidian shard")
    store.save_transmission(tx)
    tx.state = ProcessingState.COMPLETE
    store.save_transmission(tx)
    assert found(store, "obsidian") == ["tx90"]
    tx.notes = "flint"
    store.save_transmission(tx)
    assert found(store, "flint") == ["tx90"]
    store.delete_transmission("tx90")
    assert found(store, "obsidian") == []
    assert store.rebuild_search_index() == 0
    assert store.stats()["fts_enabled"] is False
    store.close()


# ---- the production pipeline -----------------------------------------------------------

def test_a_replayed_recording_writes_each_index_entry_only_when_its_text_changes(config, store, fixture_wav):
    """The real app: capture-first saves, then transcription and translation
    on the mock engines. Each message is saved several times; its index entry
    is written once when its transcript arrives and once when its translation
    does - never for a state change."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine

    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    trace = Trace(store)
    app.start_session(replay_path=fixture_wav, name="run")
    app.run_replay()
    deadline = time.time() + 30
    while time.time() < deadline and not all(
            tx.state in (ProcessingState.COMPLETE, ProcessingState.FAILED, ProcessingState.SKIPPED)
            for tx in app.recent_transmissions()):
        time.sleep(0.05)
    app.stop_session()
    store._conn.set_trace_callback(None)
    txs = app.recent_transmissions()
    assert len(txs) >= 3 and all(tx.state is ProcessingState.COMPLETE for tx in txs)
    saves = [x for x in trace.statements if x.startswith("INSERT INTO transmissions (")]
    writes = trace.index_writes()
    assert len(saves) >= 3 * len(txs), "each message is saved at least three times (captured, transcribed, translated)"
    assert len(writes) <= 2 * len(txs), (len(writes), len(txs))
    assert not any(w.startswith("DELETE") for w in writes)
    assert any(tx.translation for tx in txs), "the fixture must include a translated message"
    for tx in txs:
        assert tx.transcript
        assert tx.id in found(store, tx.transcript.split()[0].strip(",."))
        if tx.translation:                  # English source: nothing to translate
            assert tx.id in found(store, tx.translation.split()[0].strip(",."))
    assert set(mapping(store)) == {tx.id for tx in txs}
