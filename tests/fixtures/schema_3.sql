-- The schema-3 DDL, verbatim, as a self-contained test fixture.
--
-- PROVENANCE. This is the exact contents of the `_SCHEMA` string literal in
-- babelfishr/storage.py at the final schema-3 revision:
--
--   v0.3.0-alpha.3  c9299e06e8731db1645441da3677cf24358243dd
--   final schema-3  7a42cfc1307911cf241d983857de23f04ff2fe8b
--
-- The DDL is IDENTICAL at both revisions. It was extracted from each with the
-- same slice the test helper used to use - between `_SCHEMA = """` and
-- `"""\n\n_FTS_SCHEMA` - and the two extractions compare byte-for-byte equal:
-- 3787 bytes, SHA-256
-- af7dc8a1b94a78cdeace5f4a7519d32ed32a945dece44d7de39a1a6b0522d4da.
-- The commits between those two revisions change storage.py but not this DDL.
-- 7a42cfc is the last commit where storage.py carried SCHEMA_VERSION = 3; the
-- next one to touch the file, 17ffad8, raised it to 4.
--
-- WHY IT IS CHECKED IN. The helper used to read this out of Git at run time
-- with `git show 7a42cfc:babelfishr/storage.py`. That worked in a full clone
-- and failed everywhere else: on a GitHub Actions runner, where
-- actions/checkout leaves a one-commit shallow clone, `git show` exited 128 and
-- both migration tests errored before their bodies ran. A migration test must
-- be able to build its own starting point from a source tree alone - shallow
-- clone, exported archive, or a directory with no .git at all - so the
-- baseline lives here instead.
--
-- DO NOT REGENERATE THIS FROM THE CURRENT SCHEMA. It is schema 3 on purpose:
-- the thing the migration has to be able to open. Reconstructing it from
-- schema 4 would test the migration against its own output.
--
-- Everything below the last comment line is the DDL, byte for byte. The
-- helper recovers it by dropping the leading "--" lines and nothing else.

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
    notes                TEXT DEFAULT ''
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
    modulation                  TEXT DEFAULT '',
    modulation_provenance       TEXT DEFAULT 'unknown',
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
