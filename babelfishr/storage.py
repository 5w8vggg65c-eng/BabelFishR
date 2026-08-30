"""Local SQLite storage for sessions, radio profiles and transmissions.

Everything stays on the machine.  Audio files live on disk beside the database;
the database holds metadata, transcripts, translations and corrections, plus an
FTS index so the operator can search what was said.
"""

from __future__ import annotations

import contextlib
import csv
import datetime as _dt
import json
import logging
import pathlib
import shutil
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import (ContentClass, Conversation, ErrorInfo, ProcessingState,
                     RadioProfile, Session, SourceLanguageMode,
                     Transmission, TranscriptSegment, iso, parse_iso,
                     utcnow)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    radio_make              TEXT DEFAULT '',
    radio_model             TEXT DEFAULT '',
    channel_name            TEXT DEFAULT '',
    frequency_mhz           REAL,
    mode                    TEXT DEFAULT '',
    notes                   TEXT DEFAULT '',
    default_source_language TEXT,
    created_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_default INTEGER DEFAULT 0,
    position   INTEGER DEFAULT 0,
    notes      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    id                   TEXT PRIMARY KEY,
    name                 TEXT DEFAULT '',
    started_at           TEXT NOT NULL,
    ended_at             TEXT,
    audio_device         TEXT DEFAULT '',
    audio_device_id      TEXT,
    sample_rate          INTEGER DEFAULT 0,
    profile_id           TEXT,
    profile_label        TEXT DEFAULT '',
    source_language_mode TEXT DEFAULT 'automatic',
    source_language      TEXT,
    target_language      TEXT DEFAULT 'en',
    transcription_engine TEXT DEFAULT '',
    translation_engine   TEXT DEFAULT '',
    notes                TEXT DEFAULT '',
    conversation_id      TEXT
);

CREATE TABLE IF NOT EXISTS transmissions (
    id                          TEXT PRIMARY KEY,
    session_id                  TEXT NOT NULL,
    started_at                  TEXT NOT NULL,
    ended_at                    TEXT,
    duration                    REAL DEFAULT 0,
    audio_device                TEXT DEFAULT '',
    audio_path                  TEXT,
    processed_audio_path        TEXT,
    sample_rate                 INTEGER DEFAULT 0,
    peak_dbfs                   REAL DEFAULT -120,
    noise_floor_dbfs            REAL DEFAULT -120,
    clipped                     INTEGER DEFAULT 0,
    detection_confidence        REAL DEFAULT 0,
    content_class               TEXT DEFAULT 'unknown',
    auto_processed              INTEGER DEFAULT 1,
    skip_reason                 TEXT DEFAULT '',
    profile_id                  TEXT,
    profile_label               TEXT DEFAULT '',
    channel_name                TEXT DEFAULT '',
    frequency_mhz               REAL,
    frequency_provenance        TEXT DEFAULT 'unknown',
    channel_provenance          TEXT DEFAULT 'unknown',
    rssi_dbm                    REAL,
    rssi_provenance             TEXT DEFAULT 'unknown',
    snr_db                      REAL,
    snr_provenance              TEXT DEFAULT 'unknown',
    modulation                  TEXT DEFAULT '',
    modulation_provenance       TEXT DEFAULT 'unknown',
    squelch_code                TEXT DEFAULT '',
    squelch_code_provenance     TEXT DEFAULT 'unknown',
    talkgroup                   TEXT DEFAULT '',
    talkgroup_provenance        TEXT DEFAULT 'unknown',
    unit_id                     TEXT DEFAULT '',
    unit_id_provenance          TEXT DEFAULT 'unknown',
    protocol                    TEXT DEFAULT '',
    protocol_provenance         TEXT DEFAULT 'unknown',
    signal_metadata             TEXT DEFAULT '{}',
    analysis_attempts           TEXT DEFAULT '[]',
    source_language_mode        TEXT DEFAULT 'automatic',
    source_language             TEXT,
    language_confidence         REAL,
    target_language             TEXT DEFAULT 'en',
    transcript                  TEXT DEFAULT '',
    transcript_confidence       REAL,
    transcript_segments         TEXT DEFAULT '[]',
    translation                 TEXT DEFAULT '',
    transcript_correction       TEXT,
    translation_correction      TEXT,
    corrected_at                TEXT,
    transcription_engine        TEXT DEFAULT '',
    transcription_engine_version TEXT DEFAULT '',
    translation_engine          TEXT DEFAULT '',
    translation_engine_version  TEXT DEFAULT '',
    state                       TEXT DEFAULT 'captured',
    error                       TEXT,
    notes                       TEXT DEFAULT '',
    tags                        TEXT DEFAULT '[]',
    bookmarked                  INTEGER DEFAULT 0,
    reviewed                    INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_tx_session ON transmissions (session_id);
CREATE INDEX IF NOT EXISTS ix_tx_started ON transmissions (started_at);
CREATE INDEX IF NOT EXISTS ix_tx_state   ON transmissions (state);
"""

#: Indexes over columns that only exist after the ALTER TABLE step. Kept out
#: of _SCHEMA because that script runs first, against a database that may
#: still be at schema 3 - indexing a column that is not there yet fails the
#: whole migration before it can add it.
_POST_MIGRATION_SCHEMA = """
CREATE INDEX IF NOT EXISTS ix_sess_conv ON sessions (conversation_id);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS transmissions_fts USING fts5 (
    id UNINDEXED, transcript, translation, correction, notes, tags,
    tokenize = 'unicode61'
);
"""

def _conversation_from_row(row) -> Conversation:
    return Conversation(id=row["id"], name=row["name"],
                        created_at=parse_iso(row["created_at"]) or utcnow(),
                        is_default=bool(row["is_default"]),
                        position=int(row["position"] or 0),
                        notes=row["notes"] or "")


_JSON_FIELDS = ("transcript_segments", "tags", "analysis_attempts")

#: JSON columns whose empty value is an object, not a list.
_JSON_OBJECT_FIELDS = ("signal_metadata",)

#: Columns added after schema 3, as (table, column, DDL). Applied one at a
#: time with ALTER TABLE, because CREATE TABLE IF NOT EXISTS does nothing at
#: all to a table that already exists - an existing alpha 3 database would
#: keep its old columns and every read of a new one would raise.
_ADDED_COLUMNS = (
    ("sessions", "conversation_id", "TEXT"),
    ("transmissions", "snr_db", "REAL"),
    ("transmissions", "snr_provenance", "TEXT DEFAULT 'unknown'"),
    ("transmissions", "squelch_code", "TEXT DEFAULT ''"),
    ("transmissions", "squelch_code_provenance", "TEXT DEFAULT 'unknown'"),
    ("transmissions", "talkgroup", "TEXT DEFAULT ''"),
    ("transmissions", "talkgroup_provenance", "TEXT DEFAULT 'unknown'"),
    ("transmissions", "unit_id", "TEXT DEFAULT ''"),
    ("transmissions", "unit_id_provenance", "TEXT DEFAULT 'unknown'"),
    ("transmissions", "protocol", "TEXT DEFAULT ''"),
    ("transmissions", "protocol_provenance", "TEXT DEFAULT 'unknown'"),
    ("transmissions", "signal_metadata", "TEXT DEFAULT '{}'"),
)
_BOOL_FIELDS = ("clipped", "bookmarked", "reviewed", "auto_processed")


class Store:
    """Thread-safe SQLite store.

    A single connection guarded by a lock: the workload is a handful of writes
    per transmission, so contention is irrelevant and this avoids the
    per-thread-connection bookkeeping that tends to leak file handles in a GUI.
    """

    def __init__(self, path: str = "babelfishr.sqlite3",
                 recordings_dir: Optional[str] = None):
        self.path = str(path)
        self.recordings_dir = pathlib.Path(
            recordings_dir or (pathlib.Path(self.path).parent / "recordings"))
        self._lock = threading.RLock()
        if self.path != ":memory:":
            pathlib.Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.fts_enabled = True
        self._migrate()

    # ---- lifecycle -----------------------------------------------------
    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _migrate(self) -> None:
        """Create what is missing, add what is new, and lose nothing.

        Three steps, in this order and all idempotent:

        1. ``CREATE TABLE IF NOT EXISTS`` for anything absent. This alone is
           **not** a migration - it is a no-op against a table that already
           exists, so an alpha 3 database would keep its schema-3 columns.
        2. ``ALTER TABLE ... ADD COLUMN`` for every column added since, guarded
           by reading the existing column list. SQLite adds them with their
           declared default, so existing rows keep every value they had.
        3. Backfill: ensure the permanent General thread exists and attach
           every session that predates conversations to it. Nothing is
           deleted, moved on disk, or rewritten.
        """
        with self._lock:
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.executescript(_FTS_SCHEMA)
            except sqlite3.OperationalError as exc:  # FTS5 not compiled in
                self.fts_enabled = False
                log.warning("FTS5 unavailable (%s); search falls back to LIKE", exc)

            for table, column, ddl in _ADDED_COLUMNS:
                self._add_column(table, column, ddl)
            self._conn.executescript(_POST_MIGRATION_SCHEMA)

            self._backfill_default_conversation()

            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            self._conn.commit()

    def _columns(self, table: str) -> set:
        return {row["name"] for row in
                self._conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _add_column(self, table: str, column: str, ddl: str) -> bool:
        """Add one column if it is not already there. Returns True if added."""
        if column in self._columns(table):
            return False
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        log.info("migrated %s: added column %s", table, column)
        return True

    def _backfill_default_conversation(self) -> str:
        """Guarantee General exists, and file every orphan session under it."""
        from .models import DEFAULT_CONVERSATION_NAME, Conversation

        row = self._conn.execute(
            "SELECT * FROM conversations WHERE is_default = 1 "
            "ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            default = Conversation(name=DEFAULT_CONVERSATION_NAME,
                                   is_default=True, position=0)
            self._conn.execute(
                "INSERT INTO conversations (id, name, created_at, is_default, "
                "position, notes) VALUES (?,?,?,?,?,?)",
                (default.id, default.name, iso(default.created_at), 1, 0, ""))
            conversation_id = default.id
        else:
            conversation_id = row["id"]

        # Every session written before conversations existed. Assigned, never
        # rewritten otherwise: the run keeps its own identity and history.
        self._conn.execute(
            "UPDATE sessions SET conversation_id = ? "
            "WHERE conversation_id IS NULL OR conversation_id = ''",
            (conversation_id,))
        return conversation_id

    @property
    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    # ---- profiles ------------------------------------------------------
    def save_profile(self, profile: RadioProfile) -> RadioProfile:
        with self._lock:
            self._conn.execute(
                """INSERT INTO profiles (id, name, radio_make, radio_model,
                       channel_name, frequency_mhz, mode, notes,
                       default_source_language, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, radio_make=excluded.radio_make,
                       radio_model=excluded.radio_model,
                       channel_name=excluded.channel_name,
                       frequency_mhz=excluded.frequency_mhz, mode=excluded.mode,
                       notes=excluded.notes,
                       default_source_language=excluded.default_source_language""",
                (profile.id, profile.name, profile.radio_make, profile.radio_model,
                 profile.channel_name, profile.frequency_mhz, profile.mode,
                 profile.notes, profile.default_source_language,
                 iso(profile.created_at)))
            self._conn.commit()
        return profile

    def get_profile(self, profile_id: str) -> Optional[RadioProfile]:
        row = self._conn.execute("SELECT * FROM profiles WHERE id = ?",
                                 (profile_id,)).fetchone()
        return RadioProfile.from_dict(dict(row)) if row else None

    def list_profiles(self) -> List[RadioProfile]:
        rows = self._conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
        return [RadioProfile.from_dict(dict(r)) for r in rows]

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
            self._conn.commit()

    # ---- sessions ------------------------------------------------------
    def save_session(self, session: Session) -> Session:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sessions (id, name, started_at, ended_at,
                       audio_device, audio_device_id, sample_rate, profile_id,
                       profile_label, source_language_mode, source_language,
                       target_language, transcription_engine, translation_engine,
                       notes, conversation_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, ended_at=excluded.ended_at,
                       audio_device=excluded.audio_device,
                       audio_device_id=excluded.audio_device_id,
                       sample_rate=excluded.sample_rate,
                       profile_id=excluded.profile_id,
                       profile_label=excluded.profile_label,
                       source_language_mode=excluded.source_language_mode,
                       source_language=excluded.source_language,
                       target_language=excluded.target_language,
                       transcription_engine=excluded.transcription_engine,
                       translation_engine=excluded.translation_engine,
                       notes=excluded.notes,
                       conversation_id=excluded.conversation_id""",
                (session.id, session.name, iso(session.started_at),
                 iso(session.ended_at), session.audio_device, session.audio_device_id,
                 session.sample_rate, session.profile_id, session.profile_label,
                 session.source_language_mode.value, session.source_language,
                 session.target_language, session.transcription_engine,
                 session.translation_engine, session.notes,
                 session.conversation_id or None))
            self._conn.commit()
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?",
                                 (session_id,)).fetchone()
        return Session.from_dict(dict(row)) if row else None

    def list_sessions(self, limit: int = 100) -> List[Session]:
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [Session.from_dict(dict(r)) for r in rows]

    def close_session(self, session_id: str,
                      ended_at: Optional[_dt.datetime] = None) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?",
                               (iso(ended_at or utcnow()), session_id))
            self._conn.commit()

    # ---- transmissions -------------------------------------------------
    def save_transmission(self, tx: Transmission) -> Transmission:
        payload = _to_row(tx)
        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k != "id")
        with self._lock:
            self._conn.execute(
                f"INSERT INTO transmissions ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                tuple(payload.values()))
            self._index_fts(tx)
            self._conn.commit()
        return tx

    def _index_fts(self, tx: Transmission) -> None:
        if not self.fts_enabled:
            return
        self._conn.execute("DELETE FROM transmissions_fts WHERE id = ?", (tx.id,))
        self._conn.execute(
            "INSERT INTO transmissions_fts (id, transcript, translation, "
            "correction, notes, tags) VALUES (?,?,?,?,?,?)",
            (tx.id, tx.transcript, tx.translation,
             " ".join(filter(None, (tx.transcript_correction,
                                    tx.translation_correction))),
             tx.notes, " ".join(tx.tags)))

    def get_transmission(self, tx_id: str) -> Optional[Transmission]:
        row = self._conn.execute("SELECT * FROM transmissions WHERE id = ?",
                                 (tx_id,)).fetchone()
        return _from_row(row) if row else None

    # ---- conversations (the operator's named Session tabs) -------------
    def default_conversation(self) -> Conversation:
        """The permanent General thread. Created on demand, never deleted."""
        with self._lock:
            conversation_id = self._backfill_default_conversation()
            self._conn.commit()
        row = self._conn.execute("SELECT * FROM conversations WHERE id = ?",
                                 (conversation_id,)).fetchone()
        return _conversation_from_row(row)

    def list_conversations(self) -> List[Conversation]:
        self.default_conversation()      # guarantees General exists
        rows = self._conn.execute(
            "SELECT * FROM conversations ORDER BY is_default DESC, position, "
            "created_at").fetchall()
        return [_conversation_from_row(r) for r in rows]

    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        row = self._conn.execute("SELECT * FROM conversations WHERE id = ?",
                                 (conversation_id,)).fetchone()
        return _conversation_from_row(row) if row else None

    def save_conversation(self, conversation: Conversation) -> Conversation:
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversations (id, name, created_at, is_default,
                       position, notes)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, position=excluded.position,
                       notes=excluded.notes""",
                (conversation.id, conversation.name, iso(conversation.created_at),
                 1 if conversation.is_default else 0, conversation.position,
                 conversation.notes))
            self._conn.commit()
        return conversation

    def create_conversation(self, name: str) -> Conversation:
        existing = self.list_conversations()
        conversation = Conversation(name=name.strip() or "Session",
                                    position=len(existing))
        return self.save_conversation(conversation)

    def rename_conversation(self, conversation_id: str,
                            name: str) -> Optional[Conversation]:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None
        conversation.name = name.strip() or conversation.name
        return self.save_conversation(conversation)

    def session_ids_for_conversation(self, conversation_id: str) -> List[str]:
        """Every low-level monitoring run filed under one named thread."""
        rows = self._conn.execute(
            "SELECT id FROM sessions WHERE conversation_id = ?",
            (conversation_id,)).fetchall()
        return [r["id"] for r in rows]

    def conversation_transmissions(self, conversation_id: str,
                                   limit: int = 500,
                                   newest_first: bool = False
                                   ) -> List[Transmission]:
        """The newest ``limit`` transmissions in one named thread.

        Selected DESC and reversed unless the caller wants newest-first, for
        the same reason as :meth:`recent_transmissions`: ASC plus LIMIT would
        return the oldest rows in the database, not the recent thread.
        """
        limit = max(0, int(limit))
        if not limit:
            return []
        rows = self._conn.execute(
            "SELECT t.* FROM transmissions t "
            "JOIN sessions s ON s.id = t.session_id "
            "WHERE s.conversation_id = ? "
            "ORDER BY t.started_at DESC, t.rowid DESC LIMIT ?",
            (conversation_id, limit)).fetchall()
        ordered = rows if newest_first else list(reversed(rows))
        return [_from_row(r) for r in ordered]

    def list_transmissions(self, session_id: Optional[str] = None,
                           limit: int = 500, ascending: bool = True
                           ) -> List[Transmission]:
        """Transmissions in the requested order, capped at ``limit``.

        Note what ``ascending=True`` with a limit means here: the *oldest*
        rows. That is right for "the first N of a session" and wrong for
        "the thread as it stands", which is why :meth:`recent_transmissions`
        exists rather than callers passing a limit to this.
        """
        order = "ASC" if ascending else "DESC"
        if session_id:
            rows = self._conn.execute(
                f"SELECT * FROM transmissions WHERE session_id = ? "
                f"ORDER BY started_at {order} LIMIT ?", (session_id, limit)).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT * FROM transmissions ORDER BY started_at {order} LIMIT ?",
                (limit,)).fetchall()
        return [_from_row(r) for r in rows]

    def recent_transmissions(self, limit: int = 500,
                             session_id: Optional[str] = None
                             ) -> List[Transmission]:
        """The newest ``limit`` transmissions, returned oldest-first.

        Two steps, and they cannot be collapsed into one. ``ORDER BY
        started_at ASC LIMIT 500`` returns the five hundred *oldest* rows -
        so after a few days of use the message thread would open on ancient
        traffic and the operator's last transmission would not be in it. The
        newest set is selected DESC, then reversed for display, so the thread
        reads in time order and ends where the operator left off.
        """
        limit = max(0, int(limit))
        if not limit:
            return []
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM transmissions WHERE session_id = ? "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (session_id, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM transmissions "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [_from_row(r) for r in reversed(rows)]

    def pending_transmissions(self) -> List[Transmission]:
        """Anything not in a terminal state - used to resume after a restart."""
        terminal = (ProcessingState.COMPLETE.value, ProcessingState.FAILED.value,
                    ProcessingState.SKIPPED.value)
        rows = self._conn.execute(
            f"SELECT * FROM transmissions WHERE state NOT IN "
            f"({','.join('?' * len(terminal))}) ORDER BY started_at",
            terminal).fetchall()
        return [_from_row(r) for r in rows]

    def review_queue(self, threshold: float = 0.6, limit: int = 200
                     ) -> List[Transmission]:
        """Low-confidence or failed transmissions the operator should check."""
        rows = self._conn.execute(
            """SELECT * FROM transmissions
               WHERE reviewed = 0 AND (
                     state = 'failed'
                  OR (transcript_confidence IS NOT NULL AND transcript_confidence < ?)
                  OR (language_confidence IS NOT NULL AND language_confidence < ?))
               ORDER BY started_at DESC LIMIT ?""",
            (threshold, threshold, limit)).fetchall()
        return [_from_row(r) for r in rows]

    def delete_transmission(self, tx_id: str, delete_audio: bool = False) -> None:
        tx = self.get_transmission(tx_id)
        with self._lock:
            self._conn.execute("DELETE FROM transmissions WHERE id = ?", (tx_id,))
            if self.fts_enabled:
                self._conn.execute("DELETE FROM transmissions_fts WHERE id = ?", (tx_id,))
            self._conn.commit()
        if delete_audio and tx:
            for path in (tx.audio_path, tx.processed_audio_path):
                if path:
                    with contextlib.suppress(OSError):
                        pathlib.Path(path).unlink()

    # ---- search --------------------------------------------------------
    def search(self, query: str = "", *, session_id: Optional[str] = None,
               since: Optional[_dt.datetime] = None,
               until: Optional[_dt.datetime] = None,
               channel: Optional[str] = None,
               frequency_mhz: Optional[float] = None,
               language: Optional[str] = None,
               target_language: Optional[str] = None,
               tag: Optional[str] = None,
               state: Optional[str] = None,
               bookmarked: Optional[bool] = None,
               min_confidence: Optional[float] = None,
               max_confidence: Optional[float] = None,
               limit: int = 200) -> List[Transmission]:
        """Full-text search across transcripts and translations, plus filters."""
        where: List[str] = []
        params: List[Any] = []

        if query.strip():
            ids = self._matching_ids(query.strip())
            if not ids:
                return []
            where.append(f"t.id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if session_id:
            where.append("t.session_id = ?")
            params.append(session_id)
        if since:
            where.append("t.started_at >= ?")
            params.append(iso(since))
        if until:
            where.append("t.started_at <= ?")
            params.append(iso(until))
        if channel:
            where.append("t.channel_name = ?")
            params.append(channel)
        if frequency_mhz is not None:
            where.append("ABS(COALESCE(t.frequency_mhz, -1) - ?) < 0.00001")
            params.append(frequency_mhz)
        if language:
            where.append("t.source_language = ?")
            params.append(language)
        if target_language:
            where.append("t.target_language = ?")
            params.append(target_language)
        if tag:
            where.append("t.tags LIKE ?")
            params.append(f'%"{tag}"%')
        if state:
            where.append("t.state = ?")
            params.append(state)
        if bookmarked is not None:
            where.append("t.bookmarked = ?")
            params.append(1 if bookmarked else 0)
        if min_confidence is not None:
            where.append("COALESCE(t.transcript_confidence, 0) >= ?")
            params.append(min_confidence)
        if max_confidence is not None:
            where.append("COALESCE(t.transcript_confidence, 1) <= ?")
            params.append(max_confidence)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._conn.execute(
            f"SELECT t.* FROM transmissions t {clause} "
            f"ORDER BY t.started_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [_from_row(r) for r in rows]

    def _matching_ids(self, query: str) -> List[str]:
        if self.fts_enabled:
            try:
                rows = self._conn.execute(
                    "SELECT id FROM transmissions_fts WHERE transmissions_fts "
                    "MATCH ? ORDER BY rank", (_fts_query(query),)).fetchall()
                return [r["id"] for r in rows]
            except sqlite3.OperationalError as exc:
                log.debug("FTS query failed (%s); falling back to LIKE", exc)
        like = f"%{query}%"
        rows = self._conn.execute(
            """SELECT id FROM transmissions
               WHERE transcript LIKE ? OR translation LIKE ?
                  OR COALESCE(transcript_correction,'') LIKE ?
                  OR COALESCE(translation_correction,'') LIKE ?
                  OR notes LIKE ?""",
            (like, like, like, like, like)).fetchall()
        return [r["id"] for r in rows]

    # ---- retention -----------------------------------------------------
    def prune(self, retention_days: int, delete_audio: bool = True) -> int:
        """Delete transmissions older than *retention_days*. Returns the count."""
        if retention_days <= 0:
            return 0
        cutoff = utcnow() - _dt.timedelta(days=retention_days)
        rows = self._conn.execute(
            "SELECT id FROM transmissions WHERE started_at < ?",
            (iso(cutoff),)).fetchall()
        for row in rows:
            self.delete_transmission(row["id"], delete_audio=delete_audio)
        return len(rows)

    def stats(self) -> Dict[str, Any]:
        row = self._conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(duration), 0) AS secs
               FROM transmissions""").fetchone()
        sessions = self._conn.execute(
            "SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        return {
            "transmissions": row["n"], "total_seconds": round(row["secs"], 1),
            "sessions": sessions, "database": self.path,
            "recordings_dir": str(self.recordings_dir),
            "fts_enabled": self.fts_enabled,
        }


def _fts_query(query: str) -> str:
    """Quote bare terms so punctuation in a search string cannot break FTS5."""
    terms = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' if t.isalnum() else f'"{t}"' for t in terms)


def _to_row(tx: Transmission) -> Dict[str, Any]:
    d = tx.to_dict()
    for derived in ("display_transcript", "display_translation", "needs_review",
                    "frequency_is_measured"):
        d.pop(derived, None)
    d["transcript_segments"] = json.dumps(
        [s if isinstance(s, dict) else s.to_dict() for s in d["transcript_segments"]])
    d["tags"] = json.dumps(d["tags"])
    d["analysis_attempts"] = json.dumps(d.get("analysis_attempts") or [])
    # Raw decoder metadata, stored verbatim so a later version can read a
    # field this one does not model.
    d["signal_metadata"] = json.dumps(d.get("signal_metadata") or {})
    d["error"] = json.dumps(d["error"]) if d["error"] else None
    for field in _BOOL_FIELDS:
        d[field] = 1 if d[field] else 0
    return d


def _from_row(row: sqlite3.Row) -> Transmission:
    d = dict(row)
    for field in _JSON_FIELDS:
        d[field] = json.loads(d.get(field) or "[]")
    for field in _JSON_OBJECT_FIELDS:
        d[field] = json.loads(d.get(field) or "{}")
    d["error"] = json.loads(d["error"]) if d.get("error") else None
    for field in _BOOL_FIELDS:
        d[field] = bool(d.get(field))
    return Transmission.from_dict(d)
