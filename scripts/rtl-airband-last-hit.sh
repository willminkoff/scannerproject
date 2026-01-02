#!/bin/bash
set -euo pipefail

OUT="/run/rtl_airband_last_freq.txt"

mkdir -p /run

stdbuf -oL -eL journalctl -u rtl-airband -f -n 0 -o cat --no-pager \
| stdbuf -oL awk -v OUT="$OUT" '
  {
    if (match($0, /[0-9]+\.[0-9]+/)) {
      freq = substr($0, RSTART, RLENGTH);
      print freq > OUT;
      fflush(OUT);
    }
  }
'
