#!/bin/bash
# Poll icecast mounts for chirp bands. If a chirp mount is 404 but the daemon
# is running, kickstart the corresponding LaunchAgent. The daemon's internal
# publish-loop supervisor is designed to self-restart, but a 2026-06-18-style
# state where the loop signals shutdown while the daemon keeps running has
# recurred (see chirp/dsp/icecast_sink.py:756 SB7.3-E rewrite comment).
# This outer watchdog is defense in depth.

BANDS=('airband:neptune-analog.mp3' 'ground:neptune-ground.mp3')

for entry in "${BANDS[@]}"; do
  band="${entry%%:*}"
  mount="${entry#*:}"
  code=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Range: bytes=0-0' --max-time 3 "http://127.0.0.1:8000/${mount}")
  agent="com.scannerproject.chirp-${band}"
  if [[ "$code" == "404" ]] && launchctl list | grep -q "$agent"; then
    echo "[$(date -Iseconds)] $mount is 404 but $agent is loaded — kickstart"
    launchctl kickstart -k "gui/$(id -u)/$agent"
  fi
done
