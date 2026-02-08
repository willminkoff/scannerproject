#!/bin/bash
set -euo pipefail

DMR_PROFILE_PATH="${DMR_PROFILE_PATH:-${PROFILES_DIR:-/usr/local/etc/airband-profiles}/rtl_airband_dmr_nashville.conf}"
DMR_LAST_HIT_PATH="${DMR_LAST_HIT_PATH:-${LAST_HIT_GROUND_PATH:-/run/rtl_airband_last_freq_ground.txt}}"
DMR_TUNE_PATH="${DMR_TUNE_PATH:-/run/dmr_tune_freq.txt}"
DMR_STATE_PATH="${DMR_STATE_PATH:-/run/dmr_state.json}"
DMR_HOLD_SECONDS="${DMR_HOLD_SECONDS:-12}"
DMR_POLL_INTERVAL="${DMR_POLL_INTERVAL:-0.5}"
DMR_FREQS_REFRESH_SEC="${DMR_FREQS_REFRESH_SEC:-10}"

log() {
  echo "[dmr-controller] $*" >&2
}

normalize_freq() {
  local val="$1"
  awk -v f="$val" 'BEGIN{if(f=="" || f=="-") exit 1; printf "%.4f", f+0}'
}

profile_mtime() {
  if [[ -f "$DMR_PROFILE_PATH" ]]; then
    if stat -c %Y "$DMR_PROFILE_PATH" >/dev/null 2>&1; then
      stat -c %Y "$DMR_PROFILE_PATH"
    else
      stat -f %m "$DMR_PROFILE_PATH"
    fi
  else
    echo 0
  fi
}

load_freqs() {
  python3 - "$DMR_PROFILE_PATH" <<'PY'
import re
import sys

path = sys.argv[1]
try:
    text = open(path, "r", encoding="utf-8", errors="ignore").read()
except FileNotFoundError:
    sys.exit(0)

freqs = []
for block in re.findall(r"freqs\s*=\s*\((.*?)\)\s*;", text, re.S | re.I):
    for num in re.findall(r"\d+(?:\.\d+)?", block):
        freqs.append(num)

seen = set()
for f in freqs:
    if f not in seen:
        print(f)
        seen.add(f)
PY
}

write_state() {
  local freq="$1"
  local now
  now=$(date +%s)
  local tmp="${DMR_STATE_PATH}.tmp"
  mkdir -p "$(dirname "$DMR_STATE_PATH")" 2>/dev/null || true
  printf '{"tuned_mhz":"%s","tuned_at":%s}\n' "$freq" "$now" > "$tmp"
  mv "$tmp" "$DMR_STATE_PATH"
}

mkdir -p /run

declare -A DMR_FREQS=()
last_profile_mtime=0
last_refresh=0
last_tuned=""
hold_until=0

while true; do
  now_epoch=$(date +%s)
  if (( now_epoch - last_refresh >= DMR_FREQS_REFRESH_SEC )); then
    new_mtime=$(profile_mtime)
    if (( new_mtime != last_profile_mtime )); then
      DMR_FREQS=()
      while read -r f; do
        if nf=$(normalize_freq "$f" 2>/dev/null); then
          DMR_FREQS["$nf"]=1
        fi
      done < <(load_freqs)
      last_profile_mtime=$new_mtime
      log "Loaded ${#DMR_FREQS[@]} DMR freqs from $DMR_PROFILE_PATH"
    fi
    last_refresh=$now_epoch
  fi

  hit=""
  if [[ -f "$DMR_LAST_HIT_PATH" ]]; then
    hit=$(head -n1 "$DMR_LAST_HIT_PATH" | tr -d '\r\n')
  fi

  if nf=$(normalize_freq "$hit" 2>/dev/null); then
    if [[ -n "${DMR_FREQS[$nf]:-}" ]]; then
      if [[ "$nf" != "$last_tuned" ]]; then
        if (( now_epoch >= hold_until )); then
          echo "$nf" > "$DMR_TUNE_PATH"
          write_state "$nf"
          last_tuned="$nf"
          hold_until=$(( now_epoch + DMR_HOLD_SECONDS ))
          log "Tuned DMR to ${nf} MHz"
        fi
      else
        hold_until=$(( now_epoch + DMR_HOLD_SECONDS ))
      fi
    fi
  fi

  sleep "$DMR_POLL_INTERVAL"
done
