"""Disco — trust-hierarchy identification result.

Each detection runs through a fixed fall-through chain of identification
layers, ordered from "most curated" to "raw ML guess". The FIRST layer that
returns a non-empty result wins; the result is wrapped in an
IdentificationResult with a confidence tier so the UI can display only
the highest-trust signal and bury the rest behind a details toggle.

Goal (Phase ID-A): the dashboard stops showing conjecture as if it were
identification. A tag like "[TV] AM_VOICE" should display as
"⚪ Signal in TV_VHF_HIGH — unidentified" by default, with the raw ML
class kept in the details panel for retrain-set curation.

Layer order (first hit wins):
  A   HPDB exact-freq match           → confidence=high   source=hpdb
  B   CDBS exact-freq match           → confidence=high   source=cdbs
  B2  rtl_433 device decode (ISM)     → confidence=high   source=rtl_433
  B3  spectral signature match        → confidence=high/medium source=signature
  C   ULS amateur band fallback hit   → confidence=medium source=uls (amateur)
  D   ULS land-mobile licensee ≤80 km → confidence=medium source=uls
  E   ML class IN band allowed_modes  → confidence=low    source=modulation_class
  F   ML class REJECTED by band-plan  → confidence=spurious source=band_plan
  G   NOISE / very low SNR            → confidence=spurious source=modulation_class

The `evidence` dict carries the raw lookup payloads and the measured
ML features so the details panel and Claude prompts (when invoked) can
render the supporting data without re-running the lookups.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# Confidence tiers. Strings (not Enum) for trivial JSON serialization +
# direct rendering in dashboard CSS class names.
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_UNKNOWN = "unknown"
CONFIDENCE_SPURIOUS = "spurious"

_VALID_CONFIDENCE = {
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
    CONFIDENCE_UNKNOWN,
    CONFIDENCE_SPURIOUS,
}

# Sources. Same string-not-enum reasoning. "unknown" reserved for when no
# layer fires AND we don't even have a band tag.
SOURCE_HPDB = "hpdb"
SOURCE_CDBS = "cdbs"
SOURCE_RTL433 = "rtl_433"           # PR #30 — rtl_433 device decode (ISM bands)
SOURCE_ULS = "uls"
SOURCE_SIGNATURE = "signature"     # reserved for PR B fingerprinter
SOURCE_BAND_PLAN = "band_plan"
SOURCE_MODULATION_CLASS = "modulation_class"
SOURCE_UNKNOWN = "unknown"

_VALID_SOURCE = {
    SOURCE_HPDB,
    SOURCE_CDBS,
    SOURCE_RTL433,
    SOURCE_ULS,
    SOURCE_SIGNATURE,
    SOURCE_BAND_PLAN,
    SOURCE_MODULATION_CLASS,
    SOURCE_UNKNOWN,
}

# Below this SNR, even a confident ML class is downgraded to spurious.
# Empirically: 8 dB is where modulation classification accuracy collapses
# on the radioml v3.6 ONNX model. Configurable for future tuning.
SNR_FLOOR_DB = 8.0


@dataclass
class IdentificationResult:
    """Structured identification of a single radio detection.

    Fields:
        service       Human-readable label (e.g. "WSMV-TV" or "P25 control"). None
                      when no layer could name the signal. Distinct from the raw
                      ML class which lives in `evidence["modulation_class"]`.
        confidence    One of {high, medium, low, unknown, spurious}. Drives both
                      the UI tier badge and whether Claude is invoked downstream
                      (Claude only runs for high/medium with a curated DB source).
        source        Which fall-through layer produced this result.
        band_name     Band-plan name (e.g. "TV_VHF_HIGH") if the freq is in any
                      band. Always populated when band-plan is loaded, even when
                      `source` is something else. Used by interpret.py and the UI.
        evidence      Dict of raw inputs the layers considered. Always includes
                      at minimum: modulation_class, modulation_confidence, snr_db,
                      band_rejected. Layer-specific keys when present:
                      hpdb_match, cdbs_match, uls_match.
    """
    service: Optional[str]
    confidence: str
    source: str
    band_name: Optional[str] = None
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.confidence not in _VALID_CONFIDENCE:
            raise ValueError(
                f"invalid confidence {self.confidence!r}; "
                f"must be one of {sorted(_VALID_CONFIDENCE)}"
            )
        if self.source not in _VALID_SOURCE:
            raise ValueError(
                f"invalid source {self.source!r}; "
                f"must be one of {sorted(_VALID_SOURCE)}"
            )

    @property
    def should_invoke_claude(self) -> bool:
        """Gate for interpret.py: only run Claude when we have a high/medium
        result from a curated DB. Saves cost on spurious + unknown rows where
        prose adds nothing the structured fields don't already cover.
        """
        return (
            self.confidence in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM)
            and self.source in (SOURCE_HPDB, SOURCE_CDBS, SOURCE_SIGNATURE)
        )

    @property
    def is_displayed_by_default(self) -> bool:
        """Spurious rows hide by default in the dashboard."""
        return self.confidence != CONFIDENCE_SPURIOUS

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "confidence": self.confidence,
            "source": self.source,
            "band_name": self.band_name,
            "evidence": dict(self.evidence),
        }


def _format_hpdb_service(match: dict) -> str:
    """Pick the most informative label out of an HPDB row.

    Trunked control channels carry "system — site" in alpha_tag already
    (constructed in disco/src/hpdb.py); conventional rows have a short
    alpha_tag plus a group_name for context.
    """
    alpha = (match.get("alpha_tag") or "").strip()
    if match.get("source_table") == "trunk_control":
        # "<system> — <site>" already formatted by hpdb.py
        return alpha or (match.get("system_name") or "Trunked system")
    group = (match.get("group_name") or "").strip()
    if alpha and group and group != alpha:
        return f"{group} — {alpha}"
    return alpha or group or "Conventional channel"


def _format_cdbs_service(match: dict) -> str:
    """Render a CDBS station as a single human-readable label.

    Bug-3 fix (post-#27 live data): the CDBS loader synthesizes
    ``entity_name`` as ``"<callsign> (<community>)"`` already (see
    disco/src/cdbs_load.py), so naive ``f"{cs} ({name})"`` was producing
    ``"WHHM-FM (WHHM-FM (HENDERSON, TN))"``. Detect the embedded
    callsign and avoid double-printing.
    """
    cs = (match.get("callsign") or "").strip()
    name = (match.get("entity_name") or "").strip()
    if name and cs and name.startswith(cs):
        # entity_name already begins with the callsign — render it as-is.
        return name
    if cs and name:
        return f"{cs} ({name})"
    return cs or name or "Broadcast station"


def _format_uls_service(match: dict) -> str:
    cs = (match.get("callsign") or "").strip()
    name = (match.get("entity_name") or "").strip()
    src = (match.get("source") or "").strip()
    if "amateur" in src.lower():
        return f"Amateur — {cs}" if cs else f"Amateur licensee ({src})"
    if cs and name:
        return f"{name} ({cs})"
    return name or cs or "FCC licensee"


def build_identification(
    *,
    modulation_class: Optional[str],
    modulation_confidence: Optional[float],
    snr_db: Optional[float],
    band_name: Optional[str] = None,
    band_rejected: bool = False,
    band_allowed_modes: Optional[list] = None,
    hpdb_match: Optional[dict] = None,
    cdbs_match: Optional[dict] = None,
    rtl433_match: Optional[dict] = None,
    uls_match: Optional[dict] = None,
    signature_match: Optional[dict] = None,
) -> IdentificationResult:
    """Run the fall-through and return the highest-trust result.

    Pre-conditions:
      - hpdb/cdbs/uls match dicts are the top-1 row from each lookup
        (already location- and freq-filtered by the classifier loop)
      - band_name + band_rejected come from band_plan.tag_for() / classify
      - modulation_class is the raw ML output (NOISE / FM_NARROW / QAM / ...)
      - modulation_confidence is the ONNX softmax score for that class
      - snr_db is the freshly-measured signal-to-noise of the slice

    Layer-by-layer behavior is documented at module top.
    """
    evidence = {
        "modulation_class": modulation_class,
        "modulation_confidence": modulation_confidence,
        "snr_db": snr_db,
        "band_rejected": bool(band_rejected),
        "band_allowed_modes": list(band_allowed_modes or []),
    }

    # Layer A: HPDB exact-freq match. Already curated and location-filtered.
    if hpdb_match:
        evidence["hpdb_match"] = dict(hpdb_match)
        return IdentificationResult(
            service=_format_hpdb_service(hpdb_match),
            confidence=CONFIDENCE_HIGH,
            source=SOURCE_HPDB,
            band_name=band_name,
            evidence=evidence,
        )

    # Layer B: CDBS exact-freq broadcast station match.
    if cdbs_match:
        evidence["cdbs_match"] = dict(cdbs_match)
        return IdentificationResult(
            service=_format_cdbs_service(cdbs_match),
            confidence=CONFIDENCE_HIGH,
            source=SOURCE_CDBS,
            band_name=band_name,
            evidence=evidence,
        )

    # Layer B-rtl433 (PR #30): rtl_433 device decode. When the slice replays
    # cleanly through rtl_433 and a device packet decodes, that's a
    # definitive protocol-level identification (a named weather sensor, TPMS,
    # doorbell, …) — strictly better than the generic spectral fingerprint,
    # so it sits ABOVE the signature layer and below the curated DBs
    # (HPDB/CDBS). High tier. Only ever populated for ISM-band detections
    # where HPDB+CDBS already missed (the classifier gates the lookup).
    if rtl433_match:
        evidence["rtl433_match"] = dict(rtl433_match)
        return IdentificationResult(
            service=str(rtl433_match.get("device_name") or "rtl_433 device"),
            confidence=CONFIDENCE_HIGH,
            source=SOURCE_RTL433,
            band_name=band_name,
            evidence=evidence,
        )

    # Layer B-prime: spectral signature match (PR B). Confidence comes from
    # the fingerprinter's score against the curated catalog. >= 0.85 is
    # high tier (clean WiFi / FM-broadcast / NOAA-WX / etc. fit); 0.65–0.85
    # downgrades to medium. Below 0.65 the fingerprinter returns None and
    # this layer doesn't fire.
    if signature_match:
        evidence["signature_match"] = dict(signature_match)
        sig_conf = float(signature_match.get("confidence") or 0.0)
        tier = CONFIDENCE_HIGH if sig_conf >= 0.85 else CONFIDENCE_MEDIUM
        return IdentificationResult(
            service=str(signature_match.get("name") or "Signature match"),
            confidence=tier,
            source=SOURCE_SIGNATURE,
            band_name=band_name,
            evidence=evidence,
        )

    # Layer C: ULS amateur-band fallback (source string contains "amateur").
    # Distinguished from land-mobile because amateur licenses are band-specific
    # and contextually useful, not licensee-by-licensee guesses.
    if uls_match:
        evidence["uls_match"] = dict(uls_match)
        uls_src = (uls_match.get("source") or "").lower()
        if "amateur" in uls_src:
            return IdentificationResult(
                service=_format_uls_service(uls_match),
                confidence=CONFIDENCE_MEDIUM,
                source=SOURCE_ULS,
                band_name=band_name,
                evidence=evidence,
            )

        # Layer D: ULS land-mobile licensee within distance radius. The
        # license names WHO has a license at this freq, not WHAT this
        # particular signal is — medium confidence by design.
        return IdentificationResult(
            service=_format_uls_service(uls_match),
            confidence=CONFIDENCE_MEDIUM,
            source=SOURCE_ULS,
            band_name=band_name,
            evidence=evidence,
        )

    # Layer F: band-rejected ML class is structurally spurious — only when
    # the ML class is a real one. Bug-2 fix (post-#27 live data): the prior
    # version landed every band-rejected row in the spurious bucket, but
    # "unclassified" / None classes aren't in any band's allowed_modes by
    # construction, so unclassified-in-real-band rows were being treated
    # identically to band-rejected real classes. They aren't the same:
    # "FM_BROADCAST in CELL_850_DL" is structurally spurious (classifier
    # output contradicts allocation); "unclassified in CELL_850_DL" is just
    # an unknown signal in a real band. Drop the latter to the unknown
    # bucket below so the dashboard surfaces it instead of hiding it.
    if (band_name and band_rejected
            and modulation_class
            and modulation_class not in ("unclassified", "NOISE")):
        return IdentificationResult(
            service=None,
            confidence=CONFIDENCE_SPURIOUS,
            source=SOURCE_BAND_PLAN,
            band_name=band_name,
            evidence=evidence,
        )

    # Layer G: NOISE class or sub-floor SNR — almost always not a real signal
    # worth surfacing. Spurious tier hides these by default.
    if modulation_class == "NOISE":
        return IdentificationResult(
            service=None,
            confidence=CONFIDENCE_SPURIOUS,
            source=SOURCE_MODULATION_CLASS,
            band_name=band_name,
            evidence=evidence,
        )
    if snr_db is not None and snr_db < SNR_FLOOR_DB:
        return IdentificationResult(
            service=None,
            confidence=CONFIDENCE_SPURIOUS,
            source=SOURCE_MODULATION_CLASS,
            band_name=band_name,
            evidence=evidence,
        )

    # Layer E: ML class is in band's allowed_modes (or no band — permissive
    # default). Low confidence — the modulation class is supporting info,
    # not an identification on its own.
    if modulation_class and modulation_class != "unclassified":
        return IdentificationResult(
            service=None,
            confidence=CONFIDENCE_LOW,
            source=SOURCE_MODULATION_CLASS,
            band_name=band_name,
            evidence=evidence,
        )

    # Fallthrough: signal exists but no curated label, no useful ML class,
    # SNR above floor. This catches modulation_class == "unclassified" or
    # None — including the case where the signal is in a real band but
    # the classifier didn't recognize the modulation. Tier=unknown, NOT
    # spurious — the dashboard should still surface "Unknown signal in
    # <band>" for these rows so Will can see something is there.
    return IdentificationResult(
        service=None,
        confidence=CONFIDENCE_UNKNOWN,
        source=SOURCE_UNKNOWN,
        band_name=band_name,
        evidence=evidence,
    )
