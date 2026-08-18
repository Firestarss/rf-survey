-- rfsurvey schema v1
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
-- Reference data: static band plan, seeded from a file, not observed
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS band_plan (
    id              INTEGER PRIMARY KEY,
    freq_hz         INTEGER NOT NULL,
    service         TEXT    NOT NULL,   -- 'frs','gmrs','murs','part90','ham2m','ham70cm'
    label           TEXT    NOT NULL,   -- 'FRS 1', 'GMRS RPT 15', '2m simplex calling'
    channel_number  TEXT,
    is_repeater_out INTEGER NOT NULL DEFAULT 0 CHECK (is_repeater_out IN (0,1)),
    pair_offset_hz  INTEGER,            -- +/- offset to the input, NULL if simplex
    licensed        INTEGER NOT NULL DEFAULT 1 CHECK (licensed IN (0,1)),
    notes           TEXT,
    UNIQUE (freq_hz, service)
);

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

    freq_hz         INTEGER NOT NULL,   -- ppm-corrected, snapped to channel grid
    freq_raw_hz     INTEGER,            -- as measured, before correction
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

    CHECK (t_end >= t_start),
    CHECK (NOT (ctcss_hz IS NOT NULL AND dcs_code IS NOT NULL))  -- never both
);

CREATE INDEX IF NOT EXISTS idx_events_freq    ON events (freq_hz);
CREATE INDEX IF NOT EXISTS idx_events_time    ON events (t_start);
CREATE INDEX IF NOT EXISTS idx_events_channel ON events (channel_id);
CREATE INDEX IF NOT EXISTS idx_events_run     ON events (run_id, receiver_id);

-- Digital decode results, one-to-many against an event
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
    raw             TEXT                -- decoder output, JSON or plain
);

CREATE INDEX IF NOT EXISTS idx_decodes_event ON decodes (event_id);

-- ---------------------------------------------------------------------------
-- Derived: rebuildable rollup of events into things you could program a radio to
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY,
    freq_hz         INTEGER NOT NULL UNIQUE,

    service         TEXT,               -- from band_plan lookup
    label           TEXT,
    modulation      TEXT,               -- modal value across events
    content         TEXT,
    ctcss_hz        REAL,               -- modal tone, if consistent
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
    evidence_count      INTEGER NOT NULL DEFAULT 0,   -- correlated keyups observed
    median_lag_s        REAL,
    confidence          REAL CHECK (confidence BETWEEN 0 AND 1 OR confidence IS NULL),
    inferred_at         REAL,
    UNIQUE (output_channel_id, input_channel_id)
);

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

-- Human-readable event stream. ISO timestamps, MHz, tone as one column.
CREATE VIEW IF NOT EXISTS v_events AS
SELECT
    e.id,
    datetime(e.t_start, 'unixepoch')            AS utc,
    e.receiver_id,
    printf('%.4f', e.freq_hz / 1e6)             AS mhz,
    printf('%.2f', e.duration_s)                AS secs,
    e.modulation,
    e.content,
    CASE
        WHEN e.ctcss_hz IS NOT NULL THEN printf('CTCSS %.1f', e.ctcss_hz)
        WHEN e.dcs_code IS NOT NULL THEN printf('DCS %03d%s', e.dcs_code,
                                                COALESCE(e.dcs_polarity,''))
        ELSE NULL
    END                                          AS tone,
    printf('%.1f', e.snr_db)                     AS snr,
    c.label
FROM events e
LEFT JOIN channels c ON c.id = e.channel_id
ORDER BY e.t_start;

-- What you would actually program into a radio, best channels first.
CREATE VIEW IF NOT EXISTS v_contactable AS
SELECT
    printf('%.4f', c.freq_hz / 1e6)             AS mhz,
    c.service,
    c.label,
    c.modulation,
    CASE
        WHEN c.ctcss_hz IS NOT NULL THEN printf('CTCSS %.1f', c.ctcss_hz)
        WHEN c.dcs_code IS NOT NULL THEN printf('DCS %03d%s', c.dcs_code,
                                                COALESCE(c.dcs_polarity,''))
        ELSE NULL
    END                                          AS tone,
    c.tier,
    c.event_count,
    printf('%.0f', c.total_airtime_s)            AS airtime_s,
    datetime(c.last_seen, 'unixepoch')           AS last_heard,
    CASE WHEN p.id IS NOT NULL
         THEN printf('%.4f in', pi.freq_hz / 1e6) END AS repeater_input
FROM channels c
LEFT JOIN pairs    p  ON p.id = c.pair_id
LEFT JOIN channels pi ON pi.id = p.input_channel_id
WHERE c.tier IS NOT NULL
ORDER BY c.tier DESC, c.total_airtime_s DESC;

-- Busiest frequencies regardless of classification. The "what is going on here" query.
CREATE VIEW IF NOT EXISTS v_activity AS
SELECT
    printf('%.4f', freq_hz / 1e6)                AS mhz,
    COUNT(*)                                     AS keyups,
    printf('%.0f', SUM(duration_s))              AS airtime_s,
    printf('%.1f', AVG(snr_db))                  AS avg_snr,
    datetime(MIN(t_start), 'unixepoch')          AS first,
    datetime(MAX(t_start), 'unixepoch')          AS last
FROM events
GROUP BY freq_hz
ORDER BY SUM(duration_s) DESC;

-- ---------------------------------------------------------------------------
-- Schema version
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '1');
