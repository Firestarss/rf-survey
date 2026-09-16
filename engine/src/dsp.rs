//! The per-frame signal path: spectrum, channel grid, noise floor, event
//! tracker, detector and overload monitor.
//!
//! Every type here is a port of the class of the same name in
//! `src/survey_prototype.py`, and is required to make the same decisions on the
//! same samples — `tests/test_engine_equivalence.py` feeds both identical IQ and
//! compares their events. Where the Python leans on a numpy behaviour that is not
//! the obvious one (round-half-to-even, `np.partition`, `np.percentile`'s linear
//! interpolation, `np.median` averaging the two middles), the port copies the
//! behaviour, not the intent, and says so.
//!
//! What is different is allocation. The Python builds several fresh
//! half-megabyte arrays for every 6.6 ms frame; two receivers at 10 MSPS doing
//! that measured 4.3-7.1 ms per frame against 2.0 for one, and dropped samples
//! while one of them heard nothing at all. Nothing below allocates after
//! construction.

use num_complex::Complex32;
use rustfft::{Fft, FftPlanner};
use std::collections::VecDeque;
use std::sync::Arc;

pub const CHANNEL_HZ: f64 = 6250.0;
pub const NFFT: usize = 4096;
pub const GUARD_BINS: usize = 8;
pub const FLOOR_FRAMES: usize = 120;
pub const FLOOR_PCTILE: f64 = 25.0;
pub const FLOOR_EVERY: u64 = 20;
pub const FLOOR_MIN_HISTORY: usize = 20;
pub const CLIP_STRIDE: usize = 4;
pub const CLIP_LEVEL: f32 = 0.9;
pub const CLIP_FRACTION: f64 = 1e-4;
pub const DESENSE_DB: f64 = 6.0;
pub const OVERLOAD_BASELINE_FRAMES: usize = 300;
pub const OVERLOAD_BASELINE_MIN: usize = 60;
pub const OVERLOAD_BASELINE_PCTILE: f64 = 20.0;

/// `10 * log10(power + 1e-20)` — the floor keeps an empty bin off -inf.
#[inline]
pub fn to_db(p: f64) -> f64 {
    10.0 * (p + 1e-20).log10()
}

/// numpy's median: the middle value, or the mean of the two middles.
/// `scratch` is overwritten.
pub fn median_in_place(scratch: &mut [f64]) -> f64 {
    let n = scratch.len();
    let mid = n / 2;
    scratch.select_nth_unstable_by(mid, |a, b| a.total_cmp(b));
    let hi = scratch[mid];
    if n % 2 == 1 {
        return hi;
    }
    // The largest of the lower half is the other middle.
    let lo = scratch[..mid].iter().copied().fold(f64::NEG_INFINITY, f64::max);
    (lo + hi) / 2.0
}

/// numpy's `percentile(a, q)` with the default linear method.
/// `scratch` is overwritten (sorted).
pub fn percentile_linear(scratch: &mut [f64], q: f64) -> f64 {
    scratch.sort_unstable_by(|a, b| a.total_cmp(b));
    let n = scratch.len();
    // numpy: virtual = (q / 100) * (n - 1). The order of those operations is
    // not cosmetic — (n - 1) * q / 100 rounds differently and can put floor()
    // on the other side of an integer.
    let virt = (q / 100.0) * (n as f64 - 1.0);
    let lo = virt.floor() as usize;
    let hi = (lo + 1).min(n - 1);
    let t = virt - lo as f64;
    let (a, b) = (scratch[lo], scratch[hi]);
    // numpy's _lerp: a + (b - a) * t, recomputed as b - (b - a) * (1 - t)
    // where t >= 0.5.
    let diff = b - a;
    if t >= 0.5 {
        b - diff * (1.0 - t)
    } else {
        a + diff * t
    }
}

// ---------------------------------------------------------------------------
// Periodogram
// ---------------------------------------------------------------------------

/// Averaged PSD over the whole NFFT segments of a frame, in fftshift order.
///
/// Python: `mean(|fftshift(fft(segs * hann))|^2, axis=0) / (gain * rate)` with a
/// float32 Hann window. Here each 4096-point segment is windowed into one
/// reusable scratch, transformed in place, and its squared magnitude added
/// straight into a per-bin sum — no fftshift copy, no |x| then square, no
/// per-segment arrays.
pub struct Periodogram {
    fft: Arc<dyn Fft<f32>>,
    window: Vec<f32>,
    gain: f64,
    rate: f64,
    seg: Vec<Complex32>,
    fft_scratch: Vec<Complex32>,
    acc: Vec<f64>,
    pub psd: Vec<f64>,
}

impl Periodogram {
    pub fn new(rate: f64) -> Self {
        let mut planner = FftPlanner::<f32>::new();
        let fft = planner.plan_fft_forward(NFFT);
        // np.hanning(M): 0.5 - 0.5 cos(2 pi n / (M - 1)), in float64, then
        // cast to float32 as the Python does.
        let m = (NFFT - 1) as f64;
        let window: Vec<f32> = (0..NFFT)
            .map(|n| (0.5 - 0.5 * (2.0 * std::f64::consts::PI * n as f64 / m).cos()) as f32)
            .collect();
        // float(np.sum(window ** 2)) — squared as float32.
        let gain: f64 = window.iter().map(|&w| (w * w) as f64).sum();
        let scratch_len = fft.get_inplace_scratch_len();
        Periodogram {
            fft,
            window,
            gain,
            rate,
            seg: vec![Complex32::new(0.0, 0.0); NFFT],
            fft_scratch: vec![Complex32::new(0.0, 0.0); scratch_len],
            acc: vec![0.0; NFFT],
            psd: vec![0.0; NFFT],
        }
    }

    /// Fills `self.psd` from `samples`. False if shorter than one segment,
    /// matching the Python returning None.
    pub fn compute(&mut self, samples: &[Complex32]) -> bool {
        let nseg = samples.len() / NFFT;
        if nseg == 0 {
            return false;
        }
        self.acc.iter_mut().for_each(|v| *v = 0.0);
        for s in 0..nseg {
            let chunk = &samples[s * NFFT..(s + 1) * NFFT];
            for ((dst, src), w) in self.seg.iter_mut().zip(chunk).zip(&self.window) {
                *dst = Complex32::new(src.re * w, src.im * w);
            }
            self.fft.process_with_scratch(&mut self.seg, &mut self.fft_scratch);
            let half = NFFT / 2;
            // fftshift: shifted[i] = raw[(i + N/2) % N]
            for i in 0..NFFT {
                let z = self.seg[(i + half) % NFFT];
                let mag2 = z.re * z.re + z.im * z.im;
                self.acc[i] += mag2 as f64;
            }
        }
        let scale = 1.0 / (nseg as f64 * self.gain * self.rate);
        for (p, a) in self.psd.iter_mut().zip(&self.acc) {
            *p = a * scale;
        }
        true
    }
}

// ---------------------------------------------------------------------------
// Channel grid
// ---------------------------------------------------------------------------

/// Maps fftshift-ordered bins onto the absolute 6.25 kHz channel grid.
pub struct ChannelGrid {
    pub n: usize,
    pub channels: Vec<i64>,
    pub freqs_hz: Vec<f64>,
    /// Channel index for each bin, or usize::MAX for a guard bin.
    bin_channel: Vec<usize>,
    inv_counts: Vec<f64>,
}

impl ChannelGrid {
    pub fn new(center_hz: f64, rate: f64) -> Self {
        // np.fft.fftfreq(n, d=1/rate) then fftshift, plus the centre.
        let d = 1.0 / rate;
        let val = 1.0 / (NFFT as f64 * d);
        let half = (NFFT / 2) as i64;
        let mut idx: Vec<i64> = (0..NFFT as i64)
            .map(|i| {
                let k = i - half; // shifted order runs -N/2 .. N/2-1
                let f = center_hz + (k as f64) * val;
                // np.round is round-half-to-even. With a centre exactly on the
                // grid, every 64th bin at 10 MSPS lands on an exact .5.
                (f / CHANNEL_HZ).round_ties_even() as i64
            })
            .collect();
        for v in idx.iter_mut().take(GUARD_BINS) {
            *v = -1;
        }
        for v in idx.iter_mut().rev().take(GUARD_BINS) {
            *v = -1;
        }
        let mut channels: Vec<i64> = idx.iter().copied().filter(|&v| v >= 0).collect();
        channels.sort_unstable();
        channels.dedup();
        let n = channels.len();
        let mut counts = vec![0.0f64; n];
        let bin_channel: Vec<usize> = idx
            .iter()
            .map(|&v| {
                if v < 0 {
                    usize::MAX
                } else {
                    let c = channels.binary_search(&v).expect("channel present");
                    counts[c] += 1.0;
                    c
                }
            })
            .collect();
        let inv_counts = counts.iter().map(|&c| 1.0 / if c == 0.0 { 1.0 } else { c }).collect();
        let freqs_hz = channels.iter().map(|&c| c as f64 * CHANNEL_HZ).collect();
        ChannelGrid { n, channels, freqs_hz, bin_channel, inv_counts }
    }

    /// Per-channel mean PSD, in dB, into `out` (length n).
    pub fn power_db(&self, psd: &[f64], sums: &mut [f64], out: &mut [f64]) {
        sums.iter_mut().for_each(|v| *v = 0.0);
        for (&c, &p) in self.bin_channel.iter().zip(psd) {
            if c != usize::MAX {
                sums[c] += p;
            }
        }
        for ((o, s), inv) in out.iter_mut().zip(sums.iter()).zip(&self.inv_counts) {
            *o = to_db(s * inv);
        }
    }
}

// ---------------------------------------------------------------------------
// Noise floor
// ---------------------------------------------------------------------------

/// Rolling low-percentile of each channel's power. See NoiseFloor in Python for
/// why active channels contribute the last floor instead of their own power.
pub struct NoiseFloor {
    n: usize,
    hist: VecDeque<Vec<f64>>,
    spare: Vec<Vec<f64>>,
    k: usize,
    count: u64,
    pub value: Option<Vec<f64>>,
    column: Vec<f64>,
    pub last_cost_ms: f64,
}

impl NoiseFloor {
    pub fn new(n: usize) -> Self {
        let k = ((FLOOR_FRAMES as f64 * FLOOR_PCTILE / 100.0) as usize).min(FLOOR_FRAMES - 1);
        NoiseFloor {
            n,
            hist: VecDeque::with_capacity(FLOOR_FRAMES + 1),
            spare: Vec::new(),
            k,
            count: 0,
            value: None,
            column: vec![0.0; FLOOR_FRAMES],
            last_cost_ms: 0.0,
        }
    }

    /// Returns true once a floor exists (Python returns the value, or None).
    pub fn update(&mut self, power_db: &[f64], active: Option<&[bool]>) -> bool {
        let mut row = self.spare.pop().unwrap_or_else(|| vec![0.0; self.n]);
        match (active, &self.value) {
            (Some(act), Some(val)) => {
                for i in 0..self.n {
                    row[i] = if act[i] { val[i] } else { power_db[i] };
                }
            }
            _ => row.copy_from_slice(power_db),
        }
        self.hist.push_back(row);
        if self.hist.len() > FLOOR_FRAMES {
            if let Some(old) = self.hist.pop_front() {
                self.spare.push(old);
            }
        }
        self.count += 1;
        if self.hist.len() < FLOOR_MIN_HISTORY {
            return false;
        }
        if self.value.is_none() || self.count % FLOOR_EVERY == 0 {
            let t0 = std::time::Instant::now();
            let len = self.hist.len();
            // k clamps to what exists: while the history fills this is the max.
            let k = self.k.min(len - 1);
            let mut value = self.value.take().unwrap_or_else(|| vec![0.0; self.n]);
            for ch in 0..self.n {
                for (j, frame) in self.hist.iter().enumerate() {
                    self.column[j] = frame[ch];
                }
                let col = &mut self.column[..len];
                col.select_nth_unstable_by(k, |a, b| a.total_cmp(b));
                value[ch] = col[k];
            }
            self.value = Some(value);
            self.last_cost_ms = t0.elapsed().as_secs_f64() * 1000.0;
        }
        true
    }
}

// ---------------------------------------------------------------------------
// Event tracker
// ---------------------------------------------------------------------------

pub struct EventTracker {
    pub on_db: f64,
    pub off_db: f64,
    pub min_frames: usize,
    pub hang_frames: usize,
    pub active: Vec<bool>,
    above: Vec<u32>,
    below: Vec<u32>,
    pub start_sample: Vec<u64>,
    pub peak_snr: Vec<f64>,
    recent: VecDeque<u64>,
    recent_max: usize,
    pub last_start_sample: u64,
    pub last_end_sample: u64,
    pub started: Vec<usize>,
    pub ended: Vec<usize>,
}

impl EventTracker {
    pub fn new(n: usize, on_db: f64, off_db: f64, min_frames: usize, hang_frames: usize) -> Self {
        let recent_max = min_frames.max(hang_frames);
        EventTracker {
            on_db,
            off_db,
            min_frames,
            hang_frames,
            active: vec![false; n],
            above: vec![0; n],
            below: vec![0; n],
            start_sample: vec![0; n],
            peak_snr: vec![0.0; n],
            recent: VecDeque::with_capacity(recent_max + 1),
            recent_max,
            last_start_sample: 0,
            last_end_sample: 0,
            started: Vec::with_capacity(n),
            ended: Vec::with_capacity(n),
        }
    }

    fn frames_ago(&self, k: usize) -> u64 {
        if k >= self.recent.len() {
            self.recent[0]
        } else {
            self.recent[self.recent.len() - 1 - k]
        }
    }

    /// Fills `started` and `ended` in ascending channel order, as np.flatnonzero.
    pub fn update(&mut self, snr: &[f64], frame_start: u64, can_start: &[bool]) {
        self.recent.push_back(frame_start);
        if self.recent.len() > self.recent_max {
            self.recent.pop_front();
        }
        self.started.clear();
        self.ended.clear();
        let n = snr.len();
        // Decisions use the state as it was on entry, as the Python's masks do.
        for i in 0..n {
            let hot = snr[i] >= self.on_db;
            let cold = snr[i] < self.off_db;
            self.above[i] = if hot { self.above[i] + 1 } else { 0 };
            self.below[i] = if cold { self.below[i] + 1 } else { 0 };
            if !self.active[i] {
                if self.above[i] as usize >= self.min_frames && can_start[i] {
                    self.started.push(i);
                }
            } else if self.below[i] as usize >= self.hang_frames {
                self.ended.push(i);
            }
        }
        self.last_start_sample = self.frames_ago(self.min_frames - 1);
        self.last_end_sample = self.frames_ago(self.hang_frames - 1);
        for &i in &self.started {
            self.active[i] = true;
            self.start_sample[i] = self.last_start_sample;
            self.peak_snr[i] = snr[i];
        }
        for &i in &self.ended {
            self.active[i] = false;
        }
        for i in 0..n {
            if self.active[i] && snr[i] > self.peak_snr[i] {
                self.peak_snr[i] = snr[i];
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Detector
// ---------------------------------------------------------------------------

pub struct Detector {
    pub center_hz: f64,
    pub grid: ChannelGrid,
    pub tracker: EventTracker,
    pub floor: NoiseFloor,
    have_active: bool,
    snr: Vec<f64>,
    local_max: Vec<bool>,
    active_snapshot: Vec<bool>,
}

impl Detector {
    pub fn new(center_hz: f64, rate: f64, on_db: f64, off_db: f64, min_frames: usize, hang_frames: usize) -> Self {
        let grid = ChannelGrid::new(center_hz, rate);
        let n = grid.n;
        Detector {
            center_hz,
            tracker: EventTracker::new(n, on_db, off_db, min_frames, hang_frames),
            floor: NoiseFloor::new(n),
            grid,
            have_active: false,
            snr: vec![0.0; n],
            local_max: vec![false; n],
            active_snapshot: vec![false; n],
        }
    }

    /// False while the floor is still warming up (Python returns None).
    pub fn step(&mut self, power_db: &[f64], frame_start: u64) -> bool {
        let act = if self.have_active { Some(&self.active_snapshot[..]) } else { None };
        if !self.floor.update(power_db, act) {
            return false;
        }
        let floor = self.floor.value.as_ref().expect("floor present");
        let n = power_db.len();
        for i in 0..n {
            self.snr[i] = power_db[i] - floor[i];
        }
        // Only a local maximum (+/-1 channel, ties allowed) may open an event.
        for i in 0..n {
            let left = if i == 0 { f64::NEG_INFINITY } else { self.snr[i - 1] };
            let right = if i + 1 == n { f64::NEG_INFINITY } else { self.snr[i + 1] };
            self.local_max[i] = self.snr[i] >= left && self.snr[i] >= right;
        }
        self.tracker.update(&self.snr, frame_start, &self.local_max);
        self.active_snapshot.copy_from_slice(&self.tracker.active);
        self.have_active = true;
        true
    }
}

// ---------------------------------------------------------------------------
// Overload monitor
// ---------------------------------------------------------------------------

pub struct OverloadMonitor {
    history: VecDeque<f64>,
    scratch_hist: Vec<f64>,
    scratch_chan: Vec<f64>,
    pub baseline: Option<f64>,
    pub clip_frames: u64,
    pub desense_frames: u64,
}

pub struct OverloadResult {
    pub clipping: bool,
    pub desense: bool,
    pub clip_frac: f64,
}

impl OverloadMonitor {
    pub fn new(n_channels: usize) -> Self {
        OverloadMonitor {
            history: VecDeque::with_capacity(OVERLOAD_BASELINE_FRAMES + 1),
            scratch_hist: Vec::with_capacity(OVERLOAD_BASELINE_FRAMES),
            scratch_chan: vec![0.0; n_channels],
            baseline: None,
            clip_frames: 0,
            desense_frames: 0,
        }
    }

    pub fn update(&mut self, samples: &[Complex32], power_db: &[f64]) -> OverloadResult {
        let mut total = 0usize;
        let mut hot = 0usize;
        for z in samples.iter().step_by(CLIP_STRIDE) {
            total += 1;
            if z.norm() > CLIP_LEVEL {
                hot += 1;
            }
        }
        let clip_frac = if total == 0 { 0.0 } else { hot as f64 / total as f64 };
        let clipping = clip_frac > CLIP_FRACTION;

        if self.scratch_chan.len() != power_db.len() {
            self.scratch_chan.resize(power_db.len(), 0.0);
        }
        self.scratch_chan.copy_from_slice(power_db);
        let wideband = median_in_place(&mut self.scratch_chan);
        self.history.push_back(wideband);
        if self.history.len() > OVERLOAD_BASELINE_FRAMES {
            self.history.pop_front();
        }
        if self.history.len() >= OVERLOAD_BASELINE_MIN {
            self.scratch_hist.clear();
            self.scratch_hist.extend(self.history.iter().copied());
            self.baseline = Some(percentile_linear(&mut self.scratch_hist, OVERLOAD_BASELINE_PCTILE));
        }
        let desense = matches!(self.baseline, Some(b) if wideband - b > DESENSE_DB);
        if clipping {
            self.clip_frames += 1;
        }
        if desense {
            self.desense_frames += 1;
        }
        OverloadResult { clipping, desense, clip_frac }
    }
}

/// compression_verdict(): the floor should rise by equal steps; the top step
/// falling short of the lower one means the front end is compressing.
pub fn compression_verdict(levels: &[f64; 3]) -> &'static str {
    let first = levels[1] - levels[0];
    let second = levels[2] - levels[1];
    if first < 2.0 {
        return "inconclusive";
    }
    if second < 0.7 * first {
        "compressed"
    } else {
        "linear"
    }
}
