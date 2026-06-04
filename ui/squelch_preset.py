"""SB5 squelch preset engine (Phase 1).

Replaces the per-band squelch dBFS slider with a 3-state preset
(Sensitive / Balanced / Selective).  For each preset, we compute
a per-channel squelch_threshold dBFS list from the live noise floor
reported by rtl-airband stats:

    threshold[i]  =  noise_floor_dbfs[i]  +  preset_margin_db

The list is written into the per-band rtl_airband profile via the
``squelch_threshold = ( v1, v2, ... );`` list form that rtl_airband
v5.1+ supports.  Phase 2 will close the loop and re-sample on a
schedule; Phase 1 sets it ONCE at the moment of preset selection,
which is enough to unbreak the structural "channel_squelch_counter
never increments" diagnostic.

Margins are in dB above the per-channel noise floor:

    Sensitive  =>  3 dB
    Balanced   =>  6 dB  (default)
    Selective  => 12 dB

Diagnostic context: with the legacy per-band scalar squelch_threshold,
Air was sitting at -20 dBFS against a measured noise floor of -74 dBFS
(54 dB above noise — completely shut), and Ground was at -32 against
noise spread -39..-47 dBFS (13 dB above the worst channel; never
opens).  channel_squelch_counter == 0 across a 4h+21min audit.

Storage: the chosen preset, the margin, the median noise floor at
selection time, and the computed median threshold are persisted into
the existing managed_analog_controls.json under each target's
``override`` block (keys: ``squelch_preset``,
``squelch_preset_margin_db``,
``squelch_preset_noise_floor_dbfs``,
``squelch_preset_computed_at_ms``).  Old records without a preset
field migrate to "balanced" on first load — handled in
``managed_analog_controls._controls_payload``.
"""
from __future__ import annotations

import logging
import os
import re
import statistics
import time
from typing import Iterable

logger = logging.getLogger(__name__)


# ---- preset definitions -----------------------------------------------------

PRESET_MARGINS_DB: dict[str, float] = {
    "sensitive": 3.0,
    "balanced":  6.0,
    "selective": 12.0,
}
DEFAULT_PRESET = "balanced"
VALID_PRESETS = tuple(PRESET_MARGINS_DB.keys())


def normalize_preset(value):
    """Return a known preset name, defaulting to balanced."""
    token = str(value or "").strip().lower()
    if token in PRESET_MARGINS_DB:
        return token
    return DEFAULT_PRESET


def margin_for(preset: str) -> float:
    return float(PRESET_MARGINS_DB[normalize_preset(preset)])


# ---- poison noise-floor rejection (shared with squelch_tracker) -------------
#
# rtl-airband's noise estimator returns its init constant (linear 0.283 →
# ~-10.964 dBFS) before audio samples buffer post-restart.  Operator-driven
# preset chip clicks that fire seconds after a service restart would read
# that init constant and compute threshold ~= noise + margin ~= 0 dBFS
# (clamped to the _THRESHOLD_CEILING of -1 dBFS) — silencing the band.
#
# Same env vars the tracker uses so one knob controls both paths.

POISON_CEILING_AM_DBFS: float = float(
    os.getenv("SQUELCH_TRACKER_POISON_CEILING_AM_DBFS", "-55.0")
)
POISON_CEILING_NFM_DBFS: float = float(
    os.getenv("SQUELCH_TRACKER_POISON_CEILING_NFM_DBFS", "-20.0")
)


def poison_ceiling_for_band(band: str) -> float:
    """Per-band noise-floor ceiling above which a sample is "poison"."""
    b = _normalize_target(band)
    return POISON_CEILING_AM_DBFS if b == "airband" else POISON_CEILING_NFM_DBFS


# ---- target -> stats / profile resolution -----------------------------------

def _normalize_target(target: str) -> str:
    token = str(target or "").strip().lower()
    if token in ("air", "airband"):
        return "airband"
    if token in ("ground", "gnd"):
        return "ground"
    raise ValueError(f"unknown target: {target!r}")


def stats_path_for(target: str) -> str:
    """rtl-airband per-service stats file path."""
    t = _normalize_target(target)
    return f"/run/rtl_airband_{t}_stats.txt"


# ---- stats file parsing -----------------------------------------------------

# Matches a Prometheus-style metric line written by rtl-airband:
#   channel_dbfs_noise_level{freq="121.025",label="..."}<TAB>-74.346
_RE_NOISE_LINE = re.compile(
    r'^channel_dbfs_noise_level\{[^}]*freq="([0-9.]+)"[^}]*\}\s+(-?\d+(?:\.\d+)?)\s*$'
)


def parse_noise_floor_dbfs(stats_text: str) -> dict:
    """Return {freq_mhz_str: noise_dbfs} from rtl-airband stats text.

    Keys are normalized to the same float-string formatting rtl-airband
    writes (e.g. "121.025", "132.500") so callers can look them up by
    formatting their config freqs the same way.
    """
    out = {}
    for raw in (stats_text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("channel_dbfs_noise_level"):
            continue
        m = _RE_NOISE_LINE.match(line)
        if not m:
            continue
        freq_key = m.group(1)
        try:
            value = float(m.group(2))
        except ValueError:
            continue
        out[freq_key] = value
    return out


def read_noise_floor_for_target(target: str) -> dict:
    """Load and parse the stats file for a target.

    Returns {} if the stats file is missing or empty — callers must
    handle the empty case (and refuse to write a config that would be
    indistinguishable from the legacy broken state).
    """
    path = stats_path_for(target)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except FileNotFoundError:
        logger.warning("squelch_preset: stats file missing: %s", path)
        return {}
    except OSError as exc:
        logger.warning("squelch_preset: stats read failed %s: %s", path, exc)
        return {}
    return parse_noise_floor_dbfs(text)


# ---- profile freqs extraction ----------------------------------------------

# rtl-airband config uses libconfig syntax.  The ``freqs = ( ... );`` block
# can span many lines, with each entry a float in MHz.  We grab the entire
# parenthesized block then split values from it.  This is intentionally
# minimal — the profile generator owns the canonical structure; we only
# need to read freqs in the same order rtl-airband will load them so the
# per-channel threshold list aligns 1:1 with the freqs list.
_RE_FREQS_BLOCK = re.compile(
    r'(?ms)^\s*freqs\s*=\s*\((.*?)\)\s*;\s*$'
)
_RE_FREQ_VALUE = re.compile(r'-?\d+(?:\.\d+)?')


def parse_profile_freqs(conf_text: str) -> list:
    """Return the ordered freqs list (MHz) from a profile's first
    channel block.  Empty list if the block is missing or unparseable.
    """
    m = _RE_FREQS_BLOCK.search(conf_text or "")
    if not m:
        return []
    out = []
    for token in _RE_FREQ_VALUE.findall(m.group(1)):
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def read_profile_freqs(conf_path: str) -> list:
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            return parse_profile_freqs(f.read())
    except FileNotFoundError:
        return []


def _freq_key(freq_mhz: float) -> str:
    """Format a freq the same way rtl-airband stats does.

    rtl-airband renders 121.025 as "121.025", 132.5 as "132.500".  The
    rule is "three decimal places, trailing zeros preserved", which is
    what ``%.3f`` produces; the only caveat is leading zeros.  This
    matches what we observed in /run/rtl_airband_*_stats.txt today.
    """
    return f"{float(freq_mhz):.3f}"


# ---- threshold computation --------------------------------------------------

# Floor for the computed per-channel threshold.  rtl-airband treats
# squelch_threshold = 0 as "always-on / no gate"; writing 0 by accident
# would re-create the legacy bug from the other direction.  We clamp
# any computed value to <= -1 dBFS.
_THRESHOLD_CEILING = -1
# Hard lower bound — anything below -100 is below what an 8-bit/16-bit
# ADC can measure meaningfully; very negative thresholds are signal
# that the noise reader returned a sentinel.
_THRESHOLD_FLOOR = -100


def _clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def compute_threshold_list(
    freqs,
    noise_map,
    margin_db,
    fallback_noise_dbfs=None,
):
    """Return (thresholds_int, noise_used_float) for the given freqs.

    For each freq, look up the stats noise floor by formatted key; if
    missing fall back to the median of available noise samples (or to
    ``fallback_noise_dbfs`` when nothing is available).  Threshold is
    ``round(noise + margin)``, clamped to [-100, -1] dBFS.
    """
    freqs_list = list(freqs)
    samples = [v for v in noise_map.values() if isinstance(v, (int, float))]
    if samples:
        median_noise = statistics.median(samples)
    elif fallback_noise_dbfs is not None:
        median_noise = float(fallback_noise_dbfs)
    else:
        median_noise = -60.0

    thresholds = []
    noise_used = []
    for f_mhz in freqs_list:
        key = _freq_key(f_mhz)
        noise = noise_map.get(key)
        if noise is None:
            noise = median_noise
        raw = float(noise) + float(margin_db)
        clamped = _clamp(raw, _THRESHOLD_FLOOR, _THRESHOLD_CEILING)
        thresholds.append(int(round(clamped)))
        noise_used.append(float(noise))
    return thresholds, noise_used


# ---- per-channel squelch_threshold list writer ------------------------------

# Match squelch_threshold = <scalar>;  on a single line.
# Match squelch_threshold = ( v1, v2, ... );  spanning one or many
# lines.  We keep these two regexes SEPARATE because the original
# (single, lazy [^)]*) form swallowed the squelch_delay line AND the
# entire outputs:(...); block that followed — the first unbalanced
# `)` came from the outputs block, not the squelch list.  Using
# `[^()]*` on the list form guarantees the regex stops at the
# closing `)` of the squelch list itself (which contains only
# numbers, commas, comments and whitespace — no inner parens).
_RE_SQUELCH_SCALAR = re.compile(
    r'(?m)^([ \t]*)squelch_threshold\s*=\s*-?\d+(?:\.\d+)?\s*;[^\n]*\n'
)
_RE_SQUELCH_LIST = re.compile(
    r'(?ms)^([ \t]*)squelch_threshold\s*=\s*\([^()]*\)\s*;[^\n]*\n'
)
# Convenience: yield (indent, span_start, span_end) for every
# squelch_threshold line in either form.  Order by position.
def _find_squelch_lines(text):
    spans = []
    for rx in (_RE_SQUELCH_SCALAR, _RE_SQUELCH_LIST):
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), m.group(1)))
    spans.sort()
    return spans


def _format_list_line(indent: str, thresholds) -> str:
    """Format the canonical multi-line list rtl-airband will read."""
    if not thresholds:
        return f"{indent}squelch_threshold = -50;  # UI_CONTROLLED_PRESET (empty)\n"
    inner_indent = indent + "  "
    values = ",\n".join(f"{inner_indent}{v}" for v in thresholds)
    return (
        f"{indent}squelch_threshold = (  # UI_CONTROLLED_PRESET\n"
        f"{values}\n"
        f"{indent});\n"
    )


def read_current_thresholds(conf_path: str) -> list:
    """Return the on-disk squelch_threshold list as ints.

    Used by the Phase 2 tracker to compute hysteresis vs the values
    rtl-airband is currently running against, without having to keep
    its own shadow copy.  Returns the FIRST squelch_threshold block
    (whether scalar or list form) because the Phase 1 writer collapses
    all blocks into a single list and channels share one block in our
    profile template.  An empty list signals "no readable threshold"
    and callers should treat that as "always apply" (no hysteresis).
    """
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except (FileNotFoundError, OSError):
        return []
    spans = _find_squelch_lines(text)
    if not spans:
        return []
    s, e, _ind = spans[0]
    block = text[s:e]
    # Strip the leading "squelch_threshold =" and the trailing ";"
    # We then accept either a single scalar or a parenthesised list.
    m_list = re.search(r'\(([^()]*)\)', block)
    raw_values: list[str]
    if m_list:
        raw_values = re.findall(r'-?\d+(?:\.\d+)?', m_list.group(1))
    else:
        m_scalar = re.search(r'=\s*(-?\d+(?:\.\d+)?)\s*;', block)
        raw_values = [m_scalar.group(1)] if m_scalar else []
    out: list[int] = []
    for tok in raw_values:
        try:
            out.append(int(round(float(tok))))
        except ValueError:
            continue
    return out


def write_per_channel_squelch_list(conf_path: str, thresholds) -> bool:
    """Replace every squelch_threshold line in ``conf_path`` with a
    single per-channel list line.  Idempotent — returns False when the
    on-disk content already matches.  Critically, does NOT touch the
    squelch_delay line or the outputs:(...); block that follow the
    squelch_threshold line in our channel template.
    """
    if not conf_path or not os.path.isfile(conf_path):
        raise FileNotFoundError(conf_path)
    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        original = f.read()

    spans = _find_squelch_lines(original)
    if not spans:
        logger.warning(
            "squelch_preset: no squelch_threshold line found in %s; appending",
            conf_path,
        )
        indent = "      "
        new_line = _format_list_line(indent, thresholds)
        new_text = original.rstrip() + "\n" + new_line
    else:
        first_start, first_end, first_indent = spans[0]
        indent = first_indent or "      "
        new_line = _format_list_line(indent, thresholds)
        parts = []
        cursor = 0
        for i, (s, e, _ind) in enumerate(spans):
            parts.append(original[cursor:s])
            if i == 0:
                parts.append(new_line)
            cursor = e
        parts.append(original[cursor:])
        new_text = "".join(parts)

    if new_text == original:
        return False

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, conf_path)
    return True


# ---- top-level preset apply -------------------------------------------------

def compute_preset_plan(target: str, preset: str, conf_path: str) -> dict:
    """Compute the threshold list that would be written for (target, preset).

    Pure / side-effect-free.  Returns a dict suitable for both the
    apply path and the UI readout endpoint.
    """
    t = _normalize_target(target)
    p = normalize_preset(preset)
    margin = margin_for(p)

    noise_map = read_noise_floor_for_target(t)
    freqs = read_profile_freqs(conf_path)
    thresholds, noise_used = compute_threshold_list(freqs, noise_map, margin)

    noise_floor_median = float(statistics.median(noise_used)) if noise_used else 0.0
    threshold_median = int(round(statistics.median(thresholds))) if thresholds else 0

    return {
        "target": t,
        "preset": p,
        "margin_db": float(margin),
        "freqs": [float(x) for x in freqs],
        "thresholds": thresholds,
        "noise_used_dbfs": noise_used,
        "noise_floor_median": noise_floor_median,
        "threshold_median": threshold_median,
        "stats_available": bool(noise_map),
    }


def apply_preset(target: str, preset: str, conf_path: str) -> dict:
    """Compute and write the per-channel squelch list for (target, preset).

    Returns the plan dict plus ``changed`` and ``error`` fields.  Caller
    is responsible for restarting rtl-airband (typically via the
    existing /api/sitrep/action reset_radios path) — this function
    only mutates the on-disk profile.

    Poison-noise-floor rejection: if the live noise floor median is
    above the band-aware ceiling (AM=-55, NFM=-20 dBFS by default),
    REFUSE to apply.  This matches the squelch_tracker auto-apply
    gate — see f4e9eb7.  Without this, an operator chip click that
    lands during the rtl-airband post-restart warmup window reads the
    estimator init constant (~-10.964 dBFS) and writes threshold ~-1
    dBFS, silencing the band until the tracker corrects on a healthy
    cycle.
    """
    plan = compute_preset_plan(target, preset, conf_path)
    if not plan["thresholds"]:
        plan["changed"] = False
        plan["error"] = "no_freqs_in_profile"
        return plan

    # --- poison-noise-floor rejection (mirrors squelch_tracker._run_cycle)
    ceiling = poison_ceiling_for_band(plan["target"])
    noise_median = float(plan.get("noise_floor_median") or 0.0)
    stats_available = bool(plan.get("stats_available"))
    if stats_available and noise_median > ceiling:
        plan["changed"] = False
        plan["error"] = "noise_floor_not_warm"
        plan["status"] = "rejected"
        plan["reason"] = "noise_floor_median above poison ceiling"
        plan["poison_ceiling_dbfs"] = float(ceiling)
        plan["retry_after_sec"] = 30
        # Audit log: mirror the tracker's skip event so operator-driven
        # rejections appear in the same file the tracker writes to.
        try:
            try:
                from .squelch_tracker import write_audit_event
            except ImportError:
                from ui.squelch_tracker import write_audit_event  # type: ignore
            write_audit_event({
                "ts_ms": int(time.time() * 1000),
                "event": "skip",
                "source": "preset_click",
                "band": plan["target"],
                "preset": plan["preset"],
                "margin_db": plan["margin_db"],
                "freqs_count": len(plan["freqs"]),
                "noise_floor_median": noise_median,
                "poison_ceiling_dbfs": float(ceiling),
                "would_threshold_median": plan.get("threshold_median"),
                "skipped": "poison_noise_floor",
                "reason": "noise_floor_median above poison ceiling",
            })
        except Exception:
            logger.debug("squelch_preset: audit write skipped", exc_info=True)
        return plan

    # --- per-channel poison sanity: if a single channel is still showing
    # the init constant while the band median is sane, fall back to that
    # channel's prior threshold instead of writing a poison-derived value.
    # Mirrors the per-channel guard in squelch_tracker._run_cycle_for_band.
    sanitized_channels: list = []
    try:
        cur_thresholds = read_current_thresholds(conf_path)
    except Exception:
        cur_thresholds = []
    noise_used = plan.get("noise_used_dbfs") or []
    thresholds = list(plan["thresholds"])
    for i, n in enumerate(noise_used):
        try:
            n_f = float(n)
        except (TypeError, ValueError):
            continue
        if n_f <= ceiling:
            continue
        if cur_thresholds and i < len(cur_thresholds):
            fallback = int(cur_thresholds[i])
            src_label = "prior_threshold"
        else:
            fallback = -100
            src_label = "floor"
        sanitized_channels.append({
            "i": i,
            "noise_used_dbfs": n_f,
            "poisoned_threshold": int(thresholds[i]),
            "fallback": fallback,
            "fallback_source": src_label,
        })
        thresholds[i] = fallback
    plan["thresholds"] = thresholds
    plan["sanitized_channels"] = sanitized_channels
    plan["sanitized_count"] = len(sanitized_channels)
    # Recompute median for the (now sanitized) plan so persistence
    # reflects what we actually wrote.
    if thresholds:
        import statistics as _st
        plan["threshold_median"] = int(round(_st.median(thresholds)))

    try:
        changed = write_per_channel_squelch_list(conf_path, plan["thresholds"])
    except FileNotFoundError:
        plan["changed"] = False
        plan["error"] = "profile_not_found"
        return plan
    plan["changed"] = bool(changed)
    plan["error"] = ""
    plan["written_at_ms"] = int(time.time() * 1000)
    return plan
