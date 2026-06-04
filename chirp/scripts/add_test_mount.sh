#!/bin/bash
# chirp/scripts/add_test_mount.sh — add /CHIRP_TEST.mp3 to /etc/icecast2/icecast.xml
#
# Idempotent: re-running does NOT duplicate the mount.
# Reload-only: uses `systemctl reload icecast2`, NEVER restart. Reload keeps
# every existing source connection (ANALOG.mp3, ANALOG_GROUND.mp3, DIGITAL.mp3,
# any keepalives) alive; restart would drop them and bounce production.
#
# Phase 3 only — provides a brand-new sandbox mount safe for chirp smoke
# testing. Phase 4 cutover will publish to the real mountpoints.

set -euo pipefail

XML="/etc/icecast2/icecast.xml"
MOUNT="/CHIRP_TEST.mp3"
BAK="${XML}.bak.$(date +%Y%m%d-%H%M%S)"

if [[ ! -f "$XML" ]]; then
    echo "ERROR: $XML not found" >&2
    exit 1
fi

# Already present? Just reload and exit clean.
if grep -q "<mount-name>${MOUNT}</mount-name>" "$XML"; then
    echo "Mount ${MOUNT} already present — no-op, reloading icecast anyway."
    sudo systemctl reload icecast2
    exit 0
fi

echo "Backing up ${XML} → ${BAK}"
sudo cp "$XML" "$BAK"

# Insert the mount block immediately before the <paths> element. Use python
# for robust insertion (sed against XML is fragile).
TMP="$(mktemp)"
sudo python3 - "$XML" "$TMP" "$MOUNT" <<'PYEOF'
import sys
src, dst, mount = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, "r", encoding="utf-8") as f:
    body = f.read()

block = f"""
  <!-- chirp Phase 3 test mount — added by chirp/scripts/add_test_mount.sh.
       Safe to remove via chirp/scripts/remove_test_mount.sh. -->
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

# Walk back from idx to the indent of <paths>.
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

# Confirm.
sleep 0.5
if curl -sf -o /dev/null "http://127.0.0.1:8000/status-json.xsl"; then
    echo "icecast still up after reload."
else
    echo "WARN: status-json.xsl unreachable — check icecast logs" >&2
fi

echo "Done. Mount ${MOUNT} declared. Use a source client (chirp daemon) to push audio."
