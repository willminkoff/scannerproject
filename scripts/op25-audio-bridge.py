#!/usr/bin/env python3
"""Bridge OP25 UDP PCM into Icecast as the DIGITAL.mp3 mount.

The OP25 runtime writes /run/scannerproject/op25/instances.json with one
entry per channel. Each entry advertises the localhost UDP audio port that
multi_rx.py is sending to. This bridge keeps a single ffmpeg publisher alive,
mixes the available mono PCM inputs, and republishes the result to Icecast.
"""

from __future__ import annotations

import audioop
import json
import os
import select
import signal
import socket
import subprocess
import sys
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
BITRATE_KBPS = int(os.getenv("OP25_AUDIO_BITRATE", "32"))
SAMPLE_RATE = int(os.getenv("OP25_AUDIO_SAMPLE_RATE", "8000"))
CHANNELS = int(os.getenv("OP25_AUDIO_CHANNELS", "1"))
POLL_SEC = float(os.getenv("OP25_AUDIO_POLL_SEC", "0.02"))
HEALTH_CHECK_SEC = float(os.getenv("OP25_AUDIO_HEALTH_CHECK_SEC", "5.0"))
GATE_ENABLED = str(os.getenv("OP25_AUDIO_GATE", "1")).strip().lower() not in ("0", "false", "no", "off")
GATE_RELEASE_SEC = float(os.getenv("OP25_AUDIO_GATE_RELEASE_SEC", "1.5"))


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
        self.silence_frame = b"\x00" * max(2, int(SAMPLE_RATE * POLL_SEC) * 2)

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

    def _ffmpeg_cmd(self) -> list[str]:
        mount_url = f"icecast://{ICECAST_USER}:{ICECAST_PASSWORD}@{ICECAST_HOST}:{ICECAST_PORT}/{ICECAST_MOUNT}"
        return [
            FFMPEG_BIN,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-i",
            "pipe:0",
            "-acodec",
            "libmp3lame",
            "-b:a",
            f"{BITRATE_KBPS}k",
            "-content_type",
            "audio/mpeg",
            "-f",
            "mp3",
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

    def _drain_packets(self) -> list[bytes]:
        if not self.sockets:
            return []
        packets: list[bytes] = []
        ready, _, _ = select.select(self.sockets, [], [], POLL_SEC)
        for sock in ready:
            while True:
                try:
                    data, _ = sock.recvfrom(8192)
                except BlockingIOError:
                    break
                except OSError:
                    break
                if data:
                    packets.append(data)
        return packets

    def _mix_packets(self, packets: list[bytes]) -> bytes:
        cleaned = [_normalize_pcm(packet) for packet in packets if packet]
        cleaned = [packet for packet in cleaned if packet]
        if not cleaned:
            return self.silence_frame
        target_len = max(len(packet) for packet in cleaned)
        mix = _pad_pcm(cleaned[0], target_len)
        for packet in cleaned[1:]:
            mix = audioop.add(mix, _pad_pcm(packet, target_len), 2)
        return mix

    def _write_pcm(self, pcm: bytes) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(pcm)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._start_ffmpeg("ffmpeg pipe closed")

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
            while self.running:
                self._ensure_ffmpeg()
                packets = self._drain_packets()
                now = time.time()
                if packets:
                    self.last_audio_at = now
                    pcm = self._mix_packets(packets)
                else:
                    pcm = self.silence_frame
                if GATE_ENABLED and self.last_audio_at > 0.0:
                    if now - self.last_audio_at > GATE_RELEASE_SEC:
                        pcm = self.silence_frame
                self._write_pcm(pcm)
                time.sleep(POLL_SEC)
        finally:
            self._stop_ffmpeg()
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
