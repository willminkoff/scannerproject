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
import socket
import threading
import time
from dataclasses import dataclass
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

        # 4) Dispatch to flowgraph
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

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("CommandServer already started")
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
        """Push an async event to the subscriber sink (or stdout if none).

        Always emits a structured JSON line to stdout for log capture; if a
        sink is configured, also fires-and-forgets a UDP datagram to it.
        """
        ev = Event(v=PROTOCOL_VERSION, evt=evt, ts=time.time(), **fields)
        payload = ev.model_dump_json()
        # Stdout (one line per event — easy to grep, easy to forward to journald).
        print(payload, flush=True)
        if self._event_sock is not None and self.cfg.event_sink is not None:
            try:
                self._event_sock.sendto(payload.encode("utf-8"), self.cfg.event_sink)
            except Exception:
                log.warning("event sink send failed: %s", self.cfg.event_sink)


__all__ = ["CommandServer", "ServerConfig", "Dispatch"]
