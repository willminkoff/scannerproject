#!/usr/bin/env bash
set -euo pipefail

BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:5050}"
TAILSCALE_DOMAIN="${TAILSCALE_DOMAIN:-}"
HTTP_HEALTH_URL="${HTTP_HEALTH_URL:-http://127.0.0.1:5050/api/status}"

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
require_cmd timeout

if ! tailscale status >/dev/null 2>&1; then
  echo "ERROR: tailscaled is not running or not authenticated." >&2
  exit 1
fi

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

run_serve_cmd() {
  local timeout_sec="$1"
  shift
  local out rc
  out="$(timeout "${timeout_sec}" tailscale "$@" 2>&1)"
  rc=$?
  if (( rc != 0 )) && grep -qi "Access denied: serve config denied" <<<"${out}"; then
    if command -v sudo >/dev/null 2>&1; then
      local sudo_out sudo_rc
      sudo_out="$(timeout "${timeout_sec}" sudo -n tailscale "$@" 2>&1)"
      sudo_rc=$?
      if (( sudo_rc == 0 )); then
        printf "%s" "${sudo_out}"
        return 0
      fi
      out="${out}"$'\n'"${sudo_out}"
      rc="${sudo_rc}"
    fi
  fi
  printf "%s" "${out}"
  return "${rc}"
}

show_serve_status() {
  local out rc
  out="$(tailscale serve status 2>&1)"
  rc=$?
  if (( rc != 0 )) && grep -qi "Access denied: serve config denied" <<<"${out}"; then
    if command -v sudo >/dev/null 2>&1; then
      out="$(sudo -n tailscale serve status 2>&1)"
      rc=$?
    fi
  fi
  printf "%s" "${out}"
  return "${rc}"
}

echo "Enabling Tailscale HTTPS proxy: ${TAILSCALE_DOMAIN} -> ${BACKEND_URL}"
serve_timeout_sec="${TS_SERVE_TIMEOUT_SEC:-20}"
set +e
serve_out="$(run_serve_cmd "${serve_timeout_sec}" serve --yes --bg "${BACKEND_URL}")"
serve_rc=$?
set -e
if (( serve_rc != 0 )); then
  if (( serve_rc == 124 )); then
    echo "ERROR: tailscale serve timed out after ${serve_timeout_sec}s." >&2
  fi
  echo "${serve_out}" >&2
  if grep -qi "Serve is not enabled on your tailnet" <<<"${serve_out}"; then
    echo "ERROR: Tailnet Serve is disabled. Enable it in Tailscale admin, then rerun this script." >&2
  fi
  exit "${serve_rc}"
fi

if [[ -n "${serve_out}" ]]; then
  echo "${serve_out}"
fi

echo
echo "Current tailscale serve status:"
show_serve_status
echo

https_url="https://${TAILSCALE_DOMAIN}"
https_code="$(curl -sS -m 12 -o /tmp/tailscale-https-status.$$ -w "%{http_code}" "${https_url}/api/status" || true)"
if [[ "${https_code}" != "200" ]]; then
  echo "ERROR: HTTPS health check failed for ${https_url}/api/status (HTTP ${https_code})." >&2
  if [[ -s /tmp/tailscale-https-status.$$ ]]; then
    cat /tmp/tailscale-https-status.$$ >&2
  fi
  rm -f /tmp/tailscale-https-status.$$
  exit 1
fi
python3 - <<'PY' /tmp/tailscale-https-status.$$
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
if "rtl_active" not in payload:
    raise SystemExit("missing rtl_active in HTTPS /api/status response")
PY
rm -f /tmp/tailscale-https-status.$$

http_code="$(curl -sS -m 8 -o /tmp/tailscale-http-status.$$ -w "%{http_code}" "${HTTP_HEALTH_URL}" || true)"
if [[ "${http_code}" != "200" ]]; then
  echo "ERROR: local HTTP health check failed for ${HTTP_HEALTH_URL} (HTTP ${http_code})." >&2
  rm -f /tmp/tailscale-http-status.$$
  exit 1
fi
rm -f /tmp/tailscale-http-status.$$

echo "PASS: HTTPS enabled at ${https_url}"
echo "      SB3: ${https_url}/sb3"
echo "      HP3: ${https_url}/hp3"
echo "      LAN HTTP remains available on :5050"
