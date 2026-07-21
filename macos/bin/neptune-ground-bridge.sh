#!/bin/bash
# Neptune Ground self-healing UDP(SDRangel copyToUDP)->MP3->icecast bridge.
# Second bridge alongside neptune-audio-bridge.sh: reads UDP :9999 (the Ground
# deviceset's copyToUDP port, distinct from Air's :9998) and pushes to the
# neptune-ground.mp3 mount. Added 2026-07-20 for the Ground role ("mount = role").
FFMPEG="$HOME/radioconda/bin/ffmpeg"
source "$HOME/scannerproject/etc/mac/icecast-neptune-credentials.env"

# Kill the ffmpeg CHILD when this script is stopped, so a restart is clean and
# the socket frees (same orphan-trap lesson as the Air bridge, 2026-07-19).
FFMPEG_PID=""
cleanup() { [ -n "$FFMPEG_PID" ] && kill "$FFMPEG_PID" 2>/dev/null; wait "$FFMPEG_PID" 2>/dev/null; exit 0; }
trap cleanup TERM INT EXIT

while true; do
  "$FFMPEG" -hide_banner -loglevel warning \
    -thread_queue_size 1024 -rw_timeout 8000000 -f s16le -ar 48000 -ac 1 -i udp://127.0.0.1:9999 \
    -c:a libmp3lame -b:a 64k \
    -f mp3 -content_type audio/mpeg -ice_name "Neptune Ground" -legacy_icecast 1 \
    "icecast://source:${ICECAST_SOURCE_PASSWORD}@127.0.0.1:8000/neptune-ground.mp3" &
  FFMPEG_PID=$!
  wait "$FFMPEG_PID"
  echo "[ground-bridge $(date +%H:%M:%S)] ffmpeg exited ($?); restart in 3s"
  sleep 3
done
