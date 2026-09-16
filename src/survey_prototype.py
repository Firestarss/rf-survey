#!/usr/bin/env python3
"""
RF survey deck — bench prototype.

Watches a slice of spectrum, notices every time someone transmits, works out the
mode and any subaudible tone, and logs it to SQLite.

Two modes:

  --selftest      Sizes and benchmarks this machine, with no radio attached,
                  so you know whether it can keep up with two receivers.
                  Correctness is `bash tools/run-tests.sh`. Do both first.

  (normal)        Opens a radio and runs for real.

Examples:

    python3 survey_prototype.py --selftest --rate 10e6

    python3 survey_prototype.py --driver airspy --serial 0x1234ABCD \
        --freq 466.0e6 --rate 10e6 --gain 12 --ppm 0.4 \
        --db data/survey.sqlite --receiver-id uhf --stats

Needs: numpy, scipy, and (for real use) SoapySDR with python3 bindings.
"""

import argparse
import collections
import math
import pathlib
import queue
import shutil
import signal
import sys
import tempfile
import threading
import time
import wave

import numpy as np
import scipy.fft as sfft
from scipy.signal import firwin, lfilter, upfirdn

import db
import status
import dcs as dcs_mod

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
FLOOR_EVERY = 20           # recompute the background every Nth frame. Was 10;
                           # np.partition over 120 x n_channels cost 0.55 ms per
                           # frame at 10 MSPS, 8% of a core, to re-estimate
                           # something that does not change in 130 ms.
# Clipping is a bulk property of a frame, so it does not need every sample. One
# in four over 65536 still sees 16384, which resolves the 1e-4 threshold to
# three significant figures and costs a quarter as much memory traffic.
CLIP_STRIDE = 4
# Channels spanned by the frequency-domain median that estimates the noise floor
# for --spectrum. 21 channels is 131 kHz: an order of magnitude wider than any
# signal in this band, and an order of magnitude narrower than the passband
# shape it has to follow. See spectrum_floor().
FLOOR_MEDIAN_CHANNELS = 21

# Threads scipy hands the detection FFTs.
#
# The win here is not the threading: `np.fft` upcasts complex64 to complex128
# and transforms in double precision on single-precision data, so simply moving
# to scipy.fft — which respects the input dtype — is 2x on its own. Measured on
# the Pi 5, 2026-08-27, one 65536-sample frame at 10 MSPS:
#
#     np.fft            2.454 ms/frame   37.4% of one core
#     scipy workers=1   1.223 ms         18.7%
#     scipy workers=2   0.762 ms         11.6%
#     scipy workers=4   1.223 ms         18.7%
#
# So workers=2 looks best, and in isolation it is. **Under load it reverses.**
# With the analysis worker running, measured against live traffic at gain 42:
#
#     workers=1   detect 2.23 ms/frame   143.5 fps   22 overflows
#     workers=2   detect 2.61 ms/frame   137.6 fps   38 overflows
#
# The FFT's own threads contend with the analysis thread for the GIL, and the
# extra parallelism costs more than it returns. One worker.
FFT_WORKERS = 1

# Front-end linearity check. Three gain settings, `COMPRESSION_GAIN_STEP` apart,
# and the two increments must agree: a linear receiver moves its noise floor by
# the same amount for each equal step down. Self-calibrating, which matters
# because the Airspy's "dB" of gain are index steps and three of them move the
# floor about 10 dB, not 3 — so an absolute expectation would be device lore.
COMPRESSION_GAIN_STEP = 3.0
COMPRESSION_MIN_RISE_DB = 2.0   # below this the lower pair says nothing usable
COMPRESSION_RATIO = 0.7         # top step this far under the lower one = squashed
COMPRESSION_SECONDS = 0.5       # per gain point
# Ceiling for the upward probe. The SoapyAirspy overall gain is an 0-45 control
# filling LNA, then MIX, then VGA — confirmed against the hardware on
# 2026-08-26, and not the 0-21 "linearity" control the Airspy documentation
# describes. Asking for more than the device has would either clamp silently and
# make two probe points identical, or throw.
MAX_GAIN_DB = 45.0
# How far above the antenna-removed floor the band must sit before the antenna
# is believed to be connected. The two antennas measured on 2026-08-27 lifted
# the floor 7.3 and 8.1 dB at their working gains, so 3 dB is comfortably below
# any real antenna and comfortably above measurement scatter.
ANTENNA_MISSING_DB = 3.0
# Analysis has three different dwell requirements, not one, so it has three
# constants. Measured against synthetic signals by sweeping dwell (see
# docs/handoff.md); every number below is the knee of a measured curve.
#
#   deviation and frequency error are per-sample statistics and need almost
#   nothing: at 0.10 s they read within 1% of what they read at 1.40 s.
#   DCS needs two whole 23-bit words to frame and cross-check, plus up to one
#   word of rotation slack, so a little over 0.5 s.
#   CTCSS is the expensive one. Adjacent standard tones are 2.3 Hz apart at the
#   low end, which is below the Rayleigh limit for a short window, so what
#   matters is not whether the right tone wins but by how much. Margin over the
#   nearest neighbour, measured at ~14 dB in-channel SNR:
#
#       0.20 s   0.7 dB      0.70 s  11.4 dB
#       0.30 s   1.8 dB      0.90 s  22.8 dB
#       0.50 s   5.3 dB      1.40 s  35.2 dB
#
#   It identifies all 54 tones correctly at every one of those dwells, which is
#   exactly why pass/fail is the wrong measure — at 0.20 s it wins by 8%, on a
#   synthetic signal that sits exactly on a candidate frequency. 0.70 s is the
#   shortest dwell with margin left over for a real signal that does not.
ANALYZE_SECONDS = 0.9      # dwell that triggers a full analysis mid-transmission
MIN_ANALYZE_SECONDS = 0.12  # below this there is nothing worth demodulating
MIN_TONE_SECONDS = 0.7     # below this, CTCSS identification is not attempted
MIN_DCS_SECONDS = 0.55     # below this, DCS framing cannot be cross-checked
DCS_MIN_WORDS = 2          # repeats that must agree before a code is believed

# Consecutive empty reads before the capture loop gives up on the device. At the
# usual frame rate this is a few seconds. A single failed read is a timeout and
# is normal; a run of them is a radio that has stopped talking, and no amount of
# waiting in-process fixes a wedged USB endpoint.
STALL_FRAMES = 200
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

    def reset(self):
        """Forget everything. Used on retune — IQ from the old centre is not
        merely stale, it is a different part of the spectrum."""
        self.written = 0
        self.buf[:] = 0

    def push(self, x):
        n = len(x)
        if n >= self.cap:
            # Only the last `cap` samples can survive, but they still have to
            # land in the slots their absolute indices map to. Writing them at
            # buf[0:] instead — as this did — leaves every subsequent get()
            # offset by (written % cap), returning perfectly valid samples from
            # the wrong moment in time, which nothing downstream can detect.
            drop = n - self.cap
            self.written += drop
            x = x[drop:]
            n = self.cap
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


def to_db(power):
    """Power to dB, with a floor so an empty bin does not produce -inf.

    Not named db(): this module imports db, the database layer, and shadowing
    that at module scope makes every write in run() fail at the first call.
    """
    return 10.0 * np.log10(power + 1e-20)


class Periodogram:
    """Averaged power spectral density over the whole NFFT segments of a frame.

    The detector and `--spectrum` both need exactly this and had their own copy
    of it; the window and its gain are precomputed here because they depend only
    on NFFT and recomputing them per frame is pure waste.
    """

    def __init__(self, rate, nfft=NFFT):
        self.nfft = nfft
        self.rate = rate
        self.window = np.hanning(nfft).astype(np.float32)
        self.gain = float(np.sum(self.window ** 2))

    def __call__(self, samples):
        """PSD of `samples`, or None if it is shorter than one segment."""
        use = (len(samples) // self.nfft) * self.nfft
        if use == 0:
            return None
        segs = samples[:use].reshape(-1, self.nfft)
        # scipy rather than numpy: np.fft promotes complex64 to complex128 and
        # transforms in double, which is pure waste on data that arrived from
        # the radio as float32. See FFT_WORKERS.
        spec = sfft.fftshift(sfft.fft(segs * self.window, axis=1,
                                      workers=FFT_WORKERS), axes=1)
        return (np.abs(spec) ** 2).mean(axis=0) / (self.gain * self.rate)


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

    def update(self, power_db, active=None):
        """`active` marks channels currently inside an event; see below.

        A channel that is transmitting must not contribute its own power to the
        estimate of its quiet level. This history is FLOOR_FRAMES long and the
        estimate is a low percentile of it, so a carrier that stays up long
        enough to fill (100 - FLOOR_PCTILE)% of the history drags the floor up to
        meet itself, the SNR collapses, and the detector calls the transmission
        over while it is still going. At the default 120 frames and the 25th
        percentile that happens after 1.26 s — so every transmission longer than
        that was being truncated to 1.27 s, and every airtime total with it. A
        30 second ham QSO was logging as 1.27 seconds.

        Substituting the last known floor for active channels keeps their history
        at the quiet level, which is what the estimate is supposed to mean. The
        mask is one frame stale, because the floor has to exist before the SNR
        that decides what is active can be computed; a single frame of lag is
        immaterial against a 120-frame history.
        """
        if active is not None and self.value is not None:
            power_db = np.where(active, self.value, power_db)
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

        # A detection is only declared after min_frames of signal, and only
        # closed after hang_frames of silence, so the frame we notice in is
        # never the frame it happened in. Reporting the noticing frame inflates
        # every duration by min_duration + hang (0.42 s here) and every airtime
        # total with it. Keeping the recent frame boundaries lets both edges be
        # reported where they actually occurred.
        #
        # Frame lengths vary — readStream returns what it has — so this holds
        # real sample offsets rather than multiplying a nominal frame size.
        self._recent = collections.deque(maxlen=max(self.min_frames,
                                                    self.hang_frames))

    def _frames_ago(self, k):
        """Start sample of the frame k frames before the current one."""
        if k >= len(self._recent):
            return self._recent[0]
        return self._recent[-1 - k]

    def update(self, snr_db, frame_start_sample, can_start=None):
        """`can_start` gates which channels may OPEN an event, not which may
        continue one. See the local-maximum mask in run(): a channel that is
        merely the skirt of its neighbour's transmission must never start its
        own event, but a channel already in an event must not be torn down by a
        momentary dip below its neighbour."""
        self._recent.append(frame_start_sample)
        hot = snr_db >= self.on_db
        cold = snr_db < self.off_db
        self.above = np.where(hot, self.above + 1, 0)
        self.below = np.where(cold, self.below + 1, 0)

        idle = self.state == self.IDLE
        active = self.state == self.ACTIVE
        eligible = idle & (self.above >= self.min_frames)
        if can_start is not None:
            eligible &= can_start
        started = np.flatnonzero(eligible)
        ended = np.flatnonzero(active & (self.below >= self.hang_frames))

        # The signal has been hot since the first of the min_frames frames that
        # triggered this, and cold since the first of the hang_frames frames
        # that closed it.
        self.last_start_sample = self._frames_ago(self.min_frames - 1)
        self.last_end_sample = self._frames_ago(self.hang_frames - 1)

        if len(started):
            self.state[started] = self.ACTIVE
            self.start_sample[started] = self.last_start_sample
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

    BASELINE_PCTILE = 20        # quiet level across the baseline history

    def __init__(self, desense_db=6.0, baseline_frames=300):
        self.baseline = None
        self.baseline_frames = baseline_frames
        self.desense_db = desense_db
        self.history = []
        self.clip_frames = 0
        self.desense_frames = 0

    def update(self, samples, power_db):
        clip_frac = float(np.mean(np.abs(samples[::CLIP_STRIDE]) > 0.9))
        clipping = clip_frac > 1e-4

        wideband = float(np.median(power_db))
        self.history.append(wideband)
        if len(self.history) > self.baseline_frames:
            self.history.pop(0)
        if len(self.history) >= 60:
            self.baseline = float(np.percentile(self.history,
                                                self.BASELINE_PCTILE))

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


def decode_dcs(tone_sig, tone_fs):
    """Recover a DCS code from the subaudible waveform.

    Returns (code, polarity, bit_errors, words_agreeing) or None.

    Three stages, each of which can fail cheaply:

    1. Bit clock. The word runs at 134.4 bps with no preamble and no transitions
       guaranteed, so the phase is found by trying eight sampling offsets and
       keeping whichever integrates to the widest eye.
    2. Framing. There is no sync pattern either — the word simply repeats — so
       all 23 rotations are tried, in both polarities. An inverted DCS is the
       same word with every bit flipped, which is a real thing radios transmit.
    3. Agreement, then the polarity pair. Golay correction maps *every* 23-bit
       input to some codeword, so a single decode is not evidence: noise clears
       the fixed triple and lands in the standard code list about once every 37
       tries. Two repeats of the word must decode to the same code first.

       Then the structure of the standard is used as a second, much stronger
       check. Because the code is cyclic and all-ones is a codeword, a genuine
       DCS waveform presents exactly two legal readings — one normal, one
       inverted — and they are a documented pair: 023 normal is 047 inverted, the
       same signal. So a real transmission yields one N code and its
       INVERTED_PAIR partner and nothing else. Noise does not produce that
       structure, and neither does a CTCSS tone sliced at 134.4 bps.

    Reports the normal reading, which every waveform has exactly one of.
    Returning None does not mean "no DCS" — analyze_analog still flags
    dcs_suspected from the capture ratio, and the channel caps at tier 2 exactly
    as it did before decoding existed.
    """
    spb = tone_fs / dcs_mod.BPS
    nbits = int((len(tone_sig) - 1) / spb)
    if nbits < dcs_mod.WORD_BITS * DCS_MIN_WORDS:
        return None

    # Integrate each bit period rather than point-sampling it: the waveform has
    # been low-passed to 300 Hz, so it is a rounded square and the mean over the
    # period is far more robust than any single sample.
    csum = np.cumsum(np.concatenate(([0.0], tone_sig)))
    best = None
    for frac in np.arange(0.0, 1.0, 0.125):
        edges = ((np.arange(nbits + 1) + frac) * spb).astype(np.int64)
        edges = edges[edges <= len(tone_sig)]
        if len(edges) < dcs_mod.WORD_BITS * DCS_MIN_WORDS + 1:
            continue
        vals = (csum[edges[1:]] - csum[edges[:-1]]) / np.diff(edges)
        score = float(np.mean(np.abs(vals)))
        if best is None or score > best[0]:
            best = (score, vals)
    if best is None:
        return None
    raw = best[1]

    found = []
    for polarity, bits in (("N", (raw > 0).astype(np.int8)),
                           ("I", (raw <= 0).astype(np.int8))):
        for rot in range(dcs_mod.WORD_BITS):
            usable = bits[rot:]
            k = len(usable) // dcs_mod.WORD_BITS
            if k < DCS_MIN_WORDS:
                continue
            rows = usable[:k * dcs_mod.WORD_BITS].reshape(k, dcs_mod.WORD_BITS)

            # Every repeat decoded on its own. Agreement across them is the
            # evidence; the majority vote below is only how the answer is read.
            votes = {}
            for row in rows:
                word = int(np.sum(row.astype(np.int64) << np.arange(dcs_mod.WORD_BITS)))
                got = dcs_mod.decode(word)
                if got:
                    votes[got[0]] = votes.get(got[0], 0) + 1
            if not votes:
                continue
            code, agree = max(votes.items(), key=lambda kv: kv[1])
            if agree < DCS_MIN_WORDS:
                continue

            majority = (rows.sum(axis=0) * 2 > k).astype(np.int64)
            word = int(np.sum(majority << np.arange(dcs_mod.WORD_BITS)))
            got = dcs_mod.decode(word)
            if got and got[0] == code:
                found.append((agree, -got[1], code, polarity, got[1]))

    if not found:
        return None

    normal = {f[2] for f in found if f[3] == "N"}
    inverted = {f[2] for f in found if f[3] == "I"}
    if len(normal) != 1:
        # A real waveform has exactly one normal reading. Anything else is noise
        # that happened to clear the checks at more than one framing.
        return None
    code = normal.pop()
    if inverted and inverted != {dcs_mod.INVERTED_PAIR[code]}:
        # The inverted reading must be this code's documented partner. It is a
        # free consistency check and noise almost never satisfies it.
        return None

    best = max(f for f in found if f[2] == code and f[3] == "N")
    agree, _, _, _, nerr = best
    return code, "N", nerr, agree


def analyze_analog(iq, rate, offset_hz, keep_signals=False):
    """FM-demodulate one channel and identify any subaudible tone."""
    n = len(iq)

    # Frequency shift via a tiled lookup table. Channel offsets are always an
    # integer multiple of CHANNEL_HZ and rate/CHANNEL_HZ is an integer, so the
    # complex exponential repeats exactly every `period` samples. Tiling a
    # precomputed period is a memcpy instead of millions of transcendentals.
    # Broadcast the period across whole rows rather than np.tile-ing it out to
    # full length first: at 10 MSPS the tile alone is another 96 MB copy of the
    # window, allocated and thrown away for every event analysed.
    period = int(round(rate / CHANNEL_HZ))
    k = int(round(offset_hz / CHANNEL_HZ))
    lut = np.exp(-2j * np.pi * k * np.arange(period) / period).astype(np.complex64)
    whole = (n // period) * period
    baseband = np.empty(n, np.complex64)
    if whole:
        np.multiply(iq[:whole].reshape(-1, period), lut,
                    out=baseband[:whole].reshape(-1, period))
    if whole < n:
        baseband[whole:] = iq[whole:] * lut[:n - whole]

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

    # Trim to where the signal actually is, before demodulating anything.
    #
    # The analysis window deliberately begins PRETRIGGER_SECONDS before the
    # detector fired, and for a short transmission it can also run past the end,
    # so a large fraction of it may be noise with no carrier in it at all. That
    # matters far more than it sounds: an FM discriminator fed noise produces
    # instantaneous frequencies spread uniformly over +/- audio_fs/2, so a window
    # that is one third noise puts p99(|inst|) at ~11.7 kHz whatever the
    # transmitter was doing, and every CTCSS capture ratio is diluted by it.
    #
    # This was invisible for as long as the analyser was only ever tested on
    # windows that were pure signal. Run through the actual capture path, every
    # single event reported a deviation of ~11700 Hz — the noise figure, not a
    # measurement — until this trim existed.
    #
    # A constant-envelope FM carrier makes this easy: |baseband| is flat while
    # the carrier is present and drops to the noise level when it is not.
    # Smoothed before thresholding, and the edges taken from a percentile of
    # the crossings rather than the first and last one. A single noise spike in
    # the lead-in is enough to make strong[0] the very first sample, which
    # defeats the trim entirely and puts the noise figure back in the answer.
    mag = np.abs(baseband)
    win = max(1, int(0.002 * audio_fs))         # 2 ms; shorter than any keyup
    smooth = np.convolve(mag, np.ones(win) / win, mode="same")
    strong = np.flatnonzero(smooth > 0.4 * np.percentile(smooth, 95))
    if len(strong) >= 64:
        lo, hi = int(np.percentile(strong, 1)), int(np.percentile(strong, 99))
        if hi - lo >= 64:
            baseband = baseband[lo:hi + 1]

    # FM discriminator: instantaneous frequency in Hz.
    prod = baseband[1:] * np.conj(baseband[:-1])
    inst = np.angle(prod).astype(np.float64) * (audio_fs / (2.0 * np.pi))

    # Peak deviation, not RMS. std(inst) reads ~0.42x the true peak against
    # voice, so a 2.5 kHz Part 95 threshold would never fire and the FRS rule
    # would silently never rule anything out. RMS also tracks how loudly someone
    # is talking, while peak deviation is pinned near the limit by the
    # transmitter's own deviation limiter and is stable across talkers.
    #
    # p99 of |inst| rather than max(): one discriminator click from a noise
    # spike sets max() to something meaningless. Measured against synthetic
    # voice at high SNR:
    #
    #                      true peak    std(inst)   p99|inst|
    #     FRS-like  2.5 kHz     2700         1127        2537
    #     GMRS wide 5.0 kHz     5750         2581        5359
    #
    # Measured about the carrier, not about the channel centre. inst has a DC
    # term equal to however far the transmitter sits from the 6.25 kHz grid
    # slot it was filed under, and p99(|inst|) adds that offset straight onto
    # the answer:
    #
    #     carrier off grid    0 Hz      1250 Hz    2500 Hz    3125 Hz
    #     reported (2.4 kHz)  2326      3539       4788       5414
    #
    # Eleven of the seeded channels are off-grid by 1250-2500 Hz — every MURS
    # channel, several Part 90 VHF dots, and 146.520 — so a narrowband signal
    # on one of them reported wide, and "wide" is the verdict that rules FRS
    # out. Subtracting the mean is what makes this peak DEVIATION rather than
    # peak excursion from an arbitrary grid.
    #
    # This is an estimate of peak deviation in Hz. Nothing has verified it
    # against a real transmitter yet — that is Phase 3.
    freq_error = float(np.mean(inst))
    deviation = float(np.percentile(np.abs(inst - freq_error), 99.0))

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

    # Deviation and frequency error are done. Everything below needs dwell that a
    # short transmission may not have, so each stage is gated on its own
    # requirement rather than the analysis being all-or-nothing. A 0.3 s "copy
    # that" still yields deviation, and therefore still reaches tier 1.
    # Dwell is measured on what survived the trim, not on what was handed in:
    # the tone stages need that much *signal*, and the caller's window may have
    # been mostly silence.
    dwell = len(tone_sig) / tone_fs
    if keep_signals:
        # The channel as the analyser saw it, for retention. `baseband` is the
        # full complex channel at audio_fs and is everything a better algorithm
        # would need later; `inst` is the demodulated audio a human can listen
        # to. Attached by reference — no copy, and the caller is expected to use
        # them and drop the dict.
        _keep = {"baseband": baseband, "audio": inst, "audio_fs": audio_fs}
    out = {"deviation_hz": deviation, "freq_error_hz": freq_error,
           "analyzed_s": round(len(baseband) / audio_fs, 3),
           "ctcss_hz": None, "ctcss_conf": 0.0, "ctcss_dev_hz": 0.0,
           "dcs_code": None, "dcs_polarity": None, "dcs_errors": None,
           "dcs_suspected": False, "tone_checked": False}
    if keep_signals:
        out["signals"] = _keep

    if dwell < MIN_TONE_SECONDS:
        return out
    out["tone_checked"] = True

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
    # tone_dev = 4.0 * mag approximates the Hann coherent gain, so capture
    # overshoots 1.0 by up to 9e-4 on a clean strong tone — 6 of the 54 standard
    # tones do it. This value lands in events.confidence, which is CHECK
    # constrained to [0,1]. Unclamped, the cleanest possible signal is the one
    # that throws on INSERT.
    capture = min(capture, 1.0)

    is_tone = capture >= 0.50 and tone_dev >= 100.0
    dcs_suspected = (not is_tone) and capture < 0.30 and band_dev >= 150.0

    out.update(
        ctcss_hz=float(CTCSS_TONES[best]) if is_tone else None,
        ctcss_conf=float(capture),
        ctcss_dev_hz=tone_dev,
        dcs_suspected=bool(dcs_suspected),
    )
    if is_tone:
        return out

    # Only now attempt a codeword, and only for signals the capture ratio has
    # already ruled out as CTCSS. The order is not an optimisation.
    #
    # DCS is decoded by slicing the subaudible waveform at 134.4 bps and looking
    # for a 23-bit word that repeats. A pure CTCSS tone sliced at 134.4 bps also
    # produces a repeating pattern — it is periodic, so every repeat agrees with
    # every other, which is precisely the evidence decode_dcs treats as proof.
    # Golay then corrects that pattern to some codeword. Run before this test,
    # the decoder confidently reported tone 110.9 Hz as DCS 243 and tone
    # 254.1 Hz as DCS 031, and dropped two tones off the 54 it used to identify.
    #
    # The capture ratio separates the two by a factor of twenty — a real tone
    # measures ~0.99, DCS never exceeded 0.03 in testing — so it goes first and
    # DCS only ever sees signals it has already cleared.
    if dcs_suspected and dwell >= MIN_DCS_SECONDS:
        got = decode_dcs(tone_sig, tone_fs)
        if got:
            code, polarity, nerr, _agree = got
            # Stored as the three octal DIGITS read as a decimal integer — 155
            # for code 155, not its arithmetic value 109. That is what the
            # schema's views print with %03d, what the fixtures already use, and
            # what you dial into a radio. dcs.py works in the arithmetic value,
            # so this is the one place the two representations meet.
            out.update(dcs_code=int(code), dcs_polarity=polarity,
                       dcs_errors=nerr)
    return out


# ---------------------------------------------------------------------------
# Spectrum diagnostics
#
# Everything Phase 1 does, it does through --spectrum. This block is the whole
# bench procedure: the reference level the gain is set against, the peak list
# the channel sweep is read from, and the frequency estimate ppm is measured
# with. None of it goes through the Radio wrapper, so --simulate cannot reach
# it and it went unrun until 2026-08-25.
# ---------------------------------------------------------------------------

def device_args(driver, serial=None):
    """SoapySDR device arguments, as markup rather than a dict.

    `SoapySDR.Device({"driver": "airspy"})` raises `make() no match` on the
    0.8.0 Python bindings Ubuntu 26.04 ships: a plain dict is not converted to
    the Kwargs type the binding wants, and the resulting error is
    indistinguishable from no radio being present. Both hardware call sites
    built a dict, so the deck could not open a radio at all.

    Invisible until the first Airspy was plugged in on 2026-08-26, because
    --simulate substitutes SimulatedRadio for the entire Device call and never
    reaches this line. The string form works on every version, as does the
    SoapySDRKwargs that `Device.enumerate()` hands back.

    Serial matching is case-insensitive and tolerates a leading `0x`, verified
    against hardware — `airspy_info` prints `0x637862DC2E4C6DD7` while SoapySDR
    reports `637862dc2e4c6dd7`, and all three spellings select the device. A
    serial that matches nothing is refused rather than silently opening
    whatever is attached, which is what makes "address by serial, never by
    index" hold with two radios present.
    """
    spec = f"driver={driver}"
    if serial:
        spec += f",serial={serial}"
    return spec


def spectrum_floor(chan_arr, width=FLOOR_MEDIAN_CHANNELS):
    """Noise floor per channel: median over time, then median across frequency.

    A single scalar for the whole span was enough while the receiver was
    ADC-noise-limited, because the converter's noise is flat and so the floor
    genuinely was one number. Raise the gain above the knee and the analog
    passband appears — rolled off at both edges, with a raised shoulder near the
    top — and a scalar floor then attributes that shape to signals. On the first
    real capture at gain 42 it manufactured a cluster of +6 dB "channels" at
    470.29-470.39 that were nothing but the shoulder, in the same list Gate 1
    asks the operator to account for entry by entry.

    Two medians, each rejecting a different thing:

      over time       an intermittent signal occupies a minority of frames, so
                      it does not move a channel's median however strong it is.
      across frequency a signal that never stops survives the first median --
                      that is the case the old band-wide reference existed to
                      protect, a repeater idling or a trunking control channel
                      becoming its own background. It cannot survive the second,
                      because it is narrow and its neighbours are not.

    What passes both is what varies slowly with frequency and is always there,
    which is the definition wanted.
    """
    from scipy.ndimage import median_filter
    per_channel = np.median(chan_arr, axis=0)
    return median_filter(per_channel, size=width, mode="nearest")


def tune_request_hz(center_hz, ppm):
    """What to ask the driver for, so the LO lands on `center_hz` in true Hz.

    `setFrequencyCorrection` is not usable here. SoapyAirspy reports
    `hasFrequencyCorrection() == False`, and setting it neither takes effect nor
    raises — the value reads back as 0.0 and the tuning does not move. Both call
    sites wrapped it in a try/except, so even an exception would have been
    swallowed; measured on the bench, `--ppm +0.64` and `--ppm -0.64` produced
    byte-identical output. The one number Phase 1 exists to produce was being
    accepted, recorded into the run, and silently discarded.

    So the correction is applied here instead, to the request, which every
    driver honours: the Airspy accepts single-Hz tuning steps. Doing it in
    software rather than per-driver also means the behaviour does not change
    under a radio that *does* implement the call — which would otherwise
    double-correct.

    `ppm` is the receiver's clock error, positive when signals read LOW by that
    much, which is the sense every measurement in `docs/phase_log.md` records.
    """
    return center_hz / (1.0 + ppm * 1e-6)


def true_center_hz(requested_hz, ppm):
    """Where the LO actually landed, in true Hz, for a request the driver took.

    The inverse of `tune_request_hz`, and it must be used for the frequency
    axis: reading `getFrequency()` back and using it directly would re-apply the
    very offset the request just removed. Going through the readback rather than
    assuming the request was honoured keeps this correct if a driver quantises
    the tune.
    """
    return requested_hz * (1.0 + ppm * 1e-6)


def compression_verdict(levels, min_rise_db=COMPRESSION_MIN_RISE_DB,
                        ratio=COMPRESSION_RATIO):
    """Is the front end still linear? `levels` is three dB readings, low gain first.

    A linear receiver raises its noise floor by the same amount for each equal
    step of gain. When the front end starts compressing, the top step delivers
    less than the one below it. Comparing the two increments rather than either
    one against an expectation makes this self-calibrating: it needs no idea of
    what a gain unit is worth, which is essential because on this Airspy three
    units of "dB" move the floor about ten.

    Measured on hardware, 2026-08-26, bare antenna in Boston:

        146 MHz   +10.4, +10.2, +6.2, +2.5   compressing above gain 39
        466 MHz   +10.0, +10.0, +10.0        linear throughout

    Returns 'linear', 'compressed', or 'inconclusive'. The last is not a
    failure: below the ADC knee neither step moves the floor, so there is
    nothing to compare and saying so is the honest answer.

    This is the one overload mode `OverloadMonitor` cannot see. Compression
    happens well before samples reach full scale, so clipping frames read zero
    right through it, and desense looks for the floor going UP together — while
    compression makes it fail to rise. Every indicator stays clean while the
    numbers go wrong.
    """
    lo, mid, hi = (float(x) for x in levels)
    first, second = mid - lo, hi - mid
    if first < min_rise_db:
        return "inconclusive"
    return "compressed" if second < ratio * first else "linear"


def find_peaks(power_db, floor_db, freqs_hz, top=25, min_snr=6.0):
    """Strongest channels above the background, as (freq_hz, snr_db) pairs.

    Only a local maximum is reported, for the reason EventDetector.step gives
    at length: one FM transmission at 2.5-5 kHz deviation occupies about 11 kHz
    against a 6.25 kHz grid, so every carrier lights up its own channel and
    both neighbours. Measured on a simulated seven-channel FRS sweep, the
    ungated list printed twenty-one entries for seven keyups — and Gate 1 asks
    the operator to account for every entry in it. The skirts also displace
    real but weaker signals out of the `top` cap, which is backwards: the
    entries being crowded out are the ones worth looking at.

    +/-1 channel is deliberate and matches the detector. It covers the skirts
    without merging anything 12.5 kHz apart, which is the closest two real
    channels ever get.
    """
    snr = power_db - floor_db
    padded = np.concatenate(([-np.inf], snr, [-np.inf]))
    local_max = (snr >= padded[:-2]) & (snr >= padded[2:])
    idx = np.flatnonzero((snr >= min_snr) & local_max)
    if len(idx) == 0:
        return []
    idx = idx[np.argsort(snr[idx])[::-1]][:top]
    return [(float(freqs_hz[i]), float(snr[i])) for i in idx]


def refine_peak_hz(spec_db, base_hz, bin_hz, target_hz, search_hz=CHANNEL_HZ):
    """Sub-bin frequency of the strongest FFT bin near `target_hz`, or None.

    The peak list names channels, and a channel is a 6.25 kHz slot. That is the
    right answer to "which channel was that" and useless for "what is my clock
    error": Phase 1 measures ppm by reading where a known carrier lands and
    expects to resolve a few hundred Hz, but on the grid every error under
    +/-3125 Hz reports as exactly zero. 466.000 MHz sits dead on a slot
    boundary, so the measurement as documented always returned 0.0 ppm.

    Parabolic interpolation through the peak bin and its two neighbours, in dB.
    A Hann main lobe is close to parabolic in dB near its apex, which is what
    makes three points enough; bins are 2441 Hz at 10 MSPS and this resolves a
    fraction of one. Accuracy against known offsets is measured in
    tests/test_spectrum.py rather than asserted here.

    Meaningful for a carrier. A voice-modulated FM signal has no single apex to
    find, so the caller must present this as an estimate, not a measurement.
    """
    n = len(spec_db)
    # Bins within search_hz of the target, clamped so the parabola always has
    # both neighbours to sit on.
    i_lo = max(1, int(np.ceil((target_hz - search_hz - base_hz) / bin_hz)))
    i_hi = min(n - 2, int(np.floor((target_hz + search_hz - base_hz) / bin_hz)))
    if i_hi < i_lo:
        return None
    window = spec_db[i_lo:i_hi + 1]
    idx = i_lo + int(np.argmax(window))
    a, b, c = spec_db[idx - 1], spec_db[idx], spec_db[idx + 1]
    denom = a - 2.0 * b + c
    if denom >= 0:
        return None                      # no interior maximum to interpolate
    delta = 0.5 * (a - c) / denom
    if abs(delta) > 1.0:
        return None                      # apex outside the bracket; distrust it
    return float(base_hz + (idx + delta) * bin_hz)


# Rows of waterfall retained for the PNG. The image is about 600 px tall, so
# more rows than this cannot be seen; keeping every frame instead cost 3.1 GB of
# resident memory on a 60 s capture and would not have survived the 300 s one
# step 13 asks for.
WATERFALL_ROWS = 1500


def render_spectrum(peak_db, avg_db, waterfall, center, rate, path, title,
                    floor_db=None, floor_freqs_hz=None):
    """Write an averaged spectrum and waterfall to a PNG. No display needed.

    Takes the peak and average traces already accumulated rather than a stack of
    every frame: they are running statistics and never needed the history, and
    the history is what made this the memory ceiling of the whole program.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — skipping PNG "
              "(sudo apt install python3-matplotlib)", file=sys.stderr)
        return False

    arr = np.asarray(waterfall)
    mhz_lo = (center - rate / 2) / 1e6
    mhz_hi = (center + rate / 2) / 1e6

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True,
        gridspec_kw={"height_ratios": [1, 2]})

    x = np.linspace(mhz_lo, mhz_hi, len(peak_db))
    ax1.plot(x, peak_db, lw=0.6, alpha=0.5, label="peak hold")
    ax1.plot(x, avg_db, lw=0.8, label="average")
    # The floor every peak is measured against. Drawn because Gate 1 asks the
    # operator to account for each entry in the peak list, and half of that job
    # is seeing what the entry was judged against — the passband is not flat and
    # eyeballing a single number against a sloped floor is how the roll-off got
    # read as signal in the first place.
    if floor_db is not None and floor_freqs_hz is not None:
        ax1.plot(np.asarray(floor_freqs_hz) / 1e6, floor_db, lw=0.9,
                 color="crimson", alpha=0.8, label="noise floor")
    ax1.set_ylabel("dB")
    ax1.legend(loc="upper right", fontsize=8)
    ax1.grid(alpha=0.3)
    ax1.set_title(title)

    ax2.imshow(arr, aspect="auto", origin="lower", cmap="viridis",
               extent=[mhz_lo, mhz_hi, 0, arr.shape[0]],
               vmin=np.percentile(arr, 5), vmax=np.percentile(arr, 99.5))
    ax2.set_xlabel("MHz")
    ax2.set_ylabel("waterfall row")

    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def spectrum_capture(args):
    """Look at a band without a monitor. Writes a PNG and prints the peaks."""
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

    sdr = SoapySDR.Device(device_args(args.driver, args.serial))
    sdr.setSampleRate(SOAPY_SDR_RX, 0, args.rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, tune_request_hz(args.freq, args.ppm))
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    sdr.setGain(SOAPY_SDR_RX, 0, args.gain)

    rate = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    center = true_center_hz(sdr.getFrequency(SOAPY_SDR_RX, 0), args.ppm)
    fs = frame_size(rate)
    grid = ChannelGrid(center, rate)

    # Run until the requested number of SAMPLES has arrived, not a frame count.
    # readStream hands back whatever the driver's transfer size is and ignores
    # the count asked for: this Airspy returns 65536 every time against the
    # 131072 requested. Counting each return as one full frame therefore ran
    # --spectrum-seconds at exactly HALF the requested duration -- a "two
    # minute" sweep closed after 60 s of signal, which is how a seven-channel
    # bench sweep kept losing its last channels. Found on the bench 2026-08-26
    # because the operator noticed the window felt short; nothing in the output
    # said so, because the summary line printed the number that had been asked
    # for rather than the one that arrived.
    want_samples = max(20 * fs, int(round(args.spectrum_seconds * rate)))
    print(f"capturing {want_samples/rate:.0f} s at {center/1e6:.4f} MHz, "
          f"{rate/1e6:.1f} MSPS...")

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(stream)
    chunk = np.empty(fs, np.complex64)
    periodogram = Periodogram(rate)

    # The peak and average traces are running statistics, so they are
    # accumulated rather than stored. Only the waterfall wants history, and it
    # wants at most a screenful: rows are kept on a stride that doubles whenever
    # the buffer fills, so memory is bounded no matter how long the capture runs.
    spec_max = None
    spec_sum = None
    n_spec = 0
    waterfall = []
    wf_stride = 1
    wf_accum = None
    wf_count = 0
    chan_db = []
    overflows = 0
    clipped = 0
    got_samples = 0
    try:
        while got_samples < want_samples:
            st = sdr.readStream(stream, [chunk], fs, timeoutUs=2_000_000)
            if st.ret <= 0:
                if st.ret == SoapySDR.SOAPY_SDR_OVERFLOW:
                    overflows += 1
                continue
            s = chunk[:st.ret]
            got_samples += st.ret
            if np.mean(np.abs(s) > 0.9) > 1e-4:
                clipped += 1
            psd = periodogram(s)
            if psd is None:
                continue
            db = to_db(psd).astype(np.float32)
            if spec_max is None:
                spec_max = db.copy()
                spec_sum = db.astype(np.float64)
            else:
                np.maximum(spec_max, db, out=spec_max)
                spec_sum += db
            n_spec += 1
            # Max-pool each group rather than keeping one frame out of every
            # `wf_stride`. Sampling would alias away exactly what the waterfall
            # is read for: a keyup shorter than the stride falls between the
            # retained rows and vanishes from the picture, while still being
            # counted everywhere else. Pooling cannot lose a burst, only widen
            # one. Same reason the rows are merged pairwise, not thinned, when
            # the buffer fills.
            wf_accum = db if wf_accum is None else np.maximum(wf_accum, db)
            wf_count += 1
            if wf_count == wf_stride:
                waterfall.append(wf_accum)
                wf_accum = None
                wf_count = 0
                if len(waterfall) >= 2 * WATERFALL_ROWS:
                    waterfall = [np.maximum(a, b) for a, b in
                                 zip(waterfall[::2], waterfall[1::2])]
                    wf_stride *= 2
            # Kept in full: this is the narrow one (channels, not FFT bins) and
            # the band-wide floor is a median over time, which needs the history.
            chan_db.append(to_db(grid.power(psd)).astype(np.float32))
        if wf_accum is not None:
            waterfall.append(wf_accum)
    finally:
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)

    if not n_spec:
        print("no samples captured", file=sys.stderr)
        return

    chan_arr = np.asarray(chan_db)

    floor_curve = spectrum_floor(chan_arr)
    floor_db = float(np.median(floor_curve))
    peaks = find_peaks(chan_arr.max(axis=0), floor_curve, grid.freqs_hz)

    # Full-resolution peak hold, for the sub-bin frequency estimate. The channel
    # grid is what names a signal; this is what measures one.
    spec_peak = spec_max
    spec_avg = spec_sum / n_spec
    bin_hz = rate / NFFT
    base_hz = center - rate / 2.0

    actual_s = got_samples / rate
    title = (f"{center/1e6:.3f} MHz  {rate/1e6:.1f} MSPS  gain {args.gain}"
             f"   {actual_s:.0f} s   {time.strftime('%Y-%m-%d %H:%M')}")
    if render_spectrum(spec_peak, spec_avg, waterfall, center, rate,
                       args.spectrum, title,
                       floor_db=floor_curve, floor_freqs_hz=grid.freqs_hz):
        print(f"wrote {args.spectrum}")

    print(f"\n  span {(center-rate/2)/1e6:.3f} - {(center+rate/2)/1e6:.3f} MHz")
    # Steps 6 and 11 of the bench procedure both turn on this number: the gain
    # is set by raising it until the antenna lifts this level 8-10 dB over the
    # dummy load. The peaks below are quoted RELATIVE to it, so without it
    # printed the absolute level appeared nowhere in the output at all.
    print(f"  band reference level {floor_db:7.1f} dB   "
          f"(median of the floor curve over {grid.n} channels; "
          f"span {floor_curve.min():.1f} to {floor_curve.max():.1f})")
    print(f"  overflows {overflows}   clipping frames {clipped}")
    if clipped:
        print("  ** CLIPPING — add attenuation or reduce gain")

    print(f"\n  strongest channels (peak hold over {actual_s:.0f} s "
          f"of signal, {n_spec} frames):")
    if not peaks:
        print("    nothing above the background")
    else:
        print("     channel          SNR     measured        offset from channel")
    for f_hz, snr in peaks:
        got = refine_peak_hz(spec_peak, base_hz, bin_hz, f_hz)
        if got is None:
            print(f"    {f_hz/1e6:11.4f} MHz  {snr:+6.1f} dB    "
                  f"{'--':>13}")
            continue
        err = got - f_hz
        ppm = err / (f_hz / 1e6)
        print(f"    {f_hz/1e6:11.4f} MHz  {snr:+6.1f} dB   "
              f"{got/1e6:11.6f} MHz  {err:+8.0f} Hz  ({ppm:+6.2f} ppm)")
    if peaks:
        # A clock error is common to every signal in the span; a transmitter off
        # frequency is one row. Reading the column rather than any single number
        # is what separates them, and it is also step 8's evenly-spaced test,
        # which on the grid alone could never fail.
        print("\n    'measured' is an interpolated estimate, sharp for a carrier "
              "and soft for\n    voice. A consistent ppm down the column is the "
              "receiver's clock; one row\n    adrift is that transmitter.")
    print()


class CaptureStore:
    """Writes per-event audio and channel IQ to disk, under a budget.

    `events.audio_path` and `events.iq_path` have existed since v2 and nothing
    has ever written them. That is the one gap in this project that cannot be
    fixed after the fact: a festival happens once, every threshold in the repo is
    a guess, and without recordings the deployment produces no material to
    correct those guesses against. Everything else here can be re-run.

    Two levels, because they cost very different amounts:

      audio  the demodulated channel at 8 kHz, 16-bit. About 16 kB per second of
             traffic. You can listen to it, which is what settles most "what on
             earth was that" questions.
      iq     the complex channel at ~24 kHz, which is exactly what the analyser
             saw. About 190 kB per second. This is the one that lets a better
             tone or deviation algorithm be re-run later against real signals.

    Both are capped, and the cap is enforced before the write rather than after:
    a deck that fills its disk mid-festival stops logging events entirely, which
    is a far worse failure than losing recordings.
    """

    AUDIO_FS = 8000
    FULL_SCALE_HZ = 5000.0      # deviation mapped to int16 full scale
    MIN_FREE_MB = 500

    def __init__(self, root, run_id, max_mb=2000.0, keep_iq=False):
        self.root = pathlib.Path(root) / f"run{run_id}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = float(max_mb) * 1e6
        self.keep_iq = keep_iq
        self.written = 0
        self.count = 0
        self.stopped = None

    def _room_for(self, nbytes):
        if self.stopped:
            return False
        if self.written + nbytes > self.max_bytes:
            self.stopped = f"retention budget reached ({self.max_bytes/1e6:.4g} MB)"
        elif shutil.disk_usage(self.root).free < self.MIN_FREE_MB * 1e6:
            self.stopped = f"less than {self.MIN_FREE_MB} MB free on disk"
        if self.stopped:
            print(f"  ** capture retention stopped: {self.stopped}. Detection and "
                  f"logging continue.", file=sys.stderr)
            return False
        return True

    def write(self, event_id, signals):
        """Returns (audio_path, iq_path), either of which may be None."""
        audio = np.asarray(signals["audio"], dtype=np.float64)
        fs = signals["audio_fs"]
        decim = max(1, int(round(fs / self.AUDIO_FS)))
        est = len(audio) // decim * 2 + (len(signals["baseband"]) * 8
                                         if self.keep_iq else 0)
        if not self._room_for(est):
            return None, None

        # Band-limit before decimating. Without this, everything between 4 kHz
        # and 12 kHz folds down into the voice band and the recording is
        # unintelligible in a way that sounds like a receiver fault.
        taps = firwin(63, min(0.9 / decim, 0.99))
        band = lfilter(taps, 1.0, audio)[len(taps):]
        pcm = np.clip(band[::decim] / self.FULL_SCALE_HZ, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2")

        apath = self.root / f"{event_id:08d}.wav"
        with wave.open(str(apath), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(round(fs / decim)))
            w.writeframes(pcm.tobytes())
        self.written += apath.stat().st_size
        self.count += 1

        ipath = None
        if self.keep_iq:
            ipath = self.root / f"{event_id:08d}.npz"
            np.savez(ipath, baseband=np.asarray(signals["baseband"], np.complex64),
                     fs=np.float64(fs))
            self.written += ipath.stat().st_size
        return str(apath), (str(ipath) if ipath else None)


class EventLog:
    """The detector's two-phase write, expressed in schema terms.

    A transmission is inserted the moment it is detected, updated when analysis
    finishes, and closed when it ends. An unattended deck loses power
    mid-transmission, and those in-flight rows — `t_end` and `duration_s` still
    NULL — are the ones worth keeping. Migration 5 made both nullable for this.

    Every field mapping between `analyze_analog()` and the schema lives here and
    nowhere else, so the tests can drive the exact code path the field deck uses
    against a temporary database with no radio attached. That is the only way to
    test this wiring before the hardware arrives — see tests/test_endtoend.py,
    which runs the real capture loop against a synthetic radio.
    """

    def __init__(self, conn, run_id, receiver_id):
        self.conn = conn
        self.run_id = run_id
        self.receiver_id = receiver_id
        self.open_rows = {}
        # Set by the capture loop on every retune. Recording it on the event is
        # what makes "which band was this heard on" a fact rather than something
        # reconstructed from timestamps afterwards.
        self.window_id = None

    def start(self, ch, t_start, freq_hz, overload=False):
        """Detected. Everything analysis will fill in is still unknown."""
        row = db.log_event(
            self.conn, self.run_id, self.receiver_id,
            t_start=t_start,
            freq_hz=int(freq_hz),
            modulation="fm",
            # Not "none" — nothing has looked for a tone yet, and the two are
            # different claims. Transmissions shorter than ANALYZE_SECONDS stay
            # at "unknown" forever, which is what puts them on tier 0.
            tone_state="unknown",
            overload=int(bool(overload)),
            window_id=self.window_id,
        )
        self.open_rows[ch] = row
        return row

    def analysed(self, ch, result, freq_hz):
        """Fold one analyze_analog() result into the open row."""
        row = self.open_rows.get(ch)
        if row is None:
            return None
        return self.apply(row, result, freq_hz)

    def mark_harmonic(self, row, parent_row, n):
        """Record that this row is a receiver product of `parent_row`.

        Left otherwise as the detector wrote it: it keeps its frequency, its
        timing and its SNR, because those are all genuinely measured. What it
        does not get is analysis, which would copy the parent's tone onto it and
        make it indistinguishable from real traffic.
        """
        self.conn.execute(
            "UPDATE events SET harmonic_of = ?, harmonic_n = ? WHERE id = ?",
            (int(parent_row), int(n), int(row)))

    def apply(self, row, result, freq_hz):
        """Fold a result into a row by id, whether or not its event is open.

        Analysis runs on another thread now, so a short transmission can end —
        and be removed from `open_rows` — before its own analysis comes back.
        Keying the update on the row id rather than the channel is what stops
        that result being silently dropped.
        """

        # Four distinct claims, and the difference between the last two is the
        # whole point of tiering the analysis by dwell:
        #
        #   'dcs' with a code      decoded; programmable
        #   'dcs' without a code   something subaudible, demonstrably not CTCSS,
        #                          but no codeword came out. Caps at tier 2.
        #   'none'                 checked, and there is genuinely no tone
        #   'unknown'              NOT checked — too short to look. A 0.3 s
        #                          transmission lands here, and must not be
        #                          confused with a channel confirmed clean.
        if result["dcs_code"] is not None:
            tone_state = "dcs"
        elif result["ctcss_hz"] is not None:
            tone_state = "ctcss"
        elif result["dcs_suspected"]:
            tone_state = "dcs"
        elif result["tone_checked"]:
            tone_state = "none"
        else:
            tone_state = "unknown"

        # freq_error_hz has no column of its own. freq_hz is snapped to the
        # 6.25 kHz grid; freq_raw_hz holds the centre as measured, so the error
        # recovers exactly as freq_raw_hz - freq_hz. This is also the per-event
        # evidence Phase 1 checks ppm calibration against.
        self.conn.execute(
            """UPDATE events SET freq_raw_hz = ?, deviation_hz = ?, ctcss_hz = ?,
                                 ctcss_dev_hz = ?, confidence = ?, tone_state = ?,
                                 dcs_code = ?, dcs_polarity = ?, dcs_errors = ?,
                                 analyzed_s = ?
               WHERE id = ?""",
            (int(round(freq_hz + result["freq_error_hz"])),
             result["deviation_hz"],
             result["ctcss_hz"],
             result["ctcss_dev_hz"],
             # Tone capture ratio: how much of the subaudible band sits in the
             # winning tone. Clamped to 1.0 in analyze_analog because the CHECK
             # on this column is [0,1] and the Hann-gain approximation overshoots.
             result["ctcss_conf"],
             tone_state,
             result["dcs_code"],
             result["dcs_polarity"],
             result["dcs_errors"],
             result["analyzed_s"],
             row))
        return row

    def close(self, ch, t_end, duration_s, peak_snr_db):
        """Carrier dropped. The row stops being in-flight."""
        row = self.open_rows.pop(ch, None)
        if row is None:
            return None
        self.conn.execute(
            "UPDATE events SET t_end = ?, duration_s = ?, snr_db = ? WHERE id = ?",
            (t_end, duration_s, float(peak_snr_db), row))
        return row

    def attach_capture(self, row, audio_path, iq_path):
        self.conn.execute(
            "UPDATE events SET audio_path = ?, iq_path = ? WHERE id = ?",
            (audio_path, iq_path, row))

    def forget(self, ch):
        self.open_rows.pop(ch, None)


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
    """Will this machine keep up? Sizing and speed, with no hardware attached.

    Correctness is not checked here any more. It used to be — 200 lines sweeping
    tones, deviation, event timing, the DCS table, the analysis trim and a
    database round trip — and every one of those now has a test module that
    checks it harder and names the failure when it breaks. Two copies of the same
    assertions is one copy that gets updated. Run `bash tools/run-tests.sh` for
    correctness; this answers the different question of whether the deck can
    process two radios in real time, which no unit test can answer because the
    answer is a property of the machine it is running on.
    """
    print(f"\n=== self test @ {rate/1e6:.1f} MSPS ===\n")

    fs = frame_size(rate)
    grid = ChannelGrid(466.0e6, rate)
    ring_s = ANALYZE_SECONDS + PRETRIGGER_SECONDS + RING_SLACK_SECONDS
    print(f"  frame size        {fs} samples = {fs/rate*1000:.1f} ms"
          f"  ({rate/fs:.0f} frames/sec)")
    print(f"  channels watched  {grid.n}")
    print(f"  ring buffer       {ring_s*rate*8/1e6:.0f} MB per receiver\n")

    print("  speed on this machine")

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
    periodogram = Periodogram(rate)
    detect_ms = bench(lambda: grid.power(periodogram(chunk)), 20)

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

    # The deck runs two receivers. The verdict is whether that fits, which is
    # the one thing this can decide and a unit test cannot.
    ok = steady * 2 < 100.0
    if ok:
        print(f"\n  verdict: PASS — two receivers need {steady*2:.1f}% of one core")
    else:
        print(f"\n  verdict: FAIL — two receivers need {steady*2:.1f}% of one "
              f"core and will not keep up on this machine")
    print("  correctness is `bash tools/run-tests.sh`\n")
    return ok


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def load_receiver_config(profile_path, receiver_id):
    """The receiver's section of the profile, as the code will actually use it.

    The profile was already being snapshotted into the run row while every
    setting came from the command line, so a run recorded a configuration it had
    not followed — worse than recording none, because the snapshot reads as
    evidence. This is what makes the snapshot true.
    """
    import yaml
    with open(profile_path) as fh:
        prof = yaml.safe_load(fh) or {}
    rx = (prof.get("receivers") or {}).get(receiver_id)
    if rx is None:
        raise SystemExit(
            f"{profile_path} has no receivers.{receiver_id} section. "
            f"Known: {sorted((prof.get('receivers') or {}))}")

    mode = rx.get("mode", "parked")
    if mode == "rotating":
        # dwell_seconds is per window, falling back to the receiver's. Equal
        # dwell is wrong whenever the windows are not equally interesting: the
        # uhf receiver covers the whole FRS/GMRS band on one window and a ham
        # segment on the other, and splitting its time evenly would halve
        # coverage of the band the survey is actually for.
        windows = [dict(center_hz=int(w["center_hz"]), label=w.get("label"),
                        dwell_s=float(w["dwell_seconds"])
                        if w.get("dwell_seconds") else None)
                   for w in (rx.get("windows") or [])]
        if not windows:
            raise SystemExit(f"receiver {receiver_id} is rotating with no windows")
    else:
        if rx.get("center_hz") is None:
            raise SystemExit(f"receiver {receiver_id} is parked with no center_hz")
        windows = [dict(center_hz=int(rx["center_hz"]), label=rx.get("label"),
                        dwell_s=None)]

    det = (prof.get("detection") or {})
    return {
        "mode": mode,
        "windows": windows,
        "dwell_seconds": float(rx.get("dwell_seconds") or 0) or None,
        "sample_rate": float(rx.get("sample_rate") or 10e6),
        "gain": float(rx.get("gain") if rx.get("gain") is not None else 12.0),
        "ppm": float(rx.get("ppm") or 0.0),
        "serial": rx.get("serial"),
        "on_db": float(det.get("on_db", 10.0)),
        "off_db": float(det.get("off_db", 6.0)),
        # These two were in the profile from the beginning and nothing read
        # them; EventTracker took its own defaults, which happened to match, so
        # editing the profile changed nothing and said nothing. Every setting
        # the run row claims has to be one the run actually used.
        "min_duration_s": float(det.get("min_duration_s", 0.12)),
        "hang_s": float(det.get("hang_s", 0.30)),
        # Not software-controlled — recorded because six months from now "why is
        # this run 10 dB down on that one" should be answerable from the row
        # rather than from memory. run_receivers has had columns for both since
        # v2 and no real run has ever filled them in.
        "attenuator_db": rx.get("attenuator_db"),
        "antenna": rx.get("antenna"),
        # Antenna-removed reference for check_antenna(). Optional: a receiver
        # that has never been measured with a terminator simply skips the check.
        "dummy_floor_dbfs": rx.get("dummy_floor_dbfs"),
        "dummy_floor_gain": rx.get("dummy_floor_gain"),
    }


def resolve_settings(args):
    """The profile, with command-line overrides applied. What the run will use.

    The profile is the configuration and the command line overrides it only
    where something was actually typed. Keeping the two in one place matters
    because `runs.profile_yaml` snapshots the profile verbatim: a run that
    records a configuration it did not follow is worse than one that records
    none, because the snapshot reads as evidence.
    """
    cfg = load_receiver_config(args.profile, args.receiver_id)

    def override(name, key):
        given = getattr(args, name)
        return cfg[key] if given is None else given

    cfg["rate"] = override("rate", "sample_rate")
    cfg["gain"] = override("gain", "gain")
    cfg["ppm"] = override("ppm", "ppm")
    cfg["on_db"] = override("on_db", "on_db")
    cfg["off_db"] = override("off_db", "off_db")
    cfg["dwell_s"] = override("dwell_seconds", "dwell_seconds")
    cfg["serial_want"] = args.serial or cfg["serial"]

    if args.freq is not None:                       # explicit override parks it
        cfg["windows"] = [dict(center_hz=int(args.freq), label="--freq",
                               dwell_s=None)]
    # --dwell-seconds is an operator override and beats per-window values, so
    # `--simulate 8 --dwell-seconds 6` still exercises rotation quickly.
    if cfg["dwell_s"] and args.dwell_seconds is not None:
        for w in cfg["windows"]:
            w["dwell_s"] = None
    if len(cfg["windows"]) > 1:
        missing = [w for w in cfg["windows"]
                   if not w["dwell_s"] and not cfg["dwell_s"]]
        if missing:
            raise SystemExit(
                f"receiver {args.receiver_id} has {len(cfg['windows'])} windows "
                f"but no dwell_seconds on "
                f"{', '.join(w['label'] or str(w['center_hz']) for w in missing)} "
                f"and none on the receiver — it would never rotate")
    return cfg


class Radio:
    """The SoapySDR surface this program uses, real or simulated.

    Wrapping it keeps `SOAPY_SDR_RX, 0` out of the capture loop, and gives
    --simulate one place to substitute itself rather than a branch at every
    call. It is also the definition of what `simradio` has to implement.
    """

    def __init__(self, args, settings):
        if args.simulate:
            import simradio
            self.api = simradio.SimulatedRadio(
                simradio.festival_scenario(), rate=settings["rate"],
                center_hz=settings["windows"][0]["center_hz"],
                duration_s=args.simulate,
                serial=f"SIM-{args.receiver_id.upper()}", announce=True)
            self.dev = self.api
            print(f"SIMULATED radio — {args.simulate:.0f} s of synthetic signal "
                  f"per window, no hardware involved")
        else:
            import SoapySDR
            self.api = SoapySDR
            self.dev = SoapySDR.Device(
                device_args(args.driver, settings["serial_want"]))

        # Held on the radio because every tune has to apply it, not just the
        # first one: a rotating receiver retunes on every dwell.
        self.ppm = float(settings.get("ppm") or 0.0)
        self.rate_hint = float(settings.get("rate") or settings.get("sample_rate")
                               or 10e6)
        self.RX = self.api.SOAPY_SDR_RX
        self.OVERFLOW = self.api.SOAPY_SDR_OVERFLOW
        self._cf32 = self.api.SOAPY_SDR_CF32
        self.stream = None
        self.mtu = None

    def configure(self, settings):
        """Apply the settings. Returns the rate and centre the device accepted."""
        self.dev.setSampleRate(self.RX, 0, settings["rate"])
        self.dev.setFrequency(self.RX, 0, tune_request_hz(
            settings["windows"][0]["center_hz"], self.ppm))
        try:
            self.dev.setGainMode(self.RX, 0, False)  # AGC off — non-negotiable
        except Exception:
            print("warning: could not disable AGC", file=sys.stderr)
        self.dev.setGain(self.RX, 0, settings["gain"])
        return (self.dev.getSampleRate(self.RX, 0),
                true_center_hz(self.dev.getFrequency(self.RX, 0), self.ppm))

    def serial(self, fallback):
        """The serial read back off the device, not the one that was asked for.

        If the radio was addressed by driver alone this is the only record of
        which of the two it actually was, and `run_receivers.serial` is the
        identity every later row is interpreted against.
        """
        try:
            got = self.dev.getHardwareInfo()["serial"]
        except Exception:
            got = fallback
        if not got:
            print("warning: no serial from device and none given — this run "
                  "cannot prove which radio produced it", file=sys.stderr)
            return "unknown"
        return got

    def tune(self, center_hz):
        self.dev.setFrequency(self.RX, 0, tune_request_hz(center_hz, self.ppm))
        return true_center_hz(self.dev.getFrequency(self.RX, 0), self.ppm)

    def start(self):
        self.stream = self.dev.setupStream(self.RX, self._cf32)
        self.dev.activateStream(self.stream)
        # The driver decides how much a single read can return, and asking for
        # more does not get it: SoapyAirspy reports 65536 and getStreamArgsInfo
        # offers nothing to change it. Recorded here because the frame size has
        # to agree with it — see CaptureLoop.__init__.
        try:
            self.mtu = int(self.dev.getStreamMTU(self.stream))
        except Exception:
            self.mtu = None

    def read(self, buf, n):
        """Samples into `buf`. Returns the count, or <= 0 for a timeout."""
        return self.dev.readStream(self.stream, [buf], n, timeoutUs=2_000_000).ret

    def set_gain(self, gain):
        self.dev.setGain(self.RX, 0, float(gain))

    def linearity(self, grid, periodogram, frame, gain,
                  step=COMPRESSION_GAIN_STEP, seconds=COMPRESSION_SECONDS,
                  max_gain=MAX_GAIN_DB):
        """Check the front end is still linear at `gain`, and restore it.

        Steps the gain and watches whether the noise floor moves by the same
        amount each time. Runs on the open stream, so it costs about
        `3 * seconds` per probe and no retune. Called once per window because
        the answer depends on what is on the air in the band being listened to —
        the same receiver at the same gain measured linear at 466 MHz and
        compressed at 146 MHz minutes apart.

        Probes DOWNWARD first, which is the safe direction: it can only reduce
        what reaches the converter. If that comes back `inconclusive` it tries
        UPWARD, because inconclusive downward has a specific meaning — the two
        reference points were below the ADC knee, where nothing moves the floor
        and there is nothing to compare.

        That case stopped being hypothetical on 2026-08-27. Fitting the measured
        5 dB pad in place of 20 dB moved the working gain to 39, the lowest
        setting still linear, so the downward probe lands on 36 and 33 and both
        are under the knee. Every window would have reported `inconclusive` for
        the whole of an unattended run, which is precisely the reading migration
        9 exists to distinguish from a quiet band.

        Upward is the more direct test for compression anyway — it asks whether
        more gain still buys proportionally more floor — but it is second
        because it briefly raises what the front end sees, and only worth doing
        when the safe probe has already declined to answer.

        Restores the configured gain in a finally, because leaving a survey
        running at two thirds of its intended gain would be a far worse bug than
        the one this exists to catch.
        """
        for gains in ([gain - 2 * step, gain - step, gain],
                      [gain, gain + step, gain + 2 * step]):
            if min(gains) < 0 or max(gains) > max_gain:
                continue
            verdict = self._linearity_probe(grid, periodogram, frame, gain,
                                            gains, seconds)
            if verdict != "inconclusive":
                return verdict
        return "inconclusive"

    def _linearity_probe(self, grid, periodogram, frame, gain, gains, seconds):
        """One three-point sweep, restoring `gain` however it ends."""
        levels = []
        try:
            for g in gains:
                self.set_gain(g)
                # The first read after a gain change still holds samples taken
                # at the old setting; the driver's buffers do not flush.
                self.read(frame, len(frame))
                lvl = []
                need = max(1, int(seconds * self.rate_hint / len(frame)))
                for _ in range(need):
                    n = self.read(frame, len(frame))
                    if n <= 0:
                        continue
                    psd = periodogram(frame[:n])
                    if psd is None:
                        continue
                    lvl.append(float(np.median(to_db(grid.power(psd)))))
                if not lvl:
                    return "inconclusive"
                levels.append(float(np.median(lvl)))
        finally:
            self.set_gain(gain)
        return compression_verdict(levels)

    def stop(self):
        self.dev.deactivateStream(self.stream)
        self.dev.closeStream(self.stream)


class Detector:
    """Detection state for one centre frequency.

    The channel grid, the per-channel state machine and the noise floor are all
    indexed by a grid that means nothing except relative to one centre, so a
    retune invalidates all three together. Keeping them in one object makes that
    one line instead of three that can be forgotten separately.
    """

    def __init__(self, center, rate, frame_seconds, settings):
        self.center = center
        self.grid = ChannelGrid(center, rate)
        self.tracker = EventTracker(
            self.grid.n, frame_seconds,
            on_db=settings["on_db"], off_db=settings["off_db"],
            min_duration=settings["min_duration_s"], hang=settings["hang_s"])
        self.floor = NoiseFloor(self.grid.n)
        self._active = None     # one frame stale by construction, see NoiseFloor

    def step(self, power_db, frame_start_sample):
        """One frame. Returns (started, ended), or None while still warming up."""
        floor_db = self.floor.update(power_db, active=self._active)
        if floor_db is None:
            return None
        snr_db = power_db - floor_db

        # One FM transmission at 2.5-5 kHz deviation occupies roughly 11 kHz and
        # the grid is 6.25 kHz, so a single keyup lights up its own channel and
        # both neighbours. Logged as-is every transmission becomes three events —
        # and because FRS primary and interstitial channels interleave to
        # 12.5 kHz, the two skirts land on legitimate neighbouring channel
        # numbers. One GMRS keyup was reported as traffic on FRS 5 and FRS 6 as
        # well: invented activity on channels nobody touched.
        #
        # Only a local maximum may open an event. +/-1 channel is deliberate: it
        # covers the skirts without merging anything 12.5 kHz apart, which is
        # the closest two real channels get.
        padded = np.concatenate(([-np.inf], snr_db, [-np.inf]))
        local_max = (snr_db >= padded[:-2]) & (snr_db >= padded[2:])

        started, ended = self.tracker.update(snr_db, frame_start_sample,
                                             can_start=local_max)
        self._active = self.tracker.state == EventTracker.ACTIVE
        return started, ended


class AnalysisWorker:
    """Runs analyze_analog off the read thread.

    The reader must never block. One analysis measured 91 ms against a 6.55 ms
    frame, so a single call is fourteen frames' worth of samples arriving with
    nobody collecting them — and the Airspy's USB buffer is 65536 samples with
    no way to deepen it, `getStreamArgsInfo` being empty. That latency, not
    average CPU, is what produced 63 overflows in 45 s while the process sat at
    under half a core.

    Only `analyze_analog` moves. The ring is read on the reader thread and
    `Ring.get` returns a copy, so the worker never touches the buffer; every
    database write stays on the reader thread too. What crosses the boundary is
    an array the reader has finished with and a dict of numbers coming back.

    The job queue is deliberately shallow. Each job at 10 MSPS carries about
    96 MB of IQ, and a deep queue would trade a bounded overflow problem for an
    unbounded memory one. When it is full the analysis is **skipped and
    counted**: the event is still detected, logged and timed, it simply stays at
    tier 0. Dropping an analysis loses one row's detail; dropping samples
    corrupts every measurement in the window.
    """

    def __init__(self, rate, depth=2):
        self.rate = rate
        self.jobs = queue.Queue(maxsize=depth)
        self.results = queue.Queue()
        self.skipped = 0
        self.thread = threading.Thread(target=self._run, name="analysis",
                                       daemon=True)
        self.thread.start()

    def submit(self, ch, row, iq, offset_hz, freq_hz, snr_db, keep_signals):
        try:
            self.jobs.put_nowait((ch, row, iq, offset_hz, freq_hz, snr_db,
                                  keep_signals))
            return True
        except queue.Full:
            self.skipped += 1
            return False

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            ch, row, iq, offset_hz, freq_hz, snr_db, keep = job
            t0 = time.perf_counter()
            try:
                result = analyze_analog(iq, self.rate, offset_hz,
                                        keep_signals=keep)
            except Exception as exc:              # never kill the worker
                print(f"  analysis failed on {freq_hz/1e6:.4f} MHz: {exc}",
                      file=sys.stderr)
                result = None
            ms = (time.perf_counter() - t0) * 1000.0
            self.results.put((ch, row, result, freq_hz, snr_db, ms))

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.results.get_nowait())
            except queue.Empty:
                return out

    def stop(self, timeout=5.0):
        """Let the queued work finish, then retire the thread."""
        try:
            self.jobs.put(None, timeout=timeout)
        except queue.Full:
            return
        self.thread.join(timeout=timeout)


class CaptureLoop:
    """One run of the deck: a radio, a database, and the loop between them.

    Everything that outlives a retune — the device, the database, the retention
    budget, the counters — is on the instance. Everything defined relative to one
    centre is on `self.det` and is replaced wholesale by `window()`. The frame
    body is `_frame()`, which is the part worth reading.
    """

    STAT_SECONDS = 15.0

    def __init__(self, radio, settings, args, conn, run_id, rate, center):
        self.radio = radio
        self.settings = settings
        self.args = args
        self.conn = conn
        self.run_id = run_id
        self.rate = rate

        # frame_size() picks the FFT length the analysis wants; the driver
        # decides what a read actually returns. At 10 MSPS those disagree —
        # frame_size asks for 131072 and SoapyAirspy hands back 65536 — and
        # every frame-counted threshold was computed from the number we do not
        # get. min_duration_s 0.12 became 0.059 and hang_s 0.30 became 0.151,
        # so the two settings that decide what counts as an event were running
        # at half their configured values on 10 MSPS runs and correctly on
        # 2.5 MSPS ones, where 32768 fits inside the MTU. Measured 2026-08-27:
        # 898 of gym.sqlite's 12799 events are shorter than the 0.12 s minimum
        # that was supposedly in force.
        #
        # Clamping here rather than fixing the thresholds keeps one definition
        # of a frame: what a read returns, what the detector counts, and what
        # `fps (target N)` compares against are now the same number.
        self.fs = frame_size(rate)
        mtu = getattr(radio, "mtu", None)
        if mtu:
            self.fs = min(self.fs, mtu)
        self.frame_seconds = self.fs / rate
        self.chunk = np.empty(self.fs, np.complex64)
        self.periodogram = Periodogram(rate)
        self.overload = OverloadMonitor()
        self.worker = AnalysisWorker(rate)
        self.analyses_skipped = 0
        self.harmonics_found = 0

        # The window starts PRETRIGGER_SECONDS before the detector fired, so it
        # has to be that much longer to still contain ANALYZE_SECONDS of signal.
        # Sizing it at ANALYZE_SECONDS flat leaves only 0.6 s of carrier once the
        # trim in analyze_analog has dropped the lead-in — below MIN_TONE_SECONDS,
        # so no transmission of any length ever got its tone identified.
        self.pretrigger = int(PRETRIGGER_SECONDS * rate)
        self.analyze_samples = int(ANALYZE_SECONDS * rate) + self.pretrigger
        self.min_analyze_samples = int(MIN_ANALYZE_SECONDS * rate)
        ring_seconds = ANALYZE_SECONDS + PRETRIGGER_SECONDS + RING_SLACK_SECONDS
        self.ring = Ring(int(ring_seconds * rate))
        self.ring_seconds = ring_seconds

        self.log = EventLog(conn, run_id, args.receiver_id)
        self.store = None
        if args.capture_dir:
            self.store = CaptureStore(args.capture_dir, run_id,
                                      max_mb=args.capture_mb,
                                      keep_iq=args.capture_iq)

        self.det = Detector(center, rate, self.frame_seconds, settings)
        self.pending = {}
        self.window_id = None
        self.window_t0 = time.time()    # replaced per window; defined for finish()
        self.stalled = 0
        self._antenna_checked = True    # armed by open_window, per window

        self.overflows = 0
        self.events_logged = 0
        self.analyses = 0
        self.analyses_total = 0     # _stats() resets self.analyses; this survives
        self.detect_ms = 0.0
        self.analyze_ms = 0.0
        self.stat_frames = 0
        self.session_start = time.time()
        self.last_stat = time.time()

        # SIGINT stops the loop at the next frame boundary rather than killing it
        # mid-transaction: systemd sends SIGINT precisely so the in-flight events
        # and the coverage window get closed. threading.Event is the plainest
        # thing with the right semantics — set from a handler, read from a loop.
        self.running = threading.Event()
        self.running.set()
        signal.signal(signal.SIGINT, lambda *_: self.running.clear())

    # -- the loop ------------------------------------------------------------

    def go(self, windows):
        """Visit every window in turn until stopped. Returns an exit status."""
        stream_failed = False
        win_idx = 0
        try:
            while self.running.is_set():
                w = windows[win_idx % len(windows)]
                dwell = w.get("dwell_s") or self.settings["dwell_s"]
                deadline = (time.time() + dwell if len(windows) > 1 else None)
                self.open_window(w, dwell if deadline is not None else None)
                while self.running.is_set() and (deadline is None
                                                 or time.time() < deadline):
                    if not self.read_and_process():
                        break
                self.close_window()

                if self.stalled >= STALL_FRAMES and not self.args.simulate:
                    print(f"stream delivered nothing for {self.stalled} "
                          f"consecutive reads. Exiting so the supervisor restarts "
                          f"the process and the device re-enumerates — a wedged "
                          f"USB endpoint does not recover in place.",
                          file=sys.stderr)
                    stream_failed = True
                    self.running.clear()

                win_idx += 1
                if self.args.simulate and win_idx >= len(windows):
                    self.running.clear()
        finally:
            self.finish()
        # Non-zero tells the supervisor this was not a clean stop, so a Restart=
        # policy re-enumerates the device instead of treating it as a normal exit.
        return 1 if stream_failed else 0

    def open_window(self, w, dwell_s):
        """Retune, and discard everything that belonged to the old centre.

        Every per-channel index is defined relative to the centre, so the grid,
        the detector state and the ring all have to go — carrying any of it
        across a tune would attribute one band's signal to another band's
        frequency.
        """
        center = self.radio.tune(w["center_hz"])
        self.det = Detector(center, self.rate, self.frame_seconds, self.settings)
        self.pending.clear()
        self.ring.reset()
        self.stalled = 0

        # Anchors the sample clock for this window. ring.reset() has just put the
        # sample counter back to zero, so every event timestamp in this window is
        # window_t0 + samples/rate.
        self.window_t0 = time.time()
        self.window_id = db.open_window(self.conn, self.run_id,
                                        self.args.receiver_id, int(center),
                                        int(self.rate), w["label"])
        self.log.window_id = self.window_id
        self.window_info = {"center_hz": center, "label": w["label"], "opened_at": time.time(),
                            "dwell_s": dwell_s, "linearity": None}
        print(f"\n== {center/1e6:.3f} MHz"
              + (f" ({w['label']})" if w["label"] else "")
              + (f", {dwell_s:.0f} s" if dwell_s else "")
              + " ==")
        self.check_linearity()
        self._antenna_checked = False
        self._publish()

    def check_antenna(self):
        """Warn if the front end looks terminated rather than connected.

        A dummy load and a very quiet band are indistinguishable to the deck:
        both are just a low, flat noise floor. On 2026-08-27 a multi-day
        unattended survey was started with a 50 ohm terminator still screwed on
        after a padcal run, along with a Gate 1 clipping test and a spectrum
        survey, and all of it had to be discarded. Nothing in the software
        objected, because nothing was watching for it.

        `dummy_floor_dbfs` in the profile is what the floor reads with the
        antenna removed, measured at that receiver's configured gain. Within
        `ANTENNA_MISSING_DB` of it means the antenna is contributing nothing
        that the receiver's own noise is not already making. Comparison is only
        valid at the gain it was measured at, so a gain override skips it.

        A warning, never a refusal: a genuinely quiet site is possible, and a
        deck that declines to run in a field because it disagrees with a number
        in a config file is worse than one that logs a loud line and gets on
        with it.
        """
        expect = self.settings.get("dummy_floor_dbfs")
        if expect is None or self.args.simulate:
            return
        if abs(float(self.settings["gain"]) - float(
                self.settings.get("dummy_floor_gain", self.settings["gain"]))) > 0.01:
            return
        if self.det.floor.value is None:
            return                  # still warming up; try again next frame
        self._antenna_checked = True
        floor = float(np.median(self.det.floor.value))
        if floor - float(expect) < ANTENNA_MISSING_DB:
            print(f"   ** FLOOR AT {floor:.1f} dB, and this receiver reads "
                  f"{float(expect):.1f} dB with the antenna REMOVED.\n"
                  f"      Either the antenna is disconnected or the site is "
                  f"extraordinarily quiet. Check the connector before\n"
                  f"      trusting anything this run records.",
                  file=sys.stderr)

    def check_linearity(self):
        """Confirm the front end is linear on this window before surveying it.

        Per window rather than per run: it depends on what is on the air in the
        band being listened to. Measured 2026-08-26, the same radio at the same
        gain was linear at 466 MHz and compressing at 146 MHz minutes apart, and
        at 155 MHz a paging transmitter drove it in and out of compression
        between captures.
        """
        if self.args.simulate:
            # simradio does not model gain, so the floor does not move and the
            # check can only ever return "inconclusive". Skipping says that
            # plainly instead of writing a verdict that means nothing.
            db.set_window_linearity(self.conn, self.window_id, "unchecked")
            self.window_info["linearity"] = "unchecked"
            return
        verdict = self.radio.linearity(self.det.grid, self.periodogram,
                                       self.chunk, self.settings["gain"])
        db.set_window_linearity(self.conn, self.window_id, verdict)
        self.window_info["linearity"] = verdict
        if verdict == "compressed":
            print(f"   ** FRONT END COMPRESSED at gain "
                  f"{self.settings['gain']:.0f} — this band is too strong for "
                  f"the current attenuation.\n"
                  f"      Levels logged from this window are understated and "
                  f"clipping will NOT report it.\n"
                  f"      Add attenuation ahead of the receiver.",
                  file=sys.stderr)
        elif verdict == "inconclusive":
            print(f"   front-end linearity inconclusive — gain "
                  f"{self.settings['gain']:.0f} may be below the point where "
                  f"the noise floor responds at all", file=sys.stderr)
        else:
            print("   front end linear")

    def close_window(self):
        """Anything still keyed is closed rather than left in flight.

        The transmission may well continue, but this receiver stops being able to
        see it the moment it retunes, so claiming otherwise would put a duration
        on the row that nothing observed.
        """
        for ch in list(self.log.open_rows):
            self.log.close(ch, self.window_t0 + self.ring.written / self.rate,
                           None, self.det.tracker.peak_snr[ch])
            self.events_logged += 1     # closed by the retune, but still logged
        if self.window_id is not None:
            db.close_window(self.conn, self.window_id)
            self.window_id = None

    def read_and_process(self):
        """One read. False means stop this window."""
        ret = self.radio.read(self.chunk, self.fs)
        if ret <= 0:
            if ret == self.radio.OVERFLOW:
                self.overflows += 1
                print("OVERFLOW — samples dropped", file=sys.stderr)
                self.stalled = 0
                return True
            # Not an overflow: the device gave us nothing at all. One is a
            # timeout, a run of them is a radio that has stopped talking.
            self.stalled += 1
            return self.stalled < STALL_FRAMES
        self.stalled = 0
        self._frame(self.chunk[:ret])
        return True

    def _frame(self, samples):
        """Detect, analyse and log one frame's worth of samples."""
        frame_start = self.ring.written
        self.ring.push(samples)
        self.stat_frames += 1
        if not self._antenna_checked:
            self.check_antenna()

        t0 = time.perf_counter()
        psd = self.periodogram(samples)
        if psd is None:
            return
        power_db = to_db(self.det.grid.power(psd))
        self.detect_ms += (time.perf_counter() - t0) * 1000.0

        clipping, desense, clip_frac = self.overload.update(samples, power_db)
        if clipping or desense:
            total = self.overload.clip_frames + self.overload.desense_frames
            if total == 1 or total % 500 == 0:
                why = "clipping" if clipping else "desense"
                print(f"  ** OVERLOAD ({why}) — add attenuation "
                      f"[clip {clip_frac*100:.2f}%]", file=sys.stderr)

        self._collect()

        stepped = self.det.step(power_db, frame_start)
        if stepped is None:
            return
        started, ended = stepped

        # Both edges are reported where they happened, not where they were
        # noticed; see EventTracker. Every timestamp comes off the sample clock,
        # anchored once when the window opened. Reading time.time() per frame and
        # subtracting a sample-derived offset mixes two clocks that only agree
        # while samples arrive in real time — and they do not after a USB
        # overflow delivers a burst, while the process is descheduled, or under
        # --simulate, where a whole transmission can be generated in a fraction
        # of the time it represents. Mixed, an event could be stamped as ending
        # 0.18 s before it started, which the schema rejects outright:
        #   CHECK (t_end IS NULL OR t_end >= t_start).
        tracker = self.det.tracker
        t_started = self.window_t0 + tracker.last_start_sample / self.rate
        t_ended = self.window_t0 + tracker.last_end_sample / self.rate

        for ch in started:
            ch = int(ch)
            self.pending[ch] = max(0, tracker.start_sample[ch] - self.pretrigger)
            self.log.start(ch, t_started, self.det.grid.freqs_hz[ch],
                           overload=clipping or desense)

        for ch, start in list(self.pending.items()):
            if self.ring.written >= start + self.analyze_samples:
                self._analyse(ch, self.analyze_samples)

        for ch in ended:
            ch = int(ch)
            # Still pending means the transmission ended before ANALYZE_SECONDS
            # accumulated — most festival traffic, every "copy that". The IQ is
            # sitting in the ring buffer already, so analyse what there is
            # instead of discarding it. Deviation needs almost no dwell, so even
            # a very short keyup still reaches tier 1; the tone stages gate
            # themselves on their own dwell inside analyze_analog.
            if ch in self.pending:
                avail = tracker.last_end_sample - self.pending[ch]
                self._analyse(ch, min(avail, self.analyze_samples))
            self.pending.pop(ch, None)

            duration = max(0.0, (tracker.last_end_sample
                                 - tracker.start_sample[ch]) / self.rate)
            if self.log.close(ch, t_ended, duration,
                              tracker.peak_snr[ch]) is not None:
                self.events_logged += 1
                print(f"  {self.det.grid.freqs_hz[ch]/1e6:10.4f} MHz  ended, "
                      f"{duration:.2f} s")

        self._stats()

    # -- analysis ------------------------------------------------------------

    def _harmonic_parent(self, ch):
        """Is this channel an odd harmonic of a stronger concurrent event?

        A strong signal at baseband offset f from the tuned centre makes the
        receiver produce products at odd multiples of f — measured as exactly
        -3f, +5f, -7f, +9f, -11f on 2026-08-27. Because the channel grid is
        uniform, the test is integer arithmetic on channel indices and needs no
        tolerance at all.

        Three conditions, all required. The offsets must be in an odd-integer
        ratio of at least 3; the candidate parent must be **stronger**, since a
        product is never louder than what made it; and both must be on the air
        at once, because a harmonic cannot outlive its cause.

        Deliberately conservative. A real transmitter can sit at an odd-harmonic
        offset by coincidence, so this only ever runs while a stronger signal is
        actually transmitting, and the result is recorded rather than discarded.
        """
        centre_ch = int(round(self.det.center / CHANNEL_HZ))
        mine = int(round(self.det.grid.freqs_hz[ch] / CHANNEL_HZ)) - centre_ch
        if mine == 0:
            return None, None
        my_snr = float(self.det.tracker.peak_snr[ch])
        best = (None, None, 0.0)
        for other in self.log.open_rows:
            if other == ch:
                continue
            theirs = int(round(self.det.grid.freqs_hz[other] / CHANNEL_HZ)) - centre_ch
            if theirs == 0 or mine % theirs != 0:
                continue
            n = mine // theirs
            if abs(n) < 3 or n % 2 == 0:
                continue
            snr = float(self.det.tracker.peak_snr[other])
            if snr <= my_snr or snr <= best[2]:
                continue
            best = (self.log.open_rows[other], n, snr)
        return best[0], best[1]

    def _analyse(self, ch, nsamples):
        """Hand one channel's IQ to the analysis worker.

        Removes ch from `pending` either way — one analysis per event. The IQ is
        lifted off the ring here, on the reader thread, because `Ring.get`
        returns a copy and the worker must never touch the buffer the reader is
        still writing into.
        """
        start = self.pending.pop(ch, None)
        row = self.log.open_rows.get(ch)
        if (start is None or row is None
                or nsamples < self.min_analyze_samples):
            return
        parent, n = self._harmonic_parent(ch)
        if parent is not None:
            # Not analysed on purpose. The result would be the parent's, which
            # is exactly what makes these rows dangerous — they inherit its tone
            # at full confidence. Recording the parent instead is both honest
            # and free, and it takes a ~90 ms analysis off the reader.
            self.log.mark_harmonic(row, parent, n)
            self.harmonics_found += 1
            return

        iq = self.ring.get(start, int(nsamples))
        if iq is None:
            return
        if not self.worker.submit(ch, row, iq,
                                  self.det.grid.freqs_hz[ch] - self.det.center,
                                  self.det.grid.freqs_hz[ch],
                                  float(self.det.tracker.peak_snr[ch]),
                                  self.store is not None):
            self.analyses_skipped += 1

    def _collect(self):
        """Apply whatever the worker has finished. Reader thread only."""
        for ch, row, result, freq_hz, snr_db, ms in self.worker.drain():
            self.analyze_ms += ms
            if result is None:
                continue
            self.analyses += 1
            self.analyses_total += 1
            self.log.apply(row, result, freq_hz)
            if self.store is not None:
                self.log.attach_capture(row, *self.store.write(
                    row, result["signals"]))
            self._report(freq_hz, snr_db, result)

    def _report(self, freq_hz, snr_db, result):
        if result["dcs_code"] is not None:
            tone = (f"DCS {result['dcs_code']:03d}{result['dcs_polarity']}"
                    f" ({result['dcs_errors']} bit err)")
        elif result["ctcss_hz"]:
            tone = f"{result['ctcss_hz']:.1f} Hz cap {result['ctcss_conf']:.2f}"
        elif result["dcs_suspected"]:
            tone = "DCS?"
        elif result["tone_checked"]:
            tone = "no tone"
        else:
            tone = "tone not checked"
        print(f"  {freq_hz/1e6:10.4f} MHz  "
              f"snr {snr_db:5.1f} dB  "
              f"dev {result['deviation_hz']:6.0f} Hz  "
              f"[{result['analyzed_s']:.2f}s]  {tone}")

    def _stats(self):
        if not self.args.stats:
            return
        elapsed = time.time() - self.last_stat
        if elapsed < self.STAT_SECONDS:
            return
        fps = self.stat_frames / elapsed
        d_ms = self.detect_ms / max(1, self.stat_frames)
        a_ms = self.analyze_ms / max(1, self.analyses)
        uptime_h = (time.time() - self.session_start) / 3600.0
        active = int((self.det.tracker.state == EventTracker.ACTIVE).sum())
        harm = f" +{self.harmonics_found} harm" if self.harmonics_found else ""
        print(f"[stats] {fps:5.1f} fps (target {self.rate/self.fs:.0f})   "
              f"overflow {self.overflows}   active {active}   "
              f"events {self.events_logged}{harm} "
              f"({self.events_logged/max(uptime_h, 1/60):.0f}/hr)")
        print(f"        detect {d_ms:5.2f} ms/frame ({d_ms*fps/10:4.1f}% core)   "
              f"floor {self.det.floor.last_cost_ms:5.2f} ms/{FLOOR_EVERY} frames"
              f"   analyse {a_ms:6.1f} ms x{self.analyses}"
              + (f"  SKIPPED {self.worker.skipped}" if self.worker.skipped else ""))
        print(f"        clip {self.overload.clip_frames}   "
              f"desense {self.overload.desense_frames}")
        self._publish(fps=fps, cost_ms=d_ms, active=active)
        self.detect_ms = self.analyze_ms = 0.0
        self.analyses = 0
        self.stat_frames = 0
        self.last_stat = time.time()

    # -- teardown ------------------------------------------------------------

    def _publish(self, state="running", fps=None, cost_ms=None, active=None):
        uptime_h = (time.time() - self.session_start) / 3600.0
        status.write(self.args.receiver_id, {
            "state": state, "engine": "python", "profile": self.args.profile, "db": self.args.db,
            "run_id": self.run_id, "serial": getattr(self, "serial_seen", None), "rate": self.rate,
            "gain": self.settings["gain"], "started_at": self.session_start,
            "window": getattr(self, "window_info", {}),
            "fps": fps, "target": self.rate / self.fs, "overflows": self.overflows,
            "active": active, "clip": self.overload.clip_frames,
            "desense": self.overload.desense_frames, "cost_ms": cost_ms,
            "events": self.events_logged, "events_per_hr": self.events_logged / max(uptime_h, 1 / 60),
            "analysed": self.analyses_total, "skipped": self.worker.skipped,
            "aged": 0, "harmonics": self.harmonics_found,
        })

    def finish(self):
        """Close everything that is open, then say how it went.

        Coverage lives in coverage_windows (migration 7), one row per tune, so
        "what were we listening to at 21:30" is answerable for a rotating
        receiver. run_receivers still carries the radio, its serial and the rate;
        runs carries the span.
        """
        # Retire the worker before closing anything it writes through. Queued
        # analyses are finished and applied rather than discarded: they belong
        # to events already in the database, and dropping them would leave rows
        # that look like they were never worth analysing.
        self.worker.stop()
        self._collect()

        self.close_window()
        db.end_run(self.conn, self.run_id)
        self.conn.close()
        self.radio.stop()

        self._publish(state="stopped")
        print(f"\nstopped. overflows: {self.overflows}  "
              f"events: {self.events_logged}  "
              f"analysed: {self.analyses_total}  "
              f"skipped: {self.worker.skipped}  "
              f"harmonics: {self.harmonics_found}  "
              f"clip frames: {self.overload.clip_frames}  "
              f"desense frames: {self.overload.desense_frames}")
        if self.harmonics_found:
            print(f"{self.harmonics_found} events were receiver products of a "
                  f"stronger signal — odd harmonics of its offset from the "
                  f"tuned centre.\n  They are recorded with `harmonic_of` "
                  f"pointing at the parent and excluded from `channels`; they "
                  f"were not analysed,\n  because the answer would have been "
                  f"the parent's.")
        if self.worker.skipped:
            print(f"{self.worker.skipped} analyses were skipped because the "
                  f"worker was still busy. Those events are logged and timed "
                  f"but stay at tier 0.\n  Skipping analysis loses one row's "
                  f"detail; skipping reads would corrupt the whole window.")
        if self.store is not None:
            print(f"retained {self.store.count} captures, "
                  f"{self.store.written/1e6:.1f} MB"
                  + (f" — stopped early: {self.store.stopped}"
                     if self.store.stopped else ""))
        if self.overflows:
            print("Overflows mean dropped samples and unreliable data — check "
                  "USB topology with 'lsusb -t' and power with "
                  "'dmesg | grep -i voltage'.")
        if self.overload.clip_frames or self.overload.desense_frames:
            print("Front end was overloaded. Affected events are flagged in the "
                  "`overload` column.\n"
                  "  Attenuation is measured per band, not assumed — see "
                  "docs/phase_log.md Phase 1.\n"
                  "  Overload can also come from too much GAIN: dropping 42 to "
                  "30 cut spur products by 41 dB\n"
                  "  on 2026-08-27, and the carrier read stronger afterwards.")


def run(args):
    settings = resolve_settings(args)
    # getattr, not args.engine: run() is also called with a Namespace built by
    # hand (tests/test_endtoend.py), which predates the option and must keep
    # meaning the Python engine.
    if getattr(args, "engine", "python") == "rust":
        if args.simulate:
            raise SystemExit("--simulate drives the Python engine only; for the rust "
                             "engine use tests/test_engine_loop.py")
        import engine_loop
        return engine_loop.run(args, settings, iq_file=getattr(args, "engine_iq_file", None),
                               file_wall0=getattr(args, "engine_wall0", None))
    radio = Radio(args, settings)
    rate, center = radio.configure(settings)
    serial = radio.serial(settings["serial_want"])

    windows = settings["windows"]
    fs = frame_size(rate)
    grid_n = ChannelGrid(center, rate).n
    print(f"receiver {args.receiver_id}: {rate/1e6:.3f} MSPS, "
          f"gain {settings['gain']}, ppm {settings['ppm']}")
    print(f"{settings['mode']}: " + ", ".join(
        f"{w['center_hz']/1e6:.3f} MHz" + (f" ({w['label']})" if w['label'] else "")
        for w in windows)
        + ("" if len(windows) == 1 else
           "  dwell " + ", ".join(
               f"{(w.get('dwell_s') or settings['dwell_s']):.0f} s" for w in windows)))
    print(f"detect on {settings['on_db']:.1f} dB / off {settings['off_db']:.1f} dB, "
          f"min {settings['min_duration_s']:.2f} s, "
          f"hang {settings['hang_s']:.2f} s")

    db.init_schema(args.db)
    # db.connect opens with isolation_level=None, so every statement commits as
    # it executes. There is nothing to batch and nothing to flush: the calls to
    # conn.commit() that used to sit at the ends of these blocks were no-ops that
    # read as transaction boundaries. The two-phase write cannot be atomic anyway
    # — a row is inserted on keyup and updated a second later — and a half-written
    # event is exactly what an unattended deck should leave behind when it loses
    # power mid-transmission.
    conn = db.connect(args.db)
    run_id = db.start_run(conn, args.profile, notes=args.notes)
    db.register_receiver(conn, run_id, args.receiver_id,
                         serial=serial, sample_rate_hz=int(rate),
                         gain_db=settings["gain"], ppm_error=settings["ppm"],
                         center_hz=int(center),
                         attenuator_db=settings["attenuator_db"],
                         antenna=settings["antenna"])

    # Before CaptureLoop, not after: the loop sizes its frame against the
    # stream's MTU and cannot ask for it until the stream exists.
    radio.start()

    loop = CaptureLoop(radio, settings, args, conn, run_id, rate, center)
    loop.serial_seen = serial
    # After the loop, not before: frame_size() is what the analysis asks for and
    # loop.fs is what the driver will actually deliver. Printing the request
    # rather than the reality said "13.1 ms frames (76/sec)" on a 10 MSPS run
    # delivering 6.6 ms frames at 152/sec.
    print(f"{grid_n} channels on a {CHANNEL_HZ/1000:.2f} kHz grid, "
          f"{loop.frame_seconds*1000:.1f} ms frames ({rate/loop.fs:.0f}/sec)")
    print(f"ring buffer {loop.ring_seconds*rate*8/1e6:.0f} MB\n")
    if loop.store is not None:
        print(f"retaining {'audio + channel IQ' if args.capture_iq else 'audio'} "
              f"under {loop.store.root}, budget {args.capture_mb:.0f} MB")
    print(f"run {run_id}, serial {serial}, profile {args.profile}")

    return loop.go(windows)


def main():
    # Line-buffer stdout. When it is not a terminal — under systemd, or any
    # redirect — Python block-buffers it, so a run that is killed rather than
    # stopped loses everything still in the buffer. Measured on 2026-08-27: a
    # 45 s capture that logged 124 events to the database emitted not one of
    # them to its log file, because `timeout` sends SIGTERM and the buffer went
    # with the process. journald is a pipe too, so the deployed deck had the
    # same hole.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    p = argparse.ArgumentParser(description="RF survey deck bench prototype")
    p.add_argument("--selftest", action="store_true",
                   help="check and benchmark without any radio attached")
    p.add_argument("--driver", default="airspy")
    p.add_argument("--serial", default=None,
                   help="address radios by serial, never by index")
    # These all default to None so the profile can supply them. A value here is
    # an override, and the run row records what was actually used either way.
    p.add_argument("--freq", type=float, default=None,
                   help="park on this centre, ignoring the profile's windows")
    p.add_argument("--rate", type=float, default=None)
    p.add_argument("--gain", type=float, default=None)
    p.add_argument("--ppm", type=float, default=None)
    p.add_argument("--on-db", type=float, default=None)
    p.add_argument("--off-db", type=float, default=None)
    p.add_argument("--dwell-seconds", type=float, default=None,
                   help="override the profile's rotation dwell")
    p.add_argument("--simulate", type=float, metavar="SECONDS", default=None,
                   help="drive the whole capture path from a synthetic radio, "
                        "with no hardware. See src/simradio.py")
    p.add_argument("--db", default="data/survey.sqlite",
                   help="the one survey database; created or upgraded on open")
    # Not a free string, and no default. "rx0" matched nothing in the profile or
    # the schema, and a receiver_id is the only thing tying a row back to which
    # band it was heard on. Getting it wrong is silent and unrecoverable.
    p.add_argument("--receiver-id", choices=("uhf", "vhf"),
                   help="which receiver this is, as named in the profile")
    p.add_argument("--profile", default="profiles/festival.yaml",
                   help="snapshotted verbatim into the run row")
    p.add_argument("--notes", default=None, help="free text recorded on the run")
    p.add_argument("--capture-dir", default=None, metavar="PATH",
                   help="retain per-event audio here and record it in "
                        "events.audio_path. A festival happens once; without "
                        "this a deployment produces nothing to re-analyse.")
    p.add_argument("--capture-iq", action="store_true",
                   help="also retain the complex channel the analyser saw "
                        "(~190 kB/s of traffic, vs ~16 kB/s for audio)")
    p.add_argument("--capture-mb", type=float, default=2000.0,
                   help="retention budget in MB; capture stops at the cap and "
                        "logging continues (default 2000)")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--engine", choices=("python", "rust"), default="python",
                   help="python: the in-process loop. rust: engine/ reads and "
                        "detects in its own process and analysis runs in a third "
                        "(see src/engine_loop.py). Build with cargo build --release")
    # Test hook: drive the rust engine from a file of CF32 samples instead of a
    # radio, so the whole path from detection to database rows can be compared
    # against the Python engine on identical input.
    p.add_argument("--engine-iq-file", metavar="PATH", help=argparse.SUPPRESS)
    p.add_argument("--engine-wall0", type=float, help=argparse.SUPPRESS)
    p.add_argument("--spectrum", metavar="PATH",
                   help="capture a band and write a spectrum/waterfall PNG, "
                        "then print the strongest channels. For headless use.")
    p.add_argument("--spectrum-seconds", type=float, default=10.0)
    args = p.parse_args()

    if args.selftest:
        sys.exit(0 if selftest(args.rate or 10e6) else 1)
    if args.spectrum:
        # --spectrum is a standalone diagnostic and takes no profile, so the
        # argparse defaults that run() gets from the YAML have to be filled in.
        args.rate = args.rate or 10e6
        args.freq = args.freq if args.freq is not None else 466.0e6
        args.gain = args.gain if args.gain is not None else 12.0
        args.ppm = args.ppm or 0.0
        spectrum_capture(args)
        return
    if not args.receiver_id:
        p.error("--receiver-id is required: uhf or vhf")
    if not pathlib.Path(args.profile).is_file():
        p.error(f"profile not found: {args.profile}")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
