"""Database access for rfsurvey.

Every connection must set its own pragmas — journal_mode persists in the file, but
foreign_keys and busy_timeout are per-connection and silently default to off/zero.
Getting that wrong means constraints that pass in testing and do nothing in the field.

Usage:
    from db import connect, init_schema, start_run, end_run

    init_schema("data/survey.sqlite")          # idempotent
    db  = connect("data/survey.sqlite")
    run = start_run(db, "profiles/festival.yaml")
    ...
    end_run(db, run)
"""

from __future__ import annotations

import pathlib
import socket
import sqlite3
import subprocess
import time

import migrate

SCHEMA_VERSION = 4
SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")


def connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database with the pragmas this project depends on."""
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        db = sqlite3.connect(path, isolation_level=None)  # explicit transactions

    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")      # per-connection, off by default
    db.execute("PRAGMA busy_timeout = 5000")    # writer contention, not an error
    if not readonly:
        db.execute("PRAGMA synchronous = NORMAL")  # safe under WAL, much less fsync
    return db


def _read_version(db: sqlite3.Connection) -> int | None:
    try:
        row = db.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None  # schema_meta does not exist yet — fresh database


def init_schema(path: str) -> None:
    """Create or upgrade the database. Safe to call on an existing one.

    schema.sql describes a fresh database and stamps its own version. Running it
    against an already-migrated database would stamp that older version back and
    make the migration runner replay steps that have already happened, so the
    prior version is read first and restored afterwards.
    """
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        prior = _read_version(db)
        db.executescript(SCHEMA_PATH.read_text())
        if prior is not None:
            db.execute(
                "INSERT OR REPLACE INTO schema_meta (key,value) VALUES ('version',?)",
                (str(prior),))
        migrate.apply(db, SCHEMA_VERSION)

        found = _read_version(db)
        if found != SCHEMA_VERSION:
            raise RuntimeError(
                f"{path} is schema v{found}, this code expects v{SCHEMA_VERSION}")
    finally:
        db.close()


def _git_commit() -> str | None:
    """Record which version of the code produced these rows."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=pathlib.Path(__file__).parent,
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        commit = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=pathlib.Path(__file__).parent,
            capture_output=True, text=True, timeout=5,
        )
        return commit + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None


def start_run(db: sqlite3.Connection, profile_path: str, notes: str | None = None) -> int:
    """Open a run and snapshot the profile verbatim into it.

    The whole YAML goes in the row, not a path to it. Six months from now the file
    on disk will have changed and the run needs to still say what it actually used.
    """
    yaml_text = pathlib.Path(profile_path).read_text()
    name = pathlib.Path(profile_path).stem

    cur = db.execute(
        """INSERT INTO runs (started_at, profile_name, profile_yaml,
                             git_commit, hostname, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (time.time(), name, yaml_text, _git_commit(), socket.gethostname(), notes),
    )
    return cur.lastrowid


def register_receiver(db: sqlite3.Connection, run_id: int, receiver_id: str,
                      serial: str, sample_rate_hz: int, **kw) -> int:
    """Record the as-configured state of one receiver for this run.

    `serial` is the identity. Never pass a device index — USB enumeration order
    changes across reboots and the two radios will silently swap bands.
    """
    fields = ("firmware", "usb_bus", "gain_db", "ppm_error", "attenuator_db", "antenna")
    cur = db.execute(
        f"""INSERT INTO run_receivers
            (run_id, receiver_id, serial, sample_rate_hz, {', '.join(fields)})
            VALUES (?, ?, ?, ?, {', '.join('?' * len(fields))})""",
        (run_id, receiver_id, serial, sample_rate_hz,
         *(kw.get(f) for f in fields)),
    )
    return cur.lastrowid


def end_run(db: sqlite3.Connection, run_id: int) -> None:
    db.execute("UPDATE runs SET ended_at = ? WHERE id = ?", (time.time(), run_id))


def log_event(db: sqlite3.Connection, run_id: int, receiver_id: str, **kw) -> int:
    """Insert one detection. Unknown fields must be omitted, not zeroed."""
    cols = ["run_id", "receiver_id"]
    vals = [run_id, receiver_id]
    allowed = {
        "t_start", "t_end", "duration_s", "freq_hz", "freq_raw_hz", "bandwidth_hz",
        "peak_dbfs", "noise_dbfs", "snr_db", "modulation", "content",
        "ctcss_hz", "dcs_code", "dcs_polarity", "confidence",
        "audio_path", "iq_path", "channel_id", "band_plan_id", "tone_state",
    }
    unknown = set(kw) - allowed
    if unknown:
        raise ValueError(f"not event columns: {sorted(unknown)}")
    ts = kw.get("tone_state")
    if ts is not None and ts not in migrate.TONE_STATES:
        raise ValueError(
            f"tone_state must be one of {migrate.TONE_STATES}, got {ts!r}")
    for k, v in kw.items():
        if v is not None:
            cols.append(k)
            vals.append(v)

    cur = db.execute(
        f"INSERT INTO events ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    return cur.lastrowid


def checkpoint(db: sqlite3.Connection) -> None:
    """Weekly housekeeping. Do not VACUUM while the service is running."""
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "data/survey.sqlite"
    init_schema(target)
    db = connect(target)
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    views = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
    print(f"{target}: schema v{SCHEMA_VERSION}")
    print(f"  tables: {', '.join(tables)}")
    print(f"  views:  {', '.join(views)}")
    print(f"  journal_mode: {db.execute('PRAGMA journal_mode').fetchone()[0]}")
