#!/bin/bash
# sdrtrunk launch wrapper: restart SDRplay API service before starting SDRTrunk
# so that a fresh enumeration finds RSPduo 1809063632 even when acarsdec/dumpvdl2
# already hold 180903EF32.
set -e
eval "$(/opt/homebrew/bin/brew shellenv)"
export JAVA_HOME=/opt/homebrew/opt/openjdk@21

# Kickstart the SDRplay API LaunchDaemon (systemwide). Requires passwordless
# sudo — Will has that set on Neptune.
sudo launchctl kickstart -k system/com.sdrplay.service || true
sleep 6

exec /Users/willminkoff/SDRTrunk-app/bin/sdr-trunk
