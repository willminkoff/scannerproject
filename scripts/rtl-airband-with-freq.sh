#!/bin/bash
set -euo pipefail

CONF="/usr/local/etc/rtl_airband.conf"
OUT="/run/rtl_airband_last_freq.txt"

mkdir -p /run
echo "-" > "$OUT"

trap "exit 0" TERM INT

run_airband() {
  /usr/local/bin/rtl_airband -f -c "$CONF"
}

run_airband 2>&1 \
| stdbuf -oL tr "\r" "\n" \
| stdbuf -oL tr -cd "\11\12\40-\176" \
| stdbuf -oL awk -v OUT="$OUT" '
  {
    print;
    if ($0 ~ /Activity on [0-9]+\.[0-9]+/) {
      freq = $0;
      sub(/.*Activity on /, "", freq);
      sub(/ .*/, "", freq);
      if (freq != last) {
        last = freq;
        print last > OUT;
        fflush(OUT);
        close(OUT);
      }
    }
  }
'
