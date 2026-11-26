"""Session reads never show a row its writer later rolls back.

The Store shares one SQLite connection across threads. On that connection a
reader sees the writer's uncommitted changes, so an unlocked SELECT could
put a row on screen that the writer then rolls back. The active thread
(conversation_transmissions) and the Removed view (hidden_transmissions)
therefore select and fetch under the Store's RLock, the lock every write
holds until its commit or rollback.

Each test runs a writer thread and a reader thread on the same Store: the
writer opens a synthetic write transaction under the lock (a new row, plus
a flipped removed-from-view flag), the reader asks for the view meanwhile,
the writer rolls back, and the reader must return exactly the committed
rows. Temporary database only.
"""

from __future__ import annotations

import threading

import pytest

from babelfishr.models import Session, Transmission, utcnow
from babelfishr.storage import Store, _to_row


@pytest.fixture
def seeded(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"), recordings_dir=str(tmp_path / "rec"))
    conversation = store.create_conversation("Ops")
    session = store.save_session(Session(conversation_id=conversation.id))
    shown = store.save_transmission(Transmission(session_id=session.id,
                                                 started_at=utcnow(), transcript="shown"))
    removed = store.save_transmission(Transmission(session_id=session.id,
                                                   started_at=utcnow(), transcript="removed",
                                                   hidden=True))
    yield store, conversation.id, session.id, shown, removed
    store.close()


def _uncommitted_changes(store, session_id, new_hidden, flip_id, flip_to):
    """Inside the caller's lock: one new row and one flipped flag, uncommitted."""
    row = _to_row(Transmission(session_id=session_id, started_at=utcnow(),
                               transcript="rolled back", hidden=new_hidden))
    store._conn.execute(
        f"INSERT INTO transmissions ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
    store._conn.execute("UPDATE transmissions SET hidden = ? WHERE id = ?",
                        (int(flip_to), flip_id))
    return row["id"]


def _race(store, read, write):
    """Writer thread opens a transaction under the lock; the reader thread
    reads meanwhile; the writer rolls back. Returns what the reader saw and
    whether it returned before the rollback."""
    in_transaction, rolled_back = threading.Event(), threading.Event()
    read_started, release = threading.Event(), threading.Event()
    seen, early = {}, {}

    def writer():
        with store._lock:
            seen["new_id"] = write()
            seen["uncommitted"] = [t.id for t in read()]     # the writer's own view
            in_transaction.set()
            release.wait(5)
            store._conn.rollback()
            rolled_back.set()

    def reader():
        in_transaction.wait(5)
        read_started.set()
        seen["reader"] = [t.id for t in read()]
        early["before_rollback"] = not rolled_back.is_set()

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    read_started.wait(5)
    for _ in range(20):                       # give an unlocked read every chance
        threading.Event().wait(0.01)
    release.set()
    for thread in threads:
        thread.join(10)
    assert not any(t.is_alive() for t in threads)
    return seen, early["before_rollback"]


def test_the_active_thread_never_shows_a_rolled_back_row(seeded):
    store, conversation, session, shown, removed = seeded
    read = lambda: store.conversation_transmissions(conversation)          # noqa: E731
    seen, before_rollback = _race(
        store, read, lambda: _uncommitted_changes(store, session, False, removed.id, False))
    # The hazard is real: on the shared connection the transaction is visible.
    assert set(seen["uncommitted"]) == {shown.id, removed.id, seen["new_id"]}
    assert not before_rollback, "the read did not wait for the writer"
    assert seen["reader"] == [shown.id], "showed a row the writer rolled back"
    assert [t.id for t in read()] == [shown.id]


def test_the_removed_view_never_shows_a_rolled_back_row(seeded):
    store, conversation, session, shown, removed = seeded
    read = lambda: store.hidden_transmissions(conversation)                # noqa: E731
    seen, before_rollback = _race(
        store, read, lambda: _uncommitted_changes(store, session, True, shown.id, True))
    assert set(seen["uncommitted"]) == {shown.id, removed.id, seen["new_id"]}
    assert not before_rollback, "the read did not wait for the writer"
    assert seen["reader"] == [removed.id], "showed a row the writer rolled back"
    assert [t.id for t in read()] == [removed.id]


def test_a_committed_write_is_seen_after_it_commits(seeded):
    """The lock delays the reader; it does not hide committed work."""
    store, conversation, session, shown, removed = seeded
    added = store.save_transmission(Transmission(session_id=session,
                                                 started_at=utcnow(), transcript="kept"))
    assert [t.id for t in store.conversation_transmissions(conversation)] == [shown.id,
                                                                            added.id]
    assert [t.id for t in store.hidden_transmissions(conversation)] == [removed.id]
