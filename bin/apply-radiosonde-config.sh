#!/usr/bin/env bash
# apply-radiosonde-config.sh — re-apply the two auto_rx settings that make the
# radiosonde frequency scan actually complete on Neptune's RSPduo.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# Both files it edits live under tools/, which .gitignore excludes (it is a
# vendored third-party clone plus machine-local config). So the settings exist
# only on the box and a rebuild silently loses them — taking the scanner back
# to a state where it has NEVER once completed a scan. This script is the
# in-repo record, and it is idempotent so it is safe to re-run.
#
# THE TWO BUGS, AND THE MEASUREMENTS THAT PICKED THESE VALUES (2026-11-08)
# ------------------------------------------------------------------------
# auto_rx builds its scan command as (autorx/scan.py:102):
#
#     <timeout_cmd> <dwell+10> rtl_power -f <start>:<stop>:<step> -i <dwell> -1 ...
#
# so the timeout is `scan_dwell_time + 10` while the work is roughly
# `hops x dwell`. The budget never scales with hop count.
#
# 1. scan_dwell_time was 5  ->  timeout 15 s. The 400.05-406 MHz span is 3 hops
#    (rx_power caps bin bandwidth at 2.8 MHz), so 3 x 5 s is 15 s of
#    integration alone, before any SDRplay device-open or retune. Observed:
#    `rtl_power call failed with return code 124` at 15.6 s, then
#    "SDR unresponsive", looping forever. Note raising dwell helps only because
#    integration grows as dwell x hops while the budget grows as dwell + 10 --
#    at 25 the sweep measured 29.2 s against a 35 s budget.
#
# 2. The shim forced `-s 7000000`. rx_power DOES accept -s, and at 7 MS/s it
#    downsamples by 280x to reach auto_rx's 800 Hz bins:
#
#        with -s 7000000:  Dongle bandwidth 2800000Hz   Downsampling by: 280x
#        without:          Dongle bandwidth 2644444Hz   Downsampling by: 1x
#
#    That DSP load blew even the widened budget: the live scan was SIGKILLed at
#    65.3 s (rc -9 = `timeout -k 30 35`). The -s was added on 2026-10-02 to make
#    the 5.95 MHz span "fit in one tune cycle" — a sound idea that cannot work,
#    because the 2.8 MHz bin cap means 3 hops regardless of sample rate.
#
# With both applied, the live scanner completed in 27.7 s and reported:
#   Detected peaks on 5 frequencies (MHz): [403.9 405.88 401.92 405. 401.03]
# — its first successful scan ever.
#
# Both fixes are required. Either alone still fails.
#
# Usage:  bin/apply-radiosonde-config.sh [--check]
#         --check  report drift and exit non-zero; change nothing
set -euo pipefail

AUTORX_CFG="${AUTORX_CFG:-$HOME/scannerproject/tools/auto_rx/repo/auto_rx/station.cfg}"
SHIM="${SHIM:-$HOME/scannerproject/tools/auto_rx/bin/_shim.sh}"
WANT_DWELL=25

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

fail=0
note() { printf '  %s\n' "$*"; }

# ---- 1. scan_dwell_time -----------------------------------------------------
if [ ! -f "$AUTORX_CFG" ]; then
  note "MISSING $AUTORX_CFG — is auto_rx installed?"
  fail=1
else
  cur="$(sed -nE 's/^scan_dwell_time[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' "$AUTORX_CFG" | head -1)"
  if [ -z "$cur" ]; then
    note "scan_dwell_time not present in $AUTORX_CFG"
    fail=1
  elif [ "$cur" = "$WANT_DWELL" ]; then
    note "scan_dwell_time = $cur (ok)"
  elif [ "$CHECK_ONLY" = 1 ]; then
    note "DRIFT scan_dwell_time = $cur, want $WANT_DWELL"
    fail=1
  else
    cp -p "$AUTORX_CFG" "$AUTORX_CFG.bak-$(date +%Y%m%d-%H%M%S)"
    # In-place with a portable sed invocation (BSD sed needs the -i argument).
    sed -i.tmp -E "s/^(scan_dwell_time[[:space:]]*=[[:space:]]*)[0-9]+/\1$WANT_DWELL/" "$AUTORX_CFG"
    rm -f "$AUTORX_CFG.tmp"
    note "scan_dwell_time $cur -> $WANT_DWELL"
  fi
fi

# ---- 2. the shim must not force a sample rate -------------------------------
if [ ! -f "$SHIM" ]; then
  note "MISSING $SHIM"
  fail=1
elif ! grep -q 'rx_power' "$SHIM"; then
  note "no rx_power branch in $SHIM — shim layout changed, inspect by hand"
  fail=1
elif grep -qE 'rx_power".*-s[[:space:]]+[0-9]+' "$SHIM"; then
  if [ "$CHECK_ONLY" = 1 ]; then
    note "DRIFT shim still forces a sample rate on rx_power (-s)"
    fail=1
  else
    cp -p "$SHIM" "$SHIM.bak-$(date +%Y%m%d-%H%M%S)"
    sed -i.tmp -E 's/(rx_power"[[:space:]]+-d[[:space:]]+"\$SDR_DEVICE")[[:space:]]+-s[[:space:]]+[0-9]+/\1/' "$SHIM"
    rm -f "$SHIM.tmp"
    if grep -qE 'rx_power".*-s[[:space:]]+[0-9]+' "$SHIM"; then
      note "FAILED to remove -s from $SHIM — edit it by hand"
      fail=1
    else
      note "removed forced -s from the rx_power branch"
    fi
  fi
else
  note "shim does not force -s on rx_power (ok)"
fi

# ---- 3. report the derived budget -------------------------------------------
if [ -f "$AUTORX_CFG" ]; then
  d="$(sed -nE 's/^scan_dwell_time[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' "$AUTORX_CFG" | head -1)"
  lo="$(sed -nE 's/^min_freq[[:space:]]*=[[:space:]]*([0-9.]+).*/\1/p' "$AUTORX_CFG" | head -1)"
  hi="$(sed -nE 's/^max_freq[[:space:]]*=[[:space:]]*([0-9.]+).*/\1/p' "$AUTORX_CFG" | head -1)"
  if [ -n "${d:-}" ] && [ -n "${lo:-}" ] && [ -n "${hi:-}" ]; then
    python3 - "$d" "$lo" "$hi" <<'PY'
import math, sys
dwell, lo, hi = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
hops = max(1, math.ceil((hi - lo) / 2.644))   # rx_power caps a hop at ~2.8 MHz
print("  span %.2f-%.2f MHz = %d hop(s); -i %d s; timeout dwell+10 = %d s"
      % (lo, hi, hops, dwell, dwell + 10))
# -i is the integration INTERVAL, and in single-shot mode rx_power sweeps every
# hop WITHIN it -- so the cost is NOT hops*dwell. The failure mode is the
# interval being shorter than one full sweep, which rx_power's own help calls
# out: "buggy if a full sweep takes longer than the interval". It then never
# finishes and the timeout kills it. Measured on this RSPduo: 3 hops at -i 5
# hung (141 s, zero bytes); 3 hops at -i 25 completed in 29.2 s by hand and
# 27.7 s under launchd.
if hops > 1 and dwell < 20:
    print("  WARNING %d hops at -i %d s: a full sweep of this span needs more "
          "than %d s on the RSPduo, so rx_power will hang and be killed "
          "(rc 124). -i 5 was measured hanging; 25 works." % (hops, dwell, dwell))
elif hops > 1:
    print("  ok: multi-hop sweep has a %d s interval to finish in" % dwell)
PY
  fi
fi

if [ "$fail" != 0 ]; then
  [ "$CHECK_ONLY" = 1 ] && note "drift detected (see above)" || note "errors above"
  exit 1
fi
note "radiosonde scan config is correct"
