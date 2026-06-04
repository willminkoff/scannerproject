"""chirp.daemon — Phase 1 daemon entrypoint.

Runs as ``python3 -m chirp.daemon``. Phase 1 scope:

  - File-backed IQ source (no SDR — production rtl-airband stays untouched).
  - Pre-allocated channel pool (default size 1) wired at startup; add_channel
    claims a slot, remove_channel releases it. No runtime connect/disconnect.
  - UDP JSON command bus on 127.0.0.1:CHIRP_CMD_PORT (default 7400 airband /
    7401 ground), dispatching add/remove/set_*/get_status to the flowgraph.
  - Audio sink: raw float-32 samples (GR file_sink) at the per-channel
    audio_rate. The daemon does NOT write WAV — Phase 1 prompt asks only for
    a file proving audio is flowing. Phase 2 will introduce the mixer / shout
    sink.
  - SIGTERM / SIGINT → graceful shutdown of flowgraph + UDP server.

Config precedence (lowest to highest):
    chirp/config/defaults.json (committed defaults)
        ↳ env overrides (CHIRP_*)
            ↳ command-line flags (none yet in Phase 1)

Recognised env vars (all optional):
    CHIRP_BAND            airband | ground  (default airband)
    CHIRP_CMD_PORT        UDP port for command bus  (default 7400/7401)
    CHIRP_SOURCE          file:/abs/path  (Phase 1: file only)
    CHIRP_SOURCE_SAMP_RATE  sps as float  (default 1e6)
    CHIRP_AUDIO_OUT       file:/abs/path  (Phase 1: file only)
    CHIRP_MAX_CHANNELS    int  (default 1)
    CHIRP_EVENT_SINK      host:port  (optional async-event UDP listener)
    CHIRP_LOG_LEVEL       DEBUG | INFO | WARN | ERROR  (default INFO)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from gnuradio import blocks, gr

from chirp.cmd.schema import (
    AddChannelArgs,
    Envelope,
    GetStatusArgs,
    PROTOCOL_VERSION,
    RemoveChannelArgs,
    Response,
    SetFreqArgs,
    SetGainArgs,
    SetSquelchArgs,
)
from chirp.cmd.server import CommandServer, ServerConfig
from chirp.dsp.channel import Channel
from chirp.dsp.source_file import FileIQSource

log = logging.getLogger("chirp.daemon")


# Squelch level used to "park" an inactive pool slot. 0 dBFS = gate closed
# unless the input is louder than fullscale (i.e. effectively never).
PARKED_SQUELCH_DBFS = 0.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class DaemonConfig:
    band: str = "airband"
    cmd_host: str = "127.0.0.1"
    cmd_port: int = 7400
    source_kind: str = "file"  # Phase 1: only "file"
    source_path: Optional[str] = None
    source_samp_rate: float = 1e6
    audio_out_kind: str = "file"  # Phase 1: only "file"
    audio_out_path: Optional[str] = None
    audio_rate: float = 16000.0
    max_channels: int = 1
    event_sink: Optional[tuple[str, int]] = None
    log_level: str = "INFO"


def _parse_event_sink(spec: Optional[str]) -> Optional[tuple[str, int]]:
    if not spec:
        return None
    host, _, port = spec.rpartition(":")
    if not host or not port:
        raise ValueError(f"bad CHIRP_EVENT_SINK (want host:port): {spec!r}")
    return (host, int(port))


def _parse_source(spec: Optional[str]) -> tuple[str, Optional[str]]:
    if not spec:
        return ("file", None)
    if spec.startswith("file:"):
        return ("file", spec[len("file:"):])
    if spec == "sdr" or spec.startswith("sdr:"):
        return ("sdr", spec[len("sdr:"):] if ":" in spec else None)
    raise ValueError(f"unsupported CHIRP_SOURCE: {spec!r}")


def _parse_audio_out(spec: Optional[str]) -> tuple[str, Optional[str]]:
    if not spec:
        return ("file", None)
    if spec.startswith("file:"):
        return ("file", spec[len("file:"):])
    if spec.startswith("fifo:"):
        return ("fifo", spec[len("fifo:"):])
    raise ValueError(f"unsupported CHIRP_AUDIO_OUT: {spec!r}")


def load_config(defaults_path: Optional[Path] = None) -> DaemonConfig:
    here = Path(__file__).resolve().parent
    dp = defaults_path or (here / "config" / "defaults.json")
    raw: dict[str, Any] = {}
    if dp.is_file():
        try:
            with dp.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            log.warning("could not read %s: %s", dp, e)

    band = os.environ.get("CHIRP_BAND", raw.get("band", "airband"))
    default_port = 7400 if band == "airband" else 7401
    cmd_port = int(os.environ.get("CHIRP_CMD_PORT", raw.get("cmd_port", default_port)))

    src_kind, src_path = _parse_source(os.environ.get("CHIRP_SOURCE", raw.get("source")))
    audio_kind, audio_path = _parse_audio_out(os.environ.get("CHIRP_AUDIO_OUT", raw.get("audio_out")))

    return DaemonConfig(
        band=band,
        cmd_host=os.environ.get("CHIRP_CMD_HOST", raw.get("cmd_host", "127.0.0.1")),
        cmd_port=cmd_port,
        source_kind=src_kind,
        source_path=src_path,
        source_samp_rate=float(os.environ.get("CHIRP_SOURCE_SAMP_RATE", raw.get("source_samp_rate", 1e6))),
        audio_out_kind=audio_kind,
        audio_out_path=audio_path,
        audio_rate=float(os.environ.get("CHIRP_AUDIO_RATE", raw.get("audio_rate", 16000.0))),
        max_channels=int(os.environ.get("CHIRP_MAX_CHANNELS", raw.get("max_channels", 1))),
        event_sink=_parse_event_sink(os.environ.get("CHIRP_EVENT_SINK", raw.get("event_sink"))),
        log_level=os.environ.get("CHIRP_LOG_LEVEL", raw.get("log_level", "INFO")).upper(),
    )


# ---------------------------------------------------------------------------
# Flowgraph
# ---------------------------------------------------------------------------


@dataclass
class _Slot:
    index: int
    channel: Channel
    audio_sink: gr.basic_block
    audio_path: Path
    user_id: Optional[str] = None
    label: Optional[str] = None
    last_squelch_dbfs: float = PARKED_SQUELCH_DBFS
    last_gain_db: float = 0.0
    last_freq_mhz: Optional[float] = None
    # Used for hit_start/hit_end emission on squelch transitions.
    last_squelch_open: bool = False


class ChirpFlowgraph(gr.top_block):
    """Top-level GR flowgraph with a pre-allocated channel pool."""

    def __init__(self, cfg: DaemonConfig, server: CommandServer) -> None:
        super().__init__("chirp")
        self._cfg = cfg
        self._server = server
        self._lock = threading.RLock()
        self.slots: list[_Slot] = []
        self._by_id: dict[str, int] = {}

        # ---- Source ------------------------------------------------------
        if cfg.source_kind == "file":
            if not cfg.source_path:
                raise ValueError("CHIRP_SOURCE=file:/path required in Phase 1")
            self.source = FileIQSource(cfg.source_path, cfg.source_samp_rate, repeat=True)
        else:
            raise NotImplementedError(
                f"source_kind={cfg.source_kind!r} not in Phase 1 (file only)"
            )

        # ---- Channel pool -------------------------------------------------
        if cfg.max_channels < 1:
            raise ValueError("max_channels must be >= 1")
        if cfg.audio_out_kind != "file" or not cfg.audio_out_path:
            raise ValueError(
                "Phase 1 requires CHIRP_AUDIO_OUT=file:/abs/path (one file per slot)"
            )

        audio_base = Path(cfg.audio_out_path)
        audio_dir = audio_base.parent
        audio_dir.mkdir(parents=True, exist_ok=True)

        for i in range(cfg.max_channels):
            if cfg.max_channels == 1:
                path = audio_base
            else:
                # Insert _NN before the suffix.
                path = audio_base.with_name(f"{audio_base.stem}_{i:02d}{audio_base.suffix}")
            channel = Channel(
                samp_rate=cfg.source_samp_rate,
                audio_rate=cfg.audio_rate,
                center_freq_offset=0.0,
                squelch_dbfs=PARKED_SQUELCH_DBFS,
                gain_db=0.0,
            )
            sink = blocks.file_sink(gr.sizeof_float, str(path), False)
            sink.set_unbuffered(True)
            # Wire: source -> channel -> sink
            self.connect(self.source, channel)
            self.connect(channel, sink)
            self.slots.append(_Slot(
                index=i, channel=channel, audio_sink=sink, audio_path=path
            ))

        # Health probe thread: emits squelch transitions as hit_* events and
        # periodic level snapshots while running.
        self._health_thread: Optional[threading.Thread] = None
        self._health_stop = threading.Event()

    # -- pool helpers -------------------------------------------------------

    def _find_free_slot(self) -> Optional[_Slot]:
        with self._lock:
            for s in self.slots:
                if s.user_id is None:
                    return s
            return None

    def _slot_for(self, user_id: str) -> Optional[_Slot]:
        with self._lock:
            idx = self._by_id.get(user_id)
            return self.slots[idx] if idx is not None else None

    # -- dispatch callbacks -------------------------------------------------

    def dispatch(self, env: Envelope, args: Any) -> Response:
        cmd = env.cmd
        try:
            if cmd == "add_channel":
                return self._cmd_add_channel(env, args)
            if cmd == "remove_channel":
                return self._cmd_remove_channel(env, args)
            if cmd == "set_squelch":
                return self._cmd_set_squelch(env, args)
            if cmd == "set_freq":
                return self._cmd_set_freq(env, args)
            if cmd == "set_gain":
                return self._cmd_set_gain(env, args)
            if cmd == "get_status":
                return self._cmd_get_status(env, args)
        except Exception as e:  # noqa: BLE001
            log.exception("dispatch internal error: cmd=%s", cmd)
            return Response.make_error(env.id, f"internal: {e}")
        return Response.make_rejected(env.id, f"command not in Phase 1: {cmd}")

    def _freq_to_offset_hz(self, freq_mhz: float) -> float:
        """File source has no LO concept; treat freq_mhz directly as an offset
        from the source's logical center (0 Hz). The smoke-test generator
        places the carrier at +200 kHz, so add_channel with freq_mhz=0.2 hits.
        """
        return freq_mhz * 1e6

    def _cmd_add_channel(self, env: Envelope, args: AddChannelArgs) -> Response:
        with self._lock:
            if args.id in self._by_id:
                return Response.make_rejected(env.id, f"channel already exists: {args.id}")
            slot = self._find_free_slot()
            if slot is None:
                return Response.make_rejected(env.id, "channel pool exhausted")
            offset_hz = self._freq_to_offset_hz(args.freq_mhz)
            slot.channel.set_center_freq_offset(offset_hz)
            slot.channel.set_squelch(args.squelch_dbfs)
            slot.channel.set_gain(args.gain_db)
            slot.user_id = args.id
            slot.label = args.label
            slot.last_squelch_dbfs = args.squelch_dbfs
            slot.last_gain_db = args.gain_db
            slot.last_freq_mhz = args.freq_mhz
            self._by_id[args.id] = slot.index
            self._server.emit_event(
                "channel_added",
                ch=args.id, slot=slot.index,
                freq_mhz=args.freq_mhz, squelch_dbfs=args.squelch_dbfs, gain_db=args.gain_db,
            )
            return Response.make_ok(env.id, {"slot": slot.index, "audio_path": str(slot.audio_path)})

    def _cmd_remove_channel(self, env: Envelope, args: RemoveChannelArgs) -> Response:
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            # Park: slam squelch shut, retune to 0, drop mapping.
            slot.channel.set_squelch(PARKED_SQUELCH_DBFS)
            slot.channel.set_center_freq_offset(0.0)
            slot.channel.set_gain(0.0)
            self._by_id.pop(args.id, None)
            removed = slot.user_id
            slot.user_id = None
            slot.label = None
            slot.last_squelch_dbfs = PARKED_SQUELCH_DBFS
            slot.last_gain_db = 0.0
            slot.last_freq_mhz = None
            slot.last_squelch_open = False
            self._server.emit_event("channel_removed", ch=removed, slot=slot.index)
            return Response.make_ok(env.id, {"slot": slot.index})

    def _cmd_set_squelch(self, env: Envelope, args: SetSquelchArgs) -> Response:
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            slot.channel.set_squelch(args.dbfs)
            slot.last_squelch_dbfs = args.dbfs
            return Response.make_ok(env.id, {"dbfs": args.dbfs})

    def _cmd_set_freq(self, env: Envelope, args: SetFreqArgs) -> Response:
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            slot.channel.set_center_freq_offset(self._freq_to_offset_hz(args.mhz))
            slot.last_freq_mhz = args.mhz
            return Response.make_ok(env.id, {"mhz": args.mhz})

    def _cmd_set_gain(self, env: Envelope, args: SetGainArgs) -> Response:
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            slot.channel.set_gain(args.db)
            slot.last_gain_db = args.db
            return Response.make_ok(env.id, {"db": args.db})

    def _cmd_get_status(self, env: Envelope, args: GetStatusArgs) -> Response:
        with self._lock:
            channels = []
            for s in self.slots:
                if s.user_id is None:
                    continue
                snap = s.channel.snapshot()
                channels.append({
                    "id": s.user_id,
                    "label": s.label,
                    "slot": s.index,
                    "freq_mhz": s.last_freq_mhz,
                    "squelch_dbfs": s.last_squelch_dbfs,
                    "gain_db": s.last_gain_db,
                    "audio_path": str(s.audio_path),
                    "signal_level_dbfs": snap["signal_level_dbfs"],
                    "squelch_open": snap["squelch_open"],
                })
            return Response.make_ok(env.id, {
                "version": PROTOCOL_VERSION,
                "band": self._cfg.band,
                "source": {
                    "kind": self._cfg.source_kind,
                    "path": self._cfg.source_path,
                    "samp_rate": self._cfg.source_samp_rate,
                },
                "max_channels": self._cfg.max_channels,
                "channels": channels,
                "pool_free": sum(1 for s in self.slots if s.user_id is None),
            })

    # -- health / hit-event probe ------------------------------------------

    def _health_loop(self) -> None:
        while not self._health_stop.is_set():
            try:
                with self._lock:
                    for s in self.slots:
                        if s.user_id is None:
                            continue
                        is_open = s.channel.get_squelch_open()
                        if is_open != s.last_squelch_open:
                            evt = "hit_start" if is_open else "hit_end"
                            self._server.emit_event(
                                evt,
                                ch=s.user_id,
                                freq_mhz=s.last_freq_mhz,
                                level_dbfs=s.channel.get_signal_level_dbfs(),
                            )
                            s.last_squelch_open = is_open
            except Exception:
                log.exception("health loop iteration failed")
            self._health_stop.wait(0.2)

    def start_health(self) -> None:
        if self._health_thread is not None:
            return
        self._health_thread = threading.Thread(
            target=self._health_loop, name="chirp-health", daemon=True
        )
        self._health_thread.start()

    def stop_health(self) -> None:
        self._health_stop.set()
        if self._health_thread is not None:
            self._health_thread.join(timeout=2.0)
            self._health_thread = None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def main() -> int:
    cfg = load_config()
    _setup_logging(cfg.log_level)
    log.info("chirp starting band=%s cmd=%s:%d source=%s:%s out=%s:%s",
             cfg.band, cfg.cmd_host, cfg.cmd_port,
             cfg.source_kind, cfg.source_path,
             cfg.audio_out_kind, cfg.audio_out_path)

    # Build command server first (so we can pass it to the flowgraph for events).
    server = CommandServer(
        ServerConfig(host=cfg.cmd_host, port=cfg.cmd_port, event_sink=cfg.event_sink),
        dispatch=lambda env, args: tb.dispatch(env, args),  # late-bound via closure
    )

    tb = ChirpFlowgraph(cfg, server)
    tb.start()
    tb.start_health()
    try:
        server.start()
    except Exception:
        tb.stop_health()
        tb.stop()
        tb.wait()
        raise

    server.emit_event("daemon_ready", band=cfg.band, cmd_port=cfg.cmd_port)

    stop_evt = threading.Event()

    def _sig(signum, frame):  # noqa: ARG001
        log.info("signal %d received, shutting down", signum)
        stop_evt.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        while not stop_evt.is_set():
            time.sleep(0.25)
    finally:
        log.info("stopping flowgraph + server")
        server.emit_event("daemon_stopping", band=cfg.band)
        tb.stop_health()
        try:
            tb.stop()
            tb.wait()
        except Exception:
            log.exception("error stopping flowgraph")
        server.stop()
        log.info("chirp stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
