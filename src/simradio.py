"""A synthetic Airspy, so the acquisition loop can be run with no hardware.

`survey_prototype.run()` is the one part of this project that has never executed.
Everything else has fixtures or a self test; the capture loop needed a radio, so
the detector, the two-phase write, the retune logic and the database wiring were
all unverified together. That is a lot of untested code to first exercise at a
festival.

This stands in for the device. It generates IQ containing transmissions the test
already knows the answers to, at whatever centre the caller has tuned to, and
implements the handful of SoapySDR calls `run()` actually makes. Feed it to
`--simulate` and the whole path runs: detect, analyse, log, roll up, score.

It is not a channel model. There is no path loss, no multipath, no adjacent
channel splatter, no frequency drift, and the "voice" is two sine tones. It
cannot tell you whether the deck will work at a festival. It tells you whether
the code does what it says, which is a different and much cheaper question.

Phase is integrated analytically rather than by cumsum, so a transmission stays
phase-continuous across block boundaries no matter how the caller chunks its
reads. A cumsum per block would restart the integral each time and put a click
at every frame edge — which the detector would happily log as an event.
"""

from __future__ import annotations

import math

import numpy as np

import dcs as dcs_mod

# Must match survey_prototype.CHANNEL_HZ. Not imported, because survey_prototype
# imports this module — the selftest asserts they agree.
CHANNEL_HZ = 6250.0

# Signal amplitude is derived from the requested SNR relative to the noise in one
# channel. With noise switched off entirely there is no such thing as an SNR, and
# scaling by it would produce silence — so a reference level stands in. A
# noiseless capture is worth having: it is the only way to compare two reads of
# the same span for exact equality, which is how phase continuity is checked.
REFERENCE_NOISE = 0.05

TONE_DEV_HZ = 700.0          # subaudible tone/DCS deviation
VOICE_HZ = (900.0, 1700.0)   # the two-tone stand-in for speech
VOICE_MIX = (0.6, 0.4)


class Transmission:
    """One keyup: when, where, how long, and what is under it."""

    def __init__(self, freq_hz, t_start, duration_s, *, ctcss_hz=None,
                 dcs_code=None, dcs_polarity="N", deviation_hz=2400.0,
                 snr_db=30.0, label=""):
        self.freq_hz = float(freq_hz)
        self.t_start = float(t_start)
        self.duration_s = float(duration_s)
        self.ctcss_hz = ctcss_hz
        self.deviation_hz = float(deviation_hz)
        self.snr_db = float(snr_db)
        self.label = label
        self.dcs_code = dcs_code
        self.dcs_bits = None
        if dcs_code is not None:
            self.dcs_bits = np.array(
                [1.0 if b else -1.0
                 for b in dcs_mod.bit_sequence(dcs_code, dcs_polarity)])
            # Running sum of bit values, so the FM phase integral over a square
            # wave is exact and continuous rather than re-derived per block.
            self.dcs_prefix = np.concatenate(([0.0], np.cumsum(self.dcs_bits)))

    def phase(self, lt):
        """Accumulated FM phase at times `lt` seconds into the transmission.

        `acc` accumulates the integral of instantaneous frequency in CYCLES, and
        the caller-visible phase is 2*pi times that. Keeping the units explicit
        matters: for a sinusoid,

            int_0^t dev*sin(2*pi*f*tau) dtau = dev*(1 - cos(2*pi*f*t)) / (2*pi*f)

        and dropping that 2*pi — as this did at first — multiplies every
        deviation by 6.28. The symptom is not subtle but it is easy to
        misattribute: the signal occupies eleven 6.25 kHz channels instead of
        two, and the discriminator, which can only represent +/- audio_fs/2,
        saturates and returns what looks exactly like noise.
        """
        acc = np.zeros_like(lt)
        two_pi = 2.0 * np.pi
        for mix, f in zip(VOICE_MIX, VOICE_HZ):
            acc += self.deviation_hz * mix * (1.0 - np.cos(two_pi * f * lt)) / (two_pi * f)
        if self.ctcss_hz:
            f = float(self.ctcss_hz)
            acc += TONE_DEV_HZ * (1.0 - np.cos(two_pi * f * lt)) / (two_pi * f)
        elif self.dcs_bits is not None:
            n = len(self.dcs_bits)
            pos = lt * dcs_mod.BPS
            whole = np.floor(pos).astype(np.int64)
            cycles, idx = np.divmod(whole, n)
            full = cycles * self.dcs_prefix[n] + self.dcs_prefix[idx]
            partial = self.dcs_bits[idx] * (pos - whole)
            acc += TONE_DEV_HZ * (full + partial) / dcs_mod.BPS
        return 2.0 * np.pi * acc


class _Status:
    __slots__ = ("ret",)

    def __init__(self, ret):
        self.ret = ret


class SimulatedRadio:
    """Implements the SoapySDR surface `survey_prototype.run()` uses.

    Doubles as the module: it carries the SOAPY_* constants the loop reads, so
    the caller can substitute it for the real import wholesale.
    """

    SOAPY_SDR_RX = 0
    SOAPY_SDR_CF32 = "CF32"
    SOAPY_SDR_OVERFLOW = -4

    def __init__(self, transmissions, *, rate=2_400_000.0, center_hz=466_000_000.0,
                 noise=0.05, serial="SIMULATED", seed=0, duration_s=None,
                 announce=False):
        self.txs = list(transmissions)
        self.rate = float(rate)
        self.center = float(center_hz)
        self.noise = float(noise)
        self.serial = serial
        self.rng = np.random.default_rng(seed)
        self.duration_s = duration_s
        self.samples_read = 0
        # Only --simulate wants the per-window summary; a unit test calling
        # readStream directly wants its stdout back.
        self.announce = announce
        self._announced = None
        self.gain = None
        self.ppm = None

    # -- the SoapySDR surface ------------------------------------------------
    def setSampleRate(self, _dir, _ch, rate):
        self.rate = float(rate)

    def getSampleRate(self, _dir, _ch):
        return self.rate

    def setFrequency(self, _dir, _ch, hz):
        # Retuning replays the scenario from the top, so a rotating receiver
        # gets a full scenario per window rather than whatever was left of one.
        # A real radio obviously does not rewind; this is the one place the
        # simulation is deliberately not lifelike.
        #
        # It replays the WHOLE scenario, but a window only hears the part of it
        # inside its span — see audible(). Every window in the profile has
        # traffic written for it; a window with none is announced as such.
        self.center = float(hz)
        self.samples_read = 0
        self._announced = None

    def getFrequency(self, _dir, _ch):
        return self.center

    def setGainMode(self, _dir, _ch, automatic):
        if automatic:
            raise RuntimeError("AGC must stay off")

    def setGain(self, _dir, _ch, gain):
        self.gain = gain

    def setFrequencyCorrection(self, _dir, _ch, ppm):
        self.ppm = ppm

    def getHardwareInfo(self):
        return {"serial": self.serial, "driver": "simulated"}

    def setupStream(self, *_a, **_k):
        return object()

    def activateStream(self, _s):
        self.t0 = 0.0

    def deactivateStream(self, _s):
        pass

    def closeStream(self, _s):
        pass

    def audible(self):
        """The scenario transmissions this window can actually hear.

        A transmission further than half the span from the centre is simply not
        generated, so a window containing none of the scenario produces silence
        that looks exactly like a detector which has stopped working. The
        scenario is written at absolute frequencies, so which ones a window
        hears is a property of where it is tuned, not of the code under test.
        """
        return [t for t in self.txs
                if abs(t.freq_hz - self.center) <= self.rate * 0.45]

    def _announce(self):
        heard = self.audible()
        outside = len(self.txs) - len(heard)
        if not heard:
            print(f"  simulated: nothing in this window — {outside} scenario "
                  f"transmission(s), none within "
                  f"+/-{self.rate*0.45/1e6:.2f} MHz of "
                  f"{self.center/1e6:.3f} MHz. Silence here is the scenario, "
                  f"not the detector.")
            return
        print(f"  simulated: {len(heard)} transmission(s) in this window"
              + (f", {outside} outside it" if outside else ""))
        for t in heard:
            print(f"    {t.freq_hz/1e6:10.4f} MHz  {t.duration_s:.2f} s"
                  + (f"  {t.label}" if t.label else ""))

    def readStream(self, _stream, buffers, numElems, timeoutUs=0):
        """Fill the caller's buffer with the next block of synthetic IQ."""
        if self.announce and self._announced != self.center:
            self._announced = self.center
            self._announce()
        n = int(numElems)
        n0 = self.samples_read
        if self.duration_s is not None and n0 / self.rate >= self.duration_s:
            return _Status(0)               # end of scenario; caller sees a timeout

        buf = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        buf = buf.astype(np.complex64) * np.float32(self.noise)

        for tx in self.txs:
            s0 = int(tx.t_start * self.rate)
            s1 = int((tx.t_start + tx.duration_s) * self.rate)
            a, b = max(n0, s0), min(n0 + n, s1)
            if b <= a:
                continue
            offset = tx.freq_hz - self.center
            if abs(offset) > self.rate * 0.45:
                continue                    # outside what this window can hear
            lt = (np.arange(a, b) - s0) / self.rate
            # snr_db is IN-CHANNEL signal-to-noise, because that is what the
            # detector measures: it compares per-channel power against a
            # per-channel noise floor. The noise generated above is spread over
            # the whole sample rate, so only the fraction landing in one
            # CHANNEL_HZ-wide channel competes with the signal. Treating snr_db
            # as wideband instead makes every signal ~30 dB too strong, and a
            # signal that strong leaks through the window sidelobes into a dozen
            # neighbouring channels and gets logged a dozen times.
            ref = self.noise if self.noise > 0.0 else REFERENCE_NOISE
            noise_in_channel = 2.0 * ref ** 2 * (CHANNEL_HZ / self.rate)
            amp = math.sqrt(noise_in_channel) * 10.0 ** (tx.snr_db / 20.0)
            ph = 2.0 * np.pi * offset * lt + tx.phase(lt)
            buf[a - n0:b - n0] += (amp * np.exp(1j * ph)).astype(np.complex64)

        buffers[0][:n] = buf
        self.samples_read += n
        return _Status(n)


def festival_scenario(t0=2.0):
    """Transmissions with known answers, one group per window in the profile.

    t0 clears the noise-floor warm-up. NoiseFloor needs FLOOR_FRAMES of history
    before it reports anything — about 1.6 s — and until then the detector cannot
    fire at all. A scenario starting sooner tests nothing and looks like a bug.

    Chosen so that each one is the only thing that can explain a particular row
    in the database, which is what makes the assertions in --simulate meaningful
    rather than decorative.

    Every window `profiles/festival.yaml` defines has traffic here, and every
    frequency is within +/-1.08 MHz of its centre so it survives a 2.4 MSPS run
    as well as a 10 MSPS one. Without that, the documented rotation command
    logged one event across three windows and the two silent windows looked
    exactly like a broken detector.
    """
    code = sorted(dcs_mod.STANDARD_CODES)[0]
    return [
        # UHF window, centred 466.000
        Transmission(462_675_000, t0, 4.0, ctcss_hz=141.3, deviation_hz=4900,
                     snr_db=34, label="GMRS 20 wideband, CTCSS — long"),
        Transmission(462_650_000, t0 + 5.5, 0.8, ctcss_hz=88.5, deviation_hz=2400,
                     snr_db=40, label='FRS 19 narrowband — a "copy that"'),
        Transmission(462_700_000, t0 + 7.0, 0.30, deviation_hz=2400,
                     snr_db=30, label="FRS 21 — too short to analyse for tone"),
        Transmission(467_850_000, t0 + 8.5, 2.5, dcs_code=code, deviation_hz=2400,
                     snr_db=36, label=f"Part 90 — DCS {code}"),
        # VHF window, centred 146.000
        Transmission(146_520_000, t0 + 1.0, 3.0, deviation_hz=2400, snr_db=32,
                     label="2 m calling — no tone"),
        # VHF window, centred 446.000. Offset from the calling channel rather
        # than on it: a signal at exactly the centre sits on DC, which is not
        # where a real receiver is at its best and not what this is testing.
        Transmission(446_012_500, t0 + 1.0, 2.0, ctcss_hz=100.0,
                     deviation_hz=2400, snr_db=33,
                     label="70 cm simplex, CTCSS"),
        # VHF window, centred 154.950
        Transmission(154_570_000, t0 + 1.0, 2.5, ctcss_hz=67.0,
                     deviation_hz=2400, snr_db=35,
                     label="MURS 154.570 (Blue Dot), CTCSS"),
    ]
