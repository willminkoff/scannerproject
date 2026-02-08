#!/bin/bash
set -euo pipefail

DMR_PROFILE_PATH="${DMR_PROFILE_PATH:-${PROFILES_DIR:-/usr/local/etc/airband-profiles}/rtl_airband_dmr_nashville.conf}"
DMR_LAST_HIT_PATH="${DMR_LAST_HIT_PATH:-${LAST_HIT_GROUND_PATH:-/run/rtl_airband_last_freq_ground.txt}}"
DMR_TUNE_PATH="${DMR_TUNE_PATH:-/run/dmr_tune_freq.txt}"
DMR_STATE_PATH="${DMR_STATE_PATH:-/run/dmr_state.json}"
DMR_DEFAULT_FREQ="${DMR_DEFAULT_FREQ:-}"

DMR_HOLD_SECS="${DMR_HOLD_SECS:-${DMR_HOLD_SECONDS:-3.0}}"
DMR_DEBOUNCE_MS="${DMR_DEBOUNCE_MS:-250}"
DMR_MIN_DWELL_MS="${DMR_MIN_DWELL_MS:-1200}"
DMR_COOLDOWN_SECS="${DMR_COOLDOWN_SECS:-1.0}"
DMR_POLL_INTERVAL="${DMR_POLL_INTERVAL:-0.5}"

# Hold/dwell/cooldown semantics (Option A):
# - DMR_MIN_DWELL_MS: minimum time to stay on a tuned freq before switching away.
# - DMR_COOLDOWN_SECS: minimum time between *switch attempts* to a different freq.
#   This uses last_switch_attempt_ms to damp rapid A/B hit ping-pong.

log() {
  echo "[dmr-controller] $*" >&2
}

normalize_freq() {
  local val="$1"
  awk -v f="$val" 'BEGIN{if(f=="" || f=="-") exit 1; printf "%.4f", f+0}'
}

now_ms() {
  if date +%s%3N >/dev/null 2>&1; then
    date +%s%3N
  else
    python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
  fi
}

secs_to_ms() {
  local sec="$1"
  awk -v s="$sec" 'BEGIN{printf "%d", s*1000}'
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

read_hit() {
  python3 - "$DMR_LAST_HIT_PATH" <<'PY'
import os
import sys

path = sys.argv[1]
try:
    st = os.stat(path)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        line = f.readline().strip()
except FileNotFoundError:
    sys.exit(0)
except Exception:
    line = ""
print(f"{st.st_mtime_ns} {line}")
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

write_tune() {
  local freq="$1"
  local reason="$2"
  local when_ms="$3"
  if [[ "${DMR_CONTROLLER_TEST:-0}" == "1" ]]; then
    echo "TEST write ${freq}ms=${when_ms} reason=${reason}"
    return
  fi
  echo "$freq" > "$DMR_TUNE_PATH"
  write_state "$freq"
}

switch_to() {
  local freq="$1"
  local reason="$2"
  local when_ms="$3"
  last_tuned="$freq"
  last_switch_ms="$when_ms"
  hold_until_ms=$(( when_ms + HOLD_MS ))
  write_tune "$freq" "$reason" "$when_ms"
  log "switch: ${reason} ${freq}"
}

extend_hold() {
  local freq="$1"
  local when_ms="$2"
  hold_until_ms=$(( when_ms + HOLD_MS ))
  log "extend_hold ${freq}"
}

process_hit() {
  local freq="$1"
  local when_ms="$2"
  if [[ "$DMR_FREQS_OK" -ne 1 ]]; then
    return
  fi
  if [[ -z "${DMR_FREQS[$freq]:-}" ]]; then
    return
  fi

  local last_hit="${LAST_HIT_MS[$freq]:-0}"
  if (( when_ms - last_hit < DMR_DEBOUNCE_MS )); then
    return
  fi
  LAST_HIT_MS[$freq]="$when_ms"

  if [[ "$freq" == "$last_tuned" ]]; then
    extend_hold "$freq" "$when_ms"
    return
  fi

  if (( when_ms - last_switch_attempt_ms < COOLDOWN_MS )); then
    log "skip: cooldown ${freq}"
    return
  fi
  last_switch_attempt_ms="$when_ms"

  if [[ -n "$last_tuned" ]]; then
    local since_switch=$(( when_ms - last_switch_ms ))
    if (( since_switch < DMR_MIN_DWELL_MS )); then
      log "skip: min_dwell ${freq}"
      return
    fi
  fi

  switch_to "$freq" "new_hit" "$when_ms"
}

check_hold() {
  local now_ms_val="$1"
  if [[ -z "$last_tuned" ]]; then
    return
  fi
  if (( now_ms_val <= hold_until_ms )); then
    return
  fi
  if [[ -n "$DEFAULT_FREQ_NORM" && "$DEFAULT_FREQ_NORM" != "$last_tuned" ]]; then
    if (( now_ms_val - last_switch_attempt_ms < COOLDOWN_MS )); then
      log "skip: cooldown default"
      return
    fi
    last_switch_attempt_ms="$now_ms_val"
    local since_switch=$(( now_ms_val - last_switch_ms ))
    if (( since_switch < DMR_MIN_DWELL_MS )); then
      log "skip: min_dwell default"
      return
    fi
    switch_to "$DEFAULT_FREQ_NORM" "default" "$now_ms_val"
  fi
}

run_test() {
  log "TEST MODE: simulating hits (no /run writes)"
  DMR_FREQS_OK=1
  DMR_FREQS=( ["451.0000"]=1 ["451.0125"]=1 )
  LAST_HIT_MS=()
  DEFAULT_FREQ_NORM="451.0000"
  last_tuned=""
  last_switch_ms=0
  last_switch_attempt_ms=0
  hold_until_ms=0

  local events=(
    "0 451.0000"
    "200 451.0125"
    "400 451.0000"
    "600 451.0125"
    "1300 451.0125"
    "1500 451.0000"
    "2600 451.0000"
    "3600 451.0125"
    "6000 -"
    "9000 -"
  )

  for ev in "${events[@]}"; do
    local t_ms="${ev%% *}"
    local freq="${ev#* }"
    if [[ "$freq" != "-" ]]; then
      process_hit "$freq" "$t_ms"
    fi
    check_hold "$t_ms"
  done
}

mkdir -p /run

HOLD_MS=$(secs_to_ms "$DMR_HOLD_SECS")
COOLDOWN_MS=$(secs_to_ms "$DMR_COOLDOWN_SECS")

declare -A DMR_FREQS=()
declare -A LAST_HIT_MS=()
DMR_FREQS_OK=1

if [[ "${DMR_CONTROLLER_TEST:-0}" == "1" ]]; then
  run_test
  exit 0
fi

while read -r f; do
  if nf=$(normalize_freq "$f" 2>/dev/null); then
    DMR_FREQS["$nf"]=1
  fi
done < <(load_freqs)

if [[ ${#DMR_FREQS[@]} -eq 0 ]]; then
  log "error: no DMR freqs loaded from $DMR_PROFILE_PATH; controller idle"
  DMR_FREQS_OK=0
fi

DEFAULT_FREQ_NORM=""
if [[ -n "$DMR_DEFAULT_FREQ" ]]; then
  if nf=$(normalize_freq "$DMR_DEFAULT_FREQ" 2>/dev/null); then
    if [[ ${#DMR_FREQS[@]} -gt 0 && -z "${DMR_FREQS[$nf]:-}" ]]; then
      log "default freq $nf not in DMR profile; ignoring"
    else
      DEFAULT_FREQ_NORM="$nf"
    fi
  fi
fi

last_tuned=""
last_switch_ms=0
last_switch_attempt_ms=0
hold_until_ms=0
last_mtime_ns=0

while true; do
  now_ms_val=$(now_ms)
  hit_line="$(read_hit || true)"
  if [[ -n "$hit_line" ]]; then
    mtime_ns="${hit_line%% *}"
    hit_val="${hit_line#* }"
    if [[ "$mtime_ns" != "$last_mtime_ns" ]]; then
      last_mtime_ns="$mtime_ns"
      event_ms=$((mtime_ns / 1000000))
      if nf=$(normalize_freq "$hit_val" 2>/dev/null); then
        process_hit "$nf" "$event_ms"
      fi
    fi
  fi
  check_hold "$now_ms_val"
  sleep "$DMR_POLL_INTERVAL"
done
