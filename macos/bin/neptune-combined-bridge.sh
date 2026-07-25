#!/bin/bash
# neptune-combined-bridge.sh — publish ONE mount `neptune.mp3` that carries
# whatever analog (SDRangel) + digital (SDRTrunk) audio is currently live.
#
# Why a supervisor and not a plain ffmpeg amix: ffmpeg's amix REQUIRES every
# input to open, and exits the instant one source mount is 404. With manual
# tuning the two source mounts (neptune-analog.mp3, neptune-trunk.mp3) come and
# go constantly, so a plain mixer would spin. This watches which sources are
# live and (re)starts ffmpeg to mix exactly those — 1 source = passthrough,
# 2 = amix — restarting only when the live set changes.
#
# Sources are the EXISTING mounts, untouched:
#   neptune-trunk.mp3   <- SDRTrunk's own icecast broadcaster
#   neptune-analog.mp3  <- the SDRangel UDP->icecast bridge
# Output: neptune.mp3   <- the single URL to listen on.

set -u
FFMPEG="$HOME/radioconda/bin/ffmpeg"
ICE="127.0.0.1:8000"
OUT="neptune.mp3"
SRC_TRUNK="http://$ICE/neptune-trunk.mp3"
SRC_ANALOG="http://$ICE/neptune-analog.mp3"
source "$HOME/scannerproject/etc/mac/icecast-neptune-credentials.env"

FF_PID=""
cleanup() { [ -n "$FF_PID" ] && kill "$FF_PID" 2>/dev/null; wait "$FF_PID" 2>/dev/null; exit 0; }
trap cleanup TERM INT EXIT

live() {  # 200 => live
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -H 'Range: bytes=0-0' "$1")" = "200" ]
}

RC=("-reconnect" "1" "-reconnect_streamed" "1" "-reconnect_delay_max" "2")
DEST="icecast://source:${ICECAST_SOURCE_PASSWORD}@$ICE/$OUT"

while true; do
  T=off; A=off
  live "$SRC_TRUNK"  && T=on
  live "$SRC_ANALOG" && A=on
  set="$T$A"

  if [ "$set" = "offoff" ]; then
    sleep 4; continue                       # nothing to publish yet
  fi

  if [ "$T" = on ] && [ "$A" = on ]; then   # mix both
    "$FFMPEG" -hide_banner -loglevel warning \
      "${RC[@]}" -i "$SRC_TRUNK" "${RC[@]}" -i "$SRC_ANALOG" \
      -filter_complex "amix=inputs=2:duration=longest:dropout_transition=2:normalize=0" \
      -f mp3 -content_type audio/mpeg -ice_name "Neptune combined" -legacy_icecast 1 \
      "$DEST" >/tmp/neptune-combined.ff.log 2>&1 &
  else                                      # exactly one live -> passthrough copy
    [ "$T" = on ] && S="$SRC_TRUNK" || S="$SRC_ANALOG"
    "$FFMPEG" -hide_banner -loglevel warning "${RC[@]}" -i "$S" -c:a copy \
      -f mp3 -content_type audio/mpeg -ice_name "Neptune combined" -legacy_icecast 1 \
      "$DEST" >/tmp/neptune-combined.ff.log 2>&1 &
  fi
  FF_PID=$!

  # Watch: restart when ffmpeg dies OR the live set changes (a source joined/left).
  while kill -0 "$FF_PID" 2>/dev/null; do
    sleep 5
    nT=off; nA=off
    live "$SRC_TRUNK"  && nT=on
    live "$SRC_ANALOG" && nA=on
    if [ "$nT$nA" != "$set" ]; then
      kill "$FF_PID" 2>/dev/null; wait "$FF_PID" 2>/dev/null; break
    fi
  done
  FF_PID=""
  sleep 1
done
