"""WebSocket push channel for FFT spectrum bins (RFC 6455, server-side).

Hand-rolled because the surrounding UI server is stdlib ``http.server`` and
pulling in ``websockets`` or ``wsproto`` would mean reshaping the entire
request loop.  We only need server->client binary frames + ping/close
handling, which is roughly 150 lines.

Binary frame layout (sent on each pane update, little-endian throughout)::

    offset  size  field
    ------  ----  --------------------------------
    0       1     u8   pane_id (0=waterfall, 1=vfo, 2=disco)
    1       4     f32  center_mhz
    5       4     f32  span_mhz
    9       2     u16  n_bins
    11      4*N   f32[]  bins  (dBFS)

Transport: waterfall.py publishes each FFT frame to a SOCK_DGRAM unix
socket (``SPEC_PUBLISHER_PATH``).  We subscribe by binding our own
AF_UNIX path and sending a tiny hello packet; waterfall.py then
``sendto()`` each frame to us as it is computed, eliminating the old
state.json mtime-poll round-trip and dropping end-to-end latency from
20-50 ms to roughly a single context switch.  We heartbeat every
``SUBSCRIBER_HEARTBEAT_S`` so the publisher knows we are still alive.

Fallback: if the socket can't be opened (waterfall.py down or socket
not bound yet) or it stops delivering frames for ``WS_SOCKET_STALE_S``,
we fall back to the legacy mtime-poll of ``WATERFALL_STATE_PATH`` at
``WS_MTIME_POLL_INTERVAL_S``.  state.json keeps being written by
waterfall.py — it is still the /api/waterfall HTTP payload and the
heartbeat freshness probe — so the fallback is genuinely a fallback and
never silently broken.  Reconnects to the socket use exponential backoff
capped at ``WS_BACKOFF_CAP_S``.

VFO and Discovery panes are data-only on /sb5 — their spectrum plot +
waterfall were dropped in favour of Live IQ as the single spectrum view,
so the WS push only carries pane_id=0 (waterfall).  pane_id slots 1
(VFO) and 2 (Disco) stay reserved in the wire protocol for future use.
No data fan-out: each connected client owns its own subscriber socket
and its own copy of the state.  At our scale (~1 operator, maybe 2 tabs)
this is cheaper than a shared broadcaster.

Read path: inbound client frames are read directly from the raw socket
(handler.connection) rather than handler.rfile, because BufferedReader
turns socket timeouts into partial reads that look like EOF.  Outbound
frames go through handler.wfile so http.server's buffering applies.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import select
import socket
import struct
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants -- no magic numbers below this line.
# ---------------------------------------------------------------------------

# RFC 6455 fixed handshake GUID.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# mtime-poll cadence.  Fastest producer is waterfall.py at ~30 Hz, so 20 ms
# (50 Hz) is well above Nyquist and keeps end-to-end latency under 50 ms.
WS_MTIME_POLL_INTERVAL_S = 0.02

# Keepalive ping cadence.  Tailscale + nginx idle timeouts are >60s; 20s
# is comfortably under that and well above the 50 ms poller granularity.
WS_PING_INTERVAL_S = 20.0

# Maximum control payload size we are willing to read from a client.  Per
# RFC 6455 control frames are <=125 bytes; this is a defence-in-depth cap.
WS_MAX_CONTROL_LEN = 125

# Maximum data payload we are willing to read from a client.  We do not
# expect inbound data, but a misbehaving client could send anything.  64KiB
# is generous and bounds memory.
WS_MAX_INBOUND_DATA_LEN = 65535

# Reconnect backoff window (documented here because the client mirrors it).
WS_BACKOFF_BASE_S = 1.0
WS_BACKOFF_CAP_S = 10.0

# Per-frame header size in bytes (u8 + f32 + f32 + u16 = 11).
WS_FRAME_HEADER_BYTES = 11

# RFC 6455 opcodes.
WS_OP_CONT = 0x0
WS_OP_TEXT = 0x1
WS_OP_BINARY = 0x2
WS_OP_CLOSE = 0x8
WS_OP_PING = 0x9
WS_OP_PONG = 0xA

# Extended-length payload thresholds.
WS_LEN_INLINE_MAX = 125
WS_LEN_16BIT_MAX = 65535

# When the socket has nothing to read, sleep this long before the next
# mtime poll.  Same value as WS_MTIME_POLL_INTERVAL_S; named separately
# so the role is obvious at the call site.
WS_SELECT_TIMEOUT_S = WS_MTIME_POLL_INTERVAL_S

# Direct-IPC subscription to waterfall.py's spectrum publisher.
WATERFALL_RUN_DIR = "/run/scannerproject/waterfall"
SPEC_PUBLISHER_PATH = WATERFALL_RUN_DIR + "/spec.sock"
# Matches waterfall.py SUBSCRIBER_HELLO_BYTES — see that file for the
# protocol notes.  Doubles as the keepalive packet.
SUBSCRIBER_HELLO_BYTES = b"SUB\x01"
# Heartbeat cadence.  Publisher drops subscribers idle for >5 s, so
# anything well under that is fine; 2 s keeps us in the green even if
# a few datagrams get dropped.
SUBSCRIBER_HEARTBEAT_S = 2.0
# Datagram recv buffer: 2048 bins -> 8203 bytes, with headroom.
SUBSCRIBER_RECV_BUFSIZE = 65536
# Inactivity window before we consider the socket "stale" and re-enable
# the state.json mtime-poll fallback path.  ~6x the producer's 33 ms
# frame period: long enough to ride out single dropped packets, short
# enough that a real outage flips to the fallback within a tenth of a
# second.
WS_SOCKET_STALE_S = 0.2

# Pane registry.  Slot indices match the wire pane_id.  VFO (1) and
# Disco (2) are reserved but no longer broadcast — see module docstring.
WATERFALL_STATE_PATH = WATERFALL_RUN_DIR + "/state.json"

PANE_WATERFALL = 0
PANE_VFO = 1
PANE_DISCO = 2

# (pane_id, state_path) tuples polled per-client.  Only the waterfall
# pane is broadcast; adding more tuples here re-enables their pane.
WS_PANES = (
    (PANE_WATERFALL, WATERFALL_STATE_PATH),
)

# Default center/span fallbacks if the waterfall state file omits them.
# Matches the values the HTTP pass-through and the JS client use today.
WATERFALL_DEFAULT_CENTER_MHZ = 123.7
WATERFALL_DEFAULT_BW_MHZ = 4.0


# ---------------------------------------------------------------------------
# Handshake.
# ---------------------------------------------------------------------------

def compute_accept_key(client_key):
    """Compute the Sec-WebSocket-Accept value per RFC 6455 sec. 4.2.2."""
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def send_handshake(wfile, accept_key):
    """Send the 101 Switching Protocols response."""
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: " + accept_key + "\r\n"
        "\r\n"
    )
    wfile.write(response.encode("ascii"))
    wfile.flush()


# ---------------------------------------------------------------------------
# Frame encode/decode.
# ---------------------------------------------------------------------------

def encode_frame(opcode, payload):
    """Encode a single unfragmented server->client frame.

    Server frames are unmasked (RFC 6455 sec. 5.1).
    """
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))  # FIN=1
    n = len(payload)
    if n <= WS_LEN_INLINE_MAX:
        header.append(n)
    elif n <= WS_LEN_16BIT_MAX:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    return bytes(header) + payload


def _recv_exact(conn, n):
    """Read exactly n bytes from the raw socket or raise ConnectionError."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            raise ConnectionError("client closed connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(conn):
    """Read one client->server frame from the raw socket.

    Returns (opcode, payload) or raises ConnectionError on EOF / protocol
    violation.  Client frames MUST be masked per RFC 6455 sec. 5.1.
    """
    b1b2 = _recv_exact(conn, 2)
    b1 = b1b2[0]
    b2 = b1b2[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _recv_exact(conn, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _recv_exact(conn, 8))
    is_control = (opcode & 0x08) != 0
    if is_control and length > WS_MAX_CONTROL_LEN:
        raise ConnectionError("control frame too long")
    if not is_control and length > WS_MAX_INBOUND_DATA_LEN:
        raise ConnectionError("inbound frame too long")
    if not masked:
        raise ConnectionError("client frame not masked")
    mask = _recv_exact(conn, 4)
    data = bytearray(_recv_exact(conn, length))
    for i in range(length):
        data[i] ^= mask[i & 3]
    return opcode, bytes(data)


# ---------------------------------------------------------------------------
# State-file -> binary frame.
# ---------------------------------------------------------------------------

def _read_state(path):
    """Best-effort read of a producer state.json.  Returns None on miss."""
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


def build_spectrum_frame(pane_id, state):
    """Pack a single pane's state into the binary wire format.

    Returns None when the state has no usable bins, e.g. service down.
    """
    bins = state.get("bins")
    if not isinstance(bins, list) or not bins:
        return None
    try:
        floats = [float(b) for b in bins]
    except (TypeError, ValueError):
        return None
    n = len(floats)
    if pane_id == PANE_WATERFALL:
        center = float(state.get("center_mhz") or WATERFALL_DEFAULT_CENTER_MHZ)
        fmin = state.get("freq_min_mhz")
        fmax = state.get("freq_max_mhz")
        if isinstance(fmin, (int, float)) and isinstance(fmax, (int, float)) and fmax > fmin:
            span = float(fmax) - float(fmin)
        else:
            span = float(state.get("bw_mhz") or WATERFALL_DEFAULT_BW_MHZ)
    else:
        # VFO + Disco panes are not broadcast (see module docstring).
        return None
    header = struct.pack("<BffH", pane_id, center, span, n)
    body = struct.pack("<" + str(n) + "f", *floats)
    return header + body


# ---------------------------------------------------------------------------
# Direct-IPC subscriber to waterfall.py.
# ---------------------------------------------------------------------------

class SpectrumSubscriber:
    """SOCK_DGRAM subscriber to waterfall.py's frame publisher.

    Each WS client owns one of these.  We bind our own AF_UNIX path
    (unique per-pid + thread so concurrent clients don't collide) and
    send ``SUBSCRIBER_HELLO_BYTES`` to the publisher; the publisher
    harvests our address via recvfrom() and starts sendto()-ing frames.

    Failure modes are all best-effort: bind failures, send failures and
    publisher restarts all just drop us back to "not connected" so the
    caller can fall back to the mtime-poll path while we retry on the
    documented exponential-backoff schedule.
    """

    def __init__(self):
        self.pub_path = SPEC_PUBLISHER_PATH
        self.sock = None
        self.local_path = None
        self._backoff = WS_BACKOFF_BASE_S
        self._next_attempt_at = 0.0
        self._last_heartbeat_at = 0.0
        self.connect()

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> None:
        """Open the subscriber socket and send a hello to the publisher."""
        if self.sock is not None:
            return
        try:
            self.local_path = os.path.join(
                WATERFALL_RUN_DIR,
                "ws-client.%d-%d.sock" % (os.getpid(), threading.get_ident()),
            )
            try:
                os.unlink(self.local_path)
            except FileNotFoundError:
                pass
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.setblocking(False)
            s.bind(self.local_path)
            os.chmod(self.local_path, 0o666)
            s.sendto(SUBSCRIBER_HELLO_BYTES, self.pub_path)
            self.sock = s
            self._last_heartbeat_at = time.monotonic()
            self._backoff = WS_BACKOFF_BASE_S
        except OSError as e:
            logger.debug("subscriber connect failed: %s", e)
            self._teardown_socket()
            self._schedule_reconnect()

    def maybe_heartbeat(self, now: float) -> None:
        if self.sock is None:
            return
        if (now - self._last_heartbeat_at) < SUBSCRIBER_HEARTBEAT_S:
            return
        try:
            self.sock.sendto(SUBSCRIBER_HELLO_BYTES, self.pub_path)
            self._last_heartbeat_at = now
        except OSError as e:
            logger.debug("subscriber heartbeat failed: %s", e)
            self._teardown_socket()
            self._schedule_reconnect()

    def maybe_reconnect(self, now: float) -> None:
        if self.sock is not None:
            return
        if now >= self._next_attempt_at:
            self.connect()

    def recv_frame(self) -> bytes | None:
        if self.sock is None:
            return None
        try:
            return self.sock.recv(SUBSCRIBER_RECV_BUFSIZE)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError as e:
            logger.debug("subscriber recv failed: %s", e)
            self._teardown_socket()
            self._schedule_reconnect()
            return None

    def _schedule_reconnect(self) -> None:
        self._next_attempt_at = time.monotonic() + self._backoff
        self._backoff = min(WS_BACKOFF_CAP_S, self._backoff * 2.0)

    def _teardown_socket(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.local_path:
            try:
                os.unlink(self.local_path)
            except OSError:
                pass
            self.local_path = None

    def close(self) -> None:
        self._teardown_socket()


# ---------------------------------------------------------------------------
# Per-client loop.
# ---------------------------------------------------------------------------

def serve_client(wfile, conn):
    """Run the spectrum push loop for one connected client.

    Returns when the client disconnects, sends close, or errors.  Caller is
    responsible for the handshake; we just push frames and process inbound
    control frames.  Inbound frames are read from the raw socket via select;
    outbound frames go through wfile so http.server's buffering applies.

    Primary delivery path is the SOCK_DGRAM subscription to waterfall.py
    (``SpectrumSubscriber``).  When that goes quiet for ``WS_SOCKET_STALE_S``
    we fall back to the state.json mtime poll so the UI keeps updating
    while the publisher reconnects.
    """
    last_mtimes = {pid: -1.0 for pid, _ in WS_PANES}
    last_ping_at = time.monotonic()
    sub = SpectrumSubscriber()
    last_socket_frame_at = time.monotonic()
    # The raw socket has whatever timeout the HTTP server set; we drive
    # readiness via select() with WS_SELECT_TIMEOUT_S so we can interleave
    # the mtime poll without blocking inbound reads.
    conn.setblocking(True)
    conn.settimeout(None)
    try:
        while True:
            # 1) Wait for either the client (control frames) or the
            # subscriber socket (live spectrum frames) to be readable.
            fds = [conn]
            if sub.connected:
                fds.append(sub.sock)
            try:
                ready, _, _ = select.select(fds, [], [], WS_SELECT_TIMEOUT_S)
            except (OSError, ValueError):
                return

            if conn in ready:
                try:
                    opcode, payload = read_frame(conn)
                except (ConnectionError, OSError):
                    return
                if opcode == WS_OP_CLOSE:
                    try:
                        wfile.write(encode_frame(
                            WS_OP_CLOSE, payload[:WS_MAX_CONTROL_LEN]))
                        wfile.flush()
                    except OSError:
                        pass
                    return
                if opcode == WS_OP_PING:
                    try:
                        wfile.write(encode_frame(
                            WS_OP_PONG, payload[:WS_MAX_CONTROL_LEN]))
                        wfile.flush()
                    except OSError:
                        return
                # PONG and any unsolicited TEXT/BINARY we silently drop.

            # 2) Live spectrum frames from waterfall.py.  Drain everything
            # the kernel has queued so we don't lag behind under load.
            if sub.connected and sub.sock in ready:
                while True:
                    frame = sub.recv_frame()
                    if frame is None or len(frame) < WS_FRAME_HEADER_BYTES:
                        break
                    try:
                        wfile.write(encode_frame(WS_OP_BINARY, frame))
                        wfile.flush()
                    except OSError:
                        return
                    last_socket_frame_at = time.monotonic()

            now = time.monotonic()
            sub.maybe_heartbeat(now)
            sub.maybe_reconnect(now)

            # 3) Fallback: when the subscriber path is quiet, poll
            # state.json mtime so the UI keeps updating during a
            # publisher outage or reconnect window.
            socket_stale = (
                (not sub.connected)
                or (now - last_socket_frame_at) > WS_SOCKET_STALE_S
            )
            if socket_stale:
                for pane_id, path in WS_PANES:
                    try:
                        mt = os.stat(path).st_mtime
                    except OSError:
                        continue
                    if mt <= last_mtimes[pane_id]:
                        continue
                    state = _read_state(path)
                    if state is None:
                        continue
                    frame = build_spectrum_frame(pane_id, state)
                    if frame is None:
                        last_mtimes[pane_id] = mt
                        continue
                    try:
                        wfile.write(encode_frame(WS_OP_BINARY, frame))
                        wfile.flush()
                    except OSError:
                        return
                    last_mtimes[pane_id] = mt

            # 4) Periodic ping to keep intermediaries from idling us out.
            if (now - last_ping_at) >= WS_PING_INTERVAL_S:
                try:
                    wfile.write(encode_frame(WS_OP_PING, b""))
                    wfile.flush()
                except OSError:
                    return
                last_ping_at = now
    finally:
        sub.close()


def handle_spectrum_upgrade(handler):
    """Handle a /ws/spectrum GET as a WebSocket upgrade.

    Called from ``Handler.do_GET`` once the path has been matched.  Returns
    True if we consumed the connection (caller should not write anything
    further); False if the request was not a valid Upgrade (caller may
    return a normal HTTP error).
    """
    headers = handler.headers
    upgrade = (headers.get("Upgrade") or "").strip().lower()
    connection = (headers.get("Connection") or "").lower()
    client_key = (headers.get("Sec-WebSocket-Key") or "").strip()
    version = (headers.get("Sec-WebSocket-Version") or "").strip()
    if upgrade != "websocket" or "upgrade" not in connection or not client_key or version != "13":
        return False
    accept_key = compute_accept_key(client_key)
    try:
        send_handshake(handler.wfile, accept_key)
    except OSError:
        return True
    handler.close_connection = True
    try:
        serve_client(handler.wfile, handler.connection)
    except Exception:
        logger.debug("ws spectrum client loop ended", exc_info=True)
    return True
