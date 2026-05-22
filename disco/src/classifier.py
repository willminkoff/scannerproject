"""Disco classifier — consume IQ slices, classify, update DB.

Heuristic v0 by default. If an ONNX model is present at MODEL_PATH, switch to it.
Filename format: {tuner}_{freq_hz}_{bw_hz}_{rate_hz}_{ts}_{uid}.iq.f32 (6 fields)
"""
import argparse
import json
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

# ULS lookup is loaded from the same package; failure to import is non-fatal —
# the classifier still classifies, just without licensee enrichment.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from uls import lookup_uls
    _ULS_AVAILABLE = True
except Exception as _e:
    lookup_uls = None
    _ULS_AVAILABLE = False
    _ULS_IMPORT_ERROR = _e

# CDBS lookup (FCC Media Bureau broadcast database — covers commercial FM
# 88-108 MHz and AM 530-1700 kHz that ULS doesn't carry). Used as a fallback
# after lookup_uls returns nothing for in-band broadcast frequencies.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cdbs import lookup_cdbs
    _CDBS_AVAILABLE = True
except Exception as _ce:
    lookup_cdbs = None
    _CDBS_AVAILABLE = False
    _CDBS_IMPORT_ERROR = _ce

# HPDB lookup (HomePatrol / RadioReference curated DB). Surfaces human
# labels like "Williamson County Fire — Dispatch" and trunked-system
# identities ("Tennessee Advanced Communications Network — West Nashville")
# that ULS/CDBS can't. Tried BEFORE ULS so curated labels win when both
# match; ULS+CDBS still run as fallbacks when HPDB returns nothing.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from hpdb import lookup_hpdb
    _HPDB_AVAILABLE = True
except Exception as _he:
    lookup_hpdb = None
    _HPDB_AVAILABLE = False
    _HPDB_IMPORT_ERROR = _he

# Phase 4 band-plan lookup. If the YAML can't be loaded, classifier_loop
# falls back to _legacy_derive_protocol_tag so detection still works.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import band_plan
    _BAND_PLAN_AVAILABLE = True
except Exception as _bpe:
    band_plan = None
    _BAND_PLAN_AVAILABLE = False
    _BAND_PLAN_IMPORT_ERROR = _bpe

# Travel Mode: current scanner location, sourced from SB3's HPState. ULS
# and CDBS distance filtering follows the iPhone push instead of being
# pinned to Nashville. Import failure is non-fatal: lookups fall back to
# the uls.py/cdbs.py DEFAULT_LAT_DD/DEFAULT_LON_DD constants.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from current_location import get_current_location
    _LOCATION_AVAILABLE = True
except Exception as _loce:
    get_current_location = None
    _LOCATION_AVAILABLE = False
    _LOCATION_IMPORT_ERROR = _loce

# PR A — trust hierarchy. Folds raw HPDB/CDBS/ULS/ML inputs into a single
# IdentificationResult with a confidence tier so the UI can hide conjecture.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from identification import build_identification
    _IDENT_AVAILABLE = True
except Exception as _iee:
    build_identification = None
    _IDENT_AVAILABLE = False
    _IDENT_IMPORT_ERROR = _iee

# PR B — spectrum-signature fingerprinter. Matches measured IQ features
# (3 dB bandwidth, duty cycle, spectral shape) against the curated catalog
# at disco/configs/service_signatures.yaml. Surfaces service names
# (WiFi, NOAA WX, FM broadcast, GMRS/FRS, etc.) where HPDB/ULS/CDBS can't
# — particularly in unlicensed bands.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fingerprint import match_signature as _match_signature
    _FINGERPRINT_AVAILABLE = True
except Exception as _fpe:
    _match_signature = None
    _FINGERPRINT_AVAILABLE = False
    _FINGERPRINT_IMPORT_ERROR = _fpe

# PR #30 — rtl_433 specialist identifier for ISM-band devices. Replays the
# IQ slice through the rtl_433 binary; on a device decode it overrides the
# identification with a high-confidence device name. Import failure is
# non-fatal and the module itself never raises into the caller, so a missing
# binary / disabled flag is a transparent no-op.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rtl433 as _rtl433
    _RTL433_AVAILABLE = True
except Exception as _r4e:
    _rtl433 = None
    _RTL433_AVAILABLE = False
    _RTL433_IMPORT_ERROR = _r4e

# Broadcast band cutoffs (Hz) — CDBS fallback only fires when freq is in-band.
_BCAST_AM_LO_HZ = 530e3
_BCAST_AM_HI_HZ = 1710e3
_BCAST_FM_LO_HZ = 87.9e6
_BCAST_FM_HI_HZ = 108.0e6


def _is_broadcast_band(freq_hz: float) -> bool:
    """True if `freq_hz` is in the AM or FM commercial broadcast band."""
    if freq_hz is None or freq_hz <= 0:
        return False
    return (_BCAST_AM_LO_HZ <= freq_hz <= _BCAST_AM_HI_HZ) or \
           (_BCAST_FM_LO_HZ <= freq_hz <= _BCAST_FM_HI_HZ)


CONFIG_PATH = os.environ.get("DISCO_CONFIG", "/home/ubuntu/scannerproject/disco/configs/sweep.yaml")
SLICES_DIR = os.environ.get("DISCO_SLICES_DIR", "/run/scannerproject/disco/slices")
MODEL_PATH = os.environ.get("DISCO_MODEL_PATH", "/home/ubuntu/scannerproject/disco/models/radioml.onnx")
BAND_PLAN_PATH = os.environ.get("DISCO_BAND_PLAN_PATH", "/home/ubuntu/scannerproject/disco/configs/us_band_plan.yaml")
# Phase 7-mini capture: when enabled, slices from frequency ranges whose true
# modulation we know a priori get archived to per-label directories before the
# normal `unlink` happens. Used to assemble a real-data fine-tune dataset for
# the disco-trained CNN. Disable by setting DISCO_CAPTURE_ENABLED=0.
CAPTURE_DIR = os.environ.get("DISCO_CAPTURE_DIR", "/home/ubuntu/scannerproject/disco/captures")
CAPTURE_ENABLED = os.environ.get("DISCO_CAPTURE_ENABLED", "1") not in ("0", "false", "False", "")
CAPTURE_MAX_PER_LABEL = int(os.environ.get("DISCO_CAPTURE_MAX_PER_LABEL", "2000"))
# Frequency-range → ground-truth label. Order matters: first matching rule wins.
# Bandwidth filters and snr_min are optional — omit to skip a check.
CAPTURE_RULES = [
    # ----- Phase 5 digital-mode rules (placed BEFORE the FM_NARROW rules so
    # digital traffic wins on freq overlaps) ----------------------------------
    #
    # P25 — Nashville public-safety trunked (MTRTRS + TACN) lives in the
    # 700 MHz PS band (769-776 MHz) and 800 MHz PS band (851-869 MHz). These
    # bands are essentially 100% digital P25 in Nashville — band-wide rule
    # has near-zero FP risk. BW window 6-11 kHz covers Phase 1 (C4FM, ~9 kHz)
    # and Phase 2 (TDMA, ~8 kHz), per empirical p10-p90 of legacy P25 captures.
    {"label": "P25",  "freq_min": 769e6, "freq_max": 776e6, "bw_min": 6e3,   "bw_max": 11e3, "snr_min": 15.0},
    {"label": "P25",  "freq_min": 851e6, "freq_max": 869e6, "bw_min": 6e3,   "bw_max": 11e3, "snr_min": 15.0},
    # NXDN — narrow digital in UHF LMR business band. 6.25 kHz channels with
    # ~4-7 kHz occupied BW (textbook; tighter than legacy NXDN_uls empirical
    # tail which extended to 15 kHz and almost certainly wasn't true NXDN).
    {"label": "NXDN", "freq_min": 452e6, "freq_max": 454e6, "bw_min": 4e3,   "bw_max": 7e3,  "snr_min": 15.0},
    # DMR — wider digital in UHF LMR business band. 12.5 kHz channels, ~8-12
    # kHz typical occupied BW. Range 8-14 covers empirical p25-p90 of legacy
    # DMR_uls; 452-458 MHz matches where Nashville commercial DMR actually
    # operates (24 in 452, 47 in 457 per the prior capture histogram).
    {"label": "DMR",  "freq_min": 452e6, "freq_max": 458e6, "bw_min": 8e3,   "bw_max": 14e3, "snr_min": 15.0},
    # ----- Analog-mode rules (existing) --------------------------------------
    # commercial FM broadcast — wide signal, easy classify
    {"label": "FM_BROADCAST", "freq_min": 88e6, "freq_max": 108e6, "bw_min": 100e3, "bw_max": None},
    # NOAA WX (narrow FM voice with subaudible tone)
    {"label": "FM_NARROW",    "freq_min": 162.4e6, "freq_max": 162.6e6, "bw_min": None, "bw_max": 30e3},
    # 2m amateur repeaters — narrow FM, busy in metro Nashville
    {"label": "FM_NARROW",    "freq_min": 144e6, "freq_max": 148e6, "bw_min": None, "bw_max": 30e3},
    # VHF business/public-safety NFM voice (12.5/25 kHz channels)
    {"label": "FM_NARROW",    "freq_min": 150e6, "freq_max": 162e6, "bw_min": None, "bw_max": 30e3},
    # 70cm amateur — narrow FM repeaters
    {"label": "FM_NARROW",    "freq_min": 440e6, "freq_max": 450e6, "bw_min": None, "bw_max": 30e3},
    # GMRS / FRS UHF NFM (462-467 MHz, 12.5 kHz)
    {"label": "FM_NARROW",    "freq_min": 462e6, "freq_max": 468e6, "bw_min": None, "bw_max": 30e3},
    # Airband voice — narrow AM (118-137 MHz)
    {"label": "AM_VOICE",     "freq_min": 118e6, "freq_max": 137e6, "bw_min": None, "bw_max": 25e3},
    # MURS narrow FM (151.82, 151.88, 151.94, 154.57, 154.60 MHz)
    {"label": "FM_NARROW",    "freq_min": 151.7e6, "freq_max": 154.65e6, "bw_min": None, "bw_max": 30e3},
]
# Resample IQ to this rate before ONNX inference. RadioML 2018.01A's CNN learned
# the absolute sample rate, not just the modulation, so feeding it slices at the
# sweep's per-signal-adaptive rate (50 kHz floor) makes commercial FM look
# nothing like the FM examples it trained on. Fixed-rate resample is a
# diagnostic to confirm the rate-mismatch theory; 0 disables.
TARGET_RATE_HZ = int(os.environ.get("DISCO_TARGET_RATE_HZ", "1000000"))
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
        # Phase 3 — FCC ULS enrichment columns. Populated per-detection when
        # `lookup_uls` returns a match within the freq guard band; left NULL
        # for unlicensed bands (broadcast FM, ISM 902-928, airband, etc.) and
        # for amateur-band fallback hits where no point-frequency match exists.
        ("uls_callsign", "TEXT"),
        ("uls_entity_name", "TEXT"),
        ("uls_emission_designator", "TEXT"),
        ("uls_station_class", "TEXT"),
        ("uls_distance_km", "REAL"),
        ("uls_source", "TEXT"),
        ("uls_lookup_ts", "REAL"),
        # PR A (trust hierarchy): the IdentificationResult's tier + source +
        # service-name string get persisted alongside the raw ML fields so the
        # dashboard can filter by confidence without re-running the layer
        # fall-through on every render. `id_evidence_json` is the dict-as-JSON
        # of supporting evidence (raw lookup payloads, ml class, band-rejected
        # flag, snr) for the details panel.
        ("id_service", "TEXT"),
        ("id_confidence", "TEXT"),
        ("id_source", "TEXT"),
        ("id_band_name", "TEXT"),
        ("id_evidence_json", "TEXT"),
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


def _legacy_derive_protocol_tag(class_name: str, freq_hz: float, bandwidth_hz: float) -> str:
    """Pre-Phase-4 tag derivation. Superseded by band_plan.tag_for() called via
    the new derive_protocol_tag() below. Kept in place for one release as a
    fallback when the band-plan YAML can't be loaded.

    The body checks for class names ("AM", "FM", "FSK2", "FSK4", "BPSK",
    "8PSK", RadioML's "AM-DSB-*" / "AM-SSB-*") that don't match the v3 ONNX
    model's actual output (FM_BROADCAST, FM_NARROW, AM_VOICE, NXDN, P25, etc.)
    — most branches are dead code against the deployed model. Phase 4's
    band_plan path is the source of truth for new code.
    """
    f_mhz = freq_hz / 1e6
    bw_khz = bandwidth_hz / 1e3
    # heuristic-era classes ("AM", "FM", "FSK2/4", "SSB") and RadioML class names
    # ("AM-DSB-WC", "AM-DSB-SC", "AM-SSB-WC", "AM-SSB-SC", "FM", "GMSK", PSK/QAM/APSK
    # families) coexist — derive a canonical family tag first.
    is_am = class_name == "AM" or class_name.startswith("AM-")
    is_fm = class_name == "FM"
    is_ssb = class_name == "SSB" or class_name.startswith("AM-SSB-")
    is_qam_psk = class_name in ("BPSK","QPSK","8PSK","16PSK","32PSK","OQPSK") \
                 or class_name.endswith("QAM") or class_name.endswith("APSK")
    if is_am and 118 <= f_mhz <= 137 and bw_khz < 25:
        return "Airband (AM)"
    if is_am and 108 <= f_mhz <= 118:
        return "Aero NavBeacon (AM)"
    if is_fm and 162.0 <= f_mhz <= 162.6 and bw_khz < 25:
        return "NOAA WX"
    if is_fm and 88 <= f_mhz <= 108:
        return "Broadcast FM"
    if is_fm and 144 <= f_mhz <= 148:
        return "2m amateur"
    if is_fm and 420 <= f_mhz <= 450:
        return "70cm amateur"
    if is_fm and 154 <= f_mhz <= 161 and bw_khz < 30:
        return "VHF business / public safety"
    if class_name in ("FSK4", "FSK2") and 450 <= f_mhz <= 470 and 8 <= bw_khz <= 16:
        return "DMR/NXDN candidate"
    if class_name in ("FSK4", "FSK2") and 760 <= f_mhz <= 870 and 8 <= bw_khz <= 16:
        return "P25 candidate"
    if class_name in ("FSK4", "FSK2") and 25 <= f_mhz <= 60:
        return "Lo-VHF FSK / pager"
    if (class_name in ("FSK4",) or is_qam_psk) and 880 <= f_mhz <= 960:
        return "Cellular candidate"
    if class_name == "GMSK" and 880 <= f_mhz <= 960:
        return "GSM candidate"
    if is_qam_psk and 700 <= f_mhz <= 900:
        return "LTE/cellular candidate"
    if is_fm and 470 <= f_mhz <= 700:
        return "UHF TV audio?"
    if is_fm and 902 <= f_mhz <= 928:
        return "ISM 902"
    return class_name


def derive_protocol_tag(class_name: str, freq_hz: float, bandwidth_hz: float, plan=None) -> str:
    """Phase 4 band-plan-first tag derivation.

    Delegates to band_plan.tag_for(), which constrains the v3 ML class against
    FCC band-plan allowed_modes. Out-of-band predictions get downgraded to
    "<BAND_NAME> — unidentified" — operator-facing tag, no parenthetical.
    The raw ml_class is still preserved in detections.modulation_class for
    retrain-set curation and is what interpret.py reads directly.

    `bandwidth_hz` is unused (kept for API compatibility with the pre-Phase-4
    signature). Bandwidth was only consulted by stale heuristic-era branches.

    `plan` is the loaded band-plan returned by band_plan.load_band_plan().
    When None, falls back to _legacy_derive_protocol_tag — happens at startup
    if the YAML can't be loaded, and during isolated tests.
    """
    if plan is None or band_plan is None:
        return _legacy_derive_protocol_tag(class_name, freq_hz, bandwidth_hz)
    tag = band_plan.tag_for(class_name, freq_hz, plan)
    if not band_plan.is_mode_allowed(class_name, freq_hz, plan):
        # Operator-visible audit trail — every band-plan rejection logs once
        # so historical anomalies can be grep'd from the journal.
        b = band_plan.band_for(freq_hz, plan)
        LOG.info("band-plan rejected: freq=%.4f MHz ml=%s band=%s → %s",
                 freq_hz / 1e6, class_name, b.name if b else "?", tag)
    return tag


def _match_capture_rule(meta: dict, snr_db: Optional[float] = None):
    """Return the label of the first matching CAPTURE_RULES entry, or None.

    `snr_db` is optional — only rules with a `snr_min` field consult it. The
    analog (FM_BROADCAST / FM_NARROW / AM_VOICE) rules omit `snr_min` so they
    keep matching unchanged. Digital rules use snr_min=15.0 to drop low-SNR
    captures that won't make useful training data anyway.
    """
    f = meta.get("freq_hz")
    bw = meta.get("bandwidth_hz") or 0.0
    if f is None:
        return None
    for rule in CAPTURE_RULES:
        if not (rule["freq_min"] <= f <= rule["freq_max"]):
            continue
        if rule.get("bw_min") is not None and bw < rule["bw_min"]:
            continue
        if rule.get("bw_max") is not None and bw > rule["bw_max"]:
            continue
        snr_min = rule.get("snr_min")
        if snr_min is not None and (snr_db is None or snr_db < snr_min):
            continue
        return rule["label"]
    return None


_CAPTURE_COUNTS: dict = {}


def _capture_count(label: str) -> int:
    """Lazy count of .iq.f32 captures in the dir for a label.

    Counts ONLY .iq.f32 files, not the .meta sidecars maybe_archive_slice
    writes alongside each capture. Before this fix, os.listdir() returned
    both and the cap check (`>= CAPTURE_MAX_PER_LABEL`) was effectively
    halved — labels with >1000 captures (FM_BROADCAST, P25) silently
    stopped archiving because 2*N entries exceeded the 2000-file cap
    even though only N slices were present. P25 was the operational
    casualty: 1208 actual captures × 2 sidecars = 2416 ≥ 2000, so the
    hill-stint P25 traffic was being classified but not archived.
    """
    if label not in _CAPTURE_COUNTS:
        d = os.path.join(CAPTURE_DIR, label)
        try:
            _CAPTURE_COUNTS[label] = sum(1 for f in os.listdir(d) if f.endswith(".iq.f32"))
        except FileNotFoundError:
            _CAPTURE_COUNTS[label] = 0
    return _CAPTURE_COUNTS[label]


def maybe_archive_slice(slice_path: str, meta: dict, snr_db: Optional[float]) -> Optional[str]:
    """Archive the slice to /captures/<label>/ if a rule matches and we're under
    the per-label cap. Returns the destination path if archived, else None.
    Errors are logged but never block the classifier."""
    if not CAPTURE_ENABLED:
        return None
    label = _match_capture_rule(meta, snr_db)
    if label is None:
        return None
    n = _capture_count(label)
    if n >= CAPTURE_MAX_PER_LABEL:
        return None
    try:
        import shutil
        d = os.path.join(CAPTURE_DIR, label)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(slice_path))
        shutil.copy2(slice_path, dst)
        # tiny sidecar with snr (filename already encodes freq/bw/rate/ts)
        if snr_db is not None:
            with open(dst + ".meta", "w") as fh:
                fh.write(f"snr_db={snr_db:.2f}\n")
        _CAPTURE_COUNTS[label] = n + 1
        return dst
    except Exception as e:
        LOG.warning("capture archive failed for %s: %s", slice_path, e)
        return None


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


def resample_iq(iq: np.ndarray, src_rate: float, target_rate: int) -> np.ndarray:
    """Polyphase resample complex IQ from src_rate to target_rate. Returns
    iq unchanged if rates are equal or target_rate <= 0 (resample disabled)."""
    if target_rate <= 0:
        return iq
    src = int(round(src_rate))
    if src == target_rate:
        return iq
    from math import gcd
    g = gcd(src, target_rate)
    up = target_rate // g
    down = src // g
    from scipy.signal import resample_poly
    # resample real+imag separately to keep dtype tight; resample_poly handles
    # complex but routes through abs/angle which is slower than two real passes.
    r = resample_poly(iq.real.astype(np.float32), up, down).astype(np.float32)
    i = resample_poly(iq.imag.astype(np.float32), up, down).astype(np.float32)
    return r + 1j * i


def load_class_names(model_path: str):
    """Class names sit next to the ONNX as `<model>.classes.json`.
    Returns [] if not present — caller falls back to `class_<idx>`."""
    sidecar = os.path.splitext(model_path)[0] + ".classes.json"
    try:
        import json
        with open(sidecar) as f:
            names = json.load(f)
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            LOG.info("loaded %d class names from %s", len(names), sidecar)
            return names
    except FileNotFoundError:
        LOG.info("no class names sidecar at %s; using class_<idx> labels", sidecar)
    except Exception as e:
        LOG.warning("failed to read class names %s: %s", sidecar, e)
    return []


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
    class_names = load_class_names(MODEL_PATH) if onnx_sess is not None else []
    backend = "onnx" if onnx_sess else "heuristic"

    # Phase 4: load FCC band plan once at startup. Failure is non-fatal —
    # derive_protocol_tag() falls back to _legacy_derive_protocol_tag when plan is None.
    plan = None
    if _BAND_PLAN_AVAILABLE:
        try:
            plan = band_plan.load_band_plan(BAND_PLAN_PATH)
            LOG.info("band-plan loaded: %d bands from %s", len(plan), BAND_PLAN_PATH)
        except Exception as _bpe:
            LOG.warning("band-plan load failed (%s); falling back to legacy tag derivation", _bpe)
    else:
        LOG.warning("band-plan import unavailable: %s; using legacy tag derivation", _BAND_PLAN_IMPORT_ERROR)

    LOG.info("classifier backend=%s confidence_threshold=%.2f target_rate_hz=%d uls=%s cdbs=%s band_plan=%s",
             backend, confidence_threshold, TARGET_RATE_HZ,
             "available" if _ULS_AVAILABLE else "DISABLED",
             "available" if _CDBS_AVAILABLE else "DISABLED",
             "available" if plan else "DISABLED")
    if not _ULS_AVAILABLE:
        LOG.warning("ULS lookup disabled: %s", _ULS_IMPORT_ERROR)
    if not _CDBS_AVAILABLE:
        LOG.warning("CDBS lookup disabled: %s", _CDBS_IMPORT_ERROR)

    # PR #30 — log rtl_433 status and seed the stats file so /api/status
    # reflects this classifier's actual config (binary + kill switch) even
    # before the first ISM detection.
    if _RTL433_AVAILABLE and _rtl433 is not None:
        LOG.info("rtl_433 layer: binary=%s enabled=%s",
                 "present" if _rtl433.is_available() else "MISSING",
                 _rtl433.is_enabled())
        try:
            _rtl433._write_stats()
        except Exception:
            pass
    else:
        LOG.warning("rtl_433 module import unavailable: %s", _RTL433_IMPORT_ERROR)

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
                    iq_for_model = resample_iq(iq, slice_rate, TARGET_RATE_HZ)
                    n = len(iq_for_model)
                    if n < 1024:
                        # short slice — pad with zeros at the end to match model input
                        pad = np.zeros(1024 - n, dtype=iq_for_model.dtype)
                        iq_window = np.concatenate([iq_for_model, pad])
                    else:
                        # take the central 1024 samples; resampling can stretch the
                        # array well past 1024 (e.g. 50kHz → 1MHz on a 2048-sample
                        # slice yields 40960 samples) and the central window holds
                        # the cleanest signal away from edge filter artifacts.
                        start = (n - 1024) // 2
                        iq_window = iq_for_model[start:start + 1024]
                    iq_norm = iq_window / (np.max(np.abs(iq_window)) + 1e-12)
                    inp = np.stack([iq_norm.real, iq_norm.imag]).astype(np.float32)[None, :]
                    out = onnx_sess.run(None, {onnx_sess.get_inputs()[0].name: inp})[0]
                    probs = np.exp(out - np.max(out))
                    probs = probs / probs.sum()
                    cls_idx = int(np.argmax(probs))
                    conf = float(probs[0, cls_idx])
                    cls_name = class_names[cls_idx] if cls_idx < len(class_names) else f"class_{cls_idx}"
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
                tag = derive_protocol_tag(cls_name, meta["freq_hz"], meta["bandwidth_hz"], plan)
            det_id = find_detection_id(conn, slice_path)
            now = time.time()
            if det_id is not None:
                # Run all three FCC database lookups concurrently in terms of
                # what we capture — the trust hierarchy (PR A) decides which
                # one wins based on the IdentificationResult fall-through. We
                # capture the RAW match dict for each so build_identification()
                # can compose the structured result and so the details panel
                # gets the full evidence.
                hpdb_match: dict | None = None
                cdbs_match: dict | None = None
                uls_match: dict | None = None

                if _HPDB_AVAILABLE and lookup_hpdb is not None:
                    try:
                        if _LOCATION_AVAILABLE and get_current_location is not None:
                            _loc = get_current_location()
                            _hpdb_rows = lookup_hpdb(
                                meta["freq_hz"],
                                lat_dd=_loc.lat,
                                lon_dd=_loc.lon,
                                limit=1,
                            )
                        else:
                            _hpdb_rows = lookup_hpdb(meta["freq_hz"], limit=1)
                        if _hpdb_rows:
                            hpdb_match = dict(_hpdb_rows[0])
                    except Exception as _he2:
                        LOG.warning("hpdb lookup failed at %.6f MHz: %s",
                                    meta["freq_hz"] / 1e6, _he2)

                if hpdb_match is None and _ULS_AVAILABLE and lookup_uls is not None:
                    try:
                        if _LOCATION_AVAILABLE and get_current_location is not None:
                            _loc = get_current_location()
                            _uls_rows = lookup_uls(
                                meta["freq_hz"],
                                lat_dd=_loc.lat,
                                lon_dd=_loc.lon,
                                limit=1,
                            )
                        else:
                            _uls_rows = lookup_uls(meta["freq_hz"], limit=1)
                        if _uls_rows:
                            uls_match = dict(_uls_rows[0])
                    except Exception as _ee:
                        LOG.warning("uls lookup failed at %.6f MHz: %s",
                                    meta["freq_hz"] / 1e6, _ee)

                # CDBS fallback: ULS doesn't carry commercial AM/FM broadcast.
                # Only query when ULS returned nothing AND the freq is in an
                # AM or FM broadcast band.
                if (hpdb_match is None and uls_match is None
                        and _CDBS_AVAILABLE and lookup_cdbs is not None
                        and _is_broadcast_band(meta["freq_hz"])):
                    try:
                        if _LOCATION_AVAILABLE and get_current_location is not None:
                            _loc = get_current_location()
                            _cdbs_rows = lookup_cdbs(
                                meta["freq_hz"],
                                lat_dd=_loc.lat,
                                lon_dd=_loc.lon,
                                limit=1,
                            )
                        else:
                            _cdbs_rows = lookup_cdbs(meta["freq_hz"], limit=1)
                        if _cdbs_rows:
                            cdbs_match = dict(_cdbs_rows[0])
                    except Exception as _ce2:
                        LOG.warning("cdbs lookup failed at %.6f MHz: %s",
                                    meta["freq_hz"] / 1e6, _ce2)

                # Capture snr_db ahead of the identification so the spurious
                # tier can fire on sub-floor signals. Reads the sweep-computed
                # value persisted earlier in the same row.
                _snr_db = None
                try:
                    _cur = conn.execute(
                        "SELECT snr_db FROM detections WHERE id = ?", (det_id,)
                    )
                    _r = _cur.fetchone()
                    if _r and _r[0] is not None:
                        _snr_db = float(_r[0])
                except Exception:
                    pass

                # Resolve band info for the identification fall-through. The
                # `tag` string already encodes band-rejection ("BAND — unidentified")
                # but the dataclass wants band_name + band_rejected explicitly.
                _band_name = None
                _band_rejected = False
                _band_allowed_modes: list = []
                if _BAND_PLAN_AVAILABLE and band_plan is not None and plan:
                    try:
                        _b = band_plan.band_for(meta["freq_hz"], plan)
                        if _b is not None:
                            _band_name = _b.name
                            _band_allowed_modes = list(_b.allowed_modes)
                            _band_rejected = not band_plan.is_mode_allowed(
                                final_class, meta["freq_hz"], plan
                            )
                    except Exception:
                        pass

                # PR #30 — rtl_433 device decode for ISM-band slices. Sits
                # above the signature fingerprint in the trust hierarchy: a
                # decoded device packet is a definitive protocol-level ID.
                # Gated identically to the signature layer (only when HPDB+CDBS
                # missed) plus an ISM-band check, so the subprocess only runs
                # where it can plausibly help. The module never raises — a
                # missing binary, disabled flag, timeout, or garbage output
                # all return None and we fall through to the signature layer.
                rtl433_match_dict: dict | None = None
                if (hpdb_match is None and cdbs_match is None
                        and _RTL433_AVAILABLE and _rtl433 is not None
                        and _rtl433.is_ism_band(meta["freq_hz"])
                        and _rtl433.is_available() and _rtl433.is_enabled()):
                    try:
                        rtl433_match_dict = _rtl433.lookup_rtl433(
                            slice_path,
                            meta["freq_hz"],
                            sample_rate_hz=slice_rate,
                        )
                    except Exception as _r4le:
                        # Defense in depth — the module already swallows all
                        # exceptions, but never let it break classification.
                        LOG.warning("rtl_433 lookup raised at %.6f MHz: %s",
                                    meta["freq_hz"] / 1e6, _r4le)
                        rtl433_match_dict = None

                # PR B — fingerprint the slice's spectral + temporal features
                # against the curated catalog. Only fires when HPDB/CDBS
                # both returned empty (the fingerprint layer in the trust
                # hierarchy sits between CDBS and ULS, so a HPDB or CDBS hit
                # already supersedes signature_match). Errors are swallowed
                # so a misbehaving fingerprinter never blocks classification.
                sig_match_dict: dict | None = None
                if (hpdb_match is None and cdbs_match is None
                        and _FINGERPRINT_AVAILABLE and _match_signature is not None):
                    try:
                        sig = _match_signature(
                            iq,
                            float(slice_rate),
                            float(meta["freq_hz"]),
                            snr_db=float(_snr_db or 0.0),
                            band_name=_band_name,
                        )
                        if sig is not None:
                            sig_match_dict = sig.to_dict()
                    except Exception as _sige:
                        LOG.warning("fingerprint failed at %.6f MHz: %s",
                                    meta["freq_hz"] / 1e6, _sige)

                # Run the trust hierarchy fall-through. The result drives the
                # new id_* columns AND interpret.py's Claude gate.
                ident = None
                if _IDENT_AVAILABLE and build_identification is not None:
                    try:
                        ident = build_identification(
                            modulation_class=final_class,
                            modulation_confidence=conf,
                            snr_db=_snr_db,
                            band_name=_band_name,
                            band_rejected=_band_rejected,
                            band_allowed_modes=_band_allowed_modes,
                            hpdb_match=hpdb_match,
                            cdbs_match=cdbs_match,
                            rtl433_match=rtl433_match_dict,
                            uls_match=uls_match,
                            signature_match=sig_match_dict,
                        )
                    except Exception as _ide:
                        LOG.warning("build_identification failed at %.6f MHz: %s",
                                    meta["freq_hz"] / 1e6, _ide)

                # Legacy uls_* fields kept for back-compat with the dashboard's
                # existing /api/strongest SQL and any third-party consumers.
                # Populated from whichever DB layer the identification picked,
                # so the legacy columns and the new id_* columns agree on the
                # source.
                uls_call = uls_name = uls_emit = uls_stclass = uls_src = None
                uls_dist = None
                if hpdb_match is not None:
                    uls_call = hpdb_match.get("alpha_tag")
                    uls_name = hpdb_match.get("system_name") or hpdb_match.get("group_name")
                    uls_emit = hpdb_match.get("mode")
                    uls_stclass = hpdb_match.get("service_type")
                    uls_dist = hpdb_match.get("distance_km")
                    uls_src = f"hpdb-{hpdb_match.get('source_table', 'unknown')}"
                elif uls_match is not None:
                    uls_call = uls_match.get("callsign")
                    uls_name = uls_match.get("entity_name")
                    uls_emit = uls_match.get("emission_designator")
                    uls_stclass = uls_match.get("station_class")
                    uls_dist = uls_match.get("distance_km")
                    uls_src = uls_match.get("source")
                elif cdbs_match is not None:
                    uls_call = cdbs_match.get("callsign")
                    uls_name = cdbs_match.get("entity_name")
                    uls_emit = cdbs_match.get("emission_designator")
                    uls_stclass = cdbs_match.get("station_class")
                    uls_dist = cdbs_match.get("distance_km")
                    uls_src = cdbs_match.get("source") or "cdbs"

                # Serialize id_evidence_json — small enough at ~1 KB per row to
                # keep inline rather than spinning up a sidecar table.
                _id_service = _id_confidence = _id_source = _id_band = None
                _id_evidence_json = None
                if ident is not None:
                    _id_service = ident.service
                    _id_confidence = ident.confidence
                    _id_source = ident.source
                    _id_band = ident.band_name
                    try:
                        _id_evidence_json = json.dumps(
                            ident.evidence, default=str, sort_keys=True
                        )
                    except Exception:
                        _id_evidence_json = None

                conn.execute(
                    "UPDATE detections SET modulation_class = ?, modulation_confidence = ?, "
                    "protocol_tag = ?, classified_ts = ?, "
                    "uls_callsign = ?, uls_entity_name = ?, uls_emission_designator = ?, "
                    "uls_station_class = ?, uls_distance_km = ?, uls_source = ?, "
                    "uls_lookup_ts = ?, "
                    "id_service = ?, id_confidence = ?, id_source = ?, "
                    "id_band_name = ?, id_evidence_json = ? "
                    "WHERE id = ?",
                    (final_class, conf, tag, now,
                     uls_call, uls_name, uls_emit, uls_stclass, uls_dist, uls_src, now,
                     _id_service, _id_confidence, _id_source, _id_band, _id_evidence_json,
                     det_id)
                )
                conn.commit()
                classified_count += 1
            # Phase 7-mini capture: archive slice to /captures/<label>/ if a
            # frequency-range rule matches and we're under the per-label cap.
            # Pulls SNR from the freshly-updated row so the .meta sidecar has
            # the snr that sweep.py computed (more accurate than re-deriving).
            try:
                snr_db = None
                if det_id is not None:
                    cur = conn.execute("SELECT snr_db FROM detections WHERE id = ?", (det_id,))
                    row = cur.fetchone()
                    if row and row[0] is not None:
                        snr_db = float(row[0])
                maybe_archive_slice(slice_path, meta, snr_db)
            except Exception as _ce:
                LOG.warning("capture step failed: %s", _ce)
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
