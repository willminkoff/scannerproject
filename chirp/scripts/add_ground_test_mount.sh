#!/bin/bash
# chirp/scripts/add_ground_test_mount.sh — add /CHIRP_GROUND_TEST.mp3 to icecast.
#
# Phase 4a sibling of add_test_mount.sh. Same idempotent + reload-only
# discipline so existing production sources (ANALOG.mp3, ANALOG_GROUND.mp3,
# DIGITAL.mp3, VFO.mp3, every keepalive) stay connected.
#
# Reverse via chirp/scripts/remove_ground_test_mount.sh.

set -euo pipefail

XML="/etc/icecast2/icecast.xml"
MOUNT="/CHIRP_GROUND_TEST.mp3"
BAK="${XML}.bak.$(date +%Y%m%d-%H%M%S)"

if [[ ! -f "$XML" ]]; then
    echo "ERROR: $XML not found" >&2
    exit 1
fi

if grep -q "<mount-name>${MOUNT}</mount-name>" "$XML"; then
    echo "Mount ${MOUNT} already present — no-op, reloading icecast anyway."
    sudo systemctl reload icecast2
    exit 0
fi

echo "Backing up ${XML} → ${BAK}"
sudo cp "$XML" "$BAK"

TMP="$(mktemp)"
sudo python3 - "$XML" "$TMP" "$MOUNT" <<'PYEOF'
import sys
src, dst, mount = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, "r", encoding="utf-8") as f:
    body = f.read()

block = f"""
  <!-- chirp Phase 4a ground test mount — added by chirp/scripts/add_ground_test_mount.sh.
       Safe to remove via chirp/scripts/remove_ground_test_mount.sh. -->
  <mount>
    <mount-name>{mount}</mount-name>
    <type>audio/mpeg</type>
  </mount>
"""

needle = "<paths>"
idx = body.find(needle)
if idx < 0:
    print("ERROR: could not find <paths> element in icecast.xml", file=sys.stderr)
    sys.exit(2)

line_start = body.rfind("\n", 0, idx) + 1
inserted = body[:line_start] + block.lstrip("\n") + "\n" + body[line_start:]

with open(dst, "w", encoding="utf-8") as f:
    f.write(inserted)
print("inserted ok")
PYEOF

sudo mv "$TMP" "$XML"
sudo chown root:icecast "$XML" 2>/dev/null || true
sudo chmod 640 "$XML" 2>/dev/null || true

echo "Validating XML..."
if command -v xmllint >/dev/null 2>&1; then
    xmllint --noout "$XML" || { echo "ERROR: xmllint failed"; exit 3; }
fi

echo "Reloading icecast2 (NOT restart — existing sources preserved)..."
sudo systemctl reload icecast2

sleep 0.5
if curl -sf -o /dev/null "http://127.0.0.1:8000/status-json.xsl"; then
    echo "icecast still up after reload."
else
    echo "WARN: status-json.xsl unreachable — check icecast logs" >&2
fi

echo "Done. Mount ${MOUNT} declared. Use a source client (chirp ground daemon) to push audio."
