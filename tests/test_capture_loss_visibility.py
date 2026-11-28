"""Audio lost during capture stays visible, attributed and exported.

Builds on test_capture_overflow (the counting and recording of buffer
overflows). Here:

* driver-reported input overflows (PortAudio ``input_overflow``) are counted
  in the callback with plain counters and recorded per run by the capture
  thread with an unknown duration - never a made-up one;
* the main window shows a persistent indicator for a run with loss that an
  ordinary status message does not replace, with the totals in its
  accessible description; it comes back from the store when the Session is
  reopened, and goes away for a new run that loses nothing;
* the timeline says it per run, and per message only where the gap is known
  to fall inside that message;
* the Markdown, CSV and JSON exports carry it with the same attribution;
* a database from before any of this opens and exports cleanly.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import time

import numpy as np
import pytest

from babelfishr import export
from babelfishr.app import BabelFishRApp
from babelfishr.models import (GAP_DRIVER_OVERFLOW, CaptureGap, Session,
                               Transmission, capture_loss_totals,
                               message_capture_loss, utcnow)
from babelfishr.storage import SCHEMA_VERSION, Store
from babelfishr.testing import build_fixture

from test_capture_overflow import (SR, _callback, _held_capture, _live_source,
                                   _paced, _wait)

BLOCK = 960


class _Overflow:
    """A PortAudio CallbackFlags stand-in reporting an input overflow."""

    input_overflow = True

    def __bool__(self):
        return True

    def __str__(self):
        return "input overflow"


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(qt_app, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    qt_app.processEvents()
    return predicate()


def _stall(h, blocks, at, dropped):
    """The consumer stalls on block ``at``; ``4 + dropped`` more arrive, so
    exactly ``dropped`` blocks are discarded (queue of 4)."""
    h.gate["armed"] = True
    _callback(h.source, blocks[at])
    assert h.entered.wait(10.0)
    for b in blocks[at + 1:at + 5 + dropped]:
        _callback(h.source, b)
    h.entered.clear()
    h.release.set()
    assert _wait(lambda: h.source._queue.qsize() == 0)
    h.release.clear()
    return at + 5 + dropped


def _run_with_loss(monkeypatch, config, store):
    """A real monitoring run (the real callback, queue, capture thread,
    detector, recorder and store; mock engines) that loses audio three ways:

    * 5 blocks mid-voice - located, inside the one transmission;
    * 3 blocks in the quiet long after it ended - located, in no message;
    * two driver-reported input overflows - position and duration unknown;
    and a second, short transmission later that loses nothing.
    """
    h = _held_capture(monkeypatch, config, store, block_size=BLOCK, queue_blocks=4)
    h.app.begin_capture()
    audio = np.concatenate([
        build_fixture([{"gap": 0.4}], sample_rate=SR).audio,
        build_fixture([{"kind": "voice", "duration": 3.0, "level_dbfs": -14}],
                      sample_rate=SR).audio,
        build_fixture([{"gap": 6.0}], sample_rate=SR).audio,
        build_fixture([{"kind": "voice", "duration": 1.0, "level_dbfs": -14}],
                      sample_rate=SR).audio,
        build_fixture([{"gap": 4.0}], sample_rate=SR).audio])
    audio = audio[:audio.size // BLOCK * BLOCK].astype(np.float32).astype(np.float64)
    blocks = [audio[i:i + BLOCK] for i in range(0, audio.size, BLOCK)]
    try:
        _paced(h, np.concatenate(blocks[:30]), BLOCK)
        _callback(h.source, blocks[30], status=_Overflow())   # driver overflow 1
        assert _wait(lambda: h.source._queue.qsize() == 0)
        _paced(h, np.concatenate(blocks[31:80]), BLOCK)
        after = _stall(h, blocks, 80, 5)                       # mid-voice
        _paced(h, np.concatenate(blocks[after:430]), BLOCK)
        after = _stall(h, blocks, 430, 3)                      # quiet, no message
        _callback(h.source, blocks[after], status=_Overflow())  # driver overflow 2
        assert _wait(lambda: h.source._queue.qsize() == 0)
        _paced(h, np.concatenate(blocks[after + 1:]), BLOCK)
        h.app.stop_session()
        assert _wait(lambda: h.app.outstanding_work() == 0 and h.stream.closed.is_set())
        h.app.wait_for_session_ends(10.0)
    finally:
        h.release.set()
    survivors = blocks[:81] + blocks[86:431] + blocks[434:]
    return h, survivors


def _carrying(store, session_id):
    rows = store.list_transmissions(session_id=session_id, limit=1000)
    return rows, [t for t in rows if (t.signal_metadata or {}).get("capture_gaps")]


# --- the record --------------------------------------------------------------------

def test_driver_overflows_are_persisted_with_unknown_duration_and_capture_continues(
        monkeypatch, config, store):
    h, survivors = _run_with_loss(monkeypatch, config, store)

    # Capture kept going after each gap: every surviving frame, in order.
    assert np.array_equal(np.concatenate(h.handled), np.concatenate(survivors))
    assert h.source.overflow_count == 2

    gaps = store.capture_gaps(h.session.id)
    measured = [g for g in gaps if g.reason != GAP_DRIVER_OVERFLOW]
    driver = [g for g in gaps if g.reason == GAP_DRIVER_OVERFLOW]
    assert [(g.start_frame, g.frames) for g in measured] == [
        (81 * BLOCK, 5 * BLOCK), (431 * BLOCK, 3 * BLOCK)]
    # Never a fabricated duration: unknown, not zero, and not in the totals.
    assert sum(g.overflows for g in driver) == 2
    assert all(g.frames is None and g.duration is None and g.start_frame is None
               for g in driver)
    assert h.capture.dropped_frames == h.source.dropped_frames == 8 * BLOCK
    assert h.capture.driver_overflows == 2

    run = store.get_session(h.session.id)
    assert run.dropped_frames == 8 * BLOCK
    assert run.dropped_seconds == pytest.approx(8 * BLOCK / SR)
    assert run.driver_overflows == 2
    d = run.to_dict()
    assert d["driver_overflows"] == 2
    assert [g["frames"] for g in d["capture_gaps"]
            if g["reason"] == GAP_DRIVER_OVERFLOW] == [None] * len(driver)
    assert Session.from_dict(json.loads(json.dumps(d))).driver_overflows == 2

    # Attribution: only the mid-voice gap is on a message.
    rows, carrying = _carrying(store, h.session.id)
    assert len(carrying) == 1
    assert [g["start_frame"] for g in carrying[0].signal_metadata["capture_gaps"]] == [
        81 * BLOCK]
    assert message_capture_loss(carrying[0]).note() == (
        "Audio lost during capture: 0.10 s (1 gap).")
    assert all(not message_capture_loss(t) for t in rows if t is not carrying[0])

    # The operator was told about the driver's overflows too.
    assert sum(e.get("driver_overflows", 0) for e in h.events) == 2
    assert any("input overflow" in e["message"] for e in h.events)
    h.app.close()


def test_the_callback_only_counts_driver_overflows(monkeypatch):
    """The record is made on the reading side: the callback increments a
    counter; take_capture_gaps() turns it into one coalesced record."""
    source, _ = _live_source(monkeypatch, block_size=480, max_queue_blocks=8)
    source.start()
    for i in range(3):
        _callback(source, np.zeros(480), status=_Overflow())
    assert source.overflow_count == 3
    (record,) = source.take_capture_gaps()
    assert (record.reason, record.overflows, record.frames) == (GAP_DRIVER_OVERFLOW, 3, None)
    _callback(source, np.zeros(480), status=_Overflow())
    assert source.take_capture_gaps() == []          # coalesced within the interval
    (rest,) = source.take_capture_gaps(final=True)   # the end takes the rest
    assert rest.overflows == 1
    assert source.take_capture_gaps(final=True) == []
    source.stop()


def test_an_unplaced_gap_at_the_end_is_not_put_on_the_open_message(
        monkeypatch, config, store):
    """Frames counted at the drop that no read could place may have fallen
    anywhere: the run records them, no message claims them."""
    h = _held_capture(monkeypatch, config, store, block_size=BLOCK, queue_blocks=2)
    monkeypatch.setattr(h.capture, "_drain_source", lambda: None)
    voice = build_fixture([{"kind": "voice", "duration": 3.0, "level_dbfs": -14}],
                          sample_rate=SR).audio
    voice = voice[:voice.size // BLOCK * BLOCK].astype(np.float32).astype(np.float64)
    blocks = [voice[i:i + BLOCK] for i in range(0, voice.size, BLOCK)]
    h.app.begin_capture()
    try:
        _paced(h, np.concatenate(blocks[:40]), BLOCK)      # a transmission is open
        assert _wait(lambda: h.capture.detector.open)
        h.gate["armed"] = True
        _callback(h.source, blocks[40])
        assert h.entered.wait(10.0)
        for b in blocks[41:46]:                            # 2 queue, 3 dropped
            _callback(h.source, b)
        h.capture._running = False
        h.app.stop_session()
        h.release.set()
        assert _wait(lambda: h.stream.closed.is_set())
        assert _wait(lambda: h.app.outstanding_work() == 0)
        h.app.wait_for_session_ends(10.0)
    finally:
        h.release.set()
    gaps = store.capture_gaps(h.session.id)
    assert [g.start_frame for g in gaps] == [None]
    rows, carrying = _carrying(store, h.session.id)
    assert rows and carrying == []
    md = export.to_markdown(rows, store.get_session(h.session.id))
    assert "- **Capture loss:** Audio lost during capture: 0.06 s (1 gap)." in md
    assert md.count("Capture loss") == 1                   # run level only
    h.app.close()


# --- the window ------------------------------------------------------------------

def test_the_indicator_persists_over_status_messages_and_clears_for_a_clean_run(
        monkeypatch, qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    h = _held_capture(monkeypatch, config, store, block_size=480, queue_blocks=2)
    window = MainWindow(h.app)
    window.show()
    badge = window.capture_loss_badge
    try:
        assert _pump(qt_app, lambda: True) and badge.isHidden()
        h.source.start()
        for i in range(5):                                 # 2 queue, 3 dropped
            _callback(h.source, np.zeros(480), status=_Overflow() if i == 4 else None)
        h.capture._drain_source()
        assert _pump(qt_app, lambda: badge.isVisible())
        assert badge.text().endswith("Audio lost 0.03 s + driver overflow")
        description = badge.accessibleDescription()
        assert badge.accessibleName() == "Audio lost during capture"
        for part in ("0.030 s", "1,440 frames", "1 gap", "1 overflow",
                     "duration unknown"):
            assert part in description
        assert badge.toolTip() == description

        # Ordinary status messages - typed, and from an ordinary audio event -
        # replace the status bar's message, not the indicator.
        window.status.showMessage("Exported something", 8000)
        h.app.events.publish("audio-status", {"kind": "connected",
                                              "message": "capturing from stub"})
        assert _pump(qt_app, lambda: "connected" in window.status.currentMessage())
        assert badge.isVisible() and badge.accessibleDescription() == description
        assert window.timeline.run_note_lines and "(now)" in window.timeline.run_note_lines[0]
    finally:
        h.app.stop_session()
        h.app.wait_for_session_ends(10.0)
    # The run is over; it is still this Session's latest run, and it lost audio.
    assert _pump(qt_app, lambda: "latest monitoring run" in badge.accessibleDescription())
    assert badge.isVisible()

    # A new run starts its own account: nothing lost, nothing shown.
    clean, _ = _live_source(monkeypatch, block_size=480, max_queue_blocks=8)
    h.app.start_session(source=clean, name="clean", target_language="fr")
    try:
        assert _pump(qt_app, lambda: badge.isHidden())
        # The earlier run's note stays in the thread.
        assert len(window.timeline.run_note_lines) == 1
        assert "(now)" not in window.timeline.run_note_lines[0]
    finally:
        h.app.stop_session()
        h.app.wait_for_session_ends(10.0)
        window.close()
        h.app.close()


def test_reopening_shows_the_indicator_and_timeline_notes_from_the_store(
        monkeypatch, qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    h, _ = _run_with_loss(monkeypatch, config, store)
    session_id = h.session.id
    _rows, (carrier,) = _carrying(store, session_id)
    h.app.close()
    store.close()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    app = BabelFishRApp(config=config, store=reopened)
    window = MainWindow(app)
    window.show()
    try:
        badge = window.capture_loss_badge
        assert _pump(qt_app, lambda: badge.isVisible())
        description = badge.accessibleDescription()
        for part in ("0.160 s", f"{8 * BLOCK:,} frames", "2 gaps", "2 overflows",
                     "duration unknown"):
            assert part in description
        (note,) = window.timeline.run_note_lines
        assert note.endswith(
            ": Audio lost during capture: 0.16 s (2 gaps); the input driver reported "
            "2 overflows (audio lost, duration unknown).")
        assert window.timeline.run_notes.isVisibleTo(window)
        assert window.timeline.run_notes.accessibleDescription() == note

        bubble = window.timeline.bubble(carrier.id)
        assert bubble is not None
        assert bubble.capture_loss_note == "Audio lost during capture: 0.10 s (1 gap)."
        assert bubble.capture_loss_label.isVisibleTo(window)
        assert bubble.capture_loss_label.accessibleName() == "Audio lost during capture"
        assert bubble.capture_loss_note in bubble.accessibleDescription()
        others = [window.timeline.bubble(i) for i in window.timeline.order()
                  if i != carrier.id]
        assert others and all(b.capture_loss_note == "" and b.capture_loss_label.isHidden()
                              for b in others)
        # Selecting the row keeps the note in what a screen reader hears.
        window.timeline.select(carrier.id)
        assert bubble.accessibleDescription().startswith("Selected")
        assert bubble.capture_loss_note in bubble.accessibleDescription()
    finally:
        window.close()
        app.close()
        reopened.close()


# --- exports ---------------------------------------------------------------------

def test_exports_carry_the_loss_with_its_attribution(monkeypatch, config, store, tmp_path):
    h, _ = _run_with_loss(monkeypatch, config, store)
    run = store.get_session(h.session.id)
    rows, (carrier,) = _carrying(store, h.session.id)
    rows.sort(key=lambda t: (t.started_at, t.id))

    md = export.to_markdown(rows, run)
    header, _, body = md.partition("## ")
    assert ("- **Capture loss:** Audio lost during capture: 0.16 s (2 gaps); the "
            "input driver reported 2 overflows (audio lost, duration unknown).") in header
    # Per message: only the located gap inside it, never the run's others.
    assert body.count("**Capture loss:**") == 1
    section = body.split("### ")[1 + [t.id for t in rows].index(carrier.id)]
    assert "**Capture loss:** Audio lost during capture: 0.10 s (1 gap)." in section

    table = list(csv.reader(io.StringIO(export.to_csv(rows))))
    columns = table[0]
    assert columns[-2:] == ["capture_loss_seconds", "capture_loss_note"]
    assert columns[:28] == export.CSV_COLUMNS[:28]
    assert columns.index("withheld_guess") == 27       # existing positions kept
    by_id = {r[0]: dict(zip(columns, r)) for r in table[1:]}
    assert by_id[carrier.id]["capture_loss_seconds"] == "0.1"
    assert by_id[carrier.id]["capture_loss_note"] == (
        "Audio lost during capture: 0.10 s (1 gap).")
    assert all(r["capture_loss_seconds"] == "" and r["capture_loss_note"] == ""
               for i, r in by_id.items() if i != carrier.id)

    payload = json.loads(export.to_json(rows, run))
    assert payload["session"]["driver_overflows"] == 2
    assert payload["session"]["dropped_frames"] == 8 * BLOCK
    reasons = [g["reason"] for g in payload["session"]["capture_gaps"]]
    assert reasons.count(GAP_DRIVER_OVERFLOW) >= 1
    assert all(g["duration"] is None for g in payload["session"]["capture_gaps"]
               if g["reason"] == GAP_DRIVER_OVERFLOW)
    tx_json = {t["id"]: t for t in payload["transmissions"]}
    assert tx_json[carrier.id]["signal_metadata"]["capture_gaps"][0]["frames"] == 5 * BLOCK

    # The bundle's human-readable files say the same.
    bundle = export.export_session(store, h.session.id, str(tmp_path / "bundle"),
                                   include_audio=False)
    transcript = (tmp_path / "bundle" / "transcript.md").read_text(encoding="utf-8")
    assert bundle.endswith("bundle")
    assert "- **Capture loss:** Audio lost during capture: 0.16 s (2 gaps)" in transcript
    assert transcript.count("**Capture loss:**") == 2
    h.app.close()


def test_a_named_session_export_totals_every_run(store):
    """A Session spanning runs: the header totals all of them and says which
    run lost what; a run without loss adds nothing."""
    conv = store.default_conversation().id
    first = store.save_session(Session(name="a", conversation_id=conv))
    second = store.save_session(Session(name="b", conversation_id=conv))
    third = store.save_session(Session(name="c", conversation_id=conv))
    store.add_capture_gap(CaptureGap(session_id=first.id, frames=4800,
                                     sample_rate=48_000, start_frame=None))
    store.add_capture_gap(CaptureGap(session_id=second.id, frames=None,
                                     sample_rate=48_000,
                                     reason=GAP_DRIVER_OVERFLOW, overflows=3))
    runs = store.sessions_for_conversation(conv)
    assert {r.id for r in runs} == {first.id, second.id, third.id}
    md = export.to_markdown([], store.get_session(third.id), runs=runs)
    assert ("- **Capture loss:** Audio lost during capture: 0.10 s (1 gap); the "
            "input driver reported 3 overflows (audio lost, duration unknown).") in md
    assert md.count("  - Monitoring run started") == 2
    # Without the other runs, the clean run's export has no loss line at all.
    assert "Capture loss" not in export.to_markdown([], store.get_session(third.id))


# --- legacy databases --------------------------------------------------------------

def _legacy(path, drop_table):
    conn = sqlite3.connect(path)
    if drop_table:                                    # schema 8: no capture_gaps
        conn.execute("DROP TABLE capture_gaps")
        version = "8"
    else:                                             # schema 9: no overflows column
        conn.execute("ALTER TABLE capture_gaps DROP COLUMN overflows")
        version = "9"
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                 (version,))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("drop_table", [False, True], ids=["schema9", "schema8"])
def test_a_legacy_database_opens_and_exports_cleanly(config, tmp_path, drop_table):
    path = str(tmp_path / "legacy.sqlite3")
    with Store(path) as fresh:
        run = fresh.save_session(Session(name="old run"))
        fresh.save_transmission(Transmission(session_id=run.id, transcript="hello",
                                             started_at=utcnow()))
        if not drop_table:
            fresh.add_capture_gap(CaptureGap(session_id=run.id, frames=960,
                                             sample_rate=48_000, start_frame=4800))
    _legacy(path, drop_table)
    if not drop_table:
        conn = sqlite3.connect(path)
        assert "overflows" not in {r[1] for r in conn.execute(
            "PRAGMA table_info(capture_gaps)")}
        conn.close()

    with Store(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        loaded = store.get_session(run.id)
        assert loaded.driver_overflows == 0
        rows = store.list_transmissions(session_id=run.id)
        md = export.to_markdown(rows, loaded)
        table = list(csv.reader(io.StringIO(export.to_csv(rows))))
        payload = json.loads(export.to_json(rows, loaded))
        if drop_table:
            assert loaded.capture_gaps == [] and "Capture loss" not in md
        else:
            (gap,) = store.capture_gaps(run.id)
            assert (gap.frames, gap.overflows, gap.reason) == (960, 0, "buffer-overflow")
            assert "- **Capture loss:** Audio lost during capture: 0.02 s (1 gap)." in md
        # No message carries an attribution the old run never recorded.
        assert table[1][-2:] == ["", ""]
        assert payload["session"]["driver_overflows"] == 0
        # The new kind can be written to the migrated database.
        store.add_capture_gap(CaptureGap(session_id=run.id, frames=None,
                                         sample_rate=48_000,
                                         reason=GAP_DRIVER_OVERFLOW, overflows=1))
        assert store.get_session(run.id).driver_overflows == 1


def test_legacy_gap_dicts_without_new_keys_still_sum():
    totals = capture_loss_totals([{"frames": 4800, "sample_rate": 48_000}])
    assert (totals.gaps, totals.seconds, totals.driver_overflows) == (1, 0.1, 0)
