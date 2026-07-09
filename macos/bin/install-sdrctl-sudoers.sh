#!/bin/bash
# One-time root setup enabling sdrctl's automatic apiService remediation.
#
# Installs a sudoers rule allowing willminkoff-scannerbox to run EXACTLY ONE
# command without a password:
#     /bin/launchctl kickstart -k system/com.sdrplay.service
# (restart the SDRplay apiService LaunchDaemon - nothing else).
#
# Why: when SDRangel segfaults while streaming an SDRplay device, the shared
# apiService is left with leaked libusb refs and a corrupt mutex; every later
# sdrplay_api_Init then segfaults ("crashes when starting new radios"). sdrctl
# detects this (SDRangel crash reports newer than the daemon process) and cures
# it by kickstarting the daemon - which needs root, hence this rule.
#
# Usage:  sudo macos/bin/install-sdrctl-sudoers.sh
set -euo pipefail

RULE_USER="willminkoff-scannerbox"
RULE_FILE="/etc/sudoers.d/sdrctl-sdrplay-kickstart"
RULE="${RULE_USER} ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/com.sdrplay.service"

if [[ $EUID -ne 0 ]]; then
    echo "must run as root:  sudo $0" >&2
    exit 1
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
printf '%s\n' "$RULE" > "$TMP"
chmod 440 "$TMP"

# visudo -c validates syntax before install; a bad sudoers.d file can lock out sudo entirely.
if ! visudo -cf "$TMP" >/dev/null; then
    echo "generated rule failed visudo validation; NOT installed" >&2
    exit 1
fi

install -m 440 -o root -g wheel "$TMP" "$RULE_FILE"
echo "installed $RULE_FILE:"
cat "$RULE_FILE"
echo
echo "verify as ${RULE_USER}:  sudo -n /bin/launchctl kickstart -k system/com.sdrplay.service"
