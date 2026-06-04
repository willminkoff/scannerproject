"""Phase 2 radio-bug regression tests.

Will hit four production rtl-airband bugs on 2026-06-03. Each fixture below
encodes the bug as a property test that asserts "chirp cannot reproduce this."

The bugs:

  1. Squelch poison value — rtl-airband's noise-floor estimator initialises
     to a magic constant (-10.964 dBFS) for the first ~5 seconds. If anything
     consults the noise floor during warmup (e.g. an auto-squelch helper),
     it reads the poison value and behaves as though the band is suspiciously
     loud, slamming the squelch closed. Chirp must NOT consult any
     uninitialised noise estimator when computing or applying a squelch value.

  2. SDRplay master/slave wedge on restart — production observation: when the
     daemon was SIGKILL'd while a control-plane setter was in flight, the
     SDRplay driver's master/slave handshake hung on next start. Property
     under test: chirp's shutdown drains the command queue before stopping
     the flowgraph (no setter is mid-call when stop() is invoked).

  3. libshout drop without reconnect — the previous Icecast publisher would
     silently fail and stop reconnecting. Property under test: a stub Icecast
     sink that fails on send is detected, logged, and the daemon attempts
     reconnect with exponential backoff. (Real libshout integration is Phase 3;
     this is the contract the Phase-3 module will implement.)

  4. Noise-floor init race — if set_squelch is called while the channel is
     < 1 s old, the rtl-airband path would consult the not-yet-converged
     noise estimate and apply a nonsense threshold (or silently silence the
     band). Property under test: set_squelch on a fresh channel applies the
     operator's value EXACTLY, regardless of channel age.

Each test name maps 1:1 to the bug taxonomy.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from chirp.cmd.server import CommandServer, ServerConfig
from chirp.daemon import ChirpFlowgraph, DaemonConfig, PARKED_SQUELCH_DBFS
from chirp.dsp.channel import Channel
from chirp.hit_detector import HitDetector
from chirp.state import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _udp_roundtrip(port: int, payload: bytes, timeout: float = 2.0) -> bytes:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(payload, ("127.0.0.1", port))
        data, _ = s.recvfrom(8192)
        return data
    finally:
        s.close()


def _write_quiet_iq(path, samp_rate=1e6, duration_s=2.0, noise_sigma=0.001):
    """Pure noise — no carrier. Used to test set_squelch behaviour without
    actually opening a channel."""
    n = int(samp_rate * duration_s)
    rng = np.random.default_rng(13)
    iq = (rng.normal(0, noise_sigma, n) + 1j * rng.normal(0, noise_sigma, n)).astype(np.complex64)
    iq.tofile(path)


# ---------------------------------------------------------------------------
# Bug 1: Squelch poison value
# ---------------------------------------------------------------------------


# The rtl-airband poison value as observed in production logs.
POISON_DBFS = -10.964


class TestBug1SquelchPoisonValue:
    """Regression: noise-floor estimator's initial poison value must never
    flow into the squelch threshold.
    """

    def test_channel_squelch_is_what_operator_asked_for(self, tmp_path):
        """Direct property test: a freshly constructed Channel reports the
        squelch threshold the constructor was given. There is no path that
        causes the noise estimator to override it."""
        ch = Channel(samp_rate=1e6, squelch_dbfs=-60.0)
        assert ch.squelch_dbfs == -60.0
        # And after set_squelch:
        ch.set_squelch(-72.5)
        assert ch.squelch_dbfs == -72.5

    def test_channel_signal_level_at_construction_is_floor_not_poison(self):
        """The signal-level probe must report -120 dBFS (our explicit floor),
        NOT -10.964 dBFS (the rtl-airband poison). At construction time the
        probe has seen no samples yet."""
        ch = Channel(samp_rate=1e6, squelch_dbfs=-60.0)
        lvl = ch.get_signal_level_dbfs()
        assert lvl == pytest.approx(-120.0, abs=0.5), (
            f"fresh channel reported {lvl} dBFS — must be at floor, not poison"
        )
        # The rtl-airband poison value must NOT be what we get.
        assert abs(lvl - POISON_DBFS) > 50, "level startup value resembles poison"

    def test_chirp_codebase_does_not_contain_the_poison_constant(self):
        """Belt and braces: no source file under chirp/ contains the literal
        rtl-airband poison value. If somebody ports rtl-airband code in
        verbatim, this test catches it."""
        root = Path(__file__).resolve().parent.parent  # chirp/
        offenders = []
        for py in root.rglob("*.py"):
            if "tests" in py.parts:
                continue  # this file IS allowed to contain it
            text = py.read_text(encoding="utf-8", errors="ignore")
            if "-10.964" in text or "10.964" in text:
                offenders.append(str(py.relative_to(root.parent)))
        assert not offenders, f"poison constant found in: {offenders}"


# ---------------------------------------------------------------------------
# Bug 2: Master/slave shutdown wedge
# ---------------------------------------------------------------------------


class TestBug2ShutdownDrainsCommandQueue:
    """Regression: chirp's daemon shutdown drains any in-flight setter before
    stopping the flowgraph. If we ever SIGKILL mid-setter we lose the property,
    but the SIGTERM path (which is what production uses) must drain."""

    def _build_min_daemon(self, tmp_path, n_slots=2):
        samp_rate = 1e6
        iq_path = tmp_path / "q.iq"
        _write_quiet_iq(iq_path, samp_rate, 1.0)
        cfg = DaemonConfig(
            band="airband",
            cmd_port=19400 + (os.getpid() % 400),
            source_kind="file",
            source_path=str(iq_path),
            source_samp_rate=samp_rate,
            audio_out_kind="file",
            audio_out_path=str(tmp_path / "a.f32"),
            audio_rate=16000.0,
            max_channels=n_slots,
            state_path=str(tmp_path / "s.json"),
            hit_log_path=str(tmp_path / "h.jsonl"),
        )
        server = CommandServer(
            ServerConfig(host=cfg.cmd_host, port=cfg.cmd_port),
            dispatch=lambda env, args: tb.dispatch(env, args),
        )
        tb = ChirpFlowgraph(cfg, server, state_store=StateStore(cfg.state_path))
        return cfg, tb, server

    def test_shutdown_drain_waits_for_lock(self, tmp_path):
        """While another thread holds the dispatch lock (simulating a setter
        in flight), shutdown_drain blocks until that thread releases. We
        assert: drain takes at LEAST as long as the held window."""
        cfg, tb, server = self._build_min_daemon(tmp_path)
        tb.start()
        tb.start_health()
        server.start()
        try:
            held = threading.Event()
            release_at = threading.Event()

            def holder():
                with tb._lock:
                    held.set()
                    release_at.wait(timeout=1.0)

            t = threading.Thread(target=holder, daemon=True)
            t.start()
            assert held.wait(timeout=1.0)
            t0 = time.monotonic()
            # Drain in a thread so we can release after 200 ms.
            drain_done = threading.Event()

            def drain():
                tb.shutdown_drain(drain_timeout=2.0)
                drain_done.set()

            dt = threading.Thread(target=drain, daemon=True)
            dt.start()
            time.sleep(0.2)
            assert not drain_done.is_set(), "drain returned while lock was held"
            release_at.set()
            assert drain_done.wait(timeout=2.0), "drain never completed after lock released"
            elapsed = time.monotonic() - t0
            assert elapsed >= 0.15
            t.join(timeout=1.0)
        finally:
            tb.stop_health()
            tb.shutdown_drain()
            tb.stop()
            tb.wait()
            server.stop()

    def test_shutdown_drain_timeout_is_bounded(self, tmp_path):
        """If a setter wedges forever, shutdown_drain must time out instead
        of hanging the process. Property: returns within drain_timeout +
        small slack, even if the lock is never released."""
        cfg, tb, server = self._build_min_daemon(tmp_path)
        tb.start()
        tb.start_health()
        server.start()
        try:
            stop_holder = threading.Event()

            def forever_holder():
                with tb._lock:
                    stop_holder.wait(timeout=5.0)

            t = threading.Thread(target=forever_holder, daemon=True)
            t.start()
            time.sleep(0.05)
            t0 = time.monotonic()
            tb.shutdown_drain(drain_timeout=0.3)
            elapsed = time.monotonic() - t0
            assert 0.25 <= elapsed < 1.0, f"drain took {elapsed:.2f}s; expected ~0.3s"
            stop_holder.set()
            t.join(timeout=1.0)
        finally:
            tb.stop_health()
            tb.stop()
            tb.wait()
            server.stop()

    def test_main_loop_calls_drain_before_stop(self):
        """Inspect daemon.main()'s shutdown ordering by checking the source —
        more reliable than racing a real SIGTERM. The contract: drain is
        called before the final tb.stop() in the normal shutdown path
        (the post-`finally` block). main() may also call tb.stop() in an
        early-exception escape from server.start() failure; we ignore that
        path because no setter could be in flight before the server started."""
        from chirp import daemon as dmod
        import inspect
        src = inspect.getsource(dmod.main)
        drain_pos = src.find("shutdown_drain()")
        # The shutdown-path stop comes AFTER drain. Find a tb.stop() that
        # appears after the drain call — that's the one that matters.
        assert drain_pos != -1, "main() never calls shutdown_drain"
        stop_after_drain = src.find("tb.stop()", drain_pos)
        assert stop_after_drain != -1, (
            "shutdown path has no tb.stop() following shutdown_drain — "
            "either drain is misplaced or stop is missing"
        )


# ---------------------------------------------------------------------------
# Bug 3: libshout drop without reconnect
# ---------------------------------------------------------------------------


class _FakeShoutSink:
    """Tiny stub modelling the libshout-style sink contract Phase 3 will
    implement. Drops after `drop_after` writes; on `send` raises ConnectionError.
    The reconnector under test must observe the drop and call `reconnect()` with
    exponential backoff.
    """

    def __init__(self, drop_after: int = 3):
        self.drop_after = drop_after
        self.sent = 0
        self.reconnect_calls: list[float] = []
        self._reconnected_at: float = time.monotonic()
        self.connected = True

    def send(self, payload: bytes) -> None:
        if not self.connected:
            raise ConnectionError("not connected")
        self.sent += 1
        if self.sent > self.drop_after:
            self.connected = False
            raise ConnectionError("dropped")

    def reconnect(self) -> bool:
        self.reconnect_calls.append(time.monotonic() - self._reconnected_at)
        self.connected = True
        self.sent = 0
        return True


class IcecastReconnector:
    """Phase 3 contract: any libshout-style sink we ship must wrap raw
    `send()` calls so a ConnectionError triggers logged backoff-reconnect.

    Backoff schedule: 0.25, 0.5, 1.0, 2.0, 4.0, capped at 4.0 s. Resets to
    0.25 on the first successful reconnect.

    This class is the reference implementation; Phase 3 will replace its
    `sink` attribute with a real `python-shout` handle. The contract under
    test is: when `feed()` is called and the underlying sink raises a
    ConnectionError, the reconnector logs, sleeps the next backoff, calls
    reconnect(), and retries — and does NOT silently swallow the drop.
    """

    BACKOFF_SCHEDULE = (0.25, 0.5, 1.0, 2.0, 4.0)

    def __init__(self, sink, log=None, sleep=time.sleep):
        self.sink = sink
        self.log = log or logging.getLogger("chirp.icecast")
        self._sleep = sleep
        self._backoff_idx = 0
        self.drops = 0
        self.reconnects = 0

    def feed(self, payload: bytes, max_attempts: int = 5) -> bool:
        for attempt in range(max_attempts):
            try:
                self.sink.send(payload)
                # On success, reset backoff.
                self._backoff_idx = 0
                return True
            except ConnectionError as e:
                self.drops += 1
                self.log.warning("icecast drop (attempt %d): %s", attempt, e)
                wait = self.BACKOFF_SCHEDULE[
                    min(self._backoff_idx, len(self.BACKOFF_SCHEDULE) - 1)
                ]
                self._backoff_idx += 1
                self._sleep(wait)
                try:
                    if self.sink.reconnect():
                        self.reconnects += 1
                except Exception:
                    self.log.exception("reconnect raised")
                    continue
        return False


class TestBug3LibshoutReconnectsAfterDrop:
    """Regression: a libshout-style sink that drops mid-stream must be
    detected and reconnected, not silently swallowed."""

    def test_drop_triggers_reconnect(self):
        sink = _FakeShoutSink(drop_after=3)
        slept = []
        rc = IcecastReconnector(sink, sleep=lambda s: slept.append(s))
        # First 3 writes succeed.
        for _ in range(3):
            assert rc.feed(b"hello") is True
        # The 4th write drops. The reconnector must retry and succeed
        # because our stub reconnect() returns True.
        ok = rc.feed(b"hello")
        assert ok is True
        assert rc.drops >= 1
        assert rc.reconnects >= 1
        assert slept, "reconnector slept zero times — no backoff"
        assert slept[0] == 0.25, f"first backoff should be 0.25 s, got {slept[0]}"

    def test_exponential_backoff_schedule(self):
        """Repeated drops produce exponentially-growing waits (capped)."""
        # A sink that drops on EVERY send (and reconnect makes it droppable again).
        sink = _FakeShoutSink(drop_after=0)
        slept = []
        rc = IcecastReconnector(sink, sleep=lambda s: slept.append(s))
        ok = rc.feed(b"x", max_attempts=6)
        # All 6 attempts failed → False. Backoff slept 6 times, exponentially.
        assert ok is False
        assert slept[:5] == list(IcecastReconnector.BACKOFF_SCHEDULE)
        assert slept[5] == IcecastReconnector.BACKOFF_SCHEDULE[-1]  # capped

    def test_drop_is_logged_not_silent(self, caplog):
        sink = _FakeShoutSink(drop_after=0)
        rc = IcecastReconnector(sink, sleep=lambda s: None)
        with caplog.at_level(logging.WARNING, logger="chirp.icecast"):
            rc.feed(b"x", max_attempts=2)
        assert any("drop" in r.message.lower() for r in caplog.records), \
            "drop went unlogged — silent failure"

    def test_successful_send_resets_backoff(self):
        """After a successful feed (data actually went through), a SUBSEQUENT
        drop must start backoff at 0.25 again, not continue up the schedule.
        That's the property that prevents months-long lockouts after a brief
        wifi blip."""
        sink = _FakeShoutSink(drop_after=2)
        slept = []
        rc = IcecastReconnector(sink, sleep=lambda s: slept.append(s))
        # Two successful sends fill the "good" prefix.
        assert rc.feed(b"a") is True
        assert rc.feed(b"b") is True
        # Third send drops; reconnector retries and succeeds (drop_after on
        # the reconnected stream is still 2; sent reset to 0, so retry works).
        ok = rc.feed(b"c")
        assert ok is True
        assert slept[0] == 0.25
        # Now another drop later — backoff must START at 0.25 again, because
        # the previous feed() was successful.
        slept.clear()
        # We need a fresh drop. Force the sink into a drop state.
        sink.connected = False
        ok2 = rc.feed(b"d")
        assert ok2 is True
        assert slept[0] == 0.25, f"backoff didn't reset after success, got {slept[0]}"


# ---------------------------------------------------------------------------
# Bug 4: Noise-floor init race
# ---------------------------------------------------------------------------


class TestBug4NoiseFloorInitRace:
    """Regression: setting squelch on a freshly-constructed channel must
    apply the operator's value verbatim, not consult an unconverged noise
    estimate."""

    def test_set_squelch_on_fresh_channel_applies_exactly(self):
        ch = Channel(samp_rate=1e6, squelch_dbfs=-30.0)
        # Channel has run for ~0 ms — the level probe has seen nothing yet.
        # The pwr_squelch threshold is set in the ctor.
        assert ch.pwr_squelch.threshold() == pytest.approx(-30.0, abs=0.001)
        # Now operator changes their mind immediately.
        ch.set_squelch(-78.0)
        assert ch.pwr_squelch.threshold() == pytest.approx(-78.0, abs=0.001)
        # Probe still hasn't converged but squelch was applied verbatim.
        assert ch.squelch_dbfs == -78.0

    def test_set_squelch_does_not_silence_band(self, tmp_path):
        """End-to-end: with an open-squelch channel and a real signal, even a
        sub-1-second-old channel must let samples through after set_squelch."""
        samp_rate = 1e6
        iq_path = tmp_path / "carrier.iq"
        # Strong AM carrier at 200 kHz.
        n = int(samp_rate * 2.0)
        t = np.arange(n, dtype=np.float64) / samp_rate
        env = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * 1000 * t))
        iq = (env * np.exp(2j * np.pi * 200e3 * t)).astype(np.complex64)
        iq.tofile(iq_path)

        audio = tmp_path / "out.f32"
        cfg = DaemonConfig(
            band="airband",
            cmd_port=19800 + (os.getpid() % 400),
            source_kind="file",
            source_path=str(iq_path),
            source_samp_rate=samp_rate,
            audio_out_kind="file",
            audio_out_path=str(audio),
            audio_rate=16000.0,
            max_channels=2,
            state_path=str(tmp_path / "s.json"),
            hit_log_path=str(tmp_path / "h.jsonl"),
        )
        server = CommandServer(
            ServerConfig(host=cfg.cmd_host, port=cfg.cmd_port),
            dispatch=lambda env, args: tb.dispatch(env, args),
        )
        tb = ChirpFlowgraph(cfg, server, state_store=StateStore(cfg.state_path))
        tb.start()
        tb.start_health()
        server.start()
        try:
            # Add a channel and IMMEDIATELY (< 1 s) hit it with set_squelch.
            _udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1, "id": "a",
                "cmd": "add_channel", "args": {"id": "ch01", "freq_mhz": 0.2,
                "mode": "am", "squelch_dbfs": -90.0}}).encode())
            # The bug under regression: rtl-airband would consult the unconverged
            # noise estimate here and overwrite to ~poison value, silencing the band.
            resp = json.loads(_udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1,
                "id": "sq", "cmd": "set_squelch", "args": {"id": "ch01",
                "dbfs": -88.0}}).encode()))
            assert resp["status"] == "ok"
            assert resp["data"]["dbfs"] == -88.0
            # Confirm via get_status that the threshold is what we asked for.
            st = json.loads(_udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1,
                "id": "st", "cmd": "get_status", "args": {}}).encode()))
            ch = st["data"]["channels"][0]
            assert ch["squelch_dbfs"] == -88.0
            # And the audio file is growing — the band is NOT silenced.
            time.sleep(1.2)
            assert audio.stat().st_size > 0
        finally:
            tb.stop_health()
            tb.shutdown_drain()
            tb.stop()
            tb.wait()
            server.stop()

    def test_squelch_change_during_warmup_window_does_not_block(self):
        """Property: set_squelch is non-blocking on warmup. We do NOT defer
        the change waiting for a noise estimator to converge."""
        ch = Channel(samp_rate=1e6, squelch_dbfs=-50.0)
        t0 = time.monotonic()
        ch.set_squelch(-80.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, f"set_squelch blocked for {elapsed:.3f}s — synchronous warmup wait?"
        assert ch.pwr_squelch.threshold() == pytest.approx(-80.0, abs=0.001)
