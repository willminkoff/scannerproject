#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_STATUS_XSL="${ROOT_DIR}/icecast/status.xsl"
LOCAL_LISTEN_HTML="${ROOT_DIR}/icecast/listen.html"

PI_USER="${PI_USER:-willminkoff}"
PI_HOST="${PI_HOST:-sprontpi.local}"
REMOTE_STATUS_XSL="${REMOTE_STATUS_XSL:-/usr/share/icecast2/web/status.xsl}"
REMOTE_LISTEN_HTML="${REMOTE_LISTEN_HTML:-/usr/share/icecast2/web/listen.html}"
TMP_REMOTE_FILE="/tmp/status.xsl.$$"
TMP_REMOTE_LISTEN="/tmp/listen.html.$$"

if [[ ! -f "${LOCAL_STATUS_XSL}" ]]; then
  echo "ERROR: Missing template: ${LOCAL_STATUS_XSL}" >&2
  exit 1
fi
if [[ ! -f "${LOCAL_LISTEN_HTML}" ]]; then
  echo "ERROR: Missing listener page: ${LOCAL_LISTEN_HTML}" >&2
  exit 1
fi

echo "Deploying Icecast web templates to ${PI_USER}@${PI_HOST}"

scp "${LOCAL_STATUS_XSL}" "${PI_USER}@${PI_HOST}:${TMP_REMOTE_FILE}"
scp "${LOCAL_LISTEN_HTML}" "${PI_USER}@${PI_HOST}:${TMP_REMOTE_LISTEN}"

ssh "${PI_USER}@${PI_HOST}" "set -euo pipefail
  sudo cp \"${REMOTE_STATUS_XSL}\" \"${REMOTE_STATUS_XSL}.bak.\$(date +%Y%m%d-%H%M%S)\"
  sudo cp \"${REMOTE_LISTEN_HTML}\" \"${REMOTE_LISTEN_HTML}.bak.\$(date +%Y%m%d-%H%M%S)\" 2>/dev/null || true
  sudo install -m 0644 \"${TMP_REMOTE_FILE}\" \"${REMOTE_STATUS_XSL}\"
  sudo install -m 0644 \"${TMP_REMOTE_LISTEN}\" \"${REMOTE_LISTEN_HTML}\"
  rm -f \"${TMP_REMOTE_FILE}\"
  rm -f \"${TMP_REMOTE_LISTEN}\"
  for _ in \$(seq 1 15); do
    if curl -fsS http://127.0.0.1:8000/status.xsl | grep -q 'listen.html?mount='; then
      break
    fi
    sleep 1
  done
  curl -fsS http://127.0.0.1:8000/status.xsl | grep -q 'listen.html?mount='
  curl -fsS http://127.0.0.1:8000/listen.html | grep -q 'Stream Player'
"

echo "Done. Icecast status and listener page updated and verified."
