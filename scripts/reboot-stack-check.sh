#!/bin/bash
set -euo pipefail

WAIT_SEC="${1:-90}"
POLL_SEC="${STACK_CHECK_POLL_SEC:-3}"
CHECK_DIGITAL_MOUNTS="${CHECK_DIGITAL_MOUNTS:-1}"
ICECAST_BASE_URL="${ICECAST_BASE_URL:-http://127.0.0.1:8000}"
STATUS_URL="${STATUS_URL:-http://127.0.0.1:5050/api/status}"

UNITS=(
  "icecast2"
  "rtl-airband"
  "scanner-digital"
  "scanner-digital-mixer"
  "airband-ui"
)

all_units_active() {
  local unit
  for unit in "${UNITS[@]}"; do
    if ! systemctl is-active --quiet "$unit"; then
      return 1
    fi
  done
  return 0
}

echo "[stack-check] waiting up to ${WAIT_SEC}s for services..."
deadline=$((SECONDS + WAIT_SEC))
while ! all_units_active; do
  if (( SECONDS >= deadline )); then
    echo "[stack-check] timeout waiting for services"
    break
  fi
  sleep "$POLL_SEC"
done

failed=0
echo "[stack-check] service states:"
for unit in "${UNITS[@]}"; do
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  printf "  - %s: active=%s enabled=%s\n" "$unit" "$state" "$enabled"
  if [[ "$state" != "active" ]]; then
    failed=1
  fi
done

MOUNTS=("scannerbox.mp3")
if [[ "$CHECK_DIGITAL_MOUNTS" == "1" ]]; then
  MOUNTS+=("GND-air.mp3" "DIGITAL.mp3")
fi

echo "[stack-check] icecast mounts:"
for mount in "${MOUNTS[@]}"; do
  url="${ICECAST_BASE_URL}/${mount}"
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$url" || true)"
  printf "  - /%s -> HTTP %s\n" "$mount" "$code"
  if [[ "$code" != "200" ]]; then
    failed=1
  fi
done

echo "[stack-check] ui status summary:"
status_json="$(curl -sS --max-time 4 "$STATUS_URL" || true)"
if [[ -z "$status_json" ]]; then
  echo "  - ERROR: unable to read $STATUS_URL"
  failed=1
else
  python3 - "$status_json" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
fields = [
    "rtl_active",
    "ground_active",
    "digital_active",
    "digital_mixer_active",
    "icecast_active",
    "profile_airband",
    "profile_ground",
    "digital_profile",
]
for key in fields:
    print(f"  - {key}: {payload.get(key)}")
PY
fi

if (( failed != 0 )); then
  exit 1
fi

echo "[stack-check] PASS"
