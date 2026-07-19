#!/bin/bash
# Neptune self-healing UDP(SDRangel copyToUDP)->MP3->icecast bridge. Mirror of
# venus-audio-bridge.sh, with two host differences: ffmpeg comes from radioconda
# (no Homebrew needed for it), and the mount is neptune-air.mp3 (the Air role's mount;
# renamed from neptune-angel.mp3 2026-07-19 to follow SB3's "mount = role" model). Restarts ffmpeg if it
# exits (SDRangel restart / UDP starvation); rw_timeout makes it exit when the tap dries.
FFMPEG="$HOME/radioconda/bin/ffmpeg"
source "$HOME/scannerproject/etc/mac/icecast-neptune-credentials.env"
while true; do
  "$FFMPEG" -hide_banner -loglevel warning \
    -thread_queue_size 1024 -rw_timeout 8000000 -f s16le -ar 48000 -ac 1 -i udp://127.0.0.1:9998 \
    -c:a libmp3lame -b:a 64k \
    -f mp3 -content_type audio/mpeg -ice_name "Neptune SDRangel" -legacy_icecast 1 \
    "icecast://source:${ICECAST_SOURCE_PASSWORD}@127.0.0.1:8000/neptune-air.mp3"
  echo "[bridge $(date +%H:%M:%S)] ffmpeg exited ($?); restart in 3s"
  sleep 3
done
