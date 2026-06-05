#!/bin/bash
# icecast/deploy_cors_header.sh — add a global CORS header to icecast on Micro
# so the sb5 browser player can run live streams through Web Audio
# (createMediaElementSource requires Access-Control-Allow-Origin when the
# page origin :5050 differs from the audio origin :8000).
#
# Adds, as a child of <icecast>:
#     <http-headers>
#       <header name="Access-Control-Allow-Origin" value="*" />
#     </http-headers>
#
# Idempotent: re-running does NOT duplicate the block.
# RELOAD-FIRST: uses `systemctl reload icecast2`, which keeps every existing
# source (ANALOG/ANALOG_GROUND/DIGITAL/VFO + keepalives) and listener (the
# scanner-vlc-* consumers) connected — no production drop. The script then
# VERIFIES the header is actually live. Only if reload did not apply it does
# it ask for an explicit --restart (which drops listeners ~1s; the VLC
# sources auto-reconnect). We never restart without being told to.
#
# Usage:
#   ./deploy_cors_header.sh            # insert + reload + verify
#   ./deploy_cors_header.sh --restart  # also restart icecast2 IF reload didn't apply
#
# '*' is correct for the trusted LAN. To lock to a specific origin later,
# change the value to e.g. "http://micro.local:5050".

set -euo pipefail

XML="/etc/icecast2/icecast.xml"
HEADER_NAME="Access-Control-Allow-Origin"
HEADER_VALUE="*"
ALLOW_RESTART=0
[[ "${1:-}" == "--restart" ]] && ALLOW_RESTART=1

if [[ ! -f "$XML" ]]; then
    echo "ERROR: $XML not found" >&2
    exit 1
fi

verify_header() {
    # status-json.xsl is a normal (non-streaming) response and carries the
    # global http-headers, so it's a clean probe.
    curl -s -D - -o /dev/null --max-time 4 \
        -H "Origin: http://micro.local:5050" \
        "http://127.0.0.1:8000/status-json.xsl" 2>/dev/null \
        | grep -qi "access-control-allow-origin"
}

reload_and_verify() {
    echo "Reloading icecast2 (NOT restart — sources/listeners preserved)..."
    sudo systemctl reload icecast2
    sleep 1
    if verify_header; then
        echo "OK: ${HEADER_NAME} is live after reload."
        return 0
    fi
    return 1
}

# --- already present? Just (re)load and verify. -----------------------------
if grep -q "${HEADER_NAME}" "$XML"; then
    echo "CORS header already declared in ${XML} — no edit, ensuring it's live."
    if reload_and_verify; then exit 0; fi
    echo "WARN: header declared but not live after reload." >&2
else
    BAK="${XML}.bak.$(date +%Y%m%d-%H%M%S)"
    echo "Backing up ${XML} -> ${BAK}"
    sudo cp "$XML" "$BAK"

    TMP="$(mktemp)"
    sudo python3 - "$XML" "$TMP" "$HEADER_NAME" "$HEADER_VALUE" <<'PYEOF'
import sys
src, dst, name, value = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(src, "r", encoding="utf-8") as f:
    body = f.read()

block = f"""
  <!-- CORS for the sb5 browser player (Web Audio). Added by
       icecast/deploy_cors_header.sh. '*' is fine on the trusted LAN;
       lock to a specific origin later if desired. -->
  <http-headers>
    <header name="{name}" value="{value}" />
  </http-headers>
"""

# Insert before <paths>, the same stable anchor chirp's mount scripts use.
needle = "<paths>"
idx = body.find(needle)
if idx < 0:
    print("ERROR: could not find <paths> element in icecast.xml", file=sys.stderr)
    sys.exit(2)
line_start = body.rfind("\n", 0, idx) + 1
out = body[:line_start] + block.lstrip("\n") + "\n" + body[line_start:]
with open(dst, "w", encoding="utf-8") as f:
    f.write(out)
print("inserted ok")
PYEOF

    sudo mv "$TMP" "$XML"
    sudo chown root:icecast "$XML" 2>/dev/null || true
    sudo chmod 640 "$XML" 2>/dev/null || true

    echo "Validating XML..."
    if command -v xmllint >/dev/null 2>&1; then
        sudo xmllint --noout "$XML" || { echo "ERROR: xmllint failed — restoring backup"; sudo cp "$BAK" "$XML"; exit 3; }
    fi

    if reload_and_verify; then exit 0; fi
fi

# --- reload did not apply the header ----------------------------------------
echo "" >&2
echo "Reload did not surface ${HEADER_NAME}. icecast 2.4.4 may need a full" >&2
echo "restart for http-headers to take effect." >&2
if [[ "$ALLOW_RESTART" -eq 1 ]]; then
    echo "Restarting icecast2 (drops listeners ~1s; VLC sources auto-reconnect)..."
    sudo systemctl restart icecast2
    sleep 2
    echo "--- post-restart source check ---"
    curl -s --max-time 4 "http://127.0.0.1:8000/status-json.xsl" \
        | grep -o '/[A-Z_]*\.mp3' | sort -u || echo "(status unreachable)"
    if verify_header; then
        echo "OK: ${HEADER_NAME} is live after restart."
        exit 0
    fi
    echo "ERROR: header still not present after restart — check icecast logs." >&2
    exit 4
else
    echo "Re-run with --restart to apply via restart, or apply manually." >&2
    exit 5
fi
