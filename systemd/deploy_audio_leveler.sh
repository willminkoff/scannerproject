#!/bin/bash
# systemd/deploy_audio_leveler.sh — install + (re)start scanner-audio-leveler
# on Micro. Run ON the host from the repo dir AFTER `git pull` (the daemon code
# ui/audio_leveler.py ships via git, this just wires up the systemd unit).
#
# Idempotent: re-running re-copies the unit, reloads, and restarts. Safe — the
# leveler only sets volume on existing VLC sink-inputs; it never touches the
# VLC services themselves.
#
#   ./systemd/deploy_audio_leveler.sh           # install + enable + start + verify
#   ./systemd/deploy_audio_leveler.sh --status  # just show status + recent logs

set -euo pipefail

UNIT="scanner-audio-leveler.service"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${REPO}/systemd/${UNIT}"
DEST="/etc/systemd/system/${UNIT}"

show_status() {
    echo "=== status ==="
    systemctl --no-pager status "$UNIT" 2>&1 | head -12 || true
    echo "=== recent logs (last 20 JSON lines) ==="
    journalctl -u "$UNIT" --no-pager -n 20 2>/dev/null || true
}

if [[ "${1:-}" == "--status" ]]; then
    show_status
    exit 0
fi

if [[ ! -f "$SRC" ]]; then
    echo "ERROR: unit not found at $SRC (did you git pull?)" >&2
    exit 1
fi
if [[ ! -f "${REPO}/ui/audio_leveler.py" ]]; then
    echo "ERROR: ${REPO}/ui/audio_leveler.py missing (did you git pull?)" >&2
    exit 1
fi

echo "Installing ${UNIT} -> ${DEST}"
sudo cp "$SRC" "$DEST"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT"
sudo systemctl restart "$UNIT"

sleep 2
show_status

# Loud confirmation that it actually resolved + applied (or why not).
echo "=== quick health check ==="
if systemctl is-active --quiet "$UNIT"; then
    echo "OK: ${UNIT} active."
else
    echo "WARN: ${UNIT} not active — see logs above." >&2
    exit 2
fi
