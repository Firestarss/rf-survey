-- rfsurvey schema v2 — THE BASELINE, NOT THE CURRENT SHAPE
--
-- This is where every database starts. It is not what one looks like: v3 to v8
-- live in migrate.py, and init_schema() runs this and then replays all of them,
-- so `events` here is missing eight columns that every real row has. Read
-- migrate.MIGRATIONS for the rest, or ask a built database with
-- `python3 src/db.py <path>`.
--
-- Do not add anything here. A column added here as well as in a migration makes
-- that migration's ALTER TABLE fail with "duplicate column name" on every fresh
-- build. Migrations own the schema past v2; this file is frozen.
--
-- Changed from v1: band_plan stores frequency RANGES, not single frequencies.
-- A discrete channel is a narrow range; a ham band segment is a wide one. Lookup is
-- one containment query for both, and the match tolerance lives in the data where it
-- can be seen and tuned, rather than as a magic number inside the enricher.
--
-- Conventions:
--   Frequencies are INTEGER Hz. Never floats — 466.0 MHz must round-trip exactly.
--   Times are REAL unix epoch seconds, UTC. Sub-second precision matters for pairing.
--   Levels are REAL dBFS or dB. Nothing is stored in linear units.
--   Anything the software cannot determine is NULL, never 0 and never "unknown".

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Provenance: which software, which config, which radios produced these rows
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY,
    started_at      REAL    NOT NULL,
    ended_at        REAL,
    profile_name    TEXT    NOT NULL,
    profile_yaml    TEXT    NOT NULL,   -- verbatim copy, not a path
    git_commit      TEXT,               -- output of `git rev-parse HEAD`
    hostname        TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS run_receivers (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    receiver_id     TEXT    NOT NULL,   -- 'uhf' | 'vhf'
    serial          TEXT    NOT NULL,   -- authoritative identity, never an index
    firmware        TEXT,
    usb_bus         INTEGER,
    sample_rate_hz  INTEGER NOT NULL,
    gain_db         REAL,
    ppm_error       REAL,
    attenuator_db   REAL,               -- physically fitted; recorded, not read
    antenna         TEXT,
    UNIQUE (run_id, receiver_id)
);

-- ---------------------------------------------------------------------------
-- Reference data: seeded from a file, not observed.
--
-- Every row is a range. `kind` says which sort:
--   'channel'  narrow, has a nominal centre you could program into a radio
--   'segment'  wide, a band plan allocation with no single frequency
--
-- Lookup returns every range containing the measured frequency, narrowest first,
-- so specific beats general: 146.520 -> "National FM simplex calling", while
-- 146.470 falls through to "2 m simplex" from the wider segment containing it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS band_plan (
    id              INTEGER PRIMARY KEY,
    freq_lo_hz      INTEGER NOT NULL,   -- inclusive
    freq_hi_hz      INTEGER NOT NULL,   -- inclusive
    freq_center_hz  INTEGER,            -- nominal; NULL for segments
    bandwidth_hz    INTEGER,            -- authorised; NULL for segments
    service         TEXT    NOT NULL,   -- 'frs','gmrs','murs','part90','ham2m','ham70cm'
    label           TEXT    NOT NULL,
    channel_number  TEXT,
    kind            TEXT    NOT NULL CHECK (kind IN ('channel','segment')),
    is_repeater_out INTEGER NOT NULL DEFAULT 0 CHECK (is_repeater_out IN (0,1)),
    pair_offset_hz  INTEGER,            -- +/- offset to the paired frequency
    licensed        INTEGER NOT NULL DEFAULT 1 CHECK (licensed IN (0,1)),
    source          TEXT,               -- 'FCC 95B', 'ARRL', 'NESMC', ...
    notes           TEXT,
    CHECK (freq_hi_hz > freq_lo_hz),
    CHECK (freq_center_hz IS NULL
           OR freq_center_hz BETWEEN freq_lo_hz AND freq_hi_hz),
    CHECK (kind = 'segment' OR freq_center_hz IS NOT NULL),
    UNIQUE (service, label, freq_lo_hz)
);

CREATE INDEX IF NOT EXISTS idx_band_plan_lo   ON band_plan (freq_lo_hz);
CREATE INDEX IF NOT EXISTS idx_band_plan_kind ON band_plan (kind, service);

-- ---------------------------------------------------------------------------
-- Observations: one row per detected transmission. Append-only, never edited.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    receiver_id     TEXT    NOT NULL,

    t_start         REAL    NOT NULL,
    t_end           REAL    NOT NULL,
    duration_s      REAL    NOT NULL,

    freq_hz         INTEGER NOT NULL,   -- ppm-corrected, as measured
    freq_raw_hz     INTEGER,            -- before ppm correction
    bandwidth_hz    INTEGER,

    peak_dbfs       REAL,
    noise_dbfs      REAL,
    snr_db          REAL,

    modulation      TEXT,               -- 'nfm','fm','am','ssb','4fsk','cw',NULL
    content         TEXT,               -- 'voice','data','tone','cw','silence',NULL
    ctcss_hz        REAL,
    dcs_code        INTEGER,
    dcs_polarity    TEXT CHECK (dcs_polarity IN ('N','I') OR dcs_polarity IS NULL),

    confidence      REAL CHECK (confidence BETWEEN 0 AND 1 OR confidence IS NULL),
    audio_path      TEXT,
    iq_path         TEXT,

    channel_id      INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    band_plan_id    INTEGER REFERENCES band_plan(id) ON DELETE SET NULL,

    CHECK (t_end >= t_start),
    CHECK (NOT (ctcss_hz IS NOT NULL AND dcs_code IS NOT NULL))  -- never both
);

CREATE INDEX IF NOT EXISTS idx_events_freq    ON events (freq_hz);
CREATE INDEX IF NOT EXISTS idx_events_time    ON events (t_start);
CREATE INDEX IF NOT EXISTS idx_events_channel ON events (channel_id);
CREATE INDEX IF NOT EXISTS idx_events_run     ON events (run_id, receiver_id);

CREATE TABLE IF NOT EXISTS decodes (
    id              INTEGER PRIMARY KEY,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    protocol        TEXT    NOT NULL,   -- 'p25','dmr','nxdn','dstar','pocsag','aprs'
    nac             TEXT,
    color_code      INTEGER,
    talkgroup       TEXT,
    radio_id        TEXT,
    site_id         TEXT,
    is_control      INTEGER NOT NULL DEFAULT 0 CHECK (is_control IN (0,1)),
    raw             TEXT
);

CREATE INDEX IF NOT EXISTS idx_decodes_event ON decodes (event_id);

-- ---------------------------------------------------------------------------
-- Derived: rebuildable rollup of events into things you could program a radio to
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY,
    freq_hz         INTEGER NOT NULL UNIQUE,

    service         TEXT,
    label           TEXT,
    band_plan_id    INTEGER REFERENCES band_plan(id) ON DELETE SET NULL,
    modulation      TEXT,
    content         TEXT,
    ctcss_hz        REAL,
    dcs_code        INTEGER,
    dcs_polarity    TEXT,

    first_seen      REAL,
    last_seen       REAL,
    event_count     INTEGER NOT NULL DEFAULT 0,
    total_airtime_s REAL    NOT NULL DEFAULT 0,

    tier            INTEGER CHECK (tier BETWEEN 0 AND 4),
    pair_id         INTEGER REFERENCES pairs(id) ON DELETE SET NULL,

    rebuilt_at      REAL,               -- everything above this line is derived
    notes           TEXT                -- ... except this. Hand-written, preserved.
);

CREATE INDEX IF NOT EXISTS idx_channels_service ON channels (service);
CREATE INDEX IF NOT EXISTS idx_channels_tier    ON channels (tier);

CREATE TABLE IF NOT EXISTS pairs (
    id                  INTEGER PRIMARY KEY,
    output_channel_id   INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    input_channel_id    INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    offset_hz           INTEGER NOT NULL,
    evidence_count      INTEGER NOT NULL DEFAULT 0,
    median_lag_s        REAL,
    confidence          REAL CHECK (confidence BETWEEN 0 AND 1 OR confidence IS NULL),
    inferred_at         REAL,
    UNIQUE (output_channel_id, input_channel_id)
);

-- ---------------------------------------------------------------------------
-- Views
--
-- None here on purpose. v2 defined v_events, v_contactable and v_activity, and
-- migrations 4 and 5 drop and recreate all three — so every fresh build created
-- them only to replace them a moment later. Views hold no data and a migration
-- can rebuild one freely, which is why they live there and not here.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Schema version
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '2');
