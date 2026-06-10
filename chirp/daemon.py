"""chirp.daemon — Phase 3 daemon entrypoint.

Runs as ``python3 -m chirp.daemon``. Phase 3 adds end-to-end audio publish:

  - File-backed IQ source (no SDR — production rtl-airband stays untouched).
  - Pre-allocated 32-slot channel pool wired through an AudioMixer + master
    gain into either a file sink OR a libshout-backed Icecast publisher.
  - UDP JSON command bus on 127.0.0.1:CHIRP_CMD_PORT (default 7400 airband /
    7401 ground), dispatching add/remove/set_*/get_status/set_master_gain/
    reset + batched add_channel + subscribe/unsubscribe.
  - State persistence: on boot read /var/lib/chirp/<band>.state.json
    (env CHIRP_STATE_PATH overrides); on every mutation, atomically rewrite.
  - Hit detection: per-channel squelch-transition probe emits hit_start /
    hit_end events through the UDP event stream + appends to a JSONL hit log.
  - Icecast publish (Phase 3): CHIRP_AUDIO_OUT=icecast:host:port:/mount:pass
    spins up an IcecastSink that encodes via lame and pushes to the
    given mountpoint with exponential-backoff reconnect. Fallback to file
    output if the initial connection fails (logged loudly).
  - SIGTERM / SIGINT → graceful shutdown of flowgraph + UDP server.

Recognised env vars (all optional):
    CHIRP_BAND            airband | ground  (default airband)
    CHIRP_CMD_PORT        UDP port for command bus  (default 7400/7401)
    CHIRP_SOURCE          file:/abs/path  (Phase 1/2/3: file only)
    CHIRP_SOURCE_SAMP_RATE  sps as float  (default 1e6)
    CHIRP_AUDIO_OUT       file:/abs/path  OR  icecast:host:port:/mount:pass
    CHIRP_AUDIO_RATE      audio sample rate (default 16000)
    CHIRP_ICECAST_BITRATE_KBPS  MP3 bitrate (default 32)
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
    SetVadThresholdArgs,
    SetVadBypassArgs,
)
from chirp.cmd.server import CommandServer, ServerConfig
from chirp.dsp.channel import Channel
from chirp.dsp.cluster_planner import PlanChannel
from chirp.dsp.icecast_sink import IcecastSink, IcecastSinkConfig, STATE_NOT_CONFIGURED
from chirp.dsp.lo_scheduler import (
    DEFAULT_DWELL_S as LO_DEFAULT_DWELL_S,
    DEFAULT_MAX_CLUSTERS as LO_DEFAULT_MAX_CLUSTERS,
    LoScheduler,
)
from chirp.dsp.mixer import AudioMixer
from chirp.dsp.priority_gate import PriorityGate
from chirp.dsp.source_file import FileIQSource
from chirp.dsp.source_sdr import SdrIQSource, SdrSourceConfig
from chirp.hit_detector import HitDetector
from chirp.state import ChannelState, ChirpState, StateStore, default_state_path

log = logging.getLogger("chirp.daemon")


# ---------------------------------------------------------------------------
# systemd integration (sd_notify)
#
# The gr-demod@.service unit is Type=notify (see
# chirp/systemd/gr-demod@.service.template).  We must signal READY=1
# after startup completes and WATCHDOG=1 periodically once running, or
# systemd kills us.  python3-systemd is installed on Micro; the import
# is wrapped defensively so unit-test environments without it still
# import this module.
#
# See DESIGN_sdrplay_wedge_fix.md for full rationale.
try:  # pragma: no cover — env-dependent
    from systemd import daemon as _sd
    _HAVE_SD = True
except ImportError:  # pragma: no cover
    _sd = None  # type: ignore[assignment]
    _HAVE_SD = False


def _sd_notify(state: Optional[str], status: Optional[str] = None) -> None:
    """Send sd_notify state + optional STATUS line.

    No-op when systemd python bindings are not importable (test
    environments, dev laptops).  Under Type=notify on Micro, a missing
    READY=1 means systemd kills the daemon at TimeoutStartSec — which
    is correct behaviour for a misconfigured install.
    """
    if not _HAVE_SD:
        return
    parts: list[str] = []
    if state:
        parts.append(state)
    if status:
        parts.append(f"STATUS={status}")
    if not parts:
        return
    try:
        _sd.notify("\n".join(parts))
    except Exception:  # noqa: BLE001
        log.exception("sd_notify failed (state=%r status=%r)", state, status)


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
    pool_mode: str = "am"  # Phase 4a: per-band pool demod mode ("am" or "nfm")
    cmd_host: str = "127.0.0.1"
    cmd_port: int = 7400
    source_kind: str = "file"  # Phase 1/2/3: only "file"; Phase 4b adds "sdr"
    source_path: Optional[str] = None
    source_samp_rate: float = 1e6
    # Phase 4b SDR source fields (used when source_kind == "sdr").
    sdr_device_args: Optional[str] = None
    sdr_center_freq_hz: float = 0.0
    sdr_gain_db: float = 30.0
    sdr_gain_mode_auto: bool = False
    sdr_bandwidth_hz: float = 0.0
    sdr_ppm: float = 0.0
    sdr_antenna: Optional[str] = None
    sdr_element_gains: dict = field(default_factory=dict)
    audio_out_kind: str = "file"  # Phase 1/2: "file"; Phase 3 adds "icecast"
    audio_out_path: Optional[str] = None
    audio_rate: float = 16000.0
    # Per-channel post-decimation LPF cutoff (Hz) — the demod bandwidth.
    # Default 12.5 kHz (full 25 kHz AM channel); airband.json tightens to
    # 8 kHz for AM-voice SNR (carrier +-~4 kHz fits, less noise admitted).
    channel_bw_hz: float = 12500.0
    # AM AGC ceiling (linear) + attack rate. Caps the old 96 dB / fast-attack
    # AGC that pumped up the noise floor on weak signals. AM-only.
    agc_max_gain: float = 1000.0
    agc_attack: float = 0.1
    # AGC decay rate (lower = slower ramp-up on signal loss ~= hang/hold).
    agc_decay: float = 1e-4
    # AM voice band-pass edges (Hz). Defaults match the prior hardcoded values.
    audio_bandpass_low_hz: float = 300.0
    audio_bandpass_high_hz: float = 3500.0
    # AM voice denoise (RNNoise via ffmpeg arnndn at the icecast encoder).
    denoise: bool = False
    denoise_model: str = ""
    # Post-arnndn make-up gain (dB) to compensate RNNoise voice attenuation.
    denoise_gain_db: float = 0.0
    # Optional ffmpeg audio filter chain (presence-boost EQ etc.). Non-empty
    # routes the encoder through ffmpeg (EQ-only if denoise off). Empty = lame.
    audio_eq: str = ""
    max_channels: int = DEFAULT_MAX_CHANNELS
    event_sink: Optional[tuple[str, int]] = None
    log_level: str = "INFO"
    state_path: Optional[str] = None
    hit_log_path: Optional[str] = None
    # Phase 3 icecast publish fields (populated when audio_out_kind == "icecast").
    icecast_host: Optional[str] = None
    icecast_port: Optional[int] = None
    icecast_mount: Optional[str] = None
    icecast_password: Optional[str] = None
    icecast_bitrate_kbps: int = 32
    # File fallback path when icecast init fails. Defaults to a tmp file.
    icecast_fallback_file: str = "/tmp/chirp_audio_fallback.f32"
    # Phase 4-pre LO scheduler config.  When max_channels < 2 or the
    # channel list fits in one cluster these have no effect.
    lo_dwell_sec: float = LO_DEFAULT_DWELL_S
    lo_max_clusters: int = LO_DEFAULT_MAX_CLUSTERS
    # Scan-hold: latch the LO on a cluster while a TX is live instead of
    # hopping mid-transmission.  Opt-in per band.  Only affects multi-cluster
    # plans (single-cluster pools never rotate).
    scan_hold_enabled: bool = False
    scan_hold_hang_sec: float = 2.0
    scan_hold_max_sec: float = 30.0
    # Priority gate: within a cluster, pass exactly one open channel's audio
    # at a time (most-recently-opened latch).  Opt-in per band; independent
    # of scan_hold so the two can be tested separately.
    priority_gate_enabled: bool = False


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
    if spec.startswith("icecast:"):
        # Phase 3: keep the raw remainder; caller will parse fields.
        return ("icecast", spec[len("icecast:"):])
    raise ValueError(f"unsupported CHIRP_AUDIO_OUT: {spec!r}")


def _parse_icecast_spec(rem: str) -> tuple[str, int, str, str]:
    """Parse host:port:/mount:password.

    The mount always starts with '/'. Password may itself contain ':' chars, so
    we split from the LEFT for host/port/mount and treat the remainder as the
    password.
    """
    # host:port:/mount:password
    # Split into at most 4 parts using the FIRST 3 colons, then the password
    # gets whatever's left.
    parts = rem.split(":", 3)
    if len(parts) < 4:
        raise ValueError(
            f"bad icecast spec (need host:port:/mount:password): {rem!r}"
        )
    host, port_str, mount, password = parts
    if not mount.startswith("/"):
        raise ValueError(f"icecast mount must start with '/': {mount!r}")
    _PROD_MOUNTS = ("/ANALOG.mp3", "/ANALOG_GROUND.mp3", "/DIGITAL.mp3", "/VFO.mp3")
    if mount in _PROD_MOUNTS:
        # Phase 4d cutover: opt-in env CHIRP_ALLOW_PROD_MOUNT=1 lifts the
        # Phase 3 guard.  Default behaviour (no env) keeps the refusal so
        # accidental test runs do not stomp on production audio.
        _allow = os.environ.get("CHIRP_ALLOW_PROD_MOUNT", "").strip().lower()
        if _allow not in ("1", "true", "yes"):
            raise ValueError(
                f"chirp refuses to publish to production mount {mount!r} by "
                f"default; set CHIRP_ALLOW_PROD_MOUNT=1 (Phase 4d cutover) to "
                f"override, or use /CHIRP_TEST.mp3 / /CHIRP_GROUND_TEST.mp3 "
                f"for testing"
            )
    try:
        port = int(port_str)
    except ValueError as e:
        raise ValueError(f"bad icecast port {port_str!r}: {e}") from e
    return host, port, mount, password


def _as_bool(val: Any) -> bool:
    """Coerce a config/env value (str or bool) to bool.

    Env vars arrive as strings, so ``bool("false")`` would be True — parse
    the usual truthy tokens instead.  A real bool passes through.
    """
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def load_config(defaults_path: Optional[Path] = None) -> DaemonConfig:
    """Resolve daemon config from JSON file + env overrides.

    Phase 4a: when defaults_path is not given, we pick the per-band file
    chirp/config/<CHIRP_BAND>.json (airband / ground) instead of the old
    one-size defaults.json. Falls back to defaults.json for backward compat
    if no per-band file exists.
    """
    here = Path(__file__).resolve().parent
    band_env = os.environ.get("CHIRP_BAND")
    if defaults_path is None:
        candidates = []
        if band_env:
            candidates.append(here / "config" / f"{band_env}.json")
        candidates.append(here / "config" / "airband.json")
        candidates.append(here / "config" / "defaults.json")
        dp = next((c for c in candidates if c.is_file()), candidates[0])
    else:
        dp = defaults_path
    raw: dict[str, Any] = {}
    if dp.is_file():
        try:
            with dp.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"invalid JSON in chirp config {dp}: {e}"
            ) from e
        except Exception as e:
            log.warning("could not read %s: %s", dp, e)

    band = band_env or raw.get("band", "airband")
    default_port = 7400 if band == "airband" else 7401
    cmd_port = int(os.environ.get("CHIRP_CMD_PORT", raw.get("cmd_port", default_port)))

    src_kind, src_path = _parse_source(os.environ.get("CHIRP_SOURCE", raw.get("source")))
    audio_kind, audio_path = _parse_audio_out(os.environ.get("CHIRP_AUDIO_OUT", raw.get("audio_out")))

    icecast_host: Optional[str] = None
    icecast_port: Optional[int] = None
    icecast_mount: Optional[str] = None
    icecast_password: Optional[str] = None
    if audio_kind == "icecast":
        icecast_host, icecast_port, icecast_mount, icecast_password = \
            _parse_icecast_spec(audio_path or "")

    default_mode = "nfm" if band == "ground" else "am"
    pool_mode = os.environ.get("CHIRP_POOL_MODE", raw.get("pool_mode", default_mode))
    if pool_mode not in ("am", "nfm"):
        raise ValueError(f"invalid pool_mode: {pool_mode!r} (want am|nfm)")
    return DaemonConfig(
        band=band,
        pool_mode=pool_mode,
        cmd_host=os.environ.get("CHIRP_CMD_HOST", raw.get("cmd_host", "127.0.0.1")),
        cmd_port=cmd_port,
        source_kind=src_kind,
        source_path=src_path,
        source_samp_rate=float(os.environ.get("CHIRP_SOURCE_SAMP_RATE", raw.get("source_samp_rate", 1e6))),
        audio_out_kind=audio_kind,
        audio_out_path=audio_path,
        audio_rate=float(os.environ.get("CHIRP_AUDIO_RATE", raw.get("audio_rate", 16000.0))),
        channel_bw_hz=float(os.environ.get(
            "CHIRP_CHANNEL_BW_HZ",
            raw.get("channel_bw_hz", 12500.0),
        )),
        agc_max_gain=float(os.environ.get(
            "CHIRP_AGC_MAX_GAIN",
            raw.get("agc_max_gain", 1000.0),
        )),
        agc_attack=float(os.environ.get(
            "CHIRP_AGC_ATTACK",
            raw.get("agc_attack", 0.1),
        )),
        agc_decay=float(os.environ.get(
            "CHIRP_AGC_DECAY",
            raw.get("agc_decay", 1e-4),
        )),
        audio_bandpass_low_hz=float(os.environ.get(
            "CHIRP_AUDIO_BANDPASS_LOW_HZ",
            raw.get("audio_bandpass_low_hz", 300.0),
        )),
        audio_bandpass_high_hz=float(os.environ.get(
            "CHIRP_AUDIO_BANDPASS_HIGH_HZ",
            raw.get("audio_bandpass_high_hz", 3500.0),
        )),
        denoise=str(os.environ.get(
            "CHIRP_DENOISE",
            str(raw.get("denoise", "false")),
        )).strip().lower() in ("1", "true", "yes", "on"),
        denoise_model=os.environ.get(
            "CHIRP_DENOISE_MODEL",
            raw.get("denoise_model", "") or "",
        ),
        denoise_gain_db=float(os.environ.get(
            "CHIRP_DENOISE_GAIN_DB",
            raw.get("denoise_gain_db", 0.0),
        )),
        audio_eq=os.environ.get(
            "CHIRP_AUDIO_EQ",
            raw.get("audio_eq", "") or "",
        ),
        max_channels=int(os.environ.get("CHIRP_MAX_CHANNELS", raw.get("max_channels", DEFAULT_MAX_CHANNELS))),
        event_sink=_parse_event_sink(os.environ.get("CHIRP_EVENT_SINK", raw.get("event_sink"))),
        log_level=os.environ.get("CHIRP_LOG_LEVEL", raw.get("log_level", "INFO")).upper(),
        state_path=os.environ.get("CHIRP_STATE_PATH", raw.get("state_path")),
        hit_log_path=os.environ.get(
            "CHIRP_HIT_LOG",
            raw.get("hit_log_path") or f"/var/log/chirp/{band}_hits.jsonl",
        ),
        icecast_host=icecast_host,
        icecast_port=icecast_port,
        icecast_mount=icecast_mount,
        icecast_password=icecast_password,
        icecast_bitrate_kbps=int(os.environ.get("CHIRP_ICECAST_BITRATE_KBPS",
                                                raw.get("icecast_bitrate_kbps", 32))),
        icecast_fallback_file=os.environ.get(
            "CHIRP_ICECAST_FALLBACK_FILE",
            raw.get("icecast_fallback_file", "/tmp/chirp_audio_fallback.f32"),
        ),
        sdr_device_args=os.environ.get(
            "CHIRP_SDR_DEVICE_ARGS",
            (raw.get("sdr") or {}).get("device_args"),
        ),
        sdr_center_freq_hz=float(os.environ.get(
            "CHIRP_SDR_CENTER_FREQ_HZ",
            (raw.get("sdr") or {}).get("center_freq_hz", 0.0),
        )),
        sdr_gain_db=float(os.environ.get(
            "CHIRP_SDR_GAIN_DB",
            (raw.get("sdr") or {}).get("gain_db", 30.0),
        )),
        sdr_gain_mode_auto=str(os.environ.get(
            "CHIRP_SDR_GAIN_MODE_AUTO",
            str((raw.get("sdr") or {}).get("gain_mode_auto", "false")),
        )).strip().lower() in ("1", "true", "yes"),
        sdr_bandwidth_hz=float(os.environ.get(
            "CHIRP_SDR_BANDWIDTH_HZ",
            (raw.get("sdr") or {}).get("bandwidth_hz", 0.0),
        )),
        sdr_ppm=float(os.environ.get(
            "CHIRP_SDR_PPM",
            (raw.get("sdr") or {}).get("ppm", 0.0),
        )),
        sdr_antenna=os.environ.get(
            "CHIRP_SDR_ANTENNA",
            (raw.get("sdr") or {}).get("antenna"),
        ),
        sdr_element_gains=(raw.get("sdr") or {}).get("element_gains", {}) or {},
        lo_dwell_sec=float(os.environ.get(
            "CHIRP_LO_DWELL_SEC",
            raw.get("lo_dwell_sec", LO_DEFAULT_DWELL_S),
        )),
        lo_max_clusters=int(os.environ.get(
            "CHIRP_LO_MAX_CLUSTERS",
            raw.get("lo_max_clusters", LO_DEFAULT_MAX_CLUSTERS),
        )),
        scan_hold_enabled=_as_bool(os.environ.get(
            "CHIRP_SCAN_HOLD_ENABLED",
            raw.get("scan_hold_enabled", False),
        )),
        scan_hold_hang_sec=float(os.environ.get(
            "CHIRP_SCAN_HOLD_HANG_SEC",
            raw.get("scan_hold_hang_sec", 2.0),
        )),
        scan_hold_max_sec=float(os.environ.get(
            "CHIRP_SCAN_HOLD_MAX_SEC",
            raw.get("scan_hold_max_sec", 30.0),
        )),
        priority_gate_enabled=_as_bool(os.environ.get(
            "CHIRP_PRIORITY_GATE_ENABLED",
            raw.get("priority_gate_enabled", False),
        )),
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
    # SB5 2026-06-09 squelch redesign: per-channel VAD gate state.
    last_vad_threshold: float = 50.0
    last_vad_bypass: bool = False


class ChirpFlowgraph(gr.top_block):
    """Top-level GR flowgraph with a 32-slot channel pool feeding one mixer.

    Threading / lock-order discipline
    ---------------------------------
    Two reentrant locks are involved in the daemon's hot paths:

    - ``D-lock`` = ``ChirpFlowgraph._lock`` (this class).
    - ``S-lock`` = ``LoScheduler._lock`` (the cluster-rotation state machine).

    Canonical order: **any thread that takes both MUST acquire D-lock
    first**.  The cmd-server thread already does so (``_cmd_get_status``
    acquires D, then calls ``lo_scheduler.snapshot()`` which acquires S;
    ``_cmd_add_channel`` / ``_cmd_remove_channel`` / ``_cmd_set_freq`` /
    ``_cmd_reset`` acquire D, then call ``_invalidate_and_apply_now`` →
    ``lo_scheduler.step()`` which acquires S).  The scheduler's own
    thread previously violated this — ``step()`` took S, then its
    callbacks (``_scheduler_park_channels`` etc.) reacquired D — which
    produced the 2026-06-04 21:26:21 EDT wedge.  The fix lives in
    ``LoScheduler.step()`` itself: it now acquires the injected daemon
    lock BEFORE its internal S-lock.  See
    ``DESIGN_lo_scheduler_lockfix.md``.
    """

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
                raise ValueError("CHIRP_SOURCE=file:/path required for file source")
            self.source = FileIQSource(cfg.source_path, cfg.source_samp_rate, repeat=True)
        elif cfg.source_kind == "sdr":
            if not cfg.sdr_device_args:
                raise ValueError(
                    "sdr source requires sdr.device_args "
                    "(or CHIRP_SDR_DEVICE_ARGS env)"
                )
            if cfg.sdr_center_freq_hz <= 0:
                raise ValueError(
                    "sdr source requires sdr.center_freq_hz > 0"
                )
            sdr_cfg = SdrSourceConfig(
                device_args=cfg.sdr_device_args,
                sample_rate=cfg.source_samp_rate,
                center_freq_hz=cfg.sdr_center_freq_hz,
                gain_db=cfg.sdr_gain_db,
                gain_mode_auto=cfg.sdr_gain_mode_auto,
                bandwidth_hz=cfg.sdr_bandwidth_hz,
                ppm=cfg.sdr_ppm,
                antenna=cfg.sdr_antenna,
                element_gains=cfg.sdr_element_gains or {},
            )
            self.source = SdrIQSource(sdr_cfg)
            log.info(
                "sdr source: args=%r rate=%.0f sps center=%.6f MHz gain=%.1f dB",
                cfg.sdr_device_args, cfg.source_samp_rate,
                cfg.sdr_center_freq_hz / 1e6, cfg.sdr_gain_db,
            )
        else:
            raise NotImplementedError(
                f"source_kind={cfg.source_kind!r} not supported (want 'file' or 'sdr')"
            )

        # ---- Channel pool + mixer ----------------------------------------
        if cfg.max_channels < 1:
            raise ValueError("max_channels must be >= 1")

        self.mixer = AudioMixer(n_inputs=cfg.max_channels, master_gain_db=0.0)

        # Audio sink wiring.
        #   file     → blocks.file_sink (Phase 1/2 behaviour, smoke tests)
        #   icecast  → IcecastSink (Phase 3). Falls back to file_sink at
        #              icecast_fallback_file if initial connect fails.
        self.icecast_sink: Optional[IcecastSink] = None
        if cfg.audio_out_kind == "icecast":
            if not (cfg.icecast_host and cfg.icecast_port
                    and cfg.icecast_mount and cfg.icecast_password):
                raise ValueError("icecast mode requires host/port/mount/password")
            sink_cfg = IcecastSinkConfig(
                host=cfg.icecast_host,
                port=int(cfg.icecast_port),
                mount=cfg.icecast_mount,
                password=cfg.icecast_password,
                bitrate_kbps=int(cfg.icecast_bitrate_kbps),
                sample_rate=int(cfg.audio_rate),
                denoise=bool(cfg.denoise),
                denoise_model=cfg.denoise_model,
                denoise_gain_db=float(cfg.denoise_gain_db),
                audio_eq=cfg.audio_eq,
            )
            ice_ok = False
            try:
                self.icecast_sink = IcecastSink(sink_cfg)
                ice_ok = True
            except Exception:
                log.exception("IcecastSink instantiation failed — falling back to file output")
                self.icecast_sink = None

            if ice_ok and self.icecast_sink is not None:
                self.audio_sink = self.icecast_sink
                audio_path = Path(cfg.icecast_mount)  # for snapshotting
                log.info("audio sink: icecast %s:%d%s @ %d kbps",
                         cfg.icecast_host, cfg.icecast_port,
                         cfg.icecast_mount, cfg.icecast_bitrate_kbps)
            else:
                fallback = Path(cfg.icecast_fallback_file)
                fallback.parent.mkdir(parents=True, exist_ok=True)
                self.audio_sink = blocks.file_sink(gr.sizeof_float, str(fallback), False)
                self.audio_sink.set_unbuffered(True)
                audio_path = fallback
                log.warning("audio sink fallback active: file=%s", fallback)
        else:
            if not cfg.audio_out_path:
                raise ValueError(
                    "file audio sink requires CHIRP_AUDIO_OUT=file:/abs/path"
                )
            audio_path = Path(cfg.audio_out_path)
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            self.audio_sink = blocks.file_sink(gr.sizeof_float, str(audio_path), False)
            self.audio_sink.set_unbuffered(True)

        self.connect(self.mixer, self.audio_sink)

        for i in range(cfg.max_channels):
            channel = Channel(
                samp_rate=cfg.source_samp_rate,
                audio_rate=cfg.audio_rate,
                channel_bw_hz=cfg.channel_bw_hz,
                agc_max_gain=cfg.agc_max_gain,
                agc_attack=cfg.agc_attack,
                agc_decay=cfg.agc_decay,
                audio_bandpass_low_hz=cfg.audio_bandpass_low_hz,
                audio_bandpass_high_hz=cfg.audio_bandpass_high_hz,
                center_freq_offset=0.0,
                squelch_dbfs=PARKED_SQUELCH_DBFS,
                gain_db=0.0,
                mode=cfg.pool_mode,
            )
            # Wire: source -> channel -> mixer:port_i
            self.connect(self.source, channel)
            self.connect(channel, (self.mixer, i))
            self.slots.append(_Slot(index=i, channel=channel))

        self._audio_out_path = audio_path

        # Phase 4-pre: LO retuning scheduler.  Constructed BEFORE the
        # hit detector so we can pass the scheduler's
        # ``current_cluster_center_hz`` callback to the detector for
        # hit-log tagging.  Started by main() after the flowgraph is
        # running (see start_health()).
        self.lo_scheduler = LoScheduler(
            get_channels=self._get_plan_channels,
            retune_to=self._scheduler_retune_to,
            park_channels=self._scheduler_park_channels,
            unpark_channels=self._scheduler_unpark_channels,
            emit_event=self._server.emit_event,
            # Use source sample rate as the IQ window width.  The chirp
            # channel-pool decimation chain assumes >= 1 Msps, and
            # operators bump source_samp_rate to 2e6 for Phase 4d.
            iq_bw_hz=cfg.source_samp_rate,
            dwell_s=cfg.lo_dwell_sec,
            max_clusters=cfg.lo_max_clusters,
            # Canonical lock order: the scheduler acquires THIS daemon
            # lock before its own internal lock inside step(), eliminating
            # the D↔S inversion that caused the 2026-06-04 21:26 EDT
            # wedge.  See chirp/dsp/lo_scheduler.py module docstring +
            # DESIGN_lo_scheduler_lockfix.md.
            daemon_lock=self._lock,
            # Scan-hold: latch the LO on a live cluster instead of hopping
            # mid-TX.  is_open reads a live channel's squelch (reentrant on
            # the daemon lock — step() already holds it when it calls this).
            is_open=self._scheduler_is_open,
            scan_hold_enabled=cfg.scan_hold_enabled,
            scan_hold_hang_sec=cfg.scan_hold_hang_sec,
            scan_hold_max_sec=cfg.scan_hold_max_sec,
        )

        # Priority gate: single-active-channel latch driven by the hit
        # detector's 5 Hz squelch poll.  None when disabled so channels stay
        # at unity priority gain (no muting).
        self.priority_gate = (
            PriorityGate(emit_event=self._server.emit_event)
            if cfg.priority_gate_enabled
            else None
        )

        # Hit detector / health probe.  Phase 4-pre: pass the scheduler's
        # current-cluster-center callback so every hit event includes the
        # LO state at hit-start time.
        self.hit_detector = HitDetector(
            slots=self.slots,
            server=self._server,
            hit_log_path=cfg.hit_log_path,
            warmup_s=1.0,
            get_cluster_center_hz=self.lo_scheduler.current_cluster_center_hz,
            priority_gate=self.priority_gate,
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
            if cmd == "set_vad_threshold":
                return self._cmd_set_vad_threshold(env, args)
            if cmd == "set_vad_bypass":
                return self._cmd_set_vad_bypass(env, args)
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
        """Convert an absolute channel frequency (MHz) to the per-channel
        xlating offset (Hz) expected by chirp.dsp.Channel.

        File source: the synthesized fixtures place their carrier as a raw
        offset in MHz from logical 0 Hz (the smoke-test fixture has its
        carrier at +200 kHz, so add_channel --freq 0.2 hits). We pass the
        value through.

        SDR source: the LO is real and centered at sdr_center_freq_hz.
        Channels are at absolute RF; the xlating filter needs the delta
        from the LO.
        """
        if self._cfg.source_kind == "sdr":
            return (freq_mhz * 1e6) - float(self._cfg.sdr_center_freq_hz)
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
                    vad_threshold=float(getattr(s, "last_vad_threshold", 50.0)),
                    vad_bypass=bool(getattr(s, "last_vad_bypass", False)),
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
        # Phase 4-pre: any restored channels must trigger a plan compute.
        if restored:
            self._invalidate_and_apply_now()
        return restored

    def _apply_channel_to_slot(self, slot: _Slot, ch: ChannelArgs | ChannelState) -> None:
        """Internal helper. Assumes lock held.

        Phase 4-pre: park the channel BEFORE setting ``user_id`` so the
        hit detector (which polls without the daemon lock) never sees a
        live-but-uncatalogued channel firing hits during the gap between
        ``add_channel`` returning and the scheduler's first apply.  The
        scheduler's :py:meth:`invalidate_and_apply` (called by the
        command handler right after we return) will unpark the channel
        if it belongs to the active cluster.  Single-cluster pools see
        the channel un-park within microseconds.
        """
        offset_hz = self._freq_to_offset_hz(ch.freq_mhz)
        slot.channel.set_center_freq_offset(offset_hz)
        slot.channel.set_squelch(ch.squelch_dbfs)
        slot.channel.set_gain(ch.gain_db)
        # Phase 4-pre: park before publishing user_id so hit detector
        # never sees a fresh-but-already-firing channel.
        slot.channel.set_parked(True)
        slot.user_id = ch.id
        slot.label = ch.label
        slot.mode = ch.mode
        slot.last_squelch_dbfs = ch.squelch_dbfs
        slot.last_gain_db = ch.gain_db
        # SB5 2026-06-11 — restore VAD per-channel state from persistence.
        slot.last_vad_threshold = float(getattr(ch, "vad_threshold", 50.0))
        slot.last_vad_bypass = bool(getattr(ch, "vad_bypass", False))
        try:
            slot.channel.set_vad_threshold(slot.last_vad_threshold)
            slot.channel.set_vad_bypass(slot.last_vad_bypass)
        except Exception:
            log.exception("VAD restore failed for slot %s", slot.user_id)
        slot.last_freq_mhz = ch.freq_mhz
        slot.claimed_at = time.time()
        self._by_id[ch.id] = slot.index

    def _invalidate_and_apply_now(self) -> None:
        """Mark the scheduler plan stale and step it once synchronously.

        Phase 4-pre.  Used by every pool-mutating command so the
        scheduler reacts in the same critical section that mutated the
        pool — preventing the hit detector (which runs on its own
        thread) from seeing a transient inconsistent state where a
        channel is in the pool but not yet parked/unparked per plan.

        Safe to call while holding ``self._lock`` — RLock is reentrant
        and the scheduler's callbacks acquire the same lock.
        """
        self.lo_scheduler.invalidate()
        try:
            self.lo_scheduler.step()
        except Exception:
            log.exception("scheduler immediate-apply failed (will catch up on next tick)")

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
            # Phase 4a: pool is mode-homogeneous; reject channels whose mode
            # doesn't match the pool. Two-daemon coexistence relies on this.
            wrong_mode = [c.id for c in requested if c.mode != self._cfg.pool_mode]
            if wrong_mode:
                return Response.make_rejected(
                    env.id,
                    f"channel mode mismatch: pool={self._cfg.pool_mode}, "
                    f"requested {wrong_mode} != pool mode",
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
            # Phase 4-pre: pool membership changed → recompute + apply
            # NOW so the scheduler parks the non-active-cluster channels
            # before the hit detector's next tick.
            self._invalidate_and_apply_now()
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
            self._invalidate_and_apply_now()  # Phase 4-pre
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

    def _cmd_set_vad_threshold(self, env: Envelope, args: SetVadThresholdArgs) -> Response:
        """SB5 squelch redesign: set per-channel VAD score threshold."""
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            slot.channel.set_vad_threshold(args.threshold)
            slot.last_vad_threshold = float(args.threshold)
            self._persist_state()
            return Response.make_ok(env.id, {"threshold": float(args.threshold)})

    def _cmd_set_vad_bypass(self, env: Envelope, args: SetVadBypassArgs) -> Response:
        """SB5 squelch redesign: bypass the VAD gate (passthrough)."""
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            slot.channel.set_vad_bypass(bool(args.bypass))
            slot.last_vad_bypass = bool(args.bypass)
            self._persist_state()
            return Response.make_ok(env.id, {"bypass": bool(args.bypass)})

    def _cmd_set_freq(self, env: Envelope, args: SetFreqArgs) -> Response:
        with self._lock:
            slot = self._slot_for(args.id)
            if slot is None:
                return Response.make_rejected(env.id, f"unknown channel: {args.id}")
            slot.channel.set_center_freq_offset(self._freq_to_offset_hz(args.mhz))
            slot.last_freq_mhz = args.mhz
            self._persist_state()
            # Phase 4-pre: channel freq changed → cluster plan stale.
            self._invalidate_and_apply_now()
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
            self._invalidate_and_apply_now()  # Phase 4-pre
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
                vad_snap = {}
                try:
                    vad_snap = s.channel.vad_snapshot()
                except Exception:
                    pass
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
                    # Phase 4-pre: parked channels are dormant on the
                    # LO scheduler's other clusters.
                    "is_parked": snap.get("is_parked", False),
                    # SB5 2026-06-09 squelch redesign — per-channel VAD gate.
                    "vad_threshold": float(vad_snap.get("threshold", s.last_vad_threshold)),
                    "vad_bypass": bool(vad_snap.get("bypass", s.last_vad_bypass)),
                    "vad_last_score": float(vad_snap.get("last_score", 0.0)),
                    "vad_last_open": bool(vad_snap.get("last_open", False)),
                })
            data = {
                "version": PROTOCOL_VERSION,
                "band": self._cfg.band,
                "pool_mode": self._cfg.pool_mode,
                "source": {
                    "kind": self._cfg.source_kind,
                    "path": self._cfg.source_path,
                    "samp_rate": self._cfg.source_samp_rate,
                    "sdr_device_args": self._cfg.sdr_device_args,
                    "sdr_center_freq_hz": self._cfg.sdr_center_freq_hz,
                    "sdr_gain_db": self._cfg.sdr_gain_db,
                },
                "max_channels": self._cfg.max_channels,
                "channel_bw_hz": self._cfg.channel_bw_hz,
                "agc_max_gain": self._cfg.agc_max_gain,
                "agc_attack": self._cfg.agc_attack,
                "agc_decay": self._cfg.agc_decay,
                "audio_bandpass_low_hz": self._cfg.audio_bandpass_low_hz,
                "audio_bandpass_high_hz": self._cfg.audio_bandpass_high_hz,
                "denoise_enabled": bool(self._cfg.denoise),
                "denoise_gain_db": self._cfg.denoise_gain_db,
                "audio_eq": self._cfg.audio_eq,
                "master_gain_db": self._master_gain_db,
                "audio_path": str(self._audio_out_path),
                "channels": channels,
                "pool_free": sum(1 for s in self.slots if s.user_id is None),
            }
            # Phase 3: surface icecast publisher state.
            if self.icecast_sink is not None:
                snap = self.icecast_sink.snapshot()
                data.update({
                    "icecast_state": snap["icecast_state"],
                    "icecast_bytes_sent": snap["icecast_bytes_sent"],
                    "icecast_reconnect_count": snap["icecast_reconnect_count"],
                    "icecast_drop_count": snap["icecast_drop_count"],
                    "icecast_mount": snap["icecast_mount"],
                    "icecast_bitrate_kbps": snap["icecast_bitrate_kbps"],
                })
            else:
                data.update({
                    "icecast_state": STATE_NOT_CONFIGURED,
                    "icecast_bytes_sent": 0,
                    "icecast_reconnect_count": 0,
                    "icecast_drop_count": 0,
                })
            # Phase 4-pre: surface LO scheduler state for the dashboard.
            data["lo_scheduler"] = self.lo_scheduler.snapshot()
            data["priority_gate"] = {
                "enabled": self.priority_gate is not None,
                "selected": (
                    self.priority_gate.selected
                    if self.priority_gate is not None
                    else None
                ),
            }
            return Response.make_ok(env.id, data)

    # -- LO scheduler callbacks (Phase 4-pre) -----------------------------

    def _get_plan_channels(self) -> list[PlanChannel]:
        """Snapshot of pool channels for the cluster planner.

        Called from the scheduler thread.  Acquires the daemon lock for
        consistent reads against add/remove/set_freq.
        """
        with self._lock:
            out: list[PlanChannel] = []
            for s in self.slots:
                if s.user_id is None or s.last_freq_mhz is None:
                    continue
                out.append(PlanChannel(
                    id=s.user_id,
                    freq_hz=s.last_freq_mhz * 1e6,
                    # Phase 4-pre: priority is informational only.  Hit-log
                    # weighting wires up in Phase 5.
                    recent_hits=0.0,
                ))
            return out

    def _scheduler_retune_to(self, hz: float) -> None:
        """Retune the source LO to ``hz`` and re-baseline channel offsets.

        File source: no-op (the file has no LO concept; carriers sit at
        the operator's chosen freq_mhz directly).  This keeps the file-
        source unit tests deterministic.

        SDR source: ``source.set_center_freq(hz)`` and then walk every
        claimed slot recomputing its xlating-filter offset against the
        new LO so the channel stays demodulating its intended absolute
        RF frequency.
        """
        with self._lock:
            if self._cfg.source_kind != "sdr":
                return
            try:
                self.source.set_center_freq(float(hz))
            except Exception:
                log.exception(
                    "scheduler: source.set_center_freq(%.6f MHz) failed",
                    hz / 1e6,
                )
                # Don't re-baseline offsets on retune failure — channels
                # remain on the old LO's offsets, which still demodulate
                # correctly (we didn't actually move the LO).
                return
            # Remember the LO position so future add_channel calls use
            # the right offset.  (DaemonConfig is a dataclass — mutable.)
            self._cfg.sdr_center_freq_hz = float(hz)
            for s in self.slots:
                if s.user_id is None or s.last_freq_mhz is None:
                    continue
                new_offset = (s.last_freq_mhz * 1e6) - float(hz)
                s.channel.set_center_freq_offset(new_offset)

    def _scheduler_is_open(self, cid: str) -> bool:
        """Squelch-open state for one channel id, for the scheduler's
        scan-hold check.  Reentrant on the daemon lock: step() already holds
        it when it calls this, so this is a free RLock re-entry on the same
        thread (no D↔S inversion)."""
        with self._lock:
            slot = self._slot_for(cid)
            if slot is None:
                return False
            try:
                return bool(slot.channel.get_squelch_open())
            except Exception:
                return False

    def _scheduler_park_channels(self, ids) -> None:
        with self._lock:
            for cid in ids:
                slot = self._slot_for(cid)
                if slot is not None:
                    slot.channel.set_parked(True)

    def _scheduler_unpark_channels(self, ids) -> None:
        with self._lock:
            for cid in ids:
                slot = self._slot_for(cid)
                if slot is not None:
                    slot.channel.set_parked(False)
                    # Restart warmup window so the first second of audio
                    # after a hop doesn't trigger downstream alerts on
                    # the noise-estimator settling.  Mirrors the
                    # claimed_at semantic from add_channel.
                    slot.claimed_at = time.time()

    # -- health / hit-event probe (delegates to HitDetector) ---------------

    def start_health(self) -> None:
        self.hit_detector.start()
        # Phase 4-pre: LO scheduler runs alongside the hit detector.
        # Its initial step (planner recompute + apply cluster 0) happens
        # within the first tick (~250 ms), so the LO is correctly
        # positioned before any operator command arrives.
        self.lo_scheduler.start()

    def stop_health(self) -> None:
        # Stop scheduler first so it doesn't fire retune callbacks
        # against a tearing-down flowgraph.
        self.lo_scheduler.stop()
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

    # Surface startup phase to systemd / `systemctl status` so an operator
    # can tell "currently opening" from "wedged in sdrplay_api_Open".
    _sd_notify(None, f"opening {cfg.source_kind} source / building flowgraph (band={cfg.band})")

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

    # READY=1: startup complete. Under Type=notify, systemd was holding
    # the unit in "activating" until this signal — if the daemon hung in
    # sdrplay_api_Open above, this line was never reached and systemd
    # SIGKILL'd at TimeoutStartSec=60. See DESIGN_sdrplay_wedge_fix.md.
    _sd_notify("READY=1", f"chirp ready (band={cfg.band})")

    stop_evt = threading.Event()

    def _sig(signum, frame):  # noqa: ARG001
        log.info("signal %d received, shutting down", signum)
        _sd_notify("STOPPING=1", "draining + tearing down")
        stop_evt.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    # Watchdog cadence: ping every ~10 s. Unit has WatchdogSec=30, so
    # this is 3× safety margin. See DESIGN_sdrplay_wedge_fix.md §4.3.
    _WATCHDOG_INTERVAL_S = 10.0
    last_watchdog = time.monotonic()

    try:
        while not stop_evt.is_set():
            time.sleep(0.25)
            now = time.monotonic()
            if now - last_watchdog >= _WATCHDOG_INTERVAL_S:
                _sd_notify("WATCHDOG=1")
                last_watchdog = now
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

        # SDR shutdown drain. Give SoapySDR/sdrplay_api IPC time to fully
        # release the device session before this process exits, so the
        # replacement daemon's osmosdr.source() does not race against an
        # incomplete release. File-source runs skip the drain.
        # See DESIGN_sdrplay_wedge_fix.md §4.4.
        drain_s = float(os.environ.get("CHIRP_SDR_SHUTDOWN_DRAIN_S", "2.0"))
        if cfg.source_kind == "sdr" and drain_s > 0:
            log.info("sdr shutdown drain: sleeping %.2fs to let sdrplay_api release", drain_s)
            time.sleep(drain_s)

        log.info("chirp stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
