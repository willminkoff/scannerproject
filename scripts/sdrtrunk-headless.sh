#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${SDRTRUNK_PROFILE:-vanderbilt_p25}"
PROFILE_DIR="${SDRTRUNK_PROFILE_DIR:-$REPO_ROOT/profiles/sdrtrunk}"
SDRTRUNK_HOME="${SDRTRUNK_HOME:-/var/lib/sdrtrunk}"
SDRTRUNK_BIN="${SDRTRUNK_BIN:-}"
SDRTRUNK_JAR="${SDRTRUNK_JAR:-}"

/usr/bin/python3 "$REPO_ROOT/scripts/sdrtrunk_profile_sync.py" \
  --profile "$PROFILE" \
  --profiles-dir "$PROFILE_DIR" \
  --home "$SDRTRUNK_HOME"

JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:-} -Djava.awt.headless=true -Duser.home=${SDRTRUNK_HOME} -Djava.util.prefs.userRoot=${SDRTRUNK_HOME}/prefs"
export JAVA_TOOL_OPTIONS

if [[ -n "$SDRTRUNK_BIN" && -x "$SDRTRUNK_BIN" ]]; then
  exec "$SDRTRUNK_BIN"
fi

if [[ -n "$SDRTRUNK_JAR" && -f "$SDRTRUNK_JAR" ]]; then
  exec java -jar "$SDRTRUNK_JAR"
fi

if [[ -x "/opt/sdrtrunk/sdrtrunk" ]]; then
  exec /opt/sdrtrunk/sdrtrunk
fi

if [[ -x "/opt/sdrtrunk/sdrtrunk.sh" ]]; then
  exec /opt/sdrtrunk/sdrtrunk.sh
fi

if command -v sdrtrunk >/dev/null 2>&1; then
  exec sdrtrunk
fi

echo "ERROR: SDRTrunk launcher not found. Set SDRTRUNK_BIN or SDRTRUNK_JAR." >&2
exit 1
