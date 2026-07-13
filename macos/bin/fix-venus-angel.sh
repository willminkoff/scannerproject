#!/usr/bin/env bash
# fix-venus-angel.sh — one-command recovery for the venus-angel.mp3 icecast mount.
#
# On Venus the copytoudp-watchdog normally auto-heals this, so you rarely need it — this is
# a manual backup identical in spirit to fix-neptune-angel.sh. Same failure modes: DS0 idle
# after an SDRangel restart, and/or copyToUDP needing a 0->1 toggle to (re)start its sender.
#
# Idempotent, ~5-12s, exit 0 = mount live, exit 1 = failed. Deps: curl + python3 (system).
set -uo pipefail
MOUNT="venus-angel.mp3"
AUDIODEV="System default device"                 # matches Venus's sdrangel-restore audio route
AGENT="com.scannerproject.venus-audio-bridge"
R="http://127.0.0.1:8091/sdrangel"
ICE="http://127.0.0.1:8000/${MOUNT}"
UID_="$(id -u)"

code(){ curl -sS -m4 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
ds0(){ curl -sS -m4 "$R/deviceset/0" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('samplingDevice',{}).get('$1',''))" 2>/dev/null; }
mhz(){ curl -sS -m4 "$R/deviceset/0" 2>/dev/null | python3 -c "import sys,json;print(round(json.load(sys.stdin).get('samplingDevice',{}).get('centerFrequency',0)/1e6,3))" 2>/dev/null; }

[ "$(code http://127.0.0.1:8000/)" = "200" ] || { echo "fix-${MOUNT}: FAILED (icecast not answering on :8000)"; exit 1; }
[ "$(code "$R")" = "200" ]                    || { echo "fix-${MOUNT}: FAILED (SDRangel REST down — is SDRangel running?)"; exit 1; }
if [ "$(code "$ICE")" = "200" ]; then echo "fix-${MOUNT}: OK (already up, $(mhz)MHz)"; exit 0; fi

# 1) start DS0 device if idle
curl -sS -m6 -X POST "$R/deviceset/0/device/run" >/dev/null 2>&1; sleep 1
# 2) AM channel 0 -> tapped device (watchdog maintains all channels; this is belt-and-suspenders)
curl -sS -m5 -X PATCH "$R/deviceset/0/channel/0/settings" -H "Content-Type: application/json" \
  -d "{\"channelType\":\"AMDemod\",\"direction\":0,\"AMDemodSettings\":{\"audioDeviceName\":\"${AUDIODEV}\",\"audioMute\":0}}" >/dev/null 2>&1
# 3) toggle copyToUDP 0->1
curl -sS -m5 -X PATCH "$R/audio/output/parameters" -H "Content-Type: application/json" -d '{"index":0,"copyToUDP":0}' >/dev/null 2>&1; sleep 1
curl -sS -m5 -X PATCH "$R/audio/output/parameters" -H "Content-Type: application/json" \
  -d '{"index":0,"copyToUDP":1,"udpAddress":"127.0.0.1","udpPort":9998,"udpChannelMode":2,"sampleRate":48000}' >/dev/null 2>&1; sleep 1
# 4) kick the bridge
launchctl kickstart -k "gui/${UID_}/${AGENT}" >/dev/null 2>&1

for _ in 1 2 3; do
  sleep 3
  if [ "$(code "$ICE")" = "200" ]; then
    echo "fix-${MOUNT}: OK (mount 200, UDP flowing, DS0 $(ds0 state), freq $(mhz)MHz)"; exit 0
  fi
done
echo "fix-${MOUNT}: FAILED (mount still 404; DS0 state=$(ds0 state)). Check SDRangel is running and press Play on the device."
exit 1
