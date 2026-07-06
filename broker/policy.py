"""broker.policy — hard-fail loader for the SDR fleet policy (SB7).

The fleet policy (``etc/sdr_fleet_policy.json``, deployed to
``/opt/scannerproject/etc/sdr_fleet_policy.json``) is the single source of
truth for device→role allocation and the tuner-broker invariants.  The
broker, the config generators, and the wiring card all read THIS file.

Design rule: **HARD-FAIL on anything malformed or missing.**  No silent
defaults, no partial parses.  This project's north star is "no third state"
and its history is a museum of silent-fallback bugs — the serial-exclusion
resolver that resolved to an empty set on any read error (SB6 open P0 #5,
exposing chirp's RSPduo to a second opener), the icecast init that silently
fell back to writing a file, the UNITS ghost names.  A broker running on a
guessed policy would be worse than no broker: it would *look* like
arbitration while enforcing nothing.  So: a policy problem raises
:class:`PolicyError` with a structured code + detail, and ``python -m
broker`` exits non-zero (exit code 3, matching chirp's config hard-fail
convention).

The only tolerated absence: ``dual_tuner`` may be omitted on non-rspduo
devices (an rtlsdr physically has one tuner; omission is unambiguous, not a
third state).  It is REQUIRED on every ``kind=rspduo`` entry because that
flag feeds the 0x6bed invariant.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

#: Environment variable naming the policy file (see etc/mac/scannerproject.env).
POLICY_ENV_VAR = "SCANNER_FLEET_POLICY"

#: Deployed location on the Mac mini (repo copy: etc/mac/sdr_fleet_policy.json).
DEFAULT_POLICY_PATH = "/opt/scannerproject/etc/sdr_fleet_policy.json"

#: Policy schema versions this broker knows how to enforce.  v2 = the
#: single-host (M1 mini) rewrite of 2026-07-04.  Anything else hard-fails:
#: enforcing a v1 policy with v2 semantics would be a silent lie.
SUPPORTED_POLICY_VERSIONS = (2,)

#: Device kinds the broker knows the operational semantics of.  An unknown
#: kind would silently skip the rspduo open-gap serialization, so it is a
#: policy error until the broker is taught about it.
KNOWN_DEVICE_KINDS = ("rspduo", "rtlsdr")


class PolicyError(Exception):
    """The fleet policy is missing, unreadable, or malformed.

    Carries a stable machine-readable ``code`` plus a human ``detail`` so
    the daemon can emit a structured diagnostic before its non-zero exit.
    """

    def __init__(self, code: str, detail: str, path: Optional[str] = None):
        self.code = code
        self.detail = detail
        self.path = str(path) if path else None
        msg = f"[{code}] {detail}"
        if self.path:
            msg += f" (policy: {self.path})"
        super().__init__(msg)

    def to_dict(self) -> dict:
        """Structured form for logs / stderr on hard-fail exit."""
        return {
            "error": "fleet-policy",
            "code": self.code,
            "detail": self.detail,
            "path": self.path,
        }


@dataclass(frozen=True)
class Device:
    """One physical SDR from the policy ``devices`` list.

    Addressed by ``serial`` ONLY — USB position is never trusted (fleet
    policy invariant ``address_by: serial``).
    """

    id: str
    kind: str
    serial: str
    role: str
    usb_group: str
    dual_tuner: bool
    label: Optional[str] = None
    consumer: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    def summary(self) -> str:
        return f"{self.serial}({self.role})"


@dataclass(frozen=True)
class Invariants:
    """The broker-enforced fleet invariants (policy ``invariants`` block).

    - ``max_concurrent_dual_tuner_rspduo``: two concurrent dual-tuner
      RSPduos through ONE apiService deterministically segfault it (0x6bed)
      and corrupt live IQ — hence 1 on the single-host deployment.
    - ``rspduo_open_gap_sec``: the apiService does not safely serialize
      concurrent sdrplay_api_Open calls from different processes, even
      across different physical RSPduos — grants on kind=rspduo are spaced.
    - ``min_restart_interval_sec``: rapid open/close cycles wedge the
      apiService (2026-06 "sdrplay daemon wedge" history) — re-claims of a
      just-released serial are denied with a retry horizon.
    """

    max_concurrent_dual_tuner_rspduo: int
    rspduo_open_gap_sec: float
    min_restart_interval_sec: float


@dataclass(frozen=True)
class BrokerConfig:
    """The policy ``broker`` block: sockets, locks, state, grace windows."""

    socket: str
    lock_dir: str
    state_file: str
    stale_lock_grace_sec: float
    usb_release_grace_sec: float
    clear_locks_at_boot: bool
    launchd_label: Optional[str] = None


@dataclass(frozen=True)
class FleetPolicy:
    """Parsed, validated fleet policy."""

    version: int
    devices: tuple  # tuple[Device, ...]
    invariants: Invariants
    broker: BrokerConfig
    source_path: Optional[str] = None

    def device_by_serial(self, serial: str) -> Optional[Device]:
        for dev in self.devices:
            if dev.serial == serial:
                return dev
        return None

    def devices_for_role(self, role: str) -> list:
        """All devices carrying ``role`` (policy order = preference order).

        More than one device may share a role — that is how hot-spare /
        flex arbitration works (e.g. RTL-4).  Callers pick the first
        claimable candidate.
        """
        return [dev for dev in self.devices if dev.role == role]

    def known_devices_summary(self) -> str:
        """Human list of serial(role) pairs — the payload of every
        unknown-device denial, so the caller can fix its config without
        ssh-ing in to read the policy."""
        return ", ".join(dev.summary() for dev in self.devices)

    def known_roles(self) -> list:
        seen = []
        for dev in self.devices:
            if dev.role not in seen:
                seen.append(dev.role)
        return seen


# ---------------------------------------------------------------------------
# validation helpers — every failure is a PolicyError with a stable code
# ---------------------------------------------------------------------------

def _require(mapping: dict, key: str, kinds, ctx: str, path: Optional[str]) -> Any:
    if key not in mapping:
        raise PolicyError("policy-missing-key", f"{ctx}: required key {key!r} is missing", path)
    value = mapping[key]
    if not isinstance(value, kinds):
        names = kinds.__name__ if isinstance(kinds, type) else "/".join(k.__name__ for k in kinds)
        raise PolicyError(
            "policy-bad-type",
            f"{ctx}: key {key!r} must be {names}, got {type(value).__name__} ({value!r})",
            path,
        )
    return value


def _require_str(mapping: dict, key: str, ctx: str, path: Optional[str]) -> str:
    value = _require(mapping, key, str, ctx, path)
    if not value.strip():
        raise PolicyError("policy-empty-value", f"{ctx}: key {key!r} must be a non-empty string", path)
    return value


def _require_number(mapping: dict, key: str, ctx: str, path: Optional[str]) -> float:
    # bool is an int subclass; a bare `True` for a seconds field is a typo,
    # not a duration — reject it.
    value = _require(mapping, key, (int, float), ctx, path)
    if isinstance(value, bool):
        raise PolicyError("policy-bad-type", f"{ctx}: key {key!r} must be a number, got bool", path)
    if value < 0:
        raise PolicyError("policy-bad-value", f"{ctx}: key {key!r} must be >= 0, got {value}", path)
    return float(value)


def _require_bool(mapping: dict, key: str, ctx: str, path: Optional[str]) -> bool:
    value = _require(mapping, key, bool, ctx, path)
    return value


def parse_policy(data: Any, path: Optional[str] = None) -> FleetPolicy:
    """Validate a decoded policy document into a :class:`FleetPolicy`.

    Split from :func:`load_policy` so tests can feed fake policy dicts
    without touching disk.  Raises :class:`PolicyError` on ANY problem.
    """
    if not isinstance(data, dict):
        raise PolicyError("policy-bad-shape", f"top level must be an object, got {type(data).__name__}", path)

    version = _require(data, "version", int, "policy", path)
    if isinstance(version, bool) or version not in SUPPORTED_POLICY_VERSIONS:
        raise PolicyError(
            "policy-version",
            f"policy version {version!r} unsupported; this broker enforces versions {SUPPORTED_POLICY_VERSIONS}",
            path,
        )

    # ---- invariants -------------------------------------------------------
    inv_raw = _require(data, "invariants", dict, "policy", path)
    max_dual = _require(inv_raw, "max_concurrent_dual_tuner_rspduo", int, "invariants", path)
    if isinstance(max_dual, bool) or max_dual < 0:
        raise PolicyError(
            "policy-bad-value",
            f"invariants: max_concurrent_dual_tuner_rspduo must be an int >= 0, got {max_dual!r}",
            path,
        )
    invariants = Invariants(
        max_concurrent_dual_tuner_rspduo=max_dual,
        rspduo_open_gap_sec=_require_number(inv_raw, "rspduo_open_gap_sec", "invariants", path),
        min_restart_interval_sec=_require_number(inv_raw, "min_restart_interval_sec", "invariants", path),
    )

    # ---- devices ----------------------------------------------------------
    devices_raw = _require(data, "devices", list, "policy", path)
    if not devices_raw:
        raise PolicyError("policy-no-devices", "policy devices list is empty — nothing to broker", path)

    devices = []
    seen_serials: dict = {}
    seen_ids: dict = {}
    for idx, entry in enumerate(devices_raw):
        ctx = f"devices[{idx}]"
        if not isinstance(entry, dict):
            raise PolicyError("policy-bad-shape", f"{ctx}: must be an object", path)
        dev_id = _require_str(entry, "id", ctx, path)
        kind = _require_str(entry, "kind", ctx, path)
        if kind not in KNOWN_DEVICE_KINDS:
            raise PolicyError(
                "policy-unknown-kind",
                f"{ctx} ({dev_id}): kind {kind!r} is not one of {KNOWN_DEVICE_KINDS}; "
                "the broker cannot enforce invariants it does not know",
                path,
            )
        serial = _require_str(entry, "serial", ctx, path)
        role = _require_str(entry, "role", ctx, path)
        usb_group = _require_str(entry, "usb_group", ctx, path)

        if kind == "rspduo":
            # dual_tuner feeds the 0x6bed invariant — an omitted flag on an
            # RSPduo is exactly the kind of ambiguity we refuse to guess at.
            dual_tuner = _require_bool(entry, "dual_tuner", f"{ctx} ({dev_id}, kind=rspduo)", path)
        else:
            dual_tuner = entry.get("dual_tuner", False)
            if not isinstance(dual_tuner, bool):
                raise PolicyError("policy-bad-type", f"{ctx}: dual_tuner must be bool if present", path)
            if dual_tuner:
                raise PolicyError(
                    "policy-bad-value",
                    f"{ctx} ({dev_id}): dual_tuner=true on kind={kind} is physically impossible",
                    path,
                )

        if serial in seen_serials:
            # THE collision class this broker exists to kill.  Two policy
            # entries with one serial means two roles believe they own one
            # physical device — refuse to arbitrate a lie.
            raise PolicyError(
                "policy-duplicate-serial",
                f"{ctx} ({dev_id}): serial {serial!r} already declared by device {seen_serials[serial]!r}",
                path,
            )
        if dev_id in seen_ids:
            raise PolicyError("policy-duplicate-id", f"{ctx}: device id {dev_id!r} declared twice", path)
        seen_serials[serial] = dev_id
        seen_ids[dev_id] = serial

        label = entry.get("label")
        consumer = entry.get("consumer")
        devices.append(
            Device(
                id=dev_id,
                kind=kind,
                serial=serial,
                role=role,
                usb_group=usb_group,
                dual_tuner=dual_tuner,
                label=label if isinstance(label, str) else None,
                consumer=consumer if isinstance(consumer, str) else None,
                raw=entry,
            )
        )

    # ---- broker block -----------------------------------------------------
    broker_raw = _require(data, "broker", dict, "policy", path)
    launchd_label = broker_raw.get("launchd_label")
    broker_cfg = BrokerConfig(
        socket=_require_str(broker_raw, "socket", "broker", path),
        lock_dir=_require_str(broker_raw, "lock_dir", "broker", path),
        state_file=_require_str(broker_raw, "state_file", "broker", path),
        stale_lock_grace_sec=_require_number(broker_raw, "stale_lock_grace_sec", "broker", path),
        usb_release_grace_sec=_require_number(broker_raw, "usb_release_grace_sec", "broker", path),
        clear_locks_at_boot=_require_bool(broker_raw, "clear_locks_at_boot", "broker", path),
        launchd_label=launchd_label if isinstance(launchd_label, str) else None,
    )

    return FleetPolicy(
        version=version,
        devices=tuple(devices),
        invariants=invariants,
        broker=broker_cfg,
        source_path=path,
    )


def resolve_policy_path(explicit: Optional[str] = None) -> str:
    """Policy path resolution order: explicit arg > $SCANNER_FLEET_POLICY > default."""
    if explicit:
        return explicit
    env = os.environ.get(POLICY_ENV_VAR, "").strip()
    if env:
        return env
    return DEFAULT_POLICY_PATH


def load_policy(path: Optional[str] = None) -> FleetPolicy:
    """Load + validate the fleet policy from disk.  HARD-FAILS on any problem."""
    resolved = resolve_policy_path(path)
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        raise PolicyError(
            "policy-missing",
            f"fleet policy file not found; set {POLICY_ENV_VAR} or install the policy",
            resolved,
        ) from None
    except OSError as exc:
        raise PolicyError("policy-unreadable", f"cannot read fleet policy: {exc}", resolved) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError("policy-invalid-json", f"fleet policy is not valid JSON: {exc}", resolved) from exc

    return parse_policy(data, resolved)
