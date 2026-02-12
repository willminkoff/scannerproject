#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=${DIGITAL_MIXER_SERVICE:-scanner-digital-mixer.service}
ICECAST_HOST=${ICECAST_HOST:-127.0.0.1}
ICECAST_PORT=${ICECAST_PORT:-8000}
OUTPUT_MOUNT=${DIGITAL_MIXER_OUTPUT_MOUNT:-${MOUNT_NAME:-scannerbox.mp3}}
OUTPUT_MOUNT=${OUTPUT_MOUNT#/}
OUTPUT_HTTP_URL=${DIGITAL_MIXER_OUTPUT_HTTP_URL:-http://${ICECAST_HOST}:${ICECAST_PORT}/${OUTPUT_MOUNT}}
DURATION=${DIGITAL_MIXER_SMOKE_SECONDS:-30}

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found in PATH; install ffmpeg to run this test." >&2
  exit 2
fi

before_restarts=$(systemctl show -p NRestarts --value "$SERVICE_NAME" 2>/dev/null || echo "0")
if [ -z "$before_restarts" ]; then
  before_restarts=0
fi

echo "[digital-mixer-smoke-test] Checking output: $OUTPUT_HTTP_URL"
headers=$(curl -sS --max-time 5 -D - -o /dev/null "$OUTPUT_HTTP_URL" || true)
if ! echo "$headers" | tr -d '\r' | grep -qi '^Content-Type: audio/mpeg'; then
  echo "FAIL: Content-Type is not audio/mpeg" >&2
  echo "$headers" | tr -d '\r' | head -20 >&2
  exit 1
fi

echo "[digital-mixer-smoke-test] Decoding 2s with ffmpeg"
ffmpeg -v error -nostdin -t 2 -i "$OUTPUT_HTTP_URL" -f null - >/dev/null

echo "[digital-mixer-smoke-test] Monitoring restarts for ${DURATION}s"
sleep "$DURATION"

after_restarts=$(systemctl show -p NRestarts --value "$SERVICE_NAME" 2>/dev/null || echo "$before_restarts")
if [ -z "$after_restarts" ]; then
  after_restarts=$before_restarts
fi

delta=$((after_restarts - before_restarts))
if [ "$delta" -gt 1 ]; then
  echo "FAIL: $SERVICE_NAME restarted $delta times in ${DURATION}s" >&2
  exit 1
fi

echo "OK: mixer output is valid and restarts are within threshold"
