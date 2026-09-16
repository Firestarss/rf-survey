"""Two receivers opening one database at the same instant.

Found on hardware 2026-09-16, the first time both receivers ran together. Every
boot starts rfsurvey@uhf and rfsurvey@vhf at once against the shared database.
init_schema() read the version, ran the baseline script (which stamps v2), then
wrote the version it had read back — with nothing serialising those steps
across processes. vhf migrated a fresh database to v10 and started surveying;
uhf, which had read the version while vhf was part-way through, then wrote a
stale 9 over vhf's 10. The result was a database stamped v9 containing all of
migration 10, and a uhf service that crashed on `duplicate column name:
harmonic_of` on every restart until systemd gave up on it.

At a field site with nobody to fix it, that is one receiver dead for the whole
event from the first boot.
"""

import multiprocessing as mp
import os
import sqlite3
import tempfile
import time
import unittest

import support  # noqa: F401

import db as dbmod


def _init(path, barrier, results):
    try:
        barrier.wait()
        dbmod.init_schema(path)
        results.put(("ok", None))
    except Exception as e:          # noqa: BLE001 — reported to the parent
        results.put(("err", f"{type(e).__name__}: {e}"))


class ConcurrentInit(unittest.TestCase):

    def _race(self, path, workers):
        ctx = mp.get_context("fork")
        barrier, results = ctx.Barrier(workers), ctx.Queue()
        procs = [ctx.Process(target=_init, args=(path, barrier, results))
                 for _ in range(workers)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        return [results.get(timeout=5) for _ in procs]

    def _assert_consistent(self, path):
        conn = sqlite3.connect(path)
        try:
            version = int(conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0])
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        finally:
            conn.close()
        self.assertEqual(version, dbmod.SCHEMA_VERSION,
                         "schema_meta disagrees with the code's target version")
        self.assertIn("harmonic_of", cols)

    def test_fresh_database_many_times(self):
        # A single attempt is a coin toss; the failure needs a particular
        # interleaving. Many rounds make a pass meaningful.
        for round_ in range(25):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "shared.sqlite")
                outcomes = self._race(path, workers=4)
                errors = [msg for kind, msg in outcomes if kind == "err"]
                self.assertEqual(errors, [], f"round {round_}: {errors}")
                self._assert_consistent(path)

    def test_restart_against_migrated_database(self):
        # The permanent half of the failure: once the version is wrong, every
        # later start must still find a consistent database.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "shared.sqlite")
            dbmod.init_schema(path)
            for _ in range(10):
                outcomes = self._race(path, workers=4)
                self.assertEqual([m for k, m in outcomes if k == "err"], [])
                self._assert_consistent(path)


if __name__ == "__main__":
    unittest.main()
