"""Store.get_transmission is serialised with the pipeline worker's writes.

Reproduced defect (test_capture_invariant, 3 of 160 runs at bafa55a and
2 of 40 at a862cf1): app.transcribe_anyway() -> force_transcribe() read the
row with an unlocked Store.get_transmission while the worker saved under
Store._lock on the same shared connection. The read raised
sqlite3.InterfaceError ("bad parameter or other API misuse") out of the
operator's action, and the worker logged the row as having vanished and
skipped it. Audio was never modified. Only this read is locked here; the
store's other unlocked reads remain a separate follow-up.
"""

from __future__ import annotations

import threading

from babelfishr.models import Session, Transmission, utcnow


def seeded(store, count=20):
    store.save_session(Session(id="s1", name="one"))
    for i in range(count):
        store.save_transmission(Transmission(id=f"tx_{i}", session_id="s1",
                                             started_at=utcnow(), transcript=f"m {i}"))


def test_get_transmission_waits_for_a_write_in_progress(store):
    seeded(store, 1)
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def writer():                         # stands in for the worker mid-save
        with store._lock:
            held.set()
            release.wait(5)

    def reader():
        assert store.get_transmission("tx_0") is not None
        done.set()

    w = threading.Thread(target=writer)
    w.start()
    assert held.wait(5)
    r = threading.Thread(target=reader)
    r.start()
    try:
        assert not done.wait(0.3), "get_transmission read the connection mid-write"
    finally:
        release.set()
        w.join(5)
        r.join(5)
    assert done.is_set()


def test_reads_beside_continuous_saves_never_fail_or_lose_the_row(store):
    """The reproduced shape without the app around it: a worker-like thread
    saving rows while this thread reads them back."""
    seeded(store)
    stop, errors = threading.Event(), []

    def worker():
        i = 0
        try:
            while not stop.is_set():
                tx = store.get_transmission(f"tx_{i % 20}")
                tx.transcript = f"saved {i}"
                store.save_transmission(tx)
                i += 1
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))

    w = threading.Thread(target=worker, daemon=True)
    w.start()
    missing = 0
    try:
        for n in range(3000):
            if store.get_transmission(f"tx_{n % 20}") is None:
                missing += 1
    finally:
        stop.set()
        w.join(10)
    assert errors == [] and missing == 0, (errors, missing)
