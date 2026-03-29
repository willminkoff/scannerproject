# Mac Studio MCP Migration

This kit moves the local control plane from the MacBook to an always-on Mac Studio.

What moves:
- `team_coordination` MCP server code from `~/team-coordination`
- MCP ledger state from `~/.local/share/team-coordination`
- Slack watcher env/state from `~/Library/Application Support/scannerproject` and `~/Library/Application Support/dillard-it-transition`
- launchd jobs for:
  - `com.local.team-coordination`
  - `com.willminkoff.scannerproject.mcp-slack-notify`
  - `com.willminkoff.dillard-it-transition.mcp-slack-notify`

What does not automatically move:
- Codex client config on the MacBook
- Claude client config on any other machine
- repo clones, if the Mac Studio does not already have them

Recommended target repo roots:
- `/Users/<you>/ops/scannerproject`
- `/Users/<you>/ops/dillard-it-transition`

You can keep the current `Documents/...` paths for a first cutover if those folders are already mirrored on the Mac Studio.

## Create The Bundle On The MacBook

From the `scannerproject` repo:

```bash
./scripts/package_mac_studio_migration.sh
```

That creates:
- an unpacked bundle on the Desktop
- a `.tar.gz` archive beside it

The bundle contains live secrets and MCP state. Treat it as sensitive.

## Install On The Mac Studio

1. Copy the bundle to the Mac Studio and unpack it.
2. Install Node 22 on the Mac Studio.
3. Make sure the target repo clones exist.
4. Run the installer from inside the bundle:

```bash
./kit/install_from_bundle.sh
```

If the default `node` is not Node 22, point the installer at the Node 22 binary explicitly:

```bash
NODE_BIN="$(brew --prefix node@22)/bin/node" ./kit/install_from_bundle.sh
```

Optional repo-root overrides:

```bash
SCANNER_REPO_ROOT="$HOME/ops/scannerproject" \
DILLARD_REPO_ROOT="$HOME/ops/dillard-it-transition" \
./kit/install_from_bundle.sh
```

The installer will:
- restore `team-coordination`
- restore the MCP SQLite state
- restore the Slack watcher env/state files
- generate fresh launch agents using the current Mac Studio home path
- load and kickstart the MCP server and both watchers

## Point Codex Clients At The Mac Studio

On any Codex client machine that should use the moved MCP ledger, update `~/.codex/config.toml`:

```toml
[mcp_servers.team_coordination]
url = "http://<mac-studio-hostname-or-ip>:8765/mcp"
```

There is also a template snippet in `client-remote-config.toml.example`.

If you leave the MacBook pointed at `http://127.0.0.1:8765/mcp`, it will continue talking to the old local MCP server instead of the Mac Studio.

## Smoke Tests

On the Mac Studio:

```bash
curl -fsS http://127.0.0.1:8765/health
launchctl print gui/$(id -u)/com.local.team-coordination | sed -n '1,80p'
launchctl print gui/$(id -u)/com.willminkoff.scannerproject.mcp-slack-notify | sed -n '1,80p'
launchctl print gui/$(id -u)/com.willminkoff.dillard-it-transition.mcp-slack-notify | sed -n '1,80p'
python3 /Users/<you>/Documents/scannerproject/scripts/mcp_queue.py review-queue
```

In Slack:
- DM `MCP`: `who is doing what?`
- DM `MCP Dillard`: `who is doing what?`

## Cutover

Once the Mac Studio is healthy:

1. Update MacBook Codex config to the Mac Studio MCP URL.
2. Verify MCP tasks look correct from the MacBook.
3. Verify Slack replies and notifications work from the Mac Studio-hosted watchers.
4. Disable the old MacBook launch agents:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.willminkoff.scannerproject.mcp-slack-notify.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.willminkoff.dillard-it-transition.mcp-slack-notify.plist
```

If you also stop running the MCP server locally on the MacBook, make sure all clients have already been re-pointed to the Mac Studio first.
