# Phase log

Running record of gate results. One entry per phase, appended as it closes.
Full procedures live in `docs/bench-bringup.md`.

| Phase | Status | Date | Headline |
|---|---|---|---|
| **0** — Pi alone, no radio | **PASS** | 2026-08-18 | 28.8% of one core, 27 concurrent, peak 71.6 °C, zero throttling, fan confirmed |
| **1** — first radio, first signal | blocked | — | waiting on Airspy delivery |
| **2** — RF front end | — | — | |
| **3** — tones and classification | — | — | |
| **4** — logging and database | — | — | |
| **5** — second receiver | — | — | |
| **6** — dual-bus USB load | — | — | |
| **7** — repeater pairing | — | — | |
| **8** — 24 h unattended soak | — | — | |

---

## Phase 0 — 2026-08-18 — PASS

**Machine as built:** `radio-deck`, Pi 5 8 GB (CanaKit Essentials), GeeekPi metal case with
official M.2 HAT+ and Active Cooler, WD Black SN770M 500 GB NVMe, Ubuntu 26.04 LTS,
kernel 7.0.0-1016-raspi, booting NVMe with `BOOT_ORDER=0xf146`.

**Compute headroom is roughly double the estimate.** `--selftest` held 28.8% of one core
steady-state, which projects to about 14% of the machine for two radios. 27 simultaneous
transmissions handled. The second Airspy is no longer a question mark.

**Thermal is not a concern on the bench.** Peak 71.6 °C was `stress-ng` pinning four cores
at 100%; the real workload is 7–14%, so operating temperature sits much nearer the 44 °C
idle. Clock held 2400 MHz across all 292 samples — zero throttling.

**Cooling confirmed working.** Fan reads 0 rpm at 44 °C idle (correct — the controller stops
below ~50 °C), 529 rpm at ~61 °C on one sample and 5293 rpm on a second load run. Fan curve
is responding and has steps left. Revisit only when the enclosure is sealed.

**PCIe negotiated Gen2 x1** (5 GT/s, ~450 MB/s) against a workload needing single-digit MB/s.
Deliberately left there rather than forced to Gen3.

### Findings that change later assumptions

- **A third USB 2.0 controller exists.** `Bus 001` is a `dwc2` controller on the USB-C
  connector. If it can be put in host mode, that is a route to a third radio without a PCIe
  card — at the cost of powering the Pi through GPIO. Investigate after the first event.
- **`vcgencmd` installs but `/dev/vcio` is absent**, so `get_throttled` fails. Use
  `/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq` instead — below 2400 MHz under
  load means throttling.
- **NumPy is linked against reference BLAS, not OpenBLAS.** Low priority: the hot paths are
  pocketfft and scipy's `upfirdn`, neither of which uses BLAS.
- **`eth0` is down, WiFi only.** Fine for SSH and `--spectrum`. Live remote spectrum via
  SoapyRemote at full rate is ~30 MB/s and wants ethernet; over WiFi, drop to 2.5 MSPS.
- **AppArmor logs denials against `lsusb`** on 26.04. Cosmetic — output is complete.
- **DHCP moved the machine** .243 → .244 mid-session and dropped SSH. Needs a reservation
  or mDNS; `avahi-daemon` is installed but `.local` resolution failed from Windows.

### Open items carried forward

- [ ] DHCP reservation for `radio-deck`
- [ ] WiFi power save disabled via systemd oneshot (no NetworkManager on Ubuntu Server)
- [ ] Map physical USB ports to buses and label the case — the two radios must land on
      different `480M` root hubs

---

## Phase 1 — blocked

Waiting on Airspy R2 delivery. Procedure in `docs/phase1-detail.md`.
Record on completion: **serial, firmware version, gain setting, ppm error.**

The deck now records its own evidence for two of those. The serial is read back off the
device and stored in `run_receivers.serial`, so a run states which radio produced it
rather than which one was asked for. And every analysed event stores `freq_raw_hz`, the
measured centre before it is snapped to the 6.25 kHz grid — so ppm error is a query
against a known transmitter rather than a separate measurement:

```sql
SELECT ROUND(AVG(freq_raw_hz - freq_hz), 1) AS mean_error_hz,
       ROUND(AVG(freq_raw_hz - freq_hz) / (freq_hz / 1e6), 3) AS ppm
FROM events WHERE freq_hz = <the known transmitter> AND freq_raw_hz IS NOT NULL;
```

Both are untested against hardware. Nothing here has seen a real signal.
