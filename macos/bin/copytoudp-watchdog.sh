#!/bin/bash
# copytoudp-watchdog — keeps SDRangel producing the copyToUDP audio tap that the
# venus-audio-bridge (ffmpeg->icecast) depends on. After a SDRangel restart/crash the
# copyToUDP flag persists as =1 but is inactive, and the demod deviceset goes idle.
# This re-arms copyToUDP on the PHYSICAL output device (idx 0, "Mac mini Speakers" — where
# the demod audio actually flows) and ensures the NFM deviceset (DS1) is running.
# Read-only until drift; minimal PATCH on drift; logs every change. No speaker output changed
# (copyToUDP is an additive COPY). No sudo.
REST="http://127.0.0.1:8091/sdrangel"
DS=1; UDP_PORT=9998
log(){ echo "[copytoudp-watchdog $(date '+%F %T')] $*"; }
udp_body(){ echo "{\"index\":$1,\"copyToUDP\":1,\"udpAddress\":\"127.0.0.1\",\"udpPort\":$UDP_PORT,\"udpUsesRTP\":0,\"udpChannelMode\":2,\"udpChannelCodec\":0,\"sampleRate\":48000}"; }
log "started (watching DS$DS + copyToUDP idx0 -> udp 127.0.0.1:$UDP_PORT)"
while true; do
  if ! curl -sS -m4 -o /dev/null "$REST/deviceset/$DS" 2>/dev/null; then
    sleep 30; continue   # SDRangel REST down; icecast+bridge keep running (silent)
  fi
  st=$(curl -sS -m4 "$REST/deviceset/$DS/device/run" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("state","?"))' 2>/dev/null)
  if [ "$st" != "running" ]; then
    log "DS$DS state=$st -> starting"
    curl -sS -m6 -X POST "$REST/deviceset/$DS/device/run" >/dev/null 2>&1
  fi
  read -r c0 p0 <<<"$(curl -sS -m4 "$REST/audio" 2>/dev/null | python3 -c "
import sys,json
for a in json.load(sys.stdin).get('outputDevices',[]):
    if a.get('index')==0: print(a.get('copyToUDP'),a.get('udpPort'))
" 2>/dev/null)"
  if [ "$c0" != "1" ] || [ "$p0" != "$UDP_PORT" ]; then
    log "copyToUDP(idx0)=[$c0 $p0] -> re-arming idx0 + idx-1"
    curl -sS -m6 -X PATCH "$REST/audio/output/parameters" -H "Content-Type: application/json" -d "$(udp_body 0)"  >/dev/null 2>&1
    curl -sS -m6 -X PATCH "$REST/audio/output/parameters" -H "Content-Type: application/json" -d "$(udp_body -1)" >/dev/null 2>&1
  fi
  sleep 30
done
