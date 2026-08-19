"""Incremental schema migrations.

`schema.sql` describes the current shape for a *fresh* database. This applies the
steps needed to bring an *existing* one up to that shape, so a schema change is a
few lines here rather than a full rewrite of schema.sql and a rebuild.

Adding a migration:
  1. Append to MIGRATIONS with the next version number.
  2. Bump SCHEMA_VERSION in db.py.

Do NOT mirror the change into schema.sql. That file describes a *v2* database and
stamps v2; init_schema() runs it and then replays every migration on top. Adding a
column there as well makes the matching `ALTER TABLE ... ADD COLUMN` fail with
"duplicate column name" on every fresh build. Migrations own the schema past v2.

Every statement must be safe to run against a database that already has real rows.
ALTER TABLE ADD COLUMN is; dropping a NOT NULL, retyping, or adding a CHECK is not —
SQLite needs a full table rebuild for those. apply() sets up the conditions such a
rebuild needs (see below), so the steps are just: drop dependent views, create the
new table, copy, drop the old, rename, recreate indexes and views.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: dict[int, list[str]] = {

    3: [
        # "no tone present" and "we never looked" were both NULL. They are very
        # different facts: the first means you can programme a radio and be heard,
        # the second means you cannot. Tier 3 turns on exactly this distinction.
        #
        #   'none'    checked, carrier is clean
        #   'ctcss'   checked, ctcss_hz is populated
        #   'dcs'     checked, dcs_code is populated
        #   'unknown' not checked, or checked and inconclusive
        #
        # No CHECK constraint: SQLite cannot add one via ALTER TABLE. Enforced in
        # db.log_event() instead. Migration 5 rebuilds the table and adds the real
        # constraint, which is the only way to get one onto an existing column.
        "ALTER TABLE events   ADD COLUMN tone_state TEXT DEFAULT 'unknown'",
        "ALTER TABLE channels ADD COLUMN tone_state TEXT DEFAULT 'unknown'",
        # Backfill: any existing row with a tone recorded was clearly checked.
        "UPDATE events   SET tone_state='ctcss' WHERE ctcss_hz IS NOT NULL",
        "UPDATE events   SET tone_state='dcs'   WHERE dcs_code IS NOT NULL",
        "UPDATE channels SET tone_state='ctcss' WHERE ctcss_hz IS NOT NULL",
        "UPDATE channels SET tone_state='dcs'   WHERE dcs_code IS NOT NULL",
    ],
    4: [
        # v_contactable rendered tone_state 'none' and 'unknown' identically as a
        # blank column, which hid the exact distinction migration 3 existed to
        # capture. Views can be dropped and rebuilt freely — they hold no data.
        "DROP VIEW IF EXISTS v_contactable",
        """CREATE VIEW v_contactable AS
           SELECT printf('%.4f', c.freq_hz / 1e6) AS mhz,
                  c.service, c.label, c.modulation,
                  CASE
                    WHEN c.ctcss_hz IS NOT NULL THEN printf('CTCSS %.1f', c.ctcss_hz)
                    WHEN c.dcs_code IS NOT NULL THEN printf('DCS %03d%s', c.dcs_code,
                                                    COALESCE(c.dcs_polarity,''))
                    WHEN c.tone_state = 'none' THEN 'no tone'
                    ELSE '?'
                  END AS tone,
                  c.tier, c.event_count,
                  printf('%.0f', c.total_airtime_s) AS airtime_s,
                  datetime(c.last_seen, 'unixepoch') AS last_heard,
                  CASE WHEN p.id IS NOT NULL
                       THEN printf('%.4f in', pi.freq_hz / 1e6) END AS repeater_input
           FROM channels c
           LEFT JOIN pairs    p  ON p.id = c.pair_id
           LEFT JOIN channels pi ON pi.id = p.input_channel_id
           WHERE c.tier IS NOT NULL
           ORDER BY c.tier DESC, c.total_airtime_s DESC""",

        "DROP VIEW IF EXISTS v_events",
        """CREATE VIEW v_events AS
           SELECT e.id,
                  datetime(e.t_start, 'unixepoch') AS utc,
                  e.receiver_id,
                  printf('%.4f', e.freq_hz / 1e6) AS mhz,
                  printf('%.2f', e.duration_s) AS secs,
                  e.modulation, e.content,
                  CASE
                    WHEN e.ctcss_hz IS NOT NULL THEN printf('CTCSS %.1f', e.ctcss_hz)
                    WHEN e.dcs_code IS NOT NULL THEN printf('DCS %03d%s', e.dcs_code,
                                                    COALESCE(e.dcs_polarity,''))
                    WHEN e.tone_state = 'none' THEN 'no tone'
                    ELSE '?'
                  END AS tone,
                  printf('%.1f', e.snr_db) AS snr,
                  COALESCE(c.label, b.label) AS label
           FROM events e
           LEFT JOIN channels  c ON c.id = e.channel_id
           LEFT JOIN band_plan b ON b.id = e.band_plan_id
           ORDER BY e.t_start""",
    ],
    5: [
        # The prototype writes an event in two phases: a row on keyup, an update on
        # release. An unattended deck loses power mid-transmission, and those
        # in-flight rows are the ones worth keeping — so t_end and duration_s have
        # to be nullable. Dropping a NOT NULL means rebuilding the table, which is
        # also the only chance to add the tone_state CHECK migration 3 wanted and
        # the columns the prototype has always measured but had nowhere to put.
        #
        # Views that name `events` are dropped first: while the table is missing,
        # between DROP and RENAME, ALTER TABLE reparses the whole schema and would
        # fail on a view referring to a table that does not exist yet.
        "DROP VIEW IF EXISTS v_events",
        "DROP VIEW IF EXISTS v_activity",

        """CREATE TABLE events_new (
            id              INTEGER PRIMARY KEY,
            run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            receiver_id     TEXT    NOT NULL,

            t_start         REAL    NOT NULL,
            t_end           REAL,               -- NULL while still transmitting
            duration_s      REAL,               -- NULL until the event closes

            freq_hz         INTEGER NOT NULL,   -- snapped to the 6.25 kHz grid
            freq_raw_hz     INTEGER,            -- as measured, before snapping
            bandwidth_hz    INTEGER,            -- occupied; not yet measured

            peak_dbfs       REAL,
            noise_dbfs      REAL,
            snr_db          REAL,

            modulation      TEXT,
            content         TEXT,
            deviation_hz    REAL,               -- peak FM deviation, Hz
            ctcss_hz        REAL,
            ctcss_dev_hz    REAL,               -- deviation of the tone itself
            dcs_code        INTEGER,
            dcs_polarity    TEXT CHECK (dcs_polarity IN ('N','I')
                                        OR dcs_polarity IS NULL),
            tone_state      TEXT    DEFAULT 'unknown'
                            CHECK (tone_state IS NULL
                                   OR tone_state IN ('none','ctcss','dcs','unknown')),

            confidence      REAL CHECK (confidence BETWEEN 0 AND 1
                                        OR confidence IS NULL),
            overload        INTEGER NOT NULL DEFAULT 0
                            CHECK (overload IN (0,1)),
            audio_path      TEXT,
            iq_path         TEXT,

            channel_id      INTEGER REFERENCES channels(id) ON DELETE SET NULL,
            band_plan_id    INTEGER REFERENCES band_plan(id) ON DELETE SET NULL,

            CHECK (t_end IS NULL OR t_end >= t_start),
            CHECK (duration_s IS NULL OR duration_s >= 0),
            CHECK (NOT (ctcss_hz IS NOT NULL AND dcs_code IS NOT NULL))
        )""",
        """INSERT INTO events_new
              (id, run_id, receiver_id, t_start, t_end, duration_s,
               freq_hz, freq_raw_hz, bandwidth_hz, peak_dbfs, noise_dbfs, snr_db,
               modulation, content, ctcss_hz, dcs_code, dcs_polarity, tone_state,
               confidence, audio_path, iq_path, channel_id, band_plan_id)
           SELECT id, run_id, receiver_id, t_start, t_end, duration_s,
               freq_hz, freq_raw_hz, bandwidth_hz, peak_dbfs, noise_dbfs, snr_db,
               modulation, content, ctcss_hz, dcs_code, dcs_polarity, tone_state,
               confidence, audio_path, iq_path, channel_id, band_plan_id
           FROM events""",
        "DROP TABLE events",
        "ALTER TABLE events_new RENAME TO events",

        "CREATE INDEX idx_events_freq    ON events (freq_hz)",
        "CREATE INDEX idx_events_time    ON events (t_start)",
        "CREATE INDEX idx_events_channel ON events (channel_id)",
        "CREATE INDEX idx_events_run     ON events (run_id, receiver_id)",

        # Rolled up from the events on the channel. The FRS/GMRS discriminator
        # reads this, so it belongs in the derived table where its verdict can be
        # audited rather than only in the log line that announced it.
        "ALTER TABLE channels ADD COLUMN deviation_hz REAL",

        # Replaces the prototype's private `coverage` table. run_receivers already
        # says which radio, which serial and which sample rate; the centre it was
        # actually tuned to was the one thing it could not say. Answering "what did
        # we not hear" needs it, and --freq can differ from the profile.
        "ALTER TABLE run_receivers ADD COLUMN center_hz INTEGER",

        """CREATE VIEW v_events AS
           SELECT e.id,
                  datetime(e.t_start, 'unixepoch') AS utc,
                  e.receiver_id,
                  printf('%.4f', e.freq_hz / 1e6) AS mhz,
                  -- an unclosed row is not a zero-length transmission
                  CASE WHEN e.duration_s IS NULL THEN 'open'
                       ELSE printf('%.2f', e.duration_s) END AS secs,
                  e.modulation, e.content,
                  CASE WHEN e.deviation_hz IS NULL THEN NULL
                       ELSE printf('%.0f', e.deviation_hz) END AS dev_hz,
                  CASE
                    WHEN e.ctcss_hz IS NOT NULL THEN printf('CTCSS %.1f', e.ctcss_hz)
                    WHEN e.dcs_code IS NOT NULL THEN printf('DCS %03d%s', e.dcs_code,
                                                    COALESCE(e.dcs_polarity,''))
                    WHEN e.tone_state = 'dcs'  THEN 'DCS?'
                    WHEN e.tone_state = 'none' THEN 'no tone'
                    ELSE '?'
                  END AS tone,
                  printf('%.1f', e.snr_db) AS snr,
                  CASE WHEN e.overload = 1 THEN 'OVL' END AS ovl,
                  COALESCE(c.label, b.label) AS label
           FROM events e
           LEFT JOIN channels  c ON c.id = e.channel_id
           LEFT JOIN band_plan b ON b.id = e.band_plan_id
           ORDER BY e.t_start""",

        # v_contactable is the operator-facing answer to "what can I talk on".
        # It predates migration 3's tone_state and renders a suspected-but-never-
        # decoded DCS channel as '?', which reads as "nothing known" when the
        # deck in fact knows there is a subaudible signal that is not CTCSS.
        # It also never showed the deviation the FRS/GMRS verdict rests on.
        "DROP VIEW IF EXISTS v_contactable",
        """CREATE VIEW v_contactable AS
           SELECT printf('%.4f', c.freq_hz / 1e6) AS mhz,
                  c.service, c.label, c.modulation,
                  CASE
                    WHEN c.ctcss_hz IS NOT NULL THEN printf('CTCSS %.1f', c.ctcss_hz)
                    WHEN c.dcs_code IS NOT NULL THEN printf('DCS %03d%s', c.dcs_code,
                                                    COALESCE(c.dcs_polarity,''))
                    WHEN c.tone_state = 'dcs'  THEN 'DCS?'
                    WHEN c.tone_state = 'none' THEN 'no tone'
                    ELSE '?'
                  END AS tone,
                  CASE WHEN c.deviation_hz IS NULL THEN NULL
                       ELSE printf('%.0f', c.deviation_hz) END AS dev_hz,
                  c.tier, c.event_count,
                  printf('%.0f', c.total_airtime_s) AS airtime_s,
                  datetime(c.last_seen, 'unixepoch') AS last_heard,
                  CASE WHEN p.id IS NOT NULL
                       THEN printf('%.4f in', pi.freq_hz / 1e6) END AS repeater_input
           FROM channels c
           LEFT JOIN pairs    p  ON p.id = c.pair_id
           LEFT JOIN channels pi ON pi.id = p.input_channel_id
           WHERE c.tier IS NOT NULL
           ORDER BY c.tier DESC, c.total_airtime_s DESC""",

        """CREATE VIEW v_activity AS
           SELECT printf('%.4f', freq_hz / 1e6)       AS mhz,
                  COUNT(*)                            AS keyups,
                  SUM(duration_s IS NULL)             AS open,
                  printf('%.0f', SUM(duration_s))     AS airtime_s,
                  printf('%.1f', AVG(snr_db))         AS avg_snr,
                  printf('%.0f', AVG(deviation_hz))   AS avg_dev_hz,
                  datetime(MIN(t_start), 'unixepoch') AS first,
                  datetime(MAX(t_start), 'unixepoch') AS last
           FROM events
           GROUP BY freq_hz
           ORDER BY SUM(duration_s) DESC""",
    ],

    6: [
        # How much dwell the analysis actually had. Analysis used to be
        # all-or-nothing at 1.4 s, so this was implicitly constant and not worth
        # recording; now that a short transmission is analysed with whatever it
        # has, a deviation measured over 0.15 s and one measured over 3 s are
        # different evidence and the row has to say which it is.
        "ALTER TABLE events ADD COLUMN analyzed_s REAL",

        # Golay bit errors corrected while decoding the DCS word. 0-3 by
        # construction. A channel decoding consistently at 0 is solid; one
        # sitting at 3 is at the edge of the code's power and worth distrusting.
        "ALTER TABLE events ADD COLUMN dcs_errors INTEGER",
    ],

    7: [
        # Rotation. Section 3 of the handoff argued the prototype's private
        # `coverage` table said nothing `runs` and `run_receivers` did not, once
        # migration 5 added run_receivers.center_hz — and that was true for a
        # PARKED receiver, which is all the code could do at the time.
        #
        # It stops being true the moment a receiver rotates. run_receivers is
        # UNIQUE (run_id, receiver_id), so it holds exactly one centre per
        # receiver per run, and the vhf radio in profiles/festival.yaml is
        # configured for three. Without per-window intervals, "was anything on
        # 2 m at 21:30, or were we parked on 70 cm" has no answer — and for a
        # rotating receiver that question is most of what the log is worth.
        """CREATE TABLE coverage_windows (
            id              INTEGER PRIMARY KEY,
            run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            receiver_id     TEXT    NOT NULL,
            center_hz       INTEGER NOT NULL,
            sample_rate_hz  INTEGER NOT NULL,
            label           TEXT,
            t_start         REAL    NOT NULL,
            t_end           REAL,              -- NULL while the window is current
            CHECK (t_end IS NULL OR t_end >= t_start)
        )""",
        "CREATE INDEX idx_windows_run ON coverage_windows (run_id, receiver_id, t_start)",

        # What was listened to, for how long, and what came of it. The point of
        # this view is the honest denominator: a band with no events because it
        # was never tuned to looks identical in `events` to a band that was
        # quiet, and those are opposite conclusions.
        """CREATE VIEW v_coverage AS
           SELECT w.receiver_id,
                  COALESCE(w.label, printf('%.3f MHz', w.center_hz / 1e6)) AS window,
                  printf('%.3f', w.center_hz / 1e6)      AS center_mhz,
                  printf('%.1f', w.sample_rate_hz / 1e6) AS span_mhz,
                  COUNT(*)                               AS visits,
                  printf('%.0f', SUM(COALESCE(w.t_end, w.t_start) - w.t_start))
                                                         AS listened_s,
                  (SELECT COUNT(*) FROM events e
                    WHERE e.receiver_id = w.receiver_id
                      AND e.run_id = w.run_id
                      AND e.t_start BETWEEN w.t_start
                                        AND COALESCE(w.t_end, 1e18)) AS events
           FROM coverage_windows w
           GROUP BY w.run_id, w.receiver_id, w.center_hz
           ORDER BY w.receiver_id, w.center_hz""",
    ],

    8: [
        # Which window heard this event, as a fact rather than an inference.
        #
        # v_coverage originally matched events to windows by timestamp range.
        # That reconstructs an answer the capture loop already knew, and it is
        # only correct while the sample clock and the wall clock agree. They
        # diverge whenever the loop is not consuming samples in real time — after
        # an overflow delivers a burst, while the process is descheduled, and
        # under --simulate, where a window's worth of samples can be synthesised
        # in less wall time than it represents. Then a window's recorded end
        # overlaps the next window's start, the ranges are ambiguous, and events
        # are attributed to a band that was never tuned to. Which is precisely
        # the question coverage exists to answer.
        "ALTER TABLE events ADD COLUMN window_id INTEGER "
        "REFERENCES coverage_windows(id) ON DELETE SET NULL",
        "CREATE INDEX idx_events_window ON events (window_id)",

        "DROP VIEW IF EXISTS v_coverage",
        """CREATE VIEW v_coverage AS
           SELECT w.receiver_id,
                  COALESCE(w.label, printf('%.3f MHz', w.center_hz / 1e6)) AS window,
                  printf('%.3f', w.center_hz / 1e6)      AS center_mhz,
                  printf('%.1f', w.sample_rate_hz / 1e6) AS span_mhz,
                  COUNT(DISTINCT w.id)                   AS visits,
                  printf('%.0f', SUM(COALESCE(w.t_end, w.t_start) - w.t_start))
                                                         AS listened_s,
                  (SELECT COUNT(*) FROM events e
                    WHERE e.window_id IN (
                        SELECT id FROM coverage_windows x
                         WHERE x.run_id = w.run_id
                           AND x.receiver_id = w.receiver_id
                           AND x.center_hz = w.center_hz)) AS events
           FROM coverage_windows w
           GROUP BY w.run_id, w.receiver_id, w.center_hz
           ORDER BY w.receiver_id, w.center_hz""",
    ],

}

TONE_STATES = ("none", "ctcss", "dcs", "unknown")


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    return int(row[0]) if row else 0


def apply(conn: sqlite3.Connection, target: int, verbose: bool = False) -> int:
    """Bring conn up to `target`. Returns how many migrations ran.

    Foreign keys are disabled for the duration and the schema is checked once at
    the end. Both halves matter for a table rebuild:

      * `DROP TABLE events` with enforcement ON performs an implicit DELETE, which
        fires `ON DELETE CASCADE` and silently empties `decodes`.
      * `PRAGMA foreign_keys` is a no-op inside a transaction, so it cannot be set
        from within a migration's own statement list — it has to happen out here,
        before BEGIN.

    Disabling enforcement means nothing checks the copy, so `foreign_key_check`
    runs afterwards and refuses to leave a database with dangling references.
    """
    have = current_version(conn)
    if have > target:
        raise RuntimeError(
            f"database is schema v{have}, newer than this code's v{target}. "
            f"Update the code rather than downgrading the database."
        )

    pending = [v for v in sorted(MIGRATIONS) if have < v <= target]
    fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if pending and fk_was_on:
        conn.execute("PRAGMA foreign_keys = OFF")   # must be outside a transaction

    ran = 0
    try:
        for version in pending:
            if verbose:
                print(f"  migrating {have} -> {version}")
            conn.execute("BEGIN")
            try:
                for stmt in MIGRATIONS[version]:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
                    (str(version),),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            have = version
            ran += 1
    finally:
        if pending and fk_was_on:
            conn.execute("PRAGMA foreign_keys = ON")

    if ran:
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(
                f"migration left {len(broken)} dangling foreign key reference(s): "
                f"{broken[:5]}")

    if have < target:
        # schema.sql already wrote the target version for a fresh database.
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
            (str(target),),
        )
    return ran
