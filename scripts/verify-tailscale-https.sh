#!/usr/bin/env bash
set -euo pipefail

TAILSCALE_DOMAIN="${TAILSCALE_DOMAIN:-}"
LAN_HTTP_BASE="${LAN_HTTP_BASE:-}"
LOCAL_HTTP_BASE="${LOCAL_HTTP_BASE:-http://127.0.0.1:5050}"

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

if [[ -z "${TAILSCALE_DOMAIN}" ]]; then
  TAILSCALE_DOMAIN="$(
    tailscale status --json \
      | python3 -c 'import sys,json; print((json.load(sys.stdin).get("Self",{}) or {}).get("DNSName","").strip())'
  )"
fi
TAILSCALE_DOMAIN="${TAILSCALE_DOMAIN%.}"
if [[ -z "${TAILSCALE_DOMAIN}" ]]; then
  echo "ERROR: unable to determine tailscale DNS name. Set TAILSCALE_DOMAIN and retry." >&2
  exit 1
fi

if [[ -z "${LAN_HTTP_BASE}" ]]; then
  lan_ip="$(
    hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^(10\.|172\.|192\.168\.)' | head -n 1 || true
  )"
  if [[ -n "${lan_ip}" ]]; then
    LAN_HTTP_BASE="http://${lan_ip}:5050"
  fi
fi

status_out="$(tailscale serve status || true)"
echo "${status_out}"
if grep -q "No serve config" <<<"${status_out}"; then
  echo "ERROR: tailscale serve has no active mapping." >&2
  exit 1
fi

check_code() {
  local label="$1"
  local url="$2"
  local expected="$3"
  local code
  code="$(curl -sS -L -m 12 -o /tmp/tailscale-verify.$$ -w "%{http_code}" "${url}" || true)"
  if [[ "${code}" != "${expected}" ]]; then
    echo "ERROR: ${label} failed (HTTP ${code}) -> ${url}" >&2
    if [[ -s /tmp/tailscale-verify.$$ ]]; then
      head -c 300 /tmp/tailscale-verify.$$ >&2
      echo >&2
    fi
    rm -f /tmp/tailscale-verify.$$
    exit 1
  fi
  echo "PASS: ${label} (${code})"
  rm -f /tmp/tailscale-verify.$$
}

https_base="https://${TAILSCALE_DOMAIN}"
check_code "HTTPS /api/status" "${https_base}/api/status" "200"
check_code "HTTPS /api/hp/state" "${https_base}/api/hp/state" "200"
check_code "HTTPS /api/hp/service-types" "${https_base}/api/hp/service-types" "200"
check_code "HTTPS /sb3" "${https_base}/sb3" "200"
check_code "HTTPS /hp3 (follow redirects)" "${https_base}/hp3" "200"

check_code "Local HTTP /api/status" "${LOCAL_HTTP_BASE}/api/status" "200"
check_code "Local HTTP /sb3" "${LOCAL_HTTP_BASE}/sb3" "200"

if [[ -n "${LAN_HTTP_BASE}" ]]; then
  check_code "LAN HTTP /sb3" "${LAN_HTTP_BASE}/sb3" "200"
else
  echo "WARN: no RFC1918 LAN IP found; skipped LAN HTTP /sb3 check."
fi

echo "PASS: tailscale HTTPS verification complete."
