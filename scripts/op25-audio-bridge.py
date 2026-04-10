#!/usr/bin/env python3
"""Bridge OP25 UDP PCM into Icecast as the DIGITAL.mp3 mount.

The OP25 runtime writes /run/scannerproject/op25/instances.json with one
entry per channel. Each entry advertises the localhost UDP audio port that
multi_rx.py is sending to. This bridge keeps a single ffmpeg publisher alive,
mixes the available mono PCM inputs, and republishes the result to Icecast.

Local playback: when LOCAL_PLAY is enabled, the bridge also spawns a pw-cat
process that writes PCM directly to PipeWire for Bluetooth / local speaker
output, bypassing the Icecast encode-decode round-trip.  A file-based mute
flag lets the UI toggle local playback without restarting the bridge.
"""

from __future__ import annotations

import audioop
import collections
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


INSTANCE_MANIFEST = Path(os.getenv("OP25_AUDIO_INSTANCES_PATH", "/run/scannerproject/op25/instances.json"))
ICECAST_HOST = os.getenv("OP25_AUDIO_ICECAST_HOST", os.getenv("ICECAST_HOST", "127.0.0.1"))
ICECAST_PORT = int(os.getenv("OP25_AUDIO_ICECAST_PORT", os.getenv("ICECAST_PORT", "8000")))
ICECAST_USER = os.getenv("OP25_AUDIO_ICECAST_USER", os.getenv("ICECAST_SOURCE_USER", "source"))
ICECAST_PASSWORD = os.getenv("OP25_AUDIO_ICECAST_PASSWORD", os.getenv("ICECAST_SOURCE_PASSWORD", "062352"))
ICECAST_MOUNT = (os.getenv("OP25_AUDIO_ICECAST_MOUNT", "DIGITAL.mp3") or "DIGITAL.mp3").strip().lstrip("/")
FFMPEG_BIN = os.getenv("OP25_AUDIO_FFMPEG_BIN") or "ffmpeg"
BITRATE_KBPS = int(os.getenv("OP25_AUDIO_BITRATE", "96"))
SAMPLE_RATE = int(os.getenv("OP25_AUDIO_SAMPLE_RATE", "8000"))
CHANNELS = int(os.getenv("OP25_AUDIO_CHANNELS", "1"))
HEALTH_CHECK_SEC = float(os.getenv("OP25_AUDIO_HEALTH_CHECK_SEC", "5.0"))
GATE_ENABLED = str(os.getenv("OP25_AUDIO_GATE", "1")).strip().lower() not in ("0", "false", "no", "off")
GATE_RELEASE_SEC = float(os.getenv("OP25_AUDIO_GATE_RELEASE_SEC", "3.0"))

# Priority gating: when multiple systems have audio, only play the
# highest-priority one (first port in the list).  HOLDOFF prevents
# rapid switching by keeping the priority channel active for a brief
# period after its audio stops.
PRIORITY_GATE = str(os.getenv("OP25_AUDIO_PRIORITY_GATE", "1")).strip().lower() not in ("0", "false", "no", "off")
PRIORITY_HOLDOFF_SEC = float(os.getenv("OP25_AUDIO_PRIORITY_HOLDOFF_SEC", "1.5"))

# Audio normalization: boost quiet P25 audio before encoding.
# Target peak is ~80% of full scale (-2 dBFS) leaving headroom for MP3.
NORMALIZE_ENABLED = str(os.getenv("OP25_AUDIO_NORMALIZE", "1")).strip().lower() not in ("0", "false", "no", "off")
NORMALIZE_TARGET_PEAK = float(os.getenv("OP25_AUDIO_NORMALIZE_TARGET", "0.80"))  # fraction of full scale
NORMALIZE_MAX_GAIN = float(os.getenv("OP25_AUDIO_NORMALIZE_MAX_GAIN", "4.0"))    # cap amplification

# Output frame size: 20ms at 8kHz mono 16-bit = 320 bytes (160 samples).
# The output thread delivers exactly this many bytes per tick to ffmpeg,
# keeping the sample clock steady regardless of UDP input jitter.
OUTPUT_FRAME_SAMPLES = int(SAMPLE_RATE * 0.020)  # 160 samples = 20ms
OUTPUT_FRAME_BYTES = OUTPUT_FRAME_SAMPLES * 2     # 320 bytes (16-bit)
OUTPUT_TICK_SEC = OUTPUT_FRAME_SAMPLES / SAMPLE_RATE  # 0.020

# Ring buffer holds up to 500ms of audio (25 frames × 20ms).
RING_BUFFER_FRAMES = 25

# Jitter buffer: when voice starts after silence, wait until this many
# frames are buffered before the output thread starts consuming them.
# This absorbs UDP timing jitter and eliminates mid-voice dropouts.
# 5 frames = 100ms of pre-buffer.
JITTER_PREFILL_FRAMES = int(os.getenv("OP25_AUDIO_JITTER_FRAMES", "5"))

# Packet loss concealment (PLC): when the ring buffer empties during
# active voice, repeat the last frame with progressive fade instead of
# hard-cutting to silence. This hides short gaps (< PLC_MAX_FRAMES)
# caused by OP25 control-channel check-ins.
# 15 frames × 20ms = 300ms of concealment before silence.
PLC_MAX_FRAMES = int(os.getenv("OP25_AUDIO_PLC_FRAMES", "15"))
PLC_FADE_START = int(os.getenv("OP25_AUDIO_PLC_FADE_START", "5"))  # start fading after N repeats

# Local playback: write PCM directly to PipeWire via pw-cat, skipping the
# Icecast encode-decode round-trip that VLC required.
LOCAL_PLAY = str(os.getenv("OP25_AUDIO_LOCAL_PLAY", "1")).strip().lower() not in ("0", "false", "no", "off")
LOCAL_PLAY_SINK = os.getenv("OP25_AUDIO_LOCAL_SINK", os.getenv("VLC_PULSE_SINK", ""))
MUTE_FLAG = Path(os.getenv("OP25_AUDIO_MUTE_FLAG", "/run/scannerproject/op25/digital_local_mute"))
_PA_RUNTIME_DIR = os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

# HLS output: a separate ffmpeg reads from Icecast and writes HLS segments
# for browser players that cannot handle raw MP3 live streams.
HLS_ENABLED = str(os.getenv("OP25_AUDIO_HLS", "1")).strip().lower() not in ("0", "false", "no", "off")
HLS_DIR = Path(os.getenv("OP25_AUDIO_HLS_DIR", "/run/scannerproject/hls/digital"))
HLS_SEGMENT_SEC = int(os.getenv("OP25_AUDIO_HLS_SEGMENT_SEC", "2"))
HLS_LIST_SIZE = int(os.getenv("OP25_AUDIO_HLS_LIST_SIZE", "10"))


def _log(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} op25-audio-bridge: {message}", flush=True)


def _normalize_manifest(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        channels = payload.get("channels")
        if isinstance(channels, list):
            return [entry for entry in channels if isinstance(entry, dict)]
    return []


def load_instances(path: Path = INSTANCE_MANIFEST) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return _normalize_manifest(payload)


def _extract_ports(instances: list[dict[str, object]]) -> list[int]:
    ports: list[int] = []
    seen: set[int] = set()
    for entry in instances:
        raw_port = entry.get("udp_audio_port")
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            continue
        if port <= 0 or port in seen:
            continue
        seen.add(port)
        ports.append(port)
    return ports


def _normalize_pcm(packet: bytes) -> bytes:
    if not packet:
        return b""
    if len(packet) % 2:
        packet = packet[:-1]
    return packet


def _boost_and_limit(pcm: bytes) -> bytes:
    """Boost quiet PCM toward NORMALIZE_TARGET_PEAK, with soft limiting.

    P25 decoded audio is typically well below full scale.  Rather than
    cranking VLC gain (which clips the *decoded* MP3), we boost the raw
    PCM here — before MP3 encoding — so the encoder can shape the signal
    properly and the output stream is loud enough without post-decode
    amplification.
    """
    if not pcm or not NORMALIZE_ENABLED:
        return pcm
    try:
        peak = audioop.max(pcm, 2)
    except audioop.error:
        return pcm
    if peak <= 0:
        return pcm
    target = int(32767 * NORMALIZE_TARGET_PEAK)
    gain = min(target / peak, NORMALIZE_MAX_GAIN)
    if gain <= 1.02:
        # Already near target — skip processing
        return pcm
    boosted = audioop.mul(pcm, 2, gain)
    # Soft-clip: if any samples exceed 95% FS after boost, pull back
    new_peak = audioop.max(boosted, 2)
    if new_peak > 31128:  # 95% of 32767
        pullback = 31128 / new_peak
        boosted = audioop.mul(boosted, 2, pullback)
    return boosted


def _pad_pcm(packet: bytes, target_len: int) -> bytes:
    if len(packet) >= target_len:
        return packet[:target_len]
    return packet + (b"\x00" * (target_len - len(packet)))


class AudioBridge:
    def __init__(self, ports: list[int]) -> None:
        self.ports = ports
        self.sockets: list[socket.socket] = []
        self.proc: subprocess.Popen[bytes] | None = None
        self.running = True
        self.last_audio_at = 0.0
        self.last_health_check = 0.0
        self.silence_frame = b"\x00" * OUTPUT_FRAME_BYTES

        # Ring buffer: output thread pops frames at a constant rate.
        # Input thread pushes mixed audio frames as they arrive.
        self._ring: collections.deque[bytes] = collections.deque(maxlen=RING_BUFFER_FRAMES)
        self._ring_lock = threading.Lock()
        # Jitter buffer: when transitioning from silence to voice,
        # accumulate JITTER_PREFILL_FRAMES before output starts consuming.
        self._voice_active = False  # True once prefill threshold met
        # PLC: repeat last frame with fade to hide short gaps.
        self._last_voice_frame = self.silence_frame
        self._plc_count = 0  # consecutive concealment frames delivered

        # Priority gating: track which port is currently "active" and when
        # it last had audio, so lower-priority ports are suppressed.
        self._active_port: int | None = None
        self._port_last_audio: dict[int, float] = {}

        # Local playback via pw-cat (direct PipeWire output).
        self.local_proc: subprocess.Popen[bytes] | None = None
        self._local_muted: bool = MUTE_FLAG.exists()
        self._mute_check_counter: int = 0

        # HLS segmenter (separate ffmpeg reading from Icecast).
        self.hls_proc: subprocess.Popen[bytes] | None = None

    def _open_sockets(self) -> None:
        for port in self.ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.setblocking(False)
            self.sockets.append(sock)
            _log(f"listening on 127.0.0.1:{port}")

    def _close_sockets(self) -> None:
        for sock in self.sockets:
            try:
                sock.close()
            except OSError:
                pass
        self.sockets.clear()

    # -- Local playback (pw-cat → PipeWire) ----------------------------------

    def _local_play_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = _PA_RUNTIME_DIR
        pulse_native = os.path.join(_PA_RUNTIME_DIR, "pulse", "native")
        if os.path.exists(pulse_native):
            env["PULSE_SERVER"] = f"unix:{pulse_native}"
        if LOCAL_PLAY_SINK:
            env["PULSE_SINK"] = LOCAL_PLAY_SINK
        return env

    def _start_local_play(self, reason: str) -> None:
        self._stop_local_play()
        if not LOCAL_PLAY:
            return
        # Use ffmpeg with soxr resampler to upsample 8kHz→48kHz before
        # pw-cat.  This avoids PipeWire's basic resampler which adds
        # aliasing artifacts on the 6× upsample to the BT sink.
        _LOCAL_OUT_RATE = 48000
        target_flag = f"--target={LOCAL_PLAY_SINK} " if LOCAL_PLAY_SINK else ""
        shell_cmd = (
            f"{FFMPEG_BIN} -nostdin -hide_banner -loglevel warning "
            f"-f s16le -ar {SAMPLE_RATE} -ac {CHANNELS} -i pipe:0 "
            f"-af aresample=resampler=soxr:precision=28 "
            f"-f s16le -ar {_LOCAL_OUT_RATE} -ac {CHANNELS} pipe:1 "
            f"| pw-cat --playback --rate={_LOCAL_OUT_RATE} --channels={CHANNELS} "
            f"--format=s16 --latency=100ms {target_flag}-"
        )
        _log(f"starting local playback ({reason})")
        try:
            self.local_proc = subprocess.Popen(
                shell_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._local_play_env(),
                shell=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            _log("ffmpeg/pw-cat not found -- local playback disabled")

    def _stop_local_play(self) -> None:
        proc = self.local_proc
        if proc is None:
            return
        self.local_proc = None
        if proc.poll() is not None:
            return
        # The local playback is a shell pipeline (ffmpeg | pw-cat) running
        # in its own session.  Kill the entire process group so both sides
        # of the pipe are cleaned up.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _write_local(self, pcm: bytes) -> None:
        proc = self.local_proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(pcm)
        except (BrokenPipeError, OSError):
            self._start_local_play("pipe closed")

    # -- HLS output (separate ffmpeg reads from Icecast) -----------------------

    def _start_hls(self, reason: str) -> None:
        self._stop_hls()
        if not HLS_ENABLED:
            return
        try:
            HLS_DIR.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            _log(f"cannot create HLS dir {HLS_DIR} (permission denied)")
            return
        playlist = HLS_DIR / "live.m3u8"
        seg_pattern = str(HLS_DIR / "seg%05d.m4s")
        icecast_url = f"http://{ICECAST_HOST}:{ICECAST_PORT}/{ICECAST_MOUNT}"
        cmd = [
            FFMPEG_BIN,
            "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-fflags", "+nobuffer+discardcorrupt",
            "-f", "mp3",
            "-i", icecast_url,
            "-ar", "44100",
            "-c:a", "aac", "-b:a", f"{BITRATE_KBPS}k", "-ac", "1",
            "-f", "hls",
            "-hls_time", str(HLS_SEGMENT_SEC),
            "-hls_list_size", str(HLS_LIST_SIZE),
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_flags", "delete_segments",
            "-hls_segment_filename", seg_pattern,
            str(playlist),
        ]
        _log(f"starting HLS segmenter ({reason})")
        try:
            self.hls_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=None,
            )
        except FileNotFoundError:
            _log("ffmpeg not found -- HLS disabled")

    def _stop_hls(self) -> None:
        proc = self.hls_proc
        if proc is None:
            return
        self.hls_proc = None
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # Clean up stale segments
        if HLS_DIR.is_dir():
            for f in HLS_DIR.iterdir():
                try:
                    f.unlink()
                except Exception:
                    pass

    def _ensure_hls(self) -> None:
        if not HLS_ENABLED:
            return
        if self.hls_proc is not None and self.hls_proc.poll() is None:
            return
        # Only start if the Icecast mount is alive.
        if self._mount_alive():
            self._start_hls("respawn" if self.hls_proc is not None else "bootstrap")

    # -- Icecast output (ffmpeg) -----------------------------------------------

    def _ffmpeg_cmd(self) -> list[str]:
        mount_url = f"icecast://{ICECAST_USER}:{ICECAST_PASSWORD}@{ICECAST_HOST}:{ICECAST_PORT}/{ICECAST_MOUNT}"
        return [
            FFMPEG_BIN,
            "-nostdin",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "s16le",
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "-i", "pipe:0",
            "-af", "aresample=async=1:first_pts=0,acompressor=threshold=-18dB:ratio=4:attack=5:release=50:makeup=2dB,alimiter=limit=0.95:attack=5:release=50",
            "-acodec", "libmp3lame",
            "-b:a", f"{BITRATE_KBPS}k",
            "-write_xing", "0",
            "-flush_packets", "1",
            "-content_type", "audio/mpeg",
            "-f", "mp3",
            mount_url,
        ]

    def _ffmpeg_cmd_for_log(self) -> str:
        mount_url = f"icecast://{ICECAST_USER}:***@{ICECAST_HOST}:{ICECAST_PORT}/{ICECAST_MOUNT}"
        cmd = self._ffmpeg_cmd()
        if cmd:
            cmd[-1] = mount_url
        return " ".join(cmd)

    def _stop_ffmpeg(self) -> None:
        proc = self.proc
        if proc is None:
            return
        self.proc = None
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _start_ffmpeg(self, reason: str) -> None:
        self._stop_ffmpeg()
        cmd = self._ffmpeg_cmd()
        _log(f"starting ffmpeg ({reason})")
        _log(f"ffmpeg cmd: {self._ffmpeg_cmd_for_log()}")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None,
        )

    def _mount_alive(self) -> bool:
        mount_url = f"http://{ICECAST_HOST}:{ICECAST_PORT}/{ICECAST_MOUNT}"
        try:
            request = Request(mount_url, method="GET")
            with urlopen(request, timeout=2) as response:
                return 200 <= int(getattr(response, "status", 200)) < 400
        except URLError:
            return False
        except Exception:
            return False

    def _ensure_ffmpeg(self) -> None:
        now = time.time()
        if self.proc is None or self.proc.poll() is not None:
            self._start_ffmpeg("initial start" if self.proc is None else f"ffmpeg exited rc={self.proc.returncode}")
            self.last_health_check = now
            return
        if now - self.last_health_check < HEALTH_CHECK_SEC:
            return
        self.last_health_check = now
        if not self._mount_alive():
            _log("Icecast mount check failed; restarting ffmpeg")
            self._start_ffmpeg("icecast mount unavailable")

    def _drain_packets_by_port(self) -> dict[int, list[bytes]]:
        """Read all pending UDP packets grouped by socket index."""
        if not self.sockets:
            return {}
        by_port: dict[int, list[bytes]] = {}
        ready, _, _ = select.select(self.sockets, [], [], 0.005)
        for sock in ready:
            idx = self.sockets.index(sock)
            while True:
                try:
                    data, _ = sock.recvfrom(8192)
                except BlockingIOError:
                    break
                except OSError:
                    break
                if data:
                    by_port.setdefault(idx, []).append(data)
        return by_port

    def _select_priority_packets(self, by_port: dict[int, list[bytes]]) -> list[bytes]:
        """Pick packets from the highest-priority port that has audio.

        Port indices are priority-ordered (0 = highest).  Once a port is
        active, it holds priority for PRIORITY_HOLDOFF_SEC after its last
        packet to avoid rapid switching during brief gaps.
        """
        if not by_port:
            return []
        if not PRIORITY_GATE or len(self.sockets) < 2:
            # Flatten all packets when priority gating is off.
            return [pkt for pkts in by_port.values() for pkt in pkts]

        now = time.time()
        for idx in by_port:
            self._port_last_audio[idx] = now

        # If the currently active port still has audio, keep it.
        if self._active_port is not None and self._active_port in by_port:
            return by_port[self._active_port]

        # If the currently active port is within its holdoff window, stay.
        if self._active_port is not None:
            last = self._port_last_audio.get(self._active_port, 0.0)
            if (now - last) < PRIORITY_HOLDOFF_SEC:
                return []

        # Pick the lowest-index (highest-priority) port that has audio.
        for idx in sorted(by_port.keys()):
            self._active_port = idx
            return by_port[idx]

        return []

    def _mix_packets(self, packets: list[bytes]) -> bytes:
        cleaned = [_normalize_pcm(packet) for packet in packets if packet]
        cleaned = [packet for packet in cleaned if packet]
        if not cleaned:
            return b""
        target_len = max(len(packet) for packet in cleaned)
        mix = _pad_pcm(cleaned[0], target_len)
        for packet in cleaned[1:]:
            mix = audioop.add(mix, _pad_pcm(packet, target_len), 2)
        # Attenuate the mix to prevent clipping when multiple channels are
        # active. With N sources summed, each sample can be up to N× full
        # scale. Dividing by the source count keeps the mix within range.
        if len(cleaned) > 1:
            mix = audioop.mul(mix, 2, 1.0 / len(cleaned))
        return mix

    def _push_audio(self, pcm: bytes) -> None:
        """Normalize, split into frames, and push to ring buffer."""
        if not pcm:
            return
        # Boost quiet P25 audio before framing/encoding
        pcm = _boost_and_limit(pcm)
        with self._ring_lock:
            offset = 0
            while offset < len(pcm):
                chunk = pcm[offset:offset + OUTPUT_FRAME_BYTES]
                if len(chunk) < OUTPUT_FRAME_BYTES:
                    chunk = chunk + b"\x00" * (OUTPUT_FRAME_BYTES - len(chunk))
                self._ring.append(chunk)
                offset += OUTPUT_FRAME_BYTES

    def _pop_frame(self) -> bytes:
        """Pop one frame from the ring buffer, or return silence.

        Simple pass-through: no jitter buffer prefill, no PLC repeat.
        Just deliver what's available or silence when empty.  Let ffmpeg's
        aresample async handle smoothing.
        """
        with self._ring_lock:
            if self._ring:
                return self._ring.popleft()
        return self.silence_frame

    def _write_pcm(self, pcm: bytes) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(pcm)
        except (BrokenPipeError, OSError):
            self._start_ffmpeg("ffmpeg pipe closed")

    def _output_thread(self) -> None:
        """Deliver audio to ffmpeg and local playback at a constant rate.

        This thread runs independently of the UDP input loop, ensuring
        ffmpeg receives a smooth, clock-steady PCM stream. When no real
        audio is buffered, silence is delivered to keep the stream alive.

        Local playback (pw-cat) gets the same frames when unmuted.
        The mute flag file is polled every ~1 second.
        """
        next_tick = time.monotonic()
        flush_counter = 0
        while self.running:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(max(0, next_tick - now))
            next_tick += OUTPUT_TICK_SEC

            # Catch up if we fell behind (e.g. system load spike) -- skip
            # frames rather than delivering a burst that causes jitter.
            if next_tick < time.monotonic() - 0.1:
                next_tick = time.monotonic()

            frame = self._pop_frame()

            # Gate: if no real audio recently, force silence.
            if GATE_ENABLED and self.last_audio_at > 0.0:
                if (time.time() - self.last_audio_at) > GATE_RELEASE_SEC:
                    frame = self.silence_frame

            self._write_pcm(frame)

            # Local playback: write the same frame to pw-cat.
            if LOCAL_PLAY:
                self._mute_check_counter += 1
                if self._mute_check_counter >= 50:  # every ~1s (50 x 20ms)
                    self._mute_check_counter = 0
                    muted_now = MUTE_FLAG.exists()
                    if muted_now != self._local_muted:
                        self._local_muted = muted_now
                        if muted_now:
                            _log("local playback muted")
                            self._stop_local_play()
                        else:
                            _log("local playback unmuted")
                            self._start_local_play("unmuted")
                if not self._local_muted:
                    # Respawn pw-cat if it died.
                    if self.local_proc is None or self.local_proc.poll() is not None:
                        self._start_local_play("respawn")
                    self._write_local(frame)

            # Flush less aggressively -- every 10 frames (200ms) instead
            # of every write, reducing syscall overhead.
            flush_counter += 1
            if flush_counter >= 10:
                flush_counter = 0
                proc = self.proc
                if proc and proc.stdin:
                    try:
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                lproc = self.local_proc
                if lproc and lproc.stdin:
                    try:
                        lproc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass

    def run(self) -> int:
        if not self.ports:
            _log("no OP25 UDP audio ports found; exiting")
            return 1
        if not FFMPEG_BIN:
            _log("OP25_AUDIO_FFMPEG_BIN is empty; exiting")
            return 1
        self._open_sockets()
        try:
            self._start_ffmpeg("bootstrap")

            # Start local playback if enabled and not muted.
            if LOCAL_PLAY and not self._local_muted:
                self._start_local_play("bootstrap")
            elif LOCAL_PLAY and self._local_muted:
                _log("local playback muted at startup (mute flag present)")
            elif not LOCAL_PLAY:
                _log("local playback disabled (OP25_AUDIO_LOCAL_PLAY=0)")

            # Start the output thread -- delivers audio to ffmpeg (and
            # optionally pw-cat) at a constant 20ms tick rate.
            out_thread = threading.Thread(target=self._output_thread, daemon=True)
            out_thread.start()

            # Input loop: drain UDP packets, select priority, push to ring buffer.
            while self.running:
                self._ensure_ffmpeg()
                self._ensure_hls()
                by_port = self._drain_packets_by_port()
                packets = self._select_priority_packets(by_port)
                if packets:
                    self.last_audio_at = time.time()
                    pcm = self._mix_packets(packets)
                    if pcm:
                        self._push_audio(pcm)
                else:
                    # Brief sleep to avoid busy-waiting when no audio.
                    time.sleep(0.005)
        finally:
            self._stop_hls()
            self._stop_ffmpeg()
            self._stop_local_play()
            self._close_sockets()
        return 0


def _handle_signal(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    instances = load_instances()
    ports = _extract_ports(instances)
    _log(f"loaded {len(instances)} channel(s) from {INSTANCE_MANIFEST}")
    _log(f"discovered audio ports: {ports if ports else 'none'}")
    bridge = AudioBridge(ports)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        return bridge.run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
