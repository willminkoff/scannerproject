#!/bin/bash
set -euo pipefail

ICECAST_HOST="${ICECAST_HOST:-127.0.0.1}"
ICECAST_PORT="${ICECAST_PORT:-8000}"
ICECAST_USER="${ICECAST_USER:-source}"
ICECAST_PASSWORD="${ICECAST_PASSWORD:-062352}"
DMR_MOUNT="${DMR_MOUNT:-/GND.mp3}"
DMR_AUDIO_RATE="${DMR_AUDIO_RATE:-48000}"
DMR_AUDIO_CHANNELS="${DMR_AUDIO_CHANNELS:-1}"

FFMPEG_BIN="${FFMPEG_BIN:-/usr/bin/ffmpeg}"
DMR_ICECAST_URL="${DMR_ICECAST_URL:-}"

if [[ -z "$DMR_ICECAST_URL" ]]; then
  DMR_ICECAST_URL="icecast://${ICECAST_USER}:${ICECAST_PASSWORD}@${ICECAST_HOST}:${ICECAST_PORT}${DMR_MOUNT}"
fi

if ! command -v "$FFMPEG_BIN" >/dev/null 2>&1; then
  echo "[dmr-icecast] ffmpeg not found at $FFMPEG_BIN" >&2
  exit 1
fi

echo "[dmr-icecast] PCM in: ${DMR_AUDIO_RATE}Hz ${DMR_AUDIO_CHANNELS}ch -> mount ${DMR_MOUNT}" >&2

echo "[dmr-icecast] Streaming to $DMR_ICECAST_URL" >&2
exec "$FFMPEG_BIN" -hide_banner -loglevel warning \
  -f s16le -ar "$DMR_AUDIO_RATE" -ac "$DMR_AUDIO_CHANNELS" -i - \
  -content_type audio/mpeg -f mp3 "$DMR_ICECAST_URL"
