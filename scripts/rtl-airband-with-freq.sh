#!/bin/bash
set -euo pipefail

CONFIG_DIR="${CONFIG_DIR:-/usr/local/etc}"
CONF="${CONFIG_SYMLINK:-${CONFIG_DIR}/rtl_airband.conf}"
if [[ $# -gt 0 ]]; then
  CONF="$1"
fi
OUT="/run/rtl_airband_last_freq.txt"
OUT_AIR="/run/rtl_airband_last_freq_airband.txt"
OUT_GROUND="/run/rtl_airband_last_freq_ground.txt"

mkdir -p /run
echo "-" > "$OUT"
echo "-" > "$OUT_AIR"
echo "-" > "$OUT_GROUND"

trap "exit 0" TERM INT

run_airband() {
  /usr/local/bin/rtl_airband -F -c "$CONF"
}

tail_pid=""
cleanup() {
  if [[ -n "${tail_pid}" ]]; then
    kill "${tail_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

stdbuf -oL -eL journalctl -t rtl_airband -f -n 0 -o cat --no-pager \
| stdbuf -oL awk -v OUT="$OUT" -v OUT_AIR="$OUT_AIR" -v OUT_GROUND="$OUT_GROUND" '
  {
    if ($0 ~ /Activity on [0-9]+\.[0-9]+/) {
      freq = $0;
      sub(/.*Activity on /, "", freq);
      sub(/ .*/, "", freq);
      if (freq != last) {
        last = freq;
        print last > OUT;
        fflush(OUT);
        close(OUT);
        f = freq + 0.0;
        if (f >= 118.0 && f <= 136.0) {
          print freq > OUT_AIR;
          fflush(OUT_AIR);
          close(OUT_AIR);
        } else {
          print freq > OUT_GROUND;
          fflush(OUT_GROUND);
          close(OUT_GROUND);
        }
      }
    }
  }
' &
tail_pid=$!

run_airband
