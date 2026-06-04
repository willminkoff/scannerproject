"""Unit tests for ui/chirp_client.py.

Spins up a real UDP mock server (single-threaded asyncio in a worker
thread) so we exercise the actual socket round-trip — not just the
JSON encoder.  Every command in the chirp/cmd/schema.py command table
gets a round-trip test that asserts the wire shape the daemon receives.
"""
from __future__ import annotations

import importlib
import json
import os
import socket
import threading
import time

import pytest


# Tests live in chirp/tests/ on Micro; we need ui on sys.path.
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # chirp/tests/.. = chirp; ..= repo
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# --- mock UDP server --------------------------------------------------------


class MockChirpDaemon:
    """In-process UDP mock that records requests and replies per a fixture map.

    Threading: one background thread runs a blocking recvfrom loop.  Stop
    is signalled by sending a sentinel datagram from start()/stop().
    """

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))  # ephemeral port
        self.port = self.sock.getsockname()[1]
        # Map cmd -> response dict (status/data/error). None means "use ok".
        self.responses: dict[str, dict] = {}
        # Recorded requests: list of (cmd, envelope dict).
        self.received: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        # Misbehavior toggles for negative-path tests:
        self.return_malformed = False
        self.drop_replies = False
        self.delay_sec = 0.0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Wake up the recv loop by sending a sentinel datagram.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(b"\x00", ("127.0.0.1", self.port))
            s.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        try:
            self.sock.close()
        except OSError:
            pass

    def _loop(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if self._stop.is_set():
                return
            if not data or data == b"\x00":
                continue
            try:
                env = json.loads(data.decode("utf-8"))
            except Exception:
                # Don't bother replying — let the client time out.
                continue
            self.received.append(env)
            if self.delay_sec > 0:
                time.sleep(self.delay_sec)
            if self.drop_replies:
                continue
            cmd = env.get("cmd")
            req_id = env.get("id")
            spec = self.responses.get(cmd, {"status": "ok", "data": {}})
            reply = {
                "v": 1, "id": req_id,
                "status": spec.get("status", "ok"),
                "data": spec.get("data"),
                "error": spec.get("error"),
            }
            if self.return_malformed:
                self.sock.sendto(b"not json{{{", addr)
                continue
            self.sock.sendto(json.dumps(reply).encode("utf-8"), addr)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def mock_daemon():
    d = MockChirpDaemon()
    d.start()
    try:
        yield d
    finally:
        d.stop()


@pytest.fixture()
def client_for(monkeypatch, tmp_path):
    """Returns a factory that builds a ChirpClient pointed at the mock and
    redirects the audit log into tmp_path so we don't pollute ~/.cache."""
    log_path = tmp_path / "chirp_client.jsonl"
    monkeypatch.setenv("CHIRP_CLIENT_LOG_PATH", str(log_path))
    # Force re-import so module-level LOG_PATH picks up the env.
    import ui.chirp_client as cc
    importlib.reload(cc)
    cc.reset_singletons()

    def _make(port: int, **kw):
        return cc.ChirpClient(host="127.0.0.1", port=port, name="test", **kw)

    yield _make, cc, log_path
    cc.reset_singletons()


# --- envelope shape tests ---------------------------------------------------


def test_envelope_shape_add_channel(mock_daemon, client_for):
    make, cc, log_path = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["add_channel"] = {
        "status": "ok",
        "data": {"slot": 0, "audio_path": "/tmp/a.f32", "added": [{"id": "T1", "slot": 0, "freq_mhz": 121.5}]},
    }
    out = c.add_channel("T1", 121.5, mode="am", squelch_dbfs=-60, gain_db=10, label="Tower")
    assert out["slot"] == 0
    assert mock_daemon.received[0]["cmd"] == "add_channel"
    assert mock_daemon.received[0]["v"] == 1
    assert mock_daemon.received[0]["args"] == {
        "id": "T1", "freq_mhz": 121.5, "mode": "am",
        "squelch_dbfs": -60.0, "gain_db": 10.0, "label": "Tower",
    }


def test_envelope_shape_add_channels_batch(mock_daemon, client_for):
    make, cc, log_path = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["add_channel"] = {"status": "ok", "data": {"count": 2}}
    out = c.add_channels([
        {"id": "A", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -60, "gain_db": 0},
        {"id": "B", "freq_mhz": 122.0, "mode": "am", "squelch_dbfs": -55, "gain_db": 0},
    ])
    assert out["count"] == 2
    args = mock_daemon.received[0]["args"]
    assert list(args.keys()) == ["channels"]
    assert len(args["channels"]) == 2


def test_remove_channel(mock_daemon, client_for):
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["remove_channel"] = {"status": "ok", "data": {"slot": 3}}
    c.remove_channel("ZZZ")
    assert mock_daemon.received[0]["cmd"] == "remove_channel"
    assert mock_daemon.received[0]["args"] == {"id": "ZZZ"}


def test_set_squelch(mock_daemon, client_for):
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_squelch"] = {"status": "ok", "data": {"dbfs": -42.0}}
    out = c.set_squelch("ch7", -42)
    assert out["dbfs"] == -42.0
    assert mock_daemon.received[0]["cmd"] == "set_squelch"
    assert mock_daemon.received[0]["args"] == {"id": "ch7", "dbfs": -42.0}


def test_set_freq(mock_daemon, client_for):
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_freq"] = {"status": "ok", "data": {"mhz": 138.05}}
    c.set_freq("ch7", 138.05)
    assert mock_daemon.received[0]["args"] == {"id": "ch7", "mhz": 138.05}


def test_set_gain(mock_daemon, client_for):
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_gain"] = {"status": "ok", "data": {"db": 32.8}}
    c.set_gain("ch7", 32.8)
    assert mock_daemon.received[0]["args"] == {"id": "ch7", "db": 32.8}


def test_set_master_gain(mock_daemon, client_for):
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_master_gain"] = {"status": "ok", "data": {"db": 6.0}}
    c.set_master_gain(6.0)
    assert mock_daemon.received[0]["args"] == {"db": 6.0}


def test_reset(mock_daemon, client_for):
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["reset"] = {"status": "ok", "data": {"removed": ["A", "B"], "pool_free": 32}}
    out = c.reset()
    assert out["pool_free"] == 32
    assert mock_daemon.received[0]["cmd"] == "reset"
    assert mock_daemon.received[0]["args"] == {}


def test_get_status(mock_daemon, client_for):
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["get_status"] = {"status": "ok", "data": {
        "version": 1, "band": "airband", "pool_free": 28, "channels": [],
        "icecast_state": "connected", "icecast_bytes_sent": 12345,
    }}
    out = c.get_status()
    assert out["band"] == "airband"
    assert out["icecast_state"] == "connected"
    assert mock_daemon.received[0]["cmd"] == "get_status"


# --- error-path tests -------------------------------------------------------


def test_rejected_status_raises(mock_daemon, client_for):
    make, cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_squelch"] = {"status": "rejected", "error": "unknown channel: xyz"}
    with pytest.raises(cc.ChirpRejected) as ei:
        c.set_squelch("xyz", -50)
    assert "unknown channel" in str(ei.value)
    assert ei.value.code == "unknown channel: xyz"


def test_error_status_raises(mock_daemon, client_for):
    make, cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_squelch"] = {"status": "error", "error": "internal: ZeroDivision"}
    with pytest.raises(cc.ChirpDaemonError):
        c.set_squelch("ch1", -50)


def test_timeout_raises_daemon_down(client_for):
    """No mock listening at all -> timeout -> ChirpDaemonDown."""
    make, cc, _ = client_for
    # Find a port the kernel isn't using.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    c = make(free_port, timeout=0.3)
    with pytest.raises(cc.ChirpDaemonDown):
        c.get_status()


def test_drop_replies_times_out(mock_daemon, client_for):
    make, cc, _ = client_for
    c = make(mock_daemon.port, timeout=0.5)
    mock_daemon.drop_replies = True
    with pytest.raises(cc.ChirpDaemonDown):
        c.get_status()


def test_malformed_reply_raises_daemon_error(mock_daemon, client_for):
    make, cc, _ = client_for
    c = make(mock_daemon.port, timeout=0.5)
    mock_daemon.return_malformed = True
    with pytest.raises(cc.ChirpDaemonError):
        c.get_status()


def test_noise_floor_not_warm_carries_code(mock_daemon, client_for):
    """Critical 409 path — operator preset click during rtl-airband warmup."""
    make, cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_squelch"] = {
        "status": "rejected", "error": "noise_floor_not_warm",
    }
    with pytest.raises(cc.ChirpRejected) as ei:
        c.set_squelch("ch1", -40)
    assert ei.value.code == "noise_floor_not_warm"


# --- liveness probe ---------------------------------------------------------


def test_is_alive_true_when_responds(mock_daemon, client_for):
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["get_status"] = {"status": "ok", "data": {"version": 1}}
    assert c.is_alive() is True


def test_is_alive_false_when_no_daemon(client_for):
    make, _cc, _ = client_for
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    c = make(free_port, timeout=0.2)
    assert c.is_alive(timeout=0.2) is False


def test_is_alive_false_on_daemon_error(mock_daemon, client_for):
    """A daemon that replies but errors should be 'not alive' for heartbeat."""
    make, _cc, _ = client_for
    c = make(mock_daemon.port)
    # is_alive treats any ChirpClientError as "not alive" so even an internal
    # error indicates the daemon is too broken to consider OK.
    mock_daemon.responses["get_status"] = {"status": "error", "error": "internal: foo"}
    assert c.is_alive() is False


# --- audit log --------------------------------------------------------------


def test_audit_log_records_each_call(mock_daemon, client_for):
    make, cc, log_path = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_squelch"] = {"status": "ok", "data": {"dbfs": -50}}
    mock_daemon.responses["reset"] = {"status": "ok", "data": {"pool_free": 32}}
    c.set_squelch("a", -50)
    c.set_squelch("b", -60)
    c.reset()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(x) for x in lines]
    cmds = [p["cmd"] for p in parsed]
    assert cmds == ["set_squelch", "set_squelch", "reset"]
    for p in parsed:
        assert "ts_ms" in p and "req_id" in p and "elapsed_ms" in p
        assert p["status"] == "ok"


def test_audit_log_records_error(mock_daemon, client_for):
    make, cc, log_path = client_for
    c = make(mock_daemon.port)
    mock_daemon.responses["set_squelch"] = {"status": "rejected", "error": "unknown channel: x"}
    try:
        c.set_squelch("x", -50)
    except cc.ChirpRejected:
        pass
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["status"] == "rejected"
    assert rec["error"] == "unknown channel: x"


# --- feature flag -----------------------------------------------------------


def test_use_gr_demod_default_off(monkeypatch):
    monkeypatch.delenv("SB5_USE_GR_DEMOD", raising=False)
    import ui.chirp_client as cc
    importlib.reload(cc)
    assert cc.use_gr_demod() is False


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True),
    ("false", False), ("0", False), ("no", False), ("", False), ("nope", False),
])
def test_use_gr_demod_parses(monkeypatch, value, expected):
    monkeypatch.setenv("SB5_USE_GR_DEMOD", value)
    import ui.chirp_client as cc
    importlib.reload(cc)
    assert cc.use_gr_demod() is expected


# --- singletons -------------------------------------------------------------


def test_get_airband_and_ground_clients_are_distinct(client_for):
    _make, cc, _ = client_for
    a = cc.get_airband_client()
    g = cc.get_ground_client()
    assert a is not g
    assert a.port == cc.AIRBAND_PORT
    assert g.port == cc.GROUND_PORT


def test_singletons_are_stable(client_for):
    _make, cc, _ = client_for
    a1 = cc.get_airband_client()
    a2 = cc.get_airband_client()
    assert a1 is a2


def test_client_for_band(client_for):
    _make, cc, _ = client_for
    assert cc.client_for_band("airband").port == cc.AIRBAND_PORT
    assert cc.client_for_band("air").port == cc.AIRBAND_PORT
    assert cc.client_for_band("ground").port == cc.GROUND_PORT
    assert cc.client_for_band("gnd").port == cc.GROUND_PORT
    with pytest.raises(ValueError):
        cc.client_for_band("digital")


# --- protocol_version constant ---------------------------------------------


def test_protocol_version_matches_chirp_schema():
    """The wire-protocol constant must match chirp/cmd/schema.py."""
    import ui.chirp_client as cc
    from chirp.cmd.schema import PROTOCOL_VERSION as SERVER_V
    assert cc.PROTOCOL_VERSION == SERVER_V
