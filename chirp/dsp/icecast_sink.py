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

    def __init__(
        self,
        cfg: IcecastSinkConfig,
        encoder: Optional[Any] = None,
        publisher: Optional[Any] = None,
        autostart_publisher: bool = True,
    ) -> None:
        super().__init__(name="IcecastSink", in_sig=[np.float32], out_sig=None)
        self.cfg = cfg
        self._stop = threading.Event()
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
        if n == 0 or self._stop.is_set():
            return n
        # float32 in [-1, 1] → int16 LE.
        # Channels emit zeros when squelched; lame produces silent MP3 frames
        # for constant-zero PCM, preserving the icecast-keepalive heuristic.
        clipped = np.clip(samples, -1.0, 1.0)
        i16 = (clipped * 32767.0).astype(np.int16)
        try:
            if self._encoder is not None and self._encoder.stdin is not None:
                self._encoder.stdin.write(i16.tobytes())
        except (BrokenPipeError, ValueError):
            # encoder died; signal GR to stop the block.
            log.error("encoder stdin closed unexpectedly")
            return -1
        except Exception:
            log.exception("encoder write failed")
            return -1
        return n

    # -- publisher loop -----------------------------------------------------

    def _publish_loop(self) -> None:
        """Read MP3 chunks from the encoder stdout and feed them to Icecast."""
        if self._encoder is None or self._encoder.stdout is None:
            log.error("publish loop has no encoder stdout")
            return

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
            except Exception:
                log.exception("encoder stdout read failed")
                break
            if not chunk:
                # encoder closed.
                time.sleep(0.05)
                if self._stop.is_set():
                    break
                # If encoder process is dead, exit loop.
                proc = self._lame
                if proc is not None and proc.poll() is not None:
                    log.warning("encoder process exited rc=%s", proc.returncode)
                    break
                continue

            # Try to push to icecast. The reconnector handles drops with
            # backoff. On total failure (max_attempts exhausted), mark
            # disconnected and keep trying on next chunk.
            before_drops = self.reconnector.drops
            ok = self.reconnector.feed(chunk)
            after_drops = self.reconnector.drops
            if not ok:
                self._set_state(STATE_DISCONNECTED)
            else:
                if after_drops != before_drops:
                    # Recovered after some drops.
                    self._set_state(STATE_CONNECTED)
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

        # Loop exit.
        self._set_state(STATE_DISCONNECTED)
        log.info("publish loop exiting (stop=%s)", self._stop.is_set())

    # -- shutdown -----------------------------------------------------------

    def stop(self) -> bool:  # noqa: D401 — GR hier_block signature
        """GR calls this on flowgraph stop. Returns True on success."""
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
        return True

    # -- snapshot for get_status -------------------------------------------

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
