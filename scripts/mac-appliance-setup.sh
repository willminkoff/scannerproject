#!/bin/bash
# mac-appliance-setup.sh — check/apply the always-on appliance conventions
# for the single-box M1 Mac mini deployment (SB7 §4.3, docs/sb7-northstar-program.md).
#
# For each convention: CHECK it, report status, APPLY it only where that is
# safe non-interactively (pmset, via sudo), and print the exact manual steps
# otherwise (FileVault, auto-login need the GUI + an admin).
#
# REQUIRED items (exit non-zero when any is unmet):
#   - pmset: sleep 0, displaysleep 0, disksleep 0, powernap 0, autorestart 1, womp 1
#   - FileVault OFF        (a headless reboot otherwise stalls at pre-boot unlock)
#   - Auto-login enabled   (LaunchAgents + USB SDR access need a user session)
# Reported only (WARN, does not affect exit code):
#   - Tailscale installed + logged in
#   - HDMI dummy plug present (GUI apps over Screen Sharing render badly without one)
#   - services-as-LaunchAgents reminder (+ check for stray scannerproject LaunchDaemons)
#
# Usage: mac-appliance-setup.sh [--check]
#   --check   report only; never applies anything (no sudo)
#
# Idempotent — safe to re-run any time; a fully compliant box prints all-OK
# and exits 0. Run as your normal user; pmset fixes sudo where needed.

set -euo pipefail

CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check)   CHECK_ONLY=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,22p'; exit 0 ;;
    *) echo "unknown arg: $arg (valid: --check)"; exit 2 ;;
  esac
done

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; }

# Summary rows (parallel arrays — /bin/bash is 3.2, no associative arrays).
SUM_ITEM=(); SUM_REQ=(); SUM_STATUS=(); SUM_NOTE=()
REQUIRED_FAIL=0
record() { # $1 item  $2 required yes|no  $3 OK|FAIL|WARN  $4 note
  SUM_ITEM+=("$1"); SUM_REQ+=("$2"); SUM_STATUS+=("$3"); SUM_NOTE+=("$4")
  if [ "$2" = "yes" ] && [ "$3" = "FAIL" ]; then REQUIRED_FAIL=1; fi
}

step "Host"
ok "$(sw_vers -productName) $(sw_vers -productVersion) · $(uname -m) · user $(id -un)"
[ "$CHECK_ONLY" = "1" ] && warn "--check: report only, nothing will be applied"

# ---------- 1. pmset power policy (REQUIRED; auto-applied via sudo) -----------
step "pmset — never sleep, restart on power loss, wake on LAN (REQUIRED)"
PMSET_KEYS=(sleep displaysleep disksleep powernap autorestart womp)
PMSET_WANT=(0     0            0         0        1           1)
PM_OUT="$(pmset -g custom 2>/dev/null || true)"
PMSET_BAD=""
i=0
while [ "$i" -lt "${#PMSET_KEYS[@]}" ]; do
  key="${PMSET_KEYS[$i]}"; want="${PMSET_WANT[$i]}"
  cur="$(printf '%s\n' "$PM_OUT" | awk -v k="$key" '$1==k{v=$2} END{print v}')"
  if [ "$cur" = "$want" ]; then
    ok "pmset $key = $cur"
  elif [ "$CHECK_ONLY" = "1" ]; then
    warn "pmset $key = ${cur:-unset} (want $want) — fix: sudo pmset -a $key $want"
    PMSET_BAD="$PMSET_BAD $key"
  else
    warn "pmset $key = ${cur:-unset} (want $want) — applying (sudo)"
    if sudo pmset -a "$key" "$want" 2>/dev/null; then
      ok "applied: pmset -a $key $want"
    else
      err "apply failed — run manually: sudo pmset -a $key $want"
      PMSET_BAD="$PMSET_BAD $key"
    fi
  fi
  i=$((i+1))
done
if [ -z "$PMSET_BAD" ]; then
  record "pmset never-sleep" yes OK "all 6 keys set"
else
  record "pmset never-sleep" yes FAIL "unmet:$PMSET_BAD"
fi

# ---------- 2. FileVault (REQUIRED OFF; report-only) ---------------------------
step "FileVault — must be OFF for headless reboot (REQUIRED, report-only)"
FV="$(fdesetup status 2>/dev/null | head -1 || true)"
if printf '%s' "$FV" | grep -q "Off"; then
  ok "$FV"
  record "FileVault OFF" yes OK "$FV"
else
  err "${FV:-fdesetup status unavailable}"
  warn "With FileVault ON, a reboot stalls at the pre-boot unlock screen with"
  warn "nobody at the keyboard — the appliance never comes back up."
  warn "Manual: System Settings > Privacy & Security > FileVault > Turn Off"
  warn "(admin password; decryption runs in the background, can take hours)."
  record "FileVault OFF" yes FAIL "${FV:-unknown}"
fi

# ---------- 3. Auto-login (REQUIRED; report-only) ------------------------------
step "Auto-login — user session at boot (REQUIRED, report-only)"
AUTOUSER="$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null || true)"
if [ -n "$AUTOUSER" ]; then
  ok "auto-login enabled for user '$AUTOUSER'"
  record "Auto-login" yes OK "user $AUTOUSER"
else
  err "auto-login NOT enabled"
  warn "Services run as LaunchAgents in the logged-in user session (USB SDR"
  warn "access needs one) — without auto-login, nothing starts after a reboot."
  warn "Manual: System Settings > Users & Groups > 'Automatically log in as'"
  warn "> pick the service user. NOTE: the option is greyed out until"
  warn "FileVault is OFF (see above)."
  record "Auto-login" yes FAIL "disabled"
fi

# ---------- 4. Tailscale (recommended; report-only) ----------------------------
step "Tailscale — remote access (recommended, report-only)"
TS_BIN=""
if command -v tailscale >/dev/null 2>&1; then
  TS_BIN="tailscale"
elif [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]; then
  TS_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
fi
if [ -z "$TS_BIN" ]; then
  warn "Tailscale not found (no CLI, no /Applications/Tailscale.app)"
  warn "Install: https://tailscale.com/download/macos (the standalone pkg's"
  warn "CLI survives headless better than the App Store build), then: tailscale up"
  record "Tailscale" no WARN "not installed"
elif "$TS_BIN" status >/dev/null 2>&1; then
  ok "Tailscale installed + logged in ($TS_BIN)"
  record "Tailscale" no OK "logged in"
else
  warn "Tailscale installed but NOT connected/logged in — run: $TS_BIN up"
  record "Tailscale" no WARN "installed, logged out"
fi

# ---------- 5. Reminders (report-only) -----------------------------------------
step "HDMI dummy plug (reminder)"
NDISP="$(system_profiler SPDisplaysDataType 2>/dev/null | grep -c "Resolution:" || true)"
if [ "${NDISP:-0}" -ge 1 ]; then
  ok "$NDISP display(s) attached (real monitor or dummy plug)"
  record "HDMI dummy plug" no OK "$NDISP display(s)"
else
  warn "NO display detected — plug in the HDMI dummy plug: without a display,"
  warn "GUI apps (SDRTrunk, SDRangel) over Screen Sharing get a tiny software-"
  warn "rendered frame buffer."
  record "HDMI dummy plug" no WARN "no display detected"
fi

step "Services as LaunchAgents (reminder)"
warn "Install scannerproject services as LaunchAgents in the auto-logged-in"
warn "USER session (~/Library/LaunchAgents, loaded gui/$(id -u)) — NOT as"
warn "LaunchDaemons: USB SDR access requires a user session (SB7 §4.3, which"
warn "amends docs/mac-mini-port.md §3). com.sdrplay.service is the one"
warn "legitimate LaunchDaemon (vendor-installed)."
STRAY="$(ls /Library/LaunchDaemons/com.scannerproject.* 2>/dev/null || true)"
if [ -n "$STRAY" ]; then
  err "stray scannerproject LaunchDaemon(s) found — move these to LaunchAgents:"
  printf '%s\n' "$STRAY" | sed 's/^/      /'
  record "LaunchAgents-not-Daemons" no WARN "stray LaunchDaemons present"
else
  ok "no scannerproject LaunchDaemons in /Library/LaunchDaemons"
  record "LaunchAgents-not-Daemons" no OK "clean"
fi

# ---------- summary -------------------------------------------------------------
step "Summary"
printf "  %-26s %-9s %-7s %s\n" "ITEM" "REQUIRED" "STATUS" "NOTE"
printf "  %-26s %-9s %-7s %s\n" "----" "--------" "------" "----"
i=0
while [ "$i" -lt "${#SUM_ITEM[@]}" ]; do
  printf "  %-26s %-9s %-7s %s\n" \
    "${SUM_ITEM[$i]}" "${SUM_REQ[$i]}" "${SUM_STATUS[$i]}" "${SUM_NOTE[$i]}"
  i=$((i+1))
done
echo
if [ "$REQUIRED_FAIL" = "1" ]; then
  err "REQUIRED appliance conventions unmet — the box will NOT survive an"
  err "unattended reboot. Fix the FAIL rows above and re-run."
  exit 1
fi
ok "all REQUIRED appliance conventions met"
exit 0
