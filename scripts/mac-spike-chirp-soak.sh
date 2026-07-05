#!/bin/bash
# mac-spike-chirp-soak.sh — SB7.1 GO/NO-GO GATE: the 48 h chirp-on-macOS soak.
#
# WHY (docs/sb7-northstar-program.md §4.3 / SB7.1): chirp's GR + SoapySDR +
# SDRplay chain has NO documented long-term headless operators on macOS — it
# is the least-proven link in the single-box M1 plan, and the whole plan
# pivots on it. This harness runs the chirp airband daemon with the SB7.3
# nets ARMED (source validator, audio-flow probe, metrics) for N hours,
# samples /metrics every 5 minutes, and grades the run in $OUT/RESULT.md.
# PASS here is the precondition for migrating any hardware off micro.
#
# PYTHON CHOICE (deliberate): the daemon runs under the RADIOCONDA python,
# /opt/scannerproject/radioconda/bin/python3 — gnuradio / gr-soapy / osmosdr /
# SoapySDR live ONLY in that env (installed by mac-bootstrap.sh --gr).
# The /opt/scannerproject/venv python is plain CPython for the UI and cannot
# import gnuradio. Override with SPIKE_PYTHON=/path/to/python if needed.
#
# USAGE:
#   mac-spike-chirp-soak.sh [--duration <hours, default 48>] [--band <airband>]
#                           [--metrics-port <9101>] [--out <dir>] [--dry-run]
#   --out default: /opt/scannerproject/log/spike-YYYYmmdd-HHMM
#   --dry-run: validate the environment, resolve the python, print the exact
#              plan, exit 0. Works with no SDR / no radioconda present.
#
# ENV KNOBS (all optional):
#   SPIKE_PYTHON             daemon python (default: radioconda python above)
#   SPIKE_INTERVAL_S         sampling interval, default 300
#   SPIKE_ICECAST_PASS       icecast source password (or ICECAST_SOURCE_PASSWORD)
#   CHIRP_AUDIO_OUT          full sink spec, overrides the built icecast spec
#   CHIRP_SOURCE             default "sdr" (the point of the spike); any other
#                            CHIRP_* var in the environment passes through too
#
# PASS criteria (graded into $OUT/RESULT.md):
#   1. daemon never exited unexpectedly
#   2. chirp_config_load_status == 1 on every successful scrape
#   3. chirp_audio_branch_silent == 0 on >= 99% of samples
#   4. icecast byte rate >= 0.8x configured bitrate on >= 95% of rate samples
#   5. RSS growth (last vs first sample) < 20%
#
# Interrupt-safe: INT/TERM -> clean SIGTERM shutdown of the daemon, RESULT.md
# written with verdict INCOMPLETE (exit 2). PASS exits 0, FAIL exits 1.
# Transient scrape failures are recorded (curl_ok=0), never fatal.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="/opt/scannerproject"
SPIKE_PY="${SPIKE_PYTHON:-$PREFIX/radioconda/bin/python3}"
INTERVAL_S="${SPIKE_INTERVAL_S:-300}"

DURATION_H=48
BAND="airband"
METRICS_PORT=9101
OUT=""
DRY_RUN=0

usage() { sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --duration)     DURATION_H="${2:?--duration needs a value}"; shift 2 ;;
    --band)         BAND="${2:?--band needs a value}"; shift 2 ;;
    --metrics-port) METRICS_PORT="${2:?--metrics-port needs a value}"; shift 2 ;;
    --out)          OUT="${2:?--out needs a value}"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "unknown arg: $1 (see --help)"; exit 2 ;;
  esac
done

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

# ---------- resolve the run -----------------------------------------------------
[ -n "$OUT" ] || OUT="$PREFIX/log/spike-$(date +%Y%m%d-%H%M)"
DURATION_S="$(awk -v h="$DURATION_H" 'BEGIN{ if (h+0 <= 0) { print "bad"; exit } printf "%d", h*3600 }')"
[ "$DURATION_S" != "bad" ] || die "--duration must be a positive number of hours (got '$DURATION_H')"

CFG="$REPO/chirp/config/${BAND}.json"
SRC="${CHIRP_SOURCE:-sdr}"

# Configured bitrate: env override > per-band JSON > daemon default (32 kbps).
if [ -n "${CHIRP_ICECAST_BITRATE_KBPS:-}" ]; then
  BITRATE_KBPS="$CHIRP_ICECAST_BITRATE_KBPS"; BITRATE_SRC="env CHIRP_ICECAST_BITRATE_KBPS"
elif [ -f "$CFG" ]; then
  BITRATE_KBPS="$(sed -n 's/.*"icecast_bitrate_kbps"[^0-9]*\([0-9][0-9]*\).*/\1/p' "$CFG" | head -1)"
  BITRATE_SRC="$CFG"
fi
[ -n "${BITRATE_KBPS:-}" ] || { BITRATE_KBPS=32; BITRATE_SRC="daemon default"; }
# 0.8 x (kbps * 1000 / 8) bytes/sec == kbps * 100
RATE_MIN_BPS=$((BITRATE_KBPS * 100))

# Audio sink: chirp_audio_bytes_published_total only exists for the icecast
# sink, so the byte-rate criterion REQUIRES an icecast audio-out. /CHIRP_SPIKE
# is deliberately non-production (daemon refuses /ANALOG.mp3 et al by default).
AUDIO_OUT=""; AUDIO_OUT_SRC=""
if [ -n "${CHIRP_AUDIO_OUT:-}" ]; then
  AUDIO_OUT="$CHIRP_AUDIO_OUT"; AUDIO_OUT_SRC="env CHIRP_AUDIO_OUT"
else
  _pass="${SPIKE_ICECAST_PASS:-${ICECAST_SOURCE_PASSWORD:-}}"
  if [ -n "$_pass" ]; then
    AUDIO_OUT="icecast:${ICECAST_HOST:-127.0.0.1}:${ICECAST_PORT:-8000}:/CHIRP_SPIKE.mp3:${_pass}"
    AUDIO_OUT_SRC="built (host ${ICECAST_HOST:-127.0.0.1}:${ICECAST_PORT:-8000}, pass from SPIKE_ICECAST_PASS/ICECAST_SOURCE_PASSWORD)"
  fi
fi
AUDIO_OUT_REDACTED="$(printf '%s' "$AUDIO_OUT" | sed 's/:[^:]*$/:*****/')"

STATE_PATH="${CHIRP_STATE_PATH:-$OUT/chirp_${BAND}.state.json}"
HIT_LOG="${CHIRP_HIT_LOG:-$OUT/${BAND}_hits.jsonl}"
METRICS_URL="http://127.0.0.1:${METRICS_PORT}/metrics"

CSV="$OUT/samples.csv"
LOG="$OUT/daemon.log"
RESULT="$OUT/RESULT.md"

# ---------- preflight (shared by --dry-run and the real run) --------------------
PREFLIGHT_HARD_FAIL=0
hard() { # dry-run: warn + remember; real run: die
  if [ "$DRY_RUN" = "1" ]; then warn "$* [would be FATAL on a real run]"; PREFLIGHT_HARD_FAIL=1
  else die "$*"; fi
}

preflight() {
  step "Preflight"
  [ -f "$REPO/chirp/daemon.py" ] && ok "chirp daemon module: $REPO/chirp/daemon.py" \
    || hard "chirp/daemon.py not found under $REPO"
  if [ -f "$CFG" ]; then
    ok "band config: $CFG (bitrate ${BITRATE_KBPS} kbps from ${BITRATE_SRC})"
  else
    hard "band config missing: $CFG (CHIRP_CONFIG_REQUIRED=1 would hard-fail the daemon)"
  fi
  command -v curl >/dev/null 2>&1 && ok "curl present" || hard "curl not found"

  if [ -x "$SPIKE_PY" ]; then
    ok "daemon python: $SPIKE_PY ($("$SPIKE_PY" --version 2>&1))"
    if "$SPIKE_PY" -c "from gnuradio import gr, soapy; import osmosdr, SoapySDR" >/dev/null 2>&1; then
      ok "GR imports verified (gnuradio, gr-soapy, osmosdr, SoapySDR)"
    else
      hard "python at $SPIKE_PY cannot import the GR stack — run scripts/mac-bootstrap.sh --gr"
    fi
  else
    hard "daemon python not found: $SPIKE_PY — run scripts/mac-bootstrap.sh --gr (or set SPIKE_PYTHON)"
  fi

  if [ -n "$AUDIO_OUT" ]; then
    ok "audio out: $AUDIO_OUT_REDACTED (${AUDIO_OUT_SRC})"
    case "$AUDIO_OUT" in
      icecast:*)
        _host_port="$(printf '%s' "$AUDIO_OUT" | cut -d: -f2-3 | tr ':' ' ')"
        # shellcheck disable=SC2086
        if curl -fsS -m 3 -o /dev/null "http://$(printf '%s' "$_host_port" | tr ' ' ':')/" 2>/dev/null; then
          ok "icecast reachable"
        else
          warn "icecast not reachable at $_host_port — daemon would fall back to file output and the byte-rate criterion would FAIL (start icecast first)"
        fi ;;
      *) warn "non-icecast audio out — chirp_audio_bytes_published_total will be absent; byte-rate criterion will FAIL" ;;
    esac
  else
    hard "no icecast source password: set SPIKE_ICECAST_PASS (or ICECAST_SOURCE_PASSWORD, or a full CHIRP_AUDIO_OUT) — the byte-rate PASS criterion needs an icecast sink"
  fi

  if curl -fsS -m 2 -o /dev/null "$METRICS_URL" 2>/dev/null; then
    hard "something is ALREADY serving $METRICS_URL — another chirp daemon? Stop it or pick another --metrics-port"
  else
    ok "metrics port $METRICS_PORT free"
  fi

  _out_parent="$(dirname "$OUT")"
  if [ -d "$_out_parent" ] && [ -w "$_out_parent" ]; then
    ok "out dir parent writable: $_out_parent"
  elif [ "$DRY_RUN" = "1" ]; then
    warn "out dir parent missing/unwritable: $_out_parent (mac-bootstrap.sh creates $PREFIX/log)"
  else
    mkdir -p "$_out_parent" 2>/dev/null || die "cannot create $_out_parent"
  fi
}

print_plan() {
  step "Plan"
  echo "  repo            : $REPO"
  echo "  band            : $BAND"
  echo "  duration        : ${DURATION_H} h (${DURATION_S} s)"
  echo "  sample interval : ${INTERVAL_S} s (SPIKE_INTERVAL_S) -> ~$((DURATION_S / INTERVAL_S)) samples"
  echo "  out dir         : $OUT"
  echo "  daemon python   : $SPIKE_PY   <- radioconda env: gnuradio lives here,"
  echo "                    NOT in $PREFIX/venv (that is plain CPython for the UI)"
  echo "  daemon command  : cd $REPO && python -m chirp.daemon"
  echo "  metrics         : $METRICS_URL"
  echo
  echo "  daemon env (nets ARMED):"
  echo "    CHIRP_BAND=$BAND"
  echo "    CHIRP_SOURCE=$SRC"
  echo "    CHIRP_SOURCE_VALIDATE=1        <- SDR contract check gates startup"
  echo "    CHIRP_AUDIO_PROBE_ENABLED=1    <- -180 dBFS wedge detector armed"
  echo "    CHIRP_METRICS_ENABLED=1  CHIRP_METRICS_PORT=$METRICS_PORT"
  echo "    CHIRP_CONFIG_REQUIRED=1        <- missing config = hard fail"
  echo "    CHIRP_STATE_PATH=$STATE_PATH"
  echo "    CHIRP_HIT_LOG=$HIT_LOG"
  echo "    CHIRP_AUDIO_OUT=${AUDIO_OUT_REDACTED:-(UNRESOLVED — needs SPIKE_ICECAST_PASS)}"
  echo "    (+ any CHIRP_* already in the environment passes through)"
  echo
  echo "  PASS criteria:"
  echo "    1. no unexpected daemon exit"
  echo "    2. chirp_config_load_status == 1 on every successful scrape"
  echo "    3. chirp_audio_branch_silent == 0 on >= 99% of samples"
  echo "    4. byte rate >= ${RATE_MIN_BPS} B/s (0.8 x ${BITRATE_KBPS} kbps) on >= 95% of rate samples"
  echo "    5. RSS growth < 20% (last vs first)"
  echo
  echo "  note: the daemon boots with the channel pool restored from CHIRP_STATE_PATH;"
  echo "        seed/adjust channels live via: python -m chirp.cli --port 7400 add-channel ..."
}

preflight
print_plan

if [ "$DRY_RUN" = "1" ]; then
  step "Dry run complete"
  if [ "$PREFLIGHT_HARD_FAIL" = "1" ]; then
    warn "one or more preflight items above would be FATAL on a real run"
  else
    ok "environment looks ready for the real soak"
  fi
  exit 0
fi

# ================================ REAL RUN ======================================
mkdir -p "$OUT" || die "cannot create out dir $OUT"
cp "$CFG" "$OUT/config_snapshot.json" 2>/dev/null || true
{ env | grep -E '^(CHIRP_|SPIKE_|ICECAST_)' | sed 's/\(PASS[^=]*\|PASSWORD\)=.*/\1=*****/' \
    | sed 's/^\(CHIRP_AUDIO_OUT=icecast:.*\):[^:]*$/\1:*****/'; true; } > "$OUT/env.txt"
echo "ts_epoch,ts_iso,curl_ok,config_load_status,audio_branch_silent,audio_bytes_total,byte_rate_Bps,daemon_alive,rss_kb" > "$CSV"

STARTED_EPOCH="$(date +%s)"
STARTED_ISO="$(date -u -r "$STARTED_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"

step "Launching chirp daemon (band=$BAND, source=$SRC) under $SPIKE_PY"
(
  cd "$REPO"
  exec env \
    PYTHONPATH="$REPO" \
    PYTHONUNBUFFERED=1 \
    CHIRP_BAND="$BAND" \
    CHIRP_SOURCE="$SRC" \
    CHIRP_SOURCE_VALIDATE=1 \
    CHIRP_AUDIO_PROBE_ENABLED=1 \
    CHIRP_METRICS_ENABLED=1 \
    CHIRP_METRICS_PORT="$METRICS_PORT" \
    CHIRP_CONFIG_REQUIRED=1 \
    CHIRP_STATE_PATH="$STATE_PATH" \
    CHIRP_HIT_LOG="$HIT_LOG" \
    CHIRP_AUDIO_OUT="$AUDIO_OUT" \
    "$SPIKE_PY" -m chirp.daemon
) >> "$LOG" 2>&1 &
DAEMON_PID=$!
ok "daemon pid $DAEMON_PID (log: $LOG)"

INTERRUPTED=0
STOP_REQUESTED=0
UNEXPECTED_EXIT=0
DAEMON_EXIT_CODE=""

on_signal() {
  INTERRUPTED=1
  STOP_REQUESTED=1
  warn "signal received — shutting the daemon down cleanly + finalizing RESULT"
}
trap on_signal INT TERM

daemon_alive() { kill -0 "$DAEMON_PID" 2>/dev/null; }

record_unexpected_exit() {
  set +e
  wait "$DAEMON_PID" 2>/dev/null
  DAEMON_EXIT_CODE=$?
  set -e
  UNEXPECTED_EXIT=1
  local why=""
  case "$DAEMON_EXIT_CODE" in
    2) why=" (source contract validation failure)" ;;
    3) why=" (config load failure)" ;;
  esac
  warn "DAEMON EXITED UNEXPECTEDLY: code ${DAEMON_EXIT_CODE}${why}"
  { echo "exit_code=${DAEMON_EXIT_CODE}${why}"
    echo "exited_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "--- last 100 daemon log lines ---"
    tail -100 "$LOG" 2>/dev/null
  } > "$OUT/daemon_exit_tail.log" || true
  warn "exit code + last 100 log lines -> $OUT/daemon_exit_tail.log"
}

shutdown_daemon() {
  daemon_alive || return 0
  warn "stopping daemon pid $DAEMON_PID (SIGTERM, graceful drain)"
  kill -TERM "$DAEMON_PID" 2>/dev/null || true
  local i=0
  while [ "$i" -lt 30 ]; do
    daemon_alive || { ok "daemon stopped cleanly"; return 0; }
    sleep 1 || true
    i=$((i+1))
  done
  warn "daemon did not stop in 30 s — SIGKILL"
  kill -KILL "$DAEMON_PID" 2>/dev/null || true
}

# metric_value <body> <metric_name>: value of the first sample of that family.
metric_value() {
  printf '%s\n' "$1" | awk -v m="$2" 'index($0, m"{")==1 { print $NF; exit }'
}

PREV_BYTES=""
PREV_TS=""

take_sample() {
  local ts="$1" iso curl_ok=1 body cls="" abs="" bytes="" rate="" alive=1 rss=""
  iso="$(date -u -r "$ts" +%Y-%m-%dT%H:%M:%SZ)"
  body="$(curl -fsS -m 15 "$METRICS_URL" 2>/dev/null)" || { curl_ok=0; body=""; }
  if [ "$curl_ok" = "1" ]; then
    cls="$(metric_value "$body" chirp_config_load_status)"
    abs="$(metric_value "$body" chirp_audio_branch_silent)"
    bytes="$(metric_value "$body" chirp_audio_bytes_published_total)"
    if [ -n "$bytes" ] && [ -n "$PREV_BYTES" ] && [ "$ts" -gt "${PREV_TS:-0}" ]; then
      rate="$(awk -v b="$bytes" -v pb="$PREV_BYTES" -v dt="$((ts - PREV_TS))" \
        'BEGIN{ r=(b-pb)/dt; if (r<0) r=0; printf "%.1f", r }')"
    fi
    if [ -n "$bytes" ]; then PREV_BYTES="$bytes"; PREV_TS="$ts"; fi
  fi
  daemon_alive || alive=0
  if [ "$alive" = "1" ]; then
    rss="$(ps -o rss= -p "$DAEMON_PID" 2>/dev/null | tr -d ' ')" || rss=""
  fi
  echo "$ts,$iso,$curl_ok,$cls,$abs,$bytes,$rate,$alive,$rss" >> "$CSV"
}

# ---------- wait for /metrics ---------------------------------------------------
step "Waiting for $METRICS_URL (metrics bind before config load)"
METRICS_UP=0
i=0
while [ "$i" -lt 60 ]; do
  daemon_alive || break
  if curl -fsS -m 2 -o /dev/null "$METRICS_URL" 2>/dev/null; then METRICS_UP=1; break; fi
  sleep 1 || true
  i=$((i+1))
done
if [ "$METRICS_UP" = "1" ]; then
  ok "metrics endpoint up after ~${i}s"
elif daemon_alive; then
  warn "metrics endpoint not up after 60 s but daemon alive — continuing; scrapes will record curl_ok=0"
fi

# ---------- sampling loop (every ${INTERVAL_S}s, interrupt-safe) ------------------
step "Soaking for ${DURATION_H} h — sampling every ${INTERVAL_S} s (CSV: $CSV)"
END_EPOCH=$((STARTED_EPOCH + DURATION_S))
NEXT_SAMPLE="$(date +%s)"
while :; do
  [ "$STOP_REQUESTED" = "1" ] && break
  now="$(date +%s)"
  [ "$now" -ge "$END_EPOCH" ] && break
  if ! daemon_alive; then
    record_unexpected_exit
    break
  fi
  if [ "$now" -ge "$NEXT_SAMPLE" ]; then
    take_sample "$now"
    NEXT_SAMPLE=$((now + INTERVAL_S))
  fi
  sleep 5 || true
done

ENDED_EPOCH="$(date +%s)"
ENDED_ISO="$(date -u -r "$ENDED_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
ACTUAL_H="$(awk -v s="$((ENDED_EPOCH - STARTED_EPOCH))" 'BEGIN{printf "%.2f", s/3600}')"

# Clean shutdown on the normal / interrupted paths (a daemon we stop ourselves
# is NOT an unexpected exit).
if [ "$UNEXPECTED_EXIT" = "0" ]; then
  shutdown_daemon
fi

# ---------- grade the run --------------------------------------------------------
step "Grading $CSV"
# fields: 1 ts 2 iso 3 curl_ok 4 cfg 5 silent 6 bytes 7 rate 8 alive 9 rss
STATS="$(awk -F, -v minrate="$RATE_MIN_BPS" '
  NR > 1 {
    n++
    if ($3 == 1) {
      scr++
      if ($4 != "") { cfg_seen++; if ($4 != 1) cfg_bad++ }
      if ($5 != "") { abs_n++;  if ($5 == 0) abs_ok++ }
    }
    if ($7 != "") { rate_n++; if ($7 + 0 >= minrate) rate_ok++ }
    if ($9 != "") { if (first_rss == "") first_rss = $9; last_rss = $9
                    if ($9 + 0 > max_rss) max_rss = $9 }
  }
  END {
    printf "%d %d %d %d %d %d %d %d %s %s %s", \
      n+0, scr+0, cfg_seen+0, cfg_bad+0, abs_n+0, abs_ok+0, rate_n+0, rate_ok+0, \
      (first_rss==""?"-":first_rss), (last_rss==""?"-":last_rss), (max_rss==""?"0":max_rss)
  }' "$CSV")"
# shellcheck disable=SC2086
set -- $STATS
N_SAMPLES=$1; N_SCRAPES=$2; CFG_SEEN=$3; CFG_BAD=$4; ABS_N=$5; ABS_OK=$6
RATE_N=$7; RATE_OK=$8; FIRST_RSS=$9; LAST_RSS=${10}; MAX_RSS=${11}

pct() { awk -v a="$1" -v b="$2" 'BEGIN{ if (b==0) print "0.0"; else printf "%.1f", 100*a/b }'; }
ABS_PCT="$(pct "$ABS_OK" "$ABS_N")"
RATE_PCT="$(pct "$RATE_OK" "$RATE_N")"
if [ "$FIRST_RSS" != "-" ] && [ "$LAST_RSS" != "-" ] && [ "$FIRST_RSS" -gt 0 ] 2>/dev/null; then
  RSS_GROWTH_PCT="$(awk -v f="$FIRST_RSS" -v l="$LAST_RSS" 'BEGIN{printf "%.1f", 100*(l-f)/f}')"
else
  RSS_GROWTH_PCT="n/a"
fi

C1_PASS=$([ "$UNEXPECTED_EXIT" = "0" ] && echo yes || echo no)
C2_PASS=$([ "$CFG_SEEN" -gt 0 ] && [ "$CFG_BAD" -eq 0 ] && echo yes || echo no)
C3_PASS=$(awk -v p="$ABS_PCT" -v n="$ABS_N" 'BEGIN{print (n>0 && p>=99.0) ? "yes" : "no"}')
C4_PASS=$(awk -v p="$RATE_PCT" -v n="$RATE_N" 'BEGIN{print (n>0 && p>=95.0) ? "yes" : "no"}')
C5_PASS=$(awk -v g="$RSS_GROWTH_PCT" 'BEGIN{print (g!="n/a" && g+0 < 20.0) ? "yes" : "no"}')

if [ "$INTERRUPTED" = "1" ]; then
  VERDICT="INCOMPLETE"
elif [ "$C1_PASS" = "yes" ] && [ "$C2_PASS" = "yes" ] && [ "$C3_PASS" = "yes" ] \
  && [ "$C4_PASS" = "yes" ] && [ "$C5_PASS" = "yes" ]; then
  VERDICT="PASS"
else
  VERDICT="FAIL"
fi

# ---------- RESULT.md -------------------------------------------------------------
cat > "$RESULT" <<EOF
# chirp macOS soak — SB7.1 go/no-go spike

## VERDICT: $VERDICT

| | |
|---|---|
| started | $STARTED_ISO |
| ended | $ENDED_ISO |
| duration | ${ACTUAL_H} h of ${DURATION_H} h planned |
| band | $BAND |
| source | $SRC |
| daemon python | $SPIKE_PY (radioconda — gnuradio lives there, not in the UI venv) |
| audio out | $AUDIO_OUT_REDACTED |
| bitrate | ${BITRATE_KBPS} kbps -> rate floor ${RATE_MIN_BPS} B/s |
| samples | $N_SAMPLES total, $N_SCRAPES successful scrapes, $((N_SAMPLES - N_SCRAPES)) scrape failures |
| daemon exit | $([ "$UNEXPECTED_EXIT" = "1" ] && echo "UNEXPECTED, code ${DAEMON_EXIT_CODE} — see daemon_exit_tail.log" || echo "clean (stopped by harness)") |

## Criteria

| # | criterion | required | observed | pass |
|---|-----------|----------|----------|------|
| 1 | no unexpected daemon exit | never | $([ "$UNEXPECTED_EXIT" = "1" ] && echo "exited code ${DAEMON_EXIT_CODE}" || echo "never exited") | $C1_PASS |
| 2 | config_load_status == 1 | throughout | $CFG_BAD bad / $CFG_SEEN seen | $C2_PASS |
| 3 | audio_branch_silent == 0 | >= 99% | ${ABS_PCT}% ($ABS_OK/$ABS_N) | $C3_PASS |
| 4 | byte rate >= ${RATE_MIN_BPS} B/s | >= 95% | ${RATE_PCT}% ($RATE_OK/$RATE_N) | $C4_PASS |
| 5 | RSS growth < 20% | < 20% | ${RSS_GROWTH_PCT}% (first ${FIRST_RSS} KB -> last ${LAST_RSS} KB, max ${MAX_RSS} KB) | $C5_PASS |

$([ "$VERDICT" = "INCOMPLETE" ] && echo "Run was interrupted before the planned duration — criteria above reflect the partial window only. Re-run the full ${DURATION_H} h soak for a go/no-go verdict.")

## Artifacts

- samples.csv — one row per ${INTERVAL_S} s sample
- daemon.log — full daemon stdout/stderr
- config_snapshot.json — the band config at launch
- env.txt — CHIRP_/SPIKE_ env at launch (secrets redacted)
$([ -f "$OUT/daemon_exit_tail.log" ] && echo "- daemon_exit_tail.log — exit code + last 100 log lines")
EOF

# ---------- verdict banner ---------------------------------------------------------
case "$VERDICT" in
  PASS)       COLOR="1;32"; RC=0 ;;
  INCOMPLETE) COLOR="1;33"; RC=2 ;;
  *)          COLOR="1;31"; RC=1 ;;
esac
printf "\n\033[%sm############################################\033[0m\n" "$COLOR"
printf "\033[%sm##   SB7.1 CHIRP SOAK VERDICT: %-10s ##\033[0m\n" "$COLOR" "$VERDICT"
printf "\033[%sm############################################\033[0m\n" "$COLOR"
echo "  full report: $RESULT"
[ -f "$OUT/daemon_exit_tail.log" ] && echo "  failure detail: $OUT/daemon_exit_tail.log"
exit "$RC"
