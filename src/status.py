"""Live state of each receiver, published for the deck tool and the dashboard.

Every capture loop — Python or rust engine — writes one small JSON file per
receiver on each stats interval and window change. Readers never touch the
capture process, its database connection or the radio; they read a file. That
matters because anything that competes with the reader drops samples: running
the test suite on the deck during a soak on 2026-09-16 took both receivers from
zero overflows to eleven.

/dev/shm rather than the NVMe: it is written every 15 s for the life of a
deployment, it is only meaningful while the process that wrote it is alive, and
it should not survive a reboot claiming a receiver is running.
"""

import json
import os
import pathlib
import time

# Overridable so the test suite cannot publish into the live deck's status:
# before this, a test run using receiver id "uhf" overwrote the real uhf file.
DIR = pathlib.Path(os.environ.get("RFSURVEY_STATUS_DIR", "/dev/shm"))
PREFIX = "rfsurvey-status-"


def path(receiver):
    return DIR / f"{PREFIX}{receiver}.json"


def write(receiver, payload):
    """Atomic: a reader sees the old file or the new one, never half of one."""
    payload = dict(payload, receiver=receiver, pid=os.getpid(), updated_at=time.time())
    target = path(receiver)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, target)
    except OSError:
        # Publishing state must never stop a survey.
        try:
            tmp.unlink()
        except OSError:
            pass


def read_all():
    """{receiver: payload} with `alive` and `age_s` filled in."""
    out = {}
    for p in sorted(DIR.glob(f"{PREFIX}*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        rx = data.get("receiver") or p.stem[len(PREFIX):]
        pid = data.get("pid")
        alive = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
        data["alive"] = alive and data.get("state") != "stopped"
        data["age_s"] = time.time() - float(data.get("updated_at") or 0)
        out[rx] = data
    return out
