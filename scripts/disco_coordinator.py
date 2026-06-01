#!/usr/bin/env python3
"""Phase 6c — Disco coordinator for N-dongle unified sweep.

Reads /run/scannerproject/disco/coord_config.json
  {"range": {"start_mhz": F, "end_mhz": F}, "dongle_serials": ["...","..."]}

Subdivides the range equally across the listed dongle serials and writes
per-tuner /run/scannerproject/disco/sweep_config_<serial>.json files.
disco-sweep@<serial>.service instances (running scripts via the existing
disco-sweep@.service template, with disco/src/sweep.py extended in Phase 6c
to accept bare-serial tuner-ids) pick up the range via mtime poll, sweep
their slice, and write /run/scannerproject/disco/spectrum_<serial>.json.

The coordinator polls each per-tuner spectrum file, stitches the
composite bins into a unified array, looks up recent classified
detections from the disco SQLite DB, and writes
/run/scannerproject/disco/coord_state.json atomically.

Watchdog: per-dongle, stale > 5s -> "degraded", > 30s -> "down".
Coordinator does NOT manually restart sweep instances; systemd
Restart=on-failure handles dongle drop / sweep.py crash.

Architecture supports any N dongles — just add the serial to
coord_config.dongle_serials and enable disco-sweep@<serial>.service.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone

STATE_DIR = os.environ.get("DISCO_STATE_DIR", "/run/scannerproject/disco")
COORD_CONFIG_PATH = os.path.join(STATE_DIR, "coord_config.json")
COORD_STATE_PATH = os.path.join(STATE_DIR, "coord_state.json")
SWEEP_CFG_FMT = os.path.join(STATE_DIR, "sweep_config_{serial}.json")
SPECTRUM_FMT = os.path.join(STATE_DIR, "spectrum_{serial}.json")
DISCO_DB_PATH = os.environ.get(
    "DISCO_DB", "/home/ubuntu/scannerproject/disco/state/disco.sqlite"
)

# Watchdog thresholds (seconds of staleness on spectrum_<serial>.json).
DEGRADED_AFTER_SEC = 5.0
DOWN_AFTER_SEC = 30.0

# Composite stitch resolution.  We rebin into a single unified array
# spanning the full user-set range so the frontend just renders bins[].
N_COMPOSITE_BINS = 1024

# Loop cadence.
TICK_S = 0.5

# How many recent classified detections to surface.
DETECTIONS_WINDOW_S = 300.0
DETECTIONS_LIMIT = 50

# Default config used until /api/disco/range POSTs override it.
DEFAULT_CONFIG = {
    "range": {"start_mhz": 117.0, "end_mhz": 470.0},
    "dongle_serials": ["45469635", "61108285"],
}

LOG = logging.getLogger("disco.coordinator")
_STOP = False


# ---------------------------------------------------------------------
# Bus discovery — derive USB bus path (e.g. "1-3.1.2") from /sys for
# each RTL-SDR serial so the heartbeat row can include it.
# ---------------------------------------------------------------------
def _bus_for_serial(serial: str) -> str:
    """Walk /sys/bus/usb/devices to find which port owns this serial.
    Best-effort; returns "?" on miss.  Cached per-process since the
    USB topology doesn't change without a replug."""
    cache = _bus_for_serial._cache  # type: ignore[attr-defined]
    if serial in cache:
        return cache[serial]
    found = "?"
    try:
        for entry in os.listdir("/sys/bus/usb/devices"):
            if ":" in entry or entry.startswith("usb"):
                continue
            try:
                with open(f"/sys/bus/usb/devices/{entry}/serial") as f:
                    if f.read().strip() == serial:
                        found = entry
                        break
            except (OSError, FileNotFoundError):
                continue
    except OSError:
        pass
    cache[serial] = found
    return found
_bus_for_serial._cache = {}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------
# Atomic write helper.
# ---------------------------------------------------------------------
def _atomic_write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


# ---------------------------------------------------------------------
# Config + per-tuner sweep_config plumbing.
# ---------------------------------------------------------------------
def _read_coord_config() -> tuple[dict, float]:
    """Read coord_config.json, falling back to DEFAULT_CONFIG.

    Returns (config_dict, age_sec).  age_sec is wall-time since the file
    was last touched, or 0.0 if the file doesn't exist (defaults in use).
    """
    try:
        st = os.stat(COORD_CONFIG_PATH)
        with open(COORD_CONFIG_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config root not a dict")
        # Light validation; fill in missing pieces with defaults.
        rng = data.get("range") or {}
        serials = data.get("dongle_serials") or []
        if not isinstance(serials, list) or not serials:
            serials = list(DEFAULT_CONFIG["dongle_serials"])
        start = float(rng.get("start_mhz", DEFAULT_CONFIG["range"]["start_mhz"]))
        end = float(rng.get("end_mhz", DEFAULT_CONFIG["range"]["end_mhz"]))
        if end <= start:
            raise ValueError(f"invalid range: {start}-{end}")
        cfg = {
            "range": {"start_mhz": start, "end_mhz": end},
            "dongle_serials": [str(s) for s in serials],
        }
        age = max(0.0, time.time() - st.st_mtime)
        return cfg, age
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
        if not isinstance(e, FileNotFoundError):
            LOG.warning("coord_config read failed (%s); using defaults", e)
        return dict(DEFAULT_CONFIG), 0.0


def _ensure_coord_config_on_disk(cfg: dict) -> None:
    """Write DEFAULT_CONFIG to coord_config.json if the file doesn't exist
    so /api/disco GET callers see the live range immediately."""
    if os.path.exists(COORD_CONFIG_PATH):
        return
    try:
        _atomic_write_json(COORD_CONFIG_PATH, cfg)
        LOG.info("wrote default coord_config.json")
    except OSError as e:
        LOG.warning("could not seed coord_config.json: %s", e)


def _subdivide(start_mhz: float, end_mhz: float, n: int) -> list[tuple[float, float]]:
    """Equally split [start,end] into n contiguous sub-ranges."""
    if n <= 0:
        return []
    width = (end_mhz - start_mhz) / n
    return [
        (start_mhz + i * width, start_mhz + (i + 1) * width)
        for i in range(n)
    ]


def _publish_sweep_configs(serials: list[str], subranges: list[tuple[float, float]]) -> None:
    """Write per-tuner sweep_config_<serial>.json files if changed.
    Compares against the current on-disk value so we don't churn mtime
    (which would force sweep.py to retune unnecessarily)."""
    for serial, (s_mhz, e_mhz) in zip(serials, subranges):
        path = SWEEP_CFG_FMT.format(serial=serial)
        desired = {"start_mhz": round(s_mhz, 4), "end_mhz": round(e_mhz, 4)}
        try:
            with open(path) as f:
                current = json.load(f)
            if current == desired:
                continue
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        try:
            _atomic_write_json(path, desired)
            LOG.info("sweep_config[%s] -> %.3f-%.3f MHz", serial, s_mhz, e_mhz)
        except OSError as e:
            LOG.warning("sweep_config write failed for %s: %s", serial, e)


# ---------------------------------------------------------------------
# Per-tuner spectrum read.
# ---------------------------------------------------------------------
def _read_spectrum(serial: str) -> tuple[dict | None, float]:
    """Return (parsed_state, age_sec) for spectrum_<serial>.json.
    age_sec defaults to a large value if the file is missing/unreadable."""
    path = SPECTRUM_FMT.format(serial=serial)
    try:
        st = os.stat(path)
    except (FileNotFoundError, OSError):
        return None, 9999.0
    age = max(0.0, time.time() - st.st_mtime)
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None, age
        return data, age
    except (OSError, json.JSONDecodeError):
        return None, age


# ---------------------------------------------------------------------
# Stitching — composite bins are interpolated/copied from each per-tuner
# spectrum's composite block into the unified output array spanning the
# full coordinator range.
# ---------------------------------------------------------------------
def _stitch_composite(
    spectra: list[tuple[str, dict | None, tuple[float, float]]],
    coord_start_mhz: float,
    coord_end_mhz: float,
    n_out: int,
) -> list[float]:
    """Stitch per-tuner composite arrays into a single unified N_OUT-bin
    array spanning [coord_start_mhz, coord_end_mhz].  Each unified bin
    samples the per-tuner composite whose sub-range covers that bin's
    centre frequency.  Bins with no covering tuner stay at -120 dBFS."""
    out = [-120.0] * n_out
    width_hz = (coord_end_mhz - coord_start_mhz) * 1e6 / n_out
    base_hz = coord_start_mhz * 1e6
    for i in range(n_out):
        bin_centre_hz = base_hz + (i + 0.5) * width_hz
        for _serial, state, (sub_start_mhz, sub_end_mhz) in spectra:
            sub_start_hz = sub_start_mhz * 1e6
            sub_end_hz = sub_end_mhz * 1e6
            if not (sub_start_hz <= bin_centre_hz < sub_end_hz):
                continue
            if state is None:
                continue
            comp = state.get("composite") or {}
            bins = comp.get("bins_dbfs") or []
            t_start = float(comp.get("band_min_hz", sub_start_hz))
            t_end = float(comp.get("band_max_hz", sub_end_hz))
            n_in = len(bins)
            if n_in == 0 or t_end <= t_start:
                continue
            idx = int((bin_centre_hz - t_start) / (t_end - t_start) * n_in)
            if 0 <= idx < n_in:
                try:
                    out[i] = float(bins[idx])
                except (TypeError, ValueError):
                    pass
            break
    return [round(v, 1) for v in out]


# ---------------------------------------------------------------------
# Classifier output — pull recent classified detections from the
# disco SQLite DB (written by disco/src/classifier.py +
# disco/src/interpret.py).
# ---------------------------------------------------------------------
_CLASSIFIER_CONN = None


def _classifier_conn() -> sqlite3.Connection | None:
    global _CLASSIFIER_CONN
    if _CLASSIFIER_CONN is None:
        try:
            _CLASSIFIER_CONN = sqlite3.connect(
                f"file:{DISCO_DB_PATH}?mode=ro", uri=True, timeout=2.0,
            )
        except sqlite3.Error as e:
            LOG.warning("classifier DB open failed: %s", e)
            return None
    return _CLASSIFIER_CONN


def _read_detections(start_mhz: float, end_mhz: float) -> list[dict]:
    conn = _classifier_conn()
    if conn is None:
        return []
    cutoff = time.time() - DETECTIONS_WINDOW_S
    start_hz, end_hz = start_mhz * 1e6, end_mhz * 1e6
    try:
        rows = conn.execute(
            "SELECT freq_hz, modulation_class, protocol_tag, "
            "modulation_confidence, interpretation, snr_db "
            "FROM detections "
            "WHERE classified_ts IS NOT NULL "
            "AND ts >= ? AND freq_hz BETWEEN ? AND ? "
            "ORDER BY ts DESC LIMIT ?",
            (cutoff, start_hz, end_hz, DETECTIONS_LIMIT),
        ).fetchall()
    except sqlite3.Error as e:
        LOG.warning("detections query failed: %s", e)
        return []
    # Dedupe by freq bin (25 kHz) to avoid spammy near-duplicates.
    seen = set()
    out = []
    for r in rows:
        freq_hz, mod_cls, proto, conf, interp, snr = r
        if freq_hz is None:
            continue
        bin_key = int(freq_hz // 25_000)
        if bin_key in seen:
            continue
        seen.add(bin_key)
        label_bits = []
        if mod_cls:
            label_bits.append(str(mod_cls))
        if proto:
            label_bits.append(f"[{proto}]")
        label = " ".join(label_bits) or "unclassified"
        if interp:
            label = f"{label} - {str(interp)[:80]}"
        try:
            confidence = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        out.append({
            "freq_mhz": round(float(freq_hz) / 1e6, 4),
            "classification": label,
            "confidence": round(confidence, 3),
            "snr_db": round(float(snr or 0.0), 1),
        })
    return out


# ---------------------------------------------------------------------
# Main tick — read config, publish sweep configs, stitch, write state.
# ---------------------------------------------------------------------
_LAST_CYCLE_TS = 0.0
_LAST_DONGLE_STATES: dict[str, str] = {}


def _tick(prev_serials: list[str]) -> list[str]:
    """Run a single coordinator tick.  Returns the active serial list
    (caller tracks it to log roster changes)."""
    global _LAST_CYCLE_TS

    cfg, cfg_age = _read_coord_config()
    serials = cfg["dongle_serials"]
    coord_start = cfg["range"]["start_mhz"]
    coord_end = cfg["range"]["end_mhz"]

    if serials != prev_serials:
        LOG.info("dongle roster: %s -> %s", prev_serials, serials)

    # 1. Subdivide + publish per-tuner sweep configs.
    subs = _subdivide(coord_start, coord_end, len(serials))
    _publish_sweep_configs(serials, subs)

    # 2. Read every per-tuner spectrum + assemble dongles[] view.
    spectra = []
    dongles_view = []
    any_fresh = False
    all_down = True
    any_degraded = False
    for serial, (s_mhz, e_mhz) in zip(serials, subs):
        state, age = _read_spectrum(serial)
        spectra.append((serial, state, (s_mhz, e_mhz)))
        if age < DEGRADED_AFTER_SEC:
            d_state = "ok"
            any_fresh = True
            all_down = False
        elif age < DOWN_AFTER_SEC:
            d_state = "degraded"
            any_degraded = True
            all_down = False
        else:
            d_state = "down"
        # Log state transitions per dongle.
        prev_state = _LAST_DONGLE_STATES.get(serial)
        if prev_state != d_state:
            LOG.info("dongle[%s] state: %s -> %s (age=%.1fs)",
                     serial, prev_state, d_state, age)
            _LAST_DONGLE_STATES[serial] = d_state
        last_frame_age_ms = None
        if state is not None and d_state == "ok":
            # Prefer the state.ts the worker wrote so we report frame
            # age, not file-mtime jitter.
            try:
                worker_ts = float(state.get("ts") or 0.0)
                if worker_ts > 0:
                    last_frame_age_ms = int(max(0.0, time.time() - worker_ts) * 1000)
                else:
                    last_frame_age_ms = int(age * 1000)
            except (TypeError, ValueError):
                last_frame_age_ms = int(age * 1000)
        dongles_view.append({
            "serial": serial,
            "state": d_state,
            "sub_range_mhz": [round(s_mhz, 3), round(e_mhz, 3)],
            "last_frame_age_ms": last_frame_age_ms,
            "bus": _bus_for_serial(serial),
        })

    # 3. Stitch composite.
    bins = _stitch_composite(spectra, coord_start, coord_end, N_COMPOSITE_BINS)

    # 4. Top-level state.
    n_total = len(serials)
    n_ok = sum(1 for d in dongles_view if d["state"] == "ok")
    if n_total == 0:
        top_state = "down"
    elif n_ok == n_total:
        top_state = "ok"
    elif n_ok == 0:
        top_state = "down"
    else:
        top_state = "degraded"

    # 5. Cycle age — coarse proxy: time since the *youngest* per-dongle
    # state's cycle_complete_ts changed.  We just look at "ok" dongles.
    now = time.time()
    cycle_ages = []
    for serial, state, _ in spectra:
        if state is None:
            continue
        try:
            cct = float(state.get("cycle_complete_ts") or 0.0)
            if cct > 0:
                cycle_ages.append(now - cct)
        except (TypeError, ValueError):
            pass
    last_full_cycle_age_sec = round(min(cycle_ages), 1) if cycle_ages else None

    # 6. Classifier-pipeline detections.
    detections = _read_detections(coord_start, coord_end)

    out = {
        "updated_ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "state": top_state,
        "range": {"start_mhz": coord_start, "end_mhz": coord_end},
        "bins": bins,
        "last_full_cycle_age_sec": last_full_cycle_age_sec,
        "dongles": dongles_view,
        "detections": detections,
        "config_age_sec": round(cfg_age, 1),
    }
    try:
        _atomic_write_json(COORD_STATE_PATH, out)
    except OSError as e:
        LOG.warning("coord_state write failed: %s", e)
    return serials


def _handle_stop(signum, _frame):
    global _STOP
    LOG.info("stopping on signal %s", signum)
    _STOP = True


def main():
    logging.basicConfig(
        level=os.environ.get("DISCO_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    os.makedirs(STATE_DIR, exist_ok=True)
    cfg, _ = _read_coord_config()
    _ensure_coord_config_on_disk(cfg)
    LOG.info("disco coordinator starting; range=%.1f-%.1f MHz dongles=%s",
             cfg["range"]["start_mhz"], cfg["range"]["end_mhz"],
             cfg["dongle_serials"])

    prev_serials: list[str] = []
    while not _STOP:
        try:
            prev_serials = _tick(prev_serials)
        except Exception as e:  # never let the loop die
            LOG.exception("tick failed: %s", e)
        # Sleep with shutdown awareness.
        t_end = time.monotonic() + TICK_S
        while not _STOP and time.monotonic() < t_end:
            time.sleep(min(0.1, t_end - time.monotonic()))

    LOG.info("disco coordinator exiting")


if __name__ == "__main__":
    main()
