#!/bin/bash
set -euo pipefail

OUT_AIRBAND="/run/rtl_airband_last_freq_airband.txt"
OUT_GROUND="/run/rtl_airband_last_freq_ground.txt"

mkdir -p /run

run_tail() {
  local unit="$1"
  local out="$2"

  while true; do
    stdbuf -oL -eL journalctl -u "$unit" -f -n 0 -o cat --no-pager \
    | stdbuf -oL awk -v OUT="$out" '
      {
        if ($0 ~ /Activity on [0-9]+\.[0-9]+/) {
          freq = $0;
          sub(/.*Activity on /, "", freq);
          sub(/ .*/, "", freq);
          print freq > OUT;
          fflush(OUT);
          close(OUT);
        }
      }
    ' || true
    sleep 1
  done
}

run_tail "rtl-airband" "$OUT_AIRBAND" &
run_tail "rtl-airband-ground" "$OUT_GROUND" &
wait
