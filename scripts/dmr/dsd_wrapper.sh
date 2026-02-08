#!/bin/bash
set -euo pipefail

DSD_BIN="${DSD_BIN:-dsd-fme}"
DMR_DSD_ARGS="${DMR_DSD_ARGS:-}"

if ! command -v "$DSD_BIN" >/dev/null 2>&1; then
  echo "[dmr-dsd] dsd-fme not found (set DSD_BIN or install dsd-fme)" >&2
  exit 1
fi

if [[ -n "$DMR_DSD_ARGS" ]]; then
  read -r -a extra <<< "$DMR_DSD_ARGS"
  exec "$DSD_BIN" "${extra[@]}"
fi

help=$("$DSD_BIN" -h 2>&1 || true)
args=()

supports_flag() {
  local flag="$1"
  echo "$help" | grep -qE "(^|[[:space:]])${flag}([[:space:]]|,|$)"
}

supports_text() {
  local text="$1"
  echo "$help" | grep -qi "$text"
}

# Input/output from stdin/stdout when supported
if supports_flag "-i"; then
  args+=("-i" "-")
else
  echo "[dmr-dsd] warning: -i not supported; set DMR_DSD_ARGS for stdin" >&2
fi

if supports_flag "-o"; then
  args+=("-o" "-")
else
  echo "[dmr-dsd] warning: -o not supported; set DMR_DSD_ARGS for stdout" >&2
fi

# Sample format hints
if supports_flag "-r" && supports_text "rate"; then
  args+=("-r" "48000")
fi
if supports_flag "-c" && supports_text "channel"; then
  args+=("-c" "1")
fi
if supports_flag "-f" && supports_text "format"; then
  args+=("-f" "s16le")
fi

# Auto mode
if supports_flag "-a" && supports_text "auto"; then
  args+=("-a")
elif echo "$help" | grep -qE "--auto"; then
  args+=("--auto")
fi

# Quiet / low-verbosity
if supports_flag "-q" && supports_text "quiet"; then
  args+=("-q")
elif echo "$help" | grep -qE "--quiet"; then
  args+=("--quiet")
elif supports_flag "-v" && supports_text "verbose"; then
  args+=("-v" "0")
fi

exec "$DSD_BIN" "${args[@]}"
