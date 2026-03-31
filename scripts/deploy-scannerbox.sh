#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_USER="${PI_USER:-willminkoff}"
PI_HOST="${PI_HOST:-sprontpi.local}"
REMOTE_ROOT="${REMOTE_ROOT:-/home/willminkoff/scannerproject}"
STATUS_URL="${STATUS_URL:-http://localhost:5050/api/status}"

echo "[deploy-scannerbox] syncing UI to ${PI_USER}@${PI_HOST}:${REMOTE_ROOT}/ui/"
rsync -avz \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "${ROOT_DIR}/ui/" \
  "${PI_USER}@${PI_HOST}:${REMOTE_ROOT}/ui/"

echo "[deploy-scannerbox] rebuilding combined config on ${PI_HOST}"
ssh "${PI_USER}@${PI_HOST}" "cd '${REMOTE_ROOT}' && sudo python3 scripts/build-combined-config.py"

echo "[deploy-scannerbox] restarting rtl-airband and airband-ui on ${PI_HOST}"
ssh "${PI_USER}@${PI_HOST}" "sudo systemctl restart rtl-airband airband-ui"

echo "[deploy-scannerbox] verifying deployment on ${PI_HOST}"
ssh "${PI_USER}@${PI_HOST}" "sleep 2 && curl -fsS '${STATUS_URL}' | python3 -m json.tool | head -20"

echo "[deploy-scannerbox] done"
