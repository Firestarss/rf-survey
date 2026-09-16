#!/usr/bin/env python3
"""Web dashboard for the deck, served on the local network.

    python3 tools/dashboard.py [--port 8080] [--bind 0.0.0.0]

Open http://<deck address>:8080 from an iPad or laptop on the same network, or
over Tailscale. One self-contained page — no internet, no libraries, nothing
fetched from outside — because at a field site there is none.

It must never cost the survey samples. On 2026-09-16 two unrelated loads on this
machine — a second analysis worker, then the test suite at nice 19 — each put
overflows on both receivers, because what they compete for is the memory bus as
well as the CPU. So:

  * it runs under SCHED_IDLE (see systemd/rfsurvey-dashboard.service)
  * live receiver state comes from the status files the capture loops publish,
    never from the capture processes
  * database reads are read-only and cached per query, however many browsers are
    polling — the busiest-channels query measured 290-740 ms on a
    1.2-million-event database, so it runs at most once a minute
"""

import argparse
import http.server
import json
import pathlib
import shutil
import socketserver
import sys
import threading
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import deck  # noqa: E402
import status as status_mod  # noqa: E402

PAGE = ROOT / "tools" / "dashboard.html"

# Seconds each answer may be reused. Chosen by cost, not by how live it looks.
TTL = {"status": 2.0, "events": 4.0, "activity": 30.0, "channels": 60.0}


class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}
        self.locks = {}

    def get(self, key, ttl, fn):
        with self.lock:
            hit = self.entries.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            key_lock = self.locks.setdefault(key, threading.Lock())
        # One computation per key at a time; everyone else waits for its answer.
        with key_lock:
            with self.lock:
                hit = self.entries.get(key)
                if hit and time.time() - hit[0] < ttl:
                    return hit[1]
            value = fn()
            with self.lock:
                self.entries[key] = (time.time(), value)
            return value


CACHE = Cache()


def api_status():
    env = deck.read_env()
    ssid, ip, ts = deck.network()
    receivers = {}
    st = status_mod.read_all()
    for rx in deck.RECEIVERS:
        unit = deck.cached(f"unit-{rx}", 5, lambda rx=rx: deck.unit_active(f"rfsurvey@{rx}"))
        s = st.get(rx) or {}
        receivers[rx] = {"unit": unit, **{k: v for k, v in s.items() if k != "pid"}}
    return {
        "now": time.time(),
        "clock_synced": deck.clock_synced(),
        "host": deck.os.uname().nodename,
        "profile": env["RFSURVEY_PROFILE"], "engine": env["RFSURVEY_ENGINE"],
        "db": deck.DB_OVERRIDE or env["RFSURVEY_DB"],
        "receivers": receivers,
        "system": {"temp_c": deck.temp_c(), "load1": deck.load1(),
                   "disk_free_gb": shutil.disk_usage(ROOT).free / 1e9,
                   "wifi": ssid, "ip": ip, "tailscale": ts},
    }


def api_events(limit):
    rows = deck.recent_events(limit)
    if rows is None:
        return {"events": None}
    out = []
    for t, rx, f, dur, snr, ctcss, dcs, pol, ts, harm, ovl, live in rows:
        out.append({"t": t, "rx": rx, "freq_hz": f, "channel": deck.channel_name(f),
                    "duration_s": None if live else dur, "snr_db": snr,
                    "tone": deck.tone_text(ctcss, dcs, pol, ts), "harmonic": harm is not None,
                    "overload": bool(ovl), "on_air": bool(live)})
    return {"events": out}


def api_channels(minutes):
    rows = deck.busiest(minutes, limit=25)
    if rows is None:
        return {"channels": None}
    return {"minutes": minutes, "channels": [
        {"freq_hz": f, "channel": deck.channel_name(f), "rx": rx, "events": n, "airtime_s": air,
         "peak_snr_db": peak, "last": last, "tones": tones or ""}
        for f, rx, n, air, peak, last, tones in rows]}


def api_activity(minutes=60):
    """Events per minute per receiver, ending at the newest event."""
    conn, _ = deck.db_connect()
    if conn is None:
        return {"bins": None}
    try:
        newest = conn.execute("SELECT max(t_start) FROM events").fetchone()[0]
        if newest is None:
            return {"bins": [], "receivers": list(deck.RECEIVERS)}
        end = (int(newest) // 60 + 1) * 60
        start = end - minutes * 60
        rows = conn.execute(
            """SELECT CAST((t_start - ?) / 60 AS INTEGER), receiver_id, count(*)
                 FROM events WHERE t_start >= ? AND harmonic_of IS NULL
             GROUP BY 1, 2""", (start, start)).fetchall()
    finally:
        conn.close()
    bins = [{"t": start + i * 60, **{rx: 0 for rx in deck.RECEIVERS}} for i in range(minutes)]
    for b, rx, n in rows:
        if 0 <= b < minutes and rx in bins[b]:
            bins[b][rx] = n
    return {"bins": bins, "receivers": list(deck.RECEIVERS)}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "rfsurvey-dashboard"

    def log_message(self, fmt, *args):   # keep the journal for things that matter
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/status":
                body = CACHE.get("status", TTL["status"], api_status)
            elif url.path == "/api/events":
                limit = max(1, min(200, int(q.get("limit", ["40"])[0])))
                body = CACHE.get(f"events-{limit}", TTL["events"], lambda: api_events(limit))
            elif url.path == "/api/channels":
                minutes = max(5, min(1440, int(q.get("minutes", ["60"])[0])))
                body = CACHE.get(f"channels-{minutes}", TTL["channels"], lambda: api_channels(minutes))
            elif url.path == "/api/activity":
                body = CACHE.get("activity", TTL["activity"], api_activity)
            else:
                return self._send(404, "not found", "text/plain")
            return self._send(200, json.dumps(body), "application/json")
        except (ValueError, KeyError) as exc:
            return self._send(400, str(exc), "text/plain")
        except Exception as exc:                      # the page must keep working
            return self._send(500, f"{type(exc).__name__}: {exc}", "text/plain")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=deck.DASHBOARD_PORT)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--db", help="browse this database instead of the one the survey is writing")
    args = ap.parse_args()
    if args.db:
        deck.DB_OVERRIDE = str(pathlib.Path(args.db).resolve())
    srv = Server((args.bind, args.port), Handler)
    print(f"dashboard on http://{args.bind}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
