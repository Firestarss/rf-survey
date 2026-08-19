"""Incremental schema migrations.

`schema.sql` describes the current shape for a *fresh* database. This applies the
steps needed to bring an *existing* one up to that shape, so a schema change is a
few lines here rather than a full rewrite of schema.sql and a rebuild.

Adding a migration:
  1. Append to MIGRATIONS with the next version number.
  2. Make the same change in schema.sql so fresh builds match.
  3. Bump SCHEMA_VERSION in db.py.

Every statement must be safe to run against a database that already has real rows.
ALTER TABLE ADD COLUMN is; dropping or retyping a column is not — SQLite needs a
table rebuild for those, so write it out longhand when the time comes.
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
        # db.log_event() instead, and in schema.sql for fresh databases.
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

}

TONE_STATES = ("none", "ctcss", "dcs", "unknown")


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    return int(row[0]) if row else 0


def apply(conn: sqlite3.Connection, target: int, verbose: bool = False) -> int:
    """Bring conn up to `target`. Returns how many migrations ran."""
    have = current_version(conn)
    if have > target:
        raise RuntimeError(
            f"database is schema v{have}, newer than this code's v{target}. "
            f"Update the code rather than downgrading the database."
        )

    ran = 0
    for version in sorted(MIGRATIONS):
        if have < version <= target:
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

    if have < target:
        # schema.sql already wrote the target version for a fresh database.
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
            (str(target),),
        )
    return ran
