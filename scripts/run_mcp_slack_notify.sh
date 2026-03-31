#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${HOME}/Library/Application Support/scannerproject/mcp_slack_notify.env"
INTERVAL="${MCP_SLACK_NOTIFY_INTERVAL_SECONDS:-60}"
WORKSPACE_REPO_ROOT="${MCP_WORKSPACE_REPO_ROOT:-/Users/willminkoff/Documents/scannerproject}"
STATE_FILE="${HOME}/Library/Application Support/scannerproject/mcp_slack_notify_state.json"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing notifier env file: $ENV_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

: "${SLACK_WEBHOOK_URL:?SLACK_WEBHOOK_URL is required in $ENV_FILE}"

mkdir -p "$HOME/Library/Logs/scannerproject"

exec /usr/bin/env python3 "$REPO_ROOT/scripts/mcp_slack_notify.py" \
  --repo-root "$WORKSPACE_REPO_ROOT" \
  --state-file "$STATE_FILE" \
  --watch "$INTERVAL" \
  --statuses review blocked
