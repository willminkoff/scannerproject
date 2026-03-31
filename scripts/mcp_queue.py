#!/usr/bin/env python3
"""Minimal team_coordination MCP helper for local task pickup and review handoff."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-11-25"
DEFAULT_SERVER_NAME = "team_coordination"
DEFAULT_SERVER_URL = "http://127.0.0.1:8765/mcp"
CLIENT_NAME = "scannerproject-mcp-helper"
CLIENT_VERSION = "0.1.0"
ACTIVE_STATUSES = ("claimed", "in_progress", "review", "blocked")


class McpError(RuntimeError):
    """Raised when the MCP server or tool returns an error."""


def _git_root(start: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _default_repo_root() -> str:
    try:
        return _git_root(os.getcwd())
    except subprocess.CalledProcessError:
        return os.getcwd()


def _load_server_url(server_name: str) -> str:
    env_url = os.getenv("TEAM_COORDINATION_MCP_URL") or os.getenv("MCP_SERVER_URL")
    if env_url:
        return env_url

    config_path = Path.home() / ".codex" / "config.toml"
    if config_path.exists():
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
        mcp_servers = data.get("mcp_servers") or {}
        server = mcp_servers.get(server_name) or {}
        url = server.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()

    return DEFAULT_SERVER_URL


def _parse_streamable_http(body: str) -> Any:
    body = body.strip()
    if not body:
        return None
    if body.startswith("event:"):
        data_lines: list[str] = []
        for line in body.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            raise McpError("streamable HTTP response did not include data lines")
        return json.loads("\n".join(data_lines))
    return json.loads(body)


def _coerce_content_text(result: dict[str, Any]) -> Any:
    if result.get("isError"):
        raise McpError(json.dumps(result, indent=2))
    content = result.get("content") or []
    if not content:
        return None
    texts = [item.get("text", "") for item in content if item.get("type") == "text"]
    if not texts:
        return content
    merged = "\n".join(texts).strip()
    if not merged:
        return ""
    try:
        return json.loads(merged)
    except json.JSONDecodeError:
        return merged


def _short_task_id(task_id: str) -> str:
    return str(task_id).split("-", 1)[0]


def _git_commit_subject(repo_root: str, commit_sha: str) -> str:
    proc = subprocess.run(
        ["git", "show", "-s", "--format=%s", commit_sha],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _artifact_spec(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("artifact must be TYPE:PATH")
    artifact_type, path = value.split(":", 1)
    artifact_type = artifact_type.strip()
    path = path.strip()
    if not artifact_type or not path:
        raise argparse.ArgumentTypeError("artifact must be TYPE:PATH")
    return artifact_type, path


class TeamCoordinationClient:
    """Small MCP client for the team_coordination server."""

    def __init__(self, server_url: str):
        self.server_url = server_url
        self.session_id: str | None = None
        self._next_id = 1

    def initialize(self) -> None:
        response, headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_call_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
            },
            include_session=False,
        )
        self.session_id = headers.get("mcp-session-id")
        if not self.session_id:
            raise McpError("initialize response did not include an MCP session ID")
        if "error" in response:
            raise McpError(response["error"].get("message") or str(response["error"]))
        self._notify("notifications/initialized")

    def _next_call_id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload, include_session=True)

    def _post(self, payload: dict[str, Any], *, include_session: bool) -> tuple[Any, dict[str, str]]:
        req = urllib.request.Request(
            self.server_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("content-type", "application/json")
        req.add_header("accept", "application/json, text/event-stream")
        req.add_header("mcp-protocol-version", PROTOCOL_VERSION)
        if include_session:
            if not self.session_id:
                raise McpError("session not initialized")
            req.add_header("mcp-session-id", self.session_id)
        try:
            with urllib.request.urlopen(req) as response:
                body = response.read().decode("utf-8")
                headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise McpError(f"HTTP {exc.code}: {body}") from exc
        return _parse_streamable_http(body), headers

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        response, _headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_call_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            include_session=True,
        )
        if response is None:
            return None
        if "error" in response:
            raise McpError(response["error"].get("message") or str(response["error"]))
        return _coerce_content_text(response.get("result") or {})


def _workspace_for_repo(client: TeamCoordinationClient, repo_root: str) -> dict[str, Any]:
    workspace = client.call_tool("get_workspace", {"repo_root": repo_root})
    if not isinstance(workspace, dict):
        raise McpError(f"workspace not found for repo root {repo_root}")
    return workspace


def _list_tasks(client: TeamCoordinationClient, workspace_id: str, status: str | None = None) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"workspace_id": workspace_id}
    if status:
        args["status"] = status
    result = client.call_tool("list_tasks", args)
    if not isinstance(result, list):
        raise McpError("list_tasks did not return a task list")
    return result


def _get_task(client: TeamCoordinationClient, task_id: str) -> dict[str, Any]:
    result = client.call_tool("get_task", {"task_id": task_id})
    if not isinstance(result, dict):
        raise McpError(f"get_task returned unexpected payload for {task_id}")
    return result


def _get_events(
    client: TeamCoordinationClient,
    *,
    entity_id: str | None = None,
    entity_type: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"limit": limit}
    if entity_id:
        args["entity_id"] = entity_id
    if entity_type:
        args["entity_type"] = entity_type
    result = client.call_tool("get_event_log", args)
    if not isinstance(result, list):
        raise McpError("get_event_log did not return a list")
    return result


def _format_task_line(task: dict[str, Any]) -> str:
    owner = task.get("owner_id") or "-"
    return f"{_short_task_id(task['id'])}  {task['status']:<11} owner={owner:<8}  {task['title']}"


def _render_pickup(tasks: list[dict[str, Any]], owner: str) -> str:
    owned = [task for task in tasks if task.get("owner_id") == owner and task.get("status") in ACTIVE_STATUSES]
    review = [task for task in owned if task.get("status") == "review"]
    active = [task for task in owned if task.get("status") in ("claimed", "in_progress")]
    blocked = [task for task in owned if task.get("status") == "blocked"]

    lines = [f"Owner: {owner}"]
    if review:
        lines.append("Review-ready:")
        lines.extend(f"  {_format_task_line(task)}" for task in review)
    if active:
        lines.append("Active:")
        lines.extend(f"  {_format_task_line(task)}" for task in active)
    if blocked:
        lines.append("Blocked:")
        lines.extend(f"  {_format_task_line(task)}" for task in blocked)
    if len(lines) == 1:
        lines.append("  No active tasks for this owner.")
    return "\n".join(lines)


def _render_review_queue(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "No tasks are currently in review."
    lines = ["Review queue:"]
    for task in sorted(
        tasks,
        key=lambda item: (-int(item.get("priority") or 0), str(item.get("updated_at") or "")),
    ):
        lines.append(f"  {_format_task_line(task)}")
    return "\n".join(lines)


def _render_task(task: dict[str, Any], events: list[dict[str, Any]]) -> str:
    lines = [
        f"Task: {_short_task_id(task['id'])}  {task['title']}",
        f"Status: {task.get('status')}  Owner: {task.get('owner_id') or '-'}  Priority: {task.get('priority')}",
        f"Created: {task.get('created_at')}  Updated: {task.get('updated_at')}",
        "",
        "Description:",
        textwrap.indent(str(task.get("description") or "-"), "  "),
        "",
        "Acceptance Criteria:",
        textwrap.indent(str(task.get("acceptance_criteria") or "-"), "  "),
    ]
    if events:
        lines.extend(["", "Recent MCP Events:"])
        for event in events:
            details = event.get("details") or "{}"
            lines.append(
                f"  [{event.get('timestamp')}] {event.get('entity_type')} {event.get('action')} "
                f"actor={event.get('actor') or '-'} details={details}"
            )
    return "\n".join(lines)


def cmd_pickup(args: argparse.Namespace) -> int:
    client = TeamCoordinationClient(args.server_url)
    client.initialize()
    workspace = _workspace_for_repo(client, args.repo_root)
    tasks = _list_tasks(client, workspace["id"])
    filtered = [task for task in tasks if task.get("owner_id") == args.owner and task.get("status") in ACTIVE_STATUSES]
    if args.json:
        print(json.dumps(filtered, indent=2))
        return 0
    print(_render_pickup(tasks, args.owner))
    return 0


def cmd_review_queue(args: argparse.Namespace) -> int:
    client = TeamCoordinationClient(args.server_url)
    client.initialize()
    workspace = _workspace_for_repo(client, args.repo_root)
    tasks = _list_tasks(client, workspace["id"], status="review")
    if args.owner:
        tasks = [task for task in tasks if task.get("owner_id") == args.owner]
    if args.json:
        print(json.dumps(tasks, indent=2))
        return 0
    print(_render_review_queue(tasks))
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    client = TeamCoordinationClient(args.server_url)
    client.initialize()
    task = _get_task(client, args.task_id)
    events = _get_events(client, entity_id=args.task_id, limit=args.limit)
    if args.json:
        print(json.dumps({"task": task, "events": events}, indent=2))
        return 0
    print(_render_task(task, events))
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    client = TeamCoordinationClient(args.server_url)
    client.initialize()
    workspace = _workspace_for_repo(client, args.repo_root)
    client.call_tool(
        "claim_task",
        {"task_id": args.task_id, "owner_type": "agent", "owner_id": args.owner},
    )
    client.call_tool(
        "set_branch_owner",
        {
            "workspace_id": workspace["id"],
            "task_id": args.task_id,
            "branch_name": args.branch,
            "base_branch": args.base_branch,
            "owner_type": "agent",
            "owner_id": args.owner,
        },
    )
    client.call_tool(
        "append_decision_log",
        {
            "task_id": args.task_id,
            "author": args.owner,
            "summary": f"Reserved branch {args.branch} from base {args.base_branch}.",
        },
    )
    if args.status:
        client.call_tool(
            "update_task_status",
            {"task_id": args.task_id, "status": args.status, "actor": args.owner},
        )
    result = {
        "task_id": args.task_id,
        "owner": args.owner,
        "branch": args.branch,
        "status": args.status or "claimed",
    }
    print(json.dumps(result, indent=2) if args.json else f"Claimed {_short_task_id(args.task_id)} on {args.branch}")
    return 0


def cmd_handoff_review(args: argparse.Namespace) -> int:
    client = TeamCoordinationClient(args.server_url)
    client.initialize()
    workspace = _workspace_for_repo(client, args.repo_root)
    if args.commit:
        message = args.commit_message or _git_commit_subject(args.repo_root, args.commit)
        client.call_tool(
            "record_commit",
            {
                "workspace_id": workspace["id"],
                "task_id": args.task_id,
                "commit_sha": args.commit,
                "message": message,
                "author": args.actor,
            },
        )
    for artifact_type, artifact_path in args.artifact:
        client.call_tool(
            "attach_artifact",
            {
                "task_id": args.task_id,
                "workspace_id": workspace["id"],
                "type": artifact_type,
                "path_or_url": artifact_path,
            },
        )
    client.call_tool(
        "append_decision_log",
        {"task_id": args.task_id, "author": args.actor, "summary": args.summary},
    )
    client.call_tool(
        "update_task_status",
        {"task_id": args.task_id, "status": "review", "actor": args.actor},
    )
    if args.workspace_note or args.commit:
        note_bits = [bit for bit in [args.workspace_note] if bit]
        update_args: dict[str, Any] = {"workspace_id": workspace["id"]}
        if note_bits:
            update_args["notes"] = " ".join(note_bits)
        if args.commit:
            update_args["last_seen_commit"] = args.commit
        client.call_tool("update_workspace_state", update_args)
    result = {
        "task_id": args.task_id,
        "status": "review",
        "actor": args.actor,
        "summary": args.summary,
        "commit": args.commit,
    }
    print(json.dumps(result, indent=2) if args.json else f"Moved {_short_task_id(args.task_id)} to review")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-url",
        default=_load_server_url(DEFAULT_SERVER_NAME),
        help="team_coordination MCP server URL (default: from ~/.codex/config.toml)",
    )
    parser.add_argument(
        "--repo-root",
        default=_default_repo_root(),
        help="Git repo root to resolve the MCP workspace (default: current repo)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    subparsers = parser.add_subparsers(dest="command", required=True)

    pickup = subparsers.add_parser("pickup", help="Show active tasks for an owner")
    pickup.add_argument("--owner", required=True, help="Owner id, for example claude or codex")
    pickup.set_defaults(func=cmd_pickup)

    review_queue = subparsers.add_parser("review-queue", help="Show tasks currently in review")
    review_queue.add_argument("--owner", help="Optional owner filter")
    review_queue.set_defaults(func=cmd_review_queue)

    task = subparsers.add_parser("task", help="Show one task plus recent MCP events")
    task.add_argument("task_id", help="Task UUID")
    task.add_argument("--limit", type=int, default=20, help="Number of recent events to show")
    task.set_defaults(func=cmd_task)

    claim = subparsers.add_parser("claim", help="Claim a task and reserve its branch")
    claim.add_argument("task_id", help="Task UUID")
    claim.add_argument("--owner", required=True, help="Owner id")
    claim.add_argument("--branch", required=True, help="Unique branch name for this task")
    claim.add_argument("--base-branch", default="main", help="Base branch for reservation")
    claim.add_argument(
        "--status",
        choices=["claimed", "in_progress"],
        default="in_progress",
        help="Status to set after claim",
    )
    claim.set_defaults(func=cmd_claim)

    handoff = subparsers.add_parser("handoff-review", help="Record review readiness and move a task to review")
    handoff.add_argument("task_id", help="Task UUID")
    handoff.add_argument("--actor", required=True, help="Actor recording the handoff")
    handoff.add_argument("--summary", required=True, help="Decision-log summary for the review handoff")
    handoff.add_argument("--commit", help="Commit SHA to record before review")
    handoff.add_argument("--commit-message", help="Commit message; defaults to git subject when commit exists locally")
    handoff.add_argument(
        "--artifact",
        action="append",
        type=_artifact_spec,
        default=[],
        metavar="TYPE:PATH",
        help="Artifact to attach before handoff, for example test_result:/tmp/test.log",
    )
    handoff.add_argument(
        "--workspace-note",
        help="Optional workspace state note to publish with the handoff",
    )
    handoff.set_defaults(func=cmd_handoff_review)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        return exc.returncode or 1
    except McpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
