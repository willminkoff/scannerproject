"""Disco — spectrum-signature fingerprinter.

Takes the captured IQ slice that fed the modulation classifier and tries
to identify the *service* (WiFi 2.4 GHz channel 6, NOAA WX, FM broadcast,
GMRS narrow-FM, etc.) by matching measured features against a curated
catalog at ``disco/configs/service_signatures.yaml``.

Where HPDB/CDBS/ULS identify WHO is licensed at a frequency, the
fingerprinter identifies WHAT modulation/standard is being used. They're
complementary: in unlicensed bands (ISM 2.4, ISM 900) or for non-LMR
services (WiFi, BT, LoRa), HPDB/CDBS/ULS return nothing and the
fingerprinter is the only way to put a name on the signal.

Measured features (per slice):
  bw_3db_hz   — half-power bandwidth, measured from FFT
  bw_30db_hz  — wider (-30 dB) bandwidth, captures shoulders/skirt
  duty        — "continuous" | "bursty" | "hopping" (from envelope analysis)
  shape       — "narrow_carrier" | "wide_flat" | "ofdm_multicarrier" |
                "fsk_two_tone" | "broadband_noise" | "unknown"
                (heuristic from peak-counting + spectrum entropy)
  peak_count  — number of distinct spectral peaks above floor + threshold

The matcher iterates the catalog, scores each entry whose freq_min/max
brackets the detection frequency, and returns the best-scoring match
above a configurable confidence floor (default 0.65).

This module is a **best-effort MVP** of the Phase ID-B catalog scope.
The shipped catalog covers the highest-value services in Will's bands;
extending it is a YAML-only operation (no code changes needed for new
entries).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    import yaml
    _YAML_AVAILABLE = True
except Exception:
    yaml = None
    _YAML_AVAILABLE = False


_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_BUNDLED_CATALOG_PATH = os.path.normpath(
    os.path.join(_MODULE_DIR, "..", "configs", "service_signatures.yaml")
)
DEFAULT_CATALOG_PATH = os.environ.get(
    "DISCO_SIGNATURE_CATALOG_PATH",
    _BUNDLED_CATALOG_PATH,
)


@dataclass
class FingerprintFeatures:
    """Measured spectral + temporal features of a single IQ slice."""
    freq_hz: float
    sample_rate_hz: float
    bw_3db_hz: float
    bw_30db_hz: float
    peak_count: int
    duty: str          # continuous | bursty | hopping
    shape: str         # narrow_carrier | wide_flat | ofdm_multicarrier | fsk_two_tone | broadband_noise | unknown
    snr_db: float

    def to_dict(self) -> dict:
        return {
            "freq_hz": float(self.freq_hz),
            "sample_rate_hz": float(self.sample_rate_hz),
            "bw_3db_hz": float(self.bw_3db_hz),
            "bw_30db_hz": float(self.bw_30db_hz),
            "peak_count": int(self.peak_count),
            "duty": str(self.duty),
            "shape": str(self.shape),
            "snr_db": float(self.snr_db),
        }


@dataclass
class SignatureMatch:
    """A catalog-entry fit. Confidence ∈ [0, 1]."""
    name: str
    confidence: float
    catalog_entry: dict
    features: FingerprintFeatures

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "confidence": float(self.confidence),
            "catalog_entry": dict(self.catalog_entry),
            "features": self.features.to_dict(),
        }


# Module-level cached catalog. Loaded on first use; reload via load_catalog().
_CATALOG_CACHE: Optional[list] = None
_CATALOG_PATH_CACHE: Optional[str] = None


def load_catalog(path: Optional[str] = None) -> list:
    """Load the YAML catalog, cached by path. Returns [] on any failure."""
    global _CATALOG_CACHE, _CATALOG_PATH_CACHE
    target = path or DEFAULT_CATALOG_PATH
    if _CATALOG_CACHE is not None and _CATALOG_PATH_CACHE == target:
        return _CATALOG_CACHE
    if not _YAML_AVAILABLE or not os.path.isfile(target):
        _CATALOG_CACHE = []
        _CATALOG_PATH_CACHE = target
        return []
    try:
        with open(target) as f:
            data = yaml.safe_load(f) or []
        if isinstance(data, dict):
            data = data.get("signatures") or data.get("entries") or []
        if not isinstance(data, list):
            data = []
        _CATALOG_CACHE = data
        _CATALOG_PATH_CACHE = target
        return data
    except Exception:
        _CATALOG_CACHE = []
        _CATALOG_PATH_CACHE = target
        return []


def reset_catalog_cache() -> None:
    """Test hook: clear the cached catalog so the next load() re-reads disk."""
    global _CATALOG_CACHE, _CATALOG_PATH_CACHE
    _CATALOG_CACHE = None
    _CATALOG_PATH_CACHE = None


def _power_spectrum_db(iq: np.ndarray, n_segments: int = 8) -> np.ndarray:
    """Welch-style averaged periodogram in dB. DC-centered (fftshift).

    Splits the IQ into ``n_segments`` overlapping segments, FFTs each with a
    Hanning window, and averages the magnitudes. A plain single-shot FFT puts
    the bulk of an FM signal's energy into whichever bin happens to hold the
    instantaneous frequency at the snapshot moment, which makes the
    bandwidth-at-drop measurement collapse to a few bins. Averaging across
    segments smears the FM trajectory across its actual deviation range and
    recovers a realistic occupied bandwidth.
    """
    n = iq.size
    if n == 0:
        return np.zeros(0)
    if n_segments < 1 or n < n_segments * 4:
        seg_len = n
        n_segments = 1
    else:
        seg_len = n // n_segments
    if seg_len % 2:
        seg_len -= 1
    if seg_len < 4:
        seg_len = n
        n_segments = 1
    window = np.hanning(seg_len)
    accum = np.zeros(seg_len, dtype=np.float64)
    step = max(1, seg_len // 2)  # 50% overlap when n_segments > 1
    starts = list(range(0, max(1, n - seg_len + 1), step))
    if not starts:
        starts = [0]
    for s in starts:
        seg = iq[s:s + seg_len]
        if seg.size < seg_len:
            break
        spec = np.fft.fftshift(np.fft.fft(seg * window))
        accum += np.abs(spec) ** 2
    accum /= max(1, len(starts))
    return 10.0 * np.log10(accum + 1e-20)


def _bandwidth_at_drop_hz(spec_db: np.ndarray, sample_rate_hz: float, drop_db: float) -> float:
    """Bandwidth at which the spectrum drops `drop_db` from its peak.

    Uses the span between the leftmost and rightmost bins that remain within
    ``drop_db`` of the peak. The previous walk-outward-from-peak heuristic
    collapsed on jagged spectra (modulated signals, OFDM-like multi-tone)
    because a single below-threshold notch near the peak would stop the
    walk early. Taking the full above-threshold span captures the real
    occupied bandwidth and is more robust to ripple.
    """
    if spec_db.size == 0:
        return 0.0
    peak_val = float(np.max(spec_db))
    threshold = peak_val - drop_db
    above = np.where(spec_db >= threshold)[0]
    if above.size == 0:
        return 0.0
    bin_hz = sample_rate_hz / spec_db.size
    return float((int(above[-1]) - int(above[0])) * bin_hz)


def _count_peaks(spec_db: np.ndarray, above_floor_db: float = 10.0, min_separation_bins: int = 4) -> int:
    """Count distinct spectral peaks above ``above_floor_db`` over the median.

    Used to discriminate single-carrier (≤ 2 peaks) from OFDM/multi-carrier
    (8+ peaks). The threshold is noise-floor-relative (median + offset) so
    a clean tone whose neighbors leak via the window mainlobe still counts
    as one peak — earlier prominence-vs-immediate-neighbors logic dropped
    real tones whose hanning-windowed mainlobe was only a few dB taller
    than its ±1 bins.

    Peaks are coalesced within ``min_separation_bins`` so one tone smeared
    across 3 bins counts once.
    """
    if spec_db.size < 3:
        return 0
    floor = float(np.median(spec_db))
    threshold = floor + above_floor_db
    peaks: list[int] = []
    for i in range(1, spec_db.size - 1):
        here = float(spec_db[i])
        if here < threshold:
            continue
        if here < float(spec_db[i - 1]) or here < float(spec_db[i + 1]):
            continue
        if peaks and (i - peaks[-1]) < min_separation_bins:
            # Same peak — keep the taller bin.
            if here > float(spec_db[peaks[-1]]):
                peaks[-1] = i
            continue
        peaks.append(i)
    return len(peaks)


def _classify_duty(iq: np.ndarray, sample_rate_hz: float, window_ms: float = 5.0) -> str:
    """Classify amplitude-envelope behavior over time.

    continuous — envelope steady over windows (std/mean < 0.25)
    bursty     — long silent windows interleaved with active windows
                 (median envelope is < 0.2 × peak envelope)
    hopping    — would require multi-capture frequency-wander analysis;
                 single-slice fingerprinter can't infer this directly, so
                 we never return "hopping" here — that's left to a future
                 multi-slice aggregator.
    """
    if iq.size == 0:
        return "unknown"
    win = max(1, int(sample_rate_hz * window_ms / 1000.0))
    n_windows = max(1, iq.size // win)
    env = np.abs(iq[: n_windows * win]).reshape(n_windows, win).mean(axis=1)
    if env.size == 0:
        return "unknown"
    mean_env = float(env.mean())
    if mean_env <= 0:
        return "unknown"
    rel_std = float(env.std() / mean_env)
    median_to_peak = float(np.median(env) / (env.max() + 1e-20))
    if median_to_peak < 0.2:
        return "bursty"
    if rel_std < 0.25:
        return "continuous"
    return "bursty"


def _classify_shape(spec_db: np.ndarray, peak_count: int, bw_3db_hz: float, bw_30db_hz: float) -> str:
    """Coarse spectral-shape category.

    Order matters: bandwidth is checked before peak count, because FM
    modulation produces many Bessel-sideband peaks that the peak counter
    would otherwise misread as OFDM. A truly narrow signal can only have a
    small ``bw_30db_hz`` regardless of how many peaks the FFT shows inside
    its envelope.
    """
    if spec_db.size == 0:
        return "unknown"
    # A signal that fits inside ~30 kHz is structurally a narrow carrier no
    # matter how spiky the FM sideband structure looks.
    if 0 < bw_30db_hz < 30000:
        return "narrow_carrier"
    # Multi-tone signal that spreads over significant bandwidth → OFDM-like.
    if peak_count >= 8 and bw_30db_hz > 100000:
        return "ofdm_multicarrier"
    if peak_count == 2 and bw_30db_hz > 0:
        ratio = bw_3db_hz / (bw_30db_hz + 1.0)
        if 0.1 < ratio < 0.9:
            return "fsk_two_tone"
    if bw_30db_hz > 0 and bw_3db_hz / bw_30db_hz > 0.6:
        # Flat-topped, like wide FM broadcast or ATSC 8VSB.
        return "wide_flat"
    if peak_count <= 2 and bw_3db_hz < 50000:
        return "narrow_carrier"
    # Very wide, low-peak count → likely broadband noise floor.
    if peak_count <= 1 and bw_30db_hz > 500000:
        return "broadband_noise"
    return "unknown"


def measure_features(
    iq: np.ndarray,
    sample_rate_hz: float,
    freq_hz: float,
    snr_db: float = 0.0,
) -> FingerprintFeatures:
    """Compute the spectral + temporal feature set for one IQ slice."""
    if iq.dtype != np.complex64 and iq.dtype != np.complex128:
        # Accept interleaved float32 as a back-compat convenience.
        try:
            iq = iq.astype(np.complex64)
        except Exception:
            iq = np.asarray(iq, dtype=np.complex64)
    spec_db = _power_spectrum_db(iq)
    bw_3db_hz = _bandwidth_at_drop_hz(spec_db, sample_rate_hz, drop_db=3.0)
    bw_30db_hz = _bandwidth_at_drop_hz(spec_db, sample_rate_hz, drop_db=30.0)
    peak_count = _count_peaks(spec_db)
    duty = _classify_duty(iq, sample_rate_hz)
    shape = _classify_shape(spec_db, peak_count, bw_3db_hz, bw_30db_hz)
    return FingerprintFeatures(
        freq_hz=freq_hz,
        sample_rate_hz=sample_rate_hz,
        bw_3db_hz=bw_3db_hz,
        bw_30db_hz=bw_30db_hz,
        peak_count=peak_count,
        duty=duty,
        shape=shape,
        snr_db=snr_db,
    )


def _score_entry(features: FingerprintFeatures, entry: dict) -> float:
    """Score a catalog entry against measured features. Returns [0, 1].

    The score is a weighted sum:
      0.40  bandwidth fit (3 dB and 30 dB both within range)
      0.20  shape match (exact category)
      0.10  duty match (exact category)
      0.20  freq fit (within freq_min/freq_max — checked by caller; here
                     it's a gate so we don't reach this fn if it fails)
      0.10  entry-provided confidence_weight (operator's hint that this
            entry is more/less specific)
    """
    # Bandwidth fit
    bw_score = 0.0
    bw_min = float(entry.get("bandwidth_3db_hz_min") or 0.0)
    bw_max = float(entry.get("bandwidth_3db_hz_max") or 0.0)
    if bw_min > 0 and bw_max > 0:
        if bw_min <= features.bw_3db_hz <= bw_max:
            bw_score = 1.0
        else:
            # Soft penalty: fade linearly out to 2× the range.
            mid = (bw_min + bw_max) / 2.0
            spread = max(1.0, (bw_max - bw_min))
            distance = abs(features.bw_3db_hz - mid) / spread
            bw_score = max(0.0, 1.0 - distance)

    # Shape match
    shape_score = 1.0 if (entry.get("shape") or "") == features.shape else 0.0

    # Duty match
    expected_duty = (entry.get("duty_cycle") or "").lower()
    if expected_duty in ("continuous", "bursty", "hopping"):
        duty_score = 1.0 if expected_duty == features.duty else 0.0
    else:
        duty_score = 0.5  # unspecified — neutral

    # Operator confidence weight (default 1.0).
    cw = float(entry.get("confidence_weight") or 1.0)
    cw = max(0.0, min(1.0, cw))

    # Freq fit is already guaranteed (1.0) when caller filtered the catalog.
    freq_score = 1.0

    return (
        0.40 * bw_score
        + 0.20 * shape_score
        + 0.10 * duty_score
        + 0.20 * freq_score
        + 0.10 * cw
    )


def match_signature(
    iq: np.ndarray,
    sample_rate_hz: float,
    freq_hz: float,
    *,
    snr_db: float = 0.0,
    catalog_path: Optional[str] = None,
    min_confidence: float = 0.65,
) -> Optional[SignatureMatch]:
    """Top-level API: measure features, score the catalog, return best hit.

    Returns None when:
      - catalog is missing or empty
      - no catalog entry brackets `freq_hz`
      - best score < min_confidence

    Otherwise returns the highest-scoring SignatureMatch.
    """
    catalog = load_catalog(catalog_path)
    if not catalog:
        return None

    candidates = []
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        f_min = float(entry.get("freq_min_hz") or 0.0)
        f_max = float(entry.get("freq_max_hz") or 0.0)
        if f_min <= 0 or f_max <= 0 or f_min > f_max:
            continue
        if not (f_min <= freq_hz <= f_max):
            continue
        candidates.append(entry)

    if not candidates:
        return None

    features = measure_features(iq, sample_rate_hz, freq_hz, snr_db=snr_db)
    best: Optional[tuple[float, dict]] = None
    for entry in candidates:
        score = _score_entry(features, entry)
        if best is None or score > best[0]:
            best = (score, entry)

    if best is None or best[0] < min_confidence:
        return None
    score, entry = best
    return SignatureMatch(
        name=str(entry.get("name") or "unknown"),
        confidence=float(score),
        catalog_entry=entry,
        features=features,
    )
