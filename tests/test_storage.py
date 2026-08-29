"""Storage, search, filtering, retention and the review queue."""

from __future__ import annotations

import datetime as dt

import pytest

from babelfishr.models import (ProcessingState, RadioProfile, Session,
                               SourceLanguageMode, Transmission, utcnow)
from babelfishr.storage import Store


@pytest.fixture
def populated(store):
    session = store.save_session(Session(name="s", audio_device="USB Audio"))
    rows = [
        ("hola equipo en posicion", "team in position", "es", 0.92, ["field"]),
        ("achtung strassensperre", "attention roadblock", "de", 0.35, []),
        ("radio check over", "", "en", 0.88, ["net"]),
    ]
    made = []
    for index, (text, translation, language, confidence, tags) in enumerate(rows):
        tx = Transmission(
            session_id=session.id, transcript=text, translation=translation,
            source_language=language, transcript_confidence=confidence, tags=tags,
            channel_name="GMRS 16", frequency_mhz=462.575,
            state=ProcessingState.COMPLETE)
        tx.started_at = utcnow() + dt.timedelta(seconds=index)
        tx.finish()
        store.save_transmission(tx)
        made.append(tx)
    return session, made


def test_schema_is_versioned(store):
    assert store.schema_version >= 1


@pytest.fixture
def session(store):
    """Transmissions reference a session by foreign key, so make a real one."""
    return store.save_session(Session(name="fixture-session"))


def test_transmission_round_trips(store, session):
    tx = Transmission(session_id=session.id, transcript="hello", tags=["a", "b"],
                      transcript_segments=[])
    tx.finish()
    store.save_transmission(tx)
    loaded = store.get_transmission(tx.id)
    assert loaded.transcript == "hello"
    assert loaded.tags == ["a", "b"]


def test_error_state_round_trips(store, session):
    tx = Transmission(session_id=session.id)
    tx.fail("translation", "boom")
    store.save_transmission(tx)
    loaded = store.get_transmission(tx.id)
    assert loaded.state is ProcessingState.FAILED
    assert loaded.error.stage == "translation" and loaded.error.message == "boom"


def test_search_covers_original_and_translation(store, populated):
    assert len(store.search("posicion")) == 1
    assert len(store.search("roadblock")) == 1


def test_search_filters(store, populated):
    assert len(store.search(language="de")) == 1
    assert len(store.search(tag="field")) == 1
    assert len(store.search(channel="GMRS 16")) == 3
    assert len(store.search(frequency_mhz=462.575)) == 3
    assert len(store.search(max_confidence=0.5)) == 1
    assert len(store.search(state="complete")) == 3


def test_search_by_date_range(store, populated):
    session, made = populated
    assert len(store.search(since=made[0].started_at)) == 3
    future = utcnow() + dt.timedelta(days=1)
    assert store.search(since=future) == []


def test_review_queue_surfaces_low_confidence(store, populated):
    queue = store.review_queue(threshold=0.6)
    assert len(queue) == 1
    assert queue[0].source_language == "de"


def test_review_queue_includes_failures(store, session):
    tx = Transmission(session_id=session.id)
    tx.fail("transcription", "no model")
    store.save_transmission(tx)
    assert any(t.id == tx.id for t in store.review_queue())


def test_reviewed_items_leave_the_queue(store, populated):
    item = store.review_queue()[0]
    item.reviewed = True
    store.save_transmission(item)
    assert not any(t.id == item.id for t in store.review_queue())


def test_pending_transmissions_excludes_terminal_states(store, session):
    for state in (ProcessingState.CAPTURED, ProcessingState.TRANSCRIBING,
                  ProcessingState.COMPLETE, ProcessingState.FAILED,
                  ProcessingState.SKIPPED):
        tx = Transmission(session_id=session.id, state=state)
        store.save_transmission(tx)
    pending = store.pending_transmissions()
    assert {t.state for t in pending} == {ProcessingState.CAPTURED,
                                          ProcessingState.TRANSCRIBING}


def test_profiles_round_trip(store):
    profile = store.save_profile(RadioProfile(
        name="UV-5R", channel_name="GMRS 16", frequency_mhz=462.575))
    loaded = store.get_profile(profile.id)
    assert loaded.name == "UV-5R"
    assert loaded.frequency_mhz == pytest.approx(462.575)
    assert loaded.label() == "GMRS 16 - 462.5750 MHz"


def test_sessions_close(store):
    session = store.save_session(Session(name="x"))
    assert store.get_session(session.id).is_open
    store.close_session(session.id)
    assert not store.get_session(session.id).is_open


def test_prune_respects_retention(store, populated):
    session, made = populated
    old = made[0]
    old.started_at = utcnow() - dt.timedelta(days=40)
    store.save_transmission(old)
    assert store.prune(retention_days=30) == 1
    assert store.get_transmission(old.id) is None


def test_search_with_punctuation_does_not_break_fts(store, populated):
    # FTS5 treats bare punctuation as syntax; the query builder must quote it.
    assert store.search('"hola"') is not None
    assert store.search("posicion; drop") is not None


def test_stats(store, populated):
    stats = store.stats()
    assert stats["transmissions"] == 3
    assert stats["sessions"] == 1


def test_orphan_transmission_is_rejected(store):
    """The session foreign key is a real constraint, not decoration."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.save_transmission(Transmission(session_id="no-such-session"))
