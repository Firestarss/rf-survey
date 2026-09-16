//! rfsurvey-engine — the reader, channeliser and detector for one receiver.
//!
//! Replaces the per-frame hot path of the Python capture loop. Python still owns
//! everything that is not per-frame: the database, the profile, window rotation,
//! analysis, captures, and every operator-facing message.
//!
//! Three threads, so that reading the radio never waits on anything:
//!
//!   reader  readStream into pooled buffers, and device control (tune, gain,
//!           the linearity probe) between reads
//!   dsp     ring buffer, spectrum, noise floor, detection, overload; emits the
//!           same per-frame decisions the Python _frame() makes
//!   writer  JSON lines to stdout
//!
//! Protocol — one JSON object per line.
//!   stdin:  {"cmd":"open","center_hz":466e6,"linearity":true}
//!           {"cmd":"stop"}
//!   stdout: ready, linearity, window, f (frame decisions), floor, close,
//!           stats, stall, eof, error — see emit sites below.
//!
//! Configuration is one JSON argument: `rfsurvey-engine '<json>'`.

mod dsp;
mod ring;
mod soapy;

use dsp::*;
use num_complex::Complex32;
use ring::ShmRing;
use serde_json::{json, Value};
use std::io::{BufRead, Read, Write};
use std::path::PathBuf;
use std::sync::mpsc::{channel, sync_channel, Receiver, Sender, SyncSender};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const STALL_FRAMES: u32 = 200;
const READ_TIMEOUT_US: i64 = 2_000_000;
const DSP_QUEUE: usize = 64;

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

struct Config {
    file: Option<PathBuf>,
    device_args: String,
    rate: f64,
    gain: f64,
    ppm: f64,
    initial_center_hz: f64,
    fs_request: usize,
    file_mtu: usize,
    on_db: f64,
    off_db: f64,
    min_duration_s: f64,
    hang_s: f64,
    pretrigger_s: f64,
    analyze_s: f64,
    min_analyze_s: f64,
    ring_slack_s: f64,
    shm_path: PathBuf,
    stat_seconds: f64,
    gain_step: f64,
    compression_seconds: f64,
    max_gain: f64,
    file_wall0: f64,
    file_realtime: bool,
}

fn f(v: &Value, k: &str, default: Option<f64>) -> f64 {
    match (v.get(k).and_then(Value::as_f64), default) {
        (Some(x), _) => x,
        (None, Some(d)) => d,
        (None, None) => fatal(&format!("config missing '{k}'")),
    }
}

fn fatal(msg: &str) -> ! {
    let line = json!({"t": "error", "msg": msg}).to_string();
    println!("{line}");
    eprintln!("rfsurvey-engine: {msg}");
    std::process::exit(2);
}

impl Config {
    fn parse(text: &str) -> Config {
        let v: Value = serde_json::from_str(text).unwrap_or_else(|e| fatal(&format!("bad config: {e}")));
        Config {
            file: v.get("iq_file").and_then(Value::as_str).map(PathBuf::from),
            device_args: v.get("device_args").and_then(Value::as_str).unwrap_or("").to_string(),
            rate: f(&v, "rate", None),
            gain: f(&v, "gain", Some(0.0)),
            ppm: f(&v, "ppm", Some(0.0)),
            initial_center_hz: f(&v, "initial_center_hz", Some(0.0)),
            fs_request: f(&v, "fs_request", None) as usize,
            file_mtu: f(&v, "file_mtu", Some(65536.0)) as usize,
            on_db: f(&v, "on_db", None),
            off_db: f(&v, "off_db", None),
            min_duration_s: f(&v, "min_duration_s", None),
            hang_s: f(&v, "hang_s", None),
            pretrigger_s: f(&v, "pretrigger_s", None),
            analyze_s: f(&v, "analyze_s", None),
            min_analyze_s: f(&v, "min_analyze_s", None),
            ring_slack_s: f(&v, "ring_slack_s", None),
            shm_path: PathBuf::from(v.get("shm_path").and_then(Value::as_str).unwrap_or_else(|| fatal("config missing 'shm_path'"))),
            stat_seconds: f(&v, "stat_seconds", Some(15.0)),
            gain_step: f(&v, "gain_step", Some(3.0)),
            compression_seconds: f(&v, "compression_seconds", Some(0.5)),
            max_gain: f(&v, "max_gain", Some(45.0)),
            file_wall0: f(&v, "file_wall0", Some(0.0)),
            file_realtime: v.get("file_realtime").and_then(Value::as_bool).unwrap_or(false),
        }
    }
}

/// Python's `int(round(x))`: round-half-to-even, then to integer.
fn py_round(x: f64) -> i64 {
    x.round_ties_even() as i64
}

fn tune_request_hz(center: f64, ppm: f64) -> f64 {
    center / (1.0 + ppm * 1e-6)
}

fn true_center_hz(requested: f64, ppm: f64) -> f64 {
    requested * (1.0 + ppm * 1e-6)
}

fn wall_now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

// ---------------------------------------------------------------------------
// Messages between threads
// ---------------------------------------------------------------------------

enum ToDsp {
    Frame(Vec<Complex32>, usize),
    Overflow,
    Tuned { center_hz: f64 },
    Window { center_hz: f64, wall0: f64, linearity: Option<(String, Vec<f64>)> },
    Close,
    Stall,
    Eof,
    Stop,
}

enum Cmd {
    Open { center_hz: f64, linearity: bool },
    Stop,
}

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

enum Source {
    Device(soapy::Device),
    /// A file of CF32 samples. `pace` delivers them at the sample rate instead
    /// of as fast as the disk allows, for tests of the whole loop: analysis
    /// reads the ring after the decision, so a source running 5x real time
    /// would overwrite the samples before analysis got to them.
    File(std::fs::File, Option<(Instant, f64, u64)>),
}

enum ReadResult {
    Samples(usize),
    Overflow,
    Nothing,
    Eof,
}

impl Source {
    fn read(&mut self, buf: &mut [Complex32]) -> ReadResult {
        match self {
            Source::Device(d) => {
                let r = d.read(buf, READ_TIMEOUT_US);
                if r > 0 {
                    ReadResult::Samples(r as usize)
                } else if r == soapy::SOAPY_SDR_OVERFLOW {
                    ReadResult::Overflow
                } else {
                    ReadResult::Nothing
                }
            }
            Source::File(file, pace) => {
                // SAFETY: Complex32 is two f32s, 8 bytes, no padding.
                let bytes = unsafe { std::slice::from_raw_parts_mut(buf.as_mut_ptr() as *mut u8, buf.len() * 8) };
                let mut got = 0;
                while got < bytes.len() {
                    match file.read(&mut bytes[got..]) {
                        Ok(0) => break,
                        Ok(n) => got += n,
                        Err(_) => break,
                    }
                }
                let n = got / 8;
                if n == 0 {
                    return ReadResult::Eof;
                }
                if let Some((t0, rate, delivered)) = pace {
                    *delivered += n as u64;
                    let due = *delivered as f64 / *rate;
                    let now = t0.elapsed().as_secs_f64();
                    if due > now {
                        std::thread::sleep(std::time::Duration::from_secs_f64(due - now));
                    }
                }
                ReadResult::Samples(n)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Reader thread
// ---------------------------------------------------------------------------

struct Reader {
    cfg: Config,
    src: Source,
    fs: usize,
    to_dsp: SyncSender<ToDsp>,
    pool: Receiver<Vec<Complex32>>,
    cmds: Receiver<Cmd>,
}

impl Reader {
    fn buffer(&self) -> Vec<Complex32> {
        self.pool.try_recv().unwrap_or_else(|_| vec![Complex32::new(0.0, 0.0); self.fs])
    }

    fn send(&self, m: ToDsp) {
        // Blocking by design: if the DSP falls behind, the driver overflows and
        // says so, which is honest. Dropping frames here would silently stop the
        // sample clock and shift every later timestamp.
        if self.to_dsp.send(m).is_err() {
            std::process::exit(0);
        }
    }

    /// Radio.linearity(): probe down, and only if that cannot answer, up.
    fn linearity(&mut self, center_hz: f64) -> (String, Vec<f64>) {
        let dev = match &mut self.src {
            Source::Device(d) => d as *mut soapy::Device,
            Source::File(..) => return ("unchecked".into(), vec![]),
        };
        let gain = self.cfg.gain;
        let step = self.cfg.gain_step;
        let grid = ChannelGrid::new(center_hz, self.cfg.rate);
        let mut pgram = Periodogram::new(self.cfg.rate);
        let mut sums = vec![0.0; grid.n];
        let mut pdb = vec![0.0; grid.n];
        let mut frame = vec![Complex32::new(0.0, 0.0); self.fs];
        let need = ((self.cfg.compression_seconds * self.cfg.rate / self.fs as f64) as usize).max(1);
        for gains in [[gain - 2.0 * step, gain - step, gain], [gain, gain + step, gain + 2.0 * step]] {
            if gains[0] < 0.0 || gains[2] > self.cfg.max_gain {
                continue;
            }
            let mut levels = [0.0f64; 3];
            let mut ok = true;
            for (i, &g) in gains.iter().enumerate() {
                // SAFETY: dev points into self.src, alive for this call.
                let d = unsafe { &mut *dev };
                let _ = d.set_gain(g);
                let _ = d.read(&mut frame, READ_TIMEOUT_US); // stale samples at the old gain
                let mut lvl = Vec::with_capacity(need);
                for _ in 0..need {
                    let n = d.read(&mut frame, READ_TIMEOUT_US);
                    if n <= 0 || !pgram.compute(&frame[..n as usize]) {
                        continue;
                    }
                    grid.power_db(&pgram.psd, &mut sums, &mut pdb);
                    let mut s = pdb.clone();
                    lvl.push(median_in_place(&mut s));
                }
                if lvl.is_empty() {
                    ok = false;
                    break;
                }
                levels[i] = median_in_place(&mut lvl);
            }
            // SAFETY: as above. Restore the configured gain however it ended.
            let _ = unsafe { &mut *dev }.set_gain(gain);
            if !ok {
                continue;
            }
            let v = compression_verdict(&levels);
            if v != "inconclusive" {
                return (v.into(), levels.to_vec());
            }
        }
        ("inconclusive".into(), vec![])
    }

    fn open(&mut self, center_hz: f64, linearity: bool) {
        self.send(ToDsp::Close);
        let (center, wall0) = match &mut self.src {
            Source::Device(d) => {
                let got = d.set_frequency(tune_request_hz(center_hz, self.cfg.ppm)).unwrap_or_else(|e| fatal(&e));
                (true_center_hz(got, self.cfg.ppm), 0.0)
            }
            Source::File(..) => (center_hz, self.cfg.file_wall0),
        };
        // Announced before the probe: Python opens the database window at tune
        // time, and the linearity verdict is written to that window's row.
        self.send(ToDsp::Tuned { center_hz: center });
        let lin = if linearity { Some(self.linearity(center)) } else { None };
        // Anchor the sample clock AFTER the probe. The Python engine anchored
        // before it, and the probe's ~1.5-3 s of samples never reached the
        // counter, so every event there is stamped that much early — measured
        // at +1.8 to +2.3 s on correctly dated runs, 2026-09-16.
        let wall0 = if matches!(self.src, Source::Device(_)) { wall_now() } else { wall0 };
        self.send(ToDsp::Window { center_hz: center, wall0, linearity: lin });
    }

    fn run(mut self) {
        let mut open = false;
        let mut stalled = 0u32;
        loop {
            // Commands between reads; block for one while no window is open.
            loop {
                let cmd = if open { self.cmds.try_recv().ok() } else { self.cmds.recv().ok().or(Some(Cmd::Stop)) };
                match cmd {
                    None => break,
                    Some(Cmd::Stop) => {
                        self.send(ToDsp::Close);
                        self.send(ToDsp::Stop);
                        return;
                    }
                    Some(Cmd::Open { center_hz, linearity }) => {
                        self.open(center_hz, linearity);
                        open = true;
                        stalled = 0;
                    }
                }
            }
            let mut buf = self.buffer();
            match self.src.read(&mut buf) {
                ReadResult::Samples(n) => {
                    stalled = 0;
                    self.send(ToDsp::Frame(buf, n));
                }
                ReadResult::Overflow => {
                    stalled = 0;
                    eprintln!("OVERFLOW — samples dropped");
                    self.send(ToDsp::Overflow);
                }
                ReadResult::Nothing => {
                    stalled += 1;
                    if stalled >= STALL_FRAMES {
                        self.send(ToDsp::Close);
                        self.send(ToDsp::Stall);
                        return;
                    }
                }
                ReadResult::Eof => {
                    self.send(ToDsp::Close);
                    self.send(ToDsp::Eof);
                    return;
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// DSP thread
// ---------------------------------------------------------------------------

struct Dsp {
    rate: f64,
    fs: usize,
    min_frames: usize,
    hang_frames: usize,
    on_db: f64,
    off_db: f64,
    pretrigger: u64,
    analyze_samples: u64,
    min_analyze_samples: u64,
    stat_seconds: f64,
    ring: ShmRing,
    pgram: Periodogram,
    overload: OverloadMonitor,
    det: Option<Detector>,
    sums: Vec<f64>,
    power_db: Vec<f64>,
    pending: Vec<(usize, u64)>,
    open: Vec<usize>,
    floor_reported: bool,
    out: Sender<String>,
    pool: Sender<Vec<Complex32>>,
    // stats
    overflows: u64,
    frames: u64,
    detect_s: f64,
    dsp_s: f64,
    last_stat: Instant,
}

impl Dsp {
    fn emit(&self, v: Value) {
        let _ = self.out.send(v.to_string());
    }

    fn close_window(&mut self) {
        if self.det.is_none() {
            return;
        }
        let det = self.det.as_ref().unwrap();
        let open: Vec<Value> = self.open.iter().map(|&ch| json!([ch, det.tracker.peak_snr[ch]])).collect();
        self.emit(json!({"t": "close", "written": self.ring.written(), "open": open}));
        self.det = None;
        self.open.clear();
        self.pending.clear();
    }

    fn open_window(&mut self, center_hz: f64, wall0: f64, linearity: Option<(String, Vec<f64>)>) {
        if let Some((verdict, levels)) = linearity {
            self.emit(json!({"t": "linearity", "verdict": verdict, "levels": levels}));
        }
        let det = Detector::new(center_hz, self.rate, self.on_db, self.off_db, self.min_frames, self.hang_frames);
        self.sums = vec![0.0; det.grid.n];
        self.power_db = vec![0.0; det.grid.n];
        self.ring.reset(center_hz);
        self.pending.clear();
        self.open.clear();
        self.floor_reported = false;
        self.emit(json!({
            "t": "window", "gen": self.ring.generation(), "center_hz": center_hz,
            "wall0": wall0, "n": det.grid.n, "channels": det.grid.channels,
        }));
        self.det = Some(det);
    }

    fn harmonic_parent(&self, ch: usize) -> Option<(usize, i64)> {
        let det = self.det.as_ref()?;
        let centre_ch = py_round(det.center_hz / CHANNEL_HZ);
        let mine = det.grid.channels[ch] - centre_ch;
        if mine == 0 {
            return None;
        }
        let my_snr = det.tracker.peak_snr[ch];
        let mut best: Option<(usize, i64)> = None;
        let mut best_snr = 0.0f64;
        for &other in &self.open {
            if other == ch {
                continue;
            }
            let theirs = det.grid.channels[other] - centre_ch;
            if theirs == 0 || mine % theirs != 0 {
                continue;
            }
            let n = mine / theirs;
            if n.abs() < 3 || n % 2 == 0 {
                continue;
            }
            let snr = det.tracker.peak_snr[other];
            if snr <= my_snr || snr <= best_snr {
                continue;
            }
            best = Some((other, n));
            best_snr = snr;
        }
        best
    }

    /// _analyse(): the decision only — Python does the analysis.
    fn analyse(&mut self, ch: usize, nsamples: u64, acts: &mut Vec<Value>) {
        let Some(pos) = self.pending.iter().position(|&(c, _)| c == ch) else { return };
        let (_, start) = self.pending.remove(pos);
        if !self.open.contains(&ch) || nsamples < self.min_analyze_samples {
            return;
        }
        if let Some((parent, n)) = self.harmonic_parent(ch) {
            acts.push(json!(["h", ch, parent, n]));
            return;
        }
        if !self.ring.holds(start, nsamples) {
            return;
        }
        let peak = self.det.as_ref().unwrap().tracker.peak_snr[ch];
        acts.push(json!(["a", ch, start, nsamples, peak]));
    }

    fn frame(&mut self, samples: &[Complex32]) {
        let t_all = Instant::now();
        let frame_start = self.ring.written();
        self.ring.push(samples);
        self.frames += 1;
        if self.det.is_none() {
            return;
        }

        let t0 = Instant::now();
        if !self.pgram.compute(samples) {
            return;
        }
        let det = self.det.as_mut().unwrap();
        det.grid.power_db(&self.pgram.psd, &mut self.sums, &mut self.power_db);
        self.detect_s += t0.elapsed().as_secs_f64();

        let ovl = self.overload.update(samples, &self.power_db);
        let flagged = ovl.clipping || ovl.desense;
        if flagged {
            let total = self.overload.clip_frames + self.overload.desense_frames;
            if total == 1 || total % 500 == 0 {
                let why = if ovl.clipping { "clipping" } else { "desense" };
                eprintln!("  ** OVERLOAD ({why}) — add attenuation [clip {:.2}%]", ovl.clip_frac * 100.0);
            }
        }

        let det = self.det.as_mut().unwrap();
        if !det.step(&self.power_db, frame_start) {
            self.dsp_s += t_all.elapsed().as_secs_f64();
            return;
        }
        if !self.floor_reported {
            if let Some(v) = det.floor.value.as_ref() {
                let mut s = v.clone();
                let median = median_in_place(&mut s);
                self.floor_reported = true;
                self.emit(json!({"t": "floor", "median_db": median}));
            }
        }

        let det = self.det.as_ref().unwrap();
        let started = det.tracker.started.clone();
        let ended = det.tracker.ended.clone();
        let ts = det.tracker.last_start_sample;
        let te = det.tracker.last_end_sample;
        let mut acts: Vec<Value> = Vec::new();

        for &ch in &started {
            let s = det.tracker.start_sample[ch].saturating_sub(self.pretrigger);
            self.pending.push((ch, s));
            self.open.push(ch);
            acts.push(json!(["s", ch]));
        }
        let written = self.ring.written();
        let due: Vec<usize> = self
            .pending
            .iter()
            .filter(|&&(_, start)| written >= start + self.analyze_samples)
            .map(|&(ch, _)| ch)
            .collect();
        for ch in due {
            self.analyse(ch, self.analyze_samples, &mut acts);
        }
        for &ch in &ended {
            if let Some(&(_, start)) = self.pending.iter().find(|&&(c, _)| c == ch) {
                let avail = te.saturating_sub(start);
                self.analyse(ch, avail.min(self.analyze_samples), &mut acts);
            }
            self.pending.retain(|&(c, _)| c != ch);
            let det = self.det.as_ref().unwrap();
            let dur = ((te as i64 - det.tracker.start_sample[ch] as i64) as f64 / self.rate).max(0.0);
            let peak = det.tracker.peak_snr[ch];
            if let Some(pos) = self.open.iter().position(|&c| c == ch) {
                self.open.remove(pos);
                acts.push(json!(["e", ch, dur, peak]));
            }
        }
        if !acts.is_empty() {
            self.emit(json!({"t": "f", "ovl": flagged, "ts": ts, "te": te, "acts": acts}));
        }
        self.dsp_s += t_all.elapsed().as_secs_f64();
        self.stats(false);
    }

    fn stats(&mut self, force: bool) {
        let elapsed = self.last_stat.elapsed().as_secs_f64();
        if !force && elapsed < self.stat_seconds {
            return;
        }
        let frames = self.frames.max(1) as f64;
        let active = self.det.as_ref().map(|d| d.tracker.active.iter().filter(|&&a| a).count()).unwrap_or(0);
        let floor_ms = self.det.as_ref().map(|d| d.floor.last_cost_ms).unwrap_or(0.0);
        self.emit(json!({
            "t": "stats", "fps": self.frames as f64 / elapsed.max(1e-9),
            "target": self.rate / self.fs as f64, "overflows": self.overflows,
            "detect_ms": self.detect_s * 1000.0 / frames, "dsp_ms": self.dsp_s * 1000.0 / frames,
            "floor_ms": floor_ms, "active": active,
            "clip": self.overload.clip_frames, "desense": self.overload.desense_frames,
        }));
        self.frames = 0;
        self.detect_s = 0.0;
        self.dsp_s = 0.0;
        self.last_stat = Instant::now();
    }

    fn run(mut self, rx: Receiver<ToDsp>) -> i32 {
        for msg in rx {
            match msg {
                ToDsp::Frame(buf, n) => {
                    self.frame(&buf[..n]);
                    let _ = self.pool.send(buf);
                }
                ToDsp::Overflow => self.overflows += 1,
                ToDsp::Tuned { center_hz } => self.emit(json!({"t": "tuned", "center_hz": center_hz})),
                ToDsp::Window { center_hz, wall0, linearity } => self.open_window(center_hz, wall0, linearity),
                ToDsp::Close => self.close_window(),
                ToDsp::Stall => {
                    self.emit(json!({"t": "stall", "frames": STALL_FRAMES}));
                    return 3;
                }
                ToDsp::Eof => {
                    self.stats(true);
                    self.emit(json!({"t": "eof"}));
                    return 0;
                }
                ToDsp::Stop => {
                    self.stats(true);
                    return 0;
                }
            }
        }
        0
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

fn main() {
    // SIGINT is for the orchestrator. systemd delivers it to every process in
    // the unit, and dying on it here would skip the close of the last window;
    // Python catches it and sends "stop", which closes things in order.
    // SAFETY: installing SIG_IGN has no preconditions.
    unsafe {
        libc::signal(libc::SIGINT, libc::SIG_IGN);
    }
    let arg = std::env::args().nth(1).unwrap_or_else(|| fatal("usage: rfsurvey-engine '<json config>'"));
    let cfg = Config::parse(&arg);

    // Writer thread: the only thing that touches stdout.
    let (out_tx, out_rx) = channel::<String>();
    let writer = std::thread::Builder::new()
        .name("writer".into())
        .spawn(move || {
            let stdout = std::io::stdout();
            let mut w = std::io::BufWriter::new(stdout.lock());
            while let Ok(line) = out_rx.recv() {
                let _ = writeln!(w, "{line}");
                // Flush whenever the queue drains, so a quiet engine is not
                // holding a decision in a buffer.
                let mut more = true;
                while more {
                    match out_rx.try_recv() {
                        Ok(l) => {
                            let _ = writeln!(w, "{l}");
                        }
                        Err(_) => more = false,
                    }
                }
                if w.flush().is_err() {
                    std::process::exit(0);
                }
            }
        })
        .unwrap();

    // Source and frame size.
    let (src, rate, mtu, serial) = match &cfg.file {
        Some(path) => {
            let file = std::fs::File::open(path).unwrap_or_else(|e| fatal(&format!("{}: {e}", path.display())));
            let pace = if cfg.file_realtime { Some((Instant::now(), cfg.rate, 0u64)) } else { None };
            (Source::File(file, pace), cfg.rate, cfg.file_mtu, None)
        }
        None => {
            let mut d = soapy::Device::open(&cfg.device_args).unwrap_or_else(|e| fatal(&e));
            let rate = d.set_sample_rate(cfg.rate).unwrap_or_else(|e| fatal(&e));
            if cfg.initial_center_hz > 0.0 {
                d.set_frequency(tune_request_hz(cfg.initial_center_hz, cfg.ppm)).unwrap_or_else(|e| fatal(&e));
            }
            if !d.set_gain_manual() {
                eprintln!("warning: could not disable AGC");
            }
            d.set_gain(cfg.gain).unwrap_or_else(|e| fatal(&e));
            let serial = d.serial();
            let mtu = d.start().unwrap_or_else(|e| fatal(&e));
            (Source::Device(d), rate, mtu, serial)
        }
    };
    // The frame is what a read actually returns — see CaptureLoop.__init__.
    let fs = cfg.fs_request.min(if mtu > 0 { mtu } else { cfg.fs_request });
    let frame_seconds = fs as f64 / rate;
    let min_frames = py_round(cfg.min_duration_s / frame_seconds).max(1) as usize;
    let hang_frames = py_round(cfg.hang_s / frame_seconds).max(1) as usize;
    let pretrigger = (cfg.pretrigger_s * rate) as u64;
    let analyze_samples = (cfg.analyze_s * rate) as u64 + pretrigger;
    let min_analyze_samples = (cfg.min_analyze_s * rate) as u64;
    let ring_seconds = cfg.analyze_s + cfg.pretrigger_s + cfg.ring_slack_s;
    let ring_capacity = (ring_seconds * rate) as u64;

    let ring = ShmRing::create(&cfg.shm_path, ring_capacity, rate).unwrap_or_else(|e| fatal(&format!("shm {}: {e}", cfg.shm_path.display())));

    let _ = out_tx.send(
        json!({
            "t": "ready", "rate": rate, "mtu": mtu, "fs": fs, "frame_seconds": frame_seconds,
            "serial": serial, "min_frames": min_frames, "hang_frames": hang_frames,
            "pretrigger": pretrigger, "analyze_samples": analyze_samples,
            "min_analyze_samples": min_analyze_samples, "ring_capacity": ring_capacity,
            "shm_path": cfg.shm_path.to_string_lossy(),
        })
        .to_string(),
    );

    // stdin commands.
    let (cmd_tx, cmd_rx) = channel::<Cmd>();
    std::thread::Builder::new()
        .name("commands".into())
        .spawn(move || {
            for line in std::io::stdin().lock().lines() {
                let Ok(line) = line else { break };
                let Ok(v) = serde_json::from_str::<Value>(&line) else { continue };
                let cmd = match v.get("cmd").and_then(Value::as_str) {
                    Some("open") => Cmd::Open {
                        center_hz: v.get("center_hz").and_then(Value::as_f64).unwrap_or(0.0),
                        linearity: v.get("linearity").and_then(Value::as_bool).unwrap_or(false),
                    },
                    Some("stop") => Cmd::Stop,
                    _ => continue,
                };
                if cmd_tx.send(cmd).is_err() {
                    break;
                }
            }
            // stdin closed: the orchestrator is gone, so stop cleanly.
            let _ = cmd_tx.send(Cmd::Stop);
        })
        .unwrap();

    let (dsp_tx, dsp_rx) = sync_channel::<ToDsp>(DSP_QUEUE);
    let (pool_tx, pool_rx) = channel::<Vec<Complex32>>();

    let dsp = Dsp {
        rate,
        fs,
        min_frames,
        hang_frames,
        on_db: cfg.on_db,
        off_db: cfg.off_db,
        pretrigger,
        analyze_samples,
        min_analyze_samples,
        stat_seconds: cfg.stat_seconds,
        ring,
        pgram: Periodogram::new(rate),
        overload: OverloadMonitor::new(2048),
        det: None,
        sums: Vec::new(),
        power_db: Vec::new(),
        pending: Vec::new(),
        open: Vec::new(),
        floor_reported: false,
        out: out_tx.clone(),
        pool: pool_tx,
        overflows: 0,
        frames: 0,
        detect_s: 0.0,
        dsp_s: 0.0,
        last_stat: Instant::now(),
    };
    let dsp_thread = std::thread::Builder::new().name("dsp".into()).spawn(move || dsp.run(dsp_rx)).unwrap();

    let reader = Reader { cfg, src, fs, to_dsp: dsp_tx, pool: pool_rx, cmds: cmd_rx };
    let reader_thread = std::thread::Builder::new().name("reader".into()).spawn(move || reader.run()).unwrap();

    let _ = reader_thread.join();
    let code = dsp_thread.join().unwrap_or(1);
    drop(out_tx);
    let _ = writer.join();
    std::process::exit(code);
}
