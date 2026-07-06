"""Shared helpers for the tuner-broker test suite (no tests in this file).

Everything here keeps the broker tests headless and hermetic:

- ``make_policy_dict`` / ``make_policy``: a fake v2 fleet policy whose
  sockets/locks/state live in a per-test tmpdir.  Shaped like the real
  ``etc/mac/sdr_fleet_policy.json`` but with two FLEX RTLs (for the
  hot-spare/role tests) and a dual-tuner-capable RSP-A (the real policy
  ships dual_tuner=false until the D1 ladder gate — tests need the
  capability to exercise the 0x6bed limit).
- ``short_socket_dir``: AF_UNIX sun_path maxes out around 104 bytes on
  macOS; pytest tmp_path routinely blows past that.  Sockets go in a short
  mkdtemp under $TMPDIR instead.
- ``FakeClock``: injectable monotonic clock so the open-gap and
  min-restart-interval invariants are tested without real sleeps.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from broker.policy import FleetPolicy, parse_policy  # noqa: E402

RSP_A = "180903EF32"   # dual-tuner capable in the FAKE policy (see docstring)
RSP_B = "1809063632"
RTL_GROUND = "80000003"
RTL_FLEX_1 = "61108285"
RTL_FLEX_2 = "61108286"


def make_policy_dict(
    run_dir: str,
    *,
    gap: float = 0.0,
    min_restart: float = 0.0,
    max_dual: int = 1,
    grace: float = 0.05,
    clear_locks: bool = True,
    rsp_b_dual_capable: bool = False,
    socket_path: str = None,
) -> dict:
    return {
        "version": 2,
        "invariants": {
            "max_concurrent_dual_tuner_rspduo": max_dual,
            "rspduo_open_gap_sec": gap,
            "min_restart_interval_sec": min_restart,
        },
        "devices": [
            {"id": "RSP-A", "kind": "rspduo", "serial": RSP_A,
             "role": "sdrtrunk-p25", "usb_group": "G1", "dual_tuner": True},
            {"id": "RSP-B", "kind": "rspduo", "serial": RSP_B,
             "role": "chirp-airband", "usb_group": "G2",
             "dual_tuner": rsp_b_dual_capable},
            {"id": "RTL-1", "kind": "rtlsdr", "serial": RTL_GROUND,
             "role": "chirp-ground", "usb_group": "G3"},
            {"id": "RTL-4", "kind": "rtlsdr", "serial": RTL_FLEX_1,
             "role": "flex", "usb_group": "G3"},
            {"id": "RTL-5", "kind": "rtlsdr", "serial": RTL_FLEX_2,
             "role": "flex", "usb_group": "G3"},
        ],
        "broker": {
            "socket": socket_path or os.path.join(run_dir, "broker.sock"),
            "lock_dir": os.path.join(run_dir, "locks"),
            "state_file": os.path.join(run_dir, "broker_state.json"),
            "stale_lock_grace_sec": 5,
            "usb_release_grace_sec": grace,
            "clear_locks_at_boot": clear_locks,
        },
    }


def make_policy(run_dir: str, **overrides) -> FleetPolicy:
    return parse_policy(make_policy_dict(run_dir, **overrides), path="<test>")


def short_socket_dir() -> str:
    """mkdtemp short enough for an AF_UNIX socket path (sun_path <= ~104)."""
    d = tempfile.mkdtemp(prefix="tbrk")
    assert len(os.path.join(d, "broker.sock")) < 100, (
        f"tmpdir too long for AF_UNIX: {d}"
    )
    return d


class FakeClock:
    """Injectable monotonic clock: call it for now, .advance() to move it."""

    def __init__(self, start: float = 1000.0):
        self._t = float(start)
        self._mu = threading.Lock()

    def __call__(self) -> float:
        with self._mu:
            return self._t

    def advance(self, dt: float) -> None:
        with self._mu:
            self._t += float(dt)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll ``predicate()`` until truthy or timeout.  Returns the verdict."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
