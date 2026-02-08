#!/bin/bash
set -euo pipefail

DMR_DSD_CMD="${DMR_DSD_CMD:-}"

if [[ -n "$DMR_DSD_CMD" ]]; then
  exec bash -lc "$DMR_DSD_CMD"
fi

echo "[dmr-dsd] DMR_DSD_CMD not set; passing through raw audio" >&2
exec cat
