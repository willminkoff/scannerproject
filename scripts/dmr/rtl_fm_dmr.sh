#!/bin/bash
set -euo pipefail

DMR_TUNE_PATH="${DMR_TUNE_PATH:-/run/dmr_tune_freq.txt}"
DMR_PROFILE_PATH="${DMR_PROFILE_PATH:-${PROFILES_DIR:-/usr/local/etc/airband-profiles}/rtl_airband_dmr_nashville.conf}"
DMR_DEFAULT_FREQ="${DMR_DEFAULT_FREQ:-}"
DMR_TUNE_POLL="${DMR_TUNE_POLL:-0.5}"

RTL_FM_BIN="${RTL_FM_BIN:-/usr/bin/rtl_fm}"
DMR_RTL_DEVICE="${DMR_RTL_DEVICE:-}"
DMR_PPM="${DMR_PPM:-}"
DMR_RTL_FM_ARGS="${DMR_RTL_FM_ARGS:--M fm -s 48000 -g 25 -l 0}"

log() {
  echo "[dmr-rtl_fm] $*" >&2
}

normalize_freq() {
  local val="$1"
  awk -v f="$val" 'BEGIN{if(f=="" || f=="-") exit 1; printf "%.4f", f+0}'
}

freq_to_hz() {
  local val="$1"
  awk -v f="$val" 'BEGIN{if(f<1000000) printf "%.0f", f*1000000; else printf "%.0f", f}'
}

first_profile_freq() {
  python3 - "$DMR_PROFILE_PATH" <<'PY'
import re
import sys

path = sys.argv[1]
try:
    text = open(path, "r", encoding="utf-8", errors="ignore").read()
except FileNotFoundError:
    sys.exit(0)

for block in re.findall(r"freqs\s*=\s*\((.*?)\)\s*;", text, re.S | re.I):
    m = re.search(r"(\d+(?:\.\d+)?)", block)
    if m:
        print(m.group(1))
        sys.exit(0)
PY
}

read_tune() {
  if [[ -f "$DMR_TUNE_PATH" ]]; then
    head -n1 "$DMR_TUNE_PATH" | tr -d '\r\n'
    return
  fi
  echo ""
}

resolve_start_freq() {
  local raw=""
  if [[ -n "$DMR_DEFAULT_FREQ" ]]; then
    raw="$DMR_DEFAULT_FREQ"
  else
    raw=$(read_tune)
    if [[ -z "$raw" ]]; then
      raw=$(first_profile_freq)
    fi
  fi
  if nf=$(normalize_freq "$raw" 2>/dev/null); then
    echo "$nf"
  else
    echo ""
  fi
}

build_rtl_args() {
  local args=()
  if [[ -n "$DMR_RTL_DEVICE" ]]; then
    args+=("-d" "$DMR_RTL_DEVICE")
  fi
  if [[ -n "$DMR_PPM" ]]; then
    args+=("-p" "$DMR_PPM")
  fi
  read -r -a extra <<< "$DMR_RTL_FM_ARGS"
  args+=("${extra[@]}")
  printf '%s\n' "${args[@]}"
}

rtl_pid=""
current_freq=""

action_start() {
  local mhz="$1"
  local hz
  hz=$(freq_to_hz "$mhz")
  mapfile -t args < <(build_rtl_args)
  log "Starting rtl_fm on ${mhz} MHz (${hz} Hz)"
  "$RTL_FM_BIN" -f "$hz" "${args[@]}" &
  rtl_pid=$!
}

action_stop() {
  if [[ -n "$rtl_pid" ]] && kill -0 "$rtl_pid" 2>/dev/null; then
    kill "$rtl_pid" 2>/dev/null || true
    wait "$rtl_pid" 2>/dev/null || true
  fi
  rtl_pid=""
}

cleanup() {
  action_stop
}
trap cleanup EXIT INT TERM

start_freq=$(resolve_start_freq)
while [[ -z "$start_freq" ]]; do
  log "No DMR tune frequency available. Set DMR_DEFAULT_FREQ or write $DMR_TUNE_PATH"
  sleep 1
  start_freq=$(resolve_start_freq)
done
current_freq="$start_freq"
action_start "$current_freq"

while true; do
  desired_raw=$(read_tune)
  if desired_norm=$(normalize_freq "$desired_raw" 2>/dev/null); then
    if [[ "$desired_norm" != "$current_freq" ]]; then
      current_freq="$desired_norm"
      action_stop
      action_start "$current_freq"
    fi
  fi
  if [[ -z "$rtl_pid" ]] || ! kill -0 "$rtl_pid" 2>/dev/null; then
    action_start "$current_freq"
  fi
  sleep "$DMR_TUNE_POLL"
done
