"""Local SQLite storage for sessions, radio profiles and transmissions.

Everything stays on the machine.  Audio files live on disk beside the database;
the database holds metadata, transcripts, translations and corrections, plus an
FTS index so the operator can search what was said.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import datetime as _dt
import json
import logging
import os
import pathlib
import shutil
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import (ContentClass, Conversation, ErrorInfo, ProcessingState,
                     RadioProfile, Session, SourceLanguageMode,
                     Transmission, TranscriptSegment, iso, parse_iso,
                     utcnow)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 5

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
    notes      TEXT DEFAULT '',
    color      TEXT DEFAULT '',
    hidden     INTEGER DEFAULT 0
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
    hidden                      INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions (id)
);

-- A permanently deleted message leaves a tombstone. save_transmission()
-- refuses to write a row whose id is here, so a processing worker that was
-- still holding the transmission when the operator deleted it cannot bring it
-- back with its late result. leftover_files records anything the deletion
-- could not remove from disk, so it can be identified and retried rather
-- than reported as gone.
CREATE TABLE IF NOT EXISTS deleted_transmissions (
    id             TEXT PRIMARY KEY,
    deleted_at     TEXT NOT NULL,
    session_id     TEXT,
    leftover_files TEXT DEFAULT '[]'
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
    keys = row.keys()
    return Conversation(id=row["id"], name=row["name"],
                        created_at=parse_iso(row["created_at"]) or utcnow(),
                        is_default=bool(row["is_default"]),
                        position=int(row["position"] or 0),
                        notes=row["notes"] or "",
                        color=(row["color"] or "") if "color" in keys else "",
                        hidden=bool(row["hidden"]) if "hidden" in keys else False)


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
    # Schema 5: an operator-chosen tab colour per named Session, and a
    # message removed from view with its data kept.
    ("conversations", "color", "TEXT DEFAULT ''"),
    ("transmissions", "hidden", "INTEGER DEFAULT 0"),
    ("conversations", "hidden", "INTEGER DEFAULT 0"),
)
_BOOL_FIELDS = ("clipped", "bookmarked", "reviewed", "auto_processed", "hidden")


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
        #: Bumped on every transmission save. A cached picture of which files
        #: retained messages refer to is valid only while this has not moved.
        self._writes = 0
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
            if self._is_tombstoned(tx.id):
                # The operator deleted this message; a late result from a
                # worker that still held it must not recreate the row.
                log.info("refusing to save deleted transmission %s", tx.id)
                return tx
            self._conn.execute(
                f"INSERT INTO transmissions ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                tuple(payload.values()))
            self._index_fts(tx)
            self._conn.commit()
            self._writes += 1
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

    def list_conversations(self, include_hidden: bool = False
                           ) -> List[Conversation]:
        self.default_conversation()      # guarantees General exists
        where = "" if include_hidden else "WHERE hidden = 0 "
        rows = self._conn.execute(
            f"SELECT * FROM conversations {where}ORDER BY is_default DESC, "
            "position, created_at").fetchall()
        return [_conversation_from_row(r) for r in rows]

    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        row = self._conn.execute("SELECT * FROM conversations WHERE id = ?",
                                 (conversation_id,)).fetchone()
        return _conversation_from_row(row) if row else None

    def save_conversation(self, conversation: Conversation) -> Conversation:
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversations (id, name, created_at, is_default,
                       position, notes, color, hidden)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, position=excluded.position,
                       notes=excluded.notes, color=excluded.color,
                       hidden=excluded.hidden""",
                (conversation.id, conversation.name, iso(conversation.created_at),
                 1 if conversation.is_default else 0, conversation.position,
                 conversation.notes, conversation.color or "",
                 1 if conversation.hidden else 0))
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

    def set_conversation_color(self, conversation_id: str,
                               color: str) -> Optional[Conversation]:
        """Record an operator's tab colour, or clear it with "".

        Only a ``#rrggbb`` value or the empty string is stored: the colour is
        rendered into a stylesheet and an icon, and anything else is refused
        rather than written and later misread.
        """
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None
        value = (color or "").strip()
        if value and not _is_hex_color(value):
            raise ValueError(f"not a #rrggbb colour: {color!r}")
        conversation.color = value.lower()
        return self.save_conversation(conversation)

    # ---- removing a whole Session ------------------------------------------
    def hide_conversation(self, conversation_id: str, hidden: bool = True
                          ) -> Optional[Conversation]:
        """Take a Session's tab away (or bring it back). Everything is kept.

        General is refused: it is the default thread every orphaned run is
        filed under, and whether it may ever be removed is a decision that has
        not been made - so this keeps the existing guarantee rather than
        deciding it here.
        """
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None
        if conversation.is_default and hidden:
            raise ValueError("the default General Session cannot be hidden")
        conversation.hidden = bool(hidden)
        return self.save_conversation(conversation)

    def conversation_removal_inventory(self, conversation_id: str,
                                       owned_roots: Sequence[str]
                                       ) -> "ConversationInventory":
        """What deleting a whole Session would touch: its runs, every message
        in them (removed-from-view ones included), and those messages' files
        sorted the same way a single deletion sorts them."""
        inventory = ConversationInventory(conversation_id=conversation_id)
        references = self.retained_references()
        for session_id in self.session_ids_for_conversation(conversation_id):
            inventory.session_ids.append(session_id)
            for tx in self.list_transmissions(session_id=session_id,
                                              limit=1_000_000,
                                              include_hidden=True):
                inventory.transmission_ids.append(tx.id)
                files = self.deletion_inventory(tx, owned_roots, references)
                inventory.owned += files.owned
                inventory.external += files.external
                inventory.shared += files.shared
        return inventory

    def delete_conversation_permanently(self, conversation_id: str,
                                        owned_roots: Sequence[str]
                                        ) -> Optional["ConversationReport"]:
        """Delete a Session: every run under it, every message, their owned
        files, then the runs and the Session itself. Other Sessions are not
        touched; a file another Session's message still uses is kept.

        Messages go one at a time through the single-message path, so each
        gets a tombstone and each file the same ownership and sharing checks.
        """
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None
        if conversation.is_default:
            raise ValueError("the default General Session cannot be deleted")
        report = ConversationReport(conversation_id=conversation_id,
                                    name=conversation.name)
        # One picture of what retained messages refer to, kept current as
        # rows go (each deletion forgets its own id) and rebuilt by the
        # single-message path if anything was saved meanwhile.
        references = self.retained_references()
        for session_id in self.session_ids_for_conversation(conversation_id):
            for tx in self.list_transmissions(session_id=session_id,
                                              limit=1_000_000,
                                              include_hidden=True):
                one = self.delete_transmission_permanently(tx.id, owned_roots,
                                                           references)
                if one is not None:
                    report.messages.append(one)
            with self._lock:
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                self._conn.commit()
            report.session_ids.append(session_id)
        with self._lock:
            self._conn.execute("DELETE FROM conversations WHERE id = ?",
                               (conversation_id,))
            self._conn.commit()
        return report

    def session_ids_for_conversation(self, conversation_id: str) -> List[str]:
        """Every low-level monitoring run filed under one named thread."""
        rows = self._conn.execute(
            "SELECT id FROM sessions WHERE conversation_id = ?",
            (conversation_id,)).fetchall()
        return [r["id"] for r in rows]

    def conversation_transmissions(self, conversation_id: str,
                                   limit: int = 500,
                                   newest_first: bool = False,
                                   include_hidden: bool = False
                                   ) -> List[Transmission]:
        """The newest ``limit`` transmissions in one named thread.

        Selected DESC and reversed unless the caller wants newest-first, for
        the same reason as :meth:`recent_transmissions`: ASC plus LIMIT would
        return the oldest rows in the database, not the recent thread.
        Messages the operator removed from view are left out unless asked for.
        """
        limit = max(0, int(limit))
        if not limit:
            return []
        hidden = "" if include_hidden else "AND t.hidden = 0 "
        rows = self._conn.execute(
            "SELECT t.* FROM transmissions t "
            "JOIN sessions s ON s.id = t.session_id "
            f"WHERE s.conversation_id = ? {hidden}"
            "ORDER BY t.started_at DESC, t.rowid DESC LIMIT ?",
            (conversation_id, limit)).fetchall()
        ordered = rows if newest_first else list(reversed(rows))
        return [_from_row(r) for r in ordered]

    def list_transmissions(self, session_id: Optional[str] = None,
                           limit: int = 500, ascending: bool = True,
                           include_hidden: bool = False
                           ) -> List[Transmission]:
        """Transmissions in the requested order, capped at ``limit``.

        Note what ``ascending=True`` with a limit means here: the *oldest*
        rows. That is right for "the first N of a session" and wrong for
        "the thread as it stands", which is why :meth:`recent_transmissions`
        exists rather than callers passing a limit to this. Removed messages
        are left out unless asked for, so an export agrees with the thread.
        """
        order = "ASC" if ascending else "DESC"
        hidden = "" if include_hidden else "hidden = 0"
        if session_id:
            where = "WHERE session_id = ?" + (f" AND {hidden}" if hidden else "")
            rows = self._conn.execute(
                f"SELECT * FROM transmissions {where} "
                f"ORDER BY started_at {order} LIMIT ?", (session_id, limit)).fetchall()
        else:
            where = f"WHERE {hidden}" if hidden else ""
            rows = self._conn.execute(
                f"SELECT * FROM transmissions {where} ORDER BY started_at {order} LIMIT ?",
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
                "SELECT * FROM transmissions WHERE session_id = ? AND hidden = 0 "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (session_id, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM transmissions WHERE hidden = 0 "
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

    def review_queue(self, threshold: float = 0.6, limit: int = 200,
                     conversation_id: Optional[str] = None
                     ) -> List[Transmission]:
        """Low-confidence or failed transmissions the operator should check.

        ``conversation_id`` restricts the queue to one named Session, across
        every monitoring run inside it. Opened from a Session tab, a review
        queue listing another Session's traffic is not a review queue.
        """
        # Built in the order the placeholders appear in the statement: the
        # scope clause sits before the two confidence thresholds, so the
        # conversation id has to be bound first.
        scope = ""
        params: List[Any] = []
        if conversation_id:
            scope = ("AND t.session_id IN "
                     "(SELECT id FROM sessions WHERE conversation_id = ?) ")
            params.append(conversation_id)
        params += [threshold, threshold, limit]
        rows = self._conn.execute(
            f"""SELECT t.* FROM transmissions t
               WHERE t.reviewed = 0 AND t.hidden = 0 {scope}AND (
                     t.state = 'failed'
                  OR (t.transcript_confidence IS NOT NULL
                      AND t.transcript_confidence < ?)
                  OR (t.language_confidence IS NOT NULL
                      AND t.language_confidence < ?))
               ORDER BY t.started_at DESC LIMIT ?""",
            tuple(params)).fetchall()
        return [_from_row(r) for r in rows]

    def delete_transmission(self, tx_id: str, delete_audio: bool = False) -> None:
        """Retention pruning. Row, index and (optionally) the two audio
        files; errors suppressed. Leaves a tombstone like every deletion."""
        tx = self.get_transmission(tx_id)
        with self._lock:
            self._tombstone(tx_id, tx.session_id if tx else None, [])
            self._conn.execute("DELETE FROM transmissions WHERE id = ?", (tx_id,))
            if self.fts_enabled:
                self._conn.execute("DELETE FROM transmissions_fts WHERE id = ?", (tx_id,))
            self._conn.commit()
        if delete_audio and tx:
            for path in (tx.audio_path, tx.processed_audio_path):
                if path:
                    with contextlib.suppress(OSError):
                        pathlib.Path(path).unlink()

    # ---- operator removal ------------------------------------------------
    def hide_transmission(self, tx_id: str, hidden: bool = True
                          ) -> Optional[Transmission]:
        """Remove a message from view (or put it back). Nothing else moves."""
        tx = self.get_transmission(tx_id)
        if tx is None:
            return None
        tx.hidden = bool(hidden)
        return self.save_transmission(tx)

    def hidden_transmissions(self, conversation_id: str,
                             limit: int = 500) -> List[Transmission]:
        rows = self._conn.execute(
            "SELECT t.* FROM transmissions t "
            "JOIN sessions s ON s.id = t.session_id "
            "WHERE s.conversation_id = ? AND t.hidden = 1 "
            "ORDER BY t.started_at DESC LIMIT ?",
            (conversation_id, max(0, int(limit)))).fetchall()
        return [_from_row(r) for r in rows]

    def deletion_inventory(self, tx: Transmission,
                           owned_roots: Sequence[str],
                           references: Optional["RetainedReferences"] = None
                           ) -> "DeletionInventory":
        """Everything a permanent deletion would touch, and what it would not.

        Files are sorted into three piles before anything is removed:

        * ``owned`` - inside one of the application's own roots (the
          Recordings folder), a regular file, not a symlink, and referenced by
          no other retained transmission. These are deleted.
        * ``external`` - anywhere else: a WAV the operator replayed from their
          own folder, an export, a backup. Never touched; named in the report.
        * ``shared`` - inside an owned root but also referenced by another
          transmission that is being kept. Never touched.

        Paths come from the message's own fields and every analysis attempt's
        artifacts and derived input; nothing is expanded, globbed or walked.

        Sharing is decided against :meth:`retained_references` - every path a
        retained row refers to, decoded from its stored form and normalised
        the same way the candidate is - so a quotation mark or an accented
        character in a path, or a different spelling of the same file, cannot
        hide a reference. ``references`` may be a map the caller already
        built; it is reused only while no save has happened since.
        """
        roots = [pathlib.Path(os.path.realpath(r)) for r in owned_roots if r]
        references = self._current_references(references)
        candidates: List[str] = []
        for path in (tx.audio_path, tx.processed_audio_path):
            if path:
                candidates.append(path)
        for attempt in tx.analysis_attempts:
            if attempt.input_is_derived and attempt.input_path:
                candidates.append(attempt.input_path)
            for artifact in attempt.artifacts:
                if artifact.path:
                    candidates.append(artifact.path)
        seen: List[str] = []
        for path in candidates:
            if path not in seen:
                seen.append(path)

        inventory = DeletionInventory(transmission_id=tx.id)
        for path in seen:
            p = pathlib.Path(path)
            if p.is_symlink():
                inventory.external.append(path)         # never follow a link
                continue
            real = pathlib.Path(os.path.realpath(path))
            inside = any(_is_within(real, root) for root in roots)
            if not inside:
                inventory.external.append(path)
                continue
            reason = references.why_shared(path, tx.id)
            if reason:
                inventory.shared.append(path)
                inventory.reasons[path] = reason
                continue
            inventory.owned.append(path)
        return inventory

    def retained_references(self) -> "RetainedReferences":
        """Every file a transmission row still refers to, by normalised path.

        Read from the rows, not matched against their text. The earlier check
        ran ``LIKE '%path%'`` over the stored analysis JSON, in which a
        quotation mark is written ``\\"`` and a non-ASCII character as a
        ``\\uXXXX`` escape - so exactly those paths were never found, and a
        file another message still used could be deleted. Every reference is
        decoded, then keyed by :func:`_same_file_key`, the normalisation the
        ownership check applies to a candidate.

        A row whose analysis record cannot be read is remembered as
        unreadable rather than treated as referencing nothing: while any such
        row is retained, every candidate is treated as possibly shared.
        """
        references = RetainedReferences(stamp=self._writes)
        rows = self._conn.execute(
            "SELECT id, audio_path, processed_audio_path, analysis_attempts "
            "FROM transmissions").fetchall()
        for row in rows:
            tx_id = row["id"]
            references.add(tx_id, row["audio_path"])
            references.add(tx_id, row["processed_audio_path"])
            raw = row["analysis_attempts"]
            if not raw or raw == "[]":
                continue
            try:
                attempts = json.loads(raw)
                if not isinstance(attempts, list):
                    raise ValueError("analysis_attempts is not a list")
                for attempt in attempts:
                    if not isinstance(attempt, dict):
                        raise ValueError("analysis attempt is not an object")
                    references.add(tx_id, attempt.get("input_path"))
                    artifacts = attempt.get("artifacts") or []
                    if not isinstance(artifacts, list):
                        raise ValueError("artifacts is not a list")
                    for artifact in artifacts:
                        if not isinstance(artifact, dict):
                            raise ValueError("artifact is not an object")
                        references.add(tx_id, artifact.get("path"))
            except (ValueError, TypeError):
                references.unreadable.add(tx_id)
        return references

    def _current_references(self, references: Optional["RetainedReferences"]
                            ) -> "RetainedReferences":
        """The caller's map if nothing has been saved since it was built."""
        if references is None or references.stamp != self._writes:
            return self.retained_references()
        return references

    def _referenced_elsewhere(self, path: str, except_id: str) -> bool:
        return bool(self.retained_references().why_shared(path, except_id))

    def delete_transmission_permanently(self, tx_id: str,
                                        owned_roots: Sequence[str],
                                        references: Optional["RetainedReferences"]
                                        = None) -> Optional["DeletionReport"]:
        """Delete one message and the files that are its alone.

        Order: the tombstone and the row go first, in one transaction, so a
        late worker cannot recreate the message and a crash mid-way leaves a
        record rather than a half-deleted row. Then each owned file is
        unlinked individually - never a directory. A file that will not go is
        recorded on the tombstone as a leftover and reported; the deletion is
        not called complete while one remains.

        The whole of it runs under the store's lock. Deciding that a file is
        unshared and unlinking it are therefore one step with respect to
        every other writer: a worker's save that would add a reference to
        the file waits until the deletion has finished, and the decision is
        made against the rows as they stand at the moment of removal.
        """
        with self._lock:
            tx = self.get_transmission(tx_id)
            if tx is None:
                return None
            references = self._current_references(references)
            inventory = self.deletion_inventory(tx, owned_roots, references)
            self._tombstone(tx_id, tx.session_id, inventory.owned)
            self._conn.execute("DELETE FROM transmissions WHERE id = ?", (tx_id,))
            if self.fts_enabled:
                self._conn.execute("DELETE FROM transmissions_fts WHERE id = ?",
                                   (tx_id,))
            self._conn.commit()
            references.forget(tx_id)
            report = DeletionReport(transmission_id=tx_id, inventory=inventory)
            self._unlink_owned(report)
        return report

    def _unlink_owned(self, report: "DeletionReport") -> None:
        leftovers: List[str] = []
        for path in report.inventory.owned:
            p = pathlib.Path(path)
            try:
                if p.is_symlink() or p.is_dir():
                    raise IsADirectoryError(f"not a regular file: {path}")
                p.unlink()
                report.removed.append(path)
            except FileNotFoundError:
                report.already_gone.append(path)
            except OSError as exc:
                report.failed.append((path, f"{type(exc).__name__}: {exc}"))
                leftovers.append(path)
        with self._lock:
            self._conn.execute(
                "UPDATE deleted_transmissions SET leftover_files = ? WHERE id = ?",
                (json.dumps(leftovers), report.transmission_id))
            self._conn.commit()

    def _tombstone(self, tx_id: str, session_id: Optional[str],
                   leftovers: Sequence[str]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO deleted_transmissions "
            "(id, deleted_at, session_id, leftover_files) VALUES (?,?,?,?)",
            (tx_id, iso(utcnow()), session_id, json.dumps(list(leftovers))))

    def _is_tombstoned(self, tx_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM deleted_transmissions WHERE id = ?",
            (tx_id,)).fetchone() is not None

    def is_deleted(self, tx_id: str) -> bool:
        return self._is_tombstoned(tx_id)

    def leftover_deletions(self) -> Dict[str, List[str]]:
        """Deleted messages whose files could not all be removed, by id."""
        rows = self._conn.execute(
            "SELECT id, leftover_files FROM deleted_transmissions "
            "WHERE leftover_files != '[]'").fetchall()
        out: Dict[str, List[str]] = {}
        for row in rows:
            files = json.loads(row["leftover_files"] or "[]")
            if files:
                out[row["id"]] = files
        return out

    def retry_leftover_deletions(self, owned_roots: Sequence[str]
                                 ) -> "LeftoverRetry":
        """Try again for every leftover, re-checking ownership *and* sharing
        as they stand now.

        A leftover was this message's alone when the message was deleted.
        That was true then. If a retained message has since come to refer to
        the same file, the file is that message's now: it is preserved, taken
        off the leftover list because nothing is left for the tombstone to
        remove, and reported as kept - never as removed, and never handed to
        the unlink. The earlier version trusted the original inventory and
        deleted it.
        """
        roots = [pathlib.Path(os.path.realpath(r)) for r in owned_roots if r]
        result = LeftoverRetry()
        with self._lock:
            references = self.retained_references()
            for tx_id, files in self.leftover_deletions().items():
                remaining: List[str] = []
                for path in files:
                    p = pathlib.Path(path)
                    real = pathlib.Path(os.path.realpath(path))
                    if (not any(_is_within(real, root) for root in roots)
                            or p.is_symlink()):
                        remaining.append(path)     # no longer ours to remove
                        continue
                    if references.why_shared(path, tx_id):
                        result.preserved.setdefault(tx_id, []).append(path)
                        continue
                    try:
                        if p.is_dir():
                            raise IsADirectoryError(path)
                        p.unlink()
                        result.removed.append(path)
                    except FileNotFoundError:
                        result.already_gone.append(path)
                    except OSError:
                        remaining.append(path)
                self._conn.execute(
                    "UPDATE deleted_transmissions SET leftover_files = ? WHERE id = ?",
                    (json.dumps(remaining), tx_id))
                self._conn.commit()
                if remaining:
                    result.still[tx_id] = remaining
        return result

    # ---- search --------------------------------------------------------
    def search(self, query: str = "", *, session_id: Optional[str] = None,
               conversation_id: Optional[str] = None,
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
        if conversation_id:
            # Through the run to the named Session it belongs to. A named
            # Session spans every monitoring run filed under it, so this is
            # never the same as filtering on one session_id.
            where.append("t.session_id IN "
                         "(SELECT id FROM sessions WHERE conversation_id = ?)")
            params.append(conversation_id)
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

        where.append("t.hidden = 0")
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


@dataclasses.dataclass
class DeletionInventory:
    """What a permanent deletion would touch on disk, sorted by ownership."""

    transmission_id: str
    owned: List[str] = dataclasses.field(default_factory=list)
    external: List[str] = dataclasses.field(default_factory=list)
    shared: List[str] = dataclasses.field(default_factory=list)
    #: Why each shared path is shared: the id still referring to it, or the
    #: unreadable record that means it might.
    reasons: Dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class LeftoverRetry:
    """What Finish unfinished deletions did, file by file."""

    still: Dict[str, List[str]] = dataclasses.field(default_factory=dict)
    preserved: Dict[str, List[str]] = dataclasses.field(default_factory=dict)
    removed: List[str] = dataclasses.field(default_factory=list)
    already_gone: List[str] = dataclasses.field(default_factory=list)

    @property
    def preserved_paths(self) -> List[str]:
        return [p for files in self.preserved.values() for p in files]


class RetainedReferences:
    """Which retained transmissions refer to which files, by normalised path.

    ``stamp`` is the store's write counter when this was built; the store
    rebuilds rather than reuse a map that any save has outdated.
    """

    def __init__(self, stamp: int):
        self.stamp = stamp
        self.by_key: Dict[str, Set[str]] = {}
        self.unreadable: Set[str] = set()

    def add(self, tx_id: str, path: Optional[str]) -> None:
        if path:
            self.by_key.setdefault(_same_file_key(path), set()).add(tx_id)

    def forget(self, tx_id: str) -> None:
        """A row is gone: it refers to nothing any more."""
        for ids in self.by_key.values():
            ids.discard(tx_id)
        self.unreadable.discard(tx_id)

    def why_shared(self, path: str, except_id: str) -> str:
        """Why this file must be kept, or "" when no retained row refers to it."""
        others = self.by_key.get(_same_file_key(path), set()) - {except_id}
        if others:
            return f"still used by message {sorted(others)[0]}"
        unreadable = self.unreadable - {except_id}
        if unreadable:
            return (f"the analysis record of message {sorted(unreadable)[0]} "
                    f"could not be read, so it may still use this file")
        return ""


@dataclasses.dataclass
class DeletionReport:
    """What a permanent deletion actually did."""

    transmission_id: str
    inventory: DeletionInventory
    removed: List[str] = dataclasses.field(default_factory=list)
    already_gone: List[str] = dataclasses.field(default_factory=list)
    failed: List[Tuple[str, str]] = dataclasses.field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Every owned file is gone. False while any remains."""
        return not self.failed


@dataclasses.dataclass
class ConversationInventory:
    conversation_id: str
    session_ids: List[str] = dataclasses.field(default_factory=list)
    transmission_ids: List[str] = dataclasses.field(default_factory=list)
    owned: List[str] = dataclasses.field(default_factory=list)
    external: List[str] = dataclasses.field(default_factory=list)
    shared: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ConversationReport:
    conversation_id: str
    name: str
    session_ids: List[str] = dataclasses.field(default_factory=list)
    messages: List[DeletionReport] = dataclasses.field(default_factory=list)

    @property
    def removed_files(self) -> List[str]:
        return [p for m in self.messages for p in m.removed]

    @property
    def failed(self) -> List[Tuple[str, str]]:
        return [f for m in self.messages for f in m.failed]

    @property
    def complete(self) -> bool:
        return not self.failed


def _same_file_key(path: str) -> str:
    """One spelling for one file - the ownership check's own normalisation.

    ``realpath`` collapses ``/./`` and ``..`` segments and follows symlinks,
    so a reference written another way, or through a link, names the same
    key as the candidate it protects. Case is left alone, as the ownership
    check leaves it: the application only ever stores paths it produced.
    """
    return os.path.realpath(path)


def _is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_hex_color(value: str) -> bool:
    return (len(value) == 7 and value[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in value[1:]))


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
