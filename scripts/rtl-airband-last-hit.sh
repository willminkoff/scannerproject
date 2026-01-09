#!/bin/bash
set -euo pipefail

OUT_AIRBAND="/run/rtl_airband_last_freq_airband.txt"
OUT_GROUND="/run/rtl_airband_last_freq_ground.txt"

mkdir -p /run

# Initialize files
echo "-" > "$OUT_AIRBAND"
echo "-" > "$OUT_GROUND"

# Monitor rtl-airband unit for both airband and ground frequencies
# Filter by frequency range: airband is 118-136 MHz, ground is everything else
stdbuf -oL -eL journalctl -u rtl-airband -f -n 0 -o cat --no-pager \
| stdbuf -oL awk -v OUT_AIR="$OUT_AIRBAND" -v OUT_GND="$OUT_GROUND" '
  {
    if ($0 ~ /Activity on [0-9]+\.[0-9]+/) {
      freq = $0;
      sub(/.*Activity on /, "", freq);
      sub(/ .*/, "", freq);
      freq_num = freq + 0.0;
      
      # Airband: 118.0 - 136.0 MHz
      if (freq_num >= 118.0 && freq_num <= 136.0) {
        print freq > OUT_AIR;
        fflush(OUT_AIR);
        close(OUT_AIR);
      }
      # Ground: everything else
      else {
        print freq > OUT_GND;
        fflush(OUT_GND);
        close(OUT_GND);
      }
    }
  }
'
