#!/bin/bash
set -euo pipefail

FREQ="${DMR_DEFAULT_FREQ:-461.0375}"
DURATION="${DMR_SMOKE_DURATION:-10}"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

set +e
DMR_DEFAULT_FREQ="$FREQ" timeout "$DURATION" bash -lc './rtl_fm_dmr.sh | ./dsd_wrapper.sh' > "$TMP" 2>/dev/null
set -e

if [[ ! -s "$TMP" ]]; then
  echo "FAIL: no audio bytes emitted"
  exit 1
fi

nonzero=$(tr -d '\000' < "$TMP" | wc -c | tr -d ' ')
if [[ "$nonzero" -gt 0 ]]; then
  echo "PASS"
  exit 0
fi

echo "FAIL: only zero bytes emitted"
exit 1
