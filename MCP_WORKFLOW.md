# MCP Worker Loop

This repo uses the local `team_coordination` MCP server as the system of record.

## Review-ready signal

Claude should tell Codex a task is ready for review by updating MCP, not by chat alone:

1. record the commit and any validation artifacts in MCP
2. append a decision-log summary of the fix or change
3. set the task status to `review`

`review` is the handoff signal that Codex watches for.

## Stall rule

No task should sit in `claimed`, `in_progress`, or `blocked` without an explicit next checkpoint recorded in MCP.

If Claude cannot produce a commit, diff artifact, or blocker update before the checkpoint, the task must be treated as stalled and surfaced immediately in Slack.

Slack should show a visible halt signal such as `PROGRESS IS HALTED` when:

- the task is blocked, or
- the task has not advanced for the configured stale-progress window

The point is to prevent silent waiting. A stalled task must either:

1. produce a commit or artifact,
2. record a precise blocker,
3. or be escalated back to Codex / the user with evidence.

## Helper script

Use `scripts/mcp_queue.py` from the repo root. It reads the `team_coordination` server URL from `~/.codex/config.toml` and talks to the MCP server directly over the standard streamable HTTP transport.

Examples:

```bash
python3 scripts/mcp_queue.py pickup --owner claude
python3 scripts/mcp_queue.py review-queue
python3 scripts/mcp_queue.py task 8305deae-2283-4868-b444-e85c3df521e7
```

## Claude flow

Claim and reserve a branch:

```bash
python3 scripts/mcp_queue.py claim 3274d57f-fe8b-4d21-81eb-053866187f92 \
  --owner claude \
  --branch claude/analog-validation \
  --base-branch main
```

Hand off to Codex for review:

```bash
python3 scripts/mcp_queue.py handoff-review 8305deae-2283-4868-b444-e85c3df521e7 \
  --actor claude \
  --commit b124b86 \
  --summary "OP25 hit pipeline fix committed. Tests passed and task is ready for Codex review." \
  --artifact test_result:/tmp/op25-hit-pipeline-tests.txt \
  --workspace-note "OP25 hit pipeline fix ready for Codex review."
```

## Codex flow

Check for review work:

```bash
python3 scripts/mcp_queue.py review-queue
```

Inspect the task and recent MCP activity:

```bash
python3 scripts/mcp_queue.py task 8305deae-2283-4868-b444-e85c3df521e7
```

## Reasoning defaults

- Claude is the default implementation agent and should run at the highest available reasoning or thinking effort for bounded delivery work.
- Codex should use normal reasoning for routine MCP coordination, queue summaries, and straightforward task handling.
- Codex should step up to higher reasoning for review, blockers, architecture, merge decisions, deploy decisions, or any risky cross-task change.

## Slack notifier

If you want Slack pings when a task needs human attention, use the repo-local notifier with a Slack incoming webhook:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
python3 scripts/mcp_slack_notify.py --watch 60
```

Default behavior:

- polls the current repo workspace in MCP
- watches tasks that enter `review` or `blocked`
- posts a Slack message once per task status transition
- stores dedupe state in the repo git dir so repeated polls do not spam
- can optionally bridge Slack thread replies back into MCP when a Slack Web API token is configured

Useful variants:

```bash
python3 scripts/mcp_slack_notify.py --dry-run
python3 scripts/mcp_slack_notify.py --statuses review
python3 scripts/mcp_slack_notify.py --statuses blocked --owner codex
```

### Two-way Slack replies

Incoming webhooks are enough for one-way alerts. If you also want to reply from Slack and have MCP update automatically, add Slack Web API credentials and a channel ID:

```bash
export SLACK_API_TOKEN="xoxp-or-xoxb-token"
export SLACK_CHANNEL_ID="C0123456789"
python3 scripts/mcp_slack_notify.py --watch 60
```

Recommended token split for channel threads:

- `SLACK_POST_TOKEN`: token with `chat:write` used to post alerts and MCP acknowledgements
- `SLACK_REPLY_TOKEN`: token used to read thread replies
- `SLACK_CHANNEL_ID`: channel ID where MCP alerts are posted

If you use a channel thread and want to read replies from that thread, prefer a user token for `SLACK_REPLY_TOKEN` with the history scopes that match the channel type. The notifier falls back to `SLACK_API_TOKEN` for both posting and reply reads when the split variables are not set.

Supported thread commands:

- `decision: <summary>`
- `block: <reason>`
- `unblock: <summary>`
- `assign: claude`
- `reassign: codex | <reason>`
- `push`
- `deploy`
- `push and deploy`
- `status: done | <note>`

The bridge records the change in MCP and posts an acknowledgement back into the same Slack thread.

If you point the bridge at an app DM (`SLACK_CHANNEL_ID=D...`), it also supports direct DM commands:

- `task 8305deae decision: approve`
- `task 4d7a3e1a block: waiting on hardware`
- `task 8305deae push`
- `task 8305deae deploy`
- `task 8305deae status: done | ship it`

In DM mode, replying in-thread to a task message still works and is preferred. Direct `task <id> ...` commands can target any MCP task in the workspace by short task id, even if that task has not been notified in Slack yet.

The DM bridge now also supports guarded manager-style language on top of MCP:

- `what needs review?`
- `what is claude working on?`
- `have claude take the next task`
- `take the next task`
- in a task thread: `approve this`, `block because ...`, `have claude take this`

Default operating model:

- Claude is the default implementation owner for new work
- Codex stays in review, architecture, merge, deploy, and conflict-resolution lanes
- ambiguous requests answer with context instead of mutating MCP

### Push and deploy actions

Slack `push`, `deploy`, and `push and deploy` actions are guarded by explicit workspace env config:

```bash
export MCP_PUSH_COMMAND="git push origin HEAD"
export MCP_DEPLOY_COMMAND="/absolute/path/to/deploy-command"
```

Behavior:

- `push`: runs `MCP_PUSH_COMMAND`, records a log artifact in MCP, and appends a decision log entry
- `deploy`: runs `MCP_DEPLOY_COMMAND`, records a log artifact, appends a decision log entry, and marks the task `done` on success
- `push and deploy`: runs both in sequence and marks the task `done` on success

If a workspace does not define the corresponding env var, the Slack bridge refuses the action instead of guessing.

### Fresh Slack app path

If the existing Slack app has a broken reinstall or OAuth redirect configuration, create a fresh DM-only app from [admin/slack_mcp_dm_app_manifest.yml](/Users/willminkoff/Documents/scannerproject/admin/slack_mcp_dm_app_manifest.yml) instead of debugging the old app in place.

Suggested setup:

1. Create a new Slack app from manifest.
2. Paste the manifest from `admin/slack_mcp_dm_app_manifest.yml`.
3. Install the app to the workspace.
4. Open a DM with the new app.
5. Use the new bot token as `SLACK_POST_TOKEN`.
6. Use the Slack user ID or returned DM channel as `SLACK_CHANNEL_ID` for DM mode.

### Background watcher on macOS

This repo includes `scripts/run_mcp_slack_notify.sh` for launchd-based background runs.

Installed paths used by the watcher:

- env file: `~/Library/Application Support/scannerproject/mcp_slack_notify.env`
- launch agent: `~/Library/LaunchAgents/com.willminkoff.scannerproject.mcp-slack-notify.plist`
- stdout log: `~/Library/Logs/scannerproject/mcp_slack_notify.log`
- stderr log: `~/Library/Logs/scannerproject/mcp_slack_notify.err`

Useful launchd commands:

```bash
launchctl print gui/$(id -u)/com.willminkoff.scannerproject.mcp-slack-notify
launchctl kickstart -k gui/$(id -u)/com.willminkoff.scannerproject.mcp-slack-notify
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.willminkoff.scannerproject.mcp-slack-notify.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.willminkoff.scannerproject.mcp-slack-notify.plist
```
