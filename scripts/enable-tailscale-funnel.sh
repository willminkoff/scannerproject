#!/usr/bin/env bash
# Enable public Tailscale Funnel for the SB3 UI on the configured port.
#
# Funnel exposes the local backend to the public internet, terminated by
# Tailscale's HTTPS edge. We use it so an iPhone Shortcut (off-tailnet)
# can POST travel-mode location updates to /api/hp/location/push.
#
# The first run requires Tailscale admin approval of Funnel on the tailnet.
# Re-runs are idempotent.
#
# Usage:
#   FUNNEL_PORT=5050 ./scripts/enable-tailscale-funnel.sh
#   ./scripts/enable-tailscale-funnel.sh  # defaults to 5050
set -euo pipefail

FUNNEL_PORT="${FUNNEL_PORT:-5050}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:${FUNNEL_PORT}}"
PUSH_PATH="${PUSH_PATH:-/api/hp/location/push}"

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: missing required command: $cmd" >&2
    exit 1
  fi
}

require_cmd tailscale
require_cmd curl
require_cmd python3

if ! tailscale status >/dev/null 2>&1; then
  echo "ERROR: tailscaled is not running or not authenticated." >&2
  exit 1
fi

TAILSCALE_DOMAIN="$(
  tailscale status --json \
    | python3 -c 'import sys,json; print((json.load(sys.stdin).get("Self",{}) or {}).get("DNSName","").strip())'
)"
TAILSCALE_DOMAIN="${TAILSCALE_DOMAIN%.}"
if [[ -z "${TAILSCALE_DOMAIN}" ]]; then
  echo "ERROR: unable to determine tailscale DNS name. Is the device logged in?" >&2
  exit 1
fi

run_tailscale() {
  local out rc
  set +e
  out="$(tailscale "$@" 2>&1)"
  rc=$?
  if (( rc != 0 )) && grep -qiE "Access denied|requires.*sudo" <<<"${out}"; then
    if command -v sudo >/dev/null 2>&1; then
      out="$(sudo -n tailscale "$@" 2>&1)"
      rc=$?
    fi
  fi
  set -e
  printf "%s" "${out}"
  return "${rc}"
}

echo "Enabling Tailscale Funnel: https://${TAILSCALE_DOMAIN} -> ${BACKEND_URL}"
echo
echo "(First-time enable requires Tailscale admin approval. If this fails with"
echo " 'Funnel is not enabled on your tailnet', go to https://login.tailscale.com/admin/dns"
echo " -> HTTPS Certificates + Funnel, and grant the SB3 device the funnel attribute.)"
echo

# Funnel internally calls 'serve' on the backend then exposes it publicly.
funnel_out="$(run_tailscale funnel --bg "${BACKEND_URL}")" || {
  rc=$?
  echo "${funnel_out}" >&2
  exit "${rc}"
}
if [[ -n "${funnel_out}" ]]; then
  echo "${funnel_out}"
fi

echo
echo "Current funnel status:"
run_tailscale funnel status || true
echo

PUBLIC_URL="https://${TAILSCALE_DOMAIN}"
echo "Verifying public reachability of ${PUBLIC_URL}/api/status ..."
http_code="$(curl -sS -m 15 -o /tmp/funnel-status.$$ -w "%{http_code}" "${PUBLIC_URL}/api/status" || true)"
if [[ "${http_code}" != "200" ]]; then
  echo "ERROR: HTTPS health check failed (HTTP ${http_code})." >&2
  if [[ -s /tmp/funnel-status.$$ ]]; then
    head -c 2000 /tmp/funnel-status.$$ >&2
    echo >&2
  fi
  rm -f /tmp/funnel-status.$$
  exit 1
fi
rm -f /tmp/funnel-status.$$

echo "PASS: Funnel public at ${PUBLIC_URL}"
echo "      Travel mode push: POST ${PUBLIC_URL}${PUSH_PATH}"
echo "      Required header:  X-Travel-Secret: <value from HP_LOCATION_PUSH_SECRET>"
echo "      Required body:    {\"zip\": \"NNNNN\", \"lat\": <num>, \"lon\": <num>, \"source\": \"ios_shortcut\"}"
