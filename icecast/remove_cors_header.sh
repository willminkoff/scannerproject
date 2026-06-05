#!/bin/bash
# icecast/remove_cors_header.sh — revert deploy_cors_header.sh.
# Removes the <http-headers> block (and its leading comment) from
# /etc/icecast2/icecast.xml, then reloads icecast2. Idempotent.

set -euo pipefail

XML="/etc/icecast2/icecast.xml"
HEADER_NAME="Access-Control-Allow-Origin"

if [[ ! -f "$XML" ]]; then
    echo "ERROR: $XML not found" >&2
    exit 1
fi

if ! grep -q "${HEADER_NAME}" "$XML"; then
    echo "No CORS header present — nothing to remove."
    exit 0
fi

BAK="${XML}.bak.$(date +%Y%m%d-%H%M%S)"
echo "Backing up ${XML} -> ${BAK}"
sudo cp "$XML" "$BAK"

TMP="$(mktemp)"
sudo python3 - "$XML" "$TMP" <<'PYEOF'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as f:
    body = f.read()
# Drop the deploy comment (if present) + the <http-headers>...</http-headers> block.
body = re.sub(r"\n?[ \t]*<!--[^>]*sb5 browser player[^>]*-->\n", "\n", body, flags=re.DOTALL)
body = re.sub(r"\n?[ \t]*<http-headers>.*?</http-headers>\n", "\n", body, flags=re.DOTALL)
with open(dst, "w", encoding="utf-8") as f:
    f.write(body)
print("removed ok")
PYEOF

sudo mv "$TMP" "$XML"
sudo chown root:icecast "$XML" 2>/dev/null || true
sudo chmod 640 "$XML" 2>/dev/null || true

if command -v xmllint >/dev/null 2>&1; then
    sudo xmllint --noout "$XML" || { echo "ERROR: xmllint failed — restoring backup"; sudo cp "$BAK" "$XML"; exit 3; }
fi

echo "Reloading icecast2..."
sudo systemctl reload icecast2
echo "Done. CORS header removed."
