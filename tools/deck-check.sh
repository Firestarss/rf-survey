#!/usr/bin/env bash
# deck-check.sh — test and diagnostics for the RF survey deck
#
#   bash deck-check.sh diag              collect everything (~20 s)
#   bash deck-check.sh soak [MINUTES]    load test with peak capture, default 10
#   bash deck-check.sh all  [MINUTES]    soak, then diag
#   bash deck-check.sh watch [MINUTES]   sample without adding load (for long runs)
#
# Reads only. The soak runs stress-ng, which is the one thing that changes system
# state, and it cleans up after itself including on ctrl-C.
#
# Send the whole output. Redirect it:
#   bash deck-check.sh all > phase0-$(date +%Y%m%d-%H%M).txt 2>&1

# Everything below addresses the repository by relative path, so run from its
# root whatever directory the caller happened to be in. Without this, DB and
# the prototype are only found when invoked from exactly one place.
cd "$(dirname "$0")/.." || exit 1

MODE="${1:-diag}"
MINUTES="${2:-10}"
DB="${DB:-data/survey.sqlite}"   # the one survey database; there is no bench.sqlite
PROTO=src/survey_prototype.py

sep() { printf '\n===== %s =====\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# dmesg is often restricted on Ubuntu; fall back to journald
kmsg() {
  if dmesg >/dev/null 2>&1; then dmesg
  elif have journalctl; then journalctl -k --no-pager 2>/dev/null
  else echo "(kernel log unavailable without sudo)"
  fi
}

# ---------------------------------------------------------------------------
# sensor discovery — works on Pi OS and Ubuntu, no vcgencmd needed
# ---------------------------------------------------------------------------

find_temp_zone() {
  local best="" z
  for z in /sys/class/thermal/thermal_zone*; do
    [ -r "$z/temp" ] || continue
    case "$(cat "$z/type" 2>/dev/null)" in
      *cpu*|*soc*) echo "$z/temp"; return ;;
    esac
    [ -z "$best" ] && best="$z/temp"
  done
  [ -n "$best" ] && echo "$best"
}

TEMP_FILE="$(find_temp_zone)"
CLK_FILE=/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
MAXCLK_FILE=/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq

read_temp_mc() { [ -r "$TEMP_FILE" ] && cat "$TEMP_FILE" || echo 0; }
read_clk_khz() { [ -r "$CLK_FILE" ] && cat "$CLK_FILE" || echo 0; }

# throttle state: try every interface anyone has ever used, report which worked
THROT_SRC="none"
read_throttled() {
  local p
  for p in /sys/devices/platform/soc/soc:firmware/get_throttled \
           /sys/devices/platform/soc/*.firmware/get_throttled \
           /sys/firmware/raspberrypi/get_throttled; do
    if [ -r "$p" ]; then THROT_SRC="$p"; cat "$p"; return; fi
  done
  if have vcgencmd; then
    THROT_SRC="vcgencmd"; vcgencmd get_throttled 2>/dev/null | sed 's/throttled=//'; return
  fi
  echo ""
}

# undervoltage alarm exposed by the raspberrypi-hwmon driver
read_uv_alarm() {
  local f
  for f in /sys/class/hwmon/hwmon*/in0_lcrit_alarm; do
    [ -r "$f" ] && { cat "$f"; return; }
  done
  echo ""
}

# ---------------------------------------------------------------------------
# soak
# ---------------------------------------------------------------------------

cleanup_load() { [ -n "${STRESS_PID:-}" ] && kill "$STRESS_PID" 2>/dev/null; pkill -f 'stress-ng' 2>/dev/null; }

soak() {
  local add_load="$1" mins="$2"
  local secs; secs=$(awk -v m="$mins" 'BEGIN{printf "%d", (m*60 < 1 ? 1 : m*60)}')  # accepts fractions
  local csv="soak-$(date +%Y%m%d-%H%M%S).csv"
  local nproc_n; nproc_n=$(nproc)
  local maxclk=0
  [ -r "$MAXCLK_FILE" ] && maxclk=$(cat "$MAXCLK_FILE")

  sep "soak: ${mins} min, load=${add_load}"
  echo "sampling every 2 s -> $csv"
  [ -n "$TEMP_FILE" ] && echo "temperature source: $TEMP_FILE" || echo "WARNING: no temperature sensor found"
  if [ "$maxclk" -gt 0 ]; then
    echo "nominal max clock: $(( maxclk / 1000 )) MHz across ${nproc_n} cores"
  else
    echo "WARNING: cpufreq not readable - cannot detect throttling by clock"
  fi
  local t0_throt; t0_throt="$(read_throttled)"
  echo "throttle interface: $THROT_SRC   value at start: ${t0_throt:-unavailable}"
  echo

  if [ "$add_load" = "yes" ]; then
    if have stress-ng; then
      stress-ng --cpu "$nproc_n" --timeout "${secs}s" >/dev/null 2>&1 &
      STRESS_PID=$!
      trap cleanup_load EXIT INT TERM
      echo "stress-ng running on ${nproc_n} cores"
    else
      echo "stress-ng not installed (sudo apt install stress-ng) — running unloaded"
    fi
  fi

  echo "ts_s,temp_c,clock_mhz,load1,mem_avail_mb" > "$csv"

  local n=0 tmax=0 tmin=999000 tsum=0
  local cmin=99999999 cmax=0 csum=0 low=0
  local start; start=$(date +%s)
  local now elapsed temp clk la mem

  while :; do
    now=$(date +%s); elapsed=$(( now - start ))
    [ "$elapsed" -ge "$secs" ] && break

    temp=$(read_temp_mc); clk=$(read_clk_khz)
    la=$(awk '{print $1}' /proc/loadavg)
    mem=$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo)

    n=$(( n + 1 ))
    [ "$temp" -gt "$tmax" ] && tmax=$temp
    [ "$temp" -lt "$tmin" ] && tmin=$temp
    tsum=$(( tsum + temp ))
    [ "$clk" -gt "$cmax" ] && cmax=$clk
    [ "$clk" -lt "$cmin" ] && cmin=$clk
    csum=$(( csum + clk ))
    if [ "$maxclk" -gt 0 ] && [ "$clk" -lt $(( maxclk * 95 / 100 )) ]; then low=$(( low + 1 )); fi

    printf '%d,%.1f,%d,%s,%s\n' "$elapsed" \
      "$(awk -v t="$temp" 'BEGIN{printf "%.1f", t/1000}')" \
      "$(( clk / 1000 ))" "$la" "$mem" >> "$csv"

    # progress line every 30 s so a long run doesn't look hung
    if [ $(( elapsed % 30 )) -lt 2 ]; then
      printf '  %4ds  %5.1f C  %5d MHz  load %s\n' "$elapsed" \
        "$(awk -v t="$temp" 'BEGIN{printf "%.1f", t/1000}')" "$(( clk / 1000 ))" "$la"
    fi
    sleep 2
  done

  cleanup_load
  trap - EXIT INT TERM

  local t1_throt; t1_throt="$(read_throttled)"
  local uv; uv="$(read_uv_alarm)"

  sep "soak results"
  [ "$n" -eq 0 ] && { echo "no samples collected"; return; }

  awk -v n="$n" -v tmax="$tmax" -v tmin="$tmin" -v tsum="$tsum" \
      -v cmax="$cmax" -v cmin="$cmin" -v csum="$csum" -v low="$low" -v mx="$maxclk" '
  BEGIN {
    printf "  samples            %d over %d s\n", n, n*2
    printf "  temperature        peak %.1f C   mean %.1f C   min %.1f C\n", tmax/1000, tsum/n/1000, tmin/1000
    printf "  cpu clock          peak %d MHz   mean %d MHz   min %d MHz\n", cmax/1000, csum/n/1000, cmin/1000
    if (mx > 0)
      printf "  below 95%% of max   %d of %d samples (%.1f%%)\n", low, n, low*100/n
    print ""
    if (tmax == 0)           print "  TEMPERATURE: NO SENSOR FOUND - the reading above is meaningless"
    else if (tmax/1000 < 70) print "  TEMPERATURE: good (under 70 C)"
    else if (tmax/1000 < 80) print "  TEMPERATURE: marginal (70-80 C) - fine on the bench, watch it in a sealed case"
    else                     print "  TEMPERATURE: TOO HOT (over 80 C) - check the cooler is fitted and its fan is spinning"
    if (mx > 0 && low == 0)  print "  THROTTLING:  none - clock held at maximum throughout"
    else if (mx > 0)         printf "  THROTTLING:  clock dropped in %.1f%% of samples - see the CSV for when\n", low*100/n
  }'

  echo
  echo "  throttle register  start ${t0_throt:-n/a}  end ${t1_throt:-n/a}   (0x0 = clean)"
  if [ -n "$uv" ]; then
    [ "$uv" = "0" ] && echo "  undervoltage alarm none" || echo "  undervoltage alarm ACTIVE - check the power supply and cable"
  fi
  echo
  echo "  raw samples in $csv"
}

# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

diag() {
  sep "when and what"
  date -Is
  uname -a
  tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
  grep -E 'PRETTY_NAME|VERSION_ID' /etc/os-release

  sep "how this box is configured"
  [ -f /boot/firmware/user-data ] && echo "cloud-init present (Ubuntu-style install)"
  have vcgencmd && echo "vcgencmd: installed" || echo "vcgencmd: not installed (not needed)"
  have raspi-config && echo "raspi-config: present" || echo "raspi-config: absent (fine on Ubuntu)"
  read_throttled >/dev/null; echo "throttle interface: $THROT_SRC"
  echo "temperature source: ${TEMP_FILE:-NONE FOUND}"

  sep "cpu and memory"
  have lscpu && lscpu | grep -E 'Model name|^CPU\(s\)|MHz|Architecture'
  free -h
  uptime
  echo "governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
  echo "current:  $(( $(read_clk_khz) / 1000 )) MHz of $(( $(cat $MAXCLK_FILE 2>/dev/null || echo 0) / 1000 )) MHz"

  sep "thermal right now"
  for z in /sys/class/thermal/thermal_zone*; do
    [ -r "$z/temp" ] || continue
    printf '  %-16s %.1f C\n' "$(cat "$z/type" 2>/dev/null)" \
      "$(awk -v t="$(cat "$z/temp")" 'BEGIN{print t/1000}')"
  done
  echo "throttle: $(read_throttled)  (0x0 = clean)"
  for f in /sys/devices/platform/cooling_fan/hwmon/hwmon*/fan1_input; do
    [ -r "$f" ] && echo "fan: $(cat "$f") rpm"
  done
  for c in /sys/class/thermal/cooling_device*; do
    [ -r "$c/cur_state" ] && [ -r "$c/type" ] && \
      printf 'cooling %-16s state %s of %s\n' "$(cat "$c/type")" \
        "$(cat "$c/cur_state")" "$(cat "$c/max_state" 2>/dev/null)"
  done
  uv="$(read_uv_alarm)"; [ -n "$uv" ] && echo "undervoltage alarm: $uv (0 = fine)"

  sep "power and voltage warnings in the kernel log"
  kmsg | grep -iE 'voltage|under-volt|throttl|hwmon' | tail -30 || echo none

  sep "usb topology  <-- each Airspy must be under a DIFFERENT 480M root hub"
  if have lsusb; then lsusb -t; echo; lsusb
  else echo "lsusb not installed (sudo apt install usbutils)"; fi

  sep "usb errors"
  kmsg | grep -iE 'usb|xhci' | tail -40 || echo none

  sep "pcie and nvme"
  if have lspci; then lspci 2>/dev/null; else echo "lspci not installed (sudo apt install pciutils)"; fi
  echo
  if have lspci; then
    LNK="$(sudo -n lspci -vv 2>/dev/null | grep -E 'LnkCap:|LnkSta:')"
    if [ -n "$LNK" ]; then echo "$LNK" | sed 's/^\s*/  /'
    else echo "  link speed needs root. Run: sudo lspci -vv | grep -E 'LnkCap|LnkSta'"
    fi
  fi
  echo
  if have nvme; then sudo -n nvme smart-log /dev/nvme0 2>/dev/null | head -20 || echo "  nvme smart-log needs sudo"; fi

  sep "storage"
  have lsblk && lsblk
  df -h /
  mount | grep -E 'nvme|mmcblk' || true

  sep "boot config"
  for f in /boot/firmware/config.txt /boot/config.txt; do
    [ -f "$f" ] && { echo "--- $f"; grep -vE '^\s*#|^\s*$' "$f"; break; }
  done
  if have rpi-eeprom-config; then
    echo "--- eeprom"
    EE="$(sudo -n rpi-eeprom-config 2>/dev/null || rpi-eeprom-config 2>/dev/null)"
    echo "${EE:-(needs sudo)}"
    BO="$(echo "$EE" | sed -n 's/^BOOT_ORDER=0x//p')"
    if [ -n "$BO" ]; then
      printf '    boot order tried in this sequence: '
      echo "$BO" | rev | fold -w1 | while read -r d; do
        case "$d" in
          1) printf 'SD ' ;; 2) printf 'network ' ;; 3) printf 'rpiboot ' ;;
          4) printf 'USB ' ;; 6) printf 'NVMe ' ;; f) printf '(restart) ' ;;
          *) printf '%s? ' "$d" ;;
        esac
      done
      echo
    fi
  fi

  sep "network"
  hostname
  if have ip; then ip -brief addr; ip route | head -5
  else echo "iproute2 not installed"; fi

  sep "sdr devices"
  if have SoapySDRUtil; then SoapySDRUtil --find 2>&1; else echo "SoapySDRUtil not installed"; fi
  echo
  if have airspy_info; then airspy_info 2>&1; else echo "airspy_info not installed"; fi

  sep "sdr capabilities"
  if have SoapySDRUtil; then SoapySDRUtil --probe="driver=airspy" 2>&1 | head -60; fi

  sep "python environment"
  python3 --version
  python3 -c "import numpy, scipy; print('numpy', numpy.__version__, '/ scipy', scipy.__version__)" 2>&1
  python3 -c "import numpy; numpy.show_config()" 2>&1 | head -20

  sep "selftest"
  if [ -f "$PROTO" ]; then
    echo "running $PROTO"
    python3 "$PROTO" --selftest --rate 10e6 2>&1
  else
    echo "$PROTO not found under $(pwd) — this script must live in tools/"
  fi

  sep "database summary"
  if [ -f "$DB" ] && have sqlite3; then
    echo "file: $DB  ($(du -h "$DB" | cut -f1))"
    sqlite3 "$DB" <<'SQL'
.mode column
.headers on
SELECT (SELECT value FROM schema_meta WHERE key='version') AS schema_v,
       COUNT(*) AS events,
       SUM(overload) AS overload_flagged,
       SUM(t_end IS NULL) AS in_flight
FROM events;
SELECT '--- runs ---';
SELECT r.id, datetime(r.started_at,'unixepoch') AS started,
       COALESCE(datetime(r.ended_at,'unixepoch'),'open') AS ended,
       r.profile_name, GROUP_CONCAT(rr.receiver_id||'='||rr.serial,' ') AS receivers
FROM runs r LEFT JOIN run_receivers rr ON rr.run_id = r.id
GROUP BY r.id ORDER BY r.id DESC LIMIT 5;
SELECT '--- busiest channels ---';
SELECT ROUND(freq_hz/1000000.0, 4) AS mhz, COUNT(*) AS hits,
       ROUND(AVG(duration_s),2) AS avg_s, ROUND(AVG(snr_db),1) AS avg_snr,
       ROUND(AVG(deviation_hz),0) AS avg_dev, ctcss_hz,
       SUM(tone_state='dcs') AS dcs
FROM events GROUP BY freq_hz ORDER BY hits DESC LIMIT 25;
SELECT '--- last 15 events ---';
SELECT id, ROUND(freq_hz/1000000.0,4) AS mhz,
       COALESCE(ROUND(duration_s,2),'open') AS dur,
       ROUND(snr_db,1) AS snr, ROUND(deviation_hz,0) AS dev,
       ROUND(analyzed_s,2) AS seen, tone_state, ctcss_hz,
       CASE WHEN dcs_code IS NOT NULL
            THEN printf('%03d%s', dcs_code, COALESCE(dcs_polarity,'')) END AS dcs,
       ROUND(confidence,2) AS cap, overload AS ovl,
       audio_path IS NOT NULL AS wav
FROM events ORDER BY id DESC LIMIT 15;
SELECT '--- coverage: what was actually listened to ---';
SELECT * FROM v_coverage;
SELECT '--- what you could talk on ---';
SELECT * FROM v_contactable LIMIT 15;
SQL
  else
    echo "no database at $DB (set DB=path to point elsewhere)"
  fi
}

# ---------------------------------------------------------------------------

case "$MODE" in
  diag)  diag ;;
  soak)  soak yes "$MINUTES" ;;
  watch) soak no  "$MINUTES" ;;
  all)   soak yes "$MINUTES"; diag ;;
  *)     echo "usage: bash deck-check.sh [diag|soak|watch|all] [minutes]"; exit 1 ;;
esac

sep "end"
echo "done"
