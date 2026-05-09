"""Disco classifier — consume IQ slices, classify, update DB.

Heuristic v0 by default. If an ONNX model is present at MODEL_PATH, switch to it.
Filename format: {tuner}_{freq_hz}_{bw_hz}_{rate_hz}_{ts}_{uid}.iq.f32 (6 fields)
"""
import argparse
import logging
import os
import signal
import sqlite3
import sys
import time
import glob
from typing import Optional, Tuple

import numpy as np
import yaml

CONFIG_PATH = os.environ.get("DISCO_CONFIG", "/home/ubuntu/scannerproject/disco/configs/sweep.yaml")
SLICES_DIR = os.environ.get("DISCO_SLICES_DIR", "/run/scannerproject/disco/slices")
MODEL_PATH = os.environ.get("DISCO_MODEL_PATH", "/home/ubuntu/scannerproject/disco/models/radioml.onnx")
LOG = logging.getLogger("disco.classifier")
_STOP = False


def _handle_stop(signum, frame):
    global _STOP
    LOG.info("stopping on signal %s", signum)
    _STOP = True


def setup_logging():
    logging.basicConfig(
        level=os.environ.get("DISCO_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def migrate_schema(conn):
    cur = conn.execute("PRAGMA table_info(detections)")
    existing = {row[1] for row in cur.fetchall()}
    added = []
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
            added.append(col)
    if added:
        conn.commit()
        LOG.info("migrated schema: added %s", added)


def load_iq_slice(path: str) -> Optional[np.ndarray]:
    try:
        return np.fromfile(path, dtype=np.complex64)
    except Exception as e:
        LOG.warning("failed to load %s: %s", path, e)
        return None


def heuristic_classify(iq: np.ndarray, sample_rate: float) -> Tuple[str, float]:
    """Heuristic class+confidence using centered baseband IQ at given sample rate."""
    if len(iq) < 256:
        return ("unclassified", 0.0)
    abs_iq = np.abs(iq).astype(np.float32)
    mean_amp = float(np.mean(abs_iq))
    std_amp = float(np.std(abs_iq))
    if mean_amp < 1e-7:
        return ("unclassified", 0.0)
    amp_cv = std_amp / mean_amp

    phase = np.angle(iq).astype(np.float32)
    phase_unwrapped = np.unwrap(phase)
    inst_freq = np.diff(phase_unwrapped)
    inst_freq_mean = float(np.mean(inst_freq))
    inst_freq_std = float(np.std(inst_freq))

    fft_size = 2048 if len(iq) >= 2048 else (1024 if len(iq) >= 1024 else 512)
    block = iq[:fft_size]
    window = np.hanning(fft_size).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft(block * window))
    mag = np.abs(spec)
    mag_norm = mag / (np.max(mag) + 1e-12)
    threshold = 0.3
    above = mag_norm > threshold
    n_peaks = 0
    in_run = False
    for v in above:
        if v and not in_run:
            in_run = True
            n_peaks += 1
        elif not v:
            in_run = False

    spec_db = 20.0 * np.log10(mag + 1e-30)
    spec_db -= np.max(spec_db)
    half = len(spec_db) // 2
    upper_e = float(np.sum(mag[half:] ** 2))
    lower_e = float(np.sum(mag[:half] ** 2))
    sideband_imbalance = abs(upper_e - lower_e) / (upper_e + lower_e + 1e-12)

    if amp_cv < 0.20 and n_peaks <= 1:
        if inst_freq_std > 0.05:
            conf = float(min(0.95, 0.65 + (0.20 - amp_cv) * 1.5 + (inst_freq_std - 0.05) * 1.0))
            return ("FM", conf)
        else:
            return ("CW", 0.75)
    if amp_cv > 0.35 and n_peaks <= 1:
        if sideband_imbalance > 0.30:
            return ("SSB", min(0.85, 0.55 + sideband_imbalance))
        return ("AM", min(0.92, 0.60 + amp_cv * 0.5))
    if n_peaks >= 4 and amp_cv < 0.30:
        return ("FSK4", 0.70)
    if n_peaks == 2 and amp_cv < 0.30:
        return ("FSK2", 0.70)
    if 0.20 <= amp_cv <= 0.40 and n_peaks <= 2 and inst_freq_std > 0.08:
        return ("PSK", 0.55)
    return ("unclassified", 0.30)


def derive_protocol_tag(class_name: str, freq_hz: float, bandwidth_hz: float) -> str:
    f_mhz = freq_hz / 1e6
    bw_khz = bandwidth_hz / 1e3
    if class_name == "AM" and 118 <= f_mhz <= 137 and bw_khz < 25:
        return "Airband (AM)"
    if class_name == "FM" and 162.0 <= f_mhz <= 162.6 and bw_khz < 25:
        return "NOAA WX"
    if class_name == "FM" and 88 <= f_mhz <= 108:
        return "Broadcast FM"
    if class_name == "FM" and 144 <= f_mhz <= 148:
        return "2m amateur"
    if class_name == "FM" and 420 <= f_mhz <= 450:
        return "70cm amateur"
    if class_name == "FM" and 154 <= f_mhz <= 161 and bw_khz < 30:
        return "VHF business / public safety"
    if class_name in ("FSK4", "FSK2") and 450 <= f_mhz <= 470 and 8 <= bw_khz <= 16:
        return "DMR/NXDN candidate"
    if class_name in ("FSK4", "FSK2") and 760 <= f_mhz <= 870 and 8 <= bw_khz <= 16:
        return "P25 candidate"
    if class_name in ("FSK4", "FSK2") and 25 <= f_mhz <= 60:
        return "Lo-VHF FSK / pager"
    if class_name in ("FSK4",) and 880 <= f_mhz <= 960:
        return "Cellular candidate"
    if class_name == "FM" and 470 <= f_mhz <= 700:
        return "UHF TV audio?"
    if class_name == "AM" and 108 <= f_mhz <= 118:
        return "Aero NavBeacon (AM)"
    if class_name == "FM" and 902 <= f_mhz <= 928:
        return "ISM 902"
    return class_name


def init_onnx_model(path: str):
    if not os.path.isfile(path):
        return None
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        LOG.info("loaded ONNX model from %s", path)
        return sess
    except Exception as e:
        LOG.warning("failed to load ONNX model %s: %s", path, e)
        return None


def parse_slice_meta(filename: str) -> Optional[dict]:
    """Filename: {tuner}_{freq_hz}_{bw_hz}_{rate_hz}_{ts}_{uid}.iq.f32 (6 underscored fields)"""
    base = os.path.basename(filename)
    if not base.endswith(".iq.f32"):
        return None
    parts = base[: -len(".iq.f32")].split("_")
    if len(parts) < 6:
        return None
    try:
        return {
            "tuner_id": parts[0],
            "freq_hz": float(parts[1]),
            "bandwidth_hz": float(parts[2]),
            "rate_hz": float(parts[3]),
            "ts": float(parts[4]),
            "uid": parts[5],
        }
    except Exception:
        return None


def find_detection_id(conn, slice_path: str) -> Optional[int]:
    for attempt in range(3):
        rows = conn.execute(
            "SELECT id FROM detections WHERE slice_path = ? AND classified_ts IS NULL LIMIT 1",
            (slice_path,)
        ).fetchall()
        if rows:
            return rows[0][0]
        time.sleep(0.15)
    return None


def classifier_loop(cfg, conn):
    classifier_cfg = cfg.get("classifier", {})
    confidence_threshold = float(classifier_cfg.get("confidence_threshold", 0.6))
    poll_interval_ms = int(classifier_cfg.get("poll_interval_ms", 250))
    max_buffered = int(classifier_cfg.get("max_buffered_slices", 1000))

    onnx_sess = init_onnx_model(MODEL_PATH)
    backend = "onnx" if onnx_sess else "heuristic"
    LOG.info("classifier backend=%s confidence_threshold=%.2f", backend, confidence_threshold)

    seen_count = 0; classified_count = 0; last_log = time.time()

    while not _STOP:
        try:
            slices = sorted(glob.glob(os.path.join(SLICES_DIR, "*.iq.f32")))
        except Exception:
            slices = []
        if len(slices) > max_buffered:
            for old in slices[:-max_buffered]:
                try: os.unlink(old)
                except: pass
            slices = slices[-max_buffered:]
        if not slices:
            time.sleep(poll_interval_ms / 1000.0)
            continue
        for slice_path in slices:
            if _STOP:
                break
            meta = parse_slice_meta(slice_path)
            if not meta:
                try: os.unlink(slice_path)
                except: pass
                continue
            iq = load_iq_slice(slice_path)
            if iq is None or len(iq) < 256:
                try: os.unlink(slice_path)
                except: pass
                continue
            slice_rate = meta["rate_hz"]
            if onnx_sess is not None:
                try:
                    iq_norm = iq[:1024] / (np.max(np.abs(iq[:1024])) + 1e-12)
                    inp = np.stack([iq_norm.real, iq_norm.imag]).astype(np.float32)[None, :]
                    out = onnx_sess.run(None, {onnx_sess.get_inputs()[0].name: inp})[0]
                    probs = np.exp(out - np.max(out))
                    probs = probs / probs.sum()
                    cls_idx = int(np.argmax(probs))
                    conf = float(probs[0, cls_idx])
                    cls_name = f"class_{cls_idx}"
                except Exception as e:
                    LOG.warning("ONNX inference failed: %s; falling back to heuristic", e)
                    cls_name, conf = heuristic_classify(iq, slice_rate)
            else:
                cls_name, conf = heuristic_classify(iq, slice_rate)
            if conf < confidence_threshold:
                final_class = "unclassified"
                tag = "unclassified"
            else:
                final_class = cls_name
                tag = derive_protocol_tag(cls_name, meta["freq_hz"], meta["bandwidth_hz"])
            det_id = find_detection_id(conn, slice_path)
            now = time.time()
            if det_id is not None:
                conn.execute(
                    "UPDATE detections SET modulation_class = ?, modulation_confidence = ?, "
                    "protocol_tag = ?, classified_ts = ? WHERE id = ?",
                    (final_class, conf, tag, now, det_id)
                )
                conn.commit()
                classified_count += 1
            try: os.unlink(slice_path)
            except: pass
            seen_count += 1
        if time.time() - last_log > 30:
            LOG.info("processed=%d classified=%d backend=%s", seen_count, classified_count, backend)
            last_log = time.time()


def main():
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    cfg = load_config(CONFIG_PATH)
    os.makedirs(SLICES_DIR, exist_ok=True)
    conn = sqlite3.connect(cfg["db"]["path"], timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    migrate_schema(conn)
    LOG.info("DB: %s  SLICES_DIR: %s", cfg["db"]["path"], SLICES_DIR)
    try:
        classifier_loop(cfg, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
