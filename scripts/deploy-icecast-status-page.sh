#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_STATUS_XSL="${ROOT_DIR}/icecast/status.xsl"

PI_USER="${PI_USER:-willminkoff}"
PI_HOST="${PI_HOST:-sprontpi.local}"
REMOTE_STATUS_XSL="${REMOTE_STATUS_XSL:-/usr/share/icecast2/web/status.xsl}"
TMP_REMOTE_FILE="/tmp/status.xsl.$$"

if [[ ! -f "${LOCAL_STATUS_XSL}" ]]; then
  echo "ERROR: Missing template: ${LOCAL_STATUS_XSL}" >&2
  exit 1
fi

echo "Deploying ${LOCAL_STATUS_XSL} to ${PI_USER}@${PI_HOST}:${REMOTE_STATUS_XSL}"

scp "${LOCAL_STATUS_XSL}" "${PI_USER}@${PI_HOST}:${TMP_REMOTE_FILE}"

ssh "${PI_USER}@${PI_HOST}" "set -euo pipefail
  sudo cp \"${REMOTE_STATUS_XSL}\" \"${REMOTE_STATUS_XSL}.bak.\$(date +%Y%m%d-%H%M%S)\"
  sudo install -m 0644 \"${TMP_REMOTE_FILE}\" \"${REMOTE_STATUS_XSL}\"
  rm -f \"${TMP_REMOTE_FILE}\"
  sudo systemctl restart icecast2
  systemctl is-active icecast2
  for _ in \$(seq 1 15); do
    if curl -fsS http://127.0.0.1:8000/status.xsl | grep -q 'target=\"_blank\"'; then
      break
    fi
    sleep 1
  done
  curl -fsS http://127.0.0.1:8000/status.xsl | grep -q 'target=\"_blank\"'
"

echo "Done. Icecast status page updated and verified."
