#!/usr/bin/env python3
"""
RF survey deck — bench prototype.

Watches a slice of spectrum, notices every time someone transmits, works out the
mode and any subaudible tone, and logs it to SQLite.

Two modes:

  --selftest      Runs without any radio attached. Checks the tone detection
                  against synthetic signals and benchmarks this machine so you
                  know whether it will keep up. Do this first.

  (normal)        Opens a radio and runs for real.

Examples:

    python3 survey_prototype.py --selftest --rate 10e6

    python3 survey_prototype.py --driver airspy --serial 0x1234ABCD \
        --freq 466.0e6 --rate 10e6 --gain 12 --ppm 0.4 \
        --db bench.sqlite --receiver-id uhf --stats

Needs: numpy, scipy, and (for real use) SoapySDR with python3 bindings.
"""

import argparse
import math
import signal
import sqlite3
import sys
import time

import numpy as np
from scipy.signal import firwin, lfilter, upfirdn

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# EIA standard CTCSS tones plus the common extensions, in Hz.
CTCSS_TONES = np.array([
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
    171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5,
    203.5, 206.5, 210.7, 213.8, 218.1, 221.3, 225.7, 229.1, 233.6, 237.1,
    241.8, 245.5, 250.3, 254.1,
])

CHANNEL_HZ = 6250.0        # channel grid spacing
NFFT = 4096                # FFT size used for detection
FRAME_SECONDS = 0.013      # target analysis frame duration — see frame_size()
FLOOR_FRAMES = 120         # frames of history kept for the background estimate
FLOOR_PCTILE = 25          # low percentile tracks quiet without signal bias
FLOOR_EVERY = 10           # recompute the background every Nth frame
ANALYZE_SECONDS = 1.4      # dwell needed to identify a CTCSS tone reliably
PRETRIGGER_SECONDS = 0.3   # reach back before the detector fired
RING_SLACK_SECONDS = 0.7   # headroom in the sample ring buffer


def frame_size(rate, target_seconds=FRAME_SECONDS):
    """Samples per analysis frame, scaled so a frame is a fixed *duration*.

    This must scale with sample rate. A fixed sample count means the frame
    represents different amounts of time on different radios: 32768 samples is
    13.7 ms on an RTL-SDR at 2.4 MSPS but only 3.3 ms on an Airspy at 10 MSPS,
    which runs the whole analysis loop four times more often for timing
    resolution nothing needs. Frame duration sets how precisely event start and
    stop times are known; 13 ms is ample when transmissions last seconds.
    """
    return 1 << max(15, int(round(math.log2(rate * target_seconds))))


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

class Ring:
    """Fixed-capacity IQ ring buffer addressed by absolute sample index."""

    def __init__(self, capacity):
        self.buf = np.zeros(int(capacity), np.complex64)
        self.cap = int(capacity)
        self.written = 0

    def push(self, x):
        n = len(x)
        if n >= self.cap:
            self.buf[:] = x[-self.cap:]
            self.written += n
            return
        pos = self.written % self.cap
        end = pos + n
        if end <= self.cap:
            self.buf[pos:end] = x
        else:
            split = self.cap - pos
            self.buf[pos:] = x[:split]
            self.buf[:end - self.cap] = x[split:]
        self.written += n

    def get(self, start, length):
        """`length` samples from absolute index `start`, or None if aged out."""
        if start < 0 or length <= 0:
            return None
        if start < self.written - self.cap or start + length > self.written:
            return None
        out = np.empty(length, np.complex64)
        pos = start % self.cap
        end = pos + length
        if end <= self.cap:
            out[:] = self.buf[pos:end]
        else:
            split = self.cap - pos
            out[:split] = self.buf[pos:]
            out[split:] = self.buf[:end - self.cap]
        return out


class ChannelGrid:
    """Maps FFT bins onto an absolute 6.25 kHz channel grid."""

    def __init__(self, center_hz, rate, nfft=NFFT, guard_bins=8):
        freqs = center_hz + np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / rate))
        idx = np.round(freqs / CHANNEL_HZ).astype(np.int64)
        idx[:guard_bins] = -1          # band edges are partial channels and
        idx[-guard_bins:] = -1         # produce false detections
        self.valid = idx >= 0
        self.channels, self.inverse = np.unique(idx[self.valid],
                                                return_inverse=True)
        self.n = len(self.channels)
        self.freqs_hz = self.channels * CHANNEL_HZ
        counts = np.bincount(self.inverse, minlength=self.n).astype(np.float64)
        counts[counts == 0] = 1.0
        self.counts = counts

    def power(self, psd):
        summed = np.bincount(self.inverse, weights=psd[self.valid],
                             minlength=self.n)
        return summed / self.counts


class NoiseFloor:
    """Rolling estimate of the quiet level on every channel.

    Two things keep this cheap. np.partition rather than np.percentile, because
    partition only rearranges enough of the array to expose the one value we
    want instead of sorting the whole thing. And recomputing every Nth frame
    rather than every frame, because background noise does not change in 13 ms.

    Measured on a 10 MSPS / 1600-channel configuration, those two changes take
    this from roughly a whole CPU core to about 1% of one.
    """

    def __init__(self, n_channels, frames=FLOOR_FRAMES,
                 pctile=FLOOR_PCTILE, every=FLOOR_EVERY):
        self.hist = []
        self.frames = frames
        self.k = max(0, min(frames - 1, int(frames * pctile / 100.0)))
        self.every = every
        self.value = None
        self.count = 0
        self.last_cost_ms = 0.0

    def update(self, power_db):
        self.hist.append(power_db)
        if len(self.hist) > self.frames:
            self.hist.pop(0)
        self.count += 1
        if len(self.hist) < 20:
            return None
        if self.value is None or self.count % self.every == 0:
            # k must be clamped to what we actually have: the history fills over
            # the first ~1.5 s and np.partition raises if kth exceeds its length.
            k = min(self.k, len(self.hist) - 1)
            t0 = time.perf_counter()
            self.value = np.partition(np.asarray(self.hist), k, axis=0)[k]
            self.last_cost_ms = (time.perf_counter() - t0) * 1000.0
        return self.value


class EventTracker:
    """Per-channel state machine turning power over time into keyed events."""

    IDLE, ACTIVE = 0, 1

    def __init__(self, n_channels, frame_seconds,
                 on_db=10.0, off_db=6.0, min_duration=0.12, hang=0.30):
        self.state = np.zeros(n_channels, np.int8)
        self.on_db = on_db
        self.off_db = off_db
        self.min_frames = max(1, int(round(min_duration / frame_seconds)))
        self.hang_frames = max(1, int(round(hang / frame_seconds)))
        self.above = np.zeros(n_channels, np.int32)
        self.below = np.zeros(n_channels, np.int32)
        self.start_sample = np.zeros(n_channels, np.int64)
        self.peak_snr = np.zeros(n_channels, np.float64)

    def update(self, snr_db, frame_start_sample):
        hot = snr_db >= self.on_db
        cold = snr_db < self.off_db
        self.above = np.where(hot, self.above + 1, 0)
        self.below = np.where(cold, self.below + 1, 0)

        idle = self.state == self.IDLE
        active = self.state == self.ACTIVE
        started = np.flatnonzero(idle & (self.above >= self.min_frames))
        ended = np.flatnonzero(active & (self.below >= self.hang_frames))

        if len(started):
            self.state[started] = self.ACTIVE
            self.start_sample[started] = frame_start_sample
            self.peak_snr[started] = snr_db[started]
        if len(ended):
            self.state[ended] = self.IDLE

        live = self.state == self.ACTIVE
        self.peak_snr[live] = np.maximum(self.peak_snr[live], snr_db[live])
        return started, ended


class OverloadMonitor:
    """Detects front-end overload and broadband desense.

    You cannot survey a festival in advance, so the receiver has to notice when
    it is being abused. Two independent symptoms:

      clipping — samples at or near full scale. Direct ADC overload.
      desense  — the quiet level on *every* channel rises together. One real
                 transmission lifts one channel; something strong compressing
                 the front end lifts all of them at once.

    Events during either are flagged, not discarded. A flagged event may still
    be real; the outcome worth avoiding is a clean-looking log that quietly
    contains junk.
    """

    def __init__(self, desense_db=6.0, baseline_frames=300):
        self.baseline = None
        self.baseline_frames = baseline_frames
        self.desense_db = desense_db
        self.history = []
        self.clip_frames = 0
        self.desense_frames = 0

    def update(self, samples, power_db):
        clip_frac = float(np.mean(np.abs(samples) > 0.9))
        clipping = clip_frac > 1e-4

        wideband = float(np.median(power_db))
        self.history.append(wideband)
        if len(self.history) > self.baseline_frames:
            self.history.pop(0)
        if len(self.history) >= 60:
            self.baseline = float(np.percentile(self.history, 20))

        desense = (self.baseline is not None
                   and wideband - self.baseline > self.desense_db)

        if clipping:
            self.clip_frames += 1
        if desense:
            self.desense_frames += 1
        return clipping, desense, clip_frac


# ---------------------------------------------------------------------------
# Per-channel analysis
# ---------------------------------------------------------------------------

def tone_magnitudes(x, fs, freqs):
    """Magnitude at each of `freqs` by direct DFT. Exact, and fast enough."""
    n = len(x)
    if n < 32:
        return np.zeros(len(freqs))
    t = np.arange(n, dtype=np.float64) / fs
    w = np.hanning(n)
    basis = np.exp(-2j * np.pi * np.outer(freqs, t))
    return np.abs(basis @ (x * w)) / n


def analyze_analog(iq, rate, offset_hz):
    """FM-demodulate one channel and identify any subaudible tone."""
    n = len(iq)

    # Frequency shift via a tiled lookup table. Channel offsets are always an
    # integer multiple of CHANNEL_HZ and rate/CHANNEL_HZ is an integer, so the
    # complex exponential repeats exactly every `period` samples. Tiling a
    # precomputed period is a memcpy instead of millions of transcendentals.
    period = int(round(rate / CHANNEL_HZ))
    k = int(round(offset_hz / CHANNEL_HZ))
    lut = np.exp(-2j * np.pi * k * np.arange(period) / period).astype(np.complex64)
    baseband = iq * np.tile(lut, -(-n // period))[:n]

    # Decimate with upfirdn and an explicitly sized filter. Not lfilter-then-
    # slice, which computes every output sample and discards 99% of them
    # (370 ms), and not resample_poly's defaults, which pick a ~2001-tap filter
    # here (270 ms). A 301-tap filter is ample for 100:1 and runs in 40 ms.
    decim = max(1, int(round(rate / 24000.0)))
    h = firwin(301, 1.0 / decim).astype(np.float32)
    baseband = upfirdn(h, baseband, 1, decim)
    audio_fs = rate / decim
    if len(baseband) < 64:
        return None

    # FM discriminator: instantaneous frequency in Hz.
    prod = baseband[1:] * np.conj(baseband[:-1])
    inst = np.angle(prod).astype(np.float64) * (audio_fs / (2.0 * np.pi))

    deviation = float(np.std(inst))
    freq_error = float(np.mean(inst))

    # Subaudible band: low-pass to 300 Hz, then decimate to ~2 kHz. The 300 Hz
    # filter is essential — without it the tone band carries voice energy and
    # the capture ratio below can never reach threshold. Decimate to 2 kHz not
    # 1 kHz: at 1 kHz, 900 Hz leakage folds onto 100 Hz and manufactures a
    # convincing false hit on tone 12.
    taps2 = firwin(255, min(300.0 / (audio_fs / 2.0), 0.99))
    filtered = lfilter(taps2, 1.0, inst)[len(taps2):]
    decim2 = max(1, int(round(audio_fs / 2000.0)))
    tone_sig = filtered[::decim2]
    tone_fs = audio_fs / decim2
    tone_sig = tone_sig - np.mean(tone_sig)

    empty = {"deviation_hz": deviation, "freq_error_hz": freq_error,
             "ctcss_hz": None, "ctcss_conf": 0.0, "ctcss_dev_hz": 0.0,
             "dcs_suspected": False}
    if len(tone_sig) / tone_fs < 0.9:
        return empty

    mags = tone_magnitudes(tone_sig, tone_fs, CTCSS_TONES)
    best = int(np.argmax(mags))
    tone_dev = 4.0 * float(mags[best])          # Hann gain, real sinusoid
    band_dev = float(np.sqrt(np.mean(tone_sig ** 2)))

    # Energy capture: what fraction of subaudible band power sits in the single
    # winning tone. This is the discriminator that matters.
    #
    # A peak-to-median ratio is NOT sufficient. DCS repeats a 23-bit word at
    # 134.4 bps, so it is periodic at 5.84 Hz and its harmonics land close to
    # real CTCSS frequencies — across random codewords a ratio test misidentifies
    # roughly three quarters of them as CTCSS. Capture separates them by a factor
    # of twenty: real tones measure ~0.99, DCS never exceeded 0.03 in testing.
    capture = ((tone_dev / math.sqrt(2.0)) / band_dev) ** 2 if band_dev > 0 else 0.0

    is_tone = capture >= 0.50 and tone_dev >= 100.0
    dcs_suspected = (not is_tone) and capture < 0.30 and band_dev >= 150.0

    return {
        "deviation_hz": deviation,
        "freq_error_hz": freq_error,
        "ctcss_hz": float(CTCSS_TONES[best]) if is_tone else None,
        "ctcss_conf": float(capture),
        "ctcss_dev_hz": tone_dev,
        "dcs_suspected": bool(dcs_suspected),
    }


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def find_peaks(power_db, floor_db, freqs_hz, top=25, min_snr=6.0):
    """Strongest channels above the background, as (freq_hz, snr_db) pairs."""
    snr = power_db - floor_db
    idx = np.flatnonzero(snr >= min_snr)
    if len(idx) == 0:
        return []
    idx = idx[np.argsort(snr[idx])[::-1]][:top]
    return [(float(freqs_hz[i]), float(snr[i])) for i in idx]


def render_spectrum(frames_db, center, rate, path, title):
    """Write an averaged spectrum and waterfall to a PNG. No display needed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — skipping PNG "
              "(sudo apt install python3-matplotlib)", file=sys.stderr)
        return False

    arr = np.asarray(frames_db)
    mhz_lo = (center - rate / 2) / 1e6
    mhz_hi = (center + rate / 2) / 1e6

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True,
        gridspec_kw={"height_ratios": [1, 2]})

    avg = arr.mean(axis=0)
    peak = arr.max(axis=0)
    x = np.linspace(mhz_lo, mhz_hi, arr.shape[1])
    ax1.plot(x, peak, lw=0.6, alpha=0.5, label="peak hold")
    ax1.plot(x, avg, lw=0.8, label="average")
    ax1.set_ylabel("dB")
    ax1.legend(loc="upper right", fontsize=8)
    ax1.grid(alpha=0.3)
    ax1.set_title(title)

    ax2.imshow(arr, aspect="auto", origin="lower", cmap="viridis",
               extent=[mhz_lo, mhz_hi, 0, arr.shape[0]],
               vmin=np.percentile(arr, 5), vmax=np.percentile(arr, 99.5))
    ax2.set_xlabel("MHz")
    ax2.set_ylabel("frame")

    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def spectrum_capture(args):
    """Look at a band without a monitor. Writes a PNG and prints the peaks."""
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

    spec = {"driver": args.driver}
    if args.serial:
        spec["serial"] = args.serial
    sdr = SoapySDR.Device(spec)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, args.rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, args.freq)
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    sdr.setGain(SOAPY_SDR_RX, 0, args.gain)
    if args.ppm:
        try:
            sdr.setFrequencyCorrection(SOAPY_SDR_RX, 0, args.ppm)
        except Exception:
            pass

    rate = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    center = sdr.getFrequency(SOAPY_SDR_RX, 0)
    fs = frame_size(rate)
    grid = ChannelGrid(center, rate)
    floor_est = NoiseFloor(grid.n)

    n_frames = max(20, int(args.spectrum_seconds * rate / fs))
    print(f"capturing {args.spectrum_seconds:.0f} s at {center/1e6:.4f} MHz, "
          f"{rate/1e6:.1f} MSPS ({n_frames} frames)...")

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(stream)
    chunk = np.empty(fs, np.complex64)
    window = np.hanning(NFFT).astype(np.float32)
    window_gain = float(np.sum(window ** 2))

    frames_db = []
    chan_db = []
    overflows = 0
    clipped = 0
    try:
        while len(frames_db) < n_frames:
            st = sdr.readStream(stream, [chunk], fs, timeoutUs=2_000_000)
            if st.ret <= 0:
                if st.ret == SoapySDR.SOAPY_SDR_OVERFLOW:
                    overflows += 1
                continue
            s = chunk[:st.ret]
            if np.mean(np.abs(s) > 0.9) > 1e-4:
                clipped += 1
            use = (st.ret // NFFT) * NFFT
            if use == 0:
                continue
            segs = s[:use].reshape(-1, NFFT)
            sp = np.fft.fftshift(np.fft.fft(segs * window, axis=1), axes=1)
            psd = (np.abs(sp) ** 2).mean(axis=0) / (window_gain * rate)
            frames_db.append(10.0 * np.log10(psd + 1e-20))
            cdb = 10.0 * np.log10(grid.power(psd) + 1e-20)
            chan_db.append(cdb)
            floor_est.update(cdb)
    finally:
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)

    if not frames_db:
        print("no samples captured", file=sys.stderr)
        return

    chan_arr = np.asarray(chan_db)

    # For a snapshot, the reference must be the typical level ACROSS the band,
    # not a rolling percentile over time. The time-based estimate used by the
    # live detector deliberately absorbs anything continuously present — good
    # for spotting key-ups, useless here, because a carrier that never stops
    # (a repeater idling, a stuck transmitter, a trunking control channel)
    # becomes part of its own background and vanishes.
    floor_db = float(np.median(np.median(chan_arr, axis=0)))
    peaks = find_peaks(chan_arr.max(axis=0),
                       np.full(grid.n, floor_db), grid.freqs_hz)

    title = (f"{center/1e6:.3f} MHz  {rate/1e6:.1f} MSPS  gain {args.gain}"
             f"   {time.strftime('%Y-%m-%d %H:%M')}")
    if render_spectrum(frames_db, center, rate, args.spectrum, title):
        print(f"wrote {args.spectrum}")

    print(f"\n  span {(center-rate/2)/1e6:.3f} - {(center+rate/2)/1e6:.3f} MHz")
    print(f"  overflows {overflows}   clipping frames {clipped}")
    if clipped:
        print("  ** CLIPPING — add attenuation or reduce gain")
    print(f"\n  strongest channels (peak hold over {args.spectrum_seconds:.0f} s):")
    if not peaks:
        print("    nothing above the background")
    for f_hz, snr in peaks:
        print(f"    {f_hz/1e6:11.4f} MHz   +{snr:5.1f} dB")
    print()


def open_db(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY,
            receiver_id   TEXT,
            ts_start      REAL NOT NULL,
            ts_end        REAL,
            freq_hz       INTEGER NOT NULL,
            duration_s    REAL,
            peak_snr_db   REAL,
            deviation_hz  REAL,
            freq_error_hz REAL,
            ctcss_hz      REAL,
            ctcss_conf    REAL,
            ctcss_dev_hz  REAL,
            dcs_suspected INTEGER,
            overload      INTEGER DEFAULT 0
        )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_freq ON events(freq_hz)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_start)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS coverage (
            receiver_id TEXT, ts_start REAL, ts_end REAL,
            center_hz INTEGER, span_hz INTEGER
        )""")
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Self test — runs with no radio attached
# ---------------------------------------------------------------------------

def make_fm(tone_hz, rate, dur=1.4, offset=12500.0, tone_dev=700.0,
            voice_dev=3000.0, noise=0.05, dcs_word=None, seed=0):
    t = np.arange(int(rate * dur)) / rate
    voice = 0.6 * np.sin(2 * np.pi * 900 * t) + 0.4 * np.sin(2 * np.pi * 1700 * t)
    mod = voice_dev * voice
    if dcs_word is not None:
        bit = (t * 134.4).astype(np.int64) % len(dcs_word)
        mod = mod + tone_dev * dcs_word[bit].astype(np.float64)
    elif tone_hz:
        mod = mod + tone_dev * np.sin(2 * np.pi * tone_hz * t)
    ph = 2 * np.pi * np.cumsum(mod) / rate
    z = np.exp(1j * (2 * np.pi * offset * t + ph)).astype(np.complex64)
    if noise:
        rng = np.random.default_rng(seed)
        z = z + (rng.standard_normal(len(t))
                 + 1j * rng.standard_normal(len(t))).astype(np.complex64) * noise
    return z


def selftest(rate, verbose=True):
    """Correctness and speed, with no hardware. Returns True if all checks pass."""
    ok = True
    print(f"\n=== self test @ {rate/1e6:.1f} MSPS ===\n")

    fs = frame_size(rate)
    grid = ChannelGrid(466.0e6, rate)
    print(f"  frame size        {fs} samples = {fs/rate*1000:.1f} ms"
          f"  ({rate/fs:.0f} frames/sec)")
    print(f"  channels watched  {grid.n}")
    ring_s = ANALYZE_SECONDS + PRETRIGGER_SECONDS + RING_SLACK_SECONDS
    print(f"  ring buffer       {ring_s*rate*8/1e6:.0f} MB\n")

    # --- correctness -------------------------------------------------------
    # Run these at 2.4 MSPS regardless of the target rate. The tone logic
    # decimates everything to ~24 kHz and then ~2 kHz before it looks at
    # anything, so the answers are rate-independent — but generating 1.4 s of
    # synthetic signal at 10 MSPS costs 112 MB and a couple of seconds each,
    # and there are 70 test cases. Low rate here keeps the check under a minute.
    crate = 2.4e6
    print("  tone identification  (checked at 2.4 MSPS — logic is rate-independent)")
    bad = [w for w in CTCSS_TONES
           if analyze_analog(make_fm(w, crate), crate, 12500.0)["ctcss_hz"] != w]
    print(f"    {len(CTCSS_TONES)-len(bad)}/{len(CTCSS_TONES)} standard tones correct")
    ok &= not bad
    if bad and verbose:
        print(f"    FAILED: {bad}")

    r = analyze_analog(make_fm(88.5, crate, tone_dev=450.0, noise=0.5), crate, 12500.0)
    weak_ok = r["ctcss_hz"] == 88.5
    print(f"    weak tone in noise: {'ok' if weak_ok else 'FAILED'}")
    ok &= weak_ok

    dcs_bad = 0
    for seed in range(12):
        word = np.random.default_rng(seed).integers(0, 2, 23) * 2 - 1
        r = analyze_analog(make_fm(None, crate, dcs_word=word), crate, 12500.0)
        if r["ctcss_hz"] is not None or not r["dcs_suspected"]:
            dcs_bad += 1
    print(f"    {12-dcs_bad}/12 DCS codewords correctly rejected")
    ok &= dcs_bad == 0

    r = analyze_analog(make_fm(None, crate), crate, 12500.0)
    clean = r["ctcss_hz"] is None and not r["dcs_suspected"]
    print(f"    no-tone carrier: {'clean' if clean else 'FALSE POSITIVE'}")
    ok &= clean

    # --- speed -------------------------------------------------------------
    print("\n  speed on this machine")

    def bench(fn, k=5):
        fn()
        t0 = time.perf_counter()
        for _ in range(k):
            fn()
        return (time.perf_counter() - t0) / k * 1000.0

    iq = (np.random.randn(int(ANALYZE_SECONDS * rate))
          + 1j * np.random.randn(int(ANALYZE_SECONDS * rate))).astype(np.complex64)
    analyze_ms = bench(lambda: analyze_analog(iq, rate, 12500.0))

    chunk = (np.random.randn(fs) + 1j * np.random.randn(fs)).astype(np.complex64)
    win = np.hanning(NFFT).astype(np.float32)
    nseg = fs // NFFT
    segs = chunk[:nseg * NFFT].reshape(nseg, NFFT)

    def detect_step():
        spec = np.fft.fftshift(np.fft.fft(segs * win, axis=1), axes=1)
        psd = (np.abs(spec) ** 2).mean(axis=0)
        return grid.power(psd)

    detect_ms = bench(detect_step, 20)

    floor = NoiseFloor(grid.n)
    pdb = np.random.randn(grid.n) - 90.0
    for _ in range(FLOOR_FRAMES):
        floor.update(pdb)
    floor_ms = floor.last_cost_ms

    fps = rate / fs
    detect_load = detect_ms * fps / 1000 * 100
    floor_load = floor_ms * fps / FLOOR_EVERY / 1000 * 100
    concurrent = ANALYZE_SECONDS / (analyze_ms / 1000.0)

    print(f"    detection per frame   {detect_ms:6.2f} ms   -> {detect_load:5.1f}% of one core")
    print(f"    background estimate   {floor_ms:6.2f} ms   -> {floor_load:5.1f}% of one core")
    print(f"    analysing one event   {analyze_ms:6.1f} ms   -> {concurrent:.1f}x realtime per core")

    steady = detect_load + floor_load
    print(f"\n    steady load, one radio: {steady:.1f}% of one core "
          f"({steady/4:.1f}% of four)")
    print(f"    simultaneous transmissions, 4 cores: about {concurrent*4*0.7:.0f}")

    print(f"\n  verdict: {'ALL CHECKS PASS' if ok else 'FAILURES ABOVE'}")
    if steady > 60:
        print("  WARNING: steady load is high. Two radios will not fit on this machine.")
    print()
    return ok


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args):
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

    spec = {"driver": args.driver}
    if args.serial:
        spec["serial"] = args.serial
    sdr = SoapySDR.Device(spec)

    sdr.setSampleRate(SOAPY_SDR_RX, 0, args.rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, args.freq)
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)     # AGC off — non-negotiable
    except Exception:
        print("warning: could not disable AGC", file=sys.stderr)
    sdr.setGain(SOAPY_SDR_RX, 0, args.gain)
    if args.ppm:
        try:
            sdr.setFrequencyCorrection(SOAPY_SDR_RX, 0, args.ppm)
        except Exception:
            print("warning: driver rejected ppm correction", file=sys.stderr)

    rate = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    center = sdr.getFrequency(SOAPY_SDR_RX, 0)
    fs = frame_size(rate)

    grid = ChannelGrid(center, rate)
    frame_seconds = fs / rate
    tracker = EventTracker(grid.n, frame_seconds,
                           on_db=args.on_db, off_db=args.off_db)
    floor_est = NoiseFloor(grid.n)
    overload = OverloadMonitor()

    print(f"receiver {args.receiver_id}: {center/1e6:.4f} MHz, {rate/1e6:.3f} MSPS, "
          f"gain {args.gain}")
    print(f"{grid.n} channels on a {CHANNEL_HZ/1000:.2f} kHz grid, "
          f"{frame_seconds*1000:.1f} ms frames ({rate/fs:.0f}/sec)")

    analyze_samples = int(ANALYZE_SECONDS * rate)
    pretrigger = int(PRETRIGGER_SECONDS * rate)
    ring_seconds = ANALYZE_SECONDS + PRETRIGGER_SECONDS + RING_SLACK_SECONDS
    ring = Ring(int(ring_seconds * rate))
    print(f"ring buffer {ring_seconds*rate*8/1e6:.0f} MB\n")

    db = open_db(args.db)
    session_start = time.time()

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(stream)

    chunk = np.empty(fs, np.complex64)
    window = np.hanning(NFFT).astype(np.float32)
    window_gain = float(np.sum(window ** 2))
    nseg = fs // NFFT

    pending = {}
    open_events = {}
    overflows = 0
    frames = 0
    events_logged = 0
    detect_ms_total = 0.0
    analyze_ms_total = 0.0
    analyses = 0
    last_stat = time.time()
    stat_frames = 0

    running = [True]
    signal.signal(signal.SIGINT, lambda *_: running.__setitem__(0, False))

    try:
        while running[0]:
            status = sdr.readStream(stream, [chunk], fs, timeoutUs=2_000_000)
            if status.ret <= 0:
                if status.ret == SoapySDR.SOAPY_SDR_OVERFLOW:
                    overflows += 1
                    print("OVERFLOW — samples dropped", file=sys.stderr)
                continue

            samples = chunk[:status.ret]
            frame_start = ring.written
            ring.push(samples)
            frames += 1
            stat_frames += 1

            # ---- detection -------------------------------------------------
            t0 = time.perf_counter()
            if status.ret < NFFT:
                continue
            use = (status.ret // NFFT) * NFFT
            segs = samples[:use].reshape(-1, NFFT)
            spec_ = np.fft.fftshift(np.fft.fft(segs * window, axis=1), axes=1)
            psd = (np.abs(spec_) ** 2).mean(axis=0) / (window_gain * rate)
            power_db = 10.0 * np.log10(grid.power(psd) + 1e-20)
            detect_ms_total += (time.perf_counter() - t0) * 1000.0

            clipping, desense, clip_frac = overload.update(samples, power_db)
            if clipping or desense:
                total = overload.clip_frames + overload.desense_frames
                if total == 1 or total % 500 == 0:
                    why = "clipping" if clipping else "desense"
                    print(f"  ** OVERLOAD ({why}) — add attenuation "
                          f"[clip {clip_frac*100:.2f}%]", file=sys.stderr)

            floor_db = floor_est.update(power_db)
            if floor_db is None:
                continue
            snr_db = power_db - floor_db

            started, ended = tracker.update(snr_db, frame_start)
            now = time.time()

            for ch in started:
                ch = int(ch)
                pending[ch] = max(0, tracker.start_sample[ch] - pretrigger)
                cur = db.execute(
                    "INSERT INTO events (receiver_id, ts_start, freq_hz, overload)"
                    " VALUES (?,?,?,?)",
                    (args.receiver_id, now, int(grid.freqs_hz[ch]),
                     int(clipping or desense)))
                open_events[ch] = cur.lastrowid

            # ---- analyse channels with enough dwell -------------------------
            for ch, start in list(pending.items()):
                if ring.written < start + analyze_samples:
                    continue
                iq = ring.get(start, analyze_samples)
                del pending[ch]
                if iq is None or ch not in open_events:
                    continue
                t1 = time.perf_counter()
                result = analyze_analog(iq, rate, grid.freqs_hz[ch] - center)
                analyze_ms_total += (time.perf_counter() - t1) * 1000.0
                analyses += 1
                if result is None:
                    continue
                db.execute(
                    """UPDATE events SET deviation_hz=?, freq_error_hz=?,
                       ctcss_hz=?, ctcss_conf=?, ctcss_dev_hz=?, dcs_suspected=?
                       WHERE id=?""",
                    (result["deviation_hz"], result["freq_error_hz"],
                     result["ctcss_hz"], result["ctcss_conf"],
                     result["ctcss_dev_hz"], int(result["dcs_suspected"]),
                     open_events[ch]))
                tone = (f"{result['ctcss_hz']:.1f} Hz cap {result['ctcss_conf']:.2f}"
                        if result["ctcss_hz"]
                        else ("DCS?" if result["dcs_suspected"] else "no tone"))
                print(f"  {grid.freqs_hz[ch]/1e6:10.4f} MHz  "
                      f"snr {tracker.peak_snr[ch]:5.1f} dB  "
                      f"dev {result['deviation_hz']:6.0f} Hz  {tone}")

            for ch in ended:
                ch = int(ch)
                pending.pop(ch, None)
                row = open_events.pop(ch, None)
                if row is None:
                    continue
                duration = (frame_start - tracker.start_sample[ch]) / rate
                db.execute(
                    "UPDATE events SET ts_end=?, duration_s=?, peak_snr_db=? WHERE id=?",
                    (now, duration, float(tracker.peak_snr[ch]), row))
                events_logged += 1
                print(f"  {grid.freqs_hz[ch]/1e6:10.4f} MHz  ended, {duration:.2f} s")

            if started.size or ended.size:
                db.commit()

            # ---- periodic stats ---------------------------------------------
            if args.stats and time.time() - last_stat >= 15.0:
                el = time.time() - last_stat
                fps = stat_frames / el
                d_ms = detect_ms_total / max(1, stat_frames)
                a_ms = analyze_ms_total / max(1, analyses)
                uptime_h = (time.time() - session_start) / 3600.0
                print(f"[stats] {fps:5.1f} fps (target {rate/fs:.0f})   "
                      f"overflow {overflows}   active {int((tracker.state==1).sum())}   "
                      f"events {events_logged} ({events_logged/max(uptime_h,1/60):.0f}/hr)")
                print(f"        detect {d_ms:5.2f} ms/frame ({d_ms*fps/10:4.1f}% core)   "
                      f"floor {floor_est.last_cost_ms:5.2f} ms/{FLOOR_EVERY} frames   "
                      f"analyse {a_ms:6.1f} ms x{analyses}")
                print(f"        clip {overload.clip_frames}   "
                      f"desense {overload.desense_frames}")
                detect_ms_total = analyze_ms_total = 0.0
                analyses = 0
                stat_frames = 0
                last_stat = time.time()

    finally:
        db.execute(
            "INSERT INTO coverage (receiver_id, ts_start, ts_end, center_hz, span_hz)"
            " VALUES (?,?,?,?,?)",
            (args.receiver_id, session_start, time.time(), int(center), int(rate)))
        db.commit()
        db.close()
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)
        print(f"\nstopped. overflows: {overflows}  events: {events_logged}  "
              f"clip frames: {overload.clip_frames}  "
              f"desense frames: {overload.desense_frames}")
        if overflows:
            print("Overflows mean dropped samples and unreliable data — check USB "
                  "topology with 'lsusb -t' and power with 'dmesg | grep -i voltage'.")
        if overload.clip_frames or overload.desense_frames:
            print("Front end was overloaded. Affected events are flagged in the "
                  "`overload` column. Add attenuation — 20 dB is the default and "
                  "costs no usable sensitivity at festival distances.")


def main():
    p = argparse.ArgumentParser(description="RF survey deck bench prototype")
    p.add_argument("--selftest", action="store_true",
                   help="check and benchmark without any radio attached")
    p.add_argument("--driver", default="airspy")
    p.add_argument("--serial", default=None,
                   help="address radios by serial, never by index")
    p.add_argument("--freq", type=float, default=466.0e6, help="centre, Hz")
    p.add_argument("--rate", type=float, default=10e6)
    p.add_argument("--gain", type=float, default=12.0)
    p.add_argument("--ppm", type=float, default=0.0)
    p.add_argument("--on-db", type=float, default=10.0)
    p.add_argument("--off-db", type=float, default=6.0)
    p.add_argument("--db", default="survey.sqlite")
    p.add_argument("--receiver-id", default="rx0")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--spectrum", metavar="PATH",
                   help="capture a band and write a spectrum/waterfall PNG, "
                        "then print the strongest channels. For headless use.")
    p.add_argument("--spectrum-seconds", type=float, default=10.0)
    args = p.parse_args()

    if args.selftest:
        sys.exit(0 if selftest(args.rate) else 1)
    if args.spectrum:
        spectrum_capture(args)
        return
    run(args)


if __name__ == "__main__":
    main()
