#!/usr/bin/env bash
# bt-connect-speaker.sh — full-flow BT speaker connect for SITREP button.
#
# Handles three states cleanly:
#   1. Device already paired+connected → just (re)route the sinks
#   2. Device paired but disconnected   → connect, then route
#   3. Device NOT paired (lost bond, factory reset, re-pairing flow)
#      → assume speaker is in pairing mode, do full pair sequence
#
# Operator flow:
#   1. Hold BT button on speaker until pairing tone + fast-blink
#   2. Push the "Connect BT Speaker" button on the SB5 SITREP page
#   3. Script runs end-to-end, button reports ok/err
#
# Idempotent — safe to run repeatedly.

set -euo pipefail

BT_MAC="${BT_MAC:-C0:28:8D:34:6E:67}"
BT_NAME="${BT_NAME:-UE BOOM 2}"
BT_VOLUME="${BT_VOLUME:-0.50}"
PAIR_SCAN_SEC="${PAIR_SCAN_SEC:-8}"
PAIR_WAIT_SEC="${PAIR_WAIT_SEC:-8}"
CONNECT_WAIT_SEC="${CONNECT_WAIT_SEC:-8}"
SINK_WAIT_SEC="${SINK_WAIT_SEC:-15}"
VLC_TARGETS="${VLC_TARGETS:-analog ground digital}"
# Run as the ubuntu user so PipeWire/bluez D-Bus is reachable.
TARGET_USER="${TARGET_USER:-ubuntu}"
TARGET_UID="$(id -u "$TARGET_USER" 2>/dev/null || echo 1000)"

log() { echo "[bt-connect] $*"; }

# Always export the PipeWire / D-Bus paths for the ubuntu session so
# wpctl + bluetoothctl can talk to the right sockets — systemd services
# (airband-ui) run as ubuntu but don't inherit the interactive session
# env, so without this wpctl fails with "Could not connect to PipeWire".
if [[ "$(id -un)" != "$TARGET_USER" ]]; then
  log "rerunning as $TARGET_USER (uid $TARGET_UID)"
  exec sudo -u "$TARGET_USER" \
    XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${TARGET_UID}/bus" \
    PULSE_SERVER="unix:/run/user/${TARGET_UID}/pulse/native" \
    BT_MAC="$BT_MAC" BT_NAME="$BT_NAME" BT_VOLUME="$BT_VOLUME" \
    VLC_TARGETS="$VLC_TARGETS" \
    "$0" "$@"
fi

# Already running as ubuntu — make sure the session env is set even
# when invoked from a systemd service (airband-ui) that doesn't carry
# the interactive shell env.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${TARGET_UID}}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
export PULSE_SERVER="${PULSE_SERVER:-unix:${XDG_RUNTIME_DIR}/pulse/native}"

log "starting (mac=$BT_MAC name='$BT_NAME')"

# Helpers ----------------------------------------------------------------
bt_paired_yes() { bluetoothctl info "$BT_MAC" 2>/dev/null | grep -q "Paired: yes"; }
bt_bonded_yes() { bluetoothctl info "$BT_MAC" 2>/dev/null | grep -q "Bonded: yes"; }
bt_connected_yes() { bluetoothctl info "$BT_MAC" 2>/dev/null | grep -q "Connected: yes"; }

wait_for_sink() {
  local deadline=$(( SECONDS + SINK_WAIT_SEC ))
  while (( SECONDS < deadline )); do
    if wpctl status 2>/dev/null | grep -q "$BT_NAME"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

find_sink_id() {
  wpctl status 2>/dev/null \
    | awk -v name="$BT_NAME" '/Sinks:/{f=1;next} /Sink endpoints/{f=0} f && match($0, /[0-9]+\./) {
        # Extract the leading ID and the name (rest of line)
        id=$0; sub(/^[^0-9]*/, "", id); sub(/\..*$/, "", id);
        if (index($0, name)) { print id; exit }
      }'
}

# 1. Bonding state ----------------------------------------------------------
need_pair=0
if ! bt_paired_yes || ! bt_bonded_yes; then
  log "device not paired/bonded — assuming speaker is in pairing mode"
  need_pair=1
fi

if (( need_pair == 1 )); then
  log "removing any stale entry"
  bluetoothctl remove "$BT_MAC" 2>/dev/null || true

  log "scanning ${PAIR_SCAN_SEC}s + pairing ${PAIR_WAIT_SEC}s"
  {
    echo "power on"; sleep 1
    echo "agent NoInputNoOutput"
    echo "default-agent"
    echo "scan on"; sleep "$PAIR_SCAN_SEC"
    echo "scan off"
    echo "pair $BT_MAC"; sleep "$PAIR_WAIT_SEC"
    echo "trust $BT_MAC"; sleep 1
    echo "connect $BT_MAC"; sleep "$CONNECT_WAIT_SEC"
    echo "quit"
  } | bluetoothctl >/dev/null 2>&1 || true

  if ! bt_paired_yes; then
    log "ERROR: pairing failed — is the speaker in pairing mode + within range?"
    exit 2
  fi
  log "paired successfully"
else
  log "already paired — just connecting"
  if ! bt_connected_yes; then
    bluetoothctl connect "$BT_MAC" >/dev/null 2>&1 || true
    sleep "$CONNECT_WAIT_SEC"
  fi
fi

if ! bt_connected_yes; then
  log "ERROR: device did not connect after pair/connect"
  exit 3
fi
log "BT link up"

# 2. Wait for PipeWire sink -------------------------------------------------
if ! wait_for_sink; then
  log "ERROR: PipeWire sink '$BT_NAME' did not appear within ${SINK_WAIT_SEC}s"
  exit 4
fi
log "PipeWire sink ready"

sink_id="$(find_sink_id)"
if [[ -z "${sink_id:-}" ]]; then
  log "WARN: could not extract sink id; using @DEFAULT_AUDIO_SINK@ for volume"
  sink_id="@DEFAULT_AUDIO_SINK@"
else
  log "BT sink id: $sink_id"
  wpctl set-default "$sink_id" 2>/dev/null || log "WARN: set-default failed"
fi

wpctl set-volume "$sink_id" "$BT_VOLUME" 2>/dev/null || log "WARN: set-volume failed"
log "default sink set + volume=$BT_VOLUME"

# 3. Restart scanner VLC sinks so they pick up the new default --------------
for t in $VLC_TARGETS; do
  unit="scanner-vlc-${t}.service"
  if systemctl list-unit-files --no-legend --no-pager "$unit" 2>/dev/null | grep -q "$unit"; then
    if sudo -n /bin/systemctl restart "$unit" 2>/dev/null; then
      log "restarted $unit"
    else
      log "WARN: could not restart $unit (sudoers?)"
    fi
  fi
done

log "done"
exit 0
