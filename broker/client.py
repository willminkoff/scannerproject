"""broker.client — Python API + CLI wrapper for tuner-broker consumers.

Python consumers (chirp daemons, waterfall, WX decoders, disco)::

    from broker.client import claim, BrokerDenied, BrokerUnavailable

    with claim(serial="1809063632", consumer="gr-demod@airband",
               reason="airband ST tuner1") as lease:
        ...open the SDR, run forever...
    # leaving the block releases the lease; so does crashing — the lease
    # lives exactly as long as the socket (see broker.server docstring).

Non-Python consumers (SDRTrunk!) wrap their launch command::

    python -m broker.client run --serial 180903EF32 \
        --consumer sdrtrunk --reason p25 -- \
        /opt/sdrtrunk/bin/sdr-trunk ...

The wrapper claims the serial, exec's the command as a child, holds the
lease (== holds the socket) for the child's whole lifetime, forwards
SIGTERM/SIGINT, propagates the child's exit code, and releases on exit.
If SDRTrunk is SIGKILLed, the wrapper dies with it and the broker's
connection-drop auto-release frees the serial — no stale lease, ever.

``--require`` (default) hard-fails when the broker is unreachable: on the
production box a missing broker means the launchd agent is broken, and
running unbrokered would resurrect the serial-collision class this whole
package exists to kill.  ``--best-effort`` warns and runs anyway — ONLY for
early bring-up before the broker agent is installed.

Exit codes (run subcommand):
    child's code    child ran (0 included); killed-by-signal maps to 128+N
    3               broker denied the claim (mirrors the policy hard-fail
                    convention: refusing to run IS the correct outcome)
    4               broker unreachable under --require
    2               usage error (argparse)

Socket path resolution: explicit argument > ``$SCANNER_BROKER_SOCKET`` >
the fleet-policy default ``/opt/scannerproject/run/broker.sock``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
from typing import Optional

#: Env override for the broker socket (mirrors the fleet policy broker.socket).
SOCKET_ENV_VAR = "SCANNER_BROKER_SOCKET"

#: Deployed socket path per etc/mac/sdr_fleet_policy.json broker.socket.
DEFAULT_SOCKET_PATH = "/opt/scannerproject/run/broker.sock"

PROTOCOL_VERSION = 1

#: Default claim patience (covers the 2s rspduo open-gap with plenty of slack).
DEFAULT_TIMEOUT_SEC = 30.0

#: Extra socket-level allowance beyond the claim's wait budget, so the
#: broker (which may legitimately sit out the open-gap before answering)
#: is never mistaken for dead while it is deliberately pacing grants.
_SOCKET_SLACK_SEC = 10.0

EXIT_DENIED = 3
EXIT_BROKER_UNAVAILABLE = 4


class BrokerDenied(Exception):
    """The broker actively refused the claim (invariant, holder, churn...).

    Carries the structured denial: ``code`` (stable, machine-readable),
    ``reason`` (human, names the holder / invariant / horizon) and
    ``retry_after_sec`` (when the denial has a known expiry).
    """

    def __init__(self, code: str, reason: str, retry_after_sec: Optional[float] = None):
        self.code = code
        self.reason = reason
        self.retry_after_sec = retry_after_sec
        msg = f"[{code}] {reason}"
        if retry_after_sec is not None:
            msg += f" (retry_after_sec={retry_after_sec:g})"
        super().__init__(msg)


class BrokerUnavailable(Exception):
    """The broker could not be reached / spoke garbage — NOT a denial.

    A denial is the broker doing its job; this is the broker absent, which
    on the production box means the launchd agent is down and someone
    should be paged, not worked around."""


def resolve_socket_path(explicit: Optional[str] = None) -> str:
    """explicit arg > $SCANNER_BROKER_SOCKET > deployed default."""
    if explicit:
        return explicit
    env = os.environ.get(SOCKET_ENV_VAR, "").strip()
    if env:
        return env
    return DEFAULT_SOCKET_PATH


# ---------------------------------------------------------------------------
# wire plumbing
# ---------------------------------------------------------------------------

def _connect(path: str, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except OSError as exc:
        sock.close()
        raise BrokerUnavailable(f"broker not reachable at {path}: {exc}") from exc
    return sock


def _send(sock: socket.socket, msg: dict) -> None:
    try:
        sock.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8"))
    except OSError as exc:
        raise BrokerUnavailable(f"send to broker failed: {exc}") from exc


def _read_response(sock: socket.socket) -> dict:
    """Read one newline-terminated JSON response.

    The protocol is strict request/response on this socket, so buffering
    across calls is unnecessary — everything up to the newline is ours."""
    buf = bytearray()
    while b"\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except socket.timeout as exc:
            raise BrokerUnavailable("timed out waiting for broker response") from exc
        except OSError as exc:
            raise BrokerUnavailable(f"read from broker failed: {exc}") from exc
        if not chunk:
            raise BrokerUnavailable("broker closed the connection mid-request")
        buf += chunk
    line = bytes(buf).split(b"\n", 1)[0]
    try:
        resp = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BrokerUnavailable(f"broker sent invalid JSON: {exc}") from exc
    if not isinstance(resp, dict):
        raise BrokerUnavailable(f"broker sent a non-object response: {resp!r}")
    return resp


# ---------------------------------------------------------------------------
# the lease handle
# ---------------------------------------------------------------------------

class Lease:
    """A granted claim.  Holds the socket — the socket IS the lease.

    Context manager: ``__exit__`` releases.  If the process instead dies
    with the lease open, the broker's connection-drop auto-release frees
    the serial (after the USB grace) — crash-safety needs no cooperation
    from this side."""

    def __init__(self, sock: socket.socket, granted: dict, socket_path: str):
        self._sock: Optional[socket.socket] = sock
        self.socket_path = socket_path
        self.lease_id: str = granted["lease_id"]
        self.serial: str = granted["serial"]
        self.device_id: str = granted.get("device_id")
        self.role: str = granted.get("role")
        self.kind: str = granted.get("kind")

    @property
    def released(self) -> bool:
        return self._sock is None

    def release(self) -> None:
        """Clean release: tell the broker (frees the serial immediately,
        skipping the crash-grace), then drop the socket.  Idempotent; wire
        trouble degrades to just closing the socket, which auto-releases."""
        sock = self._sock
        if sock is None:
            return
        self._sock = None
        try:
            sock.settimeout(5.0)
            _send(sock, {"v": PROTOCOL_VERSION, "cmd": "release", "lease_id": self.lease_id})
            _read_response(sock)  # ack; content irrelevant, closing anyway
        except BrokerUnavailable:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __repr__(self) -> str:
        state = "released" if self.released else "held"
        return f"<Lease {self.lease_id} serial={self.serial} {state}>"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def claim(
    *,
    consumer: str,
    reason: str,
    serial: Optional[str] = None,
    role: Optional[str] = None,
    dual_tuner: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    socket_path: Optional[str] = None,
) -> Lease:
    """Claim a device by serial (preferred) or role.  Returns a live Lease.

    ``timeout`` is the claim's patience with the rspduo open-gap
    serialization (it becomes ``wait_timeout_sec`` on the wire) — NOT a
    retry-on-denial loop.  Denials raise :class:`BrokerDenied` immediately;
    the ``retry_after_sec`` attribute says when a retry could succeed.

    Raises :class:`BrokerUnavailable` when the broker cannot be reached.
    """
    path = resolve_socket_path(socket_path)
    sock = _connect(path, max(0.0, timeout) + _SOCKET_SLACK_SEC)
    try:
        _send(sock, {
            "v": PROTOCOL_VERSION,
            "cmd": "claim",
            "serial": serial,
            "role": role,
            "consumer": consumer,
            "reason": reason,
            "dual_tuner": bool(dual_tuner),
            "wait_timeout_sec": max(0.0, timeout),
        })
        resp = _read_response(sock)
    except BaseException:
        sock.close()
        raise
    if resp.get("ok") and isinstance(resp.get("granted"), dict):
        sock.settimeout(None)  # the socket now just sits, holding the lease
        return Lease(sock, resp["granted"], path)
    sock.close()
    denied = resp.get("denied")
    if isinstance(denied, dict):
        raise BrokerDenied(
            denied.get("code", "unknown"),
            denied.get("reason", "(no reason given)"),
            denied.get("retry_after_sec"),
        )
    raise BrokerUnavailable(f"broker protocol error on claim: {resp!r}")


def _oneshot(cmd: str, socket_path: Optional[str], timeout: float = 5.0) -> dict:
    sock = _connect(resolve_socket_path(socket_path), timeout)
    try:
        _send(sock, {"v": PROTOCOL_VERSION, "cmd": cmd})
        return _read_response(sock)
    finally:
        sock.close()


def status(socket_path: Optional[str] = None, timeout: float = 5.0) -> dict:
    """Full broker snapshot (devices, leases, recent denials, counters)."""
    resp = _oneshot("status", socket_path, timeout)
    if resp.get("ok") and isinstance(resp.get("status"), dict):
        return resp["status"]
    raise BrokerUnavailable(f"broker protocol error on status: {resp!r}")


def list_leases(socket_path: Optional[str] = None, timeout: float = 5.0) -> list:
    """Just the active-lease table."""
    resp = _oneshot("list", socket_path, timeout)
    if resp.get("ok") and isinstance(resp.get("leases"), list):
        return resp["leases"]
    raise BrokerUnavailable(f"broker protocol error on list: {resp!r}")


# ---------------------------------------------------------------------------
# CLI — the non-Python consumer path (SDRTrunk launcher, shell scripts)
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m broker.client",
        description="tuner-broker client: claim an SDR serial around a command, or inspect the broker",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    run_p = sub.add_parser(
        "run",
        help="claim a device, run a command holding the lease, release on exit",
        description=(
            "Claims the device, runs COMMAND as a child while holding the lease "
            "(the open broker socket IS the lease), forwards SIGTERM/SIGINT, "
            "propagates the child's exit code, releases on exit. If this wrapper "
            "is killed outright, the broker auto-releases on connection drop."
        ),
    )
    run_p.add_argument("--serial", help="physical serial to claim (preferred)")
    run_p.add_argument("--role", help="fleet-policy role to claim (first claimable candidate wins)")
    run_p.add_argument("--consumer", required=True, help="who is claiming (e.g. sdrtrunk)")
    run_p.add_argument("--reason", required=True, help="why (shows up in every denial naming this holder)")
    run_p.add_argument("--dual-tuner", action="store_true", help="claim the device in dual-tuner mode (0x6bed-guarded)")
    run_p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC,
                       help="seconds to wait out the rspduo open-gap (default %(default)s)")
    run_p.add_argument("--socket", default=None, help=f"broker socket (default ${SOCKET_ENV_VAR} or {DEFAULT_SOCKET_PATH})")
    mode = run_p.add_mutually_exclusive_group()
    mode.add_argument("--require", dest="require", action="store_true", default=True,
                      help="hard-fail if the broker is unreachable (default)")
    mode.add_argument("--best-effort", dest="require", action="store_false",
                      help="broker unreachable -> warn and run UNBROKERED (early bring-up only)")
    run_p.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND [ARG...]",
                       help="the command to run under the lease")

    status_p = sub.add_parser("status", help="print the broker's full status as JSON")
    status_p.add_argument("--socket", default=None)
    return parser


def _cli_run(args) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("broker.client run: no command given (put it after `--`)", file=sys.stderr)
        return 2
    if not args.serial and not args.role:
        print("broker.client run: need --serial or --role", file=sys.stderr)
        return 2

    # Install the signal forwarders BEFORE claiming: launchd's unload
    # SIGTERM can land at any point, and until these are in place the
    # default action would kill this wrapper mid-setup — orphaning a
    # just-spawned child outside its lease.  Signals that arrive before
    # the child exists are remembered and settled after spawn (or turn
    # into a clean no-spawn exit).
    pending_signals: list = []
    child_box: list = []

    def _forward(signum, _frame):
        if child_box:
            try:
                child_box[0].send_signal(signum)
            except (ProcessLookupError, OSError):
                pass
        else:
            pending_signals.append(signum)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    lease: Optional[Lease] = None
    try:
        lease = claim(
            consumer=args.consumer,
            reason=args.reason,
            serial=args.serial,
            role=args.role,
            dual_tuner=args.dual_tuner,
            timeout=args.timeout,
            socket_path=args.socket,
        )
        print(
            f"broker.client: lease {lease.lease_id} granted on serial {lease.serial} "
            f"({lease.device_id}, role={lease.role}) to {args.consumer!r}",
            file=sys.stderr,
        )
    except BrokerDenied as exc:
        # An active refusal — respecting it IS the product working.  Even
        # --best-effort obeys a denial; best-effort only covers broker-DOWN.
        print(f"broker.client: claim DENIED: {exc}", file=sys.stderr)
        return EXIT_DENIED
    except BrokerUnavailable as exc:
        if args.require:
            print(
                f"broker.client: broker unavailable and --require in effect: {exc}",
                file=sys.stderr,
            )
            return EXIT_BROKER_UNAVAILABLE
        print(
            f"broker.client: WARNING: broker unavailable ({exc}); running UNBROKERED "
            f"under --best-effort — serial-collision protection is OFF",
            file=sys.stderr,
        )

    try:
        if pending_signals:
            # Told to stop before the child ever existed: don't spawn it.
            signum = pending_signals[0]
            print(
                f"broker.client: received signal {signum} before spawning the "
                f"command; exiting without running it", file=sys.stderr,
            )
            return 128 + signum
        try:
            child = subprocess.Popen(command)
        except OSError as exc:
            print(f"broker.client: cannot exec {command[0]!r}: {exc}", file=sys.stderr)
            return 127
        child_box.append(child)
        for signum in pending_signals:  # landed during the spawn window
            _forward(signum, None)
        rc = child.wait()
        if rc < 0:
            rc = 128 - rc  # killed by signal N -> conventional 128+N
        return rc
    finally:
        if lease is not None:
            lease.release()
            print(f"broker.client: lease {lease.lease_id} released", file=sys.stderr)


def _cli_status(args) -> int:
    try:
        snapshot = status(socket_path=args.socket)
    except BrokerUnavailable as exc:
        print(f"broker.client: {exc}", file=sys.stderr)
        return EXIT_BROKER_UNAVAILABLE
    json.dump(snapshot, sys.stdout, indent=2)
    print()
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.subcommand == "run":
        return _cli_run(args)
    if args.subcommand == "status":
        return _cli_status(args)
    return 2  # unreachable: subparsers are required


if __name__ == "__main__":
    sys.exit(main())
