#!/bin/bash
# post-install-checks.sh — verify the macOS scanner stack is wired correctly.
# Read-only; safe to run anytime. Exit non-zero if any critical check fails.
set -u
PASS=0; FAIL=0
ok(){ printf "  \033[1;32m✓\033[0m %s\n" "$*"; PASS=$((PASS+1)); }
no(){ printf "  \033[1;31m✗\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
inf(){ printf "  \033[1;33m·\033[0m %s\n" "$*"; }

echo "== SDRplay API service =="
pgrep -x sdrplay_apiService >/dev/null && ok "sdrplay_apiService running" || no "sdrplay_apiService NOT running"
[ -d /Library/SDRplayAPI ] && ok "SDRplay API installed ($(ls /Library/SDRplayAPI 2>/dev/null | tr '\n' ' '))" || no "SDRplay API dir missing"

echo "== RSPduo detection =="
# system_profiler enumerates USB; RSPduo shows as an SDRplay device
if system_profiler SPUSBDataType 2>/dev/null | grep -qi 'sdrplay\|rspduo\|1df7'; then
  ok "RSPduo visible on USB"
else
  no "no RSPduo on USB (plug in BEFORE launching apps)"
fi

echo "== Java =="
if command -v java >/dev/null && java -version 2>&1 | grep -qE '"(21|22|23|24|25|26)\.'; then
  ok "Java 21+ ($(java -version 2>&1 | head -1))"
else
  no "Java 21+ not found (brew install openjdk@21)"
fi

echo "== SDRTrunk + JMBE =="
[ -x "${HOME}/SDRTrunk/bin/sdr-trunk" ] && ok "SDRTrunk launcher present" || no "SDRTrunk launcher missing (run mac-install-sdrtrunk.sh)"
if ls "${HOME}/SDRTrunk/jmbe/"jmbe-*.jar >/dev/null 2>&1; then ok "JMBE jar staged"; else no "JMBE jar missing (run jmbe-build.sh + set it in SDRTrunk prefs)"; fi

echo "== SDRangel + REST =="
[ -d /Applications/SDRangel.app ] && ok "SDRangel.app present" || no "SDRangel.app missing (run mac-install-sdrangel.sh)"
if curl -fsS -m 4 http://127.0.0.1:8091/sdrangel 2>/dev/null | grep -qi 'version\|appname\|qtVersion'; then
  ok "SDRangel REST reachable on :8091"
else
  no "SDRangel REST :8091 not reachable (enable Web/REST in SDRangel prefs + launch it)"
fi

echo "== Tailscale + remote =="
command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1 && ok "Tailscale up" || inf "Tailscale not up (needed for remote + Claude access)"
sudo -n true 2>/dev/null && inf "passwordless sudo available" || inf "sudo needs a password (fine if Claude doesn't need privileged ops)"

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ] || exit 1
