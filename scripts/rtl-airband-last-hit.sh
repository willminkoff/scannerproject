#!/bin/bash
set -euo pipefail

OUT_AIRBAND="/run/rtl_airband_last_freq_airband.txt"
OUT_GROUND="/run/rtl_airband_last_freq_ground.txt"

mkdir -p /run

# Function to process activity line
process_activity() {
  local line="$1"
  if [[ $line =~ Activity\ on\ ([0-9]+\.[0-9]+) ]]; then
    freq="${BASH_REMATCH[1]}"
    freq_num=$(echo "$freq" | awk '{print $1 + 0}')
    
    # Airband: 118.0 - 136.0 MHz
    if (( $(echo "$freq_num >= 118.0 && $freq_num <= 136.0" | bc -l) )); then
      echo "$freq" > "$OUT_AIRBAND"
    # Ground: everything else
    else
      echo "$freq" > "$OUT_GROUND"
    fi
  fi
}

# Initialize files
echo "-" > "$OUT_AIRBAND"
echo "-" > "$OUT_GROUND"

# First, read recent history (last 100 lines)
journalctl -u rtl-airband -n 100 -o cat --no-pager | while read -r line; do
  process_activity "$line"
done

# Then follow new entries in real time
journalctl -u rtl-airband -f -n 0 -o cat --no-pager | while read -r line; do
  process_activity "$line"
done
