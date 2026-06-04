"""chirp.daemon — Phase 2 daemon entrypoint.

Runs as ``python3 -m chirp.daemon``. Phase 2 scope:

  - File-backed IQ source (no SDR — production rtl-airband stays untouched).
  - Pre-allocated 32-slot channel pool wired through an AudioMixer + master
    gain into ONE float-32 file sink. Each slot has its own Channel hier_block;
    unused slots are parked with squelch=0 dBFS so they emit zero through the
    mixer.
  - UDP JSON command bus on 127.0.0.1:CHIRP_CMD_PORT (default 7400 airband /
    7401 ground), dispatching add/remove/set_*/get_status/set_master_gain/
    reset + batched add_channel.
  - State persistence: on boot read /var/lib/chirp/<band>.state.json
    (env CHIRP_STATE_PATH overrides); on every mutation, atomically rewrite.
  - Hit detection: per-channel squelch-transition probe emits hit_start /
    hit_end events through the UDP event stream + appends to a JSONL hit log.
  - SIGTERM / SIGINT → graceful shutdown of flowgraph + UDP server.

Recognised env vars (all optional):
    CHIRP_BAND            airband | ground  (default airband)
    CHIRP_CMD_PORT        UDP port for command bus  (default 7400/7401)
    CHIRP_SOURCE          file:/abs/path  (Phase 1/2: file only)
    CHIRP_SOURCE_SAMP_RATE  sps as float  (default 1e6)
    CHIRP_AUDIO_OUT       file:/abs/path  (Phase 1/2: file only)
    CHIRP_MAX_CHANNELS    int  (default 32 in Phase 2)
    CHIRP_EVENT_SINK      host:port  (optional async-event UDP listener)
    CHIRP_LOG_LEVEL       DEBUG | INFO | WARN | ERROR  (default INFO)
    CHIRP_STATE_PATH      override default state path
    CHIRP_HIT_LOG         path for hit JSONL log (default /var/log/chirp/hits.jsonl)
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
    ChannelArgs,
    Envelope,
    GetStatusArgs,
    PROTOCOL_VERSION,
    RemoveChannelArgs,
    ResetArgs,
    Response,
    SetFreqArgs,
    SetGainArgs,
    SetMasterGainArgs,
    SetSquelchArgs,
)
from chirp.cmd.server import CommandServer, ServerConfig
from chirp.dsp.channel import Channel
from chirp.dsp.mixer import AudioMixer
from chirp.dsp.source_file import FileIQSource
from chirp.hit_detector import HitDetector
from chirp.state import ChannelState, ChirpState, StateStore, default_state_path

log = logging.getLogger("chirp.daemon")


# Squelch level used to "park" an inactive pool slot. 0 dBFS = gate closed
# unless the input is louder than fullscale (i.e. effectively never).
PARKED_SQUELCH_DBFS = 0.0


# Phase 2 default pool size. Phase 1 used 1.
DEFAULT_MAX_CHANNELS = 32


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class DaemonConfig:
    band: str = "airband"
    cmd_host: str = "127.0.0.1"
    cmd_port: int = 7400
    source_kind: str = "file"  # Phase 1/2: only "file"
    source_path: Optional[str] = None
    source_samp_rate: float = 1e6
    audio_out_kind: str = "file"  # Phase 1/2: only "file"
    audio_out_path: Optional[str] = None
    audio_rate: float = 16000.0
    max_channels: int = DEFAULT_MAX_CHANNELS
    event_sink: Optional[tuple[str, int]] = None
    log_level: str = "INFO"
    state_path: Optional[str] = None
    hit_log_path: Optional[str] = None


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
        max_channels=int(os.environ.get("CHIRP_MAX_CHANNELS", raw.get("max_channels", DEFAULT_MAX_CHANNELS))),
        event_sink=_parse_event_sink(os.environ.get("CHIRP_EVENT_SINK", raw.get("event_sink"))),
        log_level=os.environ.get("CHIRP_LOG_LEVEL", raw.get("log_level", "INFO")).upper(),
        state_path=os.environ.get("CHIRP_STATE_PATH", raw.get("state_path")),
        hit_log_path=os.environ.get("CHIRP_HIT_LOG", raw.get("hit_log_path")),
    )


# ---------------------------------------------------------------------------
# Flowgraph
# ---------------------------------------------------------------------------


@dataclass
class _Slot:
    index: int
    channel: Channel
    user_id: Optional[str] = None
    label: Optional[str] = None
    mode: str = "am"
    last_squelch_dbfs: float = PARKED_SQUELCH_DBFS
    last_gain_db: float = 0.0
    last_freq_mhz: Optional[float] = None
    # Set when channel was claimed; HitDetector uses this for warmup gating.
    claimed_at: Optional[float] = None


class ChirpFlowgraph(gr.top_block):
    """Top-level GR flowgraph with a 32-slot channel pool feeding one mixer."""

    def __init__(
        self,
        cfg: DaemonConfig,
        server: CommandServer,
        state_store: Optional[StateStore] = None,
    ) -> None:
        super().__init__("chirp")
        self._cfg = cfg
        self._server = server
        self._lock = threading.RLock()
        self.slots: list[_Slot] = []
        self._by_id: dict[str, int] = {}
        self._master_gain_db: float = 0.0

        # State store. None during pure unit tests; production wires a real one.
        if state_store is None:
            sp = cfg.state_path or str(default_state_path(cfg.band))
            self.state_store = StateStore(sp)
        else:
            self.state_store = state_store

        # ---- Source ------------------------------------------------------
        if cfg.source_kind == "file":
            if not cfg.source_path:
                raise ValueError("CHIRP_SOURCE=file:/path required in Phase 1/2")
            self.source = FileIQSource(cfg.source_path, cfg.source_samp_rate, repeat=True)
        else:
            raise NotImplementedError(
                f"source_kind={cfg.source_kind!r} not in Phase 1/2 (file only)"
            )

        # ---- Channel pool + mixer ----------------------------------------
        if cfg.max_channels < 1:
            raise ValueError("max_channels must be >= 1")
        if cfg.audio_out_kind != "file" or not cfg.audio_out_path:
            raise ValueError(
                "Phase 1/2 requires CHIRP_AUDIO_OUT=file:/abs/path"
            )

        audio_path = Path(cfg.audio_out_path)
        audio_path.parent.mkdir(parents=True, exist_ok=True)

        self.mixer = AudioMixer(n_inputs=cfg.max_channels, master_gain_db=0.0)
        self.audio_sink = blocks.file_sink(gr.sizeof_float, str(audio_path), False)
        self.audio_sink.set_unbuffered(True)
        self.connect(self.mixer, self.audio_sink)

        for i in range(cfg.max_channels):
            channel = Channel(
                samp_rate=cfg.source_samp_rate,
                audio_rate=cfg.audio_rate,
                center_freq_offset=0.0,
                squelch_dbfs=PARKED_SQUELCH_DBFS,
                gain_db=0.0,
            )
            # Wire: source -> channel -> mixer:port_i
            self.connect(self.source, channel)
            self.connect(channel, (self.mixer, i))
            self.slots.append(_Slot(index=i, channel=channel))

        self._audio_out_path = audio_path

        # Hit detector / health probe.
        self.hit_detector = HitDetector(
            slots=self.slots,
            server=self._server,
            hit_log_path=cfg.hit_log_path,
            warmup_s=1.0,
        )

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
            if cmd == "set_master_gain":
                return self._cmd_set_master_gain(env, args)
            if cmd == "reset":
                return self._cmd_reset(env, args)
            if cmd == "get_status":
                return self._cmd_get_status(env, args)
        except Exception as e:  # noqa: BLE001
            log.exception("dispatch internal error: cmd=%s", cmd)
            return Response.make_error(env.id, f"internal: {e}")
        return Response.make_rejected(env.id, f"unknown command: {cmd}")

    def _freq_to_offset_hz(self, freq_mhz: float) -> float:
        """File source has no LO concept; treat freq_mhz directly as an offset
        from the source's logical center (0 Hz). The smoke-test generator
        places the carrier at +200 kHz, so add_channel with freq_mhz=0.2 hits.
        """
        return freq_mhz * 1e6

    # -- state persistence helper ------------------------------------------

    def _persist_state(self) -> None:
        """Save current pool state to disk. Atomic; failure is logged, not raised."""
        try:
            chs = []
            for s in self.slots:
                if s.user_id is None:
                    continue
                chs.append(ChannelState(
                    id=s.user_id,
                    freq_mhz=s.last_freq_mhz if s.last_freq_mhz is not None else 0.001,
                    mode=s.mode,
                    squelch_dbfs=s.last_squelch_dbfs,
                    gain_db=s.last_gain_db,
                    label=s.label,
                ))
            st = ChirpState(
                band=self._cfg.band,
                master_gain_db=self._master_gain_db,
                channels=chs,
            )
            self.state_store.save(st)
        except Exception:
            log.exception("failed to persist state to %s", self.state_store.path)

    # -- restore from state file -------------------------------------------

    def restore_from_state(self) -> int:
        """Apply persisted channel list to the live pool. Returns number of
        channels restored. Called from main() after start() so the flowgraph
        is already running."""
        try:
            st = self.state_store.load()
        except Exception:
            log.exception("could not load state — starting empty")
            return 0
        restored = 0
        with self._lock:
            self._master_gain_db = st.master_gain_db
            self.mixer.set_master_gain(self._master_gain_db)
            for ch in st.channels:
                slot = self._find_free_slot()
                if slot is None:
                    log.warning("state has more channels than pool — dropping %s", ch.id)
                    continue
                if ch.id in self._by_id:
                    log.warning("state contains duplicate id %s — skipping", ch.id)
                    continue
                self._apply_channel_to_slot(slot, ch)
                restored += 1
        if restored:
            self._server.emit_event("state_restored", count=restored, band=self._cfg.band)
        return restored

    def _apply_channel_to_slot(self, slot: _Slot, ch: ChannelArgs | ChannelState) -> None:
        """Internal helper. Assumes lock held."""
        offset_hz = self._freq_to_offset_hz(ch.freq_mhz)
        slot.channel.set_center_freq_offset(offset_hz)
        slot.channel.set_squelch(ch.squelch_dbfs)
        slot.channel.set_gain(ch.gain_db)
        slot.user_id = ch.id
        slot.label = ch.label
        slot.mode = ch.mode
        slot.last_squelch_dbfs = ch.squelch_dbfs
        slot.last_gain_db = ch.gain_db
        slot.last_freq_mhz = ch.freq_mhz
        slot.claimed_at = time.time()
        self._by_id[ch.id] = slot.index

    # -- commands -----------------------------------------------------------

    def _cmd_add_channel(self, env: Envelope, args: AddChannelArgs) -> Response:
        """Phase 2: accepts EITHER a single-channel form (legacy) OR a batch
        (`{"channels": [...]}`). Schema normalises both to args.channels.
        All-or-nothing: if any slot can't be allocated, no slots change."""
        with self._lock:
            requested = args.channels
            # Pre-check: duplicates against existing pool, capacity.
            existing_dup = [c.id for c in requested if c.id in self._by_id]
            if existing_dup:
                return Response.make_rejected(
                    env.id, f"channel already exists: {existing_dup}"
                )
            free = sum(1 for s in self.slots if s.user_id is None)
            if free < len(requested):
                return Response.make_rejected(
                    env.id,
                    f"channel pool exhausted: need {len(requested)}, have {free}",
                )
            # Apply.
            applied = []
            for ch in requested:
                slot = self._find_free_slot()
                assert slot is not None  # pre-check
                self._apply_channel_to_slot(slot, ch)
                applied.append({
                    "id": ch.id, "slot": slot.index, "freq_mhz": ch.freq_mhz,
                })
                self._server.emit_event(
                    "channel_added",
                    ch=ch.id, slot=slot.index,
                    freq_mhz=ch.freq_mhz, squelch_dbfs=ch.squelch_dbfs,
                    gain_db=ch.gain_db,
                )
            self._persist_state()
            # Backward compat: single-channel form returns slot in flat shape.
            if len(applied) == 1:
                a0 = applied[0]
                return Response.make_ok(env.id, {
                    "slot": a0["slot"],
                    "audio_path": str(self._audio_out_path),
                    "added": applied,
                })
            return Response.make_ok(env.id, {
                "audio_path": str(self._audio_out_path),
                "added": applied,
                "count": len(applied),
            })

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
            slot.claimed_at = None
            self._server.emit_event("channel_removed", ch=removed, slot=slot.index)
            self._persist_state()
            return Response.make_ok(env.id, {"slot": slot.index})

    def _cmd_set_squelch(self, env: Envelope, args: SetSquelchArgs) -> Response:
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            # Race-safety: do NOT consult the noise estimator (which may not
            # have converged in the first ~1s). We apply the operator's value
            # directly to the squelch threshold. See test_radio_bugs.
            slot.channel.set_squelch(args.dbfs)
            slot.last_squelch_dbfs = args.dbfs
            self._persist_state()
            return Response.make_ok(env.id, {"dbfs": args.dbfs})

    def _cmd_set_freq(self, env: Envelope, args: SetFreqArgs) -> Response:
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            slot.channel.set_center_freq_offset(self._freq_to_offset_hz(args.mhz))
            slot.last_freq_mhz = args.mhz
            self._persist_state()
            return Response.make_ok(env.id, {"mhz": args.mhz})

    def _cmd_set_gain(self, env: Envelope, args: SetGainArgs) -> Response:
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            slot.channel.set_gain(args.db)
            slot.last_gain_db = args.db
            self._persist_state()
            return Response.make_ok(env.id, {"db": args.db})

    def _cmd_set_master_gain(self, env: Envelope, args: SetMasterGainArgs) -> Response:
        with self._lock:
            self._master_gain_db = args.db
            self.mixer.set_master_gain(args.db)
            self._persist_state()
            self._server.emit_event("master_gain_changed", db=args.db)
            return Response.make_ok(env.id, {"db": args.db})

    def _cmd_reset(self, env: Envelope, args: ResetArgs) -> Response:
        with self._lock:
            removed_ids = list(self._by_id.keys())
            for s in self.slots:
                if s.user_id is None:
                    continue
                s.channel.set_squelch(PARKED_SQUELCH_DBFS)
                s.channel.set_center_freq_offset(0.0)
                s.channel.set_gain(0.0)
                s.user_id = None
                s.label = None
                s.last_squelch_dbfs = PARKED_SQUELCH_DBFS
                s.last_gain_db = 0.0
                s.last_freq_mhz = None
                s.claimed_at = None
            self._by_id.clear()
            self._master_gain_db = 0.0
            self.mixer.set_master_gain(0.0)
            try:
                self.state_store.clear()
            except Exception:
                log.exception("state clear failed")
            self._server.emit_event("reset", removed=removed_ids)
            return Response.make_ok(env.id, {
                "removed": removed_ids,
                "pool_free": len(self.slots),
            })

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
                    "mode": s.mode,
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
                "master_gain_db": self._master_gain_db,
                "audio_path": str(self._audio_out_path),
                "channels": channels,
                "pool_free": sum(1 for s in self.slots if s.user_id is None),
            })

    # -- health / hit-event probe (delegates to HitDetector) ---------------

    def start_health(self) -> None:
        self.hit_detector.start()

    def stop_health(self) -> None:
        self.hit_detector.stop()

    # -- shutdown drain ----------------------------------------------------

    def shutdown_drain(self, drain_timeout: float = 2.0) -> None:
        """Graceful drain on shutdown. Prevents the master/slave wedge bug
        (rtl-airband SIGKILL'd mid-setter): we let pending command callbacks
        complete by acquiring the same lock they use, then stop. See
        test_radio_bugs.test_shutdown_drains_command_queue."""
        deadline = time.time() + drain_timeout
        # Try to acquire — if a command is mid-flight this blocks briefly.
        while time.time() < deadline:
            if self._lock.acquire(timeout=0.1):
                try:
                    return
                finally:
                    self._lock.release()
        log.warning("shutdown_drain timed out after %.1fs", drain_timeout)


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
    log.info("chirp starting band=%s cmd=%s:%d source=%s:%s out=%s:%s max_ch=%d",
             cfg.band, cfg.cmd_host, cfg.cmd_port,
             cfg.source_kind, cfg.source_path,
             cfg.audio_out_kind, cfg.audio_out_path, cfg.max_channels)

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

    # Restore channels from persisted state (best-effort).
    try:
        restored = tb.restore_from_state()
        if restored:
            log.info("restored %d channel(s) from state", restored)
    except Exception:
        log.exception("state restore failed (continuing with empty pool)")

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
        # Drain in-flight setters before stop() — see rtl-airband
        # master/slave-on-restart bug regression test.
        tb.shutdown_drain()
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
