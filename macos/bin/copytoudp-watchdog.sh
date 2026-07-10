#!/bin/bash
# copytoudp-watchdog — keeps the SDRangel->icecast phone-audio path alive.
#   1. Re-arms copyToUDP on the PHYSICAL output device (idx 0) — the tap venus-audio-bridge.sh
#      reads (SDRangel drops it after a crash; it lives on the audio device, not a deviceset).
#   2. On SDRangel restart (pid change) OR periodically, runs sdrangel-restore.py to re-apply the
#      full scanner config: fixed gain (so the noise floor stays below squelch — no AGC-float hiss),
#      device centers, 4 airband AM channels + the 443.975 NFM, volumes. sdrangel-restore is
#      idempotent (skips when already correct), so calling it is cheap and non-disruptive.
# Read-only until drift, then minimal action. No speaker output changed. No sudo.
REST="http://127.0.0.1:8091/sdrangel"
UDP_PORT=9998
REPO="$HOME/scannerproject"
RESTORE="$REPO/macos/bin/sdrangel-restore.py"
SDRANGEL_MATCH="SDRangel.app/Contents/MacOS/SDRangel"
log(){ echo "[copytoudp-watchdog $(date '+%F %T')] $*"; }
udp_body(){ echo "{\"index\":$1,\"copyToUDP\":1,\"udpAddress\":\"127.0.0.1\",\"udpPort\":$UDP_PORT,\"udpUsesRTP\":0,\"udpChannelMode\":2,\"udpChannelCodec\":0,\"sampleRate\":48000}"; }
log "started (copyToUDP idx0 -> udp 127.0.0.1:$UDP_PORT ; sdrangel-restore on SDRangel restart)"
LAST_PID=""; CYCLE=0
while true; do
  if ! curl -sS -m4 -o /dev/null "$REST" 2>/dev/null; then sleep 30; continue; fi   # SDRangel REST down
  PID=$(pgrep -f "$SDRANGEL_MATCH" | head -1)
  # SDRangel (re)started, or first pass, or ~10-min safety sweep -> re-apply full config (idempotent)
  if [ -n "$PID" ] && { [ "$PID" != "$LAST_PID" ] || [ $((CYCLE % 20)) -eq 0 ]; }; then
    [ "$PID" != "$LAST_PID" ] && [ -n "$LAST_PID" ] && log "SDRangel restart ($LAST_PID -> $PID) — restoring config"
    [ -x "$RESTORE" ] && /usr/bin/python3 "$RESTORE" 2>&1 | sed 's/^/  restore: /'
    LAST_PID="$PID"
  fi
  # tap: re-arm copyToUDP on the physical device if it drifted (independent of the deviceset config)
  read -r c0 p0 <<<"$(curl -sS -m4 "$REST/audio" 2>/dev/null | python3 -c "
import sys,json
for a in json.load(sys.stdin).get('outputDevices',[]):
    if a.get('index')==0: print(a.get('copyToUDP'),a.get('udpPort'))
" 2>/dev/null)"
  if [ "$c0" != "1" ] || [ "$p0" != "$UDP_PORT" ]; then
    log "copyToUDP(idx0)=[$c0 $p0] -> re-arming"
    curl -sS -m6 -X PATCH "$REST/audio/output/parameters" -H "Content-Type: application/json" -d "$(udp_body 0)"  >/dev/null 2>&1
    curl -sS -m6 -X PATCH "$REST/audio/output/parameters" -H "Content-Type: application/json" -d "$(udp_body -1)" >/dev/null 2>&1
  fi
  CYCLE=$((CYCLE+1))
  sleep 30
done
