#!/usr/bin/env bash
#
# recover-sdrplay.sh — recover a wedged sdrplay_apiService and bring the chirp
# dual-tuner daemons (airband MASTER + ground SLAVE) and op25 back, in clean order.
#
# WHEN TO USE:
#   - the ground SLAVE hangs on sdrplay_api_Open (systemd shows it 'activating'
#     then timing out / auto-restarting), or
#   - any new sdrplay device Open hangs (the apiService is wedged for NEW clients
#     while existing ones keep streaming), or
#   - a full clean bounce is needed.
#
# Restarting the apiService is the ONLY thing that clears the new-Open wedge.
# It also drops op25's sdrplay session, so op25 is restarted last.
#
# Do NOT just cycle the chirp daemons without restarting the apiService — releasing
# a working connection into a wedged apiService leaves the MASTER unable to reopen.
#
# RUN AS ROOT:   sudo /usr/local/bin/recover-sdrplay.sh
#
set -uo pipefail

log(){ echo "[recover-sdrplay] $(date '+%H:%M:%S') $*"; }

# Block until a band's chirp daemon reports a real streaming level (not the -120
# "not yet streaming" placeholder). Returns non-zero if it never streams.
wait_stream(){  # $1 = band (airband|ground)
  local band="$1" i lvl
  for i in $(seq 1 30); do
    lvl=$(curl -s --max-time 3 "http://127.0.0.1:5050/api/chirp/${band}/status" 2>/dev/null \
      | python3 -c "import sys,json; d=json.load(sys.stdin); ch=d.get('channels',[]); print(round(ch[0].get('signal_level_dbfs',-999),1) if ch else -999)" 2>/dev/null)
    if python3 -c "import sys; sys.exit(0 if float('${lvl:--999}') > -110 else 1)" 2>/dev/null; then
      log "${band} streaming (lvl=${lvl}) after ${i}s"; return 0
    fi
    sleep 1
  done
  log "WARNING: ${band} did not reach streaming"; return 1
}

log "stopping chirp daemons (release RSPduo 1809063632)"
systemctl stop gr-demod@airband gr-demod@ground
sleep 3

log "restarting sdrplay apiService (clears new-Open wedge; drops op25 session)"
systemctl restart sdrplay.service
sleep 7
log "sdrplay=$(systemctl is-active sdrplay.service)"

log "starting MASTER (airband) on the fresh apiService"
systemctl start gr-demod@airband
wait_stream airband || true

log "starting SLAVE (ground)"
systemctl start gr-demod@ground
if ! wait_stream ground; then
  log "slave did not attach -> stopping it so it cannot churn the apiService"
  systemctl stop gr-demod@ground
fi

log "restarting op25 (its sdrplay session was invalidated by the apiService restart)"
systemctl restart scanner-digital-op25 || log "op25 restart failed/blocked (classifier?) — restart it manually"
sleep 5

log "FINAL: airband=$(systemctl is-active gr-demod@airband) ground=$(systemctl is-active gr-demod@ground) op25=$(systemctl is-active scanner-digital-op25) sdrplay=$(systemctl is-active sdrplay.service)"
