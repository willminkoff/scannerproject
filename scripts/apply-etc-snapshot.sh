#!/usr/bin/env bash
# Replay the version-controlled Scannerbox /etc deploy artifacts onto the box.
# Mirrors etc/scannerbox/ -> /etc/ (systemd drop-ins, op25 curated profile, dvb
# blacklist), then reloads systemd. Idempotent. Does NOT touch secrets/data.
set -euo pipefail
# NOTE: cutover.conf has __ICECAST_SOURCE_PW__ placeholder — substitute the real
# icecast source password (see /etc/airband-ui.conf) after apply, or the chirp
# publish will 401. This snapshot intentionally omits the secret.
HERE="$(cd "$(dirname "$0")/.." && pwd)"
sudo rsync -a "$HERE/etc/scannerbox/" /etc/
sudo systemctl daemon-reload
echo "applied etc/scannerbox -> /etc + daemon-reload done"
