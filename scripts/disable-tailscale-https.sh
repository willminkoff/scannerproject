#!/usr/bin/env bash
set -euo pipefail

HTTP_HEALTH_URL="${HTTP_HEALTH_URL:-http://127.0.0.1:5050/api/status}"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "ERROR: missing required command: tailscale" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: missing required command: curl" >&2
  exit 1
fi

run_serve_reset() {
  local out rc
  out="$(tailscale serve reset 2>&1)"
  rc=$?
  if (( rc != 0 )) && grep -qi "Access denied: serve config denied" <<<"${out}"; then
    if command -v sudo >/dev/null 2>&1; then
      local sudo_out sudo_rc
      sudo_out="$(sudo -n tailscale serve reset 2>&1)"
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

echo "Resetting tailscale serve configuration..."
reset_out="$(run_serve_reset)"
if [[ -n "${reset_out}" ]]; then
  echo "${reset_out}"
fi

echo
echo "Current tailscale serve status:"
show_serve_status || true
echo

http_code="$(curl -sS -m 8 -o /tmp/tailscale-http-status.$$ -w "%{http_code}" "${HTTP_HEALTH_URL}" || true)"
if [[ "${http_code}" != "200" ]]; then
  echo "ERROR: local HTTP health check failed for ${HTTP_HEALTH_URL} (HTTP ${http_code})." >&2
  rm -f /tmp/tailscale-http-status.$$
  exit 1
fi
rm -f /tmp/tailscale-http-status.$$

echo "PASS: tailscale HTTPS serve disabled; local HTTP UI is still healthy."
