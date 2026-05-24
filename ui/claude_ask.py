"""Claude Code subprocess wrapper for the SB3 "Ask Claude" panel.

Invokes the Claude Code CLI (`claude -p`) as a subprocess to answer
operator questions about scanner state.  Maintains per-session
conversation history in-memory so follow-up questions ("why?", "what
about X?") carry context.  Embeds a snapshot of /api/status into every
turn so Claude can answer "what's up with X" without the operator
having to describe state.

Invariants
----------
- The CLI is at ``CLAUDE_BIN`` (default ``/usr/local/bin/claude``).
- Auth lives in ``$HOME/.claude/`` for the airband-ui service user
  (typically ``ubuntu``).  We do **not** pass ``ANTHROPIC_API_KEY`` —
  the operator chose interactive login.
- Each invocation embeds the full conversation history + a fresh
  status snapshot.  Token usage grows linearly with turns; we cap at
  ``CLAUDE_ASK_MAX_HISTORY`` turns (default 10).
- Subprocesses are bounded by ``CLAUDE_TIMEOUT_SEC`` (default 180s).
- Session storage is in-process memory; restart of airband-ui wipes
  history.  That is acceptable for the SB3 use case.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "/usr/local/bin/claude")
CLAUDE_TIMEOUT_SEC = int(os.getenv("CLAUDE_TIMEOUT_SEC", "180"))
MAX_HISTORY_TURNS = int(os.getenv("CLAUDE_ASK_MAX_HISTORY", "10"))
STATUS_SNAPSHOT_MAX_CHARS = int(os.getenv("CLAUDE_ASK_STATUS_MAX_CHARS", "10000"))
PROJECT_DIR = os.getenv(
    "CLAUDE_ASK_PROJECT_DIR",
    "/home/ubuntu/scannerproject",
)
HTTP_HOST_FOR_CONTEXT = os.getenv("CLAUDE_ASK_HTTP_HOST", "127.0.0.1")
HTTP_PORT_FOR_CONTEXT = int(os.getenv("CLAUDE_ASK_HTTP_PORT", "5050"))

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}


def _new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def _fetch_status_snapshot() -> str:
    """Pull a fresh /api/status snapshot for prompt context.

    Returns a markdown-formatted block ready to embed.  On failure
    returns an error annotation rather than raising — Claude can still
    answer general questions without state.
    """
    try:
        import urllib.request

        url = (
            f"http://{HTTP_HOST_FOR_CONTEXT}:{HTTP_PORT_FOR_CONTEXT}/api/status"
        )
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            payload = resp.read().decode("utf-8", errors="replace")
        # Pretty-print but bound size so we don't blow out the prompt.
        try:
            parsed = json.loads(payload)
            text = json.dumps(parsed, indent=2, default=str)
        except Exception:
            text = payload
        truncated = text[:STATUS_SNAPSHOT_MAX_CHARS]
        suffix = "" if len(text) <= STATUS_SNAPSHOT_MAX_CHARS else (
            f"\n... [truncated, {len(text) - STATUS_SNAPSHOT_MAX_CHARS} more chars]"
        )
        return (
            "# Current /api/status snapshot\n"
            "```json\n"
            f"{truncated}{suffix}\n"
            "```"
        )
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("claude_ask: status fetch failed: %s", exc)
        return f"# Current /api/status snapshot unavailable: {exc}"


def _build_prompt(
    question: str,
    history: list[dict[str, str]],
    *,
    include_status: bool = True,
) -> str:
    sections: list[str] = []
    sections.append(
        "You are an in-system assistant for the SB3 scanner running on"
        " Ubuntu (host: Micro).  You have access to Bash, Read, Edit,"
        " and Write tools.  Use them when needed to diagnose or fix."
        "\n\nGuidelines:\n"
        "- Be concise.  The operator is reading on a small screen.\n"
        "- Prefer reading files and running queries (curl"
        " localhost:5050/api/..., journalctl -u ..., grep /var/log/op25/..."
        ") over speculating.\n"
        "- If a fix is appropriate, describe it clearly BEFORE applying."
        "\n- The project repo lives at /home/ubuntu/scannerproject."
        "  Refer to ui/, profiles/, scripts/ when relevant.\n"
        "- Do not commit to git or push.  The operator handles that."
    )
    if include_status:
        sections.append(_fetch_status_snapshot())
    if history:
        history_lines = ["# Conversation history (most recent last)"]
        for turn in history[-MAX_HISTORY_TURNS:]:
            history_lines.append(
                f"\n## Operator asked\n{turn.get('question', '').strip()}"
            )
            history_lines.append(
                f"\n## You answered\n{turn.get('answer', '').strip()}"
            )
        sections.append("\n".join(history_lines))
    sections.append(f"# Current operator question\n{question.strip()}")
    return "\n\n".join(sections)


def _run_claude_subprocess(prompt: str) -> tuple[bool, str]:
    """Run ``claude -p`` with the prompt over stdin.

    Returns ``(ok, text)``.  On non-zero exit returns ``(False, stderr)``.
    """
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "text",
        "--permission-mode",
        "bypassPermissions",
    ]
    env = os.environ.copy()
    # Make sure HOME points at the airband-ui user so claude finds its
    # ~/.claude/ auth tokens.  systemd unit sets User=ubuntu so $HOME
    # is correct in the normal path, but be defensive.
    env.setdefault("HOME", "/home/ubuntu")
    try:
        result = subprocess.run(  # noqa: S603 - validated cmd
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
            env=env,
            cwd=PROJECT_DIR,
        )
    except FileNotFoundError:
        return False, (
            f"claude CLI not found at {CLAUDE_BIN}; install with "
            "'sudo npm install -g @anthropic-ai/claude-code' "
            "and run 'claude /login' as the airband-ui user."
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"Claude exceeded {CLAUDE_TIMEOUT_SEC}s timeout.  "
            "Either the question is too complex or the CLI hung."
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("claude_ask: subprocess failed")
        return False, f"claude subprocess error: {exc}"
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-1500:]
        return False, stderr_tail or f"claude exited with code {result.returncode}"
    return True, (result.stdout or "").strip()


def ask(
    question: str,
    *,
    session_id: str | None = None,
    include_status: bool = True,
) -> dict[str, Any]:
    """Ask Claude a question, returning a structured response.

    Returns
    -------
    dict with keys:
      - ``ok``: bool
      - ``session_id``: str (new or existing)
      - ``answer``: str (only on ok=True)
      - ``error``: str (only on ok=False)
      - ``turn_count``: int (history length after this turn)
      - ``elapsed_ms``: int
    """
    question = str(question or "").strip()
    if not question:
        return {
            "ok": False,
            "session_id": session_id or "",
            "error": "empty question",
        }

    started_at = time.time()
    with _lock:
        if session_id and session_id in _sessions:
            session = _sessions[session_id]
        else:
            session_id = _new_session_id()
            session = {"turns": [], "created_at": started_at}
            _sessions[session_id] = session
        history_snapshot = list(session["turns"])

    prompt = _build_prompt(question, history_snapshot, include_status=include_status)
    ok, text = _run_claude_subprocess(prompt)
    elapsed_ms = int((time.time() - started_at) * 1000)

    if not ok:
        return {
            "ok": False,
            "session_id": session_id,
            "error": text,
            "elapsed_ms": elapsed_ms,
        }

    with _lock:
        session = _sessions.get(session_id)
        if session is None:
            # Reset between snapshot and write — recreate to be safe.
            session = {"turns": [], "created_at": started_at}
            _sessions[session_id] = session
        session["turns"].append(
            {
                "question": question,
                "answer": text,
                "ts": time.time(),
                "elapsed_ms": elapsed_ms,
            }
        )
        turn_count = len(session["turns"])

    return {
        "ok": True,
        "session_id": session_id,
        "answer": text,
        "turn_count": turn_count,
        "elapsed_ms": elapsed_ms,
    }


def reset_session(session_id: str) -> bool:
    """Drop a session's history.  Returns True if the session existed."""
    with _lock:
        return _sessions.pop(session_id, None) is not None


def list_sessions() -> list[dict[str, Any]]:
    """Return summary of active sessions (debugging)."""
    with _lock:
        out: list[dict[str, Any]] = []
        for sid, session in _sessions.items():
            out.append(
                {
                    "session_id": sid,
                    "turns": len(session.get("turns") or []),
                    "created_at": session.get("created_at"),
                    "last_turn_ts": (
                        (session["turns"][-1].get("ts") if session.get("turns") else None)
                    ),
                }
            )
        return out
