#!/usr/bin/env bash
# fix-venus-angel.sh — one-command, end-to-end recovery for the venus-angel.mp3 icecast mount.
#
# Handles the full failure class seen on Venus (2026-07-15):
#   1. Orphan ffmpeg stuck on udp://127.0.0.1:9998 — a leftover sender jams the port so the
#      bridge's restart loop can't bind ("Address already in use") and 404s forever. Killed
#      (targeted, never a blanket `pkill ffmpeg`) before a fresh single bridge is started.
#   2. SDRangel drops ALL channels on restart — DS0 comes back running but empty (channel
#      report returns just {"message"}). Rebuilds the 4 Nashville airband AMDemod channels.
#   3. No keepalive — with every channel squelched during quiet air, copyToUDP emits 0 bytes
#      and icecast times the source out (mount 200-then-404). Ensures one always-open,
#      low-volume keepalive channel (118.400, squelch -100, vol 0.4) so UDP flow is constant.
#   4. copyToUDP needs a 0->1 toggle to (re)start its sender thread (plain REST arming won't).
#
# Verifies UDP is actually flowing (>0 bytes/3s) BEFORE starting the bridge, then confirms
# exactly one bridge, mount 200, and a live icecast source. Idempotent, ~8-18s, exit 0 = mount
# live / exit 1 = failed. Deps: curl + python3 (system). Digital (venus-trunk) untouched.
set -uo pipefail
MOUNT="venus-angel.mp3"
AGENT="com.scannerproject.venus-audio-bridge"
R="http://127.0.0.1:8091/sdrangel"
ICE="http://127.0.0.1:8000/${MOUNT}"
UDPPORT=9998
FFMATCH="ffmpeg.*udp://127.0.0.1:${UDPPORT}"      # targeted: ffmpeg holding OUR udp port only
UID_="$(id -u)"

code(){ curl -sS -m4 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
ds0(){ curl -sS -m4 "$R/deviceset/0" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('samplingDevice',{}).get('$1',''))" 2>/dev/null; }
mhz(){ curl -sS -m4 "$R/deviceset/0" 2>/dev/null | python3 -c "import sys,json;print(round(json.load(sys.stdin).get('samplingDevice',{}).get('centerFrequency',0)/1e6,3))" 2>/dev/null; }
ffcount(){ pgrep -f "$FFMATCH" 2>/dev/null | wc -l | tr -d ' '; }
fixed=""   # accumulates what got repaired, for the one-line summary

# ---- preconditions ----
[ "$(code http://127.0.0.1:8000/)" = "200" ] || { echo "fix-${MOUNT}: FAILED (icecast not answering on :8000)"; exit 1; }
[ "$(code "$R")" = "200" ]                    || { echo "fix-${MOUNT}: FAILED (SDRangel REST down — is SDRangel running?)"; exit 1; }

# ---- fast path: already fully healthy (mount up AND exactly one bridge) ----
if [ "$(code "$ICE")" = "200" ] && [ "$(ffcount)" = "1" ]; then
  echo "fix-${MOUNT}: OK (already up, one bridge, $(mhz)MHz)"; exit 0
fi

# ---- 1) ensure DS0 device running + channels + keepalive + copyToUDP (via REST) ----
CH=$(python3 - "$R" <<'PY'
import json, sys, time, urllib.request, urllib.error
B = sys.argv[1]
def req(m, p, body=None, t=10):
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + p, data=d, method=m, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=t) as x: return x.status, (json.loads(x.read() or b'{}'))
    except urllib.error.HTTPError as e: return e.code, {}
    except Exception: return None, {}

# resolve the real tap device = audio OUTPUT index 0 (Venus: "Mac mini Speakers").
# Routing channels to this exact name is what makes copyToUDP actually emit; the literal
# "System default device" string does NOT reliably tap (same gotcha as Neptune).
_, au = req("GET", "/audio")
tap = next((a.get("name") for a in (au.get("outputDevices") or []) if a.get("index") == 0), None) or "System default device"
AUDIO = {"audioDeviceName": tap, "audioMute": 0}

# ensure DS0 device is running
_, ds = req("GET", "/deviceset/0"); sd = ds.get("samplingDevice", {})
if sd.get("state") != "running":
    req("POST", "/deviceset/0/device/run"); time.sleep(3)

# the canonical Nashville airband set; ch0 is the always-open low-volume keepalive
CHANS = [
    (-525000, "118.400 KBNA App/Dep East (keepalive)", -100, 0.4),
    (-325000, "118.600 KBNA Tower",                     -45,  4.5),
    ( 425000, "119.350 KBNA App/Dep West",              -45,  4.5),
    ( 525000, "119.450 KJWN Tower",                     -45,  4.5),
]

def am_channels():
    _, d = req("GET", "/deviceset/0"); out = []
    for i, c in enumerate(d.get("channels", [])):
        if "AMDemod" in (c.get("channelType") or c.get("id") or ""):
            _, cs = req("GET", "/deviceset/0/channel/%d/settings" % i)
            out.append((i, (cs.get("AMDemodSettings") or {})))
    return out

ams = am_channels()
action = "channels-ok"

if len(ams) < 4:
    # DS0 empty or missing the expected set -> full rebuild
    req("PATCH", "/deviceset/0/device/settings", {"deviceHwType": "SDRplayV3", "direction": 0,
        "sdrPlayV3Settings": {"tuner": 0, "centerFrequency": 118925000, "devSampleRate": 2000000,
                              "bandwidthIndex": 3, "ifAGC": 0, "ifGain": -40, "lnaIndex": 3, "log2Decim": 0}})
    time.sleep(1.5)
    _, d = req("GET", "/deviceset/0")
    for i in range(len(d.get("channels", [])) - 1, -1, -1):
        req("DELETE", "/deviceset/0/channel/%d" % i); time.sleep(0.4)
    for off, title, sq, vol in CHANS:
        req("POST", "/deviceset/0/channel", {"channelType": "AMDemod", "direction": 0}); time.sleep(0.4)
        _, d = req("GET", "/deviceset/0"); idx = len(d.get("channels", [])) - 1
        req("PATCH", "/deviceset/0/channel/%d/settings" % idx, {"channelType": "AMDemod", "direction": 0,
            "AMDemodSettings": {"inputFrequencyOffset": off, "rfBandwidth": 8000, "squelch": sq,
                                "volume": vol, "title": title, **AUDIO}})
        time.sleep(0.4)
    action = "rebuilt-4ch"
else:
    # channels present -> just make sure a keepalive (always-open, low-vol) exists
    has_keepalive = any((s.get("squelch") is not None and s.get("squelch") <= -95) for _, s in ams)
    if not has_keepalive:
        i0, s0 = ams[0]
        s0u = dict(s0); s0u.update({"squelch": -100, "volume": 0.4, **AUDIO})
        req("PATCH", "/deviceset/0/channel/%d/settings" % i0, {"channelType": "AMDemod", "direction": 0, "AMDemodSettings": s0u})
        time.sleep(0.4)
        action = "added-keepalive"
    # belt-and-suspenders: keep all AM channels pointed at the tap device
    for i, s in ams:
        su = dict(s); su.update(AUDIO)
        req("PATCH", "/deviceset/0/channel/%d/settings" % i, {"channelType": "AMDemod", "direction": 0, "AMDemodSettings": su})
        time.sleep(0.2)

# copyToUDP 0->1 to (re)start the sender thread
req("PATCH", "/audio/output/parameters", {"index": 0, "copyToUDP": 0}); time.sleep(1.2)
req("PATCH", "/audio/output/parameters", {"index": 0, "copyToUDP": 1, "udpAddress": "127.0.0.1",
    "udpPort": 9998, "udpChannelMode": 2, "sampleRate": 48000}); time.sleep(1.5)

_, d = req("GET", "/deviceset/0")
print("%s|%s|%d" % (action, tap, len(d.get("channels", []))))
PY
)
CH_ACTION="${CH%%|*}"; rest="${CH#*|}"; TAP="${rest%%|*}"; NCHAN="${rest##*|}"
[ "$CH_ACTION" = "rebuilt-4ch" ]    && fixed="${fixed}rebuilt 4 channels, "
[ "$CH_ACTION" = "added-keepalive" ] && fixed="${fixed}added keepalive, "

# ---- 2) clear the UDP port: kill ANY ffmpeg holding it (orphans + current), targeted ----
before="$(ffcount)"
if [ "${before:-0}" -gt 0 ]; then
  pkill -f "$FFMATCH" 2>/dev/null; sleep 2
  pkill -9 -f "$FFMATCH" 2>/dev/null; sleep 2
fi
[ "${before:-0}" -gt 1 ] && fixed="${fixed}killed ${before} orphan ffmpeg, "
[ "${before:-0}" = "1" ] && fixed="${fixed}cleared stuck bridge, "

# ---- 3) verify copyToUDP is actually emitting (port is free now) ----
BYTES=$(python3 - "$UDPPORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    print("-1"); sys.exit(0)      # something still holds it
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
  echo "fix-${MOUNT}: FAILED (copyToUDP SILENT — 0 bytes/3s on ${UDPPORT}; DS0 $(ds0 state), keepalive/squelch not open). SDRangel producing no audio."; exit 1
fi

# ---- 4) start exactly one fresh bridge ----
launchctl enable "gui/${UID_}/${AGENT}" >/dev/null 2>&1
launchctl kickstart -k "gui/${UID_}/${AGENT}" >/dev/null 2>&1
sleep 4
# converge to exactly one bridge (kickstart -k restarts the launchd job; guard against dup ffmpegs)
if [ "$(ffcount)" -gt 1 ]; then
  newest="$(pgrep -f "$FFMATCH" 2>/dev/null | tail -1)"
  for p in $(pgrep -f "$FFMATCH" 2>/dev/null); do [ "$p" != "$newest" ] && kill -9 "$p" 2>/dev/null; done
  fixed="${fixed}deduped bridges, "
fi

# ---- 5) final verify: mount 200 (sustained) + icecast source live ----
ok=0
for _ in 1 2 3 4; do
  sleep 3
  [ "$(code "$ICE")" = "200" ] && ok=$((ok+1))
done
SRC=$(curl -sS -m5 "http://127.0.0.1:8000/status-json.xsl" 2>/dev/null | python3 -c "
import sys,json
try:
    src=(json.load(sys.stdin).get('icestats',{}) or {}).get('source',[])
    src=[src] if isinstance(src,dict) else (src or [])
    print('yes' if any('$MOUNT' in (s.get('listenurl','')) for s in src) else 'no')
except Exception: print('?')" 2>/dev/null)

fixed="${fixed%, }"; [ -z "$fixed" ] && fixed="no repair needed"
if [ "$ok" -ge 2 ] && [ "$(ffcount)" = "1" ]; then
  echo "fix-${MOUNT}: OK (${fixed}; mount 200 ${ok}/4, source=${SRC}, UDP ${BYTES}B/3s, ${CH_ACTION}, tap='${TAP}', DS0 $(ds0 state) $(mhz)MHz, 1 bridge)"
  exit 0
fi
echo "fix-${MOUNT}: FAILED (mount 200 only ${ok}/4 after repair [${fixed}]; ffmpeg=$(ffcount), source=${SRC}, UDP ${BYTES}B/3s, DS0 $(ds0 state))"
exit 1
