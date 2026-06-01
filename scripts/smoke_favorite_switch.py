#!/usr/bin/env python3
"""Favorite-switch reliability smoke test (Phase R1, STEP 4).

Exercises the full favorite-switch path end-to-end and reports green/red per
check.  The flow:

  1. Record the currently-active favorite tile.
  2. Switch to a different *enabled* favorite tile.
  3. Wait for the backends (rtl-airband + op25) to reconfigure.
  4. Verify:
       - rtl-airband runtime freq set actually changed,
       - op25 active-profile systems.json actually changed,
       - no core service is in a failed state,
       - hits flow on a known-active frequency within a window.
  5. Switch back to the original favorite.
  6. Print a PASS/FAIL summary.

SAFETY
------
Switching favorites BOUNCES rtl-airband and op25 (audio drops for several
seconds while the backends reconfigure).  This script therefore **does not
auto-execute**.  By default it runs read-only: it prints the current state and
the plan, then exits.  To actually perform the switch you must pass
``--execute`` AND type the confirmation phrase at the prompt.

    # read-only (safe, default) — shows current favorite + what it WOULD do:
    python3 scripts/smoke_favorite_switch.py

    # full run — bounces backends; prompts for confirmation first:
    python3 scripts/smoke_favorite_switch.py --execute

Run from the Micro box (talks to the UI on 127.0.0.1:5050 and reads
/run + /etc runtime state directly).  Intended to be run when nobody is
relying on uninterrupted audio — see README note.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# --- configuration knobs (env-overridable) --------------------------------

UI_BASE = os.getenv("SMOKE_UI_BASE", "http://127.0.0.1:5050")
ACTIVE_DIGITAL_DIR = os.getenv(
    "SMOKE_ACTIVE_DIGITAL_DIR", "/etc/scannerproject/digital/active"
)
RTL_RUNTIME_CONFS = [
    os.getenv("SMOKE_RTL_AIRBAND_CONF", "/run/rtl_airband_airband_runtime.conf"),
    os.getenv("SMOKE_RTL_GROUND_CONF", "/run/rtl_airband_ground_runtime.conf"),
]
CORE_UNITS = [
    "airband-ui.service",
    "rtl-airband-airband.service",
    "rtl-airband-ground.service",
    "scanner-digital-op25.service",
    "icecast2.service",
]
# A frequency that has been observed active at the listening site.  Ground
# Control 172.812 was active during the 2026-06-01 audit.  Override per-site.
KNOWN_ACTIVE_FREQ = os.getenv("SMOKE_KNOWN_ACTIVE_FREQ", "172.8120")
RECONFIGURE_WAIT_SEC = float(os.getenv("SMOKE_RECONFIGURE_WAIT_SEC", "25"))
HITS_WAIT_SEC = float(os.getenv("SMOKE_HITS_WAIT_SEC", "60"))
CONFIRM_PHRASE = "SWITCH"

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


# --- low-level helpers -----------------------------------------------------


def _http_get_json(path: str, timeout: float = 10.0) -> dict:
    url = UI_BASE.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_form(path: str, fields: dict, timeout: float = 30.0) -> dict:
    url = UI_BASE.rstrip("/") + path
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "raw": body}


def _read_active_favorite() -> tuple[str, list[str]]:
    """Return (active_label, [enabled_tile_labels])."""
    payload = _http_get_json("/api/hp/state")
    state = payload.get("state") or {}
    active = str(state.get("favorites_name") or "").strip()
    enabled = [
        str(f.get("label") or "").strip()
        for f in (state.get("favorites") or [])
        if isinstance(f, dict) and f.get("enabled") and str(f.get("label") or "").strip()
    ]
    return active, enabled


def _switch_favorite(label: str) -> dict:
    return _http_post_form("/api/hp/state", {"favorites_name": label})


def _rtl_freq_fingerprint() -> str:
    """Stable hash of the rtl-airband runtime freq set across both confs."""
    freqs: list[str] = []
    for path in RTL_RUNTIME_CONFS:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        # rtl_airband config lists channels as `freq = ( 172.812 );` etc.
        freqs.extend(re.findall(r"\b\d{2,4}\.\d{3,6}\b", text))
    digest = hashlib.sha1("|".join(sorted(freqs)).encode("utf-8")).hexdigest()
    return f"{len(freqs)}freqs:{digest[:12]}"


def _op25_systems_fingerprint() -> str:
    """Stable hash of the active op25 profile's systems.json."""
    path = os.path.join(ACTIVE_DIGITAL_DIR, "systems.json")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return "unreadable"
    names = sorted(
        str(s.get("name") or s.get("system") or "").strip()
        for s in (data if isinstance(data, list) else data.get("systems", []))
        if isinstance(s, dict)
    )
    digest = hashlib.sha1(json.dumps(names, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{len(names)}sys:{digest[:12]}"


def _failed_units() -> list[str]:
    failed = []
    for unit in CORE_UNITS:
        out = subprocess.run(
            ["systemctl", "is-failed", unit],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if out == "failed":
            failed.append(unit)
    return failed


def _hit_seen_for_freq(freq: str, since_ts: float) -> bool:
    """True if /api/hits shows an entry matching `freq` newer than since_ts."""
    try:
        payload = _http_get_json("/api/hits")
    except Exception:
        return False
    target = freq.strip()
    target_f = None
    try:
        target_f = round(float(target), 3)
    except ValueError:
        pass
    for item in (payload.get("items") or []):
        if float(item.get("ts") or 0) < since_ts:
            continue
        raw = str(item.get("freq") or "")
        if raw.strip() == target:
            return True
        m = re.search(r"\d+\.\d+", raw)
        if m and target_f is not None and round(float(m.group(0)), 3) == target_f:
            return True
    return False


# --- check harness ---------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        line = f"  [{tag}] {name}"
        if detail:
            line += f"  {DIM}{detail}{RESET}"
        print(line)

    def all_green(self) -> bool:
        return all(ok for _n, ok, _d in self.checks)


def _wait(label: str, seconds: float) -> None:
    print(f"{DIM}… {label} ({seconds:.0f}s){RESET}")
    time.sleep(seconds)


# --- main flow -------------------------------------------------------------


def run(execute: bool, target_label: str | None, known_freq: str) -> int:
    print(f"{DIM}UI base: {UI_BASE}{RESET}")
    active, enabled = _read_active_favorite()
    print(f"Active favorite : {active!r}")
    print(f"Enabled tiles   : {enabled}")

    candidates = [lbl for lbl in enabled if lbl != active]
    if target_label and target_label not in enabled:
        print(f"{RED}Requested target {target_label!r} is not an enabled tile.{RESET}")
        return 2
    target = target_label or (candidates[0] if candidates else None)

    if not target:
        print(f"{RED}No alternate enabled favorite to switch to — cannot run.{RESET}")
        return 2

    print(f"Plan: switch {active!r} → {target!r}, verify, then switch back.")

    if not execute:
        print(
            f"\n{DIM}Read-only mode. Re-run with --execute to perform the switch "
            f"(this bounces rtl-airband + op25 and drops audio briefly).{RESET}"
        )
        return 0

    # Confirmation gate — never auto-execute.
    print(
        f"\n{RED}This will bounce rtl-airband and op25 — audio will drop for a "
        f"few seconds.{RESET}"
    )
    typed = input(f"Type '{CONFIRM_PHRASE}' to proceed (anything else aborts): ").strip()
    if typed != CONFIRM_PHRASE:
        print("Aborted. No changes made.")
        return 1

    report = Report()
    before_rtl = _rtl_freq_fingerprint()
    before_op25 = _op25_systems_fingerprint()
    print(f"{DIM}before: rtl={before_rtl} op25={before_op25}{RESET}")

    # 2) switch
    resp = _switch_favorite(target)
    report.add("switch POST accepted", bool(resp.get("ok")), json.dumps(resp.get("favorites_runtime_sync", {}))[:160])

    # 3) wait for reconfigure
    _wait("waiting for backends to reconfigure", RECONFIGURE_WAIT_SEC)

    # 4) verify
    after_rtl = _rtl_freq_fingerprint()
    after_op25 = _op25_systems_fingerprint()
    report.add("rtl-airband freqs updated", after_rtl != before_rtl, f"{before_rtl} → {after_rtl}")
    report.add("op25 systems updated", after_op25 != before_op25, f"{before_op25} → {after_op25}")

    failed = _failed_units()
    report.add("no service in failed state", not failed, ", ".join(failed) or "all OK")

    # hits flow on a known-active freq
    deadline = time.time() + HITS_WAIT_SEC
    since = time.time() - 5
    saw_hit = False
    while time.time() < deadline:
        if _hit_seen_for_freq(known_freq, since):
            saw_hit = True
            break
        time.sleep(2)
    report.add(f"hits flow on {known_freq}", saw_hit, "seen" if saw_hit else f"none in {HITS_WAIT_SEC:.0f}s")

    # 5) switch back
    print(f"{DIM}restoring original favorite {active!r}…{RESET}")
    back = _switch_favorite(active)
    report.add("restored original favorite", bool(back.get("ok")))
    _wait("waiting for restore to settle", RECONFIGURE_WAIT_SEC)
    report.add(
        "no service failed after restore",
        not _failed_units(),
        ", ".join(_failed_units()) or "all OK",
    )

    print()
    if report.all_green():
        print(f"{GREEN}SMOKE TEST GREEN — favorite switch is reliable.{RESET}")
        return 0
    print(f"{RED}SMOKE TEST RED — see failed checks above.{RESET}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="actually perform the switch (bounces backends). Default: read-only.")
    ap.add_argument("--target", default=None,
                    help="favorite tile label to switch to (default: first other enabled tile).")
    ap.add_argument("--known-freq", default=KNOWN_ACTIVE_FREQ,
                    help=f"known-active freq to verify hits on (default: {KNOWN_ACTIVE_FREQ}).")
    args = ap.parse_args()
    try:
        return run(args.execute, args.target, args.known_freq)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
