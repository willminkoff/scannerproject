"""SB7.3-E regression suite — the IcecastSink "lies healthy" wedge.

Incident (2026-06-18 19:11:49, .deploy-logs/sb6-global-squelch-postboot-2026-06-18.md):
a transient RTL-SDR V4 USB read error (``rtlsdr_read_async returned with -5``)
made the gr-osmosdr source return WORK_DONE. GNU Radio wound the flowgraph
down and — via the block_executor destructor — called ``IcecastSink.stop()``
while the DAEMON kept running. The old stop() treated every call as final
teardown, so the publish loop exited permanently ("publish loop exiting
(stop=True)"): hits kept firing, get_status looked healthy, byte_rate=0,
/ANALOG_GROUND.mp3 sourceless ALL DAY. The forbidden third state.

What this file locks down:

  1. Transient unexpected exceptions inside the publish loop do NOT kill it:
     the supervisor logs, bumps ``publish_loop_restarts``, backs off, retries.
  2. A GR stop() WITHOUT daemon shutdown intent is SPURIOUS: publishing
     survives, ``spurious_stop_count`` increments, and audio keeps flowing.
  3. Genuine shutdown (``shutdown()`` / ``request_shutdown()`` + stop())
     tears down cleanly and is idempotent.
  4. Escalation: a spurious stop with no samples for the grace window fires
     ``on_fatal`` (once); samples resuming within the window stands it down.
  5. Escalation: N consecutive reconnect-exhausted publish cycles over the
     configured window fires ``on_fatal`` (once).
  6. The snapshot stops lying: publish_loop_alive / publish_loop_restarts /
     last_publish_error / spurious_stop_count are present and truthful.
  7. ``work()`` NEVER returns -1 on encoder failure (returning -1 marks the
     block done → done-ness propagates → the same class of wedge).
  8. A dead encoder subprocess is respawned by the supervisor and publishing
     resumes.

Like the Phase 3 suite, everything below runs with an injected fake encoder
and fake shout publisher — no lame, no libshout, no network, no real GNU
Radio scheduler. When the ``gnuradio`` package is absent (CI / dev venvs), a
minimal ``gr.sync_block`` stub is installed so the reconnector/publisher
layer — plain Python — is testable anywhere.
"""

from __future__ import annotations

import sys
import threading
import time
import types

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# gnuradio stub (only when the real package is unavailable).
# IcecastSink only needs gr.sync_block.__init__ from GNU Radio; the publish
# machinery under test is plain Python threads + pipes.
# ---------------------------------------------------------------------------

try:  # pragma: no cover — env-dependent
    from gnuradio import gr as _real_gr  # noqa: F401
except Exception:  # pragma: no cover — env-dependent
    _gr_stub = types.ModuleType("gnuradio.gr")

    class _SyncBlockStub:
        def __init__(self, name=None, in_sig=None, out_sig=None) -> None:
            pass

    _gr_stub.sync_block = _SyncBlockStub
    _gnuradio_stub = types.ModuleType("gnuradio")
    _gnuradio_stub.gr = _gr_stub
    sys.modules.setdefault("gnuradio", _gnuradio_stub)
    sys.modules.setdefault("gnuradio.gr", _gr_stub)

from chirp.dsp.icecast_sink import (  # noqa: E402 — after the stub, deliberately
    IcecastReconnector,
    IcecastSink,
    IcecastSinkConfig,
)


# ---------------------------------------------------------------------------
# Fakes (same shapes as test_phase3, self-contained here)
# ---------------------------------------------------------------------------


class _FakeEncoder:
    """Mimics a subprocess.Popen with stdin + stdout pipes; passthrough PCM.

    Also usable as the ``_lame`` process handle (poll/wait/kill/returncode)
    for the encoder-respawn tests, mirroring how production assigns
    ``self._lame = self._encoder = Popen(...)``.
    """

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
            if self._p._closed:
                raise BrokenPipeError("encoder dead")
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


class _BrokenStdin:
    """stdin stand-in whose write always raises — the dead-encoder case."""

    def write(self, b: bytes) -> int:
        raise BrokenPipeError("encoder stdin gone")

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass


class _FakePublisher:
    """In-memory publisher honoring the send/reconnect/sync/close contract.

    ``crash_on_sends``: send-call ordinals (1-based, counting every call)
    that raise RuntimeError — an UNEXPECTED exception type the reconnector
    does not catch, i.e. the transient-crash path the supervisor must survive.
    ``always_down``: every send raises ConnectionError and reconnect() fails —
    the reconnect-exhaustion path.
    """

    def __init__(self, crash_on_sends: tuple = (), always_down: bool = False) -> None:
        self.crash_on_sends = set(crash_on_sends)
        self.always_down = always_down
        self.bytes_sent = 0
        self.send_calls = 0
        self.reconnect_calls = 0
        self.payloads: list[bytes] = []
        self._connected = False
        self._lock = threading.Lock()

    def send(self, payload: bytes) -> None:
        with self._lock:
            self.send_calls += 1
            if self.always_down:
                raise ConnectionError("icecast down")
            if self.send_calls in self.crash_on_sends:
                raise RuntimeError("transient unexpected publisher crash")
            if not self._connected:
                raise ConnectionError("not connected")
            self.payloads.append(bytes(payload))
            self.bytes_sent += len(payload)

    def reconnect(self) -> bool:
        with self._lock:
            self.reconnect_calls += 1
            if self.always_down:
                return False
            self._connected = True
            return True

    def sync(self) -> None:
        pass

    def get_connected(self) -> bool:
        return self._connected

    def close(self) -> None:
        with self._lock:
            self._connected = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> IcecastSinkConfig:
    base = dict(
        host="127.0.0.1", port=8000, mount="/CHIRP_TEST.mp3", password="x",
        bitrate_kbps=32, sample_rate=16000,
        # Escalations OFF by default in tests; each test opts in explicitly
        # so a slow CI box never trips an unrelated fatal.
        fatal_reconnect_failures=0, fatal_window_s=0.0,
        spurious_stop_fatal_s=0.0,
    )
    base.update(overrides)
    return IcecastSinkConfig(**base)


def _make_sink(publisher=None, on_fatal=None, **cfg_overrides):
    enc = _FakeEncoder()
    pub = publisher if publisher is not None else _FakePublisher()
    sink = IcecastSink(_cfg(**cfg_overrides), encoder=enc,
                       publisher=pub, on_fatal=on_fatal)
    # Fast supervisor restarts + fast (no-op-sleep) reconnect backoff so the
    # suite stays sub-second. Instance attributes; the class stays untouched.
    sink.RESTART_BACKOFF_SCHEDULE = (0.01,)
    sink.reconnector = IcecastReconnector(pub, sleep=lambda s: None)
    return sink, enc, pub


def _pump(sink, n_samples: int = 2048) -> None:
    """Push ``n_samples`` float32 samples through work() (2 bytes each)."""
    sink.work([np.zeros(n_samples, dtype=np.float32)], [])


def _wait_for(pred, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ---------------------------------------------------------------------------
# 1. Transient exception → loop survives, restart counter increments
# ---------------------------------------------------------------------------


class TestTransientCrashSurvival:
    def test_unexpected_publisher_exception_restarts_loop(self):
        # send #2 raises RuntimeError — escapes IcecastReconnector.feed()
        # (which only catches ConnectionError) and would have killed the old
        # single-pass loop dead.
        pub = _FakePublisher(crash_on_sends=(2,))
        sink, enc, pub = _make_sink(publisher=pub)
        try:
            _pump(sink, 2048 * 4)  # ≥ 2 chunks of 4096 B
            assert _wait_for(lambda: sink.snapshot()["publish_loop_restarts"] >= 1), \
                "supervisor never restarted after the publisher crash"
            snap = sink.snapshot()
            assert snap["publish_loop_alive"] is True, \
                "publish thread died on a transient exception — the 6/18 wedge"
            assert snap["last_publish_error"] is not None
            assert "crash" in snap["last_publish_error"]
            # Publishing must RESUME: push more audio, bytes keep flowing.
            before = pub.bytes_sent
            _pump(sink, 2048 * 4)
            assert _wait_for(lambda: pub.bytes_sent > before), \
                "no bytes published after the supervisor restart"
        finally:
            sink.shutdown()

    def test_encoder_stdout_read_error_restarts_loop(self):
        sink, enc, pub = _make_sink()
        try:
            _pump(sink)
            assert _wait_for(lambda: pub.bytes_sent > 0)

            # Break read1 once, then restore — a transient stdout hiccup.
            real_read = enc.stdout.read
            state = {"raised": False}

            def flaky_read(n):
                if not state["raised"]:
                    state["raised"] = True
                    raise OSError("transient stdout read error")
                return real_read(n)

            enc.stdout.read1 = flaky_read
            _pump(sink)
            assert _wait_for(lambda: sink.snapshot()["publish_loop_restarts"] >= 1)
            assert sink.publish_loop_alive is True
            before = pub.bytes_sent
            _pump(sink, 2048 * 4)
            assert _wait_for(lambda: pub.bytes_sent > before)
        finally:
            sink.shutdown()


# ---------------------------------------------------------------------------
# 2/3. Spurious stop vs genuine shutdown
# ---------------------------------------------------------------------------


class TestSpuriousStopVsShutdown:
    def test_spurious_stop_does_not_kill_publishing(self):
        """GR calling stop() without daemon shutdown intent (the 2026-06-18
        flowgraph wind-down) must leave the publish machinery fully alive."""
        sink, enc, pub = _make_sink()
        try:
            _pump(sink)
            assert _wait_for(lambda: pub.bytes_sent > 0)

            assert sink.stop() is True  # GR lifecycle contract: still True

            snap = sink.snapshot()
            assert snap["spurious_stop_count"] == 1
            assert snap["shutdown_requested"] is False
            assert snap["publish_loop_alive"] is True, \
                "spurious stop() killed the publish thread — the exact 6/18 bug"
            assert enc.poll() is None, "spurious stop() killed the encoder"

            # And audio still flows end to end afterwards.
            before = pub.bytes_sent
            _pump(sink, 2048 * 4)
            assert _wait_for(lambda: pub.bytes_sent > before), \
                "publishing dead after spurious stop()"
        finally:
            sink.shutdown()

    def test_shutdown_tears_down_and_is_idempotent(self):
        sink, enc, pub = _make_sink()
        _pump(sink)
        assert _wait_for(lambda: pub.bytes_sent > 0)

        sink.shutdown()
        assert _wait_for(lambda: not sink.publish_loop_alive), \
            "publish thread still alive after shutdown()"
        snap = sink.snapshot()
        assert snap["publish_loop_alive"] is False
        assert snap["shutdown_requested"] is True
        assert snap["spurious_stop_count"] == 0
        assert pub.get_connected() is False, "publisher not closed on shutdown"

        # GR's own stop() arriving afterwards (normal tb.stop() path) must be
        # a genuine, idempotent teardown — NOT a spurious count.
        assert sink.stop() is True
        assert sink.snapshot()["spurious_stop_count"] == 0
        # shutdown() again is also safe.
        sink.shutdown()

    def test_request_shutdown_then_gr_stop_is_genuine(self):
        """The ChirpFlowgraph.stop() handshake: intent first, then GR stop()."""
        sink, enc, pub = _make_sink()
        sink.request_shutdown()
        assert sink.stop() is True
        assert _wait_for(lambda: not sink.publish_loop_alive)
        assert sink.snapshot()["spurious_stop_count"] == 0


# ---------------------------------------------------------------------------
# 4. Spurious-stop escalation (no samples → on_fatal; samples resume → stand down)
# ---------------------------------------------------------------------------


class TestSpuriousStopEscalation:
    def test_fatal_fires_when_no_samples_after_spurious_stop(self):
        fatals: list[str] = []
        sink, enc, pub = _make_sink(
            on_fatal=fatals.append, spurious_stop_fatal_s=0.15,
        )
        try:
            _pump(sink)
            sink.stop()  # spurious — and work() never runs again (source dead)
            assert _wait_for(lambda: len(fatals) >= 1, timeout=2.0), \
                "spurious stop with a dead source never escalated on_fatal"
            reason = fatals[0]
            assert "outside daemon shutdown" in reason
            assert "no samples" in reason
            # Fire-once: a second spurious stop must not re-escalate.
            sink.stop()
            time.sleep(0.3)
            assert len(fatals) == 1, "on_fatal fired more than once"
            assert sink.snapshot()["spurious_stop_count"] == 2
        finally:
            sink.shutdown()

    def test_no_fatal_when_samples_resume_after_spurious_stop(self):
        """A partial flowgraph hiccup where samples resume within the grace
        window is transient — publishing survives and nobody escalates."""
        fatals: list[str] = []
        sink, enc, pub = _make_sink(
            on_fatal=fatals.append, spurious_stop_fatal_s=0.2,
        )
        try:
            _pump(sink)
            sink.stop()  # spurious
            # Samples keep flowing (flowgraph recovered).
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                _pump(sink, 512)
                time.sleep(0.02)
            assert fatals == [], \
                "escalated on_fatal despite samples resuming (false positive)"
            assert sink.publish_loop_alive is True
        finally:
            sink.shutdown()

    def test_genuine_shutdown_cancels_pending_escalation(self):
        fatals: list[str] = []
        sink, enc, pub = _make_sink(
            on_fatal=fatals.append, spurious_stop_fatal_s=0.2,
        )
        _pump(sink)
        sink.stop()  # spurious, watch armed
        sink.shutdown()  # daemon shuts down before the window elapses
        time.sleep(0.4)
        assert fatals == [], "escalated on_fatal during genuine shutdown"


# ---------------------------------------------------------------------------
# 5. Reconnect-exhaustion escalation
# ---------------------------------------------------------------------------


class TestReconnectExhaustionEscalation:
    def test_fatal_after_threshold_failures_over_window(self):
        fatals: list[str] = []
        pub = _FakePublisher(always_down=True)
        sink, enc, pub = _make_sink(
            publisher=pub, on_fatal=fatals.append,
            fatal_reconnect_failures=3, fatal_window_s=0.05,
        )
        try:
            # Let the window elapse relative to sink start (no publish has
            # ever succeeded, so the anchor is the construction timestamp).
            time.sleep(0.1)
            _pump(sink, 2048 * 6)  # ≥ 3 failed feed cycles
            assert _wait_for(lambda: len(fatals) >= 1, timeout=3.0), \
                "reconnect exhaustion never escalated on_fatal"
            assert "reconnect-exhausted" in fatals[0]
            # Fire-once even as failures continue.
            _pump(sink, 2048 * 6)
            time.sleep(0.2)
            assert len(fatals) == 1, "on_fatal fired more than once"
            # The loop itself must STILL be alive — escalation is the daemon's
            # exit signal, not the sink's suicide.
            assert sink.publish_loop_alive is True
        finally:
            sink.shutdown()

    def test_no_fatal_below_threshold_and_default_is_log_only(self):
        # 2 failing cycles < threshold 5 → no escalation; and with the
        # default on_fatal (None) escalation would only log anyway.
        pub = _FakePublisher(always_down=True)
        fatals: list[str] = []
        sink, enc, pub = _make_sink(
            publisher=pub, on_fatal=fatals.append,
            fatal_reconnect_failures=5, fatal_window_s=0.01,
        )
        try:
            _pump(sink, 2048 * 2)  # ≤ 2 failed cycles
            time.sleep(0.3)
            assert fatals == []
            assert sink.publish_loop_alive is True
            snap = sink.snapshot()
            assert snap["last_publish_error"] is not None
            assert "max reconnect attempts" in snap["last_publish_error"]
        finally:
            sink.shutdown()

    def test_success_resets_consecutive_failure_count(self):
        sink, enc, pub = _make_sink()
        try:
            _pump(sink)
            assert _wait_for(lambda: pub.bytes_sent > 0)
            sink._register_feed_result(False, time.monotonic())
            sink._register_feed_result(False, time.monotonic())
            assert sink._consecutive_feed_failures == 2
            sink._register_feed_result(True, time.monotonic())
            assert sink._consecutive_feed_failures == 0
            assert sink._last_publish_ok_ts is not None
        finally:
            sink.shutdown()


# ---------------------------------------------------------------------------
# 6. Snapshot truthfulness
# ---------------------------------------------------------------------------


class TestSnapshotTruth:
    def test_new_fields_present_and_truthful_while_running(self):
        sink, enc, pub = _make_sink()
        try:
            snap = sink.snapshot()
            for k in ("publish_loop_alive", "publish_loop_restarts",
                      "last_publish_error", "last_publish_error_ts",
                      "spurious_stop_count", "shutdown_requested"):
                assert k in snap, f"snapshot missing {k!r}"
            assert snap["publish_loop_alive"] is True
            assert snap["publish_loop_restarts"] == 0
            assert snap["last_publish_error"] is None
            assert snap["spurious_stop_count"] == 0
            assert snap["shutdown_requested"] is False
        finally:
            sink.shutdown()

    def test_alive_reads_the_thread_not_cached_state(self):
        """publish_loop_alive must come from Thread.is_alive() — the whole
        point is that a dead loop can no longer masquerade as healthy."""
        sink, enc, pub = _make_sink()
        assert sink.snapshot()["publish_loop_alive"] is True
        sink.shutdown()
        assert _wait_for(lambda: sink.snapshot()["publish_loop_alive"] is False)

    def test_no_publisher_thread_reports_not_alive(self):
        enc = _FakeEncoder()
        sink = IcecastSink(_cfg(), encoder=enc, publisher=_FakePublisher(),
                           autostart_publisher=False)
        assert sink.snapshot()["publish_loop_alive"] is False


# ---------------------------------------------------------------------------
# 7. work() never returns -1 (done-propagation kill path)
# ---------------------------------------------------------------------------


class TestWorkNeverReturnsDone:
    def test_encoder_write_failure_drops_samples_not_block(self):
        enc = _FakeEncoder()
        enc.stdin = _BrokenStdin()
        sink = IcecastSink(_cfg(), encoder=enc, publisher=_FakePublisher(),
                           autostart_publisher=False)
        n = 1024
        rc = sink.work([np.zeros(n, dtype=np.float32)], [])
        assert rc == n, (
            f"work() returned {rc}; returning -1 marks the block done and GR "
            f"propagates done-ness through the graph — the 2026-06-18 wedge class"
        )
        snap = sink.snapshot()
        assert snap["last_publish_error"] is not None
        assert "encoder stdin write failed" in snap["last_publish_error"]

    def test_work_records_sample_flow_timestamp(self):
        enc = _FakeEncoder()
        sink = IcecastSink(_cfg(), encoder=enc, publisher=_FakePublisher(),
                           autostart_publisher=False)
        assert sink._last_work_ts is None
        sink.work([np.zeros(64, dtype=np.float32)], [])
        assert sink._last_work_ts is not None


# ---------------------------------------------------------------------------
# 8. Encoder death → supervisor respawn → publishing resumes
# ---------------------------------------------------------------------------


class TestEncoderRespawn:
    def test_dead_encoder_is_respawned_and_bytes_resume(self):
        sink, enc, pub = _make_sink()
        try:
            _pump(sink)
            assert _wait_for(lambda: pub.bytes_sent > 0)

            # Make the injected encoder look production-spawned (the sink
            # assigns self._lame = self._encoder = Popen(...) in
            # _spawn_encoder), then arrange the respawn to install a fresh
            # fake — mirroring a real lame/ffmpeg crash + relaunch.
            replacement = _FakeEncoder()

            def fake_spawn():
                sink._lame = replacement
                sink._encoder = replacement

            sink._spawn_encoder = fake_spawn  # instance-level, tests only
            sink._lame = enc
            enc.stdin.close()  # encoder "dies": poll() flips to 0, stdout EOF

            assert _wait_for(
                lambda: sink._encoder is replacement, timeout=3.0
            ), "supervisor never respawned the dead encoder"
            assert sink.snapshot()["publish_loop_restarts"] >= 1
            assert sink.publish_loop_alive is True

            # Audio flows again through the replacement encoder.
            before = pub.bytes_sent
            _pump(sink, 2048 * 4)
            assert _wait_for(lambda: pub.bytes_sent > before, timeout=3.0), \
                "no bytes published after encoder respawn"
        finally:
            sink.shutdown()

    def test_injected_encoder_is_never_respawned(self):
        """Test-injected encoders (no subprocess handle) must not be touched."""
        sink, enc, pub = _make_sink()
        try:
            assert sink._lame is None
            assert sink._respawn_encoder_if_dead() is False
            assert sink._encoder is enc
        finally:
            sink.shutdown()
