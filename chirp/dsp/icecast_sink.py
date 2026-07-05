"""chirp.dsp.icecast_sink — python-shout MP3 publisher GR sink.

Phase 3. Takes mono float32 audio from the mixer at the daemon's configured
sample rate (16 kHz), encodes to MP3 via libmp3lame (subprocess pipe), and
pushes frames to an Icecast 2 mountpoint over libshout.

Topology:

    Mixer (float32 @ 16 kHz)
        │
        ▼
    IcecastSink (gr.sync_block)
        │  float32 → int16 LE
        ▼
    lame -r -s 16 --bitwidth 16 -m m --signed --cbr -b N - -
        │  MP3 frames @ N kbps CBR
        ▼
    _publish_loop thread → IcecastReconnector.feed() → shout.send()
                                                      shout.sync()  (pacing)

Threading:
  * GR thread runs work(): float32 → int16 → write to lame.stdin (blocking).
  * Reader/publisher thread: reads MP3 chunks from lame.stdout, sends via
    libshout. Backpressure naturally propagates back through lame.stdin.

Reconnect contract (carries the Phase 2 IcecastReconnector forward):
    * On a ConnectionError from shout.send(), the IcecastReconnector logs,
      sleeps via the BACKOFF_SCHEDULE, calls reconnect(), retries.
    * Backoff resets to 0.25 on first successful send after a drop.
    * `drops` and `reconnects` counters are exposed for get_status.

Silent-frame keepalive:
    * Channels park with squelch slammed shut; their audio output is 0.0.
    * The mixer sum is therefore 0.0 when all channels are silent.
    * libmp3lame still emits valid MP3 frames for a constant-zero PCM input.
    * Icecast keeps the source alive; existing /api/sample_flow.py heuristic
      `mount_publishing` (bytes/s > 0) keeps reporting healthy. Per design
      doc §10. No special keepalive code needed in this sink.

Notes:
    * shout.Shout.send() raises shout.ShoutException — we translate to
      ConnectionError so the existing reconnector contract applies unchanged.
    * shout.Shout.sync() is the pacing primitive; we call it on every chunk
      to avoid overrunning Icecast's input queue.

Lifecycle contract (SB7.3 workstream E — the 2026-06-18 ground wedge):
    GNU Radio's thread-per-block scheduler calls ``block->stop()`` from the
    ``block_executor`` destructor whenever a block's executor thread exits —
    which happens BOTH at a genuine ``tb.stop()`` AND when the flowgraph winds
    itself down after an upstream failure. On 2026-06-18 19:11:49 a transient
    RTL-SDR V4 USB read error (``rtlsdr_read_async returned with -5``) made the
    gr-osmosdr source return WORK_DONE; done-ness propagated down the graph and
    GR called ``IcecastSink.stop()`` while the DAEMON kept running. The old
    stop() treated every call as final teardown: it set ``_stop``, killed lame,
    joined the publish thread and closed the shout connection — so
    /ANALOG_GROUND.mp3 was sourceless ALL DAY while hits kept firing and
    get_status looked healthy. That is the forbidden "third state" (north star:
    docs/chirp-rebuild-scope-2026-06-12.md — real audio from real samples, or a
    clean stop with a structured diagnostic; nothing in between).

    The contract is now:
      * ``request_shutdown()`` / ``shutdown()`` — the DAEMON declares intent.
        ``ChirpFlowgraph.stop()`` calls ``shutdown()`` before stopping GR, so a
        subsequent GR-invoked ``stop()`` is recognized as genuine and performs
        (idempotent) teardown.
      * ``stop()`` WITHOUT prior shutdown intent is SPURIOUS: publishing is NOT
        torn down; we log CRITICAL, bump ``spurious_stop_count``, and start a
        grace-window watch — if no samples flow through work() within
        ``spurious_stop_fatal_s``, the source is dead and we escalate via
        ``on_fatal`` so the daemon can exit with a structured diagnostic and
        let systemd restart the whole stack (what the manual
        ``systemctl restart gr-demod@ground`` did by hand on 6/18).
      * The publish loop is supervised: an unexpected exception or encoder
        death logs loudly, backs off (same schedule as the reconnector),
        respawns the encoder if needed, and keeps publishing.
      * Icecast-connection irrecoverability (``fatal_reconnect_failures``
        consecutive exhausted feed cycles over ``fatal_window_s``) also
        escalates via ``on_fatal`` — once.
      * ``work()`` NEVER returns -1: returning -1 marks the block done, GR
        propagates done-ness through the graph, and the daemon wedges in the
        same third state. Encoder-write failures drop samples (logged,
        throttled) until the supervisor respawns the encoder.
"""

from __future__ import annotations

import logging
import os
import shutil
import io
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from gnuradio import gr

log = logging.getLogger("chirp.icecast")

# Repo root (chirp/dsp/icecast_sink.py -> parents[2]) — resolves a relative
# denoise_model path like "chirp/models/sh.rnnn" regardless of CWD.
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# IcecastReconnector — the Phase 2 contract, now living next to the real sink.
# ---------------------------------------------------------------------------


class IcecastReconnector:
    """Wraps an Icecast-ish sink with logged, exponential-backoff reconnect.

    `sink` is duck-typed: it must implement `send(bytes)` (raises
    `ConnectionError` on drop) and `reconnect() -> bool`.

    Backoff: 0.25, 0.5, 1.0, 2.0, 4.0, capped at 4.0 s. Resets to 0.25 on the
    first successful reconnect — that's the property that prevents months-long
    lockouts after a brief network blip.

    `drops` and `reconnects` are public counters for the daemon's get_status
    snapshot. `bytes_sent` is read from the wrapped sink if present.
    """

    BACKOFF_SCHEDULE = (0.25, 0.5, 1.0, 2.0, 4.0)

    def __init__(
        self,
        sink: Any,
        log: Optional[logging.Logger] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class IcecastSinkConfig:
    """All Icecast publisher options. Defaults match the design doc:
    32 kbps CBR mono MP3 at 16 kHz, matches existing /ANALOG.mp3 contract."""
    host: str
    port: int
    mount: str  # e.g. "/CHIRP_TEST.mp3" — MUST start with "/"
    password: str
    bitrate_kbps: int = 32
    sample_rate: int = 16000
    # AM voice denoise: when True AND denoise_model is set, encode via ffmpeg
    # arnndn (RNNoise) instead of lame. Same stdin/stdout (raw PCM -> MP3)
    # contract, so libshout + the flowgraph are untouched.
    denoise: bool = False
    denoise_model: str = ""
    # Post-arnndn make-up gain (dB) to compensate RNNoise's voice attenuation.
    # Applied via the ffmpeg `volume` filter after arnndn. 0 = no boost.
    denoise_gain_db: float = 0.0
    # Optional ffmpeg audio filter chain (e.g. presence-boost EQ). When set
    # (and denoise off), the encoder is ffmpeg+libmp3lame with this chain
    # instead of plain lame. Empty = plain lame.
    audio_eq: str = ""
    user: str = "source"
    server_name: str = "chirp"
    description: str = "chirp gr-demod"
    genre: str = "scanner"
    public: int = 0
    # ---- No-third-state escalation knobs (SB7.3-E, 2026-06-18 incident) ----
    # Consecutive exhausted reconnect cycles (each cycle = IcecastReconnector
    # .feed() giving up after max_attempts) before publishing is declared
    # irrecoverable. <= 0 disables the reconnect-exhaustion escalation.
    fatal_reconnect_failures: int = 10
    # Minimum seconds since the last SUCCESSFUL publish before the reconnect-
    # exhaustion escalation may fire. Both thresholds must be met. With the
    # defaults (10 cycles ≈ 78 s of backoff + 600 s window) a brief icecast
    # restart never kills the daemon, but a dead icecast escalates within
    # ~10 minutes instead of lying healthy all day. <= 0 disables.
    fatal_window_s: float = 600.0
    # Grace window after a SPURIOUS GR stop() (stop without daemon shutdown
    # intent — the 6/18 flowgraph wind-down signature). If no samples reach
    # work() within this many seconds of the spurious stop, the source is dead
    # and on_fatal fires so the daemon can exit for a systemd restart.
    # <= 0 disables the watch (spurious stops are still logged + counted).
    spurious_stop_fatal_s: float = 30.0


# ---------------------------------------------------------------------------
# python-shout wrapper — adapts libshout to the IcecastReconnector contract
# ---------------------------------------------------------------------------


class _ShoutPublisher:
    """Thin adapter around python-shout.Shout that exposes
    send(bytes) / reconnect() / close() and translates shout.ShoutException
    into ConnectionError so the reconnector contract Just Works.

    NOTE: We import `shout` lazily so the module is import-safe in CI
    environments without libshout installed (the unit tests stub the sink).
    """

    def __init__(self, cfg: IcecastSinkConfig) -> None:
        self.cfg = cfg
        self._shout = None  # type: ignore[assignment]
        self.bytes_sent = 0

    # -- internal -----------------------------------------------------------

    def _build(self):
        import shout  # lazy
        s = shout.Shout()
        s.host = self.cfg.host
        s.port = int(self.cfg.port)
        s.user = self.cfg.user
        s.password = self.cfg.password
        s.mount = self.cfg.mount
        s.format = "mp3"
        s.protocol = "http"
        s.name = self.cfg.server_name
        s.description = self.cfg.description
        s.genre = self.cfg.genre
        s.public = int(self.cfg.public)
        s.audio_info = {
            "bitrate": str(self.cfg.bitrate_kbps),
            "samplerate": str(self.cfg.sample_rate),
            "channels": "1",
        }
        return s

    # -- public API expected by IcecastReconnector --------------------------

    def send(self, payload: bytes) -> None:
        if self._shout is None:
            raise ConnectionError("shout not connected")
        try:
            self._shout.send(payload)
            self.bytes_sent += len(payload)
        except Exception as e:  # shout.ShoutException; we don't import the type
            try:
                self._shout.close()
            except Exception:
                pass
            self._shout = None
            raise ConnectionError(f"shout.send failed: {e}") from e

    def reconnect(self) -> bool:
        # Close any leftover, then re-open.
        if self._shout is not None:
            try:
                self._shout.close()
            except Exception:
                pass
            self._shout = None
        try:
            s = self._build()
            s.open()
            self._shout = s
            return True
        except Exception as e:
            log.warning("shout.open failed: %s", e)
            self._shout = None
            return False

    def sync(self) -> None:
        """Pace via libshout. Called by the publisher thread between sends
        so we don't overrun the Icecast input queue. Safe if not connected."""
        if self._shout is not None:
            try:
                self._shout.sync()
            except Exception:
                pass

    def get_connected(self) -> bool:
        return self._shout is not None

    def close(self) -> None:
        if self._shout is not None:
            try:
                self._shout.close()
            except Exception:
                pass
            self._shout = None


# ---------------------------------------------------------------------------
# IcecastSink — GR sync_block sink
# ---------------------------------------------------------------------------


# State strings exposed via get_status.icecast_state.
STATE_NOT_CONFIGURED = "not_configured"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_RECONNECTING = "reconnecting"


def _lame_available() -> bool:
    return shutil.which("lame") is not None


class IcecastSink(gr.sync_block):
    """Mono float32 GR sink → libmp3lame → Icecast (python-shout).

    The sink is safe to instantiate without `lame` installed for tests that
    pass `encoder=` to inject a stub. When `encoder=None` (production) the
    real lame subprocess is spawned.

    The `publisher=` injection point lets tests substitute a fake sink that
    matches the send/reconnect/close interface for offline integration.
    """

    INPUT_CHUNK_BYTES = 4096

    # Supervisor restart backoff after an unexpected publish-loop exit.
    # Same shape as the reconnector's schedule; a class attribute so tests can
    # shrink it per-instance (``sink.RESTART_BACKOFF_SCHEDULE = (0.01,)``).
    RESTART_BACKOFF_SCHEDULE = IcecastReconnector.BACKOFF_SCHEDULE

    def __init__(
        self,
        cfg: IcecastSinkConfig,
        encoder: Optional[Any] = None,
        publisher: Optional[Any] = None,
        autostart_publisher: bool = True,
        flow_probe: Optional[Any] = None,
        on_fatal: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(name="IcecastSink", in_sig=[np.float32], out_sig=None)
        self.cfg = cfg
        # SB6 (2026-06-18): optional AudioFlowProbe tapping PCM amplitude at the
        # sink input — the ONLY layer where the -180 dBFS "silent branch" wedge
        # is visible (the MP3 byte stream stays non-zero for zero PCM, so the
        # bytes_sent telemetry lies). See chirp/audio_probe.py.
        self._flow_probe = flow_probe
        self._stop = threading.Event()
        # SB7.3-E: shutdown INTENT, distinct from ``_stop`` (mechanism). Only
        # the daemon sets this (via request_shutdown()/shutdown()); a GR-driven
        # stop() that arrives without it is spurious — the 2026-06-18 wedge —
        # and must not tear publishing down. See module docstring.
        self._shutdown_requested = threading.Event()
        self._teardown_lock = threading.Lock()
        self._teardown_done = False
        # Escalation callback for genuinely-irrecoverable publishing. Called at
        # most once, from a sink-owned thread. None = library-safe default (the
        # CRITICAL log in _fire_fatal is the whole action). The daemon wires
        # this to a structured exit so systemd restarts the full stack.
        self._on_fatal = on_fatal
        self._fatal_lock = threading.Lock()
        self._fatal_fired = False
        # Truth-telling counters/fields for snapshot()/get_status — added
        # because on 6/18 get_status kept reporting a healthy-looking sink for
        # a publish loop that had been dead since 19:11:49.
        self._publish_loop_restarts = 0
        self._spurious_stop_count = 0
        self._last_publish_error: Optional[str] = None
        self._last_publish_error_ts: Optional[float] = None
        self._consecutive_feed_failures = 0
        self._last_publish_ok_ts: Optional[float] = None
        # monotonic timestamp of the last work() call — the spurious-stop watch
        # uses it to tell "flowgraph recovered" from "source is dead".
        self._last_work_ts: Optional[float] = None
        self._restart_backoff_idx = 0
        self._work_err_log_ts = 0.0
        self._state = STATE_NOT_CONFIGURED
        self._state_lock = threading.Lock()
        self._lame: Optional[subprocess.Popen] = None
        self._encoder = encoder  # if provided, has .stdin / .stdout file-like

        # The wrapped publisher. Tests can inject; production uses python-shout.
        self.publisher = publisher if publisher is not None else _ShoutPublisher(cfg)
        self.reconnector = IcecastReconnector(self.publisher, log=log)

        # Counters surfaced via get_status.
        self._t_start = time.monotonic()

        # Spawn the encoder subprocess if none injected (tests inject a stub).
        if self._encoder is None:
            self._spawn_encoder()

        # _publish_loop calls encoder.stdout.read1() for throughput pacing.
        # subprocess.Popen(bufsize=0) returns a raw _io.FileIO that has no
        # read1; ensure_stdout_read1 wraps it (or shims a test fake) so the
        # loop does not crash on first iteration. Regression from 0eada16.
        self._ensure_stdout_read1()

        # Start initial connection + publisher thread.
        if autostart_publisher:
            self._set_state(STATE_RECONNECTING)
            if self.publisher.reconnect():
                self.reconnector.reconnects += 1
                self._set_state(STATE_CONNECTED)
            else:
                self._set_state(STATE_DISCONNECTED)
            self._publish_thread = threading.Thread(
                target=self._publish_loop, name="chirp-icecast", daemon=True
            )
            self._publish_thread.start()
        else:
            self._publish_thread = None  # type: ignore[assignment]

    # -- lame subprocess ----------------------------------------------------

    def _spawn_encoder(self) -> None:
        """Spawn the PCM->MP3 encoder subprocess. Three backends, same
        stdin (raw int16 PCM in) / stdout (MP3 out) contract:
          * denoise on            -> ffmpeg arnndn (+volume +eq)
          * denoise off, eq set   -> ffmpeg eq-only
          * denoise off, eq empty -> plain lame (default; ground)
        """
        if self.cfg.denoise and self.cfg.denoise_model:
            self._spawn_ffmpeg_arnndn()
        elif self.cfg.audio_eq:
            self._spawn_ffmpeg_eq()
        else:
            self._spawn_lame()

    def _ensure_stdout_read1(self) -> None:
        """Ensure self._encoder.stdout supports .read1(n).

        Background: subprocess.Popen(..., bufsize=0) returns a raw
        _io.FileIO on stdout which exposes .read() but NOT .read1().
        _publish_loop wants .read1() so the encoder fills the pipe while
        shout.sync() sleeps — see the throughput comment at the read1
        call site. Without this wrap the publisher dies on first iter
        with AttributeError and the icecast mount stays disconnected.
        Regression from commit 0eada16, surfaced on the 2026-06-13 SB5
        cutover.

        Behavior:
          * stdout already has .read1 → no-op.
          * stdout is a raw FileIO         → wrap in io.BufferedReader.
          * stdout is a Python object that allows attribute assignment
            and has .read but not .read1 → shim .read1 := .read. (read()
            on an unbuffered raw stream returns whatever is available
            up to n bytes, which matches read1 semantics.)
        """
        if self._encoder is None or self._encoder.stdout is None:
            return
        stdout = self._encoder.stdout
        if hasattr(stdout, "read1"):
            return
        if isinstance(stdout, io.FileIO):
            self._encoder.stdout = io.BufferedReader(stdout)  # type: ignore[assignment]
            return
        try:
            stdout.read1 = stdout.read  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            # Fall back to BufferedReader; may still fail if the object
            # does not satisfy RawIOBase, but at that point the caller
            # injected a stub that is not a real raw stream and the
            # publish loop will surface the issue.
            self._encoder.stdout = io.BufferedReader(stdout)  # type: ignore[assignment]

    @staticmethod
    def _ffmpeg_base_cmd(sample_rate: int, bitrate_kbps: int, af: str) -> list:
        """ffmpeg PCM->MP3 wrapper for a given -af chain (encoder-agnostic)."""
        sr = int(sample_rate)
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
            "-af", af,
            "-c:a", "libmp3lame", "-b:a", f"{int(bitrate_kbps)}k", "-ac", "1",
            "-flush_packets", "1", "-f", "mp3", "pipe:1",
        ]

    @staticmethod
    def _ffmpeg_arnndn_cmd(model: str, sample_rate: int, bitrate_kbps: int,
                           gain_db: float = 0.0, audio_eq: str = "") -> list:
        """ffmpeg arnndn encoder command. arnndn runs at 48 kHz (ffmpeg
        auto-resamples 16k up); volume make-up + optional EQ follow, then
        aresample back so the MP3 keeps the icecast rate + bitrate."""
        sr = int(sample_rate)
        af = f"arnndn=m={model}"
        if gain_db:
            af += f",volume={gain_db}dB"
        if audio_eq:
            af += f",{audio_eq}"
        af += f",aresample={sr}"
        return IcecastSink._ffmpeg_base_cmd(sr, bitrate_kbps, af)

    @staticmethod
    def _ffmpeg_eq_cmd(sample_rate: int, bitrate_kbps: int, audio_eq: str) -> list:
        """ffmpeg EQ-only encoder command (no arnndn). Input is already at the
        contract rate; aresample is a harmless no-op kept for chain symmetry."""
        sr = int(sample_rate)
        return IcecastSink._ffmpeg_base_cmd(sr, bitrate_kbps, f"{audio_eq},aresample={sr}")

    def _spawn_ffmpeg_eq(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH (needed for audio_eq)")
        cmd = self._ffmpeg_eq_cmd(self.cfg.sample_rate, self.cfg.bitrate_kbps, self.cfg.audio_eq)
        log.info("spawning ffmpeg eq encoder: %s", " ".join(cmd))
        self._lame = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self._encoder = self._lame

    def _spawn_ffmpeg_arnndn(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH (needed for denoise)")
        model = self.cfg.denoise_model
        if not os.path.isabs(model):
            model = str((_REPO_ROOT / model).resolve())
        if not os.path.isfile(model):
            raise RuntimeError(f"denoise_model not found: {model}")
        cmd = self._ffmpeg_arnndn_cmd(
            model, self.cfg.sample_rate, self.cfg.bitrate_kbps,
            self.cfg.denoise_gain_db, self.cfg.audio_eq,
        )
        log.info("spawning ffmpeg arnndn encoder: %s", " ".join(cmd))
        # self._lame holds the encoder process handle for either backend
        # (stop()/_publish_loop are encoder-agnostic).
        self._lame = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self._encoder = self._lame

    def _spawn_lame(self) -> None:
        if not _lame_available():
            raise RuntimeError(
                "lame binary not found on PATH — install via "
                "`apt-get install lame` or inject encoder= for tests"
            )
        # `-r` raw PCM. `-s` rate in kHz. `--bitwidth 16` + `--signed`.
        # `-m m` = mono. `--cbr -b N` = constant bitrate at N kbps.
        # `-q 5` = mid quality. `--silent` = no progress chatter.
        sr_khz = self.cfg.sample_rate / 1000.0
        cmd = [
            "lame",
            "-r", "-s", str(sr_khz),
            "--bitwidth", "16", "--signed",
            "-m", "m", "-q", "5",
            "--cbr", "-b", str(self.cfg.bitrate_kbps),
            "--silent",
            "-", "-",
        ]
        log.info("spawning lame: %s", " ".join(cmd))
        self._lame = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._encoder = self._lame

    # -- state --------------------------------------------------------------

    def _set_state(self, s: str) -> None:
        with self._state_lock:
            self._state = s

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def bytes_sent(self) -> int:
        return getattr(self.publisher, "bytes_sent", 0)

    @property
    def reconnect_count(self) -> int:
        return self.reconnector.reconnects

    @property
    def drop_count(self) -> int:
        return self.reconnector.drops

    # -- GR work ------------------------------------------------------------

    def work(self, input_items, output_items):
        samples = input_items[0]
        n = len(samples)
        # SB7.3-E: record that samples are still reaching this block. The
        # spurious-stop watch reads this to distinguish "flowgraph recovered
        # after a partial hiccup" from "source dead, escalate".
        self._last_work_ts = time.monotonic()
        if n == 0 or self._stop.is_set():
            return n
        # float32 in [-1, 1] → int16 LE.
        # Channels emit zeros when squelched; lame produces silent MP3 frames
        # for constant-zero PCM, preserving the icecast-keepalive heuristic.
        clipped = np.clip(samples, -1.0, 1.0)
        # SB6 flow probe: hand the block's peak |sample| to the watchdog probe
        # BEFORE the int16/encode step. One cheap reduction; no-op when the
        # probe is disabled (default). The probe distinguishes real audio from
        # the exact-zero wedge that the downstream byte counters cannot see.
        if self._flow_probe is not None:
            try:
                self._flow_probe.observe_peak(float(np.abs(clipped).max()) if n else 0.0)
            except Exception:  # noqa: BLE001 — instrumentation must never break audio
                pass
        i16 = (clipped * 32767.0).astype(np.int16)
        # Local ref: the publish supervisor may respawn the encoder (swap
        # self._encoder) concurrently. Reading once keeps this lock-free — a
        # write racing the swap fails on the DEAD encoder's pipe and lands in
        # the except below; the next work() call picks up the fresh encoder.
        enc = self._encoder
        try:
            if enc is not None and enc.stdin is not None:
                enc.stdin.write(i16.tobytes())
        except Exception as e:  # noqa: BLE001 — any write failure gets the same policy
            # Encoder died (BrokenPipeError) or its stdin was closed mid-swap /
            # mid-shutdown (ValueError). The OLD code returned -1 here — which
            # tells GR the block is DONE; done-ness propagates through the
            # graph and wedges the daemon in the exact 2026-06-18 third state
            # (scanning daemon, dead audio). NEVER return -1: drop this block
            # of samples, let the publish supervisor respawn the encoder, keep
            # upstream sample flow intact. Log throttled — this fires at the
            # audio block rate while the encoder is down.
            self._note_publish_error(f"encoder stdin write failed: {e!r}")
            now = time.monotonic()
            if now - self._work_err_log_ts >= 5.0:
                self._work_err_log_ts = now
                log.error(
                    "encoder stdin write failed (%r) — dropping samples until "
                    "the publish supervisor respawns the encoder (mount=%s)",
                    e, self.cfg.mount,
                )
        return n

    # -- publisher loop -----------------------------------------------------

    def _note_publish_error(self, msg: str) -> None:
        """Record the most recent publish-path error for snapshot()/get_status.

        On 2026-06-18 the operator had NO field that said why publishing died —
        get_status showed a plausible icecast_state and stale byte counters.
        This is the structured breadcrumb the incident was missing.
        """
        self._last_publish_error = msg
        self._last_publish_error_ts = time.time()

    def _fire_fatal(self, reason: str) -> None:
        """Escalate genuinely-irrecoverable publishing. Fires AT MOST ONCE.

        Default (no on_fatal) is a CRITICAL log — library-safe. The daemon
        wires on_fatal to a structured exit (exit code 4) so systemd restarts
        the whole stack, which is exactly what the manual
        ``systemctl restart gr-demod@ground`` did on 6/18, minus the human and
        minus the all-day dead air.
        """
        with self._fatal_lock:
            if self._fatal_fired:
                return
            self._fatal_fired = True
        self._note_publish_error(f"FATAL: {reason}")
        log.critical(
            "chirp.icecast FATAL — publishing is irrecoverable (no-third-state "
            "escalation, mount=%s): %s", self.cfg.mount, reason,
        )
        cb = self._on_fatal
        if cb is None:
            return
        try:
            cb(reason)
        except Exception:  # noqa: BLE001 — escalation must never crash the sink
            log.exception("on_fatal callback raised")

    def _register_feed_result(self, ok: bool, now: float) -> None:
        """Track reconnect-exhaustion for the fatal escalation.

        One "failure" = a whole IcecastReconnector.feed() cycle giving up after
        max_attempts (≈ 7.75 s of backoff). Escalate only when BOTH thresholds
        hold: ``fatal_reconnect_failures`` consecutive failed cycles AND
        ``fatal_window_s`` seconds since the last successful publish — so a
        brief icecast restart never kills the daemon, but a dead icecast
        cannot keep us in the third state for a whole day.
        """
        if ok:
            self._consecutive_feed_failures = 0
            self._last_publish_ok_ts = now
            self._restart_backoff_idx = 0
            return
        self._consecutive_feed_failures += 1
        self._note_publish_error(
            f"icecast send failed after max reconnect attempts "
            f"(consecutive cycles={self._consecutive_feed_failures})"
        )
        n_thresh = int(getattr(self.cfg, "fatal_reconnect_failures", 0) or 0)
        window = float(getattr(self.cfg, "fatal_window_s", 0.0) or 0.0)
        if n_thresh <= 0 or window <= 0:
            return
        anchor = self._last_publish_ok_ts if self._last_publish_ok_ts is not None else self._t_start
        starved_s = now - anchor
        if self._consecutive_feed_failures >= n_thresh and starved_s >= window:
            self._fire_fatal(
                f"{self._consecutive_feed_failures} consecutive reconnect-"
                f"exhausted publish cycles; no successful publish for "
                f"{starved_s:.0f}s (threshold {n_thresh} cycles / {window:.0f}s)"
            )

    def _respawn_encoder_if_dead(self) -> bool:
        """Respawn the encoder subprocess if it died. Returns True on respawn.

        Injected test encoders (``self._lame is None``) are never respawned.
        The dead encoder's pipes are closed first so fds don't leak across
        restarts. Swap of ``self._encoder`` is a single attribute assignment
        (GIL-atomic); work() reads a local ref, so no lock is needed — see the
        comment in work().
        """
        proc = self._lame
        if proc is None:
            return False  # injected encoder (tests) — nothing to respawn
        if proc.poll() is None:
            return False  # still alive
        for f in (proc.stdin, proc.stdout):
            try:
                if f is not None:
                    f.close()
            except Exception:  # noqa: BLE001
                pass
        log.warning(
            "respawning encoder after unexpected death (previous rc=%s, mount=%s)",
            proc.returncode, self.cfg.mount,
        )
        self._spawn_encoder()
        self._ensure_stdout_read1()
        return True

    def _publish_loop(self) -> None:
        """Supervisor: keep the publisher running until GENUINE shutdown.

        SB7.3-E rewrite of the single-pass loop that died permanently at
        19:11:49 on 2026-06-18 ("publish loop exiting (stop=True)") while the
        daemon kept scanning. Each pass runs :meth:`_publish_once`; any exit
        without ``_stop`` set (unexpected exception, encoder death, stdout
        read failure) is logged loudly, counted in ``publish_loop_restarts``,
        backed off on the reconnector's schedule, and retried — respawning the
        encoder subprocess if it died. Only shutdown ends this thread.
        """
        while not self._stop.is_set():
            reason = "crash"
            try:
                reason = self._publish_once()
            except Exception as e:  # noqa: BLE001 — supervisor must survive anything
                self._note_publish_error(f"publish loop crashed: {e!r}")
                log.exception(
                    "publish loop crashed — restarting instead of dying "
                    "(no-third-state contract, see 2026-06-18 ground wedge)"
                )
            if self._stop.is_set():
                break
            self._publish_loop_restarts += 1
            sched = self.RESTART_BACKOFF_SCHEDULE
            wait = sched[min(self._restart_backoff_idx, len(sched) - 1)]
            self._restart_backoff_idx += 1
            log.error(
                "publish loop pass ended (%s) while the daemon is still "
                "running — restart #%d in %.2fs (mount=%s)",
                reason, self._publish_loop_restarts, wait, self.cfg.mount,
            )
            if self._stop.wait(wait):
                break
            try:
                self._respawn_encoder_if_dead()
            except Exception as e:  # noqa: BLE001 — retry on the next pass
                self._note_publish_error(f"encoder respawn failed: {e!r}")
                log.exception("encoder respawn failed — will retry after backoff")

        # Genuine shutdown.
        self._set_state(STATE_DISCONNECTED)
        log.info(
            "publish loop exiting (stop=%s shutdown_requested=%s)",
            self._stop.is_set(), self._shutdown_requested.is_set(),
        )

    def _publish_once(self) -> str:
        """One publisher pass: encoder stdout → reconnector → icecast.

        Returns a short reason string for the supervisor's restart log:
        ``"shutdown"`` (only non-restartable outcome), ``"no_encoder"``,
        ``"read_error"``, or ``"encoder_exited"``.
        """
        if self._encoder is None or self._encoder.stdout is None:
            log.error("publish loop has no encoder stdout")
            self._note_publish_error("publish loop has no encoder stdout")
            return "no_encoder"

        stdout = self._encoder.stdout
        while not self._stop.is_set():
            try:
                # read1, NOT read: BufferedReader.read(n) blocks until n
                # bytes accumulate — at 32 kbps CBR a 4096-byte chunk takes
                # ~1.0 s of real time to encode. shout.sync() below then
                # sleeps ANOTHER chunk-duration to pace the send. With a
                # full-chunk blocking read those two waits SERIALIZE and the
                # loop tops out at ~50% of real-time throughput; the pipe
                # backs up, ffmpeg stalls, GR work() blocks on encoder
                # stdin, and the SOURCE silently drops ~60% of samples
                # (observed 2026-06-12: mounts at 37% duty, "airband audio
                # poor"). read1 returns whatever is already buffered (>=1
                # byte), so the encoder fills the pipe WHILE sync() sleeps
                # and the waits overlap. Throughput = real-time.
                chunk = stdout.read1(self.INPUT_CHUNK_BYTES)
            except Exception as e:  # noqa: BLE001 — supervisor restarts us
                log.exception("encoder stdout read failed")
                self._note_publish_error(f"encoder stdout read failed: {e!r}")
                return "read_error"
            if not chunk:
                # encoder closed (EOF) or nothing buffered yet.
                time.sleep(0.05)
                if self._stop.is_set():
                    return "shutdown"
                proc = self._lame
                if proc is not None and proc.poll() is not None:
                    # Real encoder subprocess died. Hand back to the
                    # supervisor, which respawns it — the OLD code broke out
                    # of the loop here and publishing died forever.
                    log.warning("encoder process exited rc=%s", proc.returncode)
                    self._note_publish_error(
                        f"encoder exited rc={proc.returncode}"
                    )
                    return "encoder_exited"
                # Injected encoder (tests) or still-warming pipe: keep polling.
                continue

            # Try to push to icecast. The reconnector handles drops with
            # backoff. On total failure (max_attempts exhausted), mark
            # disconnected, count toward the fatal threshold, and keep
            # trying on the next chunk.
            ok = self.reconnector.feed(chunk)
            if not ok:
                self._set_state(STATE_DISCONNECTED)
            else:
                self._set_state(STATE_CONNECTED)
                # NO shout.sync() pacing here — deliberately. Two reasons
                # (2026-06-12, mounts measured at 37-54% of real-time):
                # 1. The pipeline is already paced upstream by the SDR
                #    sample clock; the GR work() -> encoder stdin path
                #    cannot outrun real time, so sync() adds delay without
                #    bounding anything that needs bounding.
                # 2. libshout times our 16 kHz MPEG-2 frames wrong (treats
                #    576-sample frames as 1152), so sync() paces at ~half
                #    real-time; the pipe backs up, GR blocks on encoder
                #    stdin, and the SOURCE silently drops the deficit.
                # Post-reconnect bursts are bounded by the OS pipe sizes
                # (~64 kB ≈ 16 s of 32 kbps audio), well inside icecast's
                # default 512 kB source queue.
            self._register_feed_result(ok, time.monotonic())
        return "shutdown"

    # -- shutdown / GR lifecycle ---------------------------------------------

    def request_shutdown(self) -> None:
        """Declare daemon shutdown INTENT (cheap, non-blocking, idempotent).

        Must be called before GR tears the flowgraph down (ChirpFlowgraph.stop()
        does this) so the GR-invoked :meth:`stop` is recognized as genuine.
        Also cancels any pending spurious-stop escalation watch.
        """
        self._shutdown_requested.set()

    def shutdown(self) -> None:
        """Full, intentional teardown: intent + the actual plumbing stop.

        The daemon-facing entry point. Safe to call multiple times, and safe
        to combine with a subsequent GR ``stop()`` (teardown is idempotent).
        """
        self.request_shutdown()
        self._teardown()

    def stop(self) -> bool:  # noqa: D401 — GR block-lifecycle signature
        """GR block-lifecycle stop. Returns True on success.

        GR calls this from the ``block_executor`` destructor whenever this
        block's scheduler thread exits — which happens BOTH on a genuine
        ``tb.stop()`` AND when the flowgraph winds itself down after an
        upstream failure (2026-06-18: RTL-SDR USB error → source WORK_DONE →
        done-ness propagated here → stop() → publishing dead ALL DAY while the
        daemon scanned on).

        Disambiguation is by daemon intent (``_shutdown_requested``):
          * intent present → genuine: run the (idempotent) teardown.
          * no intent → SPURIOUS: refuse to kill publishing; log CRITICAL,
            count it, and start the no-samples escalation watch.
        """
        if self._shutdown_requested.is_set():
            self._teardown()
            return True
        self._handle_spurious_stop()
        return True

    def _handle_spurious_stop(self) -> None:
        """A stop() arrived with the daemon still running — the 6/18 signature.

        Keep the publish machinery fully alive (encoder, reconnector, thread),
        make the event loudly visible, and arm a grace-window watch: if no
        samples reach work() within ``cfg.spurious_stop_fatal_s``, the source
        is dead and publishing real audio is impossible → escalate on_fatal so
        the daemon exits with a structured diagnostic instead of lying healthy.
        """
        self._spurious_stop_count += 1
        self._note_publish_error(
            "spurious GR stop() outside daemon shutdown "
            f"(count={self._spurious_stop_count})"
        )
        log.critical(
            "IcecastSink.stop() called OUTSIDE daemon shutdown (count=%d) — "
            "REFUSING to kill the publish loop. This is the 2026-06-18 ground "
            "wedge signature: an upstream source error (e.g. rtlsdr_read_async "
            "-5) wound the flowgraph down while the daemon kept running. "
            "mount=%s", self._spurious_stop_count, self.cfg.mount,
        )
        grace = float(getattr(self.cfg, "spurious_stop_fatal_s", 0.0) or 0.0)
        if grace <= 0:
            return
        threading.Thread(
            target=self._spurious_stop_watch,
            args=(time.monotonic(), grace),
            name="chirp-icecast-spurious-watch",
            daemon=True,
        ).start()

    def _spurious_stop_watch(self, t0: float, grace: float) -> None:
        """Grace-window watch armed by a spurious stop().

        Waits on the SHUTDOWN event (so a genuine shutdown cancels escalation
        immediately), then checks whether work() saw samples after the spurious
        stop. Samples resumed → transient hiccup, stand down. No samples → the
        flowgraph is dead under a live daemon: fire on_fatal.
        """
        if self._shutdown_requested.wait(timeout=grace):
            return  # genuine shutdown arrived — nothing to escalate
        if self._stop.is_set():
            return
        last = self._last_work_ts
        if last is not None and last > t0:
            log.warning(
                "samples resumed after spurious stop() — transient flowgraph "
                "hiccup, no escalation (mount=%s)", self.cfg.mount,
            )
            return
        self._fire_fatal(
            f"flowgraph stopped this sink outside daemon shutdown and no "
            f"samples reached work() for {grace:.0f}s — the source is dead; "
            f"the daemon must exit so systemd restarts the stack"
        )

    def _teardown(self) -> None:
        """The actual plumbing stop (old stop() body), now idempotent.

        Idempotency matters: ChirpFlowgraph.stop() calls shutdown() directly
        (because after a 6/18-style wind-down GR will never call stop() again),
        AND GR's block_executor may still call stop() afterwards on a normal
        shutdown. Both paths land here; only the first one acts.
        """
        with self._teardown_lock:
            if self._teardown_done:
                return
            self._teardown_done = True
        self._stop.set()
        # Close encoder stdin → encoder flushes remaining MP3 frames → publisher
        # thread drains them and exits.
        try:
            if self._encoder is not None and self._encoder.stdin is not None:
                try:
                    self._encoder.stdin.close()
                except Exception:
                    pass
        except Exception:
            pass
        if self._lame is not None:
            try:
                self._lame.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._lame.kill()
                try:
                    self._lame.wait(timeout=1.0)
                except Exception:
                    pass
        # Join publisher thread.
        pt = self._publish_thread
        if pt is not None:
            pt.join(timeout=2.0)
        try:
            self.publisher.close()
        except Exception:
            pass

    # -- snapshot for get_status -------------------------------------------

    @property
    def publish_loop_alive(self) -> bool:
        """Truthful liveness of the publisher thread — read DIRECTLY from the
        thread object, not from cached state. On 2026-06-18 every snapshot
        field looked plausible while the publish loop had been dead for hours;
        this is the field that would have said so."""
        pt = self._publish_thread
        return bool(pt is not None and pt.is_alive())

    def snapshot(self) -> dict:
        return {
            "icecast_state": self.state,
            "icecast_bytes_sent": int(self.bytes_sent),
            "icecast_reconnect_count": int(self.reconnect_count),
            "icecast_drop_count": int(self.drop_count),
            "icecast_host": self.cfg.host,
            "icecast_port": self.cfg.port,
            "icecast_mount": self.cfg.mount,
            "icecast_bitrate_kbps": self.cfg.bitrate_kbps,
            "icecast_sample_rate": self.cfg.sample_rate,
            # SB7.3-E truth-telling fields (2026-06-18 "lies healthy" wedge):
            "publish_loop_alive": self.publish_loop_alive,
            "publish_loop_restarts": int(self._publish_loop_restarts),
            "last_publish_error": self._last_publish_error,
            "last_publish_error_ts": self._last_publish_error_ts,
            "spurious_stop_count": int(self._spurious_stop_count),
            "shutdown_requested": self._shutdown_requested.is_set(),
        }


__all__ = [
    "IcecastReconnector",
    "IcecastSink",
    "IcecastSinkConfig",
    "STATE_CONNECTED",
    "STATE_DISCONNECTED",
    "STATE_NOT_CONFIGURED",
    "STATE_RECONNECTING",
]
