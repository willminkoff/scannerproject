#!/usr/bin/env python3
"""Post Slack notifications for MCP tasks and optionally bridge Slack replies back into MCP."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from scripts import mcp_queue
except ImportError:  # pragma: no cover - fallback for direct execution in unusual cwd cases
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import mcp_queue


DEFAULT_STATUSES = ("review", "blocked")
SLACK_API_BASE = "https://slack.com/api"
TASK_STATUSES = {"todo", "claimed", "in_progress", "blocked", "review", "done", "canceled"}
THREAD_COMMANDS = (
    "Reply in thread with `decision: ...`, `block: ...`, `unblock: ...`, "
    "`assign: claude`, `reassign: claude | reason`, or `status: done | note`."
)
DM_COMMANDS = (
    "In this DM, reply in-thread to a task message or send "
    "`task <id> decision: ...`, `task <id> block: ...`, "
    "`task <id> unblock: ...`, `task <id> assign: claude`, "
    "`task <id> reassign: codex | reason`, `task <id> push`, `task <id> deploy`, "
    "or `task <id> status: done | note`."
)
ACTION_COMMAND_ENV = {
    "push": "MCP_PUSH_COMMAND",
    "deploy": "MCP_DEPLOY_COMMAND",
}


class SlackReplyError(RuntimeError):
    """Raised when a Slack reply command is invalid or cannot be applied."""


def _git_dir(repo_root: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    git_dir = proc.stdout.strip()
    if not os.path.isabs(git_dir):
        git_dir = os.path.abspath(os.path.join(repo_root, git_dir))
    return git_dir


def _default_state_file(repo_root: str) -> str:
    return os.path.join(_git_dir(repo_root), "mcp_slack_notify_state.json")


def _normalize_state(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("notified"), dict):
        data["notified"] = {}
    if not isinstance(data.get("threads"), dict):
        data["threads"] = {}
    return data


def _load_state(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return _normalize_state({})
    except json.JSONDecodeError:
        return _normalize_state({})
    return _normalize_state(data)


def _save_state(path: str, state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="mcp_slack_notify_", suffix=".tmp", dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _parse_details(details: Any) -> dict[str, Any]:
    if isinstance(details, dict):
        return details
    if isinstance(details, str) and details.strip():
        try:
            parsed = json.loads(details)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"raw": details}
    return {}


def _collect_status_tasks(
    client: mcp_queue.TeamCoordinationClient,
    workspace_id: str,
    statuses: tuple[str, ...],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for status in statuses:
        for task in mcp_queue._list_tasks(client, workspace_id, status=status):
            task_id = str(task.get("id") or "")
            if task_id and task_id not in seen:
                tasks.append(task)
                seen.add(task_id)
    return tasks


def _latest_status_transition_id(task: dict[str, Any], events: list[dict[str, Any]]) -> str:
    current_status = task.get("status")
    task_id = str(task.get("id") or "")
    for event in events:
        if event.get("entity_type") != "task" or event.get("action") != "status_changed":
            continue
        if str(event.get("entity_id") or "") != task_id:
            continue
        details = _parse_details(event.get("details"))
        if details.get("to") == current_status:
            return str(event.get("id") or "")
    return f"{current_status}:{task.get('updated_at')}"


def _has_status_anchor(task: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    current_status = task.get("status")
    task_id = str(task.get("id") or "")
    for event in events:
        if event.get("entity_type") != "task" or event.get("action") != "status_changed":
            continue
        if str(event.get("entity_id") or "") != task_id:
            continue
        details = _parse_details(event.get("details"))
        if details.get("to") == current_status:
            return True
    return False


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    raw = str(event.get("timestamp") or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _focus_events_for_status(task: dict[str, Any], events: list[dict[str, Any]], window_seconds: int = 120) -> list[dict[str, Any]]:
    current_status = task.get("status")
    task_id = str(task.get("id") or "")
    anchor: datetime | None = None
    for event in events:
        if event.get("entity_type") != "task" or event.get("action") != "status_changed":
            continue
        if str(event.get("entity_id") or "") != task_id:
            continue
        details = _parse_details(event.get("details"))
        if details.get("to") != current_status:
            continue
        anchor = _event_timestamp(event)
        if anchor is not None:
            break
    if anchor is None:
        return events

    focused: list[dict[str, Any]] = []
    for event in events:
        event_ts = _event_timestamp(event)
        if event_ts is None:
            continue
        if abs((event_ts - anchor).total_seconds()) <= window_seconds:
            focused.append(event)
    return focused or events


def _latest_commit(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("entity_type") == "artifact" and event.get("action") == "commit_recorded":
            details = _parse_details(event.get("details"))
            sha = details.get("sha")
            if sha:
                return str(sha)
    return None


def _latest_summary(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("entity_type") == "decision" and event.get("action") == "recorded":
            details = _parse_details(event.get("details"))
            summary = details.get("summary")
            if summary:
                return str(summary)
    for event in events:
        if event.get("entity_type") == "workspace_state" and event.get("action") == "updated":
            details = _parse_details(event.get("details"))
            notes = details.get("notes")
            if notes:
                return str(notes)
    return None


def _truncate(value: str | None, limit: int = 350) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _task_payload(
    repo_root: str,
    task: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    reply_enabled: bool = False,
    command_help: str = THREAD_COMMANDS,
) -> dict[str, Any]:
    short_id = mcp_queue._short_task_id(str(task["id"]))
    status = str(task.get("status") or "").upper()
    owner = str(task.get("owner_id") or "-")
    priority = task.get("priority")
    focused_events = _focus_events_for_status(task, events)
    commit = _latest_commit(focused_events)
    summary = _truncate(_latest_summary(focused_events))
    acceptance = _truncate(str(task.get("acceptance_criteria") or ""), limit=220)

    fields = [
        {"type": "mrkdwn", "text": f"*Task*\n{short_id}"},
        {"type": "mrkdwn", "text": f"*Status*\n{status}"},
        {"type": "mrkdwn", "text": f"*Owner*\n{owner}"},
    ]
    if priority is not None:
        fields.append({"type": "mrkdwn", "text": f"*Priority*\n{priority}"})
    if commit:
        fields.append({"type": "mrkdwn", "text": f"*Commit*\n`{commit}`"})

    workspace_label = _workspace_label(repo_root)

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{workspace_label} task {short_id} needs attention"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{task.get('title')}*\n{_truncate(str(task.get('description') or ''), limit=260)}",
            },
        },
        {"type": "section", "fields": fields},
    ]
    if summary:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Latest MCP summary*\n{summary}"},
            }
        )
    if acceptance:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"*Acceptance*: {acceptance}"},
                    {"type": "mrkdwn", "text": f"*Repo*: `{repo_root}`"},
                ],
            }
        )
    if reply_enabled:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": command_help}],
            }
        )

    text = f"[{workspace_label}] {status} {short_id} {task.get('title')}"
    if reply_enabled:
        text = f"{text}. {command_help}"
    return {"text": text, "blocks": blocks}


def _http_post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> Any:
    encoded = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("content-type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise mcp_queue.McpError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        if not shutil.which("curl"):
            raise mcp_queue.McpError(f"Slack transport failed: {exc}") from exc
        curl_cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "-X",
            "POST",
            "-H",
            "content-type: application/json; charset=utf-8",
        ]
        for key, value in (headers or {}).items():
            curl_cmd.extend(["-H", f"{key}: {value}"])
        curl_cmd.extend(["--data-binary", "@-", url])
        proc = subprocess.run(curl_cmd, input=encoded, capture_output=True)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise mcp_queue.McpError(f"Slack transport fallback failed: {stderr}") from exc
        body = proc.stdout.decode("utf-8", errors="replace")
        status = 200

    if status < 200 or status >= 300:
        raise mcp_queue.McpError(f"Slack returned HTTP {status}: {body}")
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _post_to_webhook(webhook_url: str, payload: dict[str, Any]) -> None:
    body = _http_post_json(webhook_url, payload)
    if isinstance(body, str) and body.strip() not in {"", "ok"}:
        raise mcp_queue.McpError(f"Slack webhook returned unexpected body: {body}")


def _slack_api_call(method: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = _http_post_json(
        f"{SLACK_API_BASE}/{method}",
        payload,
        headers={"authorization": f"Bearer {token}"},
    )
    if not isinstance(response, dict):
        raise mcp_queue.McpError(f"Slack {method} returned unexpected response: {response!r}")
    if not response.get("ok"):
        raise mcp_queue.McpError(f"Slack {method} failed: {response.get('error') or response}")
    return response


def _post_message(token: str, channel: str, payload: dict[str, Any], *, thread_ts: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "channel": channel,
        "text": str(payload.get("text") or ""),
    }
    blocks = payload.get("blocks")
    if isinstance(blocks, list) and blocks:
        body["blocks"] = blocks
    if thread_ts:
        body["thread_ts"] = thread_ts
    return _slack_api_call("chat.postMessage", token, body)


def _thread_record(response: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    return {
        "channel": str(response.get("channel") or ""),
        "fingerprint": fingerprint,
        "ignored_reply_ts": [],
        "last_reply_ts": str(response.get("ts") or ""),
        "thread_ts": str(response.get("ts") or ""),
    }


def _is_dm_channel(channel: str | None) -> bool:
    return bool(channel) and str(channel).startswith("D")


def _parse_owner(value: str) -> str:
    owner = value.strip().strip("`").lower()
    if not re.fullmatch(r"[a-z0-9._-]+", owner):
        raise SlackReplyError("owner ids must match [a-z0-9._-]+")
    return owner


def _parse_status(value: str) -> str:
    status = value.strip().strip("`").lower().replace("-", "_")
    if status not in TASK_STATUSES:
        raise SlackReplyError(f"unsupported status `{status}`")
    return status


def _parse_reply_command(text: str) -> dict[str, str]:
    body = text.strip()
    if not body:
        raise SlackReplyError("empty Slack reply")

    if re.fullmatch(r"(?is)(help|\?)", body):
        return {"action": "help"}

    match = re.match(r"(?is)^decision\s*:\s*(.+)$", body)
    if match:
        return {"action": "decision", "summary": match.group(1).strip()}

    match = re.match(r"(?is)^(assign|claim)\s*:\s*([a-z0-9._-]+)\s*$", body)
    if match:
        return {"action": "assign", "owner": _parse_owner(match.group(2))}

    match = re.match(r"(?is)^reassign\s*:\s*([a-z0-9._-]+)(?:\s*\|\s*(.+)|\s+(.+))?$", body)
    if match:
        reason = match.group(2) or match.group(3) or ""
        return {
            "action": "reassign",
            "owner": _parse_owner(match.group(1)),
            "reason": reason.strip(),
        }

    match = re.match(r"(?is)^block\s*:\s*(.+)$", body)
    if match:
        return {"action": "block", "summary": match.group(1).strip()}

    match = re.match(r"(?is)^unblock\s*:\s*(.+)$", body)
    if match:
        return {"action": "unblock", "summary": match.group(1).strip()}

    match = re.match(r"(?is)^status\s*:\s*([a-z_-]+)(?:\s*\|\s*(.+))?$", body)
    if match:
        return {
            "action": "status",
            "status": _parse_status(match.group(1)),
            "summary": (match.group(2) or "").strip(),
        }

    if re.fullmatch(r"(?is)(?:push|push it|push this)", body):
        return {"action": "push"}

    if re.fullmatch(r"(?is)(?:deploy|deploy it|deploy this)", body):
        return {"action": "deploy"}

    if re.fullmatch(r"(?is)(?:push\s+(?:and|&)\s+deploy|deploy\s+(?:and|&)\s+push)", body):
        return {"action": "push_deploy"}

    conversational = _parse_conversational_reply(body)
    if conversational is not None:
        return conversational

    raise SlackReplyError("unrecognized Slack reply command; reply `help` for supported commands")


def _parse_dm_command(text: str) -> dict[str, str]:
    body = text.strip()
    if not body:
        raise SlackReplyError("empty Slack DM")
    if re.fullmatch(r"(?is)(help|\?)", body):
        return {"action": "help"}
    if re.fullmatch(r"(?is)tasks", body):
        return {"action": "tasks"}

    conversational = _parse_conversational_reply(body, top_level=True)
    if conversational is not None and conversational["action"] in {
        "help",
        "tasks",
        "smalltalk",
        "team_status",
        "owner_summary",
        "assign_next",
        "create_task",
    }:
        return conversational

    match = re.match(r"(?is)^(?:task\s+)?([0-9a-f]{8})\s+(.+)$", body)
    if not match:
        task_refs = re.findall(r"(?i)\b([0-9a-f]{8})\b", body)
        if len(task_refs) == 1:
            task_ref = task_refs[0].lower()
            without_ref = re.sub(rf"(?i)\b{re.escape(task_ref)}\b", " ", body, count=1)
            command = _parse_reply_command(without_ref.strip())
            command["task_ref"] = task_ref
            return command
        raise SlackReplyError(
            "I need a task context here. Reply in-thread to a task message, ask `what needs review?`, or send `task <id> ...`."
        )
    command = _parse_reply_command(match.group(2).strip())
    command["task_ref"] = match.group(1).lower()
    return command


def _slack_actor(user_id: str) -> str:
    return f"slack:{user_id}"


def _task_short_id(task_id: str) -> str:
    return mcp_queue._short_task_id(task_id)


def _task_title_from_request(request: str) -> str:
    text = re.sub(r"\s+", " ", request.strip()).strip(" .")
    if not text:
        return "Slack task request"
    text = text[0].upper() + text[1:]
    if len(text) <= 72:
        return text
    return f"{text[:69].rstrip()}..."


def _task_acceptance_from_request(request: str) -> str:
    text = re.sub(r"\s+", " ", request.strip())
    lowered = text.casefold()
    if re.search(r"\b(review|inspect|summarize|map|audit|analy[sz]e)\b", lowered):
        return (
            "The requested repository area is reviewed, key findings and risks are summarized, "
            "and recommended bounded next tasks are recorded in MCP."
        )
    return f"Requested outcome from Slack is completed and reflected in MCP: {text}"


def _action_log_dir(repo_root: str) -> Path:
    return Path.home() / "Library" / "Application Support" / _workspace_label(repo_root) / "slack_actions"


def _run_action_command(repo_root: str, action: str) -> Path:
    env_var = ACTION_COMMAND_ENV[action]
    command = str(os.getenv(env_var) or "").strip()
    if not command:
        raise SlackReplyError(f"`{action}` is not configured for {_workspace_label(repo_root)}")

    log_dir = _action_log_dir(repo_root)
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{timestamp}-{action}.log"
    proc = subprocess.run(
        ["/bin/zsh", "-lc", command],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    log_path.write_text(
        "\n".join(
            [
                f"action={action}",
                f"repo_root={repo_root}",
                f"command={command}",
                f"exit_code={proc.returncode}",
                "",
                "stdout:",
                proc.stdout,
                "",
                "stderr:",
                proc.stderr,
            ]
        ),
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise SlackReplyError(f"`{action}` failed for {_workspace_label(repo_root)}; see {log_path}")
    return log_path


def _open_task_from_request(
    client: mcp_queue.TeamCoordinationClient,
    repo_root: str,
    request: str,
    *,
    slack_user_id: str,
) -> dict[str, Any]:
    workspace = mcp_queue._workspace_for_repo(client, repo_root)
    title = _task_title_from_request(request)
    description = f"Slack task request from <@{slack_user_id}>: {request.strip()}"
    acceptance = _task_acceptance_from_request(request)
    created = client.call_tool(
        "create_task",
        {
            "workspace_id": workspace["id"],
            "title": title,
            "description": description,
            "acceptance_criteria": acceptance,
            "priority": 50,
        },
    )
    if not isinstance(created, dict) or not created.get("id"):
        raise mcp_queue.McpError("create_task returned an unexpected payload")
    _append_decision(
        client,
        task_id=str(created["id"]),
        author=_slack_actor(slack_user_id),
        summary=f"Slack opened task {_task_short_id(str(created['id']))}: {title}",
        rationale=request.strip(),
    )
    return created


def _tracked_task_lookup(threads: dict[str, Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for task_id in threads:
        lookup[_task_short_id(task_id)] = task_id
    return lookup


def _workspace_task_lookup(
    client: mcp_queue.TeamCoordinationClient,
    repo_root: str,
) -> dict[str, str]:
    workspace = mcp_queue._workspace_for_repo(client, repo_root)
    tasks = mcp_queue._list_tasks(client, workspace["id"])
    lookup: dict[str, str] = {}
    for task in tasks:
        task_id = str(task.get("id") or "")
        if task_id:
            lookup[_task_short_id(task_id)] = task_id
    return lookup


def _workspace_label(repo_root: str) -> str:
    return Path(repo_root).name or repo_root


def _normalize_passphrase_text(text: str) -> str:
    value = text.strip().strip("`")
    value = re.sub(r"(?is)^(?:pass[\s-]*phrase|passphrase)\s*(?::|is)?\s*", "", value)
    value = re.sub(r"[.!?]+$", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.casefold()


def _matches_expected_passphrase(expected: str | None, text: str) -> bool:
    if not expected:
        return False
    return _normalize_passphrase_text(expected) == _normalize_passphrase_text(text)


def _dm_help_text(repo_root: str) -> str:
    workspace_label = _workspace_label(repo_root)
    return (
        f"I'm watching {workspace_label}. Ask `who is doing what?`, `what needs review?`, `what is claude working on?`, "
        "or `have claude take the next task`. Claude is the default implementation owner here; Codex handles "
        "review, architecture, and merge/deploy calls. You can also reply in-thread to a task message or send "
        "`task <id> approve this`."
    )


def _thread_help_text(task_id: str) -> str:
    short_id = _task_short_id(task_id)
    return (
        f"I'm watching {short_id}. In this thread you can say `approve this`, "
        "`block because ...`, `assign to claude`, `push`, `deploy`, or `mark as done`."
    )


def _format_attention_tasks(
    client: mcp_queue.TeamCoordinationClient,
    repo_root: str,
) -> str:
    workspace = mcp_queue._workspace_for_repo(client, repo_root)
    tasks = _collect_status_tasks(client, workspace["id"], tuple(DEFAULT_STATUSES))
    workspace_label = _workspace_label(repo_root)
    if not tasks:
        return f"No MCP tasks in {workspace_label} currently need attention."
    items = []
    for task in tasks[:5]:
        items.append(
            f"{_task_short_id(str(task['id']))} {str(task.get('status') or '').upper()} {str(task.get('title') or '').strip()}"
        )
    return (
        f"Tasks needing attention in {workspace_label}: {'; '.join(items)}. "
        "Reply in-thread to a task message or send `task <id> ...`."
    )


def _format_task_collection(tasks: list[dict[str, Any]], *, limit: int = 3) -> str:
    if not tasks:
        return "none"
    items = []
    for task in _sorted_tasks(tasks)[:limit]:
        items.append(
            f"{_task_short_id(str(task['id']))} {str(task.get('status') or '').upper()} {str(task.get('title') or '').strip()}"
        )
    return "; ".join(items)


def _latest_actor_workspace_event(
    client: mcp_queue.TeamCoordinationClient,
    repo_root: str,
    actor: str,
    *,
    limit: int = 120,
) -> dict[str, Any] | None:
    workspace = mcp_queue._workspace_for_repo(client, repo_root)
    task_ids = {
        str(task.get("id") or "")
        for task in mcp_queue._list_tasks(client, workspace["id"])
        if task.get("id")
    }
    if not task_ids:
        return None

    for event in mcp_queue._get_events(client, entity_type="task", limit=limit):
        if str(event.get("actor") or "") != actor:
            continue
        if str(event.get("entity_id") or "") not in task_ids:
            continue
        return event
    return None


def _format_actor_workspace_event(event: dict[str, Any] | None) -> str:
    if not event:
        return "none recorded recently"

    task_ref = _task_short_id(str(event.get("entity_id") or ""))
    timestamp = str(event.get("timestamp") or "unknown time")
    action = str(event.get("action") or "")
    if action == "claimed":
        return f"{timestamp} claimed {task_ref}"
    if action == "status_changed":
        details = _parse_details(event.get("details"))
        to_status = str(details.get("to") or "").upper()
        if to_status:
            return f"{timestamp} moved {task_ref} to {to_status}"
    return f"{timestamp} {action} {task_ref}".strip()


def _format_team_status(
    client: mcp_queue.TeamCoordinationClient,
    repo_root: str,
) -> str:
    workspace = mcp_queue._workspace_for_repo(client, repo_root)
    tasks = mcp_queue._list_tasks(client, workspace["id"])
    workspace_label = _workspace_label(repo_root)
    claude_tasks = [
        task for task in tasks if task.get("owner_id") == "claude" and task.get("status") in mcp_queue.ACTIVE_STATUSES
    ]
    codex_tasks = [
        task for task in tasks if task.get("owner_id") == "codex" and task.get("status") in mcp_queue.ACTIVE_STATUSES
    ]
    attention = [task for task in tasks if task.get("status") in DEFAULT_STATUSES]
    blocked_tasks = [task for task in tasks if task.get("status") == "blocked"]

    claude_event = _latest_actor_workspace_event(client, repo_root, "claude")
    codex_event = _latest_actor_workspace_event(client, repo_root, "codex")

    lines = [f"Team status in {workspace_label}"]
    if blocked_tasks:
        lines.append("PROGRESS IS HALTED")
    lines.extend(
        [
            f"- Claude active: {_format_task_collection(claude_tasks)}",
            f"- Last Claude action: {_format_actor_workspace_event(claude_event)}",
            f"- Codex active: {_format_task_collection(codex_tasks)}",
            f"- Last Codex action: {_format_actor_workspace_event(codex_event)}",
            f"- Needs attention: {_format_task_collection(attention)}",
        ]
    )
    if blocked_tasks:
        lines.append(f"- Blocked work: {_format_task_collection(blocked_tasks)}")
    return "\n".join(lines)


def _sorted_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        tasks,
        key=lambda item: (
            -int(item.get("priority") or 0),
            str(item.get("created_at") or ""),
            str(item.get("updated_at") or ""),
            str(item.get("id") or ""),
        ),
    )


def _format_owner_tasks(
    client: mcp_queue.TeamCoordinationClient,
    repo_root: str,
    owner: str,
) -> str:
    workspace = mcp_queue._workspace_for_repo(client, repo_root)
    tasks = [
        task
        for task in mcp_queue._list_tasks(client, workspace["id"])
        if task.get("owner_id") == owner and task.get("status") in mcp_queue.ACTIVE_STATUSES
    ]
    workspace_label = _workspace_label(repo_root)
    if not tasks:
        return f"{owner} has no active MCP tasks in {workspace_label}."
    items = []
    for task in _sorted_tasks(tasks)[:5]:
        items.append(
            f"{_task_short_id(str(task['id']))} {str(task.get('status') or '').upper()} {str(task.get('title') or '').strip()}"
        )
    return f"{owner} is currently handling in {workspace_label}: {'; '.join(items)}."


def _next_unowned_todo_task(
    client: mcp_queue.TeamCoordinationClient,
    repo_root: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    workspace = mcp_queue._workspace_for_repo(client, repo_root)
    todo_tasks = [
        task
        for task in mcp_queue._list_tasks(client, workspace["id"], status="todo")
        if not task.get("owner_id")
    ]
    if not todo_tasks:
        return None, []
    ordered = _sorted_tasks(todo_tasks)
    top_priority = int(ordered[0].get("priority") or 0)
    contenders = [task for task in ordered if int(task.get("priority") or 0) == top_priority]
    if len(contenders) != 1:
        return None, contenders
    return contenders[0], []


def _assign_next_task_to_owner(
    client: mcp_queue.TeamCoordinationClient,
    repo_root: str,
    owner: str,
    *,
    slack_user_id: str,
) -> str:
    workspace_label = _workspace_label(repo_root)
    task, contenders = _next_unowned_todo_task(client, repo_root)
    if contenders:
        options = "; ".join(
            f"{_task_short_id(str(item['id']))} P{int(item.get('priority') or 0)} {str(item.get('title') or '').strip()}"
            for item in _sorted_tasks(contenders)[:5]
        )
        return (
            f"I didn't assign a next task in {workspace_label} because multiple unowned todo tasks tie for top priority: "
            f"{options}. Tell me which one to assign."
        )
    if task is None:
        return f"There is no unowned todo task for {owner} in {workspace_label} right now."

    task_id = str(task["id"])
    short_id = _task_short_id(task_id)
    author = _slack_actor(slack_user_id)
    client.call_tool(
        "claim_task",
        {"task_id": task_id, "owner_type": "agent", "owner_id": owner},
    )
    _append_decision(
        client,
        task_id=task_id,
        author=author,
        summary=f"Slack assigned next task {short_id} to {owner}.",
        rationale="The owner must reserve a unique MCP branch before editing.",
    )
    return f"Assigned next task {short_id} to {owner} in {workspace_label}."


def _parse_conversational_reply(text: str, *, top_level: bool = False) -> dict[str, str] | None:
    body = text.strip()
    if not body:
        return None
    compact = re.sub(r"\s+", " ", body.strip())
    clean = re.sub(r"[.!?]+$", "", compact)
    lowered = clean.casefold()

    if top_level and (
        re.search(r"\bteam\s+status\b", lowered)
        or re.search(r"\bwho(?:'s| is)\b.*\bdoing\b.*\bwhat\b", lowered)
        or re.search(r"\bwho(?:'s| is)\b.*\bworking\b.*\bon\b.*\bwhat\b", lowered)
        or lowered in {"what is everyone doing", "whats everyone doing", "what's everyone doing"}
    ):
        return {"action": "team_status"}

    if top_level and (
        re.search(r"\b(what|show|list)\b.*\b(review|attention|tasks|queue|open)\b", lowered)
        or lowered
        in {
            "what needs review",
            "what needs review?",
            "what needs attention",
            "what needs attention?",
            "show tasks",
            "show me tasks",
            "show me the tasks",
            "what's open",
            "whats open",
            "what is open",
            "review queue",
        }
    ):
        return {"action": "tasks"}

    match = re.fullmatch(r"(?is)(?:new|create|open)\s+task\b[:.\s-]*(.+)", clean)
    if top_level and match:
        request = match.group(1).strip()
        if request:
            return {"action": "create_task", "request": request}

    match = re.fullmatch(r"(?is)(?:what(?:'s| is)?|show|list)\s+([a-z0-9._-]+)\s+(?:working on|doing|handling)", clean)
    if top_level and match:
        return {"action": "owner_summary", "owner": _parse_owner(match.group(1))}

    match = re.fullmatch(
        r"(?is)(?:have|let|make|ask|give)\s+([a-z0-9._-]+)\s+(?:take|pick up|handle|do|work on)(?:\s+the)?\s+next(?:\s+\w+)?",
        clean,
    )
    if top_level and match:
        return {"action": "assign_next", "owner": _parse_owner(match.group(1))}

    match = re.fullmatch(r"(?is)(?:give|assign)\s+([a-z0-9._-]+)\s+the\s+next(?:\s+\w+)?", clean)
    if top_level and match:
        return {"action": "assign_next", "owner": _parse_owner(match.group(1))}

    if top_level and re.fullmatch(r"(?is)(?:take|pick up|handle|do|work on)(?:\s+the)?\s+next(?:\s+\w+)?", clean):
        return {"action": "assign_next", "owner": "claude"}

    if top_level and re.fullmatch(r"(?is)(?:give|assign)\s+the\s+next(?:\s+\w+)?", clean):
        return {"action": "assign_next", "owner": "claude"}

    if lowered in {"hi", "hello", "hey", "yo", "thanks", "thank you", "got it", "ok", "okay", "cool"}:
        return {"action": "smalltalk"}

    if lowered in {
        "approve",
        "approved",
        "approve this",
        "approve it",
        "looks good",
        "looks good to me",
        "ship",
        "ship it",
        "merge",
        "merge it",
        "lgtm",
        "ready to merge",
    }:
        return {"action": "decision", "summary": "Approved in Slack."}
    if re.fullmatch(r"(?is)(?:please\s+)?approve(?:\s+(?:this|it))?", clean):
        return {"action": "decision", "summary": "Approved in Slack."}

    match = re.fullmatch(
        r"(?is)(?:please\s+)?(?:block|pause|hold|stop)(?:\s+(?:this|it))?(?:\s*(?:because|for|due to|:)\s*(.+))?",
        clean,
    )
    if match:
        return {"action": "block", "summary": (match.group(1) or "Blocked from Slack.").strip()}

    match = re.fullmatch(
        r"(?is)(?:please\s+)?(?:unblock|resume|continue|proceed)(?:\s+(?:this|it))?(?:\s*(?:because|with|:)\s*(.+))?",
        clean,
    )
    if match:
        return {"action": "unblock", "summary": (match.group(1) or "Proceed from Slack.").strip()}

    match = re.fullmatch(
        r"(?is)(?:please\s+)?(?:assign|claim|give|hand)\s+(?:this|it)?\s*(?:to\s+)?([a-z0-9._-]+)",
        clean,
    )
    if match:
        return {"action": "assign", "owner": _parse_owner(match.group(1))}

    match = re.fullmatch(
        r"(?is)(?:please\s+)?have\s+([a-z0-9._-]+)\s+(?:take|pick up|handle|do|work on)\s+(?:this|it)",
        clean,
    )
    if match:
        return {"action": "assign", "owner": _parse_owner(match.group(1))}

    if re.fullmatch(r"(?is)(?:please\s+)?(?:work on|take|pick up|handle|do)(?:\s+(?:this|it))?", clean):
        return {"action": "assign", "owner": "claude"}

    match = re.fullmatch(
        r"(?is)(?:please\s+)?(?:reassign|move|switch)\s+(?:this|it)?\s*(?:to\s+)([a-z0-9._-]+)(?:\s*(?:\||because)\s*(.+))?",
        clean,
    )
    if match:
        return {
            "action": "reassign",
            "owner": _parse_owner(match.group(1)),
            "reason": (match.group(2) or "").strip(),
        }

    match = re.fullmatch(
        r"(?is)(?:please\s+)?(?:mark|set)\s+(?:this|it)?\s*(?:as\s+)?([a-z_-]+)(?:\s*(?:\||because|:)\s*(.+))?",
        compact,
    )
    if match:
        return {
            "action": "status",
            "status": _parse_status(match.group(1)),
            "summary": (match.group(2) or "").strip(),
        }
    return None


def _append_decision(
    client: mcp_queue.TeamCoordinationClient,
    *,
    task_id: str,
    author: str,
    summary: str,
    rationale: str | None = None,
) -> None:
    args: dict[str, Any] = {"task_id": task_id, "author": author, "summary": summary}
    if rationale:
        args["rationale"] = rationale
    client.call_tool("append_decision_log", args)


def _apply_reply_command(
    client: mcp_queue.TeamCoordinationClient,
    task: dict[str, Any],
    command: dict[str, str],
    *,
    repo_root: str,
    slack_user_id: str,
) -> str:
    task_id = str(task["id"])
    short_id = _task_short_id(task_id)
    author = _slack_actor(slack_user_id)
    mention = f"<@{slack_user_id}>"

    status = str(task.get("status") or "")
    current_owner = str(task.get("owner_id") or "")

    if command["action"] == "help":
        return _thread_help_text(task_id)

    if command["action"] == "smalltalk":
        return _thread_help_text(task_id)

    if command["action"] == "decision":
        summary = command["summary"]
        _append_decision(
            client,
            task_id=task_id,
            author=author,
            summary=summary,
            rationale=f"Recorded from Slack thread reply by {mention}.",
        )
        return f"Applied to MCP: recorded decision on {short_id}."

    if status in {"done", "canceled"} and command["action"] not in {"decision", "help"}:
        raise SlackReplyError(f"task {short_id} is already {status}")

    if command["action"] in {"push", "deploy", "push_deploy"}:
        workspace = mcp_queue._workspace_for_repo(client, repo_root)
        actions = ["push", "deploy"] if command["action"] == "push_deploy" else [command["action"]]
        log_paths: list[Path] = []
        for action_name in actions:
            log_path = _run_action_command(repo_root, action_name)
            log_paths.append(log_path)
            client.call_tool(
                "attach_artifact",
                {
                    "workspace_id": workspace["id"],
                    "task_id": task_id,
                    "type": "log",
                    "path_or_url": str(log_path),
                    "metadata": {"action": action_name},
                },
            )
            _append_decision(
                client,
                task_id=task_id,
                author=author,
                summary=f"Slack {action_name} completed for {short_id}.",
                rationale=f"Executed in {repo_root}. Log: {log_path}",
            )
        if "deploy" in actions and status not in {"done", "canceled"}:
            client.call_tool(
                "update_task_status",
                {"task_id": task_id, "status": "done", "actor": author},
            )
            return f"Applied to MCP: {short_id} was pushed and deployed, and is now done."
        if command["action"] == "push_deploy":
            return f"Applied to MCP: {short_id} was pushed and deployed."
        return f"Applied to MCP: {short_id} {command['action']} completed."

    if command["action"] == "assign":
        owner = command["owner"]
        if current_owner and current_owner != owner:
            raise SlackReplyError(f"task {short_id} is owned by {current_owner}; use `reassign: {owner} | reason`")
        if current_owner == owner:
            return f"No MCP change: {short_id} is already owned by {owner}."
        client.call_tool(
            "claim_task",
            {"task_id": task_id, "owner_type": "agent", "owner_id": owner},
        )
        _append_decision(
            client,
            task_id=task_id,
            author=author,
            summary=f"Slack assigned task {short_id} to {owner}.",
            rationale="The new owner must reserve a unique MCP branch before editing.",
        )
        return f"Applied to MCP: {short_id} is now assigned to {owner}."

    if command["action"] == "reassign":
        owner = command["owner"]
        reason = command.get("reason") or "Slack requested reassignment."
        previous = current_owner or "unowned"
        _append_decision(
            client,
            task_id=task_id,
            author=author,
            summary=f"Slack requested reassignment of {short_id} from {previous} to {owner}.",
            rationale=reason,
        )
        if current_owner:
            client.call_tool("release_task", {"task_id": task_id})
        client.call_tool(
            "claim_task",
            {"task_id": task_id, "owner_type": "agent", "owner_id": owner},
        )
        _append_decision(
            client,
            task_id=task_id,
            author=author,
            summary=f"Task {short_id} reassigned to {owner}.",
            rationale="Previous branch ownership remains with the prior owner; the new owner must reserve a new MCP branch before editing.",
        )
        return f"Applied to MCP: {short_id} reassigned to {owner}."

    if command["action"] == "block":
        summary = command["summary"]
        client.call_tool(
            "set_blocker",
            {"task_id": task_id, "kind": "slack_reply", "description": summary, "status": "open"},
        )
        client.call_tool(
            "update_task_status",
            {"task_id": task_id, "status": "blocked", "actor": author},
        )
        _append_decision(
            client,
            task_id=task_id,
            author=author,
            summary=f"Slack blocked task {short_id}: {summary}",
        )
        return f"Applied to MCP: {short_id} is now blocked."

    if command["action"] == "unblock":
        summary = command["summary"]
        resume_status = "claimed" if current_owner else "todo"
        client.call_tool(
            "set_blocker",
            {"task_id": task_id, "kind": "slack_reply", "description": summary, "status": "closed"},
        )
        if status == "blocked":
            client.call_tool(
                "update_task_status",
                {"task_id": task_id, "status": resume_status, "actor": author},
            )
        _append_decision(
            client,
            task_id=task_id,
            author=author,
            summary=f"Slack unblocked task {short_id}: {summary}",
            rationale=f"Resume status set to {resume_status} when possible.",
        )
        if status == "blocked":
            return f"Applied to MCP: {short_id} is now {resume_status}."
        return f"Recorded in MCP: {short_id} was not blocked, so no status change was needed."

    if command["action"] == "status":
        next_status = command["status"]
        summary = command.get("summary") or f"Slack set task {short_id} to {next_status}."
        client.call_tool(
            "update_task_status",
            {"task_id": task_id, "status": next_status, "actor": author},
        )
        if next_status == "blocked" and command.get("summary"):
            client.call_tool(
                "set_blocker",
                {"task_id": task_id, "kind": "slack_reply", "description": command["summary"], "status": "open"},
            )
        _append_decision(
            client,
            task_id=task_id,
            author=author,
            summary=summary,
        )
        return f"Applied to MCP: {short_id} is now {next_status}."

    raise SlackReplyError(f"unsupported Slack reply action `{command['action']}`")


def _sorted_replies(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(messages, key=lambda message: str(message.get("ts") or ""))


def _compact_ignored_ts(values: list[str], keep: int = 20) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen[-keep:]


def _process_thread_replies(
    args: argparse.Namespace,
    client: mcp_queue.TeamCoordinationClient,
    state: dict[str, Any],
) -> list[str]:
    if not args.reply_token:
        return []

    threads = state.get("threads") or {}
    if not isinstance(threads, dict) or not threads:
        return []

    acknowledgements: list[str] = []
    for task_id, thread in list(threads.items()):
        if not isinstance(thread, dict):
            continue
        channel = str(thread.get("channel") or "")
        thread_ts = str(thread.get("thread_ts") or "")
        if not channel or not thread_ts:
            continue

        oldest = str(thread.get("last_reply_ts") or thread_ts)
        response = _slack_api_call(
            "conversations.replies",
            args.reply_token,
            {"channel": channel, "ts": thread_ts, "oldest": oldest, "inclusive": True, "limit": 200},
        )
        messages = response.get("messages") or []
        ignored = set(_compact_ignored_ts(list(thread.get("ignored_reply_ts") or [])))

        for message in _sorted_replies(messages):
            message_ts = str(message.get("ts") or "")
            if not message_ts or message_ts <= oldest:
                continue
            if message_ts in ignored:
                continue
            if str(message.get("thread_ts") or thread_ts) != thread_ts:
                continue
            if message.get("subtype") or message.get("bot_id") or not message.get("user"):
                continue

            task = mcp_queue._get_task(client, task_id)
            user_id = str(message.get("user") or "")
            text = str(message.get("text") or "").strip()
            try:
                command = _parse_reply_command(text)
                ack_text = _apply_reply_command(client, task, command, repo_root=args.repo_root, slack_user_id=user_id)
            except SlackReplyError as exc:
                ack_text = f"Could not apply to MCP for {_task_short_id(task_id)}: {exc}"
            if args.post_token:
                ack_response = _post_message(
                    args.post_token,
                    channel,
                    {"text": ack_text},
                    thread_ts=thread_ts,
                )
                ignored.add(str(ack_response.get("ts") or ""))
                thread["ignored_reply_ts"] = _compact_ignored_ts(list(ignored))
            acknowledgements.append(ack_text)
            thread["last_reply_ts"] = message_ts
            oldest = message_ts

        thread.setdefault("last_reply_ts", oldest)

    return acknowledgements


def _post_ack(args: argparse.Namespace, channel: str, text: str, *, thread_ts: str | None = None) -> str:
    if not args.post_token:
        return ""
    response = _post_message(args.post_token, channel, {"text": text}, thread_ts=thread_ts)
    return str(response.get("ts") or "")


def _process_dm_replies(
    args: argparse.Namespace,
    client: mcp_queue.TeamCoordinationClient,
    state: dict[str, Any],
) -> list[str]:
    if not args.reply_token or not _is_dm_channel(args.channel):
        return []

    response = _slack_api_call(
        "conversations.history",
        args.reply_token,
        {"channel": args.channel, "limit": 200},
    )
    messages = response.get("messages") or []
    if not isinstance(messages, list):
        return []

    dm_last_ts = str(state.get("dm_last_ts") or "")
    latest_seen = dm_last_ts
    threads = state.get("threads") or {}
    if not isinstance(threads, dict):
        threads = {}
    thread_lookup = {str(record.get("thread_ts") or ""): task_id for task_id, record in threads.items() if isinstance(record, dict)}
    task_lookup = _tracked_task_lookup(threads)
    workspace_lookup: dict[str, str] | None = None
    acknowledgements: list[str] = []
    expected_passphrase = str(state.get("dm_expected_passphrase") or "").strip()
    workspace_label = _workspace_label(args.repo_root)

    for message in _sorted_replies(messages):
        message_ts = str(message.get("ts") or "")
        if message_ts and (not latest_seen or message_ts > latest_seen):
            latest_seen = message_ts
        if not message_ts or (dm_last_ts and message_ts <= dm_last_ts):
            continue
        if message.get("subtype") or message.get("bot_id") or not message.get("user"):
            continue

        user_id = str(message.get("user") or "")
        text = str(message.get("text") or "").strip()
        thread_ts = str(message.get("thread_ts") or "")

        try:
            if thread_ts and thread_ts in thread_lookup:
                task_id = thread_lookup[thread_ts]
                task = mcp_queue._get_task(client, task_id)
                command = _parse_reply_command(text)
                ack_text = _apply_reply_command(client, task, command, repo_root=args.repo_root, slack_user_id=user_id)
                ack_ts = _post_ack(args, args.channel, ack_text, thread_ts=thread_ts)
            else:
                if expected_passphrase and _matches_expected_passphrase(expected_passphrase, text):
                    ack_text = f"Pass phrase accepted for {workspace_label}. DM replies are working."
                    state.pop("dm_expected_passphrase", None)
                    expected_passphrase = ""
                    ack_ts = _post_ack(args, args.channel, ack_text)
                else:
                    try:
                        command = _parse_dm_command(text)
                    except SlackReplyError:
                        if expected_passphrase:
                            ack_text = (
                                f"Pass phrase not accepted for {workspace_label}. "
                                "Reply with the exact pass phrase."
                            )
                            ack_ts = _post_ack(args, args.channel, ack_text)
                        else:
                            raise
                    else:
                        if command["action"] in {"help", "smalltalk"}:
                            ack_text = _dm_help_text(args.repo_root)
                            ack_ts = _post_ack(args, args.channel, ack_text)
                        elif command["action"] == "team_status":
                            ack_text = _format_team_status(client, args.repo_root)
                            ack_ts = _post_ack(args, args.channel, ack_text)
                        elif command["action"] == "tasks":
                            ack_text = _format_attention_tasks(client, args.repo_root)
                            ack_ts = _post_ack(args, args.channel, ack_text)
                        elif command["action"] == "owner_summary":
                            ack_text = _format_owner_tasks(client, args.repo_root, command["owner"])
                            ack_ts = _post_ack(args, args.channel, ack_text)
                        elif command["action"] == "assign_next":
                            ack_text = _assign_next_task_to_owner(
                                client,
                                args.repo_root,
                                command["owner"],
                                slack_user_id=user_id,
                            )
                            ack_ts = _post_ack(args, args.channel, ack_text)
                        elif command["action"] == "create_task":
                            created = _open_task_from_request(
                                client,
                                args.repo_root,
                                command["request"],
                                slack_user_id=user_id,
                            )
                            created_id = str(created["id"])
                            short_id = _task_short_id(created_id)
                            ack_text = (
                                f"Opened MCP task {short_id} in {workspace_label}: {created.get('title')}. "
                                "It is now a todo task. Reply in-thread with `have claude take this` or send "
                                f"`work on {short_id}` to route it to Claude."
                            )
                            ack_ts = _post_ack(args, args.channel, ack_text)
                            if ack_ts:
                                state.setdefault("threads", {})
                                state["threads"][created_id] = {
                                    "channel": args.channel,
                                    "fingerprint": f"slack-created:{ack_ts}",
                                    "ignored_reply_ts": [],
                                    "last_reply_ts": ack_ts,
                                    "thread_ts": ack_ts,
                                }
                        else:
                            task_ref = str(command.pop("task_ref", ""))
                            task_id = task_lookup.get(task_ref)
                            if not task_id:
                                if workspace_lookup is None:
                                    workspace_lookup = _workspace_task_lookup(client, args.repo_root)
                                task_id = workspace_lookup.get(task_ref)
                            if not task_id:
                                raise SlackReplyError("unknown task id in DM command")
                            task = mcp_queue._get_task(client, task_id)
                            ack_text = _apply_reply_command(
                                client,
                                task,
                                command,
                                repo_root=args.repo_root,
                                slack_user_id=user_id,
                            )
                            ack_ts = _post_ack(args, args.channel, ack_text)
        except SlackReplyError as exc:
            ack_text = f"Could not apply to MCP: {exc}"
            ack_ts = _post_ack(args, args.channel, ack_text, thread_ts=thread_ts or None)

        if ack_ts and ack_ts > latest_seen:
            latest_seen = ack_ts
        acknowledgements.append(ack_text)

    if latest_seen:
        state["dm_last_ts"] = latest_seen
    return acknowledgements


def _one_pass(args: argparse.Namespace) -> int:
    client = mcp_queue.TeamCoordinationClient(args.server_url)
    client.initialize()
    workspace = mcp_queue._workspace_for_repo(client, args.repo_root)
    tasks = _collect_status_tasks(client, workspace["id"], tuple(args.statuses))
    if args.owner:
        tasks = [task for task in tasks if task.get("owner_id") == args.owner]

    state = _load_state(args.state_file)
    previous = dict(state.get("notified") or {})
    current: dict[str, str] = {}
    notifications: list[tuple[dict[str, Any], dict[str, Any], str]] = []

    reply_enabled = bool(args.post_token and args.channel and args.reply_token)
    command_help = DM_COMMANDS if _is_dm_channel(args.channel) else THREAD_COMMANDS

    for task in tasks:
        task_id = str(task["id"])
        events = mcp_queue._get_events(client, entity_id=task_id, limit=args.event_limit)
        if not _has_status_anchor(task, events) and len(events) >= args.event_limit:
            events = mcp_queue._get_events(client, entity_id=task_id, limit=max(50, args.event_limit * 4))
        fingerprint = _latest_status_transition_id(task, events)
        current[task_id] = fingerprint
        if previous.get(task_id) == fingerprint:
            continue
        payload = _task_payload(
            args.repo_root,
            task,
            events,
            reply_enabled=reply_enabled,
            command_help=command_help,
        )
        notifications.append((task, payload, fingerprint))

    if args.dry_run:
        preview = {
            "notifications": [payload for _task, payload, _fingerprint in notifications],
            "reply_enabled": reply_enabled,
            "tracked_threads": state.get("threads", {}),
        }
        print(json.dumps(preview, indent=2))
        return 0

    if notifications and not ((args.post_token and args.channel) or args.webhook_url):
        raise mcp_queue.McpError(
            "set SLACK_WEBHOOK_URL or configure SLACK_API_TOKEN/SLACK_POST_TOKEN plus SLACK_CHANNEL_ID"
        )

    for task, payload, fingerprint in notifications:
        task_id = str(task["id"])
        if args.post_token and args.channel:
            response = _post_message(args.post_token, args.channel, payload)
            state["threads"][task_id] = _thread_record(response, fingerprint)
        else:
            _post_to_webhook(args.webhook_url, payload)

    if _is_dm_channel(args.channel):
        acknowledgements = _process_dm_replies(args, client, state)
    else:
        acknowledgements = _process_thread_replies(args, client, state)

    state["notified"] = current
    _save_state(args.state_file, state)
    if notifications:
        print(f"sent {len(notifications)} Slack notification(s)")
    else:
        print("no new notifications")
    if acknowledgements:
        noun = "reply" if len(acknowledgements) == 1 else "replies"
        print(f"processed {len(acknowledgements)} Slack {noun}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-url",
        default=mcp_queue._load_server_url(mcp_queue.DEFAULT_SERVER_NAME),
        help="team_coordination MCP server URL",
    )
    parser.add_argument(
        "--repo-root",
        default=mcp_queue._default_repo_root(),
        help="Git repo root to resolve the MCP workspace",
    )
    parser.add_argument(
        "--state-file",
        help="Path to local notifier state; defaults to a file under the repo git dir",
    )
    parser.add_argument(
        "--statuses",
        nargs="+",
        default=list(DEFAULT_STATUSES),
        choices=["todo", "claimed", "in_progress", "blocked", "review", "done", "canceled"],
        help="Task statuses that should trigger human-attention notifications",
    )
    parser.add_argument("--owner", help="Optional owner filter")
    parser.add_argument("--event-limit", type=int, default=12, help="Initial MCP event fetch size per task")
    parser.add_argument("--webhook-url", help="Slack incoming webhook URL; defaults to SLACK_WEBHOOK_URL")
    parser.add_argument("--api-token", help="Slack Web API token; defaults to SLACK_API_TOKEN")
    parser.add_argument("--post-token", help="Optional token used for posting alerts and acks; defaults to SLACK_POST_TOKEN or --api-token")
    parser.add_argument("--reply-token", help="Optional token used to read thread replies; defaults to SLACK_REPLY_TOKEN or --api-token")
    parser.add_argument("--channel", help="Slack channel ID for Web API posting; defaults to SLACK_CHANNEL_ID")
    parser.add_argument("--dry-run", action="store_true", help="Print payloads instead of sending Slack messages")
    parser.add_argument(
        "--watch",
        type=int,
        default=0,
        help="Poll every N seconds until stopped. Default is one pass.",
    )
    return parser


def _resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    args.state_file = args.state_file or _default_state_file(args.repo_root)
    args.webhook_url = args.webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    args.api_token = args.api_token or os.getenv("SLACK_API_TOKEN")
    args.post_token = args.post_token or os.getenv("SLACK_POST_TOKEN") or args.api_token
    args.reply_token = args.reply_token or os.getenv("SLACK_REPLY_TOKEN") or args.api_token
    args.channel = args.channel or os.getenv("SLACK_CHANNEL_ID")
    return args


def main() -> int:
    parser = build_parser()
    args = _resolve_args(parser.parse_args())

    try:
        if args.watch <= 0:
            return _one_pass(args)
        while True:
            _one_pass(args)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 130
    except (mcp_queue.McpError, SlackReplyError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
