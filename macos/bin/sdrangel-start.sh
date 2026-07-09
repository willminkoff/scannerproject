#!/bin/bash
# sdrangel-start.sh — ensure SDRangel is up and the analog scanner config is restored.
# Safe to run repeatedly / on a timer: launches SDRangel only if down, and the
# restore step is idempotent (no-op when the config is already healthy).
set -u
B="http://127.0.0.1:8091/sdrangel"
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! curl -s -m3 -o /dev/null "$B"; then
  echo "[sdrangel-start] SDRangel REST down — launching app"
  # A prior SDRangel crash leaves the SDRplay apiService with a corrupt mutex;
  # sdrctl detects that (crash reports newer than the daemon) and kickstarts it
  # before the relaunch. Continue even if remediation is unavailable (no sudoers
  # rule) — that matches the old direct-launch behavior.
  "$HERE/sdrctl" kickstart-if-dirty || true
  open -a /Applications/SDRangel.app
  # wait up to 120s for the REST server
  for _ in $(seq 1 60); do curl -s -m3 -o /dev/null "$B" && break; sleep 2; done
fi

if ! curl -s -m3 -o /dev/null "$B"; then
  echo "[sdrangel-start] SDRangel still not reachable — giving up this cycle"; exit 1
fi

exec /usr/bin/python3 "$HERE/sdrangel-restore.py"
