#!/bin/bash
# chirp/scripts/remove_ground_test_mount.sh — remove /CHIRP_GROUND_TEST.mp3.
#
# Reverse of add_ground_test_mount.sh. Idempotent: no-op if already absent.
# systemctl reload (NOT restart) — production sources stay connected.

set -euo pipefail

XML="/etc/icecast2/icecast.xml"
MOUNT="/CHIRP_GROUND_TEST.mp3"
BAK="${XML}.bak.$(date +%Y%m%d-%H%M%S)"

if [[ ! -f "$XML" ]]; then
    echo "ERROR: $XML not found" >&2
    exit 1
fi

if ! grep -q "<mount-name>${MOUNT}</mount-name>" "$XML"; then
    echo "Mount ${MOUNT} not present — no-op, reloading icecast anyway."
    sudo systemctl reload icecast2
    exit 0
fi

echo "Backing up ${XML} → ${BAK}"
sudo cp "$XML" "$BAK"

TMP="$(mktemp)"
sudo python3 - "$XML" "$TMP" "$MOUNT" <<'PYEOF'
import re
import sys
src, dst, mount = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, "r", encoding="utf-8") as f:
    body = f.read()

mount_pat = re.compile(
    r"(\n[ \t]*<!--[^>]*chirp Phase 4a ground test mount[\s\S]*?-->)?"
    r"\n[ \t]*<mount>\s*"
    r"<mount-name>" + re.escape(mount) + r"</mount-name>"
    r"[\s\S]*?</mount>\n?",
    re.MULTILINE,
)
new_body, n = mount_pat.subn("\n", body)
if n == 0:
    simple = re.compile(
        r"\n[ \t]*<mount>\s*<mount-name>" + re.escape(mount) +
        r"</mount-name>[\s\S]*?</mount>\n?",
        re.MULTILINE,
    )
    new_body, n = simple.subn("\n", body)
if n == 0:
    print("ERROR: regex matched zero blocks", file=sys.stderr)
    sys.exit(2)
with open(dst, "w", encoding="utf-8") as f:
    f.write(new_body)
print(f"removed {n} mount block(s)")
PYEOF

sudo mv "$TMP" "$XML"
sudo chown root:icecast "$XML" 2>/dev/null || true
sudo chmod 640 "$XML" 2>/dev/null || true

if command -v xmllint >/dev/null 2>&1; then
    xmllint --noout "$XML" || { echo "ERROR: xmllint failed"; exit 3; }
fi

echo "Reloading icecast2..."
sudo systemctl reload icecast2

if curl -sf -o /dev/null "http://127.0.0.1:8000/status-json.xsl"; then
    echo "icecast still up after reload."
fi

echo "Done. Mount ${MOUNT} removed."
