"""broker.server — AF_UNIX newline-JSON protocol server (the daemon).

Wire protocol (versioned envelope, one JSON object per line, UTF-8):

    -> {"v": 1, "cmd": "claim", "serial": "180903EF32" | "role": "flex",
        "consumer": "sdrtrunk", "reason": "p25", "dual_tuner": false,
        "wait_timeout_sec": 30}
    <- {"v": 1, "ok": true,  "cmd": "claim", "granted": {"lease_id": ...,
        "serial": ..., "device_id": ..., "role": ..., "kind": ...}}
    <- {"v": 1, "ok": false, "cmd": "claim", "denied": {"code": ...,
        "reason": ..., "retry_after_sec": ...}}

    -> {"v": 1, "cmd": "release", "lease_id": "..."}
    <- {"v": 1, "ok": true, "cmd": "release", "released": "..."}

    -> {"v": 1, "cmd": "status"}   full ledger snapshot
    -> {"v": 1, "cmd": "list"}     just the lease table

    <- {"v": 1, "ok": false, "error": {"code": ..., "detail": ...}}
       (protocol-level problems: bad JSON, wrong version, unknown cmd)

Crash-safety property (THE design decision — read this twice):
**a lease is bound to the client connection; the socket staying open IS the
lease.**  Each connection may hold at most one lease.  When the connection
closes for ANY reason — clean exit, crash, SIGKILL, OOM — the broker
auto-releases the lease after ``usb_release_grace_sec`` (a beat for the
kernel to finish USB teardown before the next claimant opens the device).
There is no lease that can outlive its owner, no stale-lease reaper, no
third state.  An explicit ``release`` (a clean handoff by a live process)
frees the serial immediately, without the grace pause.

Open-gap serialization: when the ledger answers WAIT (a kind=rspduo grant
inside ``rspduo_open_gap_sec`` of the previous one — concurrent
sdrplay_api_Open calls are what the apiService cannot survive), the
connection's handler thread sleeps and retries, up to the claimant's
``wait_timeout_sec``.  A wait that cannot fit the deadline becomes a
DENIED ``open-gap-timeout`` with ``retry_after_sec`` — never a hang, never
a silent drop.

Threading model: one accept loop + one daemon thread per connection.  The
ledger serializes all state under its own lock, so the server threads never
touch shared state directly.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Callable, Optional

from broker.leases import (
    DENIED,
    DENY_OPEN_GAP_TIMEOUT,
    GRANTED,
    WAIT,
    Lease,
    LeaseLedger,
)
from broker.policy import FleetPolicy

log = logging.getLogger("broker.server")

#: Wire protocol version.  Bump only with a compatibility story.
PROTOCOL_VERSION = 1

#: Default patience for a gap-blocked claim when the claimant does not say.
DEFAULT_WAIT_TIMEOUT_SEC = 30.0

#: A protocol line longer than this is not a request, it is an attack or a
#: bug — the connection is dropped.
MAX_LINE_BYTES = 64 * 1024


class BrokerAlreadyRunning(RuntimeError):
    """Another live broker already owns the socket path.

    Two brokers arbitrating one fleet is worse than none — each would grant
    'exclusive' leases on the same physical serials.  Startup refuses."""


class BrokerServer:
    """The tuner-broker daemon: AF_UNIX listener wrapping a LeaseLedger.

    Usable as a context manager in tests::

        with BrokerServer(policy) as srv:
            ... connect to policy.broker.socket ...

    ``sleep`` is injectable alongside the clocks so tests can fake the
    open-gap and grace waits without real elapsed time.
    """

    def __init__(
        self,
        policy: FleetPolicy,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._policy = policy
        self.ledger = LeaseLedger(policy, monotonic=monotonic, wall=wall)
        self._monotonic = monotonic
        self._sleep = sleep
        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._conns: set = set()
        self._conns_mu = threading.Lock()
        self._stop = threading.Event()
        self._shutdown_mu = threading.Lock()
        self._shut_down = False

    # -- lifecycle -----------------------------------------------------------

    @property
    def socket_path(self) -> str:
        return self._policy.broker.socket

    def start(self) -> None:
        """Bind the socket and start accepting.  Non-blocking (accept loop
        runs on a daemon thread); pair with :meth:`shutdown`.

        The live-broker probe runs BEFORE ledger.startup(): startup honors
        ``clear_locks_at_boot``, and a second broker instance must refuse
        without first vandalizing the incumbent's lock files."""
        path = self.socket_path
        if os.path.exists(path):
            self._refuse_if_live_broker(path)
        self.ledger.startup()
        if os.path.exists(path):
            os.unlink(path)  # dead leftover from a crashed broker
        sock_dir = os.path.dirname(path)
        if sock_dir:
            os.makedirs(sock_dir, exist_ok=True)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(path)
        self._listener.listen(16)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="broker-accept", daemon=True
        )
        self._accept_thread.start()
        log.info("tuner-broker listening on %s", path)

    def _refuse_if_live_broker(self, path: str) -> None:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(path)
        except OSError:
            return  # stale socket file; caller unlinks it
        finally:
            probe.close()
        raise BrokerAlreadyRunning(
            f"a live broker is already accepting on {path}; two brokers would "
            f"both grant 'exclusive' leases — refusing to start"
        )

    def serve_forever(self) -> None:
        """start() + block until :meth:`shutdown` (e.g. from a signal handler)."""
        self.start()
        self._stop.wait()

    def shutdown(self) -> None:
        """Stop accepting, drop every connection (auto-releasing their
        leases), remove the socket.  Idempotent."""
        with self._shutdown_mu:
            if self._shut_down:
                return
            self._shut_down = True
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._conns_mu:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        log.info("tuner-broker shut down (%s)", self.socket_path)

    def __enter__(self) -> "BrokerServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    # -- accept / per-connection ----------------------------------------------

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                break  # listener closed by shutdown()
            with self._conns_mu:
                self._conns.add(conn)
            threading.Thread(
                target=self._handle_conn, args=(conn,), name="broker-conn", daemon=True
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        """One claimant's connection, cradle to grave.

        The ``finally`` block is the crash-safety property in code: if this
        connection ever held a lease that was not explicitly released, its
        death — for ANY reason — auto-releases it after the USB grace."""
        lease: Optional[Lease] = None
        reader = conn.makefile("rb")
        try:
            while True:
                try:
                    line = reader.readline(MAX_LINE_BYTES + 1)
                except OSError:
                    break
                if not line:
                    break  # EOF: peer closed / crashed
                if len(line) > MAX_LINE_BYTES:
                    self._send(conn, self._error("line-too-long", "request line exceeds 64KiB"))
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    self._send(conn, self._error("bad-json", f"request is not valid JSON: {exc}"))
                    break  # stream may be desynced; drop the connection
                resp, lease, keep_open = self._dispatch(msg, lease)
                if not self._send(conn, resp):
                    break
                if not keep_open:
                    break
        finally:
            try:
                reader.close()
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            with self._conns_mu:
                self._conns.discard(conn)
            if lease is not None:
                # THE crash-safety path: the socket died while owning a
                # serial.  Give USB teardown a beat, then free it.
                grace = self._policy.broker.usb_release_grace_sec
                log.info(
                    "connection for lease %s (serial %s, consumer=%r) closed without "
                    "release; auto-releasing after %.2fs grace",
                    lease.lease_id, lease.device.serial, lease.consumer, grace,
                )
                if grace > 0:
                    self._sleep(grace)
                self.ledger.release(lease.lease_id, auto=True)

    # -- dispatch ---------------------------------------------------------------

    def _dispatch(self, msg, lease: Optional[Lease]):
        """Route one request.  Returns (response, lease, keep_open)."""
        if not isinstance(msg, dict):
            return self._error("bad-envelope", "request must be a JSON object"), lease, False
        if msg.get("v") != PROTOCOL_VERSION:
            return (
                self._error(
                    "unsupported-version",
                    f"protocol version {msg.get('v')!r} not supported; this broker speaks v{PROTOCOL_VERSION}",
                ),
                lease,
                False,
            )
        cmd = msg.get("cmd")
        if cmd == "claim":
            if lease is not None:
                return (
                    self._error(
                        "lease-already-held",
                        f"this connection already holds lease {lease.lease_id} "
                        f"(serial {lease.device.serial}); one lease per connection — "
                        f"open another connection for another device",
                    ),
                    lease,
                    True,
                )
            resp, new_lease = self._cmd_claim(msg)
            return resp, new_lease, True
        if cmd == "release":
            return self._cmd_release(msg, lease)
        if cmd == "status":
            return {"v": PROTOCOL_VERSION, "ok": True, "cmd": "status", "status": self.ledger.snapshot()}, lease, True
        if cmd == "list":
            return {"v": PROTOCOL_VERSION, "ok": True, "cmd": "list", "leases": self.ledger.snapshot()["leases"]}, lease, True
        return self._error("unknown-cmd", f"unknown cmd {cmd!r}; known: claim, release, status, list"), lease, True

    def _cmd_claim(self, msg: dict):
        serial = msg.get("serial") or None
        role = msg.get("role") or None
        consumer = msg.get("consumer") if isinstance(msg.get("consumer"), str) else ""
        reason = msg.get("reason") if isinstance(msg.get("reason"), str) else ""
        dual_tuner = bool(msg.get("dual_tuner", False))
        try:
            wait_timeout = float(msg.get("wait_timeout_sec", DEFAULT_WAIT_TIMEOUT_SEC))
        except (TypeError, ValueError):
            wait_timeout = DEFAULT_WAIT_TIMEOUT_SEC
        deadline = self._monotonic() + max(0.0, wait_timeout)

        while True:
            outcome = self.ledger.claim(
                consumer=consumer, reason=reason, serial=serial, role=role, dual_tuner=dual_tuner
            )
            if outcome.kind == GRANTED:
                lease = outcome.lease
                return (
                    {
                        "v": PROTOCOL_VERSION,
                        "ok": True,
                        "cmd": "claim",
                        "granted": {
                            "lease_id": lease.lease_id,
                            "serial": lease.device.serial,
                            "device_id": lease.device.id,
                            "role": lease.device.role,
                            "kind": lease.device.kind,
                        },
                    },
                    lease,
                )
            if outcome.kind == DENIED:
                return (
                    {"v": PROTOCOL_VERSION, "ok": False, "cmd": "claim", "denied": outcome.denial.to_dict()},
                    None,
                )
            # WAIT: rspduo open-gap.  A wait can only grow (each new rspduo
            # grant pushes the gap out), so a wait that cannot fit the
            # deadline is already a certain denial — say so NOW instead of
            # making the claimant sit through a doomed timeout.
            assert outcome.kind == WAIT
            remaining = deadline - self._monotonic()
            if outcome.wait_sec > remaining:
                denial = self.ledger.note_denial(
                    DENY_OPEN_GAP_TIMEOUT,
                    f"rspduo open-gap of {outcome.wait_sec:.2f}s exceeds the claim's "
                    f"remaining wait budget ({max(0.0, remaining):.2f}s of "
                    f"wait_timeout_sec); grants are spaced "
                    f"{self._policy.invariants.rspduo_open_gap_sec:g}s apart because "
                    f"the sdrplay apiService cannot take concurrent opens",
                    consumer=consumer,
                    target=serial or role,
                    retry_after_sec=outcome.wait_sec,
                )
                return (
                    {"v": PROTOCOL_VERSION, "ok": False, "cmd": "claim", "denied": denial.to_dict()},
                    None,
                )
            self._sleep(min(outcome.wait_sec, remaining))

    def _cmd_release(self, msg: dict, lease: Optional[Lease]):
        lease_id = msg.get("lease_id")
        if lease is None:
            return (
                self._error("no-lease-held", "this connection holds no lease to release"),
                None,
                True,
            )
        if lease_id != lease.lease_id:
            # Leases are connection-bound: only the owning connection may
            # release, and only its own lease — anything else is a confused
            # (or malicious) claimant and gets a hard no.
            return (
                self._error(
                    "not-lease-owner",
                    f"lease_id {lease_id!r} does not match this connection's lease "
                    f"{lease.lease_id}",
                ),
                lease,
                True,
            )
        self.ledger.release(lease.lease_id, auto=False)
        return (
            {"v": PROTOCOL_VERSION, "ok": True, "cmd": "release", "released": lease.lease_id},
            None,
            True,
        )

    # -- plumbing -----------------------------------------------------------------

    def _error(self, code: str, detail: str) -> dict:
        return {"v": PROTOCOL_VERSION, "ok": False, "error": {"code": code, "detail": detail}}

    @staticmethod
    def _send(conn: socket.socket, payload: dict) -> bool:
        try:
            conn.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False
