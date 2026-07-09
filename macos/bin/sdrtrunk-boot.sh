#!/bin/bash
# sdrtrunk-boot.sh — launchd-friendly SDRTrunk launcher (digital P25, RSP-A).
#
# WHY A WRAPPER: a backgrounded child (nohup &) is reaped by launchd when the agent
# process exits, so mac-start-sdrtrunk.sh's interactive launch does NOT survive under
# launchd. Here we EXEC the SDRTrunk launcher (which itself `exec`s the JVM), so the JVM
# BECOMES the launchd job process and persists; KeepAlive in the plist restarts it on crash.
#
# Coexistence: this only waits for the shared SDRplay apiService (a LaunchDaemon that is
# already up at boot) — it does NOT touch SDRangel. No sudo.
set -u
cd "$HOME/SDRTrunk" || exit 1

# Wait (best-effort) for the SDRplay apiService so SDRTrunk finds the RSPduo at boot.
for _ in $(seq 1 30); do
  pgrep -x sdrplay_apiService >/dev/null 2>&1 && break
  sleep 1
done

# Replace this process with the JVM so launchd manages SDRTrunk directly.
exec "$HOME/SDRTrunk/bin/sdr-trunk"
