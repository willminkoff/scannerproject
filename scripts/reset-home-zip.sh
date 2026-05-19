#!/usr/bin/env bash
# EMERGENCY-ONLY. The SB3 UI Travel Mode toggle is the primary control —
# turning it OFF in the UI snaps the ZIP back to home automatically.
#
# Use this script only when:
#   - the UI is unreachable but you have shell access, or
#   - you need to script the reset (cron, recovery automation, etc.)
#
# Calls the local /api/hp/state endpoint over loopback (no auth needed, tailnet
# trust) and forces zip to HOME_ZIP. Leaves use_location, strict_location, and
# range_miles untouched.
#
# Usage:
#   ./scripts/reset-home-zip.sh
#   HOME_ZIP=37205 ./scripts/reset-home-zip.sh
set -euo pipefail

HOME_ZIP="${HOME_ZIP:-37221}"
UI_PORT="${UI_PORT:-5050}"
UI_HOST="${UI_HOST:-127.0.0.1}"

if [[ ! "${HOME_ZIP}" =~ ^[0-9]{5}$ ]]; then
  echo "ERROR: HOME_ZIP must be 5 digits, got: ${HOME_ZIP}" >&2
  exit 1
fi

echo "Resetting SB3 ZIP to ${HOME_ZIP} via http://${UI_HOST}:${UI_PORT}/api/hp/state"

http_code="$(curl -sS -m 10 -o /tmp/reset-home-zip.$$ -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -X POST \
  -d "{\"zip\": \"${HOME_ZIP}\", \"resolve_zip\": true}" \
  "http://${UI_HOST}:${UI_PORT}/api/hp/state" || true)"

if [[ "${http_code}" != "200" ]]; then
  echo "ERROR: reset failed (HTTP ${http_code})" >&2
  if [[ -s /tmp/reset-home-zip.$$ ]]; then
    cat /tmp/reset-home-zip.$$ >&2
    echo >&2
  fi
  rm -f /tmp/reset-home-zip.$$
  exit 1
fi

cat /tmp/reset-home-zip.$$
echo
rm -f /tmp/reset-home-zip.$$
echo "PASS: home ZIP restored to ${HOME_ZIP}"
