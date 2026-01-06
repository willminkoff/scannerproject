#!/bin/bash
set -euo pipefail

CONF="/usr/local/etc/rtl_airband.conf"
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
  /usr/local/bin/rtl_airband -F -e -c "$CONF"
}

run_airband 2>&1 \
| stdbuf -oL awk -v OUT="$OUT" -v OUT_AIR="$OUT_AIR" -v OUT_GROUND="$OUT_GROUND" '
  {
    line = $0;
    gsub(/\[[0-9;]*[A-Za-z]/, "", line);
    print line;
    if (line ~ /Activity on [0-9]+\.[0-9]+/) {
      freq = line;
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
'
