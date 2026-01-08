#!/bin/bash
set -euo pipefail

ICECAST_HOST="${ICECAST_HOST:-127.0.0.1}"
ICECAST_PORT="${ICECAST_PORT:-8000}"
ICECAST_USER="${ICECAST_USER:-source}"
ICECAST_PASS="${ICECAST_PASS:-062352}"
AIR_MOUNT="${AIR_MOUNT:-AIR.mp3}"
GROUND_MOUNT="${GROUND_MOUNT:-GROUND.mp3}"
MIX_MOUNT="${MIX_MOUNT:-GND.mp3}"
MIX_BITRATE="${MIX_BITRATE:-32k}"
MIX_SAMPLE_RATE="${MIX_SAMPLE_RATE:-8000}"

IN_AIR="http://${ICECAST_HOST}:${ICECAST_PORT}/${AIR_MOUNT}"
IN_GROUND="http://${ICECAST_HOST}:${ICECAST_PORT}/${GROUND_MOUNT}"
OUT_MIX="icecast://${ICECAST_USER}:${ICECAST_PASS}@${ICECAST_HOST}:${ICECAST_PORT}/${MIX_MOUNT}"

exec /usr/bin/ffmpeg -nostdin -hide_banner -loglevel warning \
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2 -i "$IN_AIR" \
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2 -i "$IN_GROUND" \
  -filter_complex "amix=inputs=2:duration=longest:dropout_transition=2" \
  -c:a libmp3lame -b:a "$MIX_BITRATE" -ar "$MIX_SAMPLE_RATE" -ac 1 \
  -content_type audio/mpeg -f mp3 \
  "$OUT_MIX"
