#!/bin/bash
# mac-start-sdrtrunk.sh
# One-shot launcher: make sure the SDRplay API service is healthy, then open SDRTrunk
# with the RSPduo available.
#
# ============================================================================
# Revision 5.0 (2026-07-16): DIGITAL IS ON THE RSP serial 180903EF32 (RSP-A) via
#   SDRTrunk's native SDRplay API, DUAL-TUNER for up to 2 P25 systems. So the
#   SDRplay apiService health dance below DOES apply. SDRTrunk must claim serial
#   180903EF32 through the tuner-broker (consumer "sdrtrunk") so nothing else
#   opens the RSP; it owns both tuners in one process (no 0x6bed / MA-SL
#   exposure). In SDRTrunk (View -> Tuners), add the SDRplay RSPduo, enable both
#   tuners, and pin each P25 system to its Preferred Tuner.
#
#   SERIAL CORRECTION: revision 4.1 claimed digital had moved to 1809063632
#   because 180903EF32 had supposedly been taken to another host. That was
#   BACKWARDS — 180903EF32 never left, and 1809063632 is Venus's device.
#   Confirmed by Will 2026-07-16 and measured on the box (`ioreg -p IOUSB`).
#   Digital-on-RSP was proven on 180903EF32, which IS the attached device, so
#   the "pending-soak / reconfirm" caveat 4.1 introduced is withdrawn.
#   See docs/sb3-neptune-architecture.md §7.5.
# ============================================================================
#
# WHY THIS EXISTS
#   SDRTrunk does NOT talk to the RSPduo directly. It talks to the SDRplay API *service*
#   (sdrplay_apiService), which owns the USB device. That service is installed as a
#   LaunchDaemon:
#       /Library/LaunchDaemons/com.sdrplay.service.plist  (RunAtLoad + KeepAlive)
#   so it already starts itself at boot. In the normal case you can just open SDRTrunk
#   and the tuner is there.
#
#   The annoying case is when that service is *running but wedged* (stalled USB pipe /
#   prior crash) — SDRTrunk then opens with NO tuner. A boot daemon can't fix that on its
#   own. This script does: it confirms the service is actually up, can force-restart it to
#   clear a wedge, then launches SDRTrunk.
#
# USAGE
#   ./mac-start-sdrtrunk.sh           # health-check the service, then launch SDRTrunk
#   ./mac-start-sdrtrunk.sh --reset   # force-restart the SDRplay service first, then launch
#                                     # (use this when the tuner shows up MISSING in SDRTrunk)
#
# Run as your normal user from Terminal. It only uses sudo if it actually has to touch the
# daemon (down or --reset); the normal "already running" path needs no password.

set -u

SERVICE_LABEL="com.sdrplay.service"
SERVICE_PLIST="/Library/LaunchDaemons/${SERVICE_LABEL}.plist"
SERVICE_BIN="/Library/SDRplayAPI/3.15.1/bin/sdrplay_apiService"
SERVICE_PROC="sdrplay_apiService"
SDRTRUNK="${HOME}/SDRTrunk/bin/sdr-trunk"
SDRTRUNK_MATCH="io.github.dsheirer"   # matches the running JVM
RESET=0
[ "${1:-}" = "--reset" ] && RESET=1

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

svc_running()      { pgrep -x "$SERVICE_PROC" >/dev/null 2>&1; }
sdrtrunk_running() { pgrep -f "$SDRTRUNK_MATCH" >/dev/null 2>&1; }

wait_for_svc() {  # poll up to ~10s for the service process to appear
    for _ in $(seq 1 20); do
        svc_running && return 0
        sleep 0.5
    done
    return 1
}

# ---------- 0. sanity ----------
step "Checks"
[ -x "$SERVICE_BIN" ] || die "SDRplay API service not found at $SERVICE_BIN — is the SDRplay API installed?"
[ -x "$SDRTRUNK" ]    || die "SDRTrunk launcher not found at $SDRTRUNK — run scripts/mac-install-sdrtrunk.sh"
ok "service binary + SDRTrunk launcher present"

# ---------- 1. optional force reset ----------
if [ "$RESET" = "1" ]; then
    step "Force-restarting SDRplay service (--reset)"
    if sdrtrunk_running; then
        die "SDRTrunk is already running — quit it first; a daemon restart would yank its tuner."
    fi
    sudo launchctl kickstart -k "system/${SERVICE_LABEL}" \
        && ok "service kickstarted" \
        || warn "kickstart returned non-zero (continuing to verify)"
    wait_for_svc \
        && ok "service back up (pid $(pgrep -x "$SERVICE_PROC"))" \
        || die "service did not come back — check /Library/Logs/sdrplayservice_err.log"
fi

# ---------- 2. ensure the service is up ----------
step "SDRplay API service"
if svc_running; then
    ok "running (pid $(pgrep -x "$SERVICE_PROC"))"
else
    warn "not running — starting it (you may be prompted for your password)"
    sudo launchctl kickstart "system/${SERVICE_LABEL}" 2>/dev/null \
        || sudo launchctl bootstrap system "$SERVICE_PLIST" 2>/dev/null \
        || warn "kickstart/bootstrap failed — relying on KeepAlive"
    wait_for_svc \
        && ok "service is up (pid $(pgrep -x "$SERVICE_PROC"))" \
        || die "service did not start — check /Library/Logs/sdrplayservice_err.log"
fi

# ---------- 3. launch SDRTrunk ----------
step "Launching SDRTrunk"
if sdrtrunk_running; then
    warn "SDRTrunk is already running — not starting a second copy."
else
    nohup "$SDRTRUNK" >/tmp/sdrtrunk.out 2>&1 &
    ok "SDRTrunk launched (pid $!)"
    echo "    If the tuner is missing: quit SDRTrunk and re-run with --reset, or in the"
    echo "    GUI use View → Tuners and add the SDRplay RSPduo."
fi

step "Done"
echo "  SDRplay service: running   |   SDRTrunk: launched"
echo "  Logs: /Library/Logs/sdrplayservice_err.log , /tmp/sdrtrunk.out"
