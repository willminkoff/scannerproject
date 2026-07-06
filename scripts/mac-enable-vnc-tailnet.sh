#!/usr/bin/env bash
# Enable macOS Screen Sharing (built-in VNC server, port 5900) with a legacy VNC
# password so any standard VNC client (incl. Windows tailnet hosts) can connect.
# Reach it over Tailscale at: wills-mac-mini-1 / 100.106.194.41 : 5900
# Run with: sudo bash scripts/mac-enable-vnc-tailnet.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi

# Never hardcode the VNC password in the repo (SB7.0 scrub — the old committed
# value must be considered burned; rotate it on any box that used it).
# Usage: sudo VNC_PASSWORD='newpw' bash scripts/mac-enable-vnc-tailnet.sh
VNC_PASSWORD="${VNC_PASSWORD:?set VNC_PASSWORD in the environment (max 8 chars, legacy VNC limit)}"

KICKSTART=/System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart

# Turn on Remote Management/Screen Sharing, allow all users, enable legacy VNC pw.
"$KICKSTART" -activate -configure \
  -access -on \
  -clientopts -setvnclegacy -vnclegacy yes \
  -clientopts -setvncpw -vncpw "$VNC_PASSWORD" \
  -restart -agent -console

# Ensure the Screen Sharing launch daemon is enabled & running.
launchctl enable system/com.apple.screensharing 2>/dev/null || true
launchctl kickstart -k system/com.apple.screensharing 2>/dev/null || \
  launchctl bootstrap system /System/Library/LaunchDaemons/com.apple.screensharing.plist 2>/dev/null || true

sleep 2
echo "--- Listening on 5900? ---"
lsof -nP -iTCP:5900 -sTCP:LISTEN || echo "(nothing yet — may need a few seconds)"
echo "Done. Connect via Tailscale on port 5900 (password: the one you supplied)."
