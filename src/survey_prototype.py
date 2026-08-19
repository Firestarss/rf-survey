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
import collections
import math
import pathlib
import shutil
import signal
import sys
import tempfile
import wave
import time

import numpy as np
from scipy.signal import firwin, lfilter, upfirdn

import db
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
FLOOR_EVERY = 10           # recompute the background every Nth frame
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
        over while it is still going. At the default 120 frames and the 20th
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
    mag = np.abs(baseband)
    strong = np.flatnonzero(mag > 0.4 * np.percentile(mag, 95))
    if len(strong) >= 64 and (strong[-1] - strong[0]) >= 64:
        baseband = baseband[strong[0]:strong[-1] + 1]

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
    nowhere else, so `--selftest` can drive the exact code path the field deck
    uses against a temporary database with no radio attached. That is the only
    way to test this wiring before the hardware arrives.
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

    # --- deviation ---------------------------------------------------------
    # The FRS/GMRS rule is a threshold on this number, so what it reports
    # against a known input is the whole question. Synthetic only: this closes
    # the estimator half of "measurement accuracy unverified", not the receiver
    # half, which needs a real transmitter in Phase 3.
    print("\n  deviation estimate  (peak, p99 of |inst - mean|)")
    dev_ok = True
    for voice_dev in (1500.0, 3000.0, 5000.0):
        t = np.arange(int(crate * 1.4)) / crate
        voice = 0.6 * np.sin(2 * np.pi * 900 * t) + 0.4 * np.sin(2 * np.pi * 1700 * t)
        true_peak = voice_dev * float(np.max(np.abs(voice)))
        got = analyze_analog(make_fm(None, crate, voice_dev=voice_dev),
                             crate, 12500.0)["deviation_hz"]
        err = (got - true_peak) / true_peak
        hit = abs(err) <= 0.15
        dev_ok &= hit
        print(f"    true peak {true_peak:6.0f} Hz -> reported {got:6.0f} Hz "
              f"({err*100:+5.1f}%) {'ok' if hit else 'FAILED'}")
    ok &= dev_ok

    # --- event timing ------------------------------------------------------
    # A detection is declared min_duration after the signal starts and closed
    # hang seconds after it stops. Both edges must be reported where they
    # happened; otherwise every duration and every airtime total is inflated.
    print("\n  event timing")
    fsec, fsamp = 0.04, 1000
    tr = EventTracker(1, fsec, min_duration=0.12, hang=0.30)
    hot_from, hot_to = 10, 30           # hot for frames 10..29 = 0.80 s
    got_start = got_end = None
    for i in range(60):
        snr = np.array([20.0 if hot_from <= i < hot_to else 0.0])
        started, ended = tr.update(snr, i * fsamp)
        if len(started):
            got_start = tr.start_sample[0]
        if len(ended):
            got_end = tr.last_end_sample
    dur = (got_end - got_start) / (fsamp / fsec)
    timing_ok = (got_start == hot_from * fsamp and got_end == hot_to * fsamp
                 and abs(dur - 0.80) < 1e-9)
    print(f"    signal 0.80 s -> logged {dur:.2f} s "
          f"(start frame {got_start//fsamp}, end frame {got_end//fsamp}) "
          f"{'ok' if timing_ok else 'FAILED'}")
    ok &= timing_ok

    # --- database round trip -----------------------------------------------
    # Drives the exact field mapping the field deck uses, against a real
    # schema. With no radio this is the only thing standing between a wiring
    # mistake and a festival that logs nothing.
    print("\n  database round trip")
    db_ok = True
    root = pathlib.Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        path = str(pathlib.Path(tmp) / "selftest.sqlite")
        db.init_schema(path)
        conn = db.connect(path)
        run_id = db.start_run(conn, str(root / "profiles" / "festival.yaml"),
                              notes="selftest")
        db.register_receiver(conn, run_id, "uhf", serial="SELFTEST",
                             sample_rate_hz=int(crate), center_hz=466_000_000)
        log = EventLog(conn, run_id, "uhf")

        freq = 462_675_000
        log.start(0, 1000.0, freq, overload=True)
        log.analysed(0, analyze_analog(make_fm(88.5, crate), crate, 12500.0), freq)
        log.close(0, 1002.0, 2.0, 41.5)

        # In-flight: detected, analysed, never closed — the row a power cut
        # leaves behind.
        log.start(1, 1010.0, 462_650_000, overload=False)
        log.analysed(1, analyze_analog(make_fm(None, crate), crate, 12500.0),
                     462_650_000)

        closed = conn.execute(
            "SELECT * FROM events WHERE freq_hz = ?", (freq,)).fetchone()
        inflight = conn.execute(
            "SELECT * FROM events WHERE freq_hz = ?", (462_650_000,)).fetchone()

        checks = [
            ("t_start/t_end kept", closed["t_start"] == 1000.0
                                   and closed["t_end"] == 1002.0),
            ("duration recorded", closed["duration_s"] == 2.0),
            ("snr -> snr_db", closed["snr_db"] == 41.5),
            ("ctcss identified", closed["ctcss_hz"] == 88.5),
            ("tone_state ctcss", closed["tone_state"] == "ctcss"),
            ("confidence in [0,1]", 0.0 <= closed["confidence"] <= 1.0),
            ("deviation measured", closed["deviation_hz"] > 1000.0),
            # Asserting freq_raw_hz != freq_hz was wrong: it demanded a
            # measurement error. Trimming the lead-in noise made the estimate
            # exact and the check failed on an improvement. What matters is that
            # the raw value is recorded and plausible.
            ("freq snapped, raw kept", closed["freq_hz"] == freq
                                       and closed["freq_raw_hz"] is not None
                                       and abs(closed["freq_raw_hz"] - freq) < 2000),
            ("overload flagged", closed["overload"] == 1),
            ("in-flight t_end NULL", inflight["t_end"] is None),
            ("in-flight duration NULL", inflight["duration_s"] is None),
            ("in-flight tone none", inflight["tone_state"] == "none"),
            ("no dangling refs",
             not conn.execute("PRAGMA foreign_key_check").fetchall()),
        ]
        for name, hit in checks:
            db_ok &= hit
            print(f"    {name:26s} {'ok' if hit else 'FAILED'}")

        # Every standard tone through the real INSERT path. The CHECK on
        # confidence is [0,1] and the capture ratio overshoots without its
        # clamp, so this is the case that would throw on a clean strong signal.
        try:
            for i, w in enumerate(CTCSS_TONES):
                r = analyze_analog(make_fm(w, crate, noise=0.01), crate, 12500.0)
                log.start(100 + i, 2000.0 + i, 462_700_000)
                log.analysed(100 + i, r, 462_700_000)
            worst = conn.execute("SELECT MAX(confidence) FROM events").fetchone()[0]
            print(f"    {len(CTCSS_TONES)} tones inserted, max confidence "
                  f"{worst:.6f}   ok")
        except Exception as exc:
            db_ok = False
            print(f"    tone insert path         FAILED: {exc}")
        conn.close()
    ok &= db_ok

    # --- DCS decoding ------------------------------------------------------
    print(f"\n  DCS decoding  ({len(dcs_mod.STANDARD_CODES)} standard codes, "
          f"generator 0x{dcs_mod.GOLAY_POLY:X})")
    dcs_ok = True
    probe = sorted(dcs_mod.STANDARD_CODES)[:4]
    for code in probe:
        bits = np.array([1.0 if b else -1.0
                         for b in dcs_mod.bit_sequence(code, "N")])
        r = analyze_analog(make_fm(None, crate, dur=1.4, dcs_word=bits,
                                   noise=0.5), crate, 12500.0)
        hit = r["dcs_code"] == int(code) and r["dcs_polarity"] == "N"
        dcs_ok &= hit
        shown = "none" if r["dcs_code"] is None else f"{r['dcs_code']:03d}"
        print(f"    code {code} -> {shown}{r['dcs_polarity'] or ''} "
              f"({r['dcs_errors']} bit err)  {'ok' if hit else 'FAILED'}")
    # A CTCSS tone must never come back as a DCS code. Sliced at 134.4 bps a
    # pure tone is periodic, so every 23-bit window agrees with every other —
    # which is exactly the evidence decode_dcs uses. This is a regression guard:
    # with the DCS decode run ahead of the capture-ratio test, tone 110.9 Hz
    # decoded as DCS 243 and 254.1 Hz as DCS 031.
    tone_as_dcs = [w for w in CTCSS_TONES
                   if analyze_analog(make_fm(w, crate), crate,
                                     12500.0)["dcs_code"] is not None]
    print(f"    CTCSS tones decoded as DCS: {len(tone_as_dcs)} (must be 0)"
          + (f"  {tone_as_dcs}" if tone_as_dcs else ""))
    dcs_ok &= not tone_as_dcs

    # Sending a code inverted is the same waveform as sending its documented
    # partner normally, so that is the right answer rather than a misread.
    inv_ok = True
    for code in sorted(dcs_mod.STANDARD_CODES)[:3]:
        bits = np.array([1.0 if b else -1.0
                         for b in dcs_mod.bit_sequence(code, "I")])
        got = analyze_analog(make_fm(None, crate, dur=1.4, dcs_word=bits),
                             crate, 12500.0)["dcs_code"]
        hit = got == int(dcs_mod.INVERTED_PAIR[code])
        inv_ok &= hit
        shown = "none" if got is None else f"{got:03d}"
        print(f"    {code} sent inverted -> {shown} "
              f"(= {dcs_mod.INVERTED_PAIR[code]}, its pair)  "
              f"{'ok' if hit else 'FAILED'}")
    dcs_ok &= inv_ok
    ok &= dcs_ok

    # --- analysis window trim ----------------------------------------------
    # The capture window starts before the transmission does. An FM
    # discriminator fed noise returns values spread over +/- audio_fs/2, so
    # without trimming, p99 reports the noise figure and not the signal.
    print("\n  lead-in noise rejection")
    trim_ok = True
    for lead in (0.0, 0.3, 0.6):
        n_lead = int(lead * crate)
        sig = make_fm(None, crate, dur=1.0, voice_dev=2400.0, noise=0.05)
        pad = (np.random.default_rng(3).standard_normal(n_lead)
               + 1j * np.random.default_rng(4).standard_normal(n_lead))
        iq = np.concatenate([pad.astype(np.complex64) * 0.05, sig])
        got = analyze_analog(iq, crate, 12500.0)["deviation_hz"]
        hit = 2000 < got < 4000
        trim_ok &= hit
        print(f"    {lead:.1f}s of noise before a 2.4 kHz signal -> "
              f"{got:6.0f} Hz  {'ok' if hit else 'FAILED (reads as noise)'}")
    ok &= trim_ok

    # --- noise floor must not swallow a long transmission -------------------
    # The floor is a low percentile over FLOOR_FRAMES of history. A carrier that
    # stays up long enough to fill (100-FLOOR_PCTILE)% of that history drags the
    # floor up to meet itself and the detector calls the transmission over while
    # it is still going.
    print("\n  sustained carrier")
    fl = NoiseFloor(4)
    tr = EventTracker(4, 0.0131)
    quiet = np.full(4, -90.0)
    loud = np.array([-60.0, -90.0, -90.0, -90.0])
    active = None
    closed_at = None
    for i in range(900):                      # ~12 s
        power = loud if i >= 150 else quiet
        floor = fl.update(power, active=active)
        if floor is None:
            continue
        started, ended = tr.update(power - floor, i * 1000)
        active = tr.state == EventTracker.ACTIVE
        if len(ended) and closed_at is None:
            closed_at = (i - 150) * 0.0131
    held = closed_at is None
    print(f"    10 s carrier held open: {'yes' if held else f'NO — closed after {closed_at:.2f} s'}")
    ok &= held

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
        windows = [dict(center_hz=int(w["center_hz"]), label=w.get("label"))
                   for w in (rx.get("windows") or [])]
        if not windows:
            raise SystemExit(f"receiver {receiver_id} is rotating with no windows")
    else:
        if rx.get("center_hz") is None:
            raise SystemExit(f"receiver {receiver_id} is parked with no center_hz")
        windows = [dict(center_hz=int(rx["center_hz"]), label=rx.get("label"))]

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
    }


def run(args):
    cfg = load_receiver_config(args.profile, args.receiver_id)

    # The profile is the configuration; the command line overrides it only where
    # something was actually typed. Anything still None below came from the
    # profile, which is what the run row will claim.
    rate_hz = args.rate if args.rate is not None else cfg["sample_rate"]
    gain = args.gain if args.gain is not None else cfg["gain"]
    ppm = args.ppm if args.ppm is not None else cfg["ppm"]
    on_db = args.on_db if args.on_db is not None else cfg["on_db"]
    off_db = args.off_db if args.off_db is not None else cfg["off_db"]
    min_duration_s = cfg["min_duration_s"]
    hang_s = cfg["hang_s"]
    serial_want = args.serial or cfg["serial"]
    dwell_s = (args.dwell_seconds if args.dwell_seconds is not None
               else cfg["dwell_seconds"])
    windows = cfg["windows"]
    if args.freq is not None:                       # explicit override parks it
        windows = [dict(center_hz=int(args.freq), label="--freq")]
    if len(windows) > 1 and not dwell_s:
        raise SystemExit(f"receiver {args.receiver_id} has {len(windows)} windows "
                         f"but no dwell_seconds — it would never rotate")

    if args.simulate:
        import simradio
        soapy = simradio.SimulatedRadio(
            simradio.festival_scenario(), rate=rate_hz,
            center_hz=windows[0]["center_hz"], duration_s=args.simulate,
            serial=f"SIM-{args.receiver_id.upper()}", announce=True)
        sdr = soapy
        SOAPY_SDR_RX, SOAPY_SDR_CF32 = soapy.SOAPY_SDR_RX, soapy.SOAPY_SDR_CF32
        print(f"SIMULATED radio — {args.simulate:.0f} s of synthetic signal per "
              f"window, no hardware involved")
    else:
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
        soapy = SoapySDR
        spec = {"driver": args.driver}
        if serial_want:
            spec["serial"] = serial_want
        sdr = SoapySDR.Device(spec)

    sdr.setSampleRate(SOAPY_SDR_RX, 0, rate_hz)
    sdr.setFrequency(SOAPY_SDR_RX, 0, windows[0]["center_hz"])
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)     # AGC off — non-negotiable
    except Exception:
        print("warning: could not disable AGC", file=sys.stderr)
    sdr.setGain(SOAPY_SDR_RX, 0, gain)
    if ppm:
        try:
            sdr.setFrequencyCorrection(SOAPY_SDR_RX, 0, ppm)
        except Exception:
            print("warning: driver rejected ppm correction", file=sys.stderr)

    rate = sdr.getSampleRate(SOAPY_SDR_RX, 0)
    center = sdr.getFrequency(SOAPY_SDR_RX, 0)
    fs = frame_size(rate)

    # Read the serial back off the device rather than trusting --serial. If the
    # radio was addressed by driver alone this is the only record of which of
    # the two it actually was, and run_receivers.serial is the identity every
    # later row is interpreted against.
    try:
        serial = sdr.getHardwareInfo()["serial"]
    except Exception:
        serial = serial_want
    if not serial:
        print("warning: no serial from device and none given — this run cannot "
              "prove which radio produced it", file=sys.stderr)
        serial = "unknown"

    frame_seconds = fs / rate
    overload = OverloadMonitor()

    # Rebuilt on every retune: the channel grid is defined relative to the centre,
    # so every per-channel index means something different after a tune and none
    # of this state may carry across.
    grid = ChannelGrid(center, rate)
    tracker = EventTracker(grid.n, frame_seconds, on_db=on_db, off_db=off_db,
                           min_duration=min_duration_s, hang=hang_s)
    floor_est = NoiseFloor(grid.n)

    print(f"receiver {args.receiver_id}: {rate/1e6:.3f} MSPS, gain {gain}, "
          f"ppm {ppm}")
    print(f"{cfg['mode']}: " + ", ".join(
        f"{w['center_hz']/1e6:.3f} MHz" + (f" ({w['label']})" if w['label'] else "")
        for w in windows)
        + (f", {dwell_s:.0f} s each" if len(windows) > 1 else ""))
    print(f"{grid.n} channels on a {CHANNEL_HZ/1000:.2f} kHz grid, "
          f"{frame_seconds*1000:.1f} ms frames ({rate/fs:.0f}/sec)")
    print(f"detect on {on_db:.1f} dB / off {off_db:.1f} dB, "
          f"min {min_duration_s:.2f} s, hang {hang_s:.2f} s")

    # The window starts PRETRIGGER_SECONDS before the detector fired, so it has
    # to be that much longer to still contain ANALYZE_SECONDS of signal. Sizing
    # it at ANALYZE_SECONDS flat leaves only 0.6 s of carrier once the trim in
    # analyze_analog has dropped the lead-in — below MIN_TONE_SECONDS, so no
    # transmission of any length ever got its tone identified.
    pretrigger = int(PRETRIGGER_SECONDS * rate)
    analyze_samples = int(ANALYZE_SECONDS * rate) + pretrigger
    ring_seconds = ANALYZE_SECONDS + PRETRIGGER_SECONDS + RING_SLACK_SECONDS
    ring = Ring(int(ring_seconds * rate))
    print(f"ring buffer {ring_seconds*rate*8/1e6:.0f} MB\n")

    db.init_schema(args.db)
    # db.connect opens with isolation_level=None, so every statement commits as
    # it executes. There is nothing to batch and nothing to flush: the calls to
    # conn.commit() that used to sit at the ends of these blocks were no-ops
    # that read as transaction boundaries. The two-phase write cannot be atomic
    # anyway — a row is inserted on keyup and updated a second later — and a
    # half-written event is exactly what an unattended deck should leave behind
    # when it loses power mid-transmission.
    conn = db.connect(args.db)
    session_start = time.time()

    run_id = db.start_run(conn, args.profile, notes=args.notes)
    db.register_receiver(conn, run_id, args.receiver_id,
                         serial=serial, sample_rate_hz=int(rate),
                         gain_db=gain, ppm_error=ppm,
                         center_hz=int(center),
                         attenuator_db=cfg["attenuator_db"],
                         antenna=cfg["antenna"])
    log = EventLog(conn, run_id, args.receiver_id)
    store = None
    if args.capture_dir:
        store = CaptureStore(args.capture_dir, run_id,
                             max_mb=args.capture_mb, keep_iq=args.capture_iq)
        print(f"retaining {'audio + channel IQ' if args.capture_iq else 'audio'} "
              f"under {store.root}, budget {args.capture_mb:.0f} MB")
    print(f"run {run_id}, serial {serial}, profile {args.profile}")

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(stream)

    chunk = np.empty(fs, np.complex64)
    window = np.hanning(NFFT).astype(np.float32)
    window_gain = float(np.sum(window ** 2))
    nseg = fs // NFFT

    pending = {}
    min_analyze_samples = int(MIN_ANALYZE_SECONDS * rate)

    def analyse(ch, nsamples):
        """Analyse `ch` over nsamples from where its capture started.

        Removes ch from `pending` either way — one analysis per event. Returns
        the result dict, or None if there was nothing worth analysing.
        """
        start = pending.pop(ch, None)
        if start is None or ch not in log.open_rows or nsamples < min_analyze_samples:
            return None
        iq = ring.get(start, int(nsamples))
        if iq is None:
            return None
        return analyze_analog(iq, rate, grid.freqs_hz[ch] - center,
                              keep_signals=store is not None)

    def report(ch, result):
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
        print(f"  {grid.freqs_hz[ch]/1e6:10.4f} MHz  "
              f"snr {tracker.peak_snr[ch]:5.1f} dB  "
              f"dev {result['deviation_hz']:6.0f} Hz  "
              f"[{result['analyzed_s']:.2f}s]  {tone}")

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

    win_idx = 0
    window_id = None
    window_t0 = time.time()     # replaced per window; defined for the finally
    stalled = 0
    stream_failed = False

    try:
        while running[0]:
            w = windows[win_idx % len(windows)]

            # Retune. Every per-channel index is defined relative to the centre,
            # so the grid, the detector state and the ring all have to go —
            # carrying any of it across a tune would attribute one band's signal
            # to another band's frequency.
            sdr.setFrequency(SOAPY_SDR_RX, 0, w["center_hz"])
            center = sdr.getFrequency(SOAPY_SDR_RX, 0)
            grid = ChannelGrid(center, rate)
            tracker = EventTracker(grid.n, frame_seconds, on_db=on_db,
                                   off_db=off_db, min_duration=min_duration_s,
                                   hang=hang_s)
            floor_est = NoiseFloor(grid.n)
            pending.clear()
            ring.reset()
            stalled = 0
            active_mask = None

            # Anchors the sample clock for this window. ring.reset() has just
            # put the sample counter back to zero, so every event timestamp in
            # this window is window_t0 + samples/rate.
            window_t0 = time.time()
            window_id = db.open_window(conn, run_id, args.receiver_id,
                                       int(center), int(rate), w["label"])
            log.window_id = window_id
            deadline = time.time() + dwell_s if len(windows) > 1 else None
            print(f"\n== {center/1e6:.3f} MHz"
                  + (f" ({w['label']})" if w["label"] else "")
                  + (f", {dwell_s:.0f} s" if deadline else "") + " ==")

            while running[0] and (deadline is None or time.time() < deadline):
                status = sdr.readStream(stream, [chunk], fs, timeoutUs=2_000_000)
                if status.ret <= 0:
                    if status.ret == soapy.SOAPY_SDR_OVERFLOW:
                        overflows += 1
                        print("OVERFLOW — samples dropped", file=sys.stderr)
                        stalled = 0
                        continue
                    # Not an overflow: the device gave us nothing at all. One is a
                    # timeout, a run of them is a radio that has stopped talking.
                    stalled += 1
                    if stalled >= STALL_FRAMES:
                        break
                    continue
                stalled = 0

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

                # `active_mask` is from the previous frame — see NoiseFloor.update.
                floor_db = floor_est.update(power_db, active=active_mask)
                if floor_db is None:
                    continue
                snr_db = power_db - floor_db

                # One FM transmission at 2.5-5 kHz deviation occupies roughly
                # 11 kHz, and the detector grid is 6.25 kHz, so a single keyup
                # lights up its own channel and both neighbours. Logged as-is,
                # every transmission becomes three events — and because FRS
                # primary and interstitial channels interleave to 12.5 kHz, the
                # two skirts land on legitimate neighbouring channel numbers.
                # One GMRS keyup was being reported as traffic on FRS 5 and
                # FRS 6 as well: invented activity on channels nobody touched.
                #
                # Only a local maximum may open an event. +/-1 channel is
                # deliberate — it covers the skirts without merging anything
                # 12.5 kHz apart, which is the closest two real channels get.
                padded = np.concatenate(([-np.inf], snr_db, [-np.inf]))
                local_max = (snr_db >= padded[:-2]) & (snr_db >= padded[2:])
                started, ended = tracker.update(snr_db, frame_start,
                                                can_start=local_max)
                active_mask = tracker.state == EventTracker.ACTIVE

                # Both edges are reported where they happened, not where they
                # were noticed; see EventTracker.
                #
                # Every timestamp comes off the sample clock, anchored once when
                # the window opened. Reading time.time() per frame and
                # subtracting a sample-derived offset mixes two clocks that only
                # agree while samples arrive in real time — and they do not
                # after a USB overflow delivers a burst, while the process is
                # descheduled, or under --simulate, where a whole transmission
                # can be generated in a fraction of the time it represents.
                # Mixed, an event could be stamped as ending 0.18 s before it
                # started, which the schema rejects outright:
                #   CHECK (t_end IS NULL OR t_end >= t_start).
                t_started = window_t0 + tracker.last_start_sample / rate
                t_ended = window_t0 + tracker.last_end_sample / rate

                for ch in started:
                    ch = int(ch)
                    pending[ch] = max(0, tracker.start_sample[ch] - pretrigger)
                    log.start(ch, t_started, grid.freqs_hz[ch],
                              overload=clipping or desense)

                # ---- analyse channels that have reached full dwell ---------------
                for ch, start in list(pending.items()):
                    if ring.written < start + analyze_samples:
                        continue
                    t1 = time.perf_counter()
                    result = analyse(ch, analyze_samples)
                    analyze_ms_total += (time.perf_counter() - t1) * 1000.0
                    if result is None:
                        continue
                    analyses += 1
                    row = log.analysed(ch, result, grid.freqs_hz[ch])
                    if store is not None and row is not None:
                        log.attach_capture(row, *store.write(row, result["signals"]))
                    report(ch, result)

                for ch in ended:
                    ch = int(ch)
                    # Still pending means the transmission ended before ANALYZE_SECONDS
                    # accumulated — most festival traffic, every "copy that". The IQ is
                    # sitting in the ring buffer already, so analyse what there is
                    # instead of discarding it. Deviation needs almost no dwell, so
                    # even a very short keyup still reaches tier 1; the tone stages
                    # gate themselves on their own dwell inside analyze_analog.
                    if ch in pending:
                        avail = tracker.last_end_sample - pending[ch]
                        t1 = time.perf_counter()
                        result = analyse(ch, min(avail, analyze_samples))
                        analyze_ms_total += (time.perf_counter() - t1) * 1000.0
                        if result is not None:
                            analyses += 1
                            row = log.analysed(ch, result, grid.freqs_hz[ch])
                            if store is not None and row is not None:
                                log.attach_capture(
                                    row, *store.write(row, result["signals"]))
                            report(ch, result)
                    pending.pop(ch, None)
                    duration = max(0.0, (tracker.last_end_sample
                                         - tracker.start_sample[ch]) / rate)
                    if log.close(ch, t_ended, duration,
                                 tracker.peak_snr[ch]) is None:
                        continue
                    events_logged += 1
                    print(f"  {grid.freqs_hz[ch]/1e6:10.4f} MHz  ended, {duration:.2f} s")

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


            # ---- window over --------------------------------------------------
            # Anything still keyed is closed here rather than left in flight. The
            # transmission may well continue, but this receiver stops being able
            # to see it the moment it retunes, so claiming otherwise would put a
            # duration on the row that nothing observed.
            for ch in list(log.open_rows):
                log.close(ch, window_t0 + ring.written / rate, None,
                          tracker.peak_snr[ch])
                events_logged += 1      # closed by the retune, but still logged
            db.close_window(conn, window_id)

            if stalled >= STALL_FRAMES and not args.simulate:
                print(f"stream delivered nothing for {stalled} consecutive reads. "
                      f"Exiting so the supervisor restarts the process and the "
                      f"device re-enumerates — a wedged USB endpoint does not "
                      f"recover in place.", file=sys.stderr)
                stream_failed = True
                running[0] = False

            win_idx += 1
            if args.simulate and win_idx >= len(windows):
                running[0] = False
    finally:
        # Coverage lives in coverage_windows (migration 7), one row per tune, so
        # "what were we listening to at 21:30" is answerable for a rotating
        # receiver. run_receivers still carries the radio, its serial and the
        # rate; runs carries the span.
        if window_id is not None:
            db.close_window(conn, window_id)
        for ch in list(log.open_rows):
            log.close(ch, window_t0 + ring.written / rate, None,
                      tracker.peak_snr[ch])
        db.end_run(conn, run_id)
        conn.close()
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)
        print(f"\nstopped. overflows: {overflows}  events: {events_logged}  "
              f"clip frames: {overload.clip_frames}  "
              f"desense frames: {overload.desense_frames}")
        if store is not None:
            print(f"retained {store.count} captures, {store.written/1e6:.1f} MB"
                  + (f" — stopped early: {store.stopped}" if store.stopped else ""))
        if overflows:
            print("Overflows mean dropped samples and unreliable data — check USB "
                  "topology with 'lsusb -t' and power with 'dmesg | grep -i voltage'.")
        if overload.clip_frames or overload.desense_frames:
            print("Front end was overloaded. Affected events are flagged in the "
                  "`overload` column. Add attenuation — 20 dB is the default and "
                  "costs no usable sensitivity at festival distances.")

    # Non-zero tells the supervisor this was not a clean stop, so a Restart=
    # policy re-enumerates the device instead of treating it as a normal exit.
    return 1 if stream_failed else 0


def main():
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
