"""Shared setup for the test suite.

No third-party dependencies. The deck is a Raspberry Pi that goes into a backpack
and the tests have to run on it, so this is stdlib unittest and nothing else.

Run everything from the repository root:

    python3 -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PROFILE = ROOT / "profiles" / "festival.yaml"


class TempDirCase(unittest.TestCase):
    """A test that needs somewhere to put a database or a capture."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def path(self, name):
        return str(self.tmp / name)


def build_at_version(path, version):
    """A database at exactly `version`, built the way a real one would be.

    schema_v2.sql stamps v2 and the migrations carry it forward, so this is the
    only honest way to produce an old database to upgrade from — hand-writing the
    old shape would test a schema that never existed.
    """
    import migrate

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript((SRC / "schema_v2.sql").read_text())
    if version > migrate.current_version(conn):
        migrate.apply(conn, version)
    assert migrate.current_version(conn) == version, (
        f"wanted v{version}, got v{migrate.current_version(conn)}")
    return conn


def schema_of(conn):
    """Every object in the schema, normalised for comparison."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    return [(r["type"], r["name"], " ".join((r["sql"] or "").split())) for r in rows]
