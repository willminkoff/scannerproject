"""Per-tuner sweep loop with per-peak frequency-shift + decimate before slice emission (Phase 2.1C)."""
import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import uuid

import numpy as np
import SoapySDR
import yaml
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

CONFIG_PATH = os.environ.get("DISCO_CONFIG", "/home/ubuntu/scannerproject/disco/configs/sweep.yaml")
STATE_DIR = os.environ.get("DISCO_STATE_DIR", "/run/scannerproject/disco")
SLICES_DIR = os.environ.get("DISCO_SLICES_DIR", "/run/scannerproject/disco/slices")
N_COMPOSITE_BINS = 1024
N_DISPLAY_BINS = 1024
LOG = logging.getLogger("disco.sweep")
_STOP = False
_FORCE_EXIT_ARMED = False

# Bounded graceful shutdown: SoapySDR's RSPduo Master/Slave teardown can hang
# indefinitely inside closeStream() when the SDRplay daemon is still draining
# internal state, holding the systemd unit in stop-sigterm until the 90 s
# default timeout expires and the unit gets SIGKILLed (and reported as
# failed). That breaks any caller — including disco-svc-ctl mode-off — that
# does `systemctl stop` and then expects to acquire the SDR right after.
#
# A daemon watchdog thread armed from _handle_stop bounds the entire shutdown
# at FORCE_EXIT_TIMEOUT_SEC. Healthy closes complete in well under a second;
# this only fires when the SoapySDR call is actually wedged. os._exit(0)
# bypasses the GIL, so it works even when the C call is holding it. Exit code
# 0 means systemd marks the unit inactive (not failed), which lets the wrapper
# proceed to start the next side cleanly.
FORCE_EXIT_TIMEOUT_SEC = 5.0

# Bounded startup: SoapySDR.Device(args) can deadlock when Master+Slave on the
# same RSPduo open concurrently — the SDRplay daemon doesn't safely serialize
# concurrent opens against the same physical device, and Python is trapped
# inside the C constructor with the GIL held, so signals can't reach the
# Python signal handler. Systemd ordering (After= drop-in on the slave
# instance) is the primary mitigation; this watchdog is defense in depth so a
# stuck open is bounded at STARTUP_WATCHDOG_TIMEOUT_SEC instead of hanging
# until systemd's TimeoutStartSec / TimeoutStopSec fires. os._exit(2) makes
# systemd treat the unit as failed → Restart=on-failure → fresh attempt with
# RestartSec backoff. Healthy opens complete in <1 s; this only fires when
# the call is actually wedged.
STARTUP_WATCHDOG_TIMEOUT_SEC = 10.0

# Phase 6c — bare RTL-SDR serial tuner-id support (e.g. '45469635')
# + per-tuner sweep_config_<id>.json mtime polling for retunes.
def _read_sweep_cfg(tid):
    try:
        p = os.path.join(STATE_DIR, f"sweep_config_{tid}.json")
        st = os.stat(p)
        d = json.load(open(p))
        s, e = float(d["start_mhz"])*1e6, float(d["end_mhz"])*1e6
        return (s, e, st.st_mtime) if e > s else None
    except Exception:
        return None

def _arm_force_exit_watchdog(timeout_sec):
    global _FORCE_EXIT_ARMED
    if _FORCE_EXIT_ARMED:
        return
    _FORCE_EXIT_ARMED = True

    def _killer():
        time.sleep(timeout_sec)
        LOG.warning(
            "graceful shutdown exceeded %ss (likely SoapySDR closeStream hang); force-exiting",
            timeout_sec,
        )
        os._exit(0)

    threading.Thread(target=_killer, daemon=True, name="disco-sweep-force-exit").start()


def _arm_startup_watchdog(timeout_sec, tuner_id):
    """Arm a watchdog that os._exit(2)s if the constructor doesn't return in
    time. Returns a threading.Event the caller MUST .set() once the open
    completes (success or exception) so the watchdog stands down."""
    completed = threading.Event()

    def _killer():
        if completed.wait(timeout_sec):
            return
        LOG.warning(
            "[%s] SoapySDR.Device() exceeded %ss (likely RSPduo Master/Slave race); "
            "force-exiting for systemd restart",
            tuner_id, timeout_sec,
        )
        os._exit(2)

    threading.Thread(
        target=_killer, daemon=True, name=f"disco-sweep-startup-watchdog-{tuner_id}"
    ).start()
    return completed


def _handle_stop(signum, frame):
    global _STOP
    LOG.info("stopping on signal %s", signum)
    _STOP = True
    _arm_force_exit_watchdog(FORCE_EXIT_TIMEOUT_SEC)


def setup_logging():
    logging.basicConfig(
        level=os.environ.get("DISCO_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def init_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS detections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "ts REAL NOT NULL,"
        "tuner_id TEXT NOT NULL,"
        "freq_hz REAL NOT NULL,"
        "bandwidth_hz REAL,"
        "power_dbfs REAL,"
        "snr_db REAL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_det_ts ON detections(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_det_tuner_ts ON detections(tuner_id, ts)")
    cur = conn.execute("PRAGMA table_info(detections)")
    existing = {row[1] for row in cur.fetchall()}
    for col, ddl in [
        ("modulation_class", "TEXT"),
        ("modulation_confidence", "REAL"),
        ("protocol_tag", "TEXT"),
        ("slice_path", "TEXT"),
        ("classified_ts", "REAL"),
        ("interpretation", "TEXT"),
        ("interpreted_ts", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE detections ADD COLUMN {col} {ddl}")
    conn.commit()
    return conn


def compute_spectrum_db(iq, fft_size):
    if len(iq) < fft_size:
        return None
    block = iq[:fft_size]
    window = np.hanning(fft_size).astype(np.float32)
    blk = block * window
    spec = np.fft.fftshift(np.fft.fft(blk))
    mag2 = spec.real * spec.real + spec.imag * spec.imag
    return 10.0 * np.log10(mag2 + 1e-30) - 20.0 * np.log10(fft_size)


def decimate_max(arr, target_n):
    n = len(arr)
    if n <= target_n:
        return arr.astype(np.float32)
    factor = n // target_n
    truncated = arr[: factor * target_n]
    return truncated.reshape(target_n, factor).max(axis=1).astype(np.float32)


def find_peaks_in_spectrum(mag_db, sample_rate, center_freq, fft_size, threshold_db, min_bw_hz, noise_pct):
    noise_floor_db = float(np.percentile(mag_db, noise_pct))
    freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0 / sample_rate)) + center_freq
    above = mag_db > (noise_floor_db + threshold_db)
    detections = []
    in_run = False
    run_start = 0
    L = len(above)
    for i in range(L + 1):
        active = (i < L) and bool(above[i])
        if active and not in_run:
            in_run = True
            run_start = i
        elif (not active) and in_run:
            run_end = i - 1
            in_run = False
            run_db = mag_db[run_start:run_end + 1]
            peak_idx = run_start + int(np.argmax(run_db))
            peak_freq = float(freqs[peak_idx])
            peak_db = float(mag_db[peak_idx])
            bw = (run_end - run_start + 1) * (sample_rate / fft_size)
            if bw >= min_bw_hz:
                detections.append({
                    "freq_hz": peak_freq,
                    "bandwidth_hz": float(bw),
                    "power_dbfs": peak_db,
                    "snr_db": peak_db - noise_floor_db,
                })
    return detections, noise_floor_db


def init_state(tuner_id, band_start, band_end):
    return {
        "tuner_id": tuner_id,
        "band_min_hz": float(band_start),
        "band_max_hz": float(band_end),
        "ts": 0.0, "current_center_hz": 0.0,
        "current_sweep_pos": 0, "total_steps": 0,
        "cycle_count": 0, "cycle_complete_ts": 0.0,
        "noise_floor_dbfs_recent": -90.0,
        "live_if": {"center_hz": 0.0, "sample_rate_hz": 0.0,
                    "n_bins": N_DISPLAY_BINS,
                    "bins_dbfs": [-120.0] * N_DISPLAY_BINS},
        "composite": {"band_min_hz": float(band_start), "band_max_hz": float(band_end),
                      "n_bins": N_COMPOSITE_BINS,
                      "bins_dbfs": [-120.0] * N_COMPOSITE_BINS,
                      "bin_age_s": [999.0] * N_COMPOSITE_BINS},
    }


def update_state(state, mag_db, center_freq, sample_rate, sweep_pos, total_steps, fft_size):
    band_min = state["band_min_hz"]; band_max = state["band_max_hz"]
    n_comp = state["composite"]["n_bins"]
    band_span = band_max - band_min
    composite_bin_hz = band_span / n_comp
    window_min = center_freq - sample_rate / 2
    window_max = center_freq + sample_rate / 2
    ci_start = max(0, int((window_min - band_min) / composite_bin_hz))
    ci_end = min(n_comp, int((window_max - band_min) / composite_bin_hz) + 1)
    composite_bins = state["composite"]["bins_dbfs"]
    composite_age = state["composite"]["bin_age_s"]
    now = time.time()
    for ci in range(ci_start, ci_end):
        bin_center_hz = band_min + (ci + 0.5) * composite_bin_hz
        if window_min <= bin_center_hz <= window_max:
            if_bin_idx = int((bin_center_hz - window_min) / sample_rate * fft_size)
            if 0 <= if_bin_idx < fft_size:
                composite_bins[ci] = round(float(mag_db[if_bin_idx]), 1)
                composite_age[ci] = 0.0
    decimated = decimate_max(mag_db, N_DISPLAY_BINS)
    state["live_if"] = {
        "center_hz": float(center_freq), "sample_rate_hz": float(sample_rate),
        "n_bins": N_DISPLAY_BINS,
        "bins_dbfs": [round(float(v), 1) for v in decimated],
    }
    state["ts"] = now
    state["current_center_hz"] = float(center_freq)
    state["current_sweep_pos"] = int(sweep_pos)
    state["total_steps"] = int(total_steps)
    if sweep_pos == 0:
        state["cycle_count"] += 1
        state["cycle_complete_ts"] = now


def write_state_atomic(state, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, path)


def emit_iq_slice(iq, det, center_freq, sample_rate, tuner_id, slices_dir,
                  output_samples=2048, target_rate_floor_hz=50e3, max_decim=100):
    """Frequency-shift peak to DC, lowpass+decimate, save centered baseband slice.
    Filename: {tuner}_{freq_hz}_{bw_hz}_{rate_hz}_{ts}_{uid}.iq.f32"""
    if len(iq) < 256:
        return None
    delta = det["freq_hz"] - center_freq
    n = np.arange(len(iq), dtype=np.float64)
    phase = -2j * np.pi * delta / sample_rate * n
    shifted = (iq * np.exp(phase).astype(np.complex64)).astype(np.complex64)

    target_rate = max(target_rate_floor_hz, det["bandwidth_hz"] * 4.0)
    decim = max(1, min(int(sample_rate // target_rate), max_decim))
    if decim > 1:
        try:
            from scipy.signal import decimate as scipy_decim
            decimated = scipy_decim(shifted, decim, ftype="fir", zero_phase=True).astype(np.complex64)
        except Exception as e:
            LOG.warning("[%s] decimate failed (decim=%d): %s; using strided", tuner_id, decim, e)
            decimated = shifted[::decim].astype(np.complex64)
    else:
        decimated = shifted
    actual_rate = sample_rate / decim

    if len(decimated) >= output_samples:
        out = decimated[:output_samples]
    else:
        out = np.concatenate([decimated, np.zeros(output_samples - len(decimated), dtype=np.complex64)])

    ts = time.time()
    uid = uuid.uuid4().hex[:8]
    fname = f"{tuner_id}_{int(det['freq_hz'])}_{int(det['bandwidth_hz'])}_{int(actual_rate)}_{ts:.3f}_{uid}.iq.f32"
    path = os.path.join(slices_dir, fname)
    try:
        out.tofile(path)
        return path
    except Exception as e:
        LOG.warning("[%s] slice write failed: %s", tuner_id, e)
        return None


def open_device_with_retry(soapy_args, tuner_id):
    backoff = 1.0
    for attempt in range(1, 21):
        if _STOP:
            return None
        completed = _arm_startup_watchdog(STARTUP_WATCHDOG_TIMEOUT_SEC, tuner_id)
        try:
            return SoapySDR.Device(soapy_args)
        except Exception as e:
            LOG.warning("[%s] open attempt %d failed: %s; sleep %.1fs", tuner_id, attempt, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 1.6, 20.0)
        finally:
            completed.set()
    return None


def sweep_loop(tuner_id, cfg, conn):
    tuner_cfg = cfg["tuners"][tuner_id]
    sweep_cfg = cfg["sweep"]
    detect_cfg = cfg["detection"]
    classifier_cfg = cfg.get("classifier", {})
    slice_min_snr = float(classifier_cfg.get("slice_min_snr_db", 18.0))
    slice_per_step_max = int(classifier_cfg.get("slices_per_step_max", 1))
    slice_samples = int(classifier_cfg.get("slice_samples", 2048))
    slice_target_rate_floor = float(classifier_cfg.get("slice_target_rate_floor_hz", 50e3))

    soapy_args = tuner_cfg["soapy_args"]
    band_start = float(tuner_cfg["band_start_hz"])
    band_end = float(tuner_cfg["band_end_hz"])
    sample_rate_req = float(sweep_cfg["sample_rate_hz"])
    dwell_s = sweep_cfg["dwell_ms"] / 1000.0
    step_factor = float(sweep_cfg["step_factor"])
    fft_size = int(sweep_cfg["fft_size"])
    tune_settle_s = sweep_cfg.get("tune_settle_ms", 50) / 1000.0
    threshold = float(detect_cfg["threshold_above_floor_db"])
    min_bw = float(detect_cfg["min_bandwidth_hz"])
    noise_pct = float(detect_cfg.get("noise_floor_percentile", 30))
    retention_s = float(cfg["db"]["retention_hours"]) * 3600.0

    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(SLICES_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, f"spectrum_{tuner_id}.json")
    state = init_state(tuner_id, band_start, band_end)

    LOG.info("[%s] opening %s", tuner_id, soapy_args)
    sdr = open_device_with_retry(soapy_args, tuner_id)
    if sdr is None:
        LOG.error("[%s] could not open device — exit 2", tuner_id)
        sys.exit(2)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, sample_rate_req)
    actual_rate = float(sdr.getSampleRate(SOAPY_SDR_RX, 0))
    LOG.info("[%s] sample_rate requested=%s actual=%s", tuner_id, sample_rate_req, actual_rate)
    try:
        sdr.setBandwidth(SOAPY_SDR_RX, 0, actual_rate * 0.8)
    except Exception:
        pass
    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0])
    sdr.activateStream(stream)
    LOG.info("[%s] stream activated", tuner_id)

    step_hz = max(actual_rate * step_factor, 1e3)
    def _build_freqs(bs, be):
        out, f = [], bs + actual_rate / 2
        while f < be - actual_rate / 2 + 1:
            out.append(f); f += step_hz
        return out or [(bs + be) / 2]
    freqs_to_visit = _build_freqs(band_start, band_end)
    total_steps = len(freqs_to_visit)
    LOG.info("[%s] sweep: %d steps from %.2f to %.2f MHz; slice: shift+decim, samples=%d, rate_floor=%.0fHz",
             tuner_id, total_steps, freqs_to_visit[0]/1e6, freqs_to_visit[-1]/1e6, slice_samples, slice_target_rate_floor)

    n_samps_dwell = max(int(actual_rate * dwell_s), fft_size)
    last_cleanup = time.time()
    last_age_update = time.time()
    _sc = _read_sweep_cfg(tuner_id); last_cfg_mt = _sc[2] if _sc else 0.0

    while not _STOP:
        _sc = _read_sweep_cfg(tuner_id)  # Phase 6c retune
        if _sc and _sc[2] != last_cfg_mt:
            ns, ne, last_cfg_mt = _sc
            if abs(ns - state["band_min_hz"]) > 1 or abs(ne - state["band_max_hz"]) > 1:
                LOG.info("[%s] retune %.2f-%.2fMHz", tuner_id, ns/1e6, ne/1e6)
                band_start, band_end = ns, ne
                state = init_state(tuner_id, band_start, band_end)
                freqs_to_visit = _build_freqs(band_start, band_end)
                total_steps = len(freqs_to_visit)
        for sweep_pos, center_freq in enumerate(freqs_to_visit):
            if _STOP:
                break
            sdr.setFrequency(SOAPY_SDR_RX, 0, center_freq)
            time.sleep(tune_settle_s)
            buf = np.zeros(n_samps_dwell, dtype=np.complex64)
            pos = 0
            t0 = time.time()
            while pos < n_samps_dwell:
                chunk = min(8192, n_samps_dwell - pos)
                sr = sdr.readStream(stream, [buf[pos:pos + chunk]], chunk, timeoutUs=2_000_000)
                if sr.ret < 0:
                    LOG.warning("[%s] readStream error ret=%s flags=%s", tuner_id, sr.ret, sr.flags)
                    break
                pos += sr.ret
                if time.time() - t0 > dwell_s * 4:
                    break
            if pos < fft_size:
                continue
            mag_db = compute_spectrum_db(buf[:pos], fft_size)
            if mag_db is None:
                continue
            dets, noise_floor = find_peaks_in_spectrum(
                mag_db, actual_rate, center_freq, fft_size, threshold, min_bw, noise_pct)
            update_state(state, mag_db, center_freq, actual_rate, sweep_pos, total_steps, fft_size)
            state["noise_floor_dbfs_recent"] = round(noise_floor, 1)
            now = time.time()
            if now - last_age_update > 0.5:
                dt = now - last_age_update
                comp_age = state["composite"]["bin_age_s"]
                for i in range(len(comp_age)):
                    comp_age[i] = round(comp_age[i] + dt, 1)
                last_age_update = now
            try:
                write_state_atomic(state, state_path)
            except Exception as e:
                LOG.warning("[%s] state write failed: %s", tuner_id, e)
            if dets:
                top_dets = sorted(dets, key=lambda x: x["snr_db"], reverse=True)
                slices_emitted = 0
                for d in top_dets:
                    slice_path = None
                    if slices_emitted < slice_per_step_max and d["snr_db"] >= slice_min_snr:
                        slice_path = emit_iq_slice(buf[:pos], d, center_freq, actual_rate,
                                                    tuner_id, SLICES_DIR,
                                                    output_samples=slice_samples,
                                                    target_rate_floor_hz=slice_target_rate_floor)
                        if slice_path:
                            slices_emitted += 1
                    conn.execute(
                        "INSERT INTO detections (ts, tuner_id, freq_hz, bandwidth_hz, power_dbfs, snr_db, slice_path) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (now, tuner_id, d["freq_hz"], d["bandwidth_hz"], d["power_dbfs"], d["snr_db"], slice_path)
                    )
                conn.commit()
                top = top_dets[:3]
                top_str = ", ".join("%.4fMHz@%.1fdB" % (d["freq_hz"]/1e6, d["snr_db"]) for d in top)
                LOG.info("[%s] center=%.3fMHz n=%d slices=%d top=[%s]",
                         tuner_id, center_freq/1e6, len(dets), slices_emitted, top_str)
            if time.time() - last_cleanup > 60:
                cutoff = now - retention_s
                conn.execute("DELETE FROM detections WHERE ts < ?", (cutoff,))
                conn.commit()
                last_cleanup = time.time()
    LOG.info("[%s] stopping; closing stream", tuner_id)
    try:
        sdr.deactivateStream(stream); sdr.closeStream(stream)
    except Exception as e:
        LOG.warning("[%s] cleanup error: %s", tuner_id, e)


def main():
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    p = argparse.ArgumentParser()
    p.add_argument("--tuner-id", required=True)
    args = p.parse_args()
    cfg = load_config(CONFIG_PATH)
    if args.tuner_id not in cfg["tuners"]:
        if re.match(r"^[A-Za-z0-9]+$", args.tuner_id) and "-T" not in args.tuner_id:
            sc = _read_sweep_cfg(args.tuner_id)
            bs, be = (sc[0], sc[1]) if sc else (117e6, 470e6)
            cfg["tuners"][args.tuner_id] = {
                "soapy_args": f"driver=rtlsdr,serial={args.tuner_id}",
                "band_start_hz": bs, "band_end_hz": be}
            LOG.info("synthesized RTL-SDR cfg for %s: %.2f-%.2fMHz", args.tuner_id, bs/1e6, be/1e6)
        else:
            LOG.error("unknown tuner-id: %s; available: %s", args.tuner_id, list(cfg["tuners"].keys()))
            sys.exit(2)
    conn = init_db(cfg["db"]["path"])
    LOG.info("DB: %s  STATE_DIR: %s  SLICES_DIR: %s", cfg["db"]["path"], STATE_DIR, SLICES_DIR)
    try:
        sweep_loop(args.tuner_id, cfg, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
