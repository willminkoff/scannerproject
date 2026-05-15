"""Pytest fixtures for SB3 reliability integration tests.

Phase 1 scope: read-only SSH sampling of Micro state, plus interactive prompts
that ask the human operator to perform UI actions. No service writes, no Micro
mutations from the test process.

Run with: pytest -s -v tests/integration/

Override the SSH host with the MICRO_SSH_HOST env var (default: root@micro).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest


# Sectioned-output sentinel used by the composite SSH state-sampling command.
_SECTION_RE = re.compile(r"^###SECTION:([a-z_0-9]+)###$")

# Default unit list the sampler captures. Order matters only for test
# readability; the result dict is keyed by name.
_SAMPLED_UNITS = [
    "scanner-digital-op25",
    "scanner-digital-op25-audio",
    "scanner-vlc-digital",
    "airband-ui",
    "sdrplay",
    "disco-classifier",
    "disco-interpret",
    "disco-dashboard",
]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: integration test (read-only Micro SSH sampling)",
    )
    config.addinivalue_line(
        "markers",
        "requires_ssh: requires SSH to Micro to be reachable; skipped if not",
    )


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def micro_ssh_host() -> str:
    return os.getenv("MICRO_SSH_HOST", "root@micro").strip() or "root@micro"


@pytest.fixture(scope="session")
def micro_ssh(micro_ssh_host: str) -> Callable[..., subprocess.CompletedProcess]:
    """Returns a callable that runs *cmd* on Micro via ssh and returns the
    CompletedProcess. Read-only by convention — tests must not pass mutating
    commands. ConnectTimeout=10s, BatchMode (no password prompts)."""

    def run(cmd: str, timeout: float = 30.0, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "ssh",
                "-o", "ConnectTimeout=10",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                micro_ssh_host,
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    return run


@pytest.fixture(scope="session", autouse=True)
def _ssh_reachable(request: pytest.FixtureRequest, micro_ssh) -> None:
    """Auto-skip the whole integration suite if SSH to Micro can't reach a
    trivial command. Avoids cascading errors when Micro is offline."""
    if not _any_test_requires_ssh(request):
        return
    try:
        result = micro_ssh("echo ok", timeout=15.0)
    except subprocess.TimeoutExpired:
        pytest.skip("SSH to Micro timed out — integration tests skipped.")
    if result.returncode != 0 or "ok" not in (result.stdout or ""):
        pytest.skip(
            f"SSH to Micro not reachable (rc={result.returncode}): "
            f"{(result.stderr or '').strip()[:200]}"
        )


def _any_test_requires_ssh(request: pytest.FixtureRequest) -> bool:
    """True if any collected test is marked requires_ssh. Lets us skip the SSH
    probe entirely when collecting only non-SSH tests (none today, but
    forward-compatible)."""
    try:
        items = request.session.items
    except AttributeError:
        return True
    for item in items:
        if any(m.name == "requires_ssh" for m in item.iter_markers()):
            return True
    return bool(items)  # if items exist but none marked, still probe to be safe


# ---------------------------------------------------------------------------
# State sampling
# ---------------------------------------------------------------------------


_COMPOSITE_SAMPLER_TEMPLATE = r"""
set +e
echo '###SECTION:ts_epoch###'
date -u +%s
echo '###SECTION:services###'
for u in {units}; do
  printf '%s=%s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null)"
done
echo '###SECTION:classifier_show###'
systemctl show disco-classifier -p MainPID -p ActiveEnterTimestamp 2>/dev/null
echo '###SECTION:systems_json_mtime###'
stat -c %Y /etc/scannerproject/digital/active/systems.json 2>/dev/null
echo '###SECTION:talkgroups_csv_mtime###'
stat -c %Y /etc/scannerproject/digital/active/talkgroups.csv 2>/dev/null
echo '###SECTION:systems_json###'
cat /etc/scannerproject/digital/active/systems.json 2>/dev/null
echo '###SECTION:multi_rx_json###'
cat /run/scannerproject/op25/multi_rx.json 2>/dev/null
echo '###SECTION:op25_log_tail###'
tail -n 30 /var/log/op25/op25.log 2>/dev/null
echo '###SECTION:op25_log_voice_recent_count###'
# Count "voice update" lines in the most recent log tail. Cheap, doesn't
# scan the full multi-hundred-MB log. Tests compare pre vs post: if post
# count > pre count, voice traffic landed during the test window.
tail -n 200 /var/log/op25/op25.log 2>/dev/null | grep -c "voice update"
echo '###SECTION:kill_cascade_60s###'
journalctl --since "60 seconds ago" --no-pager 2>/dev/null \
  | grep -E "sudo.*systemctl.*scanner-digital-op25|sudo.*systemctl.*sdrplay" \
  | grep -v tailscale \
  | wc -l
echo '###SECTION:end###'
"""


def _build_sampler_command() -> str:
    return _COMPOSITE_SAMPLER_TEMPLATE.format(units=" ".join(_SAMPLED_UNITS))


def _parse_sampler_sections(stdout: str) -> Dict[str, list]:
    sections: Dict[str, list] = {}
    current: Optional[str] = None
    for line in stdout.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1)
            sections[current] = []
            continue
        if current is None:
            continue
        sections[current].append(line)
    return sections


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def _parse_state(sections: Dict[str, list]) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_monotonic_ns": time.monotonic_ns(),
    }
    state["micro_ts_epoch"] = _to_int("\n".join(sections.get("ts_epoch", [])))

    services: Dict[str, str] = {}
    for line in sections.get("services", []):
        if "=" in line:
            name, _, value = line.partition("=")
            services[name.strip()] = value.strip()
    state["services"] = services

    classifier: Dict[str, Any] = {"main_pid": 0, "active_enter_timestamp": ""}
    for line in sections.get("classifier_show", []):
        if line.startswith("MainPID="):
            classifier["main_pid"] = _to_int(line.split("=", 1)[1])
        elif line.startswith("ActiveEnterTimestamp="):
            classifier["active_enter_timestamp"] = line.split("=", 1)[1].strip()
    state["classifier"] = classifier

    state["systems_json_mtime_epoch"] = _to_int("\n".join(sections.get("systems_json_mtime", [])))
    state["talkgroups_csv_mtime_epoch"] = _to_int("\n".join(sections.get("talkgroups_csv_mtime", [])))

    systems_text = "\n".join(sections.get("systems_json", [])).strip()
    state["systems"] = []
    if systems_text:
        try:
            payload = json.loads(systems_text)
            state["systems"] = list(payload.get("systems") or [])
        except json.JSONDecodeError:
            state["systems_json_parse_error"] = systems_text[:200]

    multi_rx_text = "\n".join(sections.get("multi_rx_json", [])).strip()
    state["multi_rx"] = None
    if multi_rx_text:
        try:
            state["multi_rx"] = json.loads(multi_rx_text)
        except json.JSONDecodeError:
            state["multi_rx_parse_error"] = multi_rx_text[:200]

    state["op25_log_tail"] = sections.get("op25_log_tail", [])
    state["op25_log_voice_recent_count"] = _to_int(
        "\n".join(sections.get("op25_log_voice_recent_count", []))
    )
    state["kill_cascade_count_60s"] = _to_int(
        "\n".join(sections.get("kill_cascade_60s", []))
    )
    return state


@pytest.fixture
def state_sampler(micro_ssh) -> Callable[[], Dict[str, Any]]:
    """Returns sample() -> dict snapshot of Micro state. One SSH call per
    invocation; safe to call repeatedly inside `wait_for` predicates."""
    cmd = _build_sampler_command()

    def sample() -> Dict[str, Any]:
        try:
            result = micro_ssh(cmd, timeout=30.0)
        except subprocess.TimeoutExpired as e:
            return {
                "ssh_ok": False,
                "ssh_error": f"timeout: {e}",
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        if result.returncode != 0:
            return {
                "ssh_ok": False,
                "ssh_error": (result.stderr or "").strip()[:500],
                "ssh_rc": result.returncode,
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        sections = _parse_sampler_sections(result.stdout)
        state = _parse_state(sections)
        state["ssh_ok"] = True
        return state

    return sample


# ---------------------------------------------------------------------------
# User prompts (interactive)
# ---------------------------------------------------------------------------


@pytest.fixture
def prompt_user() -> Callable[[str], None]:
    """Returns prompt(msg). Prints msg, blocks on stdin until ENTER. Requires
    `pytest -s` so stdin isn't captured. If pytest captured stdin, raises a
    clear error rather than silently hanging."""

    def _prompt(message: str) -> None:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "prompt_user requires an interactive stdin. "
                "Re-run with `pytest -s -v` so stdin is not captured."
            )
        print(f"\n>>> {message}\n>>> Press ENTER when done...", flush=True)
        try:
            input()
        except EOFError as e:
            raise RuntimeError(
                "prompt_user got EOF on stdin. Re-run with `pytest -s -v`."
            ) from e

    return _prompt


# ---------------------------------------------------------------------------
# Polling helper
# ---------------------------------------------------------------------------


@pytest.fixture
def wait_for() -> Callable[..., bool]:
    """Returns wait_for(predicate, timeout=30, interval=2) -> bool. Predicate
    is called repeatedly; returns True the moment it returns truthy. Returns
    False on timeout."""

    def _wait(predicate: Callable[[], Any], timeout: float = 30.0, interval: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if predicate():
                    return True
            except Exception:
                pass  # transient failures are absorbed; surface via assertion later
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    return _wait


# ---------------------------------------------------------------------------
# JSONL result writer
# ---------------------------------------------------------------------------


_RESULTS_DIR = Path(__file__).parent / "results"


@pytest.fixture
def result_writer(request: pytest.FixtureRequest) -> Callable[[Dict[str, Any]], None]:
    """Returns write(record). Appends one JSON line per call to
    tests/integration/results/{test_name}__{ts_utc}.jsonl. The file is
    opened-and-closed per call (cheap, crash-safe — tests can always read
    the partial file even if a later step fails)."""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    test_name = request.node.name
    # Sanitize: pytest test IDs can include '[' / ']' for parametrized tests.
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", test_name)
    started_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = _RESULTS_DIR / f"{safe_name}__{started_ts}.jsonl"

    def _write(record: Dict[str, Any]) -> None:
        record_with_meta = {
            "_test": test_name,
            "_ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **record,
        }
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record_with_meta, default=str) + "\n")

    # Stash path on the writer so tests can reference / print it.
    _write.path = out_path  # type: ignore[attr-defined]
    return _write
