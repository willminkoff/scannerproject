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
detections into a single coord_state.json, looks up recent classified
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

# Loop cadence.
TICK_S = 0.5

# How many recent detections to surface in coord_state.json.
#
# We deliberately surface BOTH classified and not-yet-classified rows so the
# Discovery pane lights up the moment the sweep finds peaks — the ML pass
# can lag behind on a backlog of slices, but the operator still wants to see
# the sweep is doing its job.  Each row carries `classified: true|false` so
# the UI can dim pending entries until the classifier catches up.
#
# Window widened from 300s to 1800s after observing intermittent NFM voice
# (the dominant traffic in this 117-470 MHz cut) goes silent for minutes at
# a time — a 5-minute window made the pane flicker empty during quiet
# stretches.
DETECTIONS_WINDOW_S = 1800.0
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
    """Surface recent detections (classified and pending) within the sweep
    range.  Rows are deduped per 25-kHz bin: we keep the most recent ts and
    fold in the best-SNR sighting + the latest classification, so a freq
    that's been hit repeatedly shows as a single row with a recent
    `last_seen_s_ago` and a stable mod-class label.

    Pending rows (sweep wrote, classifier hasn't caught up) carry
    `classified: false` and `classification: "pending"` so the UI can dim
    them; classified rows carry the protocol tag + confidence the
    classifier wrote.
    """
    conn = _classifier_conn()
    if conn is None:
        return []
    now = time.time()
    cutoff = now - DETECTIONS_WINDOW_S
    start_hz, end_hz = start_mhz * 1e6, end_mhz * 1e6
    # Pull a larger working set since we'll dedupe by 25-kHz bin — without
    # this an FM-broadcast-style row that spans many bins would crowd out
    # narrower neighbours from the LIMIT.
    fetch_limit = DETECTIONS_LIMIT * 8
    try:
        rows = conn.execute(
            "SELECT ts, freq_hz, bandwidth_hz, power_dbfs, snr_db, "
            "modulation_class, modulation_confidence, protocol_tag, "
            "interpretation, classified_ts "
            "FROM detections "
            "WHERE ts >= ? AND freq_hz BETWEEN ? AND ? "
            "ORDER BY ts DESC LIMIT ?",
            (cutoff, start_hz, end_hz, fetch_limit),
        ).fetchall()
    except sqlite3.Error as e:
        LOG.warning("detections query failed: %s", e)
        return []
    # Dedupe by 25-kHz bin while keeping the most recent ts and best SNR.
    # Classification fields prefer any classified sighting over a pending
    # one — so a freq that was classified an hour ago and just got hit
    # again still surfaces with its known label.
    by_bin: dict[int, dict] = {}
    for r in rows:
        ts, freq_hz, bw_hz, power_dbfs, snr_db, mod_cls, conf, proto, interp, classified_ts = r
        if freq_hz is None or ts is None:
            continue
        bin_key = int(float(freq_hz) // 25_000)
        slot = by_bin.get(bin_key)
        has_class = classified_ts is not None and mod_cls and mod_cls != "unclassified"
        if slot is None:
            slot = {
                "ts": float(ts),
                "freq_hz": float(freq_hz),
                "bandwidth_hz": float(bw_hz or 0.0),
                "power_dbfs": float(power_dbfs or 0.0),
                "snr_db": float(snr_db or 0.0),
                "mod_cls": mod_cls if has_class else None,
                "confidence": float(conf or 0.0) if has_class else 0.0,
                "proto": proto if has_class else None,
                "interp": str(interp)[:80] if interp else None,
            }
            by_bin[bin_key] = slot
            continue
        # Most-recent wins for ts; best-SNR wins for the SNR field.
        if float(ts) > slot["ts"]:
            slot["ts"] = float(ts)
        s = float(snr_db or 0.0)
        if s > slot["snr_db"]:
            slot["snr_db"] = s
            slot["power_dbfs"] = float(power_dbfs or slot["power_dbfs"])
            slot["bandwidth_hz"] = float(bw_hz or slot["bandwidth_hz"])
        # Prefer the first classified sighting we see (rows are DESC by ts,
        # so this is the most-recent successful classification).
        if has_class and slot["mod_cls"] is None:
            slot["mod_cls"] = mod_cls
            slot["confidence"] = float(conf or 0.0)
            slot["proto"] = proto
            if interp:
                slot["interp"] = str(interp)[:80]
    out: list[dict] = []
    for slot in by_bin.values():
        is_classified = slot["mod_cls"] is not None
        if is_classified:
            label_bits = [str(slot["mod_cls"])]
            if slot["proto"] and slot["proto"] != slot["mod_cls"]:
                label_bits.append(f"[{slot['proto']}]")
            label = " ".join(label_bits)
            if slot["interp"]:
                label = f"{label} - {slot['interp']}"
        else:
            label = "pending"
        out.append({
            "freq_mhz": round(slot["freq_hz"] / 1e6, 4),
            "classification": label,
            "classified": is_classified,
            "confidence": round(slot["confidence"], 3),
            "snr_db": round(slot["snr_db"], 1),
            "power_dbfs": round(slot["power_dbfs"], 1),
            "bandwidth_khz": round(slot["bandwidth_hz"] / 1e3, 2),
            "last_seen_s_ago": int(max(0.0, now - slot["ts"])),
        })
    # Most recent first; cap at DETECTIONS_LIMIT post-dedupe.
    out.sort(key=lambda d: d["last_seen_s_ago"])
    return out[:DETECTIONS_LIMIT]


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

    # 3. Top-level state.
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

    # 4. Cycle age — coarse proxy: time since the *youngest* per-dongle
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

    # 5. Classifier-pipeline detections.
    detections = _read_detections(coord_start, coord_end)

    out = {
        "updated_ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "state": top_state,
        "range": {"start_mhz": coord_start, "end_mhz": coord_end},
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
