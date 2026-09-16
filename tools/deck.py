#!/usr/bin/env python3
"""deck — run the RF survey deck from a menu.

    deck                 the menu
    deck status          one-shot status, for scripts and quick checks
    deck events [N]      the last N events
    deck channels [MIN]  busiest channels over the last MIN minutes

Built for SSH from an iPad or laptop onto a headless deck: numbered choices, no
arguments to remember, nothing that needs arrow keys or a mouse. Everything it
changes is a line in systemd/rfsurvey.env, so what it did is always visible.

It reads the survey's state from the status files the capture loops publish in
/dev/shm and from the database read-only. It never talks to a running capture
process, and the heavier screens are one indexed query each — anything that
competes with the reader drops samples (docs/wildfire.md).
"""

import json
import os
import pathlib
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import status as status_mod  # noqa: E402

ENV = ROOT / "systemd" / "rfsurvey.env"
PROFILES = ROOT / "profiles"
RECEIVERS = ("uhf", "vhf")
DASHBOARD = "rfsurvey-dashboard"
DASHBOARD_PORT = 8080

TTY = sys.stdout.isatty()


def c(code, text):
    return f"\033[{code}m{text}\033[0m" if TTY else str(text)


def good(t): return c("32", t)
def warn(t): return c("33", t)
def bad(t): return c("31", t)
def dim(t): return c("2", t)
def bold(t): return c("1", t)


# ---------------------------------------------------------------------------
# Configuration: systemd/rfsurvey.env
# ---------------------------------------------------------------------------

DEFAULTS = {
    "RFSURVEY_PROFILE": "profiles/festival.yaml",
    "RFSURVEY_ENGINE": "python",
    "RFSURVEY_DB": "data/survey.sqlite",
    "RFSURVEY_CAPTURE_DIR": "data/captures/default",
    "RFSURVEY_CAPTURE_MB": "2000",
}


def read_env(path=ENV):
    """KEY=value lines, with the unit's defaults for anything absent."""
    out = dict(DEFAULTS)
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_env(updates, path=ENV):
    """Change keys in place, keep comments and order, append new keys."""
    path = pathlib.Path(path)
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                lines[i] = f"{k}={updates[k]}"
                seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# System facts
# ---------------------------------------------------------------------------

def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def unit_active(unit):
    return run(["systemctl", "is-active", unit]) or "unknown"


def unit_enabled(unit):
    return run(["systemctl", "is-enabled", unit]) or "unknown"


def sudo(*args):
    """systemctl and friends need root; sudo asks for the password once."""
    return subprocess.call(["sudo", *args])


def temp_c():
    try:
        return int(pathlib.Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        return None


def load1():
    try:
        return float(pathlib.Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


_CACHE = {}


def cached(key, seconds, fn):
    """Slow-changing facts, so a 2 s live refresh is not a burst of subprocesses."""
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = fn()
    _CACHE[key] = (time.time(), value)
    return value


def network():
    return cached("network", 15, _network)


def _network():
    ssid = ""
    link = run(["iw", "dev", "wlan0", "link"])
    m = re.search(r"SSID:\s*(.+)", link)
    if m:
        ssid = m.group(1).strip()
    ip = ""
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", run(["ip", "-4", "-o", "addr", "show", "wlan0"]))
    if m:
        ip = m.group(1)
    ts = run(["tailscale", "ip", "-4"], timeout=3).splitlines()
    return ssid, ip, (ts[0] if ts else "")


def clock_synced():
    return cached("clock", 60, lambda: run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]) == "yes")


def profile_info(rel):
    import yaml
    try:
        return yaml.safe_load((ROOT / rel).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


# ---------------------------------------------------------------------------
# Channel names
# ---------------------------------------------------------------------------

_CHANNELS = None


def channel_name(freq_hz):
    """'FRS 19/GMRS 19', 'MURS 3' ... or '' — from tools/seed_band_plan.py."""
    global _CHANNELS
    if _CHANNELS is None:
        _CHANNELS = []
        try:
            import seed_band_plan as sbp
            for r in sbp.add_windows(sbp.channels()):
                _CHANNELS.append((r["freq_lo_hz"], r["freq_hi_hz"], r["label"]))
        except Exception:
            pass
    names = [label for lo, hi, label in _CHANNELS if lo <= freq_hz <= hi]
    return "/".join(dict.fromkeys(names))


def tone_text(ctcss, dcs, pol, tone_state):
    if dcs is not None:
        return f"DCS {int(dcs):03d}{pol or ''}"
    if ctcss:
        return f"{ctcss:.1f} Hz"
    if tone_state == "none":
        return "no tone"
    return ""


# ---------------------------------------------------------------------------
# Database, read-only
# ---------------------------------------------------------------------------

# Set to browse a database other than the one the survey is writing (an archived
# run, or a big one for load-testing the dashboard).
DB_OVERRIDE = None


def db_connect(env=None):
    env = env or read_env()
    path = pathlib.Path(DB_OVERRIDE) if DB_OVERRIDE else ROOT / env["RFSURVEY_DB"]
    if not path.exists():
        return None, path
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    conn.execute("PRAGMA query_only = 1")
    return conn, path


def recent_events(limit=25):
    conn, path = db_connect()
    if conn is None:
        return None
    try:
        return conn.execute(
            """SELECT e.t_start, e.receiver_id, e.freq_hz, e.duration_s, e.snr_db,
                      e.ctcss_hz, e.dcs_code, e.dcs_polarity, e.tone_state,
                      e.harmonic_of, e.overload, e.t_end IS NULL
                 FROM events e ORDER BY e.t_start DESC LIMIT ?""", (limit,)).fetchall()
    finally:
        conn.close()


def busiest(minutes=60, limit=20):
    conn, path = db_connect()
    if conn is None:
        return None
    try:
        # Relative to the newest event rather than to now: after a cold boot the
        # deck's clock can be weeks out, and "the last hour" should still mean
        # the last hour of what it heard.
        newest = conn.execute("SELECT max(t_start) FROM events").fetchone()[0]
        if newest is None:
            return []
        return conn.execute(
            """SELECT freq_hz, receiver_id, count(*), sum(coalesce(duration_s, 0)),
                      max(snr_db), max(t_start),
                      group_concat(DISTINCT CASE WHEN dcs_code IS NOT NULL
                                   THEN printf('DCS %03d', dcs_code)
                                   WHEN ctcss_hz IS NOT NULL THEN printf('%.1f Hz', ctcss_hz) END)
                 FROM events
                WHERE t_start >= ? AND harmonic_of IS NULL
             GROUP BY freq_hz, receiver_id
             ORDER BY count(*) DESC LIMIT ?""", (newest - minutes * 60, limit)).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def clear():
    if TTY:
        print("\033[2J\033[H", end="")


def fmt_age(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def receiver_line(rx, st, detail=False):
    active = unit_active(f"rfsurvey@{rx}")
    s = st.get(rx)
    if active != "active":
        state = bad(active) if active == "failed" else dim(active)
        return [f" {bold(rx.upper())}  {state}"]
    if not s or not s.get("alive") or s.get("age_s", 1e9) > 60:
        return [f" {bold(rx.upper())}  {warn('starting')}  (no status yet)"]
    w = s.get("window") or {}
    centre = f"{w['center_hz']/1e6:.3f} MHz" if w.get("center_hz") else "tuning"
    fps, target = s.get("fps"), s.get("target")
    rate = f"{fps:5.1f}/{target:.0f} fps" if fps and target else "  -  fps"
    if fps and target and fps < 0.97 * target:
        rate = warn(rate)
    ovf = s.get("overflows", 0) or 0
    ovf_t = good(f"overflow {ovf}") if ovf == 0 else (warn if ovf < 50 else bad)(f"overflow {ovf}")
    lines = [f" {bold(rx.upper())}  {good('running')}  {centre:<13} {rate}  {ovf_t}  "
             f"events {s.get('events', 0)} ({s.get('events_per_hr', 0):.0f}/hr)"]
    if detail:
        left = ""
        if w.get("dwell_s") and w.get("opened_at"):
            left = f", {fmt_age(w['opened_at'] + w['dwell_s'] - time.time())} left"
        lin = w.get("linearity") or "checking"
        lin_t = good(lin) if lin == "linear" else (bad(lin) if lin == "compressed" else warn(lin))
        lines.append(f"      window {w.get('label') or ''}{left}   front end {lin_t}   "
                     f"engine {s.get('engine')}  gain {s.get('gain')}")
        cost = s.get("cost_ms")
        lines.append(f"      analysed {s.get('analysed', 0)}  skipped {s.get('skipped', 0)}  "
                     f"aged {s.get('aged', 0)}  harmonics {s.get('harmonics', 0)}  "
                     f"clip {s.get('clip', 0)}  desense {s.get('desense', 0)}"
                     + (f"  {cost:.2f} ms/frame" if cost else "")
                     + f"   run {s.get('run_id')}  up {fmt_age(time.time() - s.get('started_at', time.time()))}")
    return lines


def header(detail=False):
    env = read_env()
    st = status_mod.read_all()
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    clock = "" if clock_synced() else "  " + warn("clock not synced — times may be weeks off")
    print(bold("RF SURVEY DECK") + f"  {os.uname().nodename}   {now}{clock}")
    print(dim("─" * 78))
    print(f" profile {env['RFSURVEY_PROFILE'].replace('profiles/', '')}   engine {env['RFSURVEY_ENGINE']}"
          f"   database {env['RFSURVEY_DB']}")
    for rx in RECEIVERS:
        for line in receiver_line(rx, st, detail):
            print(line)
    t, la = temp_c(), load1()
    free = shutil.disk_usage(ROOT).free / 1e9
    ssid, ip, ts = network()
    t_txt = f"{t:.1f} C" if t is not None else "?"
    if t is not None and t >= 75:
        t_txt = bad(t_txt)
    elif t is not None and t >= 68:
        t_txt = warn(t_txt)
    print(f" temp {t_txt}   load {la if la is not None else '?'}   disk {free:.0f} GB free   "
          f"wifi {ssid or warn('none')} {ip}")
    dash = unit_active(DASHBOARD)
    if dash == "active":
        urls = [f"http://{h}:{DASHBOARD_PORT}" for h in (ip, ts) if h]
        print(f" dashboard {good('on')}  " + "  ".join(urls))
    else:
        print(f" dashboard {dim(dash)}")
    print(dim("─" * 78))


def wait_or_enter(seconds):
    """True if Enter was pressed within `seconds`."""
    if not sys.stdin.isatty():
        time.sleep(seconds)
        return False
    r, _, _ = select.select([sys.stdin], [], [], seconds)
    if r:
        sys.stdin.readline()
        return True
    return False


def ask(prompt, default=None):
    tail = f" [{default}]" if default is not None else ""
    try:
        got = input(f"{prompt}{tail}: ").strip()
    except EOFError:
        return default
    return got if got else default


def confirm(prompt):
    return (ask(prompt + " (y/n)", "n") or "n").lower().startswith("y")


def choose(title, options):
    """options: list of (key, label). Returns the key, or None."""
    print(bold(title))
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i:>2}  {label}")
    print("   b  back")
    got = ask("choose")
    if not got or got.lower() == "b":
        return None
    try:
        return options[int(got) - 1][0]
    except (ValueError, IndexError):
        print(warn("not one of the choices"))
        return None


def pause():
    ask(dim("press Enter to continue"))


def which_receivers():
    k = choose("Which receivers?", [("both", "both"), ("uhf", "UHF"), ("vhf", "VHF")])
    if k is None:
        return []
    return list(RECEIVERS) if k == "both" else [k]


# -- actions -----------------------------------------------------------------

def screen_live():
    while True:
        clear()
        header(detail=True)
        print(dim(" updates every 2 s — press Enter to go back"))
        if wait_or_enter(2.0):
            return


def screen_start():
    rxs = which_receivers()
    if not rxs:
        return
    sudo("systemctl", "start", *[f"rfsurvey@{r}" for r in rxs])
    print("waiting for the receivers to report (the front-end check takes a few seconds)...")
    deadline = time.time() + 45
    while time.time() < deadline:
        st = status_mod.read_all()
        if all(st.get(r, {}).get("alive") and (st[r].get("window") or {}).get("center_hz") for r in rxs):
            break
        time.sleep(1)
    for r in rxs:
        for line in receiver_line(r, status_mod.read_all(), detail=True):
            print(line)
    pause()


def screen_stop():
    rxs = which_receivers()
    if not rxs:
        return
    print("stopping — each receiver closes its window and finishes queued analyses (up to 90 s)...")
    sudo("systemctl", "stop", *[f"rfsurvey@{r}" for r in rxs])
    for r in rxs:
        print(f" {r.upper()}  {unit_active('rfsurvey@' + r)}")
    pause()


def screen_restart():
    rxs = which_receivers()
    if not rxs:
        return
    sudo("systemctl", "restart", *[f"rfsurvey@{r}" for r in rxs])
    print("restarted.")
    pause()


def restart_offer():
    running = [r for r in RECEIVERS if unit_active(f"rfsurvey@{r}") == "active"]
    if running and confirm(f"Restart the running receivers ({', '.join(running)}) so this takes effect?"):
        sudo("systemctl", "restart", *[f"rfsurvey@{r}" for r in running])
        print("restarted.")
    elif running:
        print("Takes effect the next time the survey starts.")


def screen_events():
    n = ask("How many", "25")
    try:
        n = max(1, min(500, int(n)))
    except ValueError:
        n = 25
    while True:
        clear()
        header()
        rows = recent_events(n)
        if rows is None:
            print(warn(" no database yet — it is created when the survey first starts"))
        else:
            print(bold(f" {'time UTC':<9} {'rx':<3} {'MHz':>9} {'channel':<16} {'dur s':>6} {'snr':>5}  tone"))
            for t, rx, f, dur, snr, ctcss, dcs, pol, ts, harm, ovl, live in rows:
                name = channel_name(f)[:16]
                flags = (" " + warn("harmonic") if harm else "") + (" " + warn("overload") if ovl else "")
                d = "on air" if live else f"{dur:6.2f}"
                print(f" {time.strftime('%H:%M:%S', time.gmtime(t)):<9} {rx:<3} {f/1e6:9.4f} {name:<16} "
                      f"{d:>6} {snr or 0:5.1f}  {tone_text(ctcss, dcs, pol, ts)}{flags}")
        print(dim(" refreshes every 3 s — press Enter to go back"))
        if wait_or_enter(3.0):
            return


def screen_channels():
    m = ask("Over how many minutes", "60")
    try:
        m = max(1, min(24 * 60, int(m)))
    except ValueError:
        m = 60
    clear()
    header()
    rows = busiest(m)
    if rows is None:
        print(warn(" no database yet"))
    elif not rows:
        print(" nothing heard in that period")
    else:
        print(bold(f" {'MHz':>9} {'channel':<16} {'rx':<3} {'events':>6} {'airtime':>8} {'peak':>5}  tones"))
        for f, rx, n, air, peak, last, tones in rows:
            print(f" {f/1e6:9.4f} {channel_name(f)[:16]:<16} {rx:<3} {n:>6} {air:7.0f}s {peak or 0:5.1f}  {tones or ''}")
    pause()


def screen_profile():
    env = read_env()
    options = []
    for p in sorted(PROFILES.glob("*.yaml")):
        d = profile_info(f"profiles/{p.name}")
        rx = d.get("receivers") or {}
        desc = []
        for name, cfg in rx.items():
            wins = ", ".join(f"{w['center_hz']/1e6:g}" for w in cfg.get("windows") or [])
            desc.append(f"{name} {cfg.get('sample_rate', 0)/1e6:g} MSPS [{wins}]")
        mark = "  <- current" if env["RFSURVEY_PROFILE"] == f"profiles/{p.name}" else ""
        options.append((f"profiles/{p.name}", f"{p.name}{mark}\n      " + "   ".join(desc)))
    k = choose("Profile", options)
    if not k:
        return
    write_env({"RFSURVEY_PROFILE": k})
    print(f"profile set to {k}")
    d = profile_info(k)
    rates = [cfg.get("sample_rate", 0) for cfg in (d.get("receivers") or {}).values()]
    if read_env()["RFSURVEY_ENGINE"] == "python" and sum(r >= 10e6 for r in rates) >= 2:
        print(warn("Two receivers at 10 MSPS on the python engine measured thousands of overflows. "
                   "Use the rust engine (menu 8)."))
    restart_offer()
    pause()


def screen_engine():
    env = read_env()
    k = choose(f"Engine (current: {env['RFSURVEY_ENGINE']})", [
        ("rust", "rust    — both receivers at 10 MSPS measured 0 overflows"),
        ("python", "python  — the original; needs profiles/wildfire-fallback.yaml for two radios"),
    ])
    if not k:
        return
    updates = {"RFSURVEY_ENGINE": k}
    if k == "rust" and not (ROOT / "engine/target/release/rfsurvey-engine").exists():
        print(bad("The rust engine is not built: cd ~/rfsurvey/engine && cargo build --release"))
        pause()
        return
    if k == "python" and env["RFSURVEY_PROFILE"] != "profiles/wildfire-fallback.yaml":
        if confirm("Also switch to profiles/wildfire-fallback.yaml (vhf at 2.5 MSPS)?"):
            updates["RFSURVEY_PROFILE"] = "profiles/wildfire-fallback.yaml"
    write_env(updates)
    print("set: " + ", ".join(f"{a}={b}" for a, b in updates.items()))
    restart_offer()
    pause()


def screen_database():
    env = read_env()
    conn, path = db_connect(env)
    if conn is not None:
        try:
            n = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        print(f"current: {env['RFSURVEY_DB']}  ({path.stat().st_size/1e6:.0f} MB, {n} events)")
    else:
        print(f"current: {env['RFSURVEY_DB']}  (not created yet)")
    name = ask("New run name, letters/digits/-/_ (e.g. wildfire-day2), blank to cancel", "")
    if not name:
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", name):
        print(warn("use letters, digits, - and _ only"))
        pause()
        return
    db_rel = f"data/{name}.sqlite"
    if (ROOT / db_rel).exists() and not confirm(f"{db_rel} exists — add to it?"):
        return
    # Audio is stored by run number, and run numbers restart in every database,
    # so each database needs its own capture directory or they overwrite.
    write_env({"RFSURVEY_DB": db_rel, "RFSURVEY_CAPTURE_DIR": f"data/captures/{name}"})
    print(f"database {db_rel}, audio data/captures/{name}")
    restart_offer()
    pause()


def screen_padcal():
    import yaml
    env = read_env()
    prof = profile_info(env["RFSURVEY_PROFILE"])
    rx = choose("Measure which receiver?", [(r, r.upper()) for r in RECEIVERS if r in (prof.get("receivers") or {})])
    if not rx:
        return
    cfg = prof["receivers"][rx]
    wins = cfg.get("windows") or []
    k = choose("At which frequency?", [(w["center_hz"], f"{w['center_hz']/1e6:.3f} MHz  {w.get('label', '')}") for w in wins])
    if not k:
        return
    pad = cfg.get("attenuator_db") or 0
    print(f"\n{rx.upper()}: serial {cfg.get('serial')}, {pad} dB of attenuation per the profile, gain now {cfg.get('gain')}.")
    print("This stops the receiver while it measures (about 2 minutes). You will be asked to fit the")
    print("ANTENNA, then swap it for the DUMMY LOAD — and to put the antenna back afterwards.")
    if not confirm("Go ahead?"):
        return
    was_running = unit_active(f"rfsurvey@{rx}") == "active"
    if was_running:
        print(f"stopping {rx}...")
        sudo("systemctl", "stop", f"rfsurvey@{rx}")
    cmd = [sys.executable, str(ROOT / "tools/padcal.py"), "--serial", str(cfg.get("serial")),
           "--freq", str(k), "--ppm", str(cfg.get("ppm") or 0), "--pad", str(pad)]
    out = []
    prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        for line in proc.stdout:
            print(line, end="")
            out.append(line)
        proc.wait()
    finally:
        signal.signal(signal.SIGINT, prev)
    text = "".join(out)
    print(bold("\n*** Put the ANTENNA back on before restarting. ***\n"))
    m = re.search(r"usable gains:\s*([\d., ]+)", text)
    if m:
        usable = [float(x) for x in re.findall(r"[\d.]+", m.group(1))]
        lowest = min(usable)
        if float(cfg.get("gain") or 0) != lowest and confirm(
                f"Lowest linear gain here is {lowest:g}. Set {rx} gain to {lowest:g} in {env['RFSURVEY_PROFILE']}?"):
            if set_profile_gain(env["RFSURVEY_PROFILE"], rx, lowest):
                print(good(f"{rx} gain set to {lowest:g}"))
            else:
                print(bad("could not edit the profile safely; change the gain line by hand"))
    fit = re.search(r"FIT (\d+) dB", text)
    if fit and int(fit.group(1)) != int(pad):
        print(warn(f"padcal suggests {fit.group(1)} dB of attenuation; {pad} dB is fitted. "
                   f"Change the pads and attenuator_db in the profile if you agree."))
    if was_running and confirm(f"Antenna back on? Restart {rx}?"):
        sudo("systemctl", "start", f"rfsurvey@{rx}")
    pause()


def set_profile_gain(rel, rx, gain):
    """Change `gain:` inside one receiver's block, keep every comment, verify."""
    import yaml
    path = ROOT / rel
    lines = path.read_text().split("\n")
    start = next((i for i, l in enumerate(lines) if l.rstrip() == f"  {rx}:"), None)
    if start is None:
        return False
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^  \S", lines[i]) or re.match(r"^\S", lines[i])), len(lines))
    hits = [i for i in range(start, end) if re.match(r"^    gain:\s", lines[i])]
    if len(hits) != 1:
        return False
    i = hits[0]
    lines[i] = re.sub(r"^(    gain:\s*)[\d.]+", lambda m: f"{m.group(1)}{gain:g}", lines[i])
    new = "\n".join(lines)
    try:
        if yaml.safe_load(new)["receivers"][rx]["gain"] != gain:
            return False
    except Exception:
        return False
    path.write_text(new)
    return True


def screen_preflight():
    sudo(str(ROOT / "systemd/rfsurvey-preflight"))
    pause()


def screen_dashboard():
    active = unit_active(DASHBOARD)
    enabled = unit_enabled(DASHBOARD)
    ssid, ip, ts = network()
    print(f"web dashboard: {active}, at boot: {enabled}")
    for h, what in ((ip, f"on {ssid or 'this wifi'}"), (ts, "over Tailscale"), ("radio-deck.local", "by name")):
        if h:
            print(f"   http://{h}:{DASHBOARD_PORT}   {dim(what)}")
    k = choose("Dashboard", [("start", "start now"), ("stop", "stop now"),
                             ("enable", "start at every boot"), ("disable", "do not start at boot")])
    if k in ("start", "stop"):
        sudo("systemctl", k, DASHBOARD)
    elif k in ("enable", "disable"):
        sudo("systemctl", k, DASHBOARD)
    if k:
        print(f"dashboard: {unit_active(DASHBOARD)}, at boot: {unit_enabled(DASHBOARD)}")
        pause()


def screen_boot():
    units = [f"rfsurvey@{r}" for r in RECEIVERS] + [DASHBOARD]
    for u in units:
        print(f"  {u:<22} at boot: {unit_enabled(u)}")
    k = choose("Start at boot", [(("enable", u), f"enable {u}") for u in units]
               + [(("disable", u), f"disable {u}") for u in units])
    if k:
        sudo("systemctl", k[0], k[1])
        print(f"{k[1]}: {unit_enabled(k[1])}")
        pause()


def screen_logs():
    k = choose("Logs for", [("rfsurvey@uhf", "UHF receiver"), ("rfsurvey@vhf", "VHF receiver"),
                            (DASHBOARD, "dashboard"), ("rfsurvey-netwatch", "network watchdog"),
                            ("rfsurvey-preflight", "boot checks")])
    if not k:
        return
    print(dim("following — Ctrl-C to go back"))
    prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        subprocess.call(["journalctl", "-u", k, "-n", "40", "-f", "--no-pager"],
                        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    finally:
        signal.signal(signal.SIGINT, prev)


def screen_power():
    k = choose("Power", [
        ("ready", "WildFire ready: stop, report, archive the soak, check, power off"),
        ("ready-dry", "WildFire ready — dry run, changes nothing"),
        ("reboot", "reboot (keeps the clock)"),
        ("poweroff", "power off (the clock is lost when power is removed)"),
    ])
    if k == "ready":
        sudo(str(ROOT / "tools/wildfire-ready"))
    elif k == "ready-dry":
        subprocess.call([str(ROOT / "tools/wildfire-ready"), "--dry-run"])
    elif k in ("reboot", "poweroff") and confirm(f"Really {k}?"):
        sudo("systemctl", k)
    if k:
        pause()


MENU = [
    ("Live status", screen_live),
    ("Start survey", screen_start),
    ("Stop survey", screen_stop),
    ("Restart survey", screen_restart),
    ("Recent events (live)", screen_events),
    ("Busiest channels", screen_channels),
    ("Change profile", screen_profile),
    ("Change engine", screen_engine),
    ("New run / database", screen_database),
    ("Measure antenna & attenuation (padcal)", screen_padcal),
    ("Boot checks", screen_preflight),
    ("Web dashboard", screen_dashboard),
    ("Start at boot", screen_boot),
    ("Logs", screen_logs),
    ("WildFire ready / reboot / power off", screen_power),
]


def menu():
    while True:
        clear()
        header()
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i:>2}  {label}")
        print("   q  quit")
        got = ask("choose")
        if got is None or got.lower() in ("q", "quit", "exit"):
            return
        try:
            fn = MENU[int(got) - 1][1]
        except (ValueError, IndexError):
            continue
        clear()
        try:
            fn()
        except KeyboardInterrupt:
            print()


def main(argv):
    if len(argv) > 1:
        cmd = argv[1]
        if cmd == "status":
            header(detail=True)
            return 0
        if cmd == "events":
            n = int(argv[2]) if len(argv) > 2 else 25
            rows = recent_events(n) or []
            for t, rx, f, dur, snr, ctcss, dcs, pol, ts, harm, ovl, live in rows:
                print(f"{time.strftime('%H:%M:%S', time.gmtime(t))} {rx} {f/1e6:.4f} {channel_name(f)} "
                      f"{'on air' if live else f'{dur:.2f}s'} {snr or 0:.1f}dB {tone_text(ctcss, dcs, pol, ts)}")
            return 0
        if cmd == "channels":
            m = int(argv[2]) if len(argv) > 2 else 60
            for f, rx, n, air, peak, last, tones in busiest(m) or []:
                print(f"{f/1e6:.4f} {channel_name(f)} {rx} {n} events {air:.0f}s {tones or ''}")
            return 0
        print(__doc__)
        return 2
    if not sys.stdin.isatty():
        header(detail=True)
        return 0
    try:
        menu()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
