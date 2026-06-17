"""ui.chirp_client — sync UDP JSON client for the chirp daemon command bus.

Phase 4c. When ``SB5_USE_GR_DEMOD=true``, airband-ui's existing operator
endpoints proxy to chirp daemons (airband on 127.0.0.1:7400, ground on
127.0.0.1:7401) instead of writing rtl-airband config + invoking
``safe_restart_rtl_airband``.

Wire format mirrors ``chirp/cmd/schema.py``: a request is an Envelope
``{v, id, cmd, args}`` JSON object; the daemon replies with
``{v, id, status, data, error}``.  We DO NOT take a hard dependency on
``chirp/`` here — we construct/parse the JSON ourselves so the airband-ui
process does not pay the chirp/pydantic import cost on every restart
when the feature flag is off (the default).

Threading: this client is sync.  Each ``_send`` opens a UDP socket,
sends one datagram, blocks waiting for the reply (default timeout 2 s).
For chirp (loopback), end-to-end latency is sub-100 ms in practice.

Audit log: every call appends one JSON line to
``~/.cache/airband-ui/chirp_client.jsonl`` so the operator can replay
who touched what when.  Path overridable via ``CHIRP_CLIENT_LOG_PATH``.

Production safety: this module is dormant when ``SB5_USE_GR_DEMOD`` is
unset/false.  No background threads.  No daemons started.  Importing it
in handlers.py costs <1 ms (stdlib-only).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (env-overridable, safe defaults)
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = 1  # Matches chirp/cmd/schema.py.

#: Default UDP ports — these MUST match chirp/config/{airband,ground}.json.
AIRBAND_PORT = int(os.environ.get("CHIRP_AIRBAND_PORT", "7400"))
GROUND_PORT = int(os.environ.get("CHIRP_GROUND_PORT", "7401"))
LOOPBACK = os.environ.get("CHIRP_BIND_HOST", "127.0.0.1")

#: Per-call socket timeout, seconds.  Loopback UDP should be sub-100 ms;
#: 2 s gives the daemon room to spin up its asyncio handler under load.
DEFAULT_TIMEOUT_SEC = float(os.environ.get("CHIRP_CLIENT_TIMEOUT_SEC", "2.0"))

#: Liveness-probe timeout (used by heartbeat).  Tighter than the call
#: timeout because heartbeat must not block the SSE for seconds at a time.
LIVENESS_TIMEOUT_SEC = float(os.environ.get("CHIRP_CLIENT_LIVENESS_TIMEOUT_SEC", "0.5"))

DEFAULT_LOG_PATH = str(Path.home() / ".cache" / "airband-ui" / "chirp_client.jsonl")
LOG_PATH = os.environ.get("CHIRP_CLIENT_LOG_PATH", DEFAULT_LOG_PATH)
LOG_MAX_BYTES = int(os.environ.get("CHIRP_CLIENT_LOG_MAX_BYTES", "1048576"))  # 1 MiB


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

#: Env-var name that toggles the chirp code path on/off in airband-ui.
FEATURE_FLAG_ENV = "SB5_USE_GR_DEMOD"


def use_gr_demod() -> bool:
    """Return True iff the chirp code path is enabled.

    Default is False.  Production behavior is identical to pre-Phase-4c
    until the operator sets ``SB5_USE_GR_DEMOD=true`` in the airband-ui
    systemd unit (or shell env) and restarts.
    """
    return str(os.environ.get(FEATURE_FLAG_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ChirpClientError(Exception):
    """Base class for all ChirpClient errors."""


class ChirpDaemonDown(ChirpClientError):
    """Daemon did not reply (timeout / ECONNREFUSED / sendto failed).

    Caller should treat this as a transient infrastructure failure and
    surface it in the UI as a recoverable error (the operator can retry
    or fall back to rtl-airband by flipping the flag off).
    """


class ChirpRejected(ChirpClientError):
    """Daemon replied ``status=rejected`` — well-formed command refused.

    Examples: ``unknown channel: <id>``, ``noise_floor_not_warm``,
    ``unsupported mode``, ``pool full``.  The ``code`` attribute carries
    the raw daemon error string so callers can switch on it.
    """

    def __init__(self, message: str, *, code: str | None = None,
                 payload: dict | None = None):
        super().__init__(message)
        self.code = code or message
        self.payload = payload or {}


class ChirpDaemonError(ChirpClientError):
    """Daemon replied ``status=error`` — schema validation failure or
    internal exception inside the daemon.  Usually indicates a bug on
    one side or the other; should be loud in logs."""


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def _rotate_audit_log(path: str) -> None:
    """Rotate ``path`` -> ``path.1`` when it crosses LOG_MAX_BYTES."""
    try:
        if os.path.getsize(path) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    backup = path + ".1"
    try:
        if os.path.exists(backup):
            os.remove(backup)
        os.replace(path, backup)
    except OSError:
        logger.debug("chirp_client: audit log rotate failed", exc_info=True)


def _write_audit(event: dict) -> None:
    """Append one JSON line to the audit log.  Best-effort; never raises."""
    path = LOG_PATH
    if not path:
        return
    try:
        with _log_lock:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            _rotate_audit_log(path)
            line = json.dumps(event, separators=(",", ":"), sort_keys=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        logger.debug("chirp_client: audit write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ChirpClient:
    """Synchronous UDP JSON client for one chirp daemon.

    Construct one per daemon (airband / ground).  Thread-safe: per-call
    socket creation + an internal lock serializing concurrent _send calls
    so concurrent dashboard handlers don't interleave datagrams.
    """

    def __init__(
        self,
        host: str = LOOPBACK,
        port: int = AIRBAND_PORT,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        name: str = "",
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.timeout = float(timeout)
        self.name = name or f"{host}:{self.port}"
        self._lock = threading.Lock()

    # -- low-level roundtrip ------------------------------------------------

    def _send(self, cmd: str, args: dict, *, timeout: float | None = None) -> dict:
        """Send one envelope, parse the reply.

        Returns the response ``data`` dict on ok.  Raises:
          - ChirpRejected on status='rejected'
          - ChirpDaemonError on status='error' or malformed reply
          - ChirpDaemonDown on socket-level failure (timeout, ECONNREFUSED)
        """
        req_id = uuid.uuid4().hex[:16]
        envelope = {"v": PROTOCOL_VERSION, "id": req_id, "cmd": cmd, "args": args}
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        eff_timeout = float(timeout) if timeout is not None else self.timeout
        t0 = time.monotonic()
        data: bytes = b""

        with self._lock:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.settimeout(eff_timeout)
                try:
                    sock.sendto(body, (self.host, self.port))
                except OSError as e:
                    self._audit(cmd, req_id, args, status="send_failed",
                                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                                error=str(e))
                    raise ChirpDaemonDown(f"sendto {self.name}: {e}") from e
                try:
                    data, _ = sock.recvfrom(65536)
                except socket.timeout as e:
                    self._audit(cmd, req_id, args, status="timeout",
                                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                                error=f"timeout {eff_timeout}s")
                    raise ChirpDaemonDown(
                        f"chirp {self.name} timeout after {eff_timeout}s "
                        f"(cmd={cmd})"
                    ) from e
                except OSError as e:
                    self._audit(cmd, req_id, args, status="recv_failed",
                                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                                error=str(e))
                    raise ChirpDaemonDown(
                        f"chirp {self.name} recv failed: {e}"
                    ) from e
            finally:
                sock.close()

        elapsed_ms = (time.monotonic() - t0) * 1000.0

        # Decode JSON reply
        try:
            resp = json.loads(data.decode("utf-8"))
            if not isinstance(resp, dict):
                raise ValueError("response is not a JSON object")
        except Exception as e:
            self._audit(cmd, req_id, args, status="decode_failed",
                        elapsed_ms=elapsed_ms, error=str(e))
            raise ChirpDaemonError(
                f"chirp {self.name} reply decode failed: {e}"
            ) from e

        status = resp.get("status")
        err = resp.get("error")
        data_block = resp.get("data") or {}
        self._audit(cmd, req_id, args, status=str(status or "?"),
                    elapsed_ms=elapsed_ms, error=err)

        if status == "ok":
            if not isinstance(data_block, dict):
                return {}
            return dict(data_block)
        if status == "rejected":
            raise ChirpRejected(str(err or "rejected"),
                                code=str(err or "rejected"), payload=resp)
        if status == "error":
            raise ChirpDaemonError(str(err or "error"))
        raise ChirpDaemonError(f"unknown response status: {status!r}")

    def _audit(self, cmd: str, req_id: str, args: dict, *,
               status: str, elapsed_ms: float, error: Any = None) -> None:
        evt = {
            "ts_ms": int(time.time() * 1000),
            "client": self.name,
            "cmd": cmd,
            "req_id": req_id,
            "args": args,
            "status": status,
            "elapsed_ms": round(float(elapsed_ms), 3),
        }
        if error:
            evt["error"] = str(error)
        _write_audit(evt)

    # -- high-level commands ------------------------------------------------

    def add_channel(
        self,
        id: str,
        freq_mhz: float,
        *,
        mode: str = "am",
        squelch_dbfs: float = -60.0,
        gain_db: float = 0.0,
        label: str | None = None,
    ) -> dict:
        """Add ONE channel.  See ``add_channels`` for batches."""
        args: dict[str, Any] = {
            "id": str(id),
            "freq_mhz": float(freq_mhz),
            "mode": str(mode),
            "squelch_dbfs": float(squelch_dbfs),
            "gain_db": float(gain_db),
        }
        if label is not None:
            args["label"] = str(label)
        return self._send("add_channel", args)

    def add_channels(self, channels: list[dict]) -> dict:
        """Add a batch.

        Chunks large channel lists so individual UDP packets stay under
        the chirp daemon's max_packet limit (default 4096 bytes).  The
        un-chunked single-envelope path serialises all channels into one
        JSON datagram, which trips the limit at roughly 13-15 ground
        marine channels with full labels (we hit this 2026-06-09 on
        fav-sic activation after a power cycle: "add_channels_failed:
        packet too large (5533 > 4096)").
        """
        import json as _json
        _MAX_PAYLOAD = 3500
        chans = list(channels)
        if not chans:
            return {"ok": True, "added_count": 0, "channels": []}
        try_one = _json.dumps({"channels": chans}, separators=(",", ":"))
        if len(try_one.encode("utf-8")) < _MAX_PAYLOAD:
            return self._send("add_channel", {"channels": chans})
        total_added = 0
        last_resp: dict = {}
        chunk: list = []
        for ch in chans:
            tentative = chunk + [ch]
            payload = _json.dumps({"channels": tentative}, separators=(",", ":"))
            if len(payload.encode("utf-8")) >= _MAX_PAYLOAD and chunk:
                resp = self._send("add_channel", {"channels": chunk})
                last_resp = resp
                total_added += int(resp.get("added_count", 0) or 0)
                chunk = [ch]
            else:
                chunk = tentative
        if chunk:
            resp = self._send("add_channel", {"channels": chunk})
            last_resp = resp
            total_added += int(resp.get("added_count", 0) or 0)
        return {**last_resp, "added_count": total_added}

    def remove_channel(self, id: str) -> dict:
        return self._send("remove_channel", {"id": str(id)})

    def set_squelch(self, id: str, dbfs: float) -> dict:
        return self._send("set_squelch", {"id": str(id), "dbfs": float(dbfs)})

    def set_vad_threshold(self, id: str, threshold: float) -> dict:
        """SB5 squelch redesign: set per-channel VAD score threshold (0-100)."""
        return self._send("set_vad_threshold", {"id": str(id), "threshold": float(threshold)})

    def set_vad_bypass(self, id: str, bypass: bool) -> dict:
        """SB5 squelch redesign: bypass the VAD gate (passthrough audio)."""
        return self._send("set_vad_bypass", {"id": str(id), "bypass": bool(bypass)})

    def set_freq(self, id: str, mhz: float) -> dict:
        return self._send("set_freq", {"id": str(id), "mhz": float(mhz)})

    def set_gain(self, id: str, db: float) -> dict:
        return self._send("set_gain", {"id": str(id), "db": float(db)})

    def set_master_gain(self, db: float) -> dict:
        return self._send("set_master_gain", {"db": float(db)})

    def set_sdr_gain(self, db: float) -> dict:
        return self._send("set_sdr_gain", {"db": float(db)})

    def reset(self) -> dict:
        """Park every channel + zero master gain + clear state file.

        Sub-second op — no SDR restart cascade.  This is the chirp-side
        analogue of ``safe_restart_rtl_airband(reset_radios)`` but vastly
        cheaper (no service restart, no SDRplay-daemon recovery).
        """
        return self._send("reset", {})

    def get_status(self) -> dict:
        """Snapshot of daemon state — channels, pool_free, icecast_state, etc."""
        return self._send("get_status", {})

    def is_alive(self, *, timeout: float | None = None) -> bool:
        """Quick liveness probe — used by ``/api/heartbeat`` when the
        feature flag is on.  Returns True iff the daemon answered
        get_status within ``timeout`` (default LIVENESS_TIMEOUT_SEC).

        Never raises.  Safe to call from latency-sensitive paths.
        """
        eff = float(timeout) if timeout is not None else LIVENESS_TIMEOUT_SEC
        try:
            self._send("get_status", {}, timeout=eff)
            return True
        except ChirpClientError:
            return False
        except Exception:
            logger.debug("chirp_client: is_alive unexpected", exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Module-level singletons (lazy)
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_airband_client: Optional[ChirpClient] = None
_ground_client: Optional[ChirpClient] = None


def get_airband_client() -> ChirpClient:
    """Lazy singleton for the airband daemon (port 7400).

    Constructed on first call; subsequent calls return the same instance.
    Safe to call when the feature flag is off — the constructor does NOT
    touch the socket.  No daemons are contacted until you call a method.
    """
    global _airband_client
    with _singleton_lock:
        if _airband_client is None:
            _airband_client = ChirpClient(
                host=LOOPBACK, port=AIRBAND_PORT, name="chirp-airband"
            )
        return _airband_client


def get_ground_client() -> ChirpClient:
    """Lazy singleton for the ground daemon (port 7401)."""
    global _ground_client
    with _singleton_lock:
        if _ground_client is None:
            _ground_client = ChirpClient(
                host=LOOPBACK, port=GROUND_PORT, name="chirp-ground"
            )
        return _ground_client


def reset_singletons() -> None:
    """Test hook — drops the singletons so a re-import picks up env changes."""
    global _airband_client, _ground_client
    with _singleton_lock:
        _airband_client = None
        _ground_client = None


def client_for_band(band: str) -> ChirpClient:
    """Convenience: 'airband' / 'air' -> airband client; 'ground' / 'gnd' -> ground client."""
    b = str(band or "").strip().lower()
    if b in ("airband", "air"):
        return get_airband_client()
    if b in ("ground", "gnd"):
        return get_ground_client()
    raise ValueError(f"unknown band for chirp client: {band!r}")


__all__ = [
    "PROTOCOL_VERSION",
    "AIRBAND_PORT",
    "GROUND_PORT",
    "DEFAULT_TIMEOUT_SEC",
    "LIVENESS_TIMEOUT_SEC",
    "FEATURE_FLAG_ENV",
    "ChirpClient",
    "ChirpClientError",
    "ChirpDaemonDown",
    "ChirpRejected",
    "ChirpDaemonError",
    "use_gr_demod",
    "get_airband_client",
    "get_ground_client",
    "client_for_band",
    "reset_singletons",
]
