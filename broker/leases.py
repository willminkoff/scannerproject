"""broker.leases — the ownership ledger: invariants, flocks, state file.

This module is the broker's brain.  It holds the authoritative table of
"who owns which physical serial right now" and enforces every fleet
invariant at claim time:

  (a) unknown serial/role      → denied, listing the known devices so the
                                 caller can fix its config without ssh-ing in;
  (b) already-leased serial    → denied, NAMING the holder (consumer + age) —
                                 this denial-with-reason is the product
                                 surface that replaces SB6's silent-empty
                                 exclusion resolver (open P0 #5);
  (c) dual-tuner claims        → denied if the policy says the device is not
                                 dual-tuner, or if granting would exceed
                                 ``max_concurrent_dual_tuner_rspduo`` — two
                                 concurrent dual-tuner RSPduos through one
                                 sdrplay apiService deterministically
                                 segfault it (the 0x6bed crash) and corrupt
                                 live IQ for every consumer;
  (d) kind=rspduo grants       → SERIALIZED: at least ``rspduo_open_gap_sec``
                                 between consecutive rspduo grants (the
                                 apiService does not safely serialize
                                 concurrent sdrplay_api_Open calls, even
                                 across different physical RSPduos).  The
                                 ledger returns a WAIT outcome with the
                                 remaining gap; the server sleeps and
                                 retries (see :mod:`broker.server`);
  (e) re-claim churn           → a claim on a serial within
                                 ``min_restart_interval_sec`` of its last
                                 release is denied with ``retry_after_sec``
                                 (rapid open/close cycles wedge the sdrplay
                                 apiService — the 2026-06 "daemon wedge"
                                 recovery-ritual history).

Crash-safety property (owned by :mod:`broker.server`, stated here too):
**a lease is bound to the claimant's socket connection — the socket staying
open IS the lease.**  The ledger itself never expires anything on a timer;
release happens exactly when the owner releases or its connection closes.
No reaper, no stale-lease third state.

Belt-and-braces: while a lease is active the ledger also holds an
``flock(2)`` on ``<lock_dir>/<serial>.lock``.  A non-broker process that at
least checks the flock convention is excluded even if it never talks to the
broker; conversely a foreign flock on that file denies the claim here
(code ``external-lock``) instead of letting two openers fight over USB.

Observability: every transition (grant / deny / release / auto-release) is
logged at INFO with its reason, and the full ledger is mirrored to the
policy's ``broker.state_file`` via atomic tmp+rename on every transition —
a crash can never leave a half-written state file.

The clock is injected (``monotonic=`` / ``wall=`` constructor params) so
tests can fake time instead of sleeping through the open-gap and
min-restart windows.
"""

from __future__ import annotations

import collections
import fcntl
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from broker.policy import Device, FleetPolicy

log = logging.getLogger("broker.leases")

# ---------------------------------------------------------------------------
# claim outcomes
# ---------------------------------------------------------------------------

#: Outcome kinds.  There are exactly three, and WAIT is *provisional* (the
#: server retries it) — a claim always terminates as GRANTED or DENIED.
GRANTED = "granted"
DENIED = "denied"
WAIT = "wait"

#: Stable machine-readable denial codes (the product surface — consumers
#: branch on these; humans read the ``reason`` next to them).
DENY_BAD_REQUEST = "bad-request"
DENY_UNKNOWN_DEVICE = "unknown-device"
DENY_UNKNOWN_ROLE = "unknown-role"
DENY_ALREADY_LEASED = "already-leased"
DENY_DUAL_TUNER_FORBIDDEN = "dual-tuner-forbidden"
DENY_DUAL_TUNER_LIMIT = "dual-tuner-limit"
DENY_MIN_RESTART_INTERVAL = "min-restart-interval"
DENY_EXTERNAL_LOCK = "external-lock"
DENY_OPEN_GAP_TIMEOUT = "open-gap-timeout"
DENY_ROLE_EXHAUSTED = "role-exhausted"

#: How many denials the state file remembers (ring buffer).
RECENT_DENIALS_KEPT = 50


@dataclass(frozen=True)
class Denial:
    """A structured claim refusal: stable ``code`` + human ``reason``.

    ``retry_after_sec`` is set when the denial has a known horizon
    (min-restart-interval, open-gap-timeout) so callers can back off
    intelligently instead of hammering the broker.
    """

    code: str
    reason: str
    retry_after_sec: Optional[float] = None

    def to_dict(self) -> dict:
        out = {"code": self.code, "reason": self.reason}
        if self.retry_after_sec is not None:
            out["retry_after_sec"] = round(self.retry_after_sec, 3)
        return out


@dataclass
class Lease:
    """One active ownership of one physical serial."""

    lease_id: str
    device: Device
    consumer: str
    reason: str
    dual_tuner: bool
    since_monotonic: float
    since_wall: float
    lock_path: Optional[str] = None
    lock_fd: Optional[int] = None

    def to_dict(self, now_monotonic: float) -> dict:
        return {
            "lease_id": self.lease_id,
            "serial": self.device.serial,
            "device_id": self.device.id,
            "role": self.device.role,
            "kind": self.device.kind,
            "consumer": self.consumer,
            "reason": self.reason,
            "dual_tuner": self.dual_tuner,
            "since": _iso(self.since_wall),
            "age_sec": round(max(0.0, now_monotonic - self.since_monotonic), 3),
        }


@dataclass(frozen=True)
class ClaimOutcome:
    """Result of :meth:`LeaseLedger.claim` — exactly one of three kinds.

    - ``GRANTED``: ``lease`` is set; the caller now owns the serial.
    - ``DENIED``: ``denial`` is set (code + reason + optional retry horizon).
    - ``WAIT``: the rspduo open-gap has not elapsed; retry in ``wait_sec``.
      This is NOT a third state a claim can end in — the server converts an
      expired wait into a DENIED ``open-gap-timeout``.
    """

    kind: str
    lease: Optional[Lease] = None
    denial: Optional[Denial] = None
    wait_sec: float = 0.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------

class LeaseLedger:
    """Authoritative, thread-safe table of physical-serial ownership.

    All mutation happens under one lock; the WAIT-sleep for the rspduo
    open-gap happens OUTSIDE the ledger (in the server) so a waiting claim
    never blocks grants/releases on other serials.  Between a WAIT and the
    retry another claimant may take the gap slot — the invariant is grant
    *spacing*, not FIFO fairness, and that is all the apiService needs.
    """

    def __init__(
        self,
        policy: FleetPolicy,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self._policy = policy
        self._monotonic = monotonic
        self._wall = wall
        self._mu = threading.RLock()
        self._leases: dict = {}          # serial -> Lease
        self._by_id: dict = {}           # lease_id -> Lease
        self._last_release: dict = {}    # serial -> monotonic timestamp
        self._last_rspduo_grant: Optional[float] = None
        self._recent_denials: collections.deque = collections.deque(maxlen=RECENT_DENIALS_KEPT)
        self._counters = {"grants": 0, "denials": 0, "releases": 0, "auto_releases": 0}
        self._started_wall = self._wall()

    # -- lifecycle -----------------------------------------------------------

    def startup(self) -> None:
        """Prepare lock_dir/state_file; honor ``clear_locks_at_boot``.

        Hard-fails (OSError propagates) if the runtime dirs cannot be
        created — a broker that cannot persist state or hold flocks would
        be arbitration theater, and this project does not do theater.
        """
        cfg = self._policy.broker
        os.makedirs(cfg.lock_dir, exist_ok=True)
        state_dir = os.path.dirname(cfg.state_file) or "."
        os.makedirs(state_dir, exist_ok=True)
        if cfg.clear_locks_at_boot:
            # Lock files are only meaningful while a live process flocks
            # them; leftovers from before the boot are noise.  Clearing them
            # also un-confuses humans reading the lock dir after a crash.
            for name in os.listdir(cfg.lock_dir):
                if name.endswith(".lock"):
                    path = os.path.join(cfg.lock_dir, name)
                    try:
                        os.unlink(path)
                        log.info("startup: cleared boot-stale lock file %s", path)
                    except OSError as exc:
                        log.warning("startup: could not clear lock file %s: %s", path, exc)
        self._write_state()
        log.info(
            "ledger up: %d devices under management (%s)",
            len(self._policy.devices),
            self._policy.known_devices_summary(),
        )

    # -- claim ---------------------------------------------------------------

    def claim(
        self,
        *,
        consumer: str,
        reason: str,
        serial: Optional[str] = None,
        role: Optional[str] = None,
        dual_tuner: bool = False,
    ) -> ClaimOutcome:
        """Attempt to claim a device by serial (preferred) or by role.

        Role claims walk the policy's candidates in order and grant the
        first claimable one — this is how hot-spare promotion works (e.g.
        the FLEX RTL stepping in for a dead dedicated dongle): the role's
        primary is already leased or missing, so the next candidate wins.
        """
        if not isinstance(consumer, str) or not consumer.strip():
            return self._deny(
                Denial(DENY_BAD_REQUEST, "claim requires a non-empty 'consumer'"),
                consumer=str(consumer), target=serial or role,
            )
        if not isinstance(reason, str) or not reason.strip():
            # The reason string is what the NEXT denied claimant (and the
            # human debugging at 2am) gets shown.  Refusing empty reasons is
            # what keeps "held by sdrtrunk (reason='p25')" meaningful.
            return self._deny(
                Denial(DENY_BAD_REQUEST, "claim requires a non-empty 'reason'"),
                consumer=consumer, target=serial or role,
            )

        with self._mu:
            if serial:
                dev = self._policy.device_by_serial(serial)
                if dev is None:
                    return self._deny(
                        Denial(
                            DENY_UNKNOWN_DEVICE,
                            f"serial {serial!r} is not in the fleet policy; known devices: "
                            f"{self._policy.known_devices_summary()}",
                        ),
                        consumer=consumer, target=serial,
                    )
                outcome = self._try_grant(dev, consumer, reason, dual_tuner)
                if outcome.kind == DENIED:
                    return self._deny(outcome.denial, consumer=consumer, target=serial)
                return outcome

            if role:
                candidates = self._policy.devices_for_role(role)
                if not candidates:
                    return self._deny(
                        Denial(
                            DENY_UNKNOWN_ROLE,
                            f"role {role!r} is not in the fleet policy; known roles: "
                            f"{', '.join(self._policy.known_roles())}",
                        ),
                        consumer=consumer, target=role,
                    )
                denials = []
                best_wait: Optional[float] = None
                for dev in candidates:
                    outcome = self._try_grant(dev, consumer, reason, dual_tuner)
                    if outcome.kind == GRANTED:
                        return outcome
                    if outcome.kind == WAIT:
                        best_wait = outcome.wait_sec if best_wait is None else min(best_wait, outcome.wait_sec)
                    else:
                        denials.append((dev, outcome.denial))
                if best_wait is not None:
                    # At least one candidate is only gap-blocked: wait for it
                    # rather than denying (the server retries).
                    return ClaimOutcome(kind=WAIT, wait_sec=best_wait)
                if len(denials) == 1:
                    return self._deny(denials[0][1], consumer=consumer, target=role)
                detail = "; ".join(f"{dev.summary()}: [{d.code}] {d.reason}" for dev, d in denials)
                retries = [d.retry_after_sec for _, d in denials if d.retry_after_sec is not None]
                return self._deny(
                    Denial(
                        DENY_ROLE_EXHAUSTED,
                        f"no claimable device for role {role!r}: {detail}",
                        retry_after_sec=min(retries) if retries else None,
                    ),
                    consumer=consumer, target=role,
                )

            return self._deny(
                Denial(DENY_BAD_REQUEST, "claim requires 'serial' or 'role'"),
                consumer=consumer, target=None,
            )

    def _try_grant(self, dev: Device, consumer: str, reason: str, dual_tuner: bool) -> ClaimOutcome:
        """Run the invariant gauntlet for one device.  Caller holds the lock.

        Returns GRANTED (with all side effects done), DENIED (no side
        effects; recording is the caller's job so role claims can aggregate),
        or WAIT (rspduo open-gap not yet elapsed).
        """
        inv = self._policy.invariants
        now = self._monotonic()

        # (b) already leased — name the holder; this line IS the product.
        existing = self._leases.get(dev.serial)
        if existing is not None:
            age = max(0.0, now - existing.since_monotonic)
            return ClaimOutcome(kind=DENIED, denial=Denial(
                DENY_ALREADY_LEASED,
                f"serial {dev.serial} ({dev.id}, role={dev.role}) is held by "
                f"{existing.consumer!r} (reason={existing.reason!r}, age={age:.1f}s, "
                f"lease {existing.lease_id})",
            ))

        # (c) dual-tuner invariants — the 0x6bed segfault class.
        if dual_tuner:
            if dev.kind != "rspduo" or not dev.dual_tuner:
                return ClaimOutcome(kind=DENIED, denial=Denial(
                    DENY_DUAL_TUNER_FORBIDDEN,
                    f"device {dev.id} (serial {dev.serial}, kind={dev.kind}) is not "
                    f"dual_tuner in the fleet policy; flipping it is a policy edit "
                    f"(the D1 ladder gate), not a claim-time option",
                ))
            active_dual = [l for l in self._leases.values() if l.dual_tuner]
            if len(active_dual) + 1 > inv.max_concurrent_dual_tuner_rspduo:
                holders = ", ".join(
                    f"{l.consumer!r} on {l.device.serial}" for l in active_dual
                )
                return ClaimOutcome(kind=DENIED, denial=Denial(
                    DENY_DUAL_TUNER_LIMIT,
                    f"granting a dual-tuner lease on {dev.serial} would put "
                    f"{len(active_dual) + 1} concurrent dual-tuner RSPduos through one "
                    f"sdrplay apiService (policy max "
                    f"{inv.max_concurrent_dual_tuner_rspduo}) — that is the 0x6bed "
                    f"segfault; active dual lease(s): {holders}",
                ))

        # (e) anti-churn — rapid open/close wedges the apiService.
        last_rel = self._last_release.get(dev.serial)
        if last_rel is not None and inv.min_restart_interval_sec > 0:
            elapsed = now - last_rel
            if elapsed < inv.min_restart_interval_sec:
                remaining = inv.min_restart_interval_sec - elapsed
                return ClaimOutcome(kind=DENIED, denial=Denial(
                    DENY_MIN_RESTART_INTERVAL,
                    f"serial {dev.serial} was released {elapsed:.1f}s ago; policy "
                    f"min_restart_interval_sec={inv.min_restart_interval_sec:g} "
                    f"(rapid open/close wedges the sdrplay apiService — 2026-06 "
                    f"daemon-wedge history); retry in {remaining:.1f}s",
                    retry_after_sec=remaining,
                ))

        # (d) rspduo open-gap serialization — apiService cannot take
        # concurrent sdrplay_api_Open calls, even for different hardware.
        if dev.kind == "rspduo" and inv.rspduo_open_gap_sec > 0 and self._last_rspduo_grant is not None:
            since_last = now - self._last_rspduo_grant
            if since_last < inv.rspduo_open_gap_sec:
                return ClaimOutcome(kind=WAIT, wait_sec=inv.rspduo_open_gap_sec - since_last)

        # Belt-and-braces flock vs non-broker processes.
        lock_path, lock_fd, flock_denial = self._acquire_flock(dev, consumer)
        if flock_denial is not None:
            return ClaimOutcome(kind=DENIED, denial=flock_denial)

        lease = Lease(
            lease_id=uuid.uuid4().hex[:12],
            device=dev,
            consumer=consumer,
            reason=reason,
            dual_tuner=dual_tuner,
            since_monotonic=now,
            since_wall=self._wall(),
            lock_path=lock_path,
            lock_fd=lock_fd,
        )
        self._leases[dev.serial] = lease
        self._by_id[lease.lease_id] = lease
        if dev.kind == "rspduo":
            self._last_rspduo_grant = now
        self._counters["grants"] += 1
        log.info(
            "GRANT lease %s: serial %s (%s, role=%s) -> consumer=%r reason=%r dual_tuner=%s",
            lease.lease_id, dev.serial, dev.id, dev.role, consumer, reason, dual_tuner,
        )
        self._write_state()
        return ClaimOutcome(kind=GRANTED, lease=lease)

    # -- release -------------------------------------------------------------

    def release(self, lease_id: str, auto: bool = False) -> bool:
        """Release a lease by id.  ``auto=True`` marks a connection-drop
        auto-release (the crash-safety path) — same mechanics, distinct log
        line and counter so the heartbeat can tell clean exits from crashes.

        Returns False for an unknown lease_id (already released / never
        existed) — releasing twice is not an error, it is idempotence.
        """
        with self._mu:
            lease = self._by_id.pop(lease_id, None)
            if lease is None:
                return False
            self._leases.pop(lease.device.serial, None)
            self._last_release[lease.device.serial] = self._monotonic()
            self._release_flock(lease)
            key = "auto_releases" if auto else "releases"
            self._counters[key] += 1
            log.info(
                "%s lease %s: serial %s freed from consumer=%r (held %.1fs)%s",
                "AUTO-RELEASE" if auto else "RELEASE",
                lease.lease_id, lease.device.serial, lease.consumer,
                max(0.0, self._monotonic() - lease.since_monotonic),
                " — owner connection closed" if auto else "",
            )
            self._write_state()
            return True

    # -- flock (belt-and-braces vs non-broker openers) ------------------------

    def _acquire_flock(self, dev: Device, consumer: str):
        """Take LOCK_EX|LOCK_NB on <lock_dir>/<serial>.lock.

        A foreign holder (some process that bypassed the broker but honors
        the flock convention) turns into an ``external-lock`` denial instead
        of a USB fight.  The file content (pid/consumer JSON) is advisory,
        for humans running ``cat`` — the flock is the actual mutex.
        """
        path = os.path.join(self._policy.broker.lock_dir, f"{dev.serial}.lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            return path, None, Denial(
                DENY_EXTERNAL_LOCK,
                f"cannot open lock file {path}: {exc}",
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return path, None, Denial(
                DENY_EXTERNAL_LOCK,
                f"serial {dev.serial} is flocked by a non-broker process "
                f"(lock file {path}) — some consumer bypassed the broker",
            )
        try:
            os.ftruncate(fd, 0)
            os.write(fd, (json.dumps({
                "pid": os.getpid(), "consumer": consumer, "serial": dev.serial,
                "at": _iso(self._wall()),
            }) + "\n").encode("utf-8"))
        except OSError:
            pass  # advisory content only; the flock itself is held
        return path, fd, None

    def _release_flock(self, lease: Lease) -> None:
        if lease.lock_fd is None:
            return
        try:
            fcntl.flock(lease.lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lease.lock_fd)
        except OSError:
            pass
        # The file itself is left in place (unlinking a lock file while
        # another opener races on the same path can silently split the lock
        # across two inodes); clear_locks_at_boot handles the cosmetics.
        lease.lock_fd = None

    # -- denial recording ------------------------------------------------------

    def note_denial(
        self,
        code: str,
        reason: str,
        *,
        consumer: str,
        target: Optional[str],
        retry_after_sec: Optional[float] = None,
    ) -> Denial:
        """Record + log a denial decided OUTSIDE the ledger (the server uses
        this for ``open-gap-timeout`` when a WAIT outlives the claimant's
        patience).  Returns the Denial for the wire response."""
        return self._deny(
            Denial(code, reason, retry_after_sec=retry_after_sec),
            consumer=consumer, target=target,
        ).denial

    def _deny(self, denial: Denial, *, consumer: Optional[str], target: Optional[str]) -> ClaimOutcome:
        with self._mu:
            self._recent_denials.append({
                "at": _iso(self._wall()),
                "target": target,
                "consumer": consumer,
                "code": denial.code,
                "reason": denial.reason,
                "retry_after_sec": denial.retry_after_sec,
            })
            self._counters["denials"] += 1
            log.info(
                "DENY claim on %r by consumer=%r: [%s] %s",
                target, consumer, denial.code, denial.reason,
            )
            self._write_state()
        return ClaimOutcome(kind=DENIED, denial=denial)

    # -- introspection ---------------------------------------------------------

    def find_lease(self, serial: str) -> Optional[Lease]:
        with self._mu:
            return self._leases.get(serial)

    def snapshot(self) -> dict:
        """Full ledger state — the payload of the ``status`` command and the
        exact document mirrored to the state file."""
        with self._mu:
            now = self._monotonic()
            leases = [l.to_dict(now) for l in self._leases.values()]
            devices = []
            for dev in self._policy.devices:
                held = self._leases.get(dev.serial)
                devices.append({
                    "id": dev.id,
                    "serial": dev.serial,
                    "role": dev.role,
                    "kind": dev.kind,
                    "dual_tuner_capable": dev.dual_tuner,
                    "leased": held is not None,
                    "holder": held.consumer if held else None,
                })
            return {
                "v": 1,
                "written_at": _iso(self._wall()),
                "started_at": _iso(self._started_wall),
                "socket": self._policy.broker.socket,
                "policy_path": self._policy.source_path,
                "devices": devices,
                "leases": leases,
                "recent_denials": list(self._recent_denials),
                "counters": dict(self._counters),
            }

    # -- state file --------------------------------------------------------------

    def _write_state(self) -> None:
        """Mirror the ledger to broker.state_file atomically (tmp+rename).

        ``os.replace`` is atomic on APFS/ext4: readers see either the old
        complete document or the new complete document, never a torn write.
        A runtime write failure is logged at ERROR but does NOT abort the
        transition — arbitration integrity outranks the diagnostics mirror
        (startup validates the directory is writable, so this only fires on
        disk-level trouble).
        """
        path = self._policy.broker.state_file
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.snapshot(), fh, indent=2)
                fh.write("\n")
            os.replace(tmp, path)
        except OSError as exc:
            log.error("state file write failed (%s): %s", path, exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass
