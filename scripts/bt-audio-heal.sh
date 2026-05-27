#!/usr/bin/env bash
set -euo pipefail

# Reconnect preferred Bluetooth speaker, route default sink, and restart local VLC playback.
# Intended for periodic one-shot execution under systemd.

BT_SPEAKER_MAC="${BT_SPEAKER_MAC:-C0:28:8D:34:6E:67}"
# Default to the current PipeWire default-sink description (e.g. "UE BOOM 2")
# so vlc_stream_routed_to_bt and find_bt_sink_id work without hard-coding a name.
BT_SINK_NAME="${BT_SINK_NAME:-$(XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)} wpctl inspect @DEFAULT_AUDIO_SINK@ 2>/dev/null | sed -n 's/.*node.description = "\([^"]*\)".*/\1/p' | head -n1)}"
BT_SINK_NAME="${BT_SINK_NAME:-BLUE}"
BT_PULSE_SINK="${BT_PULSE_SINK:-${VLC_PULSE_SINK:-bluez_output.C0_28_8D_34_6E_67.1}}"
BT_AUDIO_VOLUME="${BT_AUDIO_VOLUME:-0.75}"
BT_HEAL_ENFORCE_VOLUME="${BT_HEAL_ENFORCE_VOLUME:-0}"
BT_HEAL_FORCE_UNMUTE="${BT_HEAL_FORCE_UNMUTE:-0}"
BT_VLC_TARGET="${BT_VLC_TARGET:-analog}"
BT_HEAL_MAX_WAIT_SEC="${BT_HEAL_MAX_WAIT_SEC:-45}"
BT_HEAL_POLL_SEC="${BT_HEAL_POLL_SEC:-2}"
BT_HEAL_SINK_WAIT_SEC="${BT_HEAL_SINK_WAIT_SEC:-15}"
BT_HEAL_FORCE_RESTART="${BT_HEAL_FORCE_RESTART:-0}"
BT_HEAL_REQUIRE_UI_ACTIVE="${BT_HEAL_REQUIRE_UI_ACTIVE:-1}"
UI_BASE_URL="${UI_BASE_URL:-http://127.0.0.1:5050}"

uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${uid}}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
export PULSE_SERVER="${PULSE_SERVER:-unix:${XDG_RUNTIME_DIR}/pulse/native}"
export PULSE_SINK="${PULSE_SINK:-${BT_PULSE_SINK}}"

log() {
  echo "[bt-audio-heal] $*"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

wait_for_audio_session() {
  local waited=0
  while (( waited < BT_HEAL_MAX_WAIT_SEC )); do
    if [[ -S "${XDG_RUNTIME_DIR}/bus" && -S "${XDG_RUNTIME_DIR}/pulse/native" ]] && wpctl status >/dev/null 2>&1; then
      return 0
    fi
    sleep "${BT_HEAL_POLL_SEC}"
    waited=$((waited + BT_HEAL_POLL_SEC))
  done
  return 1
}

is_bt_connected() {
  bluetoothctl info "${BT_SPEAKER_MAC}" 2>/dev/null | grep -q "Connected: yes"
}

connect_bt() {
  bluetoothctl connect "${BT_SPEAKER_MAC}" >/dev/null 2>&1 || true
}

hard_reconnect_bt() {
  bluetoothctl disconnect "${BT_SPEAKER_MAC}" >/dev/null 2>&1 || true
  sleep 1
  bluetoothctl connect "${BT_SPEAKER_MAC}" >/dev/null 2>&1 || true
}

find_bt_sink_id() {
  local sink_ids id
  sink_ids="$(
    wpctl status 2>/dev/null \
      | sed -n '/Sinks:/,/Sink endpoints:/p' \
      | sed -n 's/^[^0-9]*\([0-9][0-9]*\)\..*/\1/p'
  )"
  for id in ${sink_ids}; do
    if wpctl inspect "${id}" 2>/dev/null | grep -Fq "node.name = \"${BT_PULSE_SINK}\""; then
      echo "${id}"
      return 0
    fi
  done
  if [[ -n "${BT_SINK_NAME}" ]]; then
    for id in ${sink_ids}; do
      if wpctl inspect "${id}" 2>/dev/null | grep -Fq "node.description = \"${BT_SINK_NAME}\""; then
        echo "${id}"
        return 0
      fi
    done
  fi
  return 1
}

vlc_stream_routed_to_bt() {
  local summary
  summary="$(wpctl status 2>/dev/null)"
  echo "${summary}" | grep -q "VLC media player" || return 1
  if [[ -n "${BT_SINK_NAME}" ]] && echo "${summary}" | grep -q "> ${BT_SINK_NAME}:playback_"; then
    return 0
  fi
  return 1
}

restart_vlc_target() {
  local target="$1"
  curl -fsS --max-time 4 -X POST -d "action=stop&target=${target}" "${UI_BASE_URL}/api/vlc" >/dev/null || true
  sleep 0.4
  curl -fsS --max-time 4 -X POST -d "action=start&target=${target}" "${UI_BASE_URL}/api/vlc" >/dev/null || true
}

main() {
  local sink_id="" deadline

  if [[ "${BT_HEAL_REQUIRE_UI_ACTIVE}" == "1" ]] && ! systemctl --quiet is-active airband-ui.service; then
    log "airband-ui inactive; skipping"
    exit 0
  fi

  if ! have_cmd bluetoothctl || ! have_cmd wpctl || ! have_cmd curl; then
    log "missing dependency (bluetoothctl/wpctl/curl); skipping"
    exit 0
  fi

  if ! wait_for_audio_session; then
    log "audio session unavailable after ${BT_HEAL_MAX_WAIT_SEC}s; skipping"
    exit 0
  fi

  if ! is_bt_connected; then
    log "connecting ${BT_SPEAKER_MAC}"
    connect_bt
  fi
  if ! is_bt_connected; then
    log "speaker not connected; skipping"
    exit 0
  fi

  deadline=$((SECONDS + BT_HEAL_SINK_WAIT_SEC))
  while (( SECONDS < deadline )); do
    if sink_id="$(find_bt_sink_id 2>/dev/null)"; then
      break
    fi
    sleep 1
  done

  if [[ -z "${sink_id}" ]]; then
    log "sink not found, attempting reconnect"
    hard_reconnect_bt
    deadline=$((SECONDS + BT_HEAL_SINK_WAIT_SEC))
    while (( SECONDS < deadline )); do
      if sink_id="$(find_bt_sink_id 2>/dev/null)"; then
        break
      fi
      sleep 1
    done
  fi

  if [[ -z "${sink_id}" ]]; then
    log "speaker sink not found; skipping"
    exit 0
  fi

  wpctl set-default "${sink_id}" >/dev/null 2>&1 || true
  if [[ "${BT_HEAL_FORCE_UNMUTE}" == "1" ]]; then
    wpctl set-mute "${sink_id}" 0 >/dev/null 2>&1 || true
  fi
  if [[ "${BT_HEAL_ENFORCE_VOLUME}" == "1" ]]; then
    wpctl set-volume "${sink_id}" "${BT_AUDIO_VOLUME}" >/dev/null 2>&1 || true
    log "sink ${sink_id} selected, volume=${BT_AUDIO_VOLUME} (enforced)"
  else
    log "sink ${sink_id} selected (volume unchanged)"
  fi

  if [[ "${BT_HEAL_FORCE_RESTART}" == "1" ]]; then
    restart_vlc_target "${BT_VLC_TARGET}"
    log "forced VLC ${BT_VLC_TARGET} restart"
    exit 0
  fi

  if vlc_stream_routed_to_bt; then
    log "VLC stream already healthy on BT sink"
    exit 0
  fi

  restart_vlc_target "${BT_VLC_TARGET}"
  sleep 1
  if vlc_stream_routed_to_bt; then
    log "recovered VLC ${BT_VLC_TARGET} stream on BT sink"
  else
    log "VLC ${BT_VLC_TARGET} restart requested; stream not yet visible"
  fi
}

main "$@"
