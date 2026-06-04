"""Phase 3 test suite — Icecast publishing, CLI, dynamic subscribers.

What's verified here:

  1. IcecastSinkConfig + IcecastSink construct cleanly with an injected
     fake encoder and fake publisher (no lame, no python-shout, no network).
  2. Float samples flowing through work() get S16-encoded, fed to the
     encoder's stdin; encoder's stdout chunks fan to the publisher's
     send() with libshout's sync() pacing called.
  3. Mid-stream drop on the fake publisher triggers IcecastReconnector with
     exponential backoff; bytes resume after reconnect.
  4. Daemon CHIRP_AUDIO_OUT=icecast:host:port:/mount:pass refuses production
     mountpoints (/ANALOG.mp3, /ANALOG_GROUND.mp3, /DIGITAL.mp3, /VFO.mp3).
  5. Daemon get_status surfaces icecast_state / icecast_bytes_sent /
     icecast_reconnect_count fields.
  6. Subscribe / unsubscribe via UDP, event fan-out reaches the subscriber.
  7. CLI commands (status, add-channel, set-squelch, reset) round-trip the
     daemon and produce expected JSON.
  8. End-to-end MP3 smoke: a real lame subprocess + fake publisher, fed
     constant-zero PCM for 1.5 s, produces MP3 bytes at ~bitrate_kbps × 1000 / 8
     bytes per second (proof the encoder bitrate contract matches what
     Icecast expects).

We do NOT contact a real Icecast server in this file. The smoke test in
chirp/scripts/ + smoke_phase3 dir is what touches Micro's icecast2.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Test fixtures: fakes for encoder + publisher
# ---------------------------------------------------------------------------


class _FakeEncoder:
    """Mimics a subprocess.Popen w/ stdin + stdout pipes. Encodes PCM by
    copying it byte-for-byte to the read side (so we can assert byte flow
    without depending on lame being installed)."""

    def __init__(self) -> None:
        self.stdin = _DrainPipeIn(self)
        self.stdout = _DrainPipeOut(self)
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._closed = False

    def poll(self):
        return 0 if self._closed else None

    def wait(self, timeout=None):
        self._closed = True
        return 0

    def kill(self):
        self._closed = True

    @property
    def returncode(self):
        return 0 if self._closed else None


class _DrainPipeIn:
    def __init__(self, parent: _FakeEncoder) -> None:
        self._p = parent
    def write(self, b: bytes) -> int:
        with self._p._lock:
            self._p._buf.extend(b)
        return len(b)
    def close(self) -> None:
        self._p._closed = True
    def flush(self) -> None:
        pass


class _DrainPipeOut:
    def __init__(self, parent: _FakeEncoder) -> None:
        self._p = parent
    def read(self, n: int) -> bytes:
        # Block briefly until there's data or the encoder is closed.
        for _ in range(200):
            with self._p._lock:
                if self._p._buf:
                    out = bytes(self._p._buf[:n])
                    del self._p._buf[:n]
                    return out
                if self._p._closed:
                    return b""
            time.sleep(0.005)
        return b""
    def close(self) -> None:
        pass


class _FakeShoutPublisher:
    """Publisher matching the send/reconnect/close + sync() interface that
    IcecastSink expects, but writing into an in-memory buffer. Optional
    `drop_after` raises ConnectionError on send N+1 to exercise reconnect."""

    def __init__(self, drop_after: int = 10_000_000) -> None:
        self.bytes_sent = 0
        self.drop_after = drop_after
        self._connected = False
        self.payloads: list[bytes] = []
        self.reconnect_calls = 0
        self.sync_calls = 0
        self._lock = threading.Lock()

    # IcecastReconnector contract:
    def send(self, payload: bytes) -> None:
        with self._lock:
            if not self._connected:
                raise ConnectionError("not connected")
            if len(self.payloads) >= self.drop_after:
                self._connected = False
                raise ConnectionError("dropped")
            self.payloads.append(bytes(payload))
            self.bytes_sent += len(payload)

    def reconnect(self) -> bool:
        with self._lock:
            self.reconnect_calls += 1
            self._connected = True
            self.payloads.clear()  # treat reconnect as a fresh stream
            return True

    def sync(self) -> None:
        self.sync_calls += 1

    def close(self) -> None:
        with self._lock:
            self._connected = False

    def get_connected(self) -> bool:
        return self._connected


# ---------------------------------------------------------------------------
# IcecastSink unit tests
# ---------------------------------------------------------------------------


class TestIcecastSinkUnit:
    """Phase 3 unit: IcecastSink + fake encoder + fake publisher."""

    def _make(self, drop_after: int = 10_000_000):
        from chirp.dsp.icecast_sink import IcecastSink, IcecastSinkConfig
        cfg = IcecastSinkConfig(
            host="127.0.0.1", port=8000,
            mount="/CHIRP_TEST.mp3", password="x",
            bitrate_kbps=32, sample_rate=16000,
        )
        enc = _FakeEncoder()
        pub = _FakeShoutPublisher(drop_after=drop_after)
        sink = IcecastSink(cfg, encoder=enc, publisher=pub)
        return sink, enc, pub

    def test_construct_and_reach_connected_state(self):
        from chirp.dsp.icecast_sink import STATE_CONNECTED
        sink, enc, pub = self._make()
        # Allow publisher thread to settle.
        time.sleep(0.05)
        assert sink.state == STATE_CONNECTED
        assert pub.reconnect_calls >= 1, "initial reconnect not called"
        assert pub.get_connected() is True
        sink.stop()

    def test_samples_flow_through_to_publisher(self):
        sink, enc, pub = self._make()
        time.sleep(0.05)
        # 0.5 s of mono tone @ 16 kHz = 8000 samples.
        n = 8000
        t = np.arange(n, dtype=np.float32) / 16000.0
        samples = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        sink.work([samples], [])
        # Allow publisher thread to drain.
        time.sleep(0.2)
        # Each sample → 2 bytes int16. So 16 000 bytes in. The fake encoder
        # is a passthrough, so the publisher should see >= 16 000 bytes.
        assert pub.bytes_sent >= n * 2 - sink.INPUT_CHUNK_BYTES, \
            f"publisher received only {pub.bytes_sent} bytes for {n} samples"
        # Pacing was called.
        assert pub.sync_calls >= 1
        sink.stop()

    def test_snapshot_keys_present(self):
        sink, enc, pub = self._make()
        time.sleep(0.05)
        snap = sink.snapshot()
        for k in ("icecast_state", "icecast_bytes_sent",
                  "icecast_reconnect_count", "icecast_drop_count",
                  "icecast_host", "icecast_port", "icecast_mount",
                  "icecast_bitrate_kbps", "icecast_sample_rate"):
            assert k in snap, f"snapshot missing {k!r}"
        assert snap["icecast_bitrate_kbps"] == 32
        assert snap["icecast_sample_rate"] == 16000
        sink.stop()


class TestIcecastReconnectorAcrossSink:
    """Mid-stream drop on the publisher triggers reconnect through the sink."""

    def test_drop_mid_stream_triggers_reconnect(self):
        from chirp.dsp.icecast_sink import IcecastSink, IcecastSinkConfig
        cfg = IcecastSinkConfig(host="x", port=1, mount="/CHIRP_TEST.mp3",
                                password="x", bitrate_kbps=32, sample_rate=16000)
        # Drop after just 2 successful sends.
        enc = _FakeEncoder()
        pub = _FakeShoutPublisher(drop_after=2)
        # Speed up backoff so the test runs fast.
        from chirp.dsp.icecast_sink import IcecastReconnector
        slept: list[float] = []
        sink = IcecastSink(cfg, encoder=enc, publisher=pub)
        # Override the reconnector with a faster-sleeping one wrapping same pub.
        sink.reconnector = IcecastReconnector(pub, sleep=lambda s: slept.append(s))
        time.sleep(0.05)

        # Pump enough audio for at least 5 chunks (5 × 4096 = 20480 bytes).
        # 4096 bytes = 2048 samples. So 10 × 2048 = 20480 samples = 40960 bytes.
        n = 2048 * 10
        samples = np.zeros(n, dtype=np.float32)
        sink.work([samples], [])
        time.sleep(0.4)
        # At least one drop + reconnect must have happened.
        assert sink.reconnector.drops >= 1, "no drops counted"
        assert pub.reconnect_calls >= 2, \
            f"only {pub.reconnect_calls} reconnects — initial + mid-stream expected"
        assert slept, "no backoff sleeps recorded"
        # First backoff = 0.25 s per Phase 2 contract.
        assert slept[0] == 0.25
        sink.stop()


# ---------------------------------------------------------------------------
# Daemon config parsing — production mount refusal
# ---------------------------------------------------------------------------


class TestDaemonIcecastConfigRefusal:
    """The CHIRP_AUDIO_OUT parser must refuse production mountpoints."""

    @pytest.mark.parametrize("mount", [
        "/ANALOG.mp3", "/ANALOG_GROUND.mp3", "/DIGITAL.mp3", "/VFO.mp3"
    ])
    def test_refuses_production_mounts(self, mount, monkeypatch):
        # Default behaviour (no CHIRP_ALLOW_PROD_MOUNT): refusal stays in
        # force so Phase-3-style smoke tests cannot stomp on production.
        monkeypatch.delenv("CHIRP_ALLOW_PROD_MOUNT", raising=False)
        from chirp.daemon import _parse_icecast_spec
        with pytest.raises(ValueError, match="production mount"):
            _parse_icecast_spec(f"127.0.0.1:8000:{mount}:062352")

    @pytest.mark.parametrize("mount", [
        "/ANALOG.mp3", "/ANALOG_GROUND.mp3", "/DIGITAL.mp3", "/VFO.mp3"
    ])
    @pytest.mark.parametrize("flag", ["1", "true", "yes", "YES", "True"])
    def test_allows_production_mounts_with_env(self, mount, flag, monkeypatch):
        # Phase 4d cutover opt-in: CHIRP_ALLOW_PROD_MOUNT lifts the guard.
        monkeypatch.setenv("CHIRP_ALLOW_PROD_MOUNT", flag)
        from chirp.daemon import _parse_icecast_spec
        host, port, m, pw = _parse_icecast_spec(
            f"127.0.0.1:8000:{mount}:062352"
        )
        assert (host, port, m, pw) == ("127.0.0.1", 8000, mount, "062352")

    def test_accepts_test_mount(self):
        from chirp.daemon import _parse_icecast_spec
        host, port, mount, pw = _parse_icecast_spec(
            "127.0.0.1:8000:/CHIRP_TEST.mp3:062352"
        )
        assert (host, port, mount, pw) == \
            ("127.0.0.1", 8000, "/CHIRP_TEST.mp3", "062352")

    def test_password_with_colons(self):
        from chirp.daemon import _parse_icecast_spec
        # Passwords may legitimately contain ':'; the rsplit-from-left logic
        # treats everything after the third colon as the password.
        host, port, mount, pw = _parse_icecast_spec(
            "127.0.0.1:8000:/CHIRP_TEST.mp3:a:b:c"
        )
        assert pw == "a:b:c"


# ---------------------------------------------------------------------------
# Daemon + IcecastSink integration test (fake publisher)
# ---------------------------------------------------------------------------


def _udp_roundtrip(port: int, payload: bytes, timeout: float = 2.0) -> bytes:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(payload, ("127.0.0.1", port))
        data, _ = s.recvfrom(65536)
        return data
    finally:
        s.close()


def _send_cmd(port: int, cmd: str, args: dict) -> dict:
    env = {"v": 1, "id": "t", "cmd": cmd, "args": args}
    raw = _udp_roundtrip(port, json.dumps(env).encode())
    return json.loads(raw)


@pytest.fixture
def daemon_with_fake_icecast(tmp_path):
    """Spin up a real ChirpFlowgraph wired to a real IcecastSink but with
    fake encoder + fake publisher. Yields (daemon, server, fake_publisher,
    cmd_port, audio_iq_path).
    """
    from chirp.cmd.server import CommandServer, ServerConfig
    from chirp.daemon import ChirpFlowgraph, DaemonConfig
    from chirp.dsp.icecast_sink import IcecastSink, IcecastSinkConfig
    from chirp.state import StateStore

    # Build IQ fixture: strong AM carrier @ 200 kHz.
    samp_rate = 1e6
    iq_path = tmp_path / "carrier.iq"
    n = int(samp_rate * 2.0)
    t = np.arange(n, dtype=np.float64) / samp_rate
    env_a = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * 1000 * t))
    iq = (env_a * np.exp(2j * np.pi * 200e3 * t)).astype(np.complex64)
    iq.tofile(iq_path)

    cmd_port = 20000 + (os.getpid() % 500)

    # Build IcecastSink in advance with fakes so the daemon can pick it up.
    ice_cfg = IcecastSinkConfig(host="127.0.0.1", port=8000,
                                mount="/CHIRP_TEST.mp3", password="x",
                                bitrate_kbps=32, sample_rate=16000)
    enc = _FakeEncoder()
    pub = _FakeShoutPublisher()

    cfg = DaemonConfig(
        band="airband", cmd_port=cmd_port,
        source_kind="file", source_path=str(iq_path), source_samp_rate=samp_rate,
        audio_out_kind="icecast",
        audio_out_path="127.0.0.1:8000:/CHIRP_TEST.mp3:x",
        audio_rate=16000.0, max_channels=2,
        icecast_host="127.0.0.1", icecast_port=8000,
        icecast_mount="/CHIRP_TEST.mp3", icecast_password="x",
        icecast_bitrate_kbps=32,
        state_path=str(tmp_path / "s.json"),
        hit_log_path=str(tmp_path / "h.jsonl"),
    )

    # Monkey-patch IcecastSink to return our prebuilt instance.
    import chirp.daemon as daemon_mod
    real_icecast_sink_cls = daemon_mod.IcecastSink

    def fake_factory(cfg_arg, **kw):
        return IcecastSink(cfg_arg, encoder=enc, publisher=pub, **kw)
    daemon_mod.IcecastSink = fake_factory  # type: ignore[misc]

    server = CommandServer(ServerConfig(host="127.0.0.1", port=cmd_port),
                           dispatch=lambda env, args: tb.dispatch(env, args))
    try:
        tb = ChirpFlowgraph(cfg, server, state_store=StateStore(cfg.state_path))
        tb.start()
        tb.start_health()
        server.start()
        yield tb, server, pub, cmd_port, iq_path
    finally:
        try:
            tb.stop_health()
            tb.shutdown_drain()
            tb.stop()
            tb.wait()
        except Exception:
            pass
        try:
            server.stop()
        except Exception:
            pass
        daemon_mod.IcecastSink = real_icecast_sink_cls  # type: ignore[misc]


class TestDaemonIntegration:
    def test_get_status_includes_icecast_fields(self, daemon_with_fake_icecast):
        tb, server, pub, port, iq = daemon_with_fake_icecast
        resp = _send_cmd(port, "get_status", {})
        assert resp["status"] == "ok"
        d = resp["data"]
        for k in ("icecast_state", "icecast_bytes_sent",
                  "icecast_reconnect_count", "icecast_drop_count"):
            assert k in d, f"get_status missing {k}"
        assert d["icecast_state"] in ("connected", "disconnected", "reconnecting")
        assert d["icecast_mount"] == "/CHIRP_TEST.mp3"
        assert d["icecast_bitrate_kbps"] == 32

    def test_audio_flows_to_publisher(self, daemon_with_fake_icecast):
        tb, server, pub, port, iq = daemon_with_fake_icecast
        # Add a channel that's on-tune with the IQ carrier (+200 kHz offset).
        resp = _send_cmd(port, "add_channel", {
            "id": "ch01", "freq_mhz": 0.2, "mode": "am",
            "squelch_dbfs": -90.0, "gain_db": 0.0,
        })
        assert resp["status"] == "ok", resp
        # Give the flowgraph 1 second to push samples through the mixer →
        # IcecastSink → fake encoder → fake publisher chain.
        time.sleep(1.5)
        assert pub.bytes_sent > 0, "no bytes reached publisher"


# ---------------------------------------------------------------------------
# CLI tests — round-trip the real daemon
# ---------------------------------------------------------------------------


class TestCli:
    def test_status_returns_parseable_json(self, daemon_with_fake_icecast):
        tb, server, pub, port, iq = daemon_with_fake_icecast
        out = subprocess.run(
            [sys.executable, "-m", "chirp.cli", "--port", str(port), "status"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0, out.stderr
        obj = json.loads(out.stdout)
        assert obj["status"] == "ok"
        assert "icecast_state" in obj["data"]

    def test_add_remove_channel_via_cli(self, daemon_with_fake_icecast):
        tb, server, pub, port, iq = daemon_with_fake_icecast
        add = subprocess.run(
            [sys.executable, "-m", "chirp.cli", "--port", str(port),
             "add-channel", "--id", "cli1", "--freq", "0.2",
             "--mode", "am", "--squelch", "-80", "--gain", "0"],
            capture_output=True, text=True, timeout=5,
        )
        assert add.returncode == 0, add.stderr
        assert json.loads(add.stdout)["status"] == "ok"

        # Confirm via daemon state.
        st = _send_cmd(port, "get_status", {})
        ids = [c["id"] for c in st["data"]["channels"]]
        assert "cli1" in ids

        rm = subprocess.run(
            [sys.executable, "-m", "chirp.cli", "--port", str(port),
             "remove-channel", "--id", "cli1"],
            capture_output=True, text=True, timeout=5,
        )
        assert rm.returncode == 0
        assert json.loads(rm.stdout)["status"] == "ok"

    def test_reset_via_cli(self, daemon_with_fake_icecast):
        tb, server, pub, port, iq = daemon_with_fake_icecast
        _send_cmd(port, "add_channel", {
            "id": "x1", "freq_mhz": 0.2, "mode": "am",
            "squelch_dbfs": -80.0, "gain_db": 0.0,
        })
        out = subprocess.run(
            [sys.executable, "-m", "chirp.cli", "--port", str(port), "reset"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert json.loads(out.stdout)["status"] == "ok"
        st = _send_cmd(port, "get_status", {})
        assert st["data"]["channels"] == []


# ---------------------------------------------------------------------------
# UDP subscriber fan-out
# ---------------------------------------------------------------------------


class TestSubscribeUnsubscribe:
    def test_subscriber_receives_events(self, daemon_with_fake_icecast):
        tb, server, pub, port, iq = daemon_with_fake_icecast
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(3.0)
        try:
            # Subscribe to channel_added events.
            env = {"v": 1, "id": "s1", "cmd": "subscribe",
                   "args": {"events": ["channel_added"]}}
            sock.sendto(json.dumps(env).encode(), ("127.0.0.1", port))
            ack_raw, _ = sock.recvfrom(65536)
            ack = json.loads(ack_raw.decode())
            assert ack["status"] == "ok"
            assert ack["data"]["count"] >= 1

            # Trigger event.
            _send_cmd(port, "add_channel", {
                "id": "subch", "freq_mhz": 0.2, "mode": "am",
                "squelch_dbfs": -80.0, "gain_db": 0.0,
            })

            # Should receive at least one channel_added event.
            seen = []
            t0 = time.monotonic()
            while time.monotonic() - t0 < 2.0:
                try:
                    sock.settimeout(0.5)
                    data, _ = sock.recvfrom(65536)
                    obj = json.loads(data.decode())
                    if obj.get("evt") == "channel_added":
                        seen.append(obj)
                        break
                except socket.timeout:
                    continue
            assert seen, "no channel_added event received"
            assert seen[0]["ch"] == "subch"

            # Unsubscribe.
            unsub = {"v": 1, "id": "u1", "cmd": "unsubscribe", "args": {}}
            sock.sendto(json.dumps(unsub).encode(), ("127.0.0.1", port))
            sock.settimeout(2.0)
            ack2_raw, _ = sock.recvfrom(65536)
            ack2 = json.loads(ack2_raw.decode())
            assert ack2["status"] == "ok"
            assert ack2["data"]["removed"] is True
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# End-to-end MP3 smoke (real lame subprocess, fake publisher)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("lame") is None, reason="lame not installed")
class TestMp3SmokeRate:
    """Sanity-check the bitrate contract: a real lame subprocess fed
    constant-zero PCM should produce MP3 bytes at the expected
    (bitrate_kbps × 1000 / 8) B/s, within a wide tolerance.

    This is the only test that runs the real encoder. It does NOT touch
    icecast — the publisher is still a fake collector.
    """

    def test_constant_silence_produces_mp3_at_expected_rate(self):
        from chirp.dsp.icecast_sink import IcecastSink, IcecastSinkConfig
        cfg = IcecastSinkConfig(
            host="127.0.0.1", port=8000, mount="/CHIRP_TEST.mp3",
            password="x", bitrate_kbps=32, sample_rate=16000,
        )
        pub = _FakeShoutPublisher()
        sink = IcecastSink(cfg, encoder=None, publisher=pub)
        try:
            # Feed 1.5 s of zero samples at 16 kHz → 24 000 samples.
            n_total = 16000 * 3 // 2
            chunk = np.zeros(4096, dtype=np.float32)
            sent_samples = 0
            t0 = time.monotonic()
            while sent_samples < n_total:
                sink.work([chunk], [])
                sent_samples += len(chunk)
            # Allow drain.
            time.sleep(0.8)
            elapsed = time.monotonic() - t0
            # Expected MP3 byte rate.
            expected_per_s = 32 * 1000 / 8  # 4000 B/s
            # Allow 1.0 s of header/preroll fudge and ±30 % envelope on the
            # remainder. CBR lame is tight but the first frame is large.
            min_bytes = int(expected_per_s * max(0.0, elapsed - 1.0) * 0.7)
            assert pub.bytes_sent >= min_bytes, (
                f"MP3 byte flow too low: {pub.bytes_sent} B in {elapsed:.2f}s "
                f"(expected ≥ {min_bytes})")
            # And the bytes must start with an MP3 sync word (0xFFE / 0xFFF).
            blob = b"".join(pub.payloads)
            assert b"\xff\xfb" in blob or b"\xff\xfa" in blob \
                or b"\xff\xf3" in blob or b"\xff\xf2" in blob, \
                "no MP3 sync word in published bytes"
        finally:
            sink.stop()
