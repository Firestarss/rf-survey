"""The capture loop driving the Rust engine (`--engine rust`).

Three processes per receiver instead of one:

  rfsurvey-engine   reads the radio and makes every per-frame decision: spectrum,
                    noise floor, detection, overload, which events to analyse
                    and which are harmonics. engine/src/.
  this process      the orchestrator — the database, window rotation, captures,
                    and every message an operator reads. Replays the engine's
                    decisions in the order the engine made them.
  analysis          analyze_analog() in its own process, reading IQ straight out
                    of the engine's shared-memory ring.

Why. Two receivers at 10 MSPS on the Python engine dropped 2,177 and 1,899
blocks of samples in 6.5 minutes on 2026-09-16; and one receiver at 10 MSPS
fell ~2.5 overflows/s behind under Boston traffic while keeping up in a quiet
window, because analysis shared the reader's interpreter. Nothing in this
process is on the sample path any more, so it can be as slow as the database
makes it without losing a sample.

The Python engine is untouched and remains the default. This path is only
trusted because tests/test_engine_equivalence.py shows the engine makes the
same detections on the same samples, and tests/test_engine_loop.py shows this
loop turns them into the same database rows.
"""

import json
import os
import pathlib
import queue
import signal
import subprocess
import sys
import threading
import time

import numpy as np

import db
import survey_prototype as sp

ENGINE = pathlib.Path(__file__).resolve().parent.parent / "engine" / "target" / "release" / "rfsurvey-engine"

# Jobs waiting for the analysis process. Kept shallow for the same reason as the
# thread version: when analysis cannot keep up, skip it and count it, and keep
# every event detected and timed.
ANALYSIS_DEPTH = 2

# Analysis processes sharing that queue. ONE, and measured to be one, on
# 2026-09-16 with both receivers at 10 MSPS under ~22,000 events/hour of Boston
# traffic on uhf:
#
#   workers  priority  overflows (uhf / vhf)  engine ms/frame  analyses skipped
#      1      normal    0 / 0 in 6.5 min          1.8               8.1%
#      2      normal    1 / 0 in 4.5 min          2.8               5.1%
#      2      nice 15   1 / 1 in 4.5 min          2.6               5.1%
#
# A second worker analyses more, and costs the engines headroom, and lowering its
# priority did not buy that back: what it competes for is the memory bus as much
# as CPU time, and scheduling priority does not arbitrate that. A skipped
# analysis loses one event's tone; an overflow corrupts every level and duration
# in the window. The trade goes to the samples.
ANALYSIS_WORKERS = 1
ANALYSIS_NICE = 15

RING_MAGIC = int.from_bytes(b"RFSVRING", "little")

# Headroom in the shared ring beyond one analysis window. The Python engine's
# 0.7 s was enough because it copied an event's IQ the instant it decided to
# analyse it. Here the analysis process reads later — after whatever job is
# ahead of it — and a slice overwritten in the meantime is discarded ("aged").
# 3 s is 240 MB more per receiver at 10 MSPS, which 8 GB affords. It changes no
# detection decision: at decision time the slice is held with either slack.
RING_SLACK_SECONDS = 3.0


# ---------------------------------------------------------------------------
# Analysis process
# ---------------------------------------------------------------------------

def read_ring(mm, generation, start, n):
    """IQ `[start, start+n)` from the engine's ring, or None if it is gone.

    Copy, then re-check: the engine may have written past the slice, or retuned,
    while this was copying. A copy that raced either is discarded rather than
    analysed — samples from the wrong moment, or the wrong band, would produce a
    confident wrong answer.
    """
    hdr = np.frombuffer(mm, dtype="<u8", count=8)
    if int(hdr[0]) != RING_MAGIC:
        return None
    cap = int(hdr[1])

    def held():
        written, gen = int(hdr[2]), int(hdr[3])
        return gen == generation and start + n <= written and start + cap >= written

    if n <= 0 or not held():
        return None
    data = np.frombuffer(mm, dtype=np.complex64, offset=64, count=cap)
    out = np.empty(n, np.complex64)
    pos = start % cap
    end = pos + n
    if end <= cap:
        out[:] = data[pos:end]
    else:
        split = cap - pos
        out[:split] = data[pos:]
        out[split:] = data[:end - cap]
    return out if held() else None


def analysis_main(jobs, results, shm_path):
    """Entry point of the analysis process."""
    # systemd signals every process in the unit. The orchestrator owns shutdown
    # and ends this with a None job once queued work is done.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Analysis yields the CPU to the engines. Not sufficient on its own to make a
    # second worker safe (see ANALYSIS_WORKERS), but it can only help the reader.
    try:
        os.nice(ANALYSIS_NICE)
    except OSError:
        pass
    mm = None
    while True:
        job = jobs.get()
        if job is None:
            return
        row, ch, gen, start, n, rate, offset_hz, freq_hz, snr, keep = job
        if mm is None:
            try:
                mm = np.memmap(shm_path, dtype=np.uint8, mode="r")
            except OSError as exc:
                print(f"  analysis cannot map {shm_path}: {exc}", file=sys.stderr)
                results.put((row, ch, None, freq_hz, snr, 0.0, "unmapped"))
                continue
        iq = read_ring(mm, gen, start, n)
        if iq is None:
            results.put((row, ch, None, freq_hz, snr, 0.0, "aged"))
            continue
        t0 = time.perf_counter()
        try:
            result = sp.analyze_analog(iq, rate, offset_hz, keep_signals=keep)
            why = None
        except Exception as exc:                  # never kill the process
            print(f"  analysis failed on {freq_hz/1e6:.4f} MHz: {exc}", file=sys.stderr)
            result, why = None, "failed"
        results.put((row, ch, result, freq_hz, snr, (time.perf_counter() - t0) * 1000.0, why))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class EngineGone(Exception):
    pass


class EngineCaptureLoop:
    STAT_SECONDS = 15.0

    def __init__(self, settings, args, windows, iq_file=None, file_wall0=None):
        self.settings = settings
        self.args = args
        self.windows = windows
        self.iq_file = iq_file
        self.shm_path = f"/dev/shm/rfsurvey-{args.receiver_id}-{os.getpid()}.iq"

        # The analysis process is started before any thread exists, so fork is
        # safe: nothing can be holding a lock at the moment of the copy.
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        self.jobs = ctx.Queue(maxsize=ANALYSIS_DEPTH)
        self.results = ctx.Queue()
        self.analysis = [ctx.Process(target=analysis_main, name=f"analysis-{i}",
                                     args=(self.jobs, self.results, self.shm_path),
                                     daemon=True)
                         for i in range(ANALYSIS_WORKERS)]
        for proc in self.analysis:
            proc.start()

        rate_req = float(settings["rate"])
        cfg = {
            "rate": rate_req, "gain": float(settings["gain"]), "ppm": float(settings["ppm"] or 0.0),
            "initial_center_hz": float(windows[0]["center_hz"]),
            "fs_request": sp.frame_size(rate_req),
            "on_db": settings["on_db"], "off_db": settings["off_db"],
            "min_duration_s": settings["min_duration_s"], "hang_s": settings["hang_s"],
            "pretrigger_s": sp.PRETRIGGER_SECONDS, "analyze_s": sp.ANALYZE_SECONDS,
            "min_analyze_s": sp.MIN_ANALYZE_SECONDS, "ring_slack_s": RING_SLACK_SECONDS,
            "shm_path": self.shm_path, "stat_seconds": self.STAT_SECONDS,
            "gain_step": sp.COMPRESSION_GAIN_STEP, "compression_seconds": sp.COMPRESSION_SECONDS,
            "max_gain": sp.MAX_GAIN_DB,
        }
        if iq_file is not None:
            cfg.update({"iq_file": str(iq_file), "file_mtu": 65536, "file_realtime": True,
                        "file_wall0": time.time() if file_wall0 is None else file_wall0})
        else:
            cfg["device_args"] = sp.device_args(args.driver, settings["serial_want"])

        if not ENGINE.exists():
            raise SystemExit(f"--engine rust: {ENGINE} is not built "
                             f"(cargo build --release in engine/)")
        self.proc = subprocess.Popen([str(ENGINE), json.dumps(cfg)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.inbox = queue.Queue()
        self.stdout_thread = threading.Thread(target=self._read_engine, name="engine-stdout",
                                              daemon=True)
        self.stdout_thread.start()

        self.ready = self._wait_for("ready", timeout=30.0)
        self.rate = float(self.ready["rate"])
        self.fs = int(self.ready["fs"])
        self.frame_seconds = float(self.ready["frame_seconds"])
        # A file run must not claim the serial of a radio it never touched —
        # run_receivers.serial is what every later row is interpreted against.
        # --simulate records SIM-<receiver> for the same reason.
        if iq_file is not None:
            self.serial = f"FILE-{args.receiver_id.upper()}"
        else:
            self.serial = self.ready.get("serial") or settings["serial_want"] or "unknown"

        self.conn = None
        self.run_id = None
        self.log = None
        self.store = None
        self.window_id = None
        self.window_t0 = time.time()
        self.center = None
        self.freqs_hz = None
        self.generation = None
        self.engine_exit = None

        self.events_logged = 0
        self.harmonics_found = 0
        self.analyses = 0
        self.analyses_total = 0
        self.analyses_skipped = 0
        self.analyses_aged = 0
        self.analyze_ms = 0.0
        self.last_engine_stats = {}
        self.session_start = time.time()

        self.running = threading.Event()
        self.running.set()
        signal.signal(signal.SIGINT, lambda *_: self.running.clear())

    # -- engine I/O ----------------------------------------------------------

    def _read_engine(self):
        for line in self.proc.stdout:
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                print(f"  engine sent something unreadable: {line[:120]!r}", file=sys.stderr)
        self.inbox.put({"t": "__eof__"})

    def _send(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def _wait_for(self, kind, timeout):
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise SystemExit(f"engine did not send '{kind}' within {timeout:.0f} s")
            try:
                m = self.inbox.get(timeout=left)
            except queue.Empty:
                continue
            if m["t"] == kind:
                return m
            if m["t"] == "error":
                raise SystemExit(f"engine: {m['msg']}")
            if m["t"] == "__eof__":
                raise SystemExit(f"engine exited before sending '{kind}'")
            self._handle(m)

    # -- database ------------------------------------------------------------

    def attach(self, conn, run_id):
        self.conn = conn
        self.run_id = run_id
        self.log = sp.EventLog(conn, run_id, self.args.receiver_id)
        if self.args.capture_dir:
            self.store = sp.CaptureStore(self.args.capture_dir, run_id,
                                         max_mb=self.args.capture_mb,
                                         keep_iq=self.args.capture_iq)

    # -- the loop ------------------------------------------------------------

    def go(self):
        """Visit every window in turn until stopped. Returns an exit status."""
        win_idx = 0
        status = 0
        try:
            while self.running.is_set():
                w = self.windows[win_idx % len(self.windows)]
                dwell = w.get("dwell_s") or self.settings["dwell_s"]
                deadline = time.time() + dwell if len(self.windows) > 1 else None
                self.open_window(w, dwell if deadline is not None else None)
                while self.running.is_set() and (deadline is None or time.time() < deadline):
                    self.pump(0.2)
                win_idx += 1
        except EngineGone as gone:
            status = self._engine_gone(str(gone))
        finally:
            self.finish()
        return status

    def open_window(self, w, dwell_s):
        linearity = not self.iq_file
        self._send({"cmd": "open", "center_hz": float(w["center_hz"]), "linearity": linearity})
        tuned = self._wait_for("tuned", timeout=60.0)
        # The previous window's decisions and its close arrived before "tuned"
        # and were handled on the way; everything from here belongs to this one.
        self.center = float(tuned["center_hz"])
        self.window_id = db.open_window(self.conn, self.run_id, self.args.receiver_id,
                                        int(self.center), int(self.rate), w["label"])
        self.log.window_id = self.window_id
        print(f"\n== {self.center/1e6:.3f} MHz"
              + (f" ({w['label']})" if w["label"] else "")
              + (f", {dwell_s:.0f} s" if dwell_s else "")
              + " ==")
        if not linearity:
            db.set_window_linearity(self.conn, self.window_id, "unchecked")
        win = self._wait_for("window", timeout=60.0)
        self._apply_window(win)

    def _apply_window(self, m):
        self.window_t0 = float(m["wall0"])
        self.generation = int(m["gen"])
        self.freqs_hz = np.asarray(m["channels"], dtype=np.float64) * sp.CHANNEL_HZ
        # Verify at the point of effect: the engine's grid must be the grid the
        # Python engine would have built for this centre, or every frequency in
        # the database is wrong in a way nothing downstream can detect.
        expect = sp.ChannelGrid(self.center, self.rate)
        if len(m["channels"]) != expect.n or not np.array_equal(
                np.asarray(m["channels"], dtype=np.int64), expect.channels):
            print(f"   ** ENGINE CHANNEL GRID DISAGREES WITH PYTHON at {self.center/1e6:.4f} MHz "
                  f"({len(m['channels'])} vs {expect.n} channels). Frequencies in this "
                  f"window cannot be trusted.", file=sys.stderr)

    def pump(self, timeout):
        """Handle everything waiting from the engine and the analysis process."""
        try:
            m = self.inbox.get(timeout=timeout)
            self._handle(m)
            while True:
                self._handle(self.inbox.get_nowait())
        except queue.Empty:
            pass
        self._collect()

    def _handle(self, m):
        t = m["t"]
        if t == "f":
            self._frame(m)
        elif t == "close":
            self._close(m)
        elif t == "linearity":
            self._linearity(m)
        elif t == "window":
            self._apply_window(m)
        elif t == "tuned":
            self.center = float(m["center_hz"])
        elif t == "floor":
            self._antenna(m["median_db"])
        elif t == "stats":
            self.last_engine_stats = m
            self._stats(m)
        elif t in ("stall", "eof", "__eof__", "error"):
            self.engine_exit = m
            raise EngineGone(t)

    def _frame(self, m):
        t_started = self.window_t0 + m["ts"] / self.rate
        t_ended = self.window_t0 + m["te"] / self.rate
        for act in m["acts"]:
            kind, ch = act[0], int(act[1])
            if kind == "s":
                self.log.start(ch, t_started, self.freqs_hz[ch], overload=bool(m["ovl"]))
            elif kind == "a":
                _, _, start, n, peak = act
                row = self.log.open_rows.get(ch)
                if row is None:
                    continue
                job = (row, ch, self.generation, int(start), int(n), self.rate,
                       float(self.freqs_hz[ch] - self.center), float(self.freqs_hz[ch]),
                       float(peak), self.store is not None)
                try:
                    self.jobs.put_nowait(job)
                except queue.Full:
                    self.analyses_skipped += 1
            elif kind == "h":
                _, _, parent, n = act
                row = self.log.open_rows.get(ch)
                prow = self.log.open_rows.get(int(parent))
                if row is not None and prow is not None:
                    self.log.mark_harmonic(row, prow, int(n))
                    self.harmonics_found += 1
            elif kind == "e":
                _, _, dur, peak = act
                if self.log.close(ch, t_ended, float(dur), float(peak)) is not None:
                    self.events_logged += 1
                    print(f"  {self.freqs_hz[ch]/1e6:10.4f} MHz  ended, {float(dur):.2f} s")

    def _close(self, m):
        """The engine retuned or stopped: anything still keyed is closed."""
        if self.log is None:
            return
        peaks = {int(ch): float(p) for ch, p in m["open"]}
        t_end = self.window_t0 + m["written"] / self.rate
        for ch in list(self.log.open_rows):
            self.log.close(ch, t_end, None, peaks.get(ch, 0.0))
            self.events_logged += 1     # closed by the retune, but still logged
        if self.window_id is not None:
            db.close_window(self.conn, self.window_id)
            self.window_id = None

    def _linearity(self, m):
        verdict = m["verdict"]
        if self.window_id is not None:
            db.set_window_linearity(self.conn, self.window_id, verdict)
        gain = float(self.settings["gain"])
        if verdict == "compressed":
            print(f"   ** FRONT END COMPRESSED at gain {gain:.0f} — this band is too strong for "
                  f"the current attenuation.\n"
                  f"      Levels logged from this window are understated and clipping will NOT "
                  f"report it.\n      Add attenuation ahead of the receiver.", file=sys.stderr)
        elif verdict == "inconclusive":
            print(f"   front-end linearity inconclusive — gain {gain:.0f} may be below the point "
                  f"where the noise floor responds at all", file=sys.stderr)
        elif verdict == "linear":
            print("   front end linear")

    def _antenna(self, floor):
        expect = self.settings.get("dummy_floor_dbfs")
        if expect is None or self.iq_file:
            return
        if abs(float(self.settings["gain"]) - float(
                self.settings.get("dummy_floor_gain", self.settings["gain"]))) > 0.01:
            return
        if floor - float(expect) < sp.ANTENNA_MISSING_DB:
            print(f"   ** FLOOR AT {floor:.1f} dB, and this receiver reads {float(expect):.1f} dB "
                  f"with the antenna REMOVED.\n      Either the antenna is disconnected or the "
                  f"site is extraordinarily quiet. Check the connector before\n      trusting "
                  f"anything this run records.", file=sys.stderr)

    def _collect(self):
        while True:
            try:
                row, ch, result, freq_hz, snr, ms, why = self.results.get_nowait()
            except queue.Empty:
                return
            except (EOFError, OSError):
                return
            if result is None:
                if why == "aged":
                    self.analyses_aged += 1
                continue
            self.analyze_ms += ms
            self.analyses += 1
            self.analyses_total += 1
            self.log.apply(row, result, freq_hz)
            if self.store is not None:
                self.log.attach_capture(row, *self.store.write(row, result["signals"]))
            self._report(freq_hz, snr, result)

    _report = sp.CaptureLoop._report

    def _stats(self, e):
        if not self.args.stats:
            return
        uptime_h = (time.time() - self.session_start) / 3600.0
        harm = f" +{self.harmonics_found} harm" if self.harmonics_found else ""
        a_ms = self.analyze_ms / max(1, self.analyses)
        print(f"[stats] {e['fps']:5.1f} fps (target {e['target']:.0f})   "
              f"overflow {e['overflows']}   active {e['active']}   "
              f"events {self.events_logged}{harm} "
              f"({self.events_logged/max(uptime_h, 1/60):.0f}/hr)")
        print(f"        engine {e['dsp_ms']:5.2f} ms/frame ({e['dsp_ms']*e['fps']/10:4.1f}% core)"
              f"   spectrum {e['detect_ms']:5.2f} ms   floor {e['floor_ms']:5.2f} ms/20 frames"
              f"   analyse {a_ms:6.1f} ms x{self.analyses} (separate process)"
              + (f"  SKIPPED {self.analyses_skipped}" if self.analyses_skipped else "")
              + (f"  AGED {self.analyses_aged}" if self.analyses_aged else ""))
        print(f"        clip {e['clip']}   desense {e['desense']}")
        self.analyze_ms = 0.0
        self.analyses = 0

    def _engine_gone(self, why):
        if why == "stall":
            print(f"stream delivered nothing for {self.engine_exit.get('frames')} consecutive "
                  f"reads. Exiting so the supervisor restarts the process and the device "
                  f"re-enumerates — a wedged USB endpoint does not recover in place.",
                  file=sys.stderr)
            return 1
        if why == "eof" and self.iq_file:
            return 0
        if why == "error":
            print(f"engine error: {self.engine_exit.get('msg')}", file=sys.stderr)
        else:
            print(f"engine exited unexpectedly ({why})", file=sys.stderr)
        return 1

    def finish(self):
        # Stop the engine and take everything it still has to say, including the
        # close of the last window.
        if self.proc.poll() is None:
            self._send({"cmd": "stop"})
        deadline = time.time() + 30.0
        while time.time() < deadline:
            try:
                m = self.inbox.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None and self.inbox.empty():
                    break
                continue
            if m["t"] in ("__eof__",):
                break
            if m["t"] in ("stall", "eof", "error"):
                continue
            try:
                self._handle(m)
            except EngineGone:
                pass
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

        # Let queued analyses finish and apply them: they belong to events
        # already in the database.
        for _ in self.analysis:
            self.jobs.put(None)
        deadline = time.time() + 30.0
        while any(p.is_alive() for p in self.analysis) and time.time() < deadline:
            self._collect()
            time.sleep(0.1)
        self._collect()
        for proc in self.analysis:
            if proc.is_alive():
                proc.terminate()

        if self.window_id is not None and self.conn is not None:
            db.close_window(self.conn, self.window_id)
            self.window_id = None
        if self.conn is not None:
            db.end_run(self.conn, self.run_id)
            self.conn.close()
        try:
            os.unlink(self.shm_path)
        except OSError:
            pass

        e = self.last_engine_stats
        print(f"\nstopped. overflows: {e.get('overflows', 0)}  events: {self.events_logged}  "
              f"analysed: {self.analyses_total}  skipped: {self.analyses_skipped}  "
              f"aged: {self.analyses_aged}  harmonics: {self.harmonics_found}  "
              f"clip frames: {e.get('clip', 0)}  desense frames: {e.get('desense', 0)}")
        if self.store is not None:
            print(f"retained {self.store.count} captures, {self.store.written/1e6:.1f} MB"
                  + (f" — stopped early: {self.store.stopped}" if self.store.stopped else ""))
        if e.get("overflows"):
            print("Overflows mean dropped samples and unreliable data — check USB topology "
                  "with 'lsusb -t' and power with 'dmesg | grep -i voltage'.")


def run(args, settings, iq_file=None, file_wall0=None):
    """`survey_prototype.run()` for the Rust engine."""
    windows = settings["windows"]
    loop = EngineCaptureLoop(settings, args, windows, iq_file=iq_file, file_wall0=file_wall0)
    rate = loop.rate
    print(f"receiver {args.receiver_id}: {rate/1e6:.3f} MSPS, gain {settings['gain']}, "
          f"ppm {settings['ppm']}  [rust engine]")
    print(f"{settings['mode']}: " + ", ".join(
        f"{w['center_hz']/1e6:.3f} MHz" + (f" ({w['label']})" if w['label'] else "")
        for w in windows)
        + ("" if len(windows) == 1 else
           "  dwell " + ", ".join(
               f"{(w.get('dwell_s') or settings['dwell_s']):.0f} s" for w in windows)))
    print(f"detect on {settings['on_db']:.1f} dB / off {settings['off_db']:.1f} dB, "
          f"min {settings['min_duration_s']:.2f} s, hang {settings['hang_s']:.2f} s")

    db.init_schema(args.db)
    conn = db.connect(args.db)
    run_id = db.start_run(conn, args.profile, notes=args.notes)
    first_center = float(windows[0]["center_hz"])
    db.register_receiver(conn, run_id, args.receiver_id,
                         serial=loop.serial, sample_rate_hz=int(rate),
                         gain_db=settings["gain"], ppm_error=settings["ppm"],
                         center_hz=int(first_center),
                         attenuator_db=settings["attenuator_db"],
                         antenna=settings["antenna"])
    loop.attach(conn, run_id)

    print(f"{sp.ChannelGrid(first_center, rate).n} channels on a {sp.CHANNEL_HZ/1000:.2f} kHz grid, "
          f"{loop.frame_seconds*1000:.1f} ms frames ({rate/loop.fs:.0f}/sec)")
    print(f"ring buffer {int(loop.ready['ring_capacity'])*8/1e6:.0f} MB in {loop.shm_path}, "
          f"shared with {ANALYSIS_WORKERS} analysis processes\n")
    if loop.store is not None:
        print(f"retaining {'audio + channel IQ' if args.capture_iq else 'audio'} "
              f"under {loop.store.root}, budget {args.capture_mb:.0f} MB")
    print(f"run {run_id}, serial {loop.serial}, profile {args.profile}")
    return loop.go()
