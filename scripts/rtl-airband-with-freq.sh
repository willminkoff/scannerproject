#!/bin/bash
set -euo pipefail

CONF="/usr/local/etc/rtl_airband.conf"
OUT="/run/rtl_airband_last_freq.txt"

mkdir -p /run
echo "-" > "$OUT"

trap "exit 0" TERM INT

run_airband() {
  /usr/local/bin/rtl_airband -F -e -c "$CONF"
}

run_airband 2>&1 \
| stdbuf -oL awk -v OUT="$OUT" '
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
      }
    }
  }
'
