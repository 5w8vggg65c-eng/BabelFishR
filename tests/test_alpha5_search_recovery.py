"""The search index recovers from the two ways it can be left behind the
messages (F6 follow-up).

A. A layout-1 build (71ada52 and earlier) opens a layout-2 database, deletes
   message A and indexes a new message B. FTS5 re-uses A's freed rowid for B,
   so the map's "A -> 1" now names B's row while row count and highest rowid
   still agree. The next start must establish the true associations; editing
   B must remove its former wording and leave one entry.
B. Messages are created, edited, cleared and deleted while FTS5 is
   unavailable (LIKE fallback). Index and map still agree with each other, so
   the next start with FTS5 used to skip reconciliation and search answered
   from the stale index. An existing limitation, present before F6 as well.

The layout-1 build's index maintenance is reproduced here in SQL, statement
for statement, from babelfishr/storage.py at 71ada52 (save_transmission +
_index_fts: DELETE FROM transmissions_fts WHERE id = ? then INSERT ...;
delete_transmission: tombstone, DELETE FROM transmissions, DELETE FROM
transmissions_fts WHERE id = ?). No git history is needed at test time.
FTS5 unavailability is produced at the connection boundary only: a
connection class whose statements against the index fail the way SQLite
fails them when the module is not compiled in ("no such module: fts5");
every Store operation after that is production code.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

import babelfishr.storage as storage
from babelfishr.models import ProcessingState, Session, Transmission
from babelfishr.storage import FTS_LAYOUT, Store, _searchable, _to_row, iso, utcnow

T0 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)


def record(tx_id, text="", **fields) -> Transmission:
    base = dict(id=tx_id, session_id="s", started_at=T0, duration=2.0, audio_path=f"/rec/{tx_id}.wav",
                transcript=text, state=ProcessingState.COMPLETE, target_language="en", source_language="en")
    base.update(fields)
    return Transmission(**base)


def found(store, word):
    return sorted(t.id for t in store.search(word, conversation_id=None))


def index_rows(store):
    return [tuple(r) for r in store._conn.execute(
        "SELECT rowid, id, transcript FROM transmissions_fts ORDER BY rowid")]


def mapping(store):
    return {r[0]: r[1] for r in store._conn.execute("SELECT id, fts_rowid FROM transmissions_fts_map")}


def meta(store, key):
    row = store._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def opened(tmp_path, name="recovery.sqlite3"):
    return Store(str(tmp_path / name), recordings_dir=str(tmp_path / "rec"))


class OldBuild:
    """The layout-1 build's index maintenance, as 71ada52 performed it."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path)

    def save_transmission(self, tx):
        payload = _to_row(tx)
        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k != "id")
        self.conn.execute(f"INSERT INTO transmissions ({columns}) VALUES ({placeholders}) "
                          f"ON CONFLICT(id) DO UPDATE SET {updates}", tuple(payload.values()))
        # _index_fts at 71ada52: delete by id, insert again.
        self.conn.execute("DELETE FROM transmissions_fts WHERE id = ?", (tx.id,))
        self.conn.execute(
            "INSERT INTO transmissions_fts (id, transcript, translation, correction, notes, tags) "
            "VALUES (?,?,?,?,?,?)",
            (tx.id, tx.transcript, tx.translation,
             " ".join(filter(None, (tx.transcript_correction, tx.translation_correction))),
             tx.notes, " ".join(tx.tags)))
        self.conn.commit()

    def delete_transmission(self, tx_id):
        row = self.conn.execute("SELECT session_id FROM transmissions WHERE id = ?", (tx_id,)).fetchone()
        self.conn.execute("INSERT OR REPLACE INTO deleted_transmissions (id, deleted_at, session_id, "
                          "leftover_files) VALUES (?,?,?,?)",
                          (tx_id, iso(utcnow()), row[0] if row else None, "[]"))
        self.conn.execute("DELETE FROM transmissions WHERE id = ?", (tx_id,))
        self.conn.execute("DELETE FROM transmissions_fts WHERE id = ?", (tx_id,))
        self.conn.commit()

    def close(self):
        self.conn.close()


class NoFTS5AtInit(sqlite3.Connection):
    """Codex's reproduction boundary: the CREATE VIRTUAL TABLE script itself
    fails (as it does for a new database without FTS5); everything after
    that runs on ordinary SQLite. Substitutes the initialisation failure only."""

    def executescript(self, script):
        if "fts5" in script:
            raise sqlite3.OperationalError("no such module: fts5")
        return super().executescript(script)


class NoFTS5(sqlite3.Connection):
    """What SQLite does without the FTS5 module: CREATE VIRTUAL TABLE IF NOT
    EXISTS is accepted for a table that already exists, and every statement
    that then touches the table fails with "no such module: fts5"."""

    def execute(self, sql, *args):
        if "transmissions_fts" in sql and "CREATE VIRTUAL TABLE" not in sql:
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, *args)


def seeded(tmp_path):
    store = opened(tmp_path)
    store.save_session(Session(id="s", conversation_id=store.default_conversation().id))
    return store


# ---- A -----------------------------------------------------------------------------------

def test_a_rowid_reused_by_an_older_build_is_reassociated_on_the_next_start(tmp_path):
    store = seeded(tmp_path)
    store.save_transmission(record("A", "albatross"))
    assert index_rows(store) == [(1, "A", "albatross")] and mapping(store) == {"A": 1}
    store.close()

    old = OldBuild(str(tmp_path / "recovery.sqlite3"))
    old.delete_transmission("A")
    old.save_transmission(record("B", "bramble"))
    assert [tuple(r) for r in old.conn.execute("SELECT rowid, id FROM transmissions_fts")] == [(1, "B")]
    assert [tuple(r) for r in old.conn.execute("SELECT id, fts_rowid FROM transmissions_fts_map")] == [("A", 1)]
    old.close()

    store = opened(tmp_path)
    assert mapping(store) == {"B": 1}, "the stale A -> 1 association was accepted"
    assert index_rows(store) == [(1, "B", "bramble")]
    assert found(store, "bramble") == ["B"] and found(store, "albatross") == []
    b = store.get_transmission("B")
    b.transcript = "cobalt"
    store.save_transmission(b)
    assert index_rows(store) == [(1, "B", "cobalt")], "editing B left a duplicate entry"
    assert found(store, "bramble") == [] and found(store, "cobalt") == ["B"]
    store.close()

    again = opened(tmp_path)
    assert index_rows(again) == [(1, "B", "cobalt")] and mapping(again) == {"B": 1}
    assert found(again, "bramble") == [] and found(again, "cobalt") == ["B"]
    assert again.get_transmission("A") is None and again.is_deleted("A")
    again.close()


def test_a_wrong_association_met_during_a_save_or_deletion_touches_no_other_message(tmp_path):
    """The guard on the save and deletion paths themselves. The map is
    corrupted directly here - not the sequence of A, which the start-up
    check catches - so this is a test of the per-operation guard alone."""
    store = seeded(tmp_path)
    store.save_transmission(record("P", "peregrine"))
    store.save_transmission(record("Q", "quail"))
    rows = {r[1]: r[0] for r in index_rows(store)}
    store._conn.execute("UPDATE transmissions_fts_map SET fts_rowid = -1 WHERE id = 'P'")
    store._conn.execute("UPDATE transmissions_fts_map SET fts_rowid = ? WHERE id = 'Q'", (rows["P"],))
    store._conn.execute("UPDATE transmissions_fts_map SET fts_rowid = ? WHERE id = 'P'", (rows["Q"],))
    store._conn.commit()

    p = store.get_transmission("P")
    p.transcript = "peregrine returns"
    store.save_transmission(p)                                # must not overwrite Q's row
    assert found(store, "quail") == ["Q"] and found(store, "returns") == ["P"]
    assert found(store, "peregrine") == ["P"]
    store.delete_transmission("Q")                           # must not delete P's row
    assert found(store, "quail") == [] and found(store, "returns") == ["P"]
    assert sorted(r[1] for r in index_rows(store)) == ["P"]
    assert mapping(store) == {r[1]: r[0] for r in index_rows(store)}


# ---- B -----------------------------------------------------------------------------------

def test_writes_made_while_fts5_is_unavailable_are_indexed_when_it_returns(tmp_path, monkeypatch):
    # A layout-2 database with a healthy index, made with FTS5 present.
    store = seeded(tmp_path)
    store.save_transmission(record("A", "obsolete"))
    store.save_transmission(record("C", "clearable", notes="keep me"))
    store.save_transmission(record("D", "doomed"))
    assert meta(store, "fts_layout") == str(FTS_LAYOUT) and meta(store, "fts_stale") is None
    store.close()

    # FTS5 gone: the store falls back to LIKE, and everything it writes is
    # recorded as leaving the index behind.
    original_connect = sqlite3.connect
    monkeypatch.setattr(storage.sqlite3, "connect",
                        lambda path, **kw: original_connect(path, factory=NoFTS5, **kw))
    store = opened(tmp_path)
    assert store.fts_enabled is False
    a = store.get_transmission("A")
    a.transcript = "replacement"
    store.save_transmission(a)                                  # edit
    assert meta(store, "fts_stale") == "1", "the first fallback save left no mark"
    store.save_transmission(record("B", "juniper"))             # create
    c = store.get_transmission("C")
    c.transcript, c.notes = "", ""                              # clear everything searchable
    store.save_transmission(c)
    store.delete_transmission("D")                              # delete
    assert found(store, "replacement") == ["A"] and found(store, "juniper") == ["B"]
    assert found(store, "clearable") == [] and found(store, "doomed") == []
    assert meta(store, "fts_stale") == "1"
    store.close()

    # FTS5 back: the first start reconciles from the messages before search
    # is answered from the index.
    monkeypatch.setattr(storage.sqlite3, "connect", original_connect)
    store = opened(tmp_path)
    assert store.fts_enabled is True
    assert meta(store, "fts_stale") is None and meta(store, "fts_layout") == str(FTS_LAYOUT)
    assert found(store, "replacement") == ["A"] and found(store, "obsolete") == []
    assert found(store, "juniper") == ["B"]
    assert found(store, "clearable") == [] and found(store, "keep") == []
    assert found(store, "doomed") == [] and store.is_deleted("D")
    assert sorted(r[1] for r in index_rows(store)) == ["A", "B"]
    assert mapping(store) == {r[1]: r[0] for r in index_rows(store)}
    assert store.get_transmission("A").transcript == "replacement"
    assert store.get_transmission("C").transcript == "" and store.get_transmission("C").notes == ""
    store.close()

    # And a start after that does no index work.
    store = opened(tmp_path)
    statements = []
    store._conn.set_trace_callback(statements.append)
    store._migrate()
    writes = [x for x in statements if x.startswith(("INSERT INTO transmissions_fts", "UPDATE transmissions_fts",
                                                     "DELETE FROM transmissions_fts"))]
    assert writes == [], writes
    store.close()


def test_a_deletion_made_while_fts5_is_unavailable_marks_the_index_stale(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    store.save_transmission(record("D", "doomed"))
    store.save_transmission(record("K", "kept"))
    store.close()
    real_connect = sqlite3.connect
    monkeypatch.setattr(storage.sqlite3, "connect",
                        lambda path, **kw: real_connect(path, factory=NoFTS5, **kw))
    store = opened(tmp_path)
    assert store.fts_enabled is False and meta(store, "fts_stale") is None
    store.delete_transmission("D")                              # the only write
    assert meta(store, "fts_stale") == "1"
    store.close()
    monkeypatch.setattr(storage.sqlite3, "connect", real_connect)
    store = opened(tmp_path)
    assert found(store, "doomed") == [] and found(store, "kept") == ["K"]
    assert sorted(r[1] for r in index_rows(store)) == ["K"] and meta(store, "fts_stale") is None
    store.close()


def test_writes_made_after_an_initialisation_failure_are_indexed_when_fts5_returns(tmp_path,
                                                                                    monkeypatch):
    """The exact sequence Codex reproduced on 26b7b17 and 71ada52: the FTS
    schema step fails, the store falls back to LIKE, A is edited and B is
    created, FTS5 returns - and search used to answer "obsolete" for A and
    nothing for "replacement" or "juniper", because index and map still
    agreed with each other."""
    store = seeded(tmp_path)
    store.save_transmission(record("A", "obsolete"))
    assert meta(store, "fts_layout") == str(FTS_LAYOUT)
    store.close()
    real_connect = sqlite3.connect
    monkeypatch.setattr(storage.sqlite3, "connect",
                        lambda path, **kw: real_connect(path, factory=NoFTS5AtInit, **kw))
    store = opened(tmp_path)
    assert store.fts_enabled is False
    a = store.get_transmission("A")
    a.transcript = "replacement"
    store.save_transmission(a)
    store.save_transmission(record("B", "juniper"))
    assert found(store, "replacement") == ["A"] and found(store, "juniper") == ["B"]
    store.close()
    monkeypatch.setattr(storage.sqlite3, "connect", real_connect)
    store = opened(tmp_path)
    assert store.fts_enabled is True
    assert found(store, "replacement") == ["A"], "the edit made during fallback is not searchable"
    assert found(store, "juniper") == ["B"], "the message created during fallback is not searchable"
    assert found(store, "obsolete") == [], "search answers from a stale index"
    assert store.get_transmission("A").transcript == "replacement"
    store.close()


def test_an_existing_index_whose_module_is_missing_is_detected_as_unavailable(tmp_path, monkeypatch):
    """CREATE VIRTUAL TABLE IF NOT EXISTS succeeds for an existing table
    whatever module it names; only a statement that touches the table shows
    that FTS5 is not there. Without the probe the store believed it had FTS5
    and the first save raised."""
    store = seeded(tmp_path)
    store.save_transmission(record("A", "present"))
    store.close()
    real_connect = sqlite3.connect
    monkeypatch.setattr(storage.sqlite3, "connect",
                        lambda path, **kw: real_connect(path, factory=NoFTS5, **kw))
    store = opened(tmp_path)
    assert store.fts_enabled is False
    store.save_transmission(record("B", "arrives"))             # no error
    assert found(store, "arrives") == ["B"] and found(store, "present") == ["A"]
    store.close()


def test_a_failed_recovery_keeps_the_stale_mark_and_the_next_start_retries(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    store.save_transmission(record("A", "obsolete"))
    store.close()
    real_connect = sqlite3.connect
    monkeypatch.setattr(storage.sqlite3, "connect",
                        lambda path, **kw: real_connect(path, factory=NoFTS5, **kw))
    store = opened(tmp_path)
    a = store.get_transmission("A")
    a.transcript = "replacement"
    store.save_transmission(a)
    assert meta(store, "fts_stale") == "1"
    store.close()
    monkeypatch.setattr(storage.sqlite3, "connect", real_connect)

    real = Store._reconcile_fts

    def half_way(self):
        real(self)
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(Store, "_reconcile_fts", half_way)
    rows_before = [tuple(r) for r in sqlite3.connect(str(tmp_path / "recovery.sqlite3"))
                   .execute("SELECT * FROM transmissions ORDER BY id")]
    store = opened(tmp_path)
    assert store.fts_enabled is False, "a stale index was put into service after a failed recovery"
    assert meta(store, "fts_stale") == "1", "the stale mark was cleared by a failed recovery"
    assert index_rows(store) == [(1, "A", "obsolete")], "the failed recovery half-wrote the index"
    assert found(store, "replacement") == ["A"]                 # LIKE, from the messages
    assert [tuple(r) for r in store._conn.execute("SELECT * FROM transmissions ORDER BY id")] == rows_before
    store.close()

    monkeypatch.setattr(Store, "_reconcile_fts", real)
    store = opened(tmp_path)
    assert store.fts_enabled is True and meta(store, "fts_stale") is None
    assert found(store, "replacement") == ["A"] and found(store, "obsolete") == []
    assert index_rows(store) == [(1, "A", "replacement")]
    store.close()


def test_a_healthy_start_reads_the_index_and_writes_nothing(tmp_path):
    store = seeded(tmp_path)
    for i in range(30):
        store.save_transmission(record(f"m{i}", f"message {i}"))
    store.close()
    store = opened(tmp_path)
    statements = []
    store._conn.set_trace_callback(statements.append)
    store._migrate()
    assert not any(x.startswith(("INSERT INTO transmissions_fts", "UPDATE transmissions_fts",
                                 "DELETE FROM transmissions_fts")) for x in statements), statements
    assert store._fts_index_consistent()
    store.close()
