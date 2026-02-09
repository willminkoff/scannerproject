#!/bin/bash
set -euo pipefail

DSD_BIN="${DSD_BIN:-dsd-fme}"
DMR_DSD_ARGS="${DMR_DSD_ARGS:-}"
DMR_DSD_GUARD="${DMR_DSD_GUARD:-1}"

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
  echo "$help" | grep -qE -- "(^|[[:space:]])${flag}([[:space:]]|,|$)"
}

supports_text() {
  local text="$1"
  echo "$help" | grep -qi "$text"
}

# Input/output from stdin/stdout when supported
if supports_flag "-i"; then
  args+=("-i" "-")
else
  echo "[dmr-dsd] error: -i not supported; set DMR_DSD_ARGS to read stdin" >&2
  exit 1
fi

if supports_flag "-o"; then
  args+=("-o" "-")
else
  echo "[dmr-dsd] error: -o not supported; set DMR_DSD_ARGS to write stdout" >&2
  exit 1
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
elif echo "$help" | grep -qE -- "--auto"; then
  args+=("--auto")
fi

# Quiet / low-verbosity
if supports_flag "-q" && supports_text "quiet"; then
  args+=("-q")
elif echo "$help" | grep -qE -- "--quiet"; then
  args+=("--quiet")
elif supports_flag "-v" && supports_text "verbose"; then
  args+=("-v" "0")
fi

# Explicit raw PCM output when supported
raw_set=0
if echo "$help" | grep -qE -- "--out-raw"; then
  args+=("--out-raw")
  raw_set=1
elif echo "$help" | grep -qE -- "--output-raw"; then
  args+=("--output-raw")
  raw_set=1
elif echo "$help" | grep -qE -- "--raw" && supports_text "raw"; then
  args+=("--raw")
  raw_set=1
elif supports_flag "-R" && supports_text "raw"; then
  args+=("-R")
  raw_set=1
elif echo "$help" | grep -qE -- "--output-format" && supports_text "raw"; then
  args+=("--output-format" "raw")
  raw_set=1
elif echo "$help" | grep -qE -- "--out-fmt" && supports_text "raw"; then
  args+=("--out-fmt" "raw")
  raw_set=1
fi

if [[ "$raw_set" -ne 1 ]]; then
  echo "[dmr-dsd] error: unable to enforce raw PCM output; set DMR_DSD_ARGS explicitly" >&2
  exit 1
fi

if [[ "$DMR_DSD_GUARD" == "1" ]]; then
  exec "$DSD_BIN" "${args[@]}" | python3 - <<'PY'
import sys

buf = sys.stdin.buffer
out = sys.stdout.buffer
head = buf.read(12)
if not head:
    sys.exit(0)
if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
    # Drop standard 44-byte WAV header
    rest = buf.read(32)
    sys.stderr.write("[dmr-dsd] warning: WAV header detected; stripping\n")
else:
    out.write(head)
    out.flush()

while True:
    chunk = buf.read(8192)
    if not chunk:
        break
    out.write(chunk)
PY
fi

exec "$DSD_BIN" "${args[@]}"
