#!/bin/bash
# Neptune self-healing UDP(SDRangel copyToUDP)->MP3->icecast bridge. Mirror of
# venus-audio-bridge.sh, with two host differences: ffmpeg comes from radioconda
# (no Homebrew needed for it), and the mount is neptune-analog.mp3 (the analog scanner
# mount; neptune-angel.mp3 -> neptune-air.mp3 2026-07-19 -> neptune-analog.mp3 2026-07-21,
# renamed to "analog" now that VFO gets its own neptune-vfo.mp3). Restarts ffmpeg if it
# exits (SDRangel restart / UDP starvation); rw_timeout makes it exit when the tap dries.
FFMPEG="$HOME/radioconda/bin/ffmpeg"
source "$HOME/scannerproject/etc/mac/icecast-neptune-credentials.env"

# Kill the ffmpeg CHILD when this script is stopped. Without this, `launchctl
# kickstart -k` (or bootout) kills the bash loop but ORPHANS ffmpeg (reparented
# to launchd, PPID 1), which keeps holding UDP :9998 so the restarted bridge's
# fresh ffmpeg cannot bind — the mount never comes back. Observed twice on
# 2026-07-19. The trap reaps the child so a restart is clean.
FFMPEG_PID=""
cleanup() { [ -n "$FFMPEG_PID" ] && kill "$FFMPEG_PID" 2>/dev/null; wait "$FFMPEG_PID" 2>/dev/null; exit 0; }
trap cleanup TERM INT EXIT

while true; do
  "$FFMPEG" -hide_banner -loglevel warning \
    -thread_queue_size 1024 -rw_timeout 8000000 -f s16le -ar 48000 -ac 1 -i udp://127.0.0.1:9998 \
    -c:a libmp3lame -b:a 64k \
    -f mp3 -content_type audio/mpeg -ice_name "Neptune SDRangel" -legacy_icecast 1 \
    "icecast://source:${ICECAST_SOURCE_PASSWORD}@127.0.0.1:8000/neptune-analog.mp3" &
  FFMPEG_PID=$!
  wait "$FFMPEG_PID"
  echo "[bridge $(date +%H:%M:%S)] ffmpeg exited ($?); restart in 3s"
  sleep 3
done
