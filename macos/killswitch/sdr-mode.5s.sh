#!/usr/bin/env bash
# ============================================================================
# sdr-mode.5s.sh — SwiftBar / xbar menubar plugin for the SDR killswitch.
#
# Shows the current SDR owner in the menubar and lets you switch modes from a
# dropdown. It is a THIN wrapper: all real logic lives in `sdr-killswitch`,
# which stays the source of truth. Refreshes every 5s (the ".5s." in the name).
#
# INSTALL (SwiftBar):
#   1. brew install --cask swiftbar     # then launch SwiftBar, pick a plugin folder
#   2. cp this file into that plugin folder (keep the ".5s.sh" suffix), chmod +x
#   3. set KILLSWITCH below if the auto-path is wrong
#   (xbar works too — same plugin format; use a "sdr-mode.5s.sh" name.)
#
# If SwiftBar/xbar isn't installed, this file does nothing on its own — the CLI
# still does everything. See README.md.
# ============================================================================

# Absolute path to the killswitch CLI. Auto-resolves a couple of common spots;
# override here if yours differs.
KILLSWITCH="${KILLSWITCH:-}"
if [ -z "$KILLSWITCH" ]; then
  for p in \
    "$HOME/scannerproject/macos/killswitch/sdr-killswitch" \
    "$HOME/Documents/scannerproject/macos/killswitch/sdr-killswitch" \
    "/opt/scannerproject/app/macos/killswitch/sdr-killswitch"; do
    [ -x "$p" ] && { KILLSWITCH="$p"; break; }
  done
fi

# SwiftBar passes the plugin's own path in $0; that's what we hand back to the
# refresh actions so clicks re-run THIS plugin. The mode actions call the CLI.
SELF="$0"

if [ -z "$KILLSWITCH" ] || [ ! -x "$KILLSWITCH" ]; then
  echo "📡 SDR ⚠"
  echo "---"
  echo "sdr-killswitch not found | color=red"
  echo "Set KILLSWITCH at the top of this plugin | color=gray"
  exit 0
fi

MODE="$("$KILLSWITCH" status 2>/dev/null | awk -F': ' '/^  mode:/{print $2; exit}')"
[ -n "$MODE" ] || MODE="unknown"

case "$MODE" in
  scanner)  ICON="📡"; LABEL="Scanner" ;;
  sdrangel) ICON="🎛"; LABEL="SDRangel" ;;
  sdrtrunk) ICON="📻"; LABEL="Trunk" ;;
  released) ICON="⚪️"; LABEL="Idle" ;;
  *)        ICON="❔"; LABEL="?" ;;
esac

# menubar title
echo "$ICON $LABEL"
echo "---"
echo "SDR owner: $LABEL"
echo "---"

# Each action runs the CLI in a terminal (so you see progress + any sudo
# prompt), then refreshes the menubar. bash= wants an absolute path.
emit() {  # emit "<menu text>" "<subcommand>" "<check>"
  local text="$1" sub="$2" check="$3"
  local prefix=""; [ "$check" = "1" ] && prefix="✓ "
  echo "${prefix}${text} | bash=\"$KILLSWITCH\" param1=$sub terminal=true refresh=true"
}

emit "Scanner mode (sb5 + daemons)" scanner  "$([ "$MODE" = scanner ]  && echo 1)"
emit "SDRangel desktop"            sdrangel "$([ "$MODE" = sdrangel ] && echo 1)"
emit "SDRTrunk desktop"            sdrtrunk "$([ "$MODE" = sdrtrunk ] && echo 1)"
echo "---"
emit "Release (free the radios)"   release  "$([ "$MODE" = released ] && echo 1)"
echo "Status (full report) | bash=\"$KILLSWITCH\" param1=status terminal=true refresh=false"
echo "Refresh | refresh=true"
