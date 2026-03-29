#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BUNDLE_ROOT="${1:-$HOME/Desktop/mcp-mac-studio-migration-$TIMESTAMP}"
ARCHIVE_PATH="${BUNDLE_ROOT}.tar.gz"

TEAM_COORDINATION_ROOT="$HOME/team-coordination"
TEAM_COORDINATION_STATE_DIR="$HOME/.local/share/team-coordination"
SCANNER_SUPPORT_DIR="$HOME/Library/Application Support/scannerproject"
DILLARD_SUPPORT_DIR="$HOME/Library/Application Support/dillard-it-transition"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
CODEX_CONFIG="$HOME/.codex/config.toml"

for path in \
  "$TEAM_COORDINATION_ROOT" \
  "$TEAM_COORDINATION_STATE_DIR" \
  "$SCANNER_SUPPORT_DIR" \
  "$DILLARD_SUPPORT_DIR" \
  "$LAUNCH_AGENTS_DIR/com.willminkoff.scannerproject.mcp-slack-notify.plist" \
  "$LAUNCH_AGENTS_DIR/com.willminkoff.dillard-it-transition.mcp-slack-notify.plist" \
  "$TEAM_COORDINATION_ROOT/com.local.team-coordination.plist"
do
  if [[ ! -e "$path" ]]; then
    echo "Missing required migration source: $path" >&2
    exit 1
  fi
done

rm -rf "$BUNDLE_ROOT"
mkdir -p \
  "$BUNDLE_ROOT/kit" \
  "$BUNDLE_ROOT/private/app-support" \
  "$BUNDLE_ROOT/private/launchagents" \
  "$BUNDLE_ROOT/private/codex-config"

cp "$ROOT_DIR/admin/mac-studio-migration/README.md" "$BUNDLE_ROOT/README.md"
cp "$ROOT_DIR/admin/mac-studio-migration/install_from_bundle.sh" "$BUNDLE_ROOT/kit/install_from_bundle.sh"
cp "$ROOT_DIR/admin/mac-studio-migration/client-remote-config.toml.example" "$BUNDLE_ROOT/kit/client-remote-config.toml.example"
chmod +x "$BUNDLE_ROOT/kit/install_from_bundle.sh"

rsync -a --exclude 'node_modules' "$TEAM_COORDINATION_ROOT/" "$BUNDLE_ROOT/private/team-coordination/"
rsync -a "$TEAM_COORDINATION_STATE_DIR/" "$BUNDLE_ROOT/private/team-coordination-state/"
rsync -a "$SCANNER_SUPPORT_DIR/" "$BUNDLE_ROOT/private/app-support/scannerproject/"
rsync -a "$DILLARD_SUPPORT_DIR/" "$BUNDLE_ROOT/private/app-support/dillard-it-transition/"

cp "$LAUNCH_AGENTS_DIR/com.willminkoff.scannerproject.mcp-slack-notify.plist" "$BUNDLE_ROOT/private/launchagents/"
cp "$LAUNCH_AGENTS_DIR/com.willminkoff.dillard-it-transition.mcp-slack-notify.plist" "$BUNDLE_ROOT/private/launchagents/"
cp "$TEAM_COORDINATION_ROOT/com.local.team-coordination.plist" "$BUNDLE_ROOT/private/launchagents/"
cp "$CODEX_CONFIG" "$BUNDLE_ROOT/private/codex-config/config.toml"

cat > "$BUNDLE_ROOT/private/MANIFEST.txt" <<EOF
Generated: $(date)
Source machine: $(hostname)

Included:
- team-coordination server from $TEAM_COORDINATION_ROOT
- team-coordination state from $TEAM_COORDINATION_STATE_DIR
- scannerproject watcher support from $SCANNER_SUPPORT_DIR
- dillard-it-transition watcher support from $DILLARD_SUPPORT_DIR
- launch agents from $LAUNCH_AGENTS_DIR
- Codex config from $CODEX_CONFIG

Sensitive contents:
- Slack bot tokens and webhooks
- MCP SQLite state
- local Codex config
EOF

tar -czf "$ARCHIVE_PATH" -C "$(dirname "$BUNDLE_ROOT")" "$(basename "$BUNDLE_ROOT")"

echo "Created Mac Studio MCP migration bundle:"
echo "  directory: $BUNDLE_ROOT"
echo "  archive:   $ARCHIVE_PATH"
