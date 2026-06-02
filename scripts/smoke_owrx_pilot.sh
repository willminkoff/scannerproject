#!/usr/bin/env bash
#
# smoke_owrx_pilot.sh — regression smoke test for the OpenWebRX+ Live IQ pilot.
#
# Run on Micro after a deploy, or any time, to confirm the OWRX+ cutover did not
# regress the surrounding stack.  Read-only and non-disruptive: it only probes
# HTTP endpoints, systemd state, and /sys — it never bounces a service or grabs a
# dongle, so it is safe to run while people are listening.
#
# Exits non-zero on the first hard failure (see `set -e` + `check`/`fail`).
# Docker health is a soft check: if this user can't reach the Docker socket it is
# SKIPPED (HTTP reachability already proves OWRX is serving), so the test stays
# runnable without sudo.  See scripts/SMOKE_TESTS.md.
#
set -euo pipefail

# ---- config (overridable via env) -----------------------------------------
OWRX_HOST="${OWRX_HOST:-localhost}"
OWRX_PORT="${OWRX_PORT:-8073}"
ICECAST_HOST="${ICECAST_HOST:-localhost}"
ICECAST_PORT="${ICECAST_PORT:-8000}"
UI_HOST="${UI_HOST:-localhost}"
UI_PORT="${UI_PORT:-5050}"
OWRX_SERIAL="${OWRX_SERIAL:-83241970}"          # dongle OWRX is meant to drive
OWRX_CONTAINER="${OWRX_CONTAINER:-owrxp}"
MOUNT_DATA_TIMEOUT="${MOUNT_DATA_TIMEOUT:-5}"    # seconds to wait for mount bytes

OWRX_BASE="http://${OWRX_HOST}:${OWRX_PORT}"
ICECAST_BASE="http://${ICECAST_HOST}:${ICECAST_PORT}"
UI_BASE="http://${UI_HOST}:${UI_PORT}"

ICECAST_MOUNTS=(/ANALOG.mp3 /ANALOG_GROUND.mp3 /DIGITAL.mp3 /VFO.mp3)
SERVICES=(
    airband-ui
    rtl-airband-airband
    rtl-airband-ground
    scanner-digital-op25
    scanner-digital-op25-audio
    scanner-vfo
)

# ---- reporting -------------------------------------------------------------
PASS=0; FAIL=0; SKIP=0
green=$'\033[32m'; red=$'\033[31m'; yellow=$'\033[33m'; dim=$'\033[2m'; rst=$'\033[0m'

pass() { PASS=$((PASS+1)); printf '  %sPASS%s %s\n' "$green" "$rst" "$1"; }
skip() { SKIP=$((SKIP+1)); printf '  %sSKIP%s %s\n' "$yellow" "$rst" "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  %sFAIL%s %s\n' "$red" "$rst" "$1"; }

# Hard failure: print the summary and bail non-zero immediately.
die() {
    fail "$1"
    printf '\n%s======== SMOKE FAILED ========%s\n' "$red" "$rst"
    printf 'pass=%d fail=%d skip=%d  (stopped at first failure)\n' "$PASS" "$FAIL" "$SKIP"
    exit 1
}

section() { printf '\n%s== %s ==%s\n' "$dim" "$1" "$rst"; }

# ---- docker access (best-effort, no password prompt) -----------------------
DOCKER=""
if docker info >/dev/null 2>&1; then
    DOCKER="docker"
elif sudo -n docker info >/dev/null 2>&1; then
    DOCKER="sudo -n docker"
fi

printf '%sOpenWebRX+ pilot smoke test%s  —  OWRX=%s  icecast=%s  ui=%s\n' \
    "$dim" "$rst" "$OWRX_BASE" "$ICECAST_BASE" "$UI_BASE"

# ---- 1. OWRX serving -------------------------------------------------------
section "OWRX serving"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$OWRX_BASE/" || true)
[ "$code" = "200" ] && pass "GET $OWRX_BASE/ → 200" || die "GET $OWRX_BASE/ → $code (expected 200)"

# OWRX status API — also the data source for /api/owrx/diag.
scode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$OWRX_BASE/status.json" || true)
[ "$scode" = "200" ] && pass "GET /status.json → 200" || fail "GET /status.json → $scode (expected 200)"

# ---- 2. OWRX container health (soft if Docker socket unreachable) ----------
section "OWRX container health"
if [ -n "$DOCKER" ]; then
    health=$($DOCKER inspect "$OWRX_CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo "missing")
    case "$health" in
        healthy) pass "container '$OWRX_CONTAINER' health = healthy" ;;
        none)    skip "container '$OWRX_CONTAINER' has no healthcheck (running)" ;;
        missing) die  "container '$OWRX_CONTAINER' not found" ;;
        *)       die  "container '$OWRX_CONTAINER' health = $health (expected healthy)" ;;
    esac
else
    skip "Docker socket not reachable as $(id -un) — health check skipped (HTTP 200 already proves OWRX is serving; run with sudo for this check)"
fi

# ---- 3. icecast mounts publishing + receiving data -------------------------
section "icecast mounts"
for m in "${ICECAST_MOUNTS[@]}"; do
    # Stream up to MOUNT_DATA_TIMEOUT s; curl exits 28 on the cap, which is the
    # expected outcome for an endless mp3 stream — what matters is bytes flowed.
    read -r mcode msize < <(curl -s -o /dev/null \
        --max-time "$MOUNT_DATA_TIMEOUT" \
        -w '%{http_code} %{size_download}' "$ICECAST_BASE$m" || true)
    if [ "$mcode" = "200" ] && [ "${msize:-0}" -gt 0 ]; then
        pass "$m → 200, ${msize}B in ${MOUNT_DATA_TIMEOUT}s"
    else
        die "$m → http=$mcode bytes=${msize:-0} (expected 200 + data)"
    fi
done

# ---- 4. core services active (running) -------------------------------------
section "core services active"
for s in "${SERVICES[@]}"; do
    st=$(systemctl is-active "$s.service" 2>/dev/null || true)
    [ "$st" = "active" ] && pass "$s = active" || die "$s = ${st:-unknown} (expected active)"
done

# scanner-waterfall is intentionally masked by the pilot — assert it is NOT
# running (a running waterfall would mean it's contending for the dongle).
wst=$(systemctl is-active scanner-waterfall.service 2>/dev/null || true)
[ "$wst" != "active" ] && pass "scanner-waterfall = ${wst:-inactive} (intentionally masked)" \
    || die "scanner-waterfall = active (should be masked under the OWRX pilot)"

# ---- 5. heartbeat: 200 + no stale masked-waterfall false positive ----------
section "heartbeat rollup"
hb=$(curl -s --max-time 8 "$UI_BASE/api/heartbeat" || true)
hcode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$UI_BASE/api/heartbeat" || true)
[ "$hcode" = "200" ] && pass "GET /api/heartbeat → 200" || die "GET /api/heartbeat → $hcode (expected 200)"

if printf '%s' "$hb" | grep -qiE 'waterfall (service not running|dongle)'; then
    die "heartbeat still reports a waterfall-service/dongle row — masked waterfall is false-positiving"
else
    pass "no stale 'waterfall service' row in heartbeat"
fi
if printf '%s' "$hb" | grep -q 'OpenWebRX'; then
    pass "heartbeat carries the Live IQ (OpenWebRX+) row"
else
    fail "heartbeat missing the Live IQ (OpenWebRX+) row"
fi

# ---- 6. dongle assigned to OWRX, nothing else contending -------------------
section "dongle $OWRX_SERIAL → OWRX"
# /sys enumeration is non-disruptive (no device open). OWRX opens the dongle
# on-demand (per client), so "held right now" would false-fail when idle — the
# real invariant is: the serial is present AND OWRX has it configured AND no
# competing service (waterfall) is running.
if grep -qsx "$OWRX_SERIAL" /sys/bus/usb/devices/*/serial 2>/dev/null; then
    pass "RTL-SDR $OWRX_SERIAL present on USB (/sys)"
else
    die "RTL-SDR $OWRX_SERIAL not found in /sys — dongle missing/unplugged"
fi
if curl -s --max-time 8 "$OWRX_BASE/status.json" | grep -q '"sdrs"'; then
    pass "OWRX status.json reports a configured SDR"
else
    fail "OWRX status.json has no sdrs[] — check config/owrx/settings.json"
fi

# ---- summary ---------------------------------------------------------------
printf '\n%s======== SMOKE PASSED ========%s\n' "$green" "$rst"
printf 'pass=%d fail=%d skip=%d\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
