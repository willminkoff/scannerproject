#!/usr/bin/env bash
# fix-neptune-angel.sh — one-command recovery for the neptune-angel.mp3 icecast mount.
#
# When it 404s, the cause is almost always: an SDRangel restart left DS0 idle (a stopped
# device emits no audio), the copyToUDP tap needs a 0->1 toggle to (re)start its sender
# thread (plain REST arming does NOT start it), and/or an ORPHAN ffmpeg is stuck on
# udp://127.0.0.1:9998 so the bridge's restart loop can't bind the port ("Address already
# in use") and 404s forever. This script fixes all three.
#
# Neptune's DS0 is the RTL FRS/GMRS multiplex (15 NFMDemods + a low-vol keepalive) — this
# script does NOT rebuild those channels (unlike Venus's RSPduo airband set); it only heals
# the device/tap/bridge plumbing. Idempotent, ~8-16s, exit 0 = mount live / exit 1 = failed.
# Deps: curl + python3. Run from anywhere with SSH to Neptune, or on the box. Digital untouched.
set -uo pipefail
MOUNT="neptune-angel.mp3"
AUDIODEV="Mac mini Speakers"                     # the exact copyToUDP tap device (audio idx0)
AGENT="com.scannerproject.neptune-audio-bridge"
R="http://127.0.0.1:8091/sdrangel"
ICE="http://127.0.0.1:8000/${MOUNT}"
UDPPORT=9998
FFMATCH="ffmpeg.*udp://127.0.0.1:${UDPPORT}"     # targeted: ffmpeg holding OUR udp port only
UID_="$(id -u)"
fixed=""

code(){ curl -sS -m4 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
ds0(){ curl -sS -m4 "$R/deviceset/0" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('samplingDevice',{}).get('$1',''))" 2>/dev/null; }
mhz(){ curl -sS -m4 "$R/deviceset/0" 2>/dev/null | python3 -c "import sys,json;print(round(json.load(sys.stdin).get('samplingDevice',{}).get('centerFrequency',0)/1e6,3))" 2>/dev/null; }
ffcount(){ pgrep -f "$FFMATCH" 2>/dev/null | wc -l | tr -d ' '; }

# preconditions
[ "$(code http://127.0.0.1:8000/)" = "200" ] || { echo "fix-${MOUNT}: FAILED (icecast not answering on :8000)"; exit 1; }
[ "$(code "$R")" = "200" ]                    || { echo "fix-${MOUNT}: FAILED (SDRangel REST down — is SDRangel running?)"; exit 1; }

# fast path: already fully healthy (mount up AND exactly one bridge)
if [ "$(code "$ICE")" = "200" ] && [ "$(ffcount)" = "1" ]; then
  echo "fix-${MOUNT}: OK (already up, one bridge, $(mhz)MHz)"; exit 0
fi

# 1) start DS0 device if it came up idle
curl -sS -m6 -X POST "$R/deviceset/0/device/run" >/dev/null 2>&1; sleep 1
# 2) make sure channel 0 (the keepalive) outputs to the tapped device (resets a drift)
curl -sS -m5 -X PATCH "$R/deviceset/0/channel/0/settings" -H "Content-Type: application/json" \
  -d "{\"channelType\":\"NFMDemod\",\"direction\":0,\"NFMDemodSettings\":{\"audioDeviceName\":\"${AUDIODEV}\",\"audioMute\":0}}" >/dev/null 2>&1
# 3) toggle copyToUDP 0->1 to (re)start the sender thread
curl -sS -m5 -X PATCH "$R/audio/output/parameters" -H "Content-Type: application/json" -d '{"index":0,"copyToUDP":0}' >/dev/null 2>&1; sleep 1
curl -sS -m5 -X PATCH "$R/audio/output/parameters" -H "Content-Type: application/json" \
  -d '{"index":0,"copyToUDP":1,"udpAddress":"127.0.0.1","udpPort":9998,"udpChannelMode":2,"sampleRate":48000}' >/dev/null 2>&1; sleep 1

# 4) clear the UDP port: kill ANY ffmpeg holding it (orphans + current), targeted — never a
#    blanket `pkill ffmpeg`. This is the fix for the stuck-restart-loop 404.
before="$(ffcount)"
if [ "${before:-0}" -gt 0 ]; then
  pkill -f "$FFMATCH" 2>/dev/null; sleep 2
  pkill -9 -f "$FFMATCH" 2>/dev/null; sleep 2
fi
[ "${before:-0}" -gt 1 ] && fixed="${fixed}killed ${before} orphan ffmpeg, "
[ "${before:-0}" = "1" ] && fixed="${fixed}cleared stuck bridge, "

# 5) confirm copyToUDP is actually emitting now that the port is free (>0 bytes/3s)
BYTES=$(python3 - "$UDPPORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    print("-1"); sys.exit(0)
s.settimeout(3); n = b = 0
try:
    while True:
        d = s.recv(65535); n += 1; b += len(d)
        if n >= 600: break
except socket.timeout:
    pass
s.close(); print(b)
PY
)
if [ "${BYTES:-0}" = "-1" ]; then
  echo "fix-${MOUNT}: FAILED (could not free UDP ${UDPPORT}; an ffmpeg is still holding it). Re-run."; exit 1
fi
if [ "${BYTES:-0}" -le 0 ]; then
  echo "fix-${MOUNT}: FAILED (copyToUDP SILENT — 0 bytes/3s on ${UDPPORT}; DS0 $(ds0 state), all channels gated/keepalive not open)."; exit 1
fi

# 6) start exactly one fresh bridge
launchctl enable "gui/${UID_}/${AGENT}" >/dev/null 2>&1
launchctl kickstart -k "gui/${UID_}/${AGENT}" >/dev/null 2>&1
sleep 4
if [ "$(ffcount)" -gt 1 ]; then
  newest="$(pgrep -f "$FFMATCH" 2>/dev/null | tail -1)"
  for p in $(pgrep -f "$FFMATCH" 2>/dev/null); do [ "$p" != "$newest" ] && kill -9 "$p" 2>/dev/null; done
  fixed="${fixed}deduped bridges, "
fi

# 7) verify — mount 200 proves the whole chain (device->copyToUDP->bridge->icecast) is live
ok=0
for _ in 1 2 3 4; do
  sleep 3
  [ "$(code "$ICE")" = "200" ] && ok=$((ok+1))
done
fixed="${fixed%, }"; [ -z "$fixed" ] && fixed="toggled copyToUDP"
if [ "$ok" -ge 2 ] && [ "$(ffcount)" = "1" ]; then
  echo "fix-${MOUNT}: OK (${fixed}; mount 200 ${ok}/4, UDP ${BYTES}B/3s, DS0 $(ds0 state) $(mhz)MHz, 1 bridge)"; exit 0
fi
echo "fix-${MOUNT}: FAILED (mount 200 only ${ok}/4 after repair [${fixed}]; ffmpeg=$(ffcount), UDP ${BYTES}B/3s, DS0 $(ds0 state)). Check SDRangel is running and press Play on the device."
exit 1
