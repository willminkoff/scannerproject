"""sb3.gitdeploy — read-only git introspection of the deploy checkout.

Everything here observes; nothing here fetches, checks out, or mutates. It backs
both `sb3-ctl status` (the deployed_sha / divergent fields) and `sb3-ctl update`
(which decides what to do from these facts). Kept separate from update.py so the
read path can never accidentally acquire a write path.

The deploy root is the checkout `sb3/` itself lives in — resolved from this
file, not from cwd, so `status` reports the code it is ACTUALLY running rather
than whatever directory the operator happened to be standing in. A tool that
reports the SHA of the wrong checkout is a §4.6 liar.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

GIT_TIMEOUT_SEC = 20.0


def deploy_root() -> Path:
    """The checkout this package is running from (…/<root>/sb3/ -> <root>)."""
    return Path(__file__).resolve().parent.parent


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT_SEC):
    """Run a git command in `root`. Returns (rc, stdout, stderr); never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", repr(exc)


def is_git_checkout(root: Optional[Path] = None) -> bool:
    root = root or deploy_root()
    rc, out, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


class DeployState(NamedTuple):
    is_git: bool
    root: str
    sha: Optional[str]            # deployed HEAD, full
    short_sha: Optional[str]
    branch: Optional[str]         # symbolic branch, or None if detached
    dirty: bool                   # uncommitted changes in the sparse tree
    remote_sha: Optional[str]     # tip of origin/<branch>, via ls-remote (network)
    divergent: Optional[bool]     # sha != remote_sha; None if unknown
    error: Optional[str]          # populated when something could not be read

    def summary(self) -> str:
        if not self.is_git:
            return f"NOT a git checkout ({self.root})"
        d = "dirty" if self.dirty else "clean"
        div = ("DIVERGENT" if self.divergent
               else "up-to-date" if self.divergent is False
               else "remote-unknown")
        return (f"{self.short_sha} on {self.branch or 'DETACHED'} ({d}), "
                f"remote {self.remote_sha[:7] if self.remote_sha else '?'} → {div}")


def observe(root: Optional[Path] = None, *, check_remote: bool = True) -> DeployState:
    """One read of the deploy checkout's git state.

    check_remote=False skips the network (ls-remote); status uses it lightly,
    update wants the truth. divergent stays None when the remote is not
    consulted or cannot be reached — an UNKNOWN, never silently "up-to-date"
    (§4.6: could-not-determine must not be spelled the same as ok).
    """
    root = root or deploy_root()
    if not is_git_checkout(root):
        return DeployState(False, str(root), None, None, None, False,
                           None, None, "not a git checkout")

    rc, sha, err = _git(root, "rev-parse", "HEAD")
    if rc != 0:
        return DeployState(True, str(root), None, None, None, False,
                           None, None, f"rev-parse HEAD failed: {err}")

    _, short_sha, _ = _git(root, "rev-parse", "--short=7", "HEAD")
    _, branch, _ = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = None if branch in ("HEAD", "") else branch
    _, porcelain, _ = _git(root, "status", "--porcelain")
    dirty = bool(porcelain)

    remote_sha: Optional[str] = None
    divergent: Optional[bool] = None
    error: Optional[str] = None
    if check_remote and branch:
        rrc, rout, rerr = _git(root, "ls-remote", "origin", branch)
        if rrc == 0 and rout:
            remote_sha = rout.split()[0]
            divergent = (remote_sha != sha)
        else:
            error = f"ls-remote failed: {rerr or 'no output'}"

    return DeployState(True, str(root), sha, short_sha, branch, dirty,
                       remote_sha, divergent, error)
