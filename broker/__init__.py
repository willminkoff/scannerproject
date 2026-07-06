"""broker — single-host SDR tuner-broker (SB7.2 workstream D, the SB7 keystone).

Every consumer of a physical SDR (chirp daemons, the SDRTrunk launcher
wrapper, the waterfall, WX decoders, disco) must CLAIM the device's physical
serial through this broker before opening it.  The broker exists to kill
three failure classes by construction:

  1. **Serial collisions** — two daemons opening one dongle produces dead IQ
     for both (the 2026-05-25 LIBUSB_BUSY incident, and the whole class the
     old silent-empty exclusion resolver failed to prevent).  The broker is
     the single arbiter: one lease per physical serial, ever.
  2. **The 0x6bed apiService segfault** — two concurrent dual-tuner RSPduos
     through one sdrplay_apiService deterministically segfault it and corrupt
     live IQ for every consumer.  The broker REFUSES any claim that would
     exceed ``max_concurrent_dual_tuner_rspduo`` (policy: 1).
  3. **Open/restart churn wedges** — the apiService does not safely serialize
     concurrent ``sdrplay_api_Open`` calls (even across different physical
     RSPduos), and rapid open/close cycles wedge it (2026-06 history: the
     "sdrplay daemon wedge" recovery ritual).  The broker serializes RSPduo
     grants with ``rspduo_open_gap_sec`` between them and rate-limits
     re-claims with ``min_restart_interval_sec``.

Crash-safety property (read this twice)
---------------------------------------
**A lease is bound to the client's socket connection: the socket staying
open IS the lease.**  If a claimant crashes, is SIGKILLed, or simply exits,
its socket closes and the broker auto-releases the lease (after
``usb_release_grace_sec``).  There is no lease that can outlive its owner,
and therefore no stale-lease "third state" that needs a reaper cron.

Denials are the product surface: every denial carries a structured
machine-readable code and a human reason naming the holder / the invariant /
the retry horizon.  This replaces the old silent-empty exclusion resolver
(SB6 open P0 #5) — the exact "useful liar" the coding standards banned.

Package map:
    broker.policy   — hard-fail loader for etc/sdr_fleet_policy.json
    broker.leases   — the ownership ledger: invariants, flocks, state file
    broker.server   — AF_UNIX newline-JSON protocol server (the daemon)
    broker.client   — Python API + CLI wrapper for non-Python consumers
    broker.__main__ — daemon entrypoint (``python -m broker``)
"""

from __future__ import annotations

import importlib

from broker.policy import FleetPolicy, PolicyError, load_policy, parse_policy

#: Lazy (PEP 562) re-exports.  Eagerly importing broker.client here makes
#: ``python -m broker.client`` (the SDRTrunk wrapper invocation) emit a
#: runpy RuntimeWarning on every launch ("found in sys.modules after import
#: of package 'broker'") — so the client/server symbols resolve on first
#: attribute access instead.  ``from broker import claim`` works unchanged.
_LAZY_EXPORTS = {
    "BrokerDenied": "broker.client",
    "BrokerUnavailable": "broker.client",
    "claim": "broker.client",
    "status": "broker.client",
    "BrokerServer": "broker.server",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | set(__all__))


__all__ = [
    "BrokerDenied",
    "BrokerServer",
    "BrokerUnavailable",
    "FleetPolicy",
    "PolicyError",
    "claim",
    "load_policy",
    "parse_policy",
    "status",
]
