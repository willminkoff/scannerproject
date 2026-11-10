"""chirp.cmd.server — UDP JSON command server (asyncio, loopback only).

Phase 1. Binds 127.0.0.1:<port> (default 7400 airband / 7401 ground). One
datagram in, one datagram out, on the same socket. JSON-only wire format.

A separate "events" path pushes async events (`hit_start`, `hit_end`, `level`,
`health`, `warn`) to an env-configured listener (host:port). If no listener
is configured, events are emitted as structured JSON lines to stdout — the
Phase-1 prompt's required minimum.

Threading model:
    Daemon main thread builds the flowgraph (which runs in its own GR threads).
    CommandServer.start() spins up the asyncio event loop in a dedicated
    background thread. Command callbacks ARE invoked on that asyncio thread.
    Dispatch targets (the flowgraph's hot setters) must be thread-safe — the
    GR hier_block setters all are.

See SDR_DEMOD_DESIGN_2026-06-03.md Section 5.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from pydantic import ValidationError

from chirp.cmd.schema import (
    COMMAND_ARGS,
    Envelope,
    Event,
    PROTOCOL_VERSION,
    Response,
    parse_args,
    parse_envelope,
)

log = logging.getLogger("chirp.cmd.server")


# ---------------------------------------------------------------------------
# Caller attribution
# ---------------------------------------------------------------------------
#
# This socket binds whatever CHIRP_CMD_HOST says, and on scannerbox-1 that is
# 0.0.0.0 -- so any host able to route to port 7400 can retune the radio, with
# no authentication and, until this, no record of who did.
#
# That gap made one failure undiagnosable. Airband kept being wiped from a
# 10-channel scan pool down to a single VFO channel at 119.35 MHz, three times
# in a day, always the same signature: a gain change, then ten remove_channel,
# then one add_channel. sb3-ui's request log held no matching request -- which
# looked like evidence and was not, because a client speaking straight to this
# port never touches sb3-ui, so its absence from that log is what you would
# expect whether or not it was the culprit. These lines name the sender
# whichever path it takes.
#
# get_status, subscribe and unsubscribe are excluded: the UI polls get_status
# several times a second, and logging it would bury the commands that matter.
audit = logging.getLogger("chirp.cmd.audit")
# Pinned to INFO so attribution survives someone quieting the rest of the
# daemon: during propagation Python consults ancestor *handlers*, never
# ancestor logger levels, so a root-level change cannot suppress these.
audit.setLevel(logging.INFO)

READ_ONLY_COMMANDS = frozenset({"get_status", "subscribe", "unsubscribe"})

# Args worth seeing in an audit line. A whitelist, so a field added to a
# command's schema later can never silently start being logged.
_AUDIT_ARG_FIELDS = ("id", "freq_mhz", "mhz", "mode", "db", "dbfs",
                     "threshold", "bypass")
# add_channel carries a list; show the first few so a wipe-and-retune reads at
# a glance without opening the payload.
_AUDIT_CHANNEL_PREVIEW = 4
# One "new client" line per source IP -- keyed by IP, not (ip, port), because a
# client gets a fresh source port per socket and a port scan would otherwise
# grow the table without bound.
_AUDIT_MAX_PEERS = 128
# Cap on any one field written into an audit line.
_AUDIT_MAX_FIELD = 400

_audit_handler_installed = False
_audit_path: Optional[str] = None


def _audit_log_path() -> Path:
    """Where audit lines go.

    Deliberately NOT the daemon's stdout log. That file takes a cluster_hop
    event roughly every 0.8 s -- 1.4 GB in four hours on airband, with
    StandardOutput=append and so no rotation -- and a handful of audit lines a
    day would be unfindable in it.
    """
    explicit = os.environ.get("CHIRP_CMD_AUDIT_LOG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    hit = os.environ.get("CHIRP_HIT_LOG", "").strip()
    if hit:
        return Path(hit).expanduser().parent / "cmd-audit.log"
    return Path("~/.local/share/chirp/logs/cmd-audit.log").expanduser()


def install_audit_handler() -> Optional[str]:
    """Give the audit logger its own small rotating file. Idempotent.

    Returns the path in use, or None if it could not be opened -- in which case
    the records still reach the root logger's handlers, so attribution degrades
    instead of disappearing.
    """
    global _audit_handler_installed, _audit_path
    if _audit_handler_installed:
        return _audit_path
    path = _audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            str(path), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        audit.addHandler(handler)
        _audit_path = str(path)
    except Exception as exc:  # permissions, read-only fs, full disk
        log.warning("could not open command audit log %s: %r", path, exc)
        _audit_path = None
    _audit_handler_installed = True
    return _audit_path


def _audit_scrub(text: str) -> str:
    """Keep an audit line to one line, and bounded.

    This port takes commands from unauthenticated clients, and the request id
    and channel ids are free-form strings, so a newline in one would otherwise
    let a caller forge an audit entry -- including one naming a different
    sender. Length is capped for the same reason a 4 KB packet is.
    """
    try:
        out = str(text).replace("\r", " ").replace("\n", " ")
    except Exception:
        return "<unprintable>"
    if len(out) > _AUDIT_MAX_FIELD:
        return out[:_AUDIT_MAX_FIELD] + "..."
    return out


def _fmt_addr(addr: Any) -> str:
    """"ip:port" from a datagram address, tolerant of odd shapes."""
    try:
        if isinstance(addr, (str, bytes, bytearray)):
            return _audit_scrub(str(addr))
        return "%s:%s" % (addr[0], addr[1])
    except Exception:
        return _audit_scrub(str(addr))


def _audit_args(cmd: str, args: Any) -> str:
    """Compact summary of a command's args.

    Never raises: a vague audit line is worth far more than a command dropped
    because summarising it went wrong.
    """
    try:
        if cmd == "add_channel":
            chans = list(getattr(args, "channels", None) or [])
            parts = ["n=%d" % len(chans)]
            for c in chans[:_AUDIT_CHANNEL_PREVIEW]:
                parts.append("%s@%s" % (getattr(c, "id", "?"),
                                        getattr(c, "freq_mhz", "?")))
            if len(chans) > _AUDIT_CHANNEL_PREVIEW:
                parts.append("+%d" % (len(chans) - _AUDIT_CHANNEL_PREVIEW))
            return _audit_scrub(" ".join(parts))
        parts = []
        for field in _AUDIT_ARG_FIELDS:
            val = getattr(args, field, None)
            if val is not None:
                parts.append("%s=%s" % (field, val))
        return _audit_scrub(" ".join(parts))
    except Exception:
        return "<args unsummarizable>"


# A dispatch callable receives a validated Envelope + validated args model
# and returns a Response. May raise; the server traps and replies "error".
Dispatch = Callable[[Envelope, Any], Response]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 7400
    # Optional event sink (host, port). If None, events go to stdout.
    event_sink: Optional[tuple[str, int]] = None
    # Max datagram size (design doc: 4 KB).
    max_packet: int = 4096


class _CommandProtocol(asyncio.DatagramProtocol):
    """asyncio.DatagramProtocol that validates + dispatches each request."""

    def __init__(self, dispatch: Dispatch, server: "CommandServer") -> None:
        self.dispatch = dispatch
        self.server = server
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:  # type: ignore[override]
        self.transport = transport  # type: ignore[assignment]
        log.info("command server listening on %s:%d", self.server.cfg.host, self.server.cfg.port)

    def datagram_received(self, data: bytes, addr: tuple) -> None:  # type: ignore[override]
        if self.transport is None:
            return
        # Before validation, so a client speaking the wrong protocol still
        # shows up as a host that is talking to the radio.
        if self.server.note_peer(addr):
            audit.info("new client %s", _fmt_addr(addr))
        if len(data) > self.server.cfg.max_packet:
            resp = Response.make_error("unknown", f"packet too large ({len(data)} > {self.server.cfg.max_packet})")
            self._reply(resp, addr)
            return

        # Phase 1: each datagram is one JSON object. Trim trailing newline if any.
        body = data.rstrip(b"\r\n")

        # 1) Envelope
        try:
            env = parse_envelope(body)
        except ValidationError as ve:
            # We don't know the request id; use "unknown".
            resp = Response.make_error("unknown", _short_validation_msg(ve))
            self._reply(resp, addr)
            return
        except Exception as e:
            resp = Response.make_error("unknown", f"malformed json: {e}")
            self._reply(resp, addr)
            return

        # 2) Known command?
        if env.cmd not in COMMAND_ARGS:
            resp = Response.make_rejected(env.id, f"unknown cmd: {env.cmd!r}")
            self._reply(resp, addr)
            return

        # 3) Args validation
        try:
            args = parse_args(env.cmd, env.args)
        except ValidationError as ve:
            resp = Response.make_rejected(env.id, _short_validation_msg(ve))
            self._reply(resp, addr)
            return
        except Exception as e:
            resp = Response.make_error(env.id, f"args parse failed: {e}")
            self._reply(resp, addr)
            return

        # 4a) Phase 3: subscribe/unsubscribe are server-state commands; handle
        # them directly (no flowgraph dispatch) so we have the source addr.
        if env.cmd == "subscribe":
            self.server.add_subscriber(addr, list(getattr(args, "events", []) or []))
            self._reply(Response.make_ok(env.id, {
                "subscribed": True,
                "events": list(getattr(args, "events", []) or []),
                "count": self.server.subscriber_count(),
            }), addr)
            return
        if env.cmd == "unsubscribe":
            removed = self.server.remove_subscriber(addr)
            self._reply(Response.make_ok(env.id, {
                "subscribed": False,
                "removed": removed,
                "count": self.server.subscriber_count(),
            }), addr)
            return

        # 4c) Record who is about to change the radio. Logged before dispatch
        # so a command that wedges the flowgraph still leaves its sender named.
        if env.cmd not in READ_ONLY_COMMANDS:
            audit.info("cmd=%s id=%s from=%s %s", _audit_scrub(env.cmd),
                       _audit_scrub(env.id), _fmt_addr(addr),
                       _audit_args(env.cmd, args))

        # 4b) Dispatch to flowgraph
        try:
            resp = self.dispatch(env, args)
            if not isinstance(resp, Response):
                resp = Response.make_error(env.id, f"dispatch returned non-Response: {type(resp).__name__}")
        except Exception as e:  # noqa: BLE001 — server must never die from a bad cmd
            log.exception("dispatch failed for cmd=%s id=%s", env.cmd, env.id)
            resp = Response.make_error(env.id, f"internal: {e}")

        self._reply(resp, addr)

    def _reply(self, resp: Response, addr: tuple) -> None:
        if self.transport is None:
            return
        payload = resp.model_dump_json().encode("utf-8")
        try:
            self.transport.sendto(payload, addr)
        except Exception:
            log.exception("sendto failed addr=%s", addr)

    def error_received(self, exc: Exception) -> None:  # type: ignore[override]
        log.warning("udp error_received: %s", exc)


def _short_validation_msg(ve: ValidationError) -> str:
    """One-line summary of a pydantic ValidationError, suitable for the wire."""
    errs = ve.errors()
    if not errs:
        return "validation failed"
    first = errs[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = first.get("msg", "invalid")
    extra = f" ({len(errs) - 1} more)" if len(errs) > 1 else ""
    return f"{loc}: {msg}{extra}" if loc else f"{msg}{extra}"


class CommandServer:
    """UDP command server. Runs an asyncio loop in a dedicated background thread."""

    def __init__(self, cfg: ServerConfig, dispatch: Dispatch) -> None:
        self.cfg = cfg
        self.dispatch = dispatch
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._event_sock: Optional[socket.socket] = None
        self._started = threading.Event()
        self._stop = threading.Event()
        # Phase 3: dynamic event subscribers.
        # Map (host, port) → set of event-name strings ('' set ⇒ all events).
        self._subscribers: dict[tuple[str, int], set[str]] = {}
        self._sub_lock = threading.Lock()
        # Source IPs seen at least once, for the one-line-per-host notice.
        self._seen_peers: set[str] = set()
        self._peer_lock = threading.Lock()
        self._peer_table_full = False

    def note_peer(self, addr: tuple) -> bool:
        """Record a source host; True only the first time one is seen.

        Keyed by IP rather than (ip, port): a client gets a fresh source port
        per socket, so port-keying would log the same host repeatedly and let a
        port scan grow the table without bound. Capped anyway -- past the cap
        new hosts stop being announced, but their mutating commands are still
        logged with ``from=``, which is the part that matters.
        """
        try:
            ip = str(addr[0])
        except Exception:
            return False
        with self._peer_lock:
            if ip in self._seen_peers:
                return False
            if len(self._seen_peers) >= _AUDIT_MAX_PEERS:
                if not self._peer_table_full:
                    self._peer_table_full = True
                    log.warning("audit peer table full at %d hosts; new-client "
                                "notices suppressed", _AUDIT_MAX_PEERS)
                return False
            self._seen_peers.add(ip)
            return True

    # -- Phase 3: dynamic event subscriber registry -------------------------

    def add_subscriber(self, addr: tuple[str, int], events: list[str]) -> None:
        """Register an addr to receive future emit_event() pushes. Empty
        `events` list means 'all events'."""
        with self._sub_lock:
            self._subscribers[(addr[0], int(addr[1]))] = set(events)
        log.info("subscriber added %s events=%s", addr, events)

    def remove_subscriber(self, addr: tuple[str, int]) -> bool:
        with self._sub_lock:
            return self._subscribers.pop((addr[0], int(addr[1])), None) is not None

    def subscriber_count(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    def subscribers_for(self, evt: str) -> list[tuple[str, int]]:
        """Return addrs whose filter matches `evt` (empty filter = all)."""
        with self._sub_lock:
            return [a for a, f in self._subscribers.items() if not f or evt in f]

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("CommandServer already started")
        install_audit_handler()
        self._thread = threading.Thread(target=self._run, name="chirp-cmdserver", daemon=True)
        self._thread.start()
        # Wait for bind to succeed (or fail) before returning.
        if not self._started.wait(timeout=5.0):
            raise RuntimeError("CommandServer did not start within 5 s")

    def stop(self, timeout: float = 5.0) -> None:
        if self._loop is None or self._thread is None:
            return
        self._stop.set()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        self._thread.join(timeout=timeout)
        if self._event_sock is not None:
            try:
                self._event_sock.close()
            except Exception:
                pass
            self._event_sock = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            coro = loop.create_datagram_endpoint(
                lambda: _CommandProtocol(self.dispatch, self),
                local_addr=(self.cfg.host, self.cfg.port),
            )
            transport, _proto = loop.run_until_complete(coro)
            self._transport = transport  # type: ignore[assignment]
            # Open the event socket lazily, only if a sink is configured.
            if self.cfg.event_sink is not None:
                self._event_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._started.set()
            try:
                loop.run_forever()
            finally:
                if self._transport is not None:
                    self._transport.close()
                loop.run_until_complete(asyncio.sleep(0.05))
        except Exception:
            log.exception("command server crashed")
            self._started.set()  # release any waiter
        finally:
            try:
                loop.close()
            except Exception:
                pass

    # -- events -------------------------------------------------------------

    def emit_event(self, evt: str, **fields: Any) -> None:
        """Push an async event to subscribers.

        Always emits a structured JSON line to stdout for log capture. If a
        static sink is configured (CHIRP_EVENT_SINK), fires-and-forgets a UDP
        datagram to it. Phase 3: additionally fan-out to any dynamic
        subscribers registered via the `subscribe` command, filtered by their
        event-type set.
        """
        ev = Event(v=PROTOCOL_VERSION, evt=evt, ts=time.time(), **fields)
        payload = ev.model_dump_json()
        encoded = payload.encode("utf-8")
        # Stdout (one line per event — easy to grep, easy to forward to journald).
        print(payload, flush=True)
        # Static event sink.
        if self._event_sock is not None and self.cfg.event_sink is not None:
            try:
                self._event_sock.sendto(encoded, self.cfg.event_sink)
            except Exception:
                log.warning("event sink send failed: %s", self.cfg.event_sink)
        # Phase 3 dynamic subscribers.
        targets = self.subscribers_for(evt)
        if targets:
            sock = self._event_sock
            if sock is None:
                # Create a one-shot socket if no static sink was configured.
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._event_sock = sock
                except Exception:
                    log.exception("could not open subscriber send socket")
                    return
            for addr in targets:
                try:
                    sock.sendto(encoded, addr)
                except Exception:
                    log.warning("subscriber send failed: %s", addr)


__all__ = ["CommandServer", "ServerConfig", "Dispatch"]
