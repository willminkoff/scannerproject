"""Diagnostic log generation."""
import os
import subprocess
import sys
from typing import Optional

# Handle both relative and absolute imports
try:
    from .config import DIAGNOSTIC_DIR
    from .icecast import fetch_local_icecast_status
except ImportError:
    from ui.config import DIAGNOSTIC_DIR
    from ui.icecast import fetch_local_icecast_status


def run_cmd_capture(cmd):
    """Run a command and capture output."""
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return -1, "", str(e)


def _find_git_root(start_path: str) -> Optional[str]:
    """Find the git repository root."""
    path = os.path.abspath(start_path)
    if os.path.isfile(path):
        path = os.path.dirname(path)
    while True:
        if os.path.isdir(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _commit_diagnostic_log(path: str) -> None:
    """Commit the diagnostic log to git."""
    repo_root = _find_git_root(path)
    if not repo_root:
        raise RuntimeError("Unable to locate git repo for diagnostic log")
    rel_path = os.path.relpath(path, repo_root)
    code, _, err = run_cmd_capture(["git", "-C", repo_root, "add", rel_path])
    if code != 0:
        raise RuntimeError(f"git add failed: {err.strip()}")
    code, out, err = run_cmd_capture(
        ["git", "-C", repo_root, "diff", "--cached", "--name-only", "--", rel_path]
    )
    if code != 0:
        raise RuntimeError(f"git diff failed: {err.strip()}")
    if not out.strip():
        return
    message = f"Add diagnostic log {os.path.basename(path)}"
    code, _, err = run_cmd_capture(["git", "-C", repo_root, "commit", "-m", message])
    if code != 0:
        raise RuntimeError(f"git commit failed: {err.strip()}")


def write_diagnostic_log():
    """Generate and save a diagnostic log."""
    import time
    os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = os.path.join(DIAGNOSTIC_DIR, f"diagnostic-{ts}.txt")

    lines = []
    lines.append(f"SprontPi Diagnostic Log (UTC {ts})")
    lines.append("")
    lines.append("### icecast status-json.xsl (localhost)")
    lines.append(fetch_local_icecast_status())
    lines.append("")

    from config import UNITS
    commands = [
        ["date", "-u"],
        ["uname", "-a"],
        ["uptime"],
        ["systemctl", "status", UNITS["icecast"], "--no-pager"],
        ["systemctl", "status", UNITS["keepalive"], "--no-pager"],
        ["systemctl", "status", UNITS["rtl"], "--no-pager"],
        ["systemctl", "status", "airband-ui", "--no-pager"],
        ["journalctl", "-u", UNITS["icecast"], "-n", "200", "--no-pager"],
        ["journalctl", "-u", UNITS["keepalive"], "-n", "200", "--no-pager"],
        ["journalctl", "-u", UNITS["rtl"], "-n", "200", "--no-pager"],
        ["journalctl", "-u", "airband-ui", "-n", "200", "--no-pager"],
        ["tail", "-n", "200", "/var/log/icecast2/error.log"],
        ["tail", "-n", "200", "/var/log/icecast2/access.log"],
        ["grep", "-n", "fallback-mount", "/etc/icecast2/icecast.xml"],
    ]

    for cmd in commands:
        lines.append(f"### command: {' '.join(cmd)}")
        code, out, err = run_cmd_capture(cmd)
        lines.append(f"(exit={code})")
        if out:
            lines.append("stdout:")
            lines.append(out.rstrip())
        if err:
            lines.append("stderr:")
            lines.append(err.rstrip())
        if not out and not err:
            lines.append("no output")
        lines.append("")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.replace(tmp, path)
    _commit_diagnostic_log(path)
    return path
