"""Shared behaviour for the command-line entry points.

Small, and deliberately not a grab bag: everything here is something all of
enrich.py, seed_band_plan.py and make_fixtures.py need in exactly the same way.
"""

from __future__ import annotations

import signal


def quiet_broken_pipe() -> None:
    """Die quietly when a reader closes the pipe.

    Python turns SIGPIPE into an exception, so `python3 src/enrich.py db | head`
    raises BrokenPipeError and prints a traceback the moment head has seen
    enough. Restoring the default handler makes these behave like every other
    command-line tool.
    """
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass  # not POSIX, or not on the main thread
