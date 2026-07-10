#!/bin/bash
# Self-healing UDP(SDRangel copyToUDP)->MP3->icecast bridge. Restarts ffmpeg if it exits
# (e.g. SDRangel restart / UDP starvation). rw_timeout makes ffmpeg exit when the tap goes dry.
eval "$(/usr/local/bin/brew shellenv)"
source "$HOME/scannerproject/etc/mac/icecast-credentials.env"
while true; do
  ffmpeg -hide_banner -loglevel warning \
    -thread_queue_size 1024 -rw_timeout 8000000 -f s16le -ar 48000 -ac 1 -i udp://127.0.0.1:9998 \
    -c:a libmp3lame -b:a 64k \
    -f mp3 -content_type audio/mpeg -ice_name "Venus SDRangel" -legacy_icecast 1 \
    "icecast://source:${ICECAST_SOURCE_PASSWORD}@127.0.0.1:8000/live.mp3"
  echo "[bridge $(date +%H:%M:%S)] ffmpeg exited ($?); restart in 3s"
  sleep 3
done
