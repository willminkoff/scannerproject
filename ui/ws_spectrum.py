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

The handler mtime-polls the waterfall state file at ``WS_MTIME_POLL_INTERVAL_S``
and emits a frame to the client whenever the file is newer than the last
one sent.  VFO and Discovery panes are data-only on /sb5 — their spectrum
plot + waterfall were dropped in favour of Live IQ as the single spectrum
view, so the WS push only carries pane_id=0 (waterfall).  pane_id slots
1 (VFO) and 2 (Disco) stay reserved in the wire protocol for future use.
No data fan-out: each connected client owns its own poller thread and its
own copy of the state.  At our scale (~1 operator, maybe 2 tabs) this is
cheaper than a shared broadcaster.

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

# Pane registry.  Slot indices match the wire pane_id.  VFO (1) and
# Disco (2) are reserved but no longer broadcast — see module docstring.
WATERFALL_STATE_PATH = "/run/scannerproject/waterfall/state.json"

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
WATERFALL_DEFAULT_BW_MHZ = 4.8


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
# Per-client loop.
# ---------------------------------------------------------------------------

def serve_client(wfile, conn):
    """Run the spectrum push loop for one connected client.

    Returns when the client disconnects, sends close, or errors.  Caller is
    responsible for the handshake; we just push frames and process inbound
    control frames.  Inbound frames are read from the raw socket via select;
    outbound frames go through wfile so http.server's buffering applies.
    """
    last_mtimes = {pid: -1.0 for pid, _ in WS_PANES}
    last_ping_at = time.monotonic()
    # The raw socket has whatever timeout the HTTP server set; we drive
    # readiness via select() with WS_SELECT_TIMEOUT_S so we can interleave
    # the mtime poll without blocking inbound reads.
    conn.setblocking(True)
    conn.settimeout(None)
    while True:
        # 1) Drain any pending inbound frame (close/ping/pong).
        try:
            ready, _, _ = select.select([conn], [], [], WS_SELECT_TIMEOUT_S)
        except (OSError, ValueError):
            return
        if ready:
            try:
                opcode, payload = read_frame(conn)
            except (ConnectionError, OSError):
                return
            if opcode == WS_OP_CLOSE:
                try:
                    wfile.write(encode_frame(WS_OP_CLOSE, payload[:WS_MAX_CONTROL_LEN]))
                    wfile.flush()
                except OSError:
                    pass
                return
            if opcode == WS_OP_PING:
                try:
                    wfile.write(encode_frame(WS_OP_PONG, payload[:WS_MAX_CONTROL_LEN]))
                    wfile.flush()
                except OSError:
                    return
            # PONG and any unsolicited TEXT/BINARY we silently drop.

        # 2) Push spectrum frames for any pane whose state.json has changed.
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

        # 3) Periodic ping to keep intermediaries from idling us out.
        now = time.monotonic()
        if (now - last_ping_at) >= WS_PING_INTERVAL_S:
            try:
                wfile.write(encode_frame(WS_OP_PING, b""))
                wfile.flush()
            except OSError:
                return
            last_ping_at = now


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
