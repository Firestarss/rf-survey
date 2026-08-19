"""Migrations.

A migration is the one operation here that can destroy data that cannot be
recreated. `events` is append-only observation of a festival that happens once;
`channels` and `pairs` can be rebuilt from it, and a survey cannot.

Migration 5 rebuilds `events` outright, which means DROP TABLE on a table that
`decodes` references ON DELETE CASCADE. With foreign keys enforced that DROP
performs an implicit DELETE and empties `decodes` silently.
"""

import sqlite3
import unittest

from support import SRC, TempDirCase, build_at_version, schema_of  # noqa: F401

import db
import migrate


class TestMigrationStructure(unittest.TestCase):

    def test_versions_are_contiguous(self):
        # schema.sql stamps v2 and init_schema replays from there. A gap would
        # leave apply() unable to reach the target and fail only on a fresh build.
        versions = sorted(migrate.MIGRATIONS)
        self.assertEqual(versions, list(range(3, max(versions) + 1)))

    def test_code_version_matches_the_last_migration(self):
        self.assertEqual(db.SCHEMA_VERSION, max(migrate.MIGRATIONS),
                         "db.SCHEMA_VERSION and MIGRATIONS have drifted apart")


class TestFreshBuild(TempDirCase):

    def test_fresh_database_is_at_the_current_version(self):
        path = self.path("fresh.sqlite")
        db.init_schema(path)
        conn = db.connect(path)
        self.assertEqual(migrate.current_version(conn), db.SCHEMA_VERSION)

    def test_fresh_and_upgraded_schemas_are_identical(self):
        # If these ever diverge, a bug reproduces on a deployed deck and not on a
        # freshly built one, which is the worst possible place for it to live.
        fresh_path = self.path("fresh.sqlite")
        db.init_schema(fresh_path)
        fresh = db.connect(fresh_path)

        old = build_at_version(self.path("old.sqlite"), 2)
        migrate.apply(old, db.SCHEMA_VERSION)

        self.assertEqual(schema_of(fresh), schema_of(old))

    def test_expected_objects_exist(self):
        path = self.path("fresh.sqlite")
        db.init_schema(path)
        conn = db.connect(path)
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for required in ("events", "channels", "pairs", "runs", "run_receivers",
                         "decodes", "band_plan", "schema_meta",
                         "coverage_windows", "v_events", "v_activity",
                         "v_contactable", "v_coverage"):
            self.assertIn(required, names)


class TestEventsRebuild(TempDirCase):
    """Migration 5 rebuilds `events`. Nothing may be lost doing it."""

    def _populate_v4(self, conn):
        conn.execute("INSERT INTO runs (started_at, profile_name, profile_yaml) "
                     "VALUES (1000.0, 'test', 'x: 1')")
        for i in range(5):
            conn.execute(
                "INSERT INTO events (run_id, receiver_id, t_start, t_end, "
                "duration_s, freq_hz, snr_db, tone_state, ctcss_hz) "
                "VALUES (1,'uhf',?,?,1.0,462675000,?, 'ctcss', 141.3)",
                (1000.0 + i, 1001.0 + i, 20.0 + i))
        conn.execute("INSERT INTO decodes (event_id, protocol, talkgroup) "
                     "SELECT id, 'dmr', '101' FROM events")
        conn.commit()

    def test_decodes_survive_the_rebuild(self):
        # The CASCADE trap. PRAGMA foreign_keys is a no-op inside a transaction,
        # so it has to be cleared outside one; if apply() ever stops doing that,
        # this test is what notices, and it notices before a festival does.
        path = self.path("v4.sqlite")
        conn = build_at_version(path, 4)
        self._populate_v4(conn)
        before = conn.execute("SELECT COUNT(*) FROM decodes").fetchone()[0]
        self.assertEqual(before, 5)
        conn.close()

        db.init_schema(path)
        conn = db.connect(path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM decodes").fetchone()[0],
                         5, "decodes were cascaded away by the events rebuild")

    def test_event_rows_and_values_survive_the_rebuild(self):
        path = self.path("v4b.sqlite")
        conn = build_at_version(path, 4)
        self._populate_v4(conn)
        conn.close()

        db.init_schema(path)
        conn = db.connect(path)
        rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["tone_state"], "ctcss")
        self.assertEqual(rows[0]["ctcss_hz"], 141.3)
        self.assertEqual(rows[0]["freq_hz"], 462675000)
        self.assertAlmostEqual(rows[2]["snr_db"], 22.0)

    def test_no_dangling_references_after_migrating(self):
        path = self.path("v4c.sqlite")
        conn = build_at_version(path, 4)
        self._populate_v4(conn)
        conn.close()
        db.init_schema(path)
        conn = db.connect(path)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_t_end_becomes_nullable(self):
        # The whole point of migration 5: an unattended deck loses power
        # mid-transmission and that row is the one worth keeping.
        path = self.path("v5.sqlite")
        db.init_schema(path)
        conn = db.connect(path)
        conn.execute("INSERT INTO runs (started_at, profile_name, profile_yaml) "
                     "VALUES (1.0,'t','x: 1')")
        conn.execute("INSERT INTO events (run_id, receiver_id, t_start, freq_hz) "
                     "VALUES (1,'uhf',1.0,462675000)")
        row = conn.execute("SELECT t_end, duration_s FROM events").fetchone()
        self.assertIsNone(row["t_end"])
        self.assertIsNone(row["duration_s"])

    def test_tone_state_check_is_enforced_after_the_rebuild(self):
        # Migration 3 could not add this constraint via ALTER; migration 5's
        # rebuild is what finally installs it.
        path = self.path("v5b.sqlite")
        db.init_schema(path)
        conn = db.connect(path)
        conn.execute("INSERT INTO runs (started_at, profile_name, profile_yaml) "
                     "VALUES (1.0,'t','x: 1')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO events (run_id, receiver_id, t_start, "
                         "freq_hz, tone_state) VALUES (1,'uhf',1.0,1,'banana')")


class TestApplyBehaviour(TempDirCase):

    def test_running_twice_is_a_no_op(self):
        path = self.path("twice.sqlite")
        db.init_schema(path)
        conn = db.connect(path)
        self.assertEqual(migrate.apply(conn, db.SCHEMA_VERSION), 0)

    def test_downgrade_is_refused(self):
        path = self.path("down.sqlite")
        db.init_schema(path)
        conn = db.connect(path)
        with self.assertRaises(RuntimeError):
            migrate.apply(conn, db.SCHEMA_VERSION - 1)

    def test_foreign_key_enforcement_is_restored(self):
        # apply() turns enforcement off to rebuild a table. Leaving it off would
        # silently disable every constraint for the rest of the process.
        path = self.path("fk.sqlite")
        conn = build_at_version(path, 4)
        conn.execute("PRAGMA foreign_keys = ON")
        migrate.apply(conn, db.SCHEMA_VERSION)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1,
                         "foreign key enforcement was left off")

    def test_a_failing_migration_leaves_the_version_behind(self):
        # Each migration runs in its own transaction, so a failure must roll back
        # both the statements and the version stamp rather than half-applying.
        path = self.path("boom.sqlite")
        conn = build_at_version(path, 4)
        original = dict(migrate.MIGRATIONS)
        try:
            migrate.MIGRATIONS[max(original) + 1] = ["THIS IS NOT SQL"]
            with self.assertRaises(sqlite3.Error):
                migrate.apply(conn, max(original) + 1)
            self.assertEqual(migrate.current_version(conn), max(original),
                             "version advanced past a migration that failed")
        finally:
            migrate.MIGRATIONS.clear()
            migrate.MIGRATIONS.update(original)


if __name__ == "__main__":
    unittest.main()
