#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_ROOT="${BUNDLE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

TEAM_COORDINATION_SRC="$BUNDLE_ROOT/private/team-coordination"
TEAM_COORDINATION_STATE_SRC="$BUNDLE_ROOT/private/team-coordination-state"
SCANNER_SUPPORT_SRC="$BUNDLE_ROOT/private/app-support/scannerproject"
DILLARD_SUPPORT_SRC="$BUNDLE_ROOT/private/app-support/dillard-it-transition"

TEAM_COORDINATION_ROOT="${TEAM_COORDINATION_ROOT:-$HOME/team-coordination}"
TEAM_COORDINATION_STATE_DIR="${TEAM_COORDINATION_STATE_DIR:-$HOME/.local/share/team-coordination}"
SCANNER_REPO_ROOT="${SCANNER_REPO_ROOT:-$HOME/Documents/scannerproject}"
DILLARD_REPO_ROOT="${DILLARD_REPO_ROOT:-$HOME/Documents/dillard-it-transition}"

APP_SUPPORT_ROOT="$HOME/Library/Application Support"
LAUNCH_AGENTS_ROOT="$HOME/Library/LaunchAgents"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
USER_ID="$(id -u)"

if [[ -z "$NODE_BIN" ]]; then
  echo "node is required on the Mac Studio before running this installer." >&2
  exit 1
fi

NODE_VERSION="$("$NODE_BIN" -v)"
NODE_MAJOR="${NODE_VERSION#v}"
NODE_MAJOR="${NODE_MAJOR%%.*}"
if [[ "$NODE_MAJOR" != "22" ]]; then
  echo "team-coordination currently expects Node 22.x on the target host." >&2
  echo "Found: $NODE_VERSION at $NODE_BIN" >&2
  echo "Install node@22 and rerun with NODE_BIN set to that binary." >&2
  exit 1
fi

for path in \
  "$TEAM_COORDINATION_SRC" \
  "$TEAM_COORDINATION_STATE_SRC" \
  "$SCANNER_SUPPORT_SRC" \
  "$DILLARD_SUPPORT_SRC"
do
  if [[ ! -e "$path" ]]; then
    echo "Missing bundle path: $path" >&2
    exit 1
  fi
done

mkdir -p "$TEAM_COORDINATION_ROOT" "$TEAM_COORDINATION_STATE_DIR" "$APP_SUPPORT_ROOT" "$LAUNCH_AGENTS_ROOT"

rsync -a --delete "$TEAM_COORDINATION_SRC/" "$TEAM_COORDINATION_ROOT/"
rsync -a --delete "$TEAM_COORDINATION_STATE_SRC/" "$TEAM_COORDINATION_STATE_DIR/"
rsync -a --delete "$SCANNER_SUPPORT_SRC/" "$APP_SUPPORT_ROOT/scannerproject/"
rsync -a --delete "$DILLARD_SUPPORT_SRC/" "$APP_SUPPORT_ROOT/dillard-it-transition/"

python3 - <<PY
from pathlib import Path

updates = {
    Path("$APP_SUPPORT_ROOT/scannerproject/mcp_slack_notify.env"): {
        "MCP_WORKSPACE_REPO_ROOT": "$SCANNER_REPO_ROOT",
        "MCP_DEPLOY_COMMAND": "$SCANNER_REPO_ROOT/scripts/deploy-scannerbox.sh",
    },
    Path("$APP_SUPPORT_ROOT/dillard-it-transition/mcp_slack_notify.env"): {
        "MCP_WORKSPACE_REPO_ROOT": "$DILLARD_REPO_ROOT",
    },
}

for path, wanted in updates.items():
    lines = []
    existing = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                existing[key] = line
            else:
                lines.append(line)
    keys_done = set()
    merged = []
    for line in lines:
        merged.append(line)
    for key, value in wanted.items():
        merged.append(f"{key}='{value}'")
        keys_done.add(key)
    for key, line in existing.items():
        if key not in keys_done:
            merged.append(line)
    path.write_text("\\n".join(merged).rstrip() + "\\n", encoding="utf-8")
PY

(
  cd "$TEAM_COORDINATION_ROOT"
  rm -rf node_modules
  npm install
  npm run build
)

mkdir -p "$HOME/Library/Logs/scannerproject" "$HOME/Library/Logs/dillard-it-transition"

cat > "$LAUNCH_AGENTS_ROOT/com.local.team-coordination.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.local.team-coordination</string>
    <key>ProgramArguments</key>
    <array>
        <string>$NODE_BIN</string>
        <string>$TEAM_COORDINATION_ROOT/dist/index.js</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$TEAM_COORDINATION_STATE_DIR/server.log</string>
    <key>StandardErrorPath</key>
    <string>$TEAM_COORDINATION_STATE_DIR/server.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
EOF

cat > "$LAUNCH_AGENTS_ROOT/com.willminkoff.scannerproject.mcp-slack-notify.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.willminkoff.scannerproject.mcp-slack-notify</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$APP_SUPPORT_ROOT/scannerproject/scripts/run_mcp_slack_notify.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>WorkingDirectory</key>
  <string>$APP_SUPPORT_ROOT/scannerproject</string>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/scannerproject/mcp_slack_notify.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/scannerproject/mcp_slack_notify.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
</dict>
</plist>
EOF

cat > "$LAUNCH_AGENTS_ROOT/com.willminkoff.dillard-it-transition.mcp-slack-notify.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.willminkoff.dillard-it-transition.mcp-slack-notify</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$APP_SUPPORT_ROOT/dillard-it-transition/scripts/run_mcp_slack_notify.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>WorkingDirectory</key>
  <string>$APP_SUPPORT_ROOT/dillard-it-transition</string>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/dillard-it-transition/mcp_slack_notify.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/dillard-it-transition/mcp_slack_notify.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
</dict>
</plist>
EOF

for label in \
  "com.local.team-coordination" \
  "com.willminkoff.scannerproject.mcp-slack-notify" \
  "com.willminkoff.dillard-it-transition.mcp-slack-notify"
do
  launchctl bootout "gui/$USER_ID" "$LAUNCH_AGENTS_ROOT/$label.plist" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$USER_ID" "$LAUNCH_AGENTS_ROOT/$label.plist"
  launchctl kickstart -k "gui/$USER_ID/$label"
done

echo ""
echo "Installed MCP control plane on this Mac."
echo "Repo roots:"
echo "  scannerproject: $SCANNER_REPO_ROOT"
echo "  dillard-it-transition: $DILLARD_REPO_ROOT"
echo ""
echo "Smoke tests:"
echo "  curl -fsS http://127.0.0.1:8765/health"
echo "  launchctl print gui/$USER_ID/com.local.team-coordination | sed -n '1,80p'"
echo "  launchctl print gui/$USER_ID/com.willminkoff.scannerproject.mcp-slack-notify | sed -n '1,80p'"
echo "  launchctl print gui/$USER_ID/com.willminkoff.dillard-it-transition.mcp-slack-notify | sed -n '1,80p'"
