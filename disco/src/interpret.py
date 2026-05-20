"""Disco interpret service — Claude LLM gloss for high-confidence detections.

Reads ANTHROPIC_API_KEY from /etc/disco/api_keys.conf (mode 0600).
If missing, runs in stub mode and emits 'no key configured' for all interpretations.
"""
import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import urllib.request
import urllib.error

import yaml

# Phase 4 band-plan lookup (Layer 1). Failure to import is non-fatal —
# interpret_loop falls back to a None plan and skips band-plan augmentation.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import band_plan
    _BAND_PLAN_AVAILABLE = True
except Exception as _bpe:
    band_plan = None
    _BAND_PLAN_AVAILABLE = False
    _BAND_PLAN_IMPORT_ERROR = _bpe

CONFIG_PATH = os.environ.get("DISCO_CONFIG", "/home/ubuntu/scannerproject/disco/configs/sweep.yaml")
BAND_PLAN_PATH = os.environ.get("DISCO_BAND_PLAN_PATH", "/home/ubuntu/scannerproject/disco/configs/us_band_plan.yaml")
KEY_FILE = os.environ.get("DISCO_API_KEY_FILE", "/etc/disco/api_keys.conf")
LOG = logging.getLogger("disco.interpret")
_STOP = False

# Geographic context is now sourced from SB3's HPState at prompt-construction
# time so Disco follows Travel Mode. See disco/src/current_location.py for
# the read/cache/fallback behavior.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from current_location import (
        get_current_location,
        get_location_bucket,
    )
    _LOCATION_AVAILABLE = True
except Exception as _le:  # pragma: no cover - import failure path
    get_current_location = None
    get_location_bucket = None
    _LOCATION_AVAILABLE = False
    _LOCATION_IMPORT_ERROR = _le


def _build_geographic_context() -> str:
    """Compose the geographic-context line injected into every Claude prompt.

    Reads SB3's current location at call time; falls back to a static
    Nashville string if `current_location` can't be imported (defensive —
    Disco should keep working even if the module is missing).
    """
    if not _LOCATION_AVAILABLE or get_current_location is None:
        return "Nashville, TN. Operating a multi-RSPduo SDR scanner setup."
    loc = get_current_location()
    return (
        f"User is at {loc.label} (ZIP {loc.zip}, "
        f"{loc.lat:.4f}, {loc.lon:.4f}). "
        f"Operating a multi-RSPduo SDR scanner setup."
    )

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


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


def load_api_key(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == "ANTHROPIC_API_KEY":
                        return v.strip().strip('"').strip("'")
    except Exception as e:
        LOG.warning("failed reading %s: %s", path, e)
    return None


def migrate_schema(conn):
    cur = conn.execute("PRAGMA table_info(detections)")
    cols = {r[1] for r in cur.fetchall()}
    if "interpretation" not in cols:
        conn.execute("ALTER TABLE detections ADD COLUMN interpretation TEXT")
    if "interpreted_ts" not in cols:
        conn.execute("ALTER TABLE detections ADD COLUMN interpreted_ts REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS interpretation_cache ("
        "bundle_hash TEXT PRIMARY KEY,"
        "text TEXT NOT NULL,"
        "ts REAL NOT NULL)"
    )
    conn.commit()


def hash_bundle(bundle: dict) -> str:
    s = json.dumps(bundle, sort_keys=True)
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def call_claude(api_key: str, bundle: dict, model: str, timeout: float = 20.0) -> str:
    """Call Anthropic Messages API. Returns interpretation string or stub error string."""
    if not api_key:
        return "no key configured"
    # Licensee / curated-label context — included only when the row was
    # enriched. Header differs by source (uls_source) so Claude knows which
    # database the match came from and how much to trust it:
    #   hpdb-conventional   → "Curated label match (HomePatrol)"
    #   hpdb-trunk_control  → "Curated label match (HomePatrol — trunked control)"
    #   <uls source string> → "FCC license match (ULS)"
    #   cdbs                → "Broadcast station match (CDBS)"
    #   (other)             → generic "Database match"
    licensee_block = ""
    if bundle.get("uls_callsign") or bundle.get("uls_entity_name"):
        bits = []
        if bundle.get("uls_callsign"):
            bits.append(f"  Callsign: {bundle['uls_callsign']}")
        if bundle.get("uls_entity_name"):
            bits.append(f"  Licensee/owner: {bundle['uls_entity_name']}")
        if bundle.get("uls_emission_designator"):
            bits.append(f"  Emission designator: {bundle['uls_emission_designator']}")
        if bundle.get("uls_station_class"):
            bits.append(f"  Station class: {bundle['uls_station_class']}")
        if bundle.get("uls_distance_km") is not None:
            bits.append(f"  Distance from receiver: {bundle['uls_distance_km']:.1f} km")
        src_value = (bundle.get("uls_source") or "").strip()
        if src_value:
            bits.append(f"  Database source: {src_value}")
        src_lower = src_value.lower()
        if src_lower.startswith("hpdb-trunk"):
            header = ("Curated label match (HomePatrol — trunked control channel; "
                      "indicates the system covering this site, not a per-call talkgroup)")
        elif src_lower.startswith("hpdb"):
            header = "Curated label match (HomePatrol — conventional channel)"
        elif src_lower == "cdbs":
            header = "Broadcast station match (CDBS)"
        elif src_lower:
            header = "FCC license match (ULS)"
        else:
            header = "Database match for this frequency"
        licensee_block = f"{header}:\n" + "\n".join(bits) + "\n"

    # Phase 4 band-plan context — the band allocation is treated as authoritative
    # (FCC regulatory source) and supersedes any prior knowledge the model has
    # about specific licensees at this frequency. C5 fix: previously the rejected-
    # case prose hallucinated specific licensees (MNPD, BNA approach, etc.) at
    # frequencies that don't have those services. Anomaly clause now explicitly
    # forbids licensee invention when band_rejected=true.
    band_block = ""
    band_anomaly_clause = ""
    band_name = bundle.get("band_name")
    ml_class = bundle.get("ml_class") or bundle.get("modulation_class", "?")
    band_rejected = bool(bundle.get("band_rejected"))
    if band_name:
        allowed = ", ".join(bundle.get("band_allowed_modes") or []) or "(none — protected band)"
        agreement = "REJECTED — ML class is not allowed in this band" if band_rejected else "accepted"
        ml_qualifier = "HYPOTHESIS, may be wrong" if band_rejected else "in allowed_modes for this band"
        band_block = (
            f"AUTHORITATIVE BAND ALLOCATION (per FCC 47 CFR § 2.106):\n"
            f"  This frequency is allocated to: {band_name}\n"
            f"  Allowed modulation modes for this band: {allowed}\n"
            f"  ML classifier output: {ml_class}  ({ml_qualifier})\n"
            f"  Band-plan agreement: {agreement}\n"
            f"\n"
            f"The band allocation above is from a regulatory source and supersedes any\n"
            f"prior knowledge you have about specific licensees at this frequency. Do\n"
            f"not contradict it.\n"
        )
        if band_rejected:
            band_anomaly_clause = (
                "\n**Band-plan anomaly handling** (band_rejected=true):\n\n"
                "You MUST NOT:\n"
                "  - Name specific services (e.g., \"Metro Nashville Police\", \"BNA approach\")\n"
                "  - Cite specific licensees, agencies, or call signs\n"
                "  - Claim the signal is from a service that contradicts the band allocation\n\n"
                "You MAY:\n"
                "  - Describe what services the band allocation IS for (in general)\n"
                "  - List candidate explanations for the anomaly:\n"
                "      (1) model misidentification (most common, especially low SNR or short slices)\n"
                "      (2) spurious emission from a neighboring band / harmonic\n"
                "      (3) equipment leak (cellphone amp, switching power supply, intermod)\n"
                "      (4) propagation anomaly (sporadic-E, tropospheric ducting)\n"
                "      (5) unauthorized transmitter operating outside its allocation\n"
                "  - Note that without further data (callsign, traffic pattern) the actual source is unknown\n\n"
                f"Required prose structure (3-4 sentences):\n"
                f"  1. This frequency is in {band_name}, allocated to <band's intended use>.\n"
                f"  2. The ML classifier reported {ml_class}, which is not consistent with this allocation.\n"
                f"  3. Most likely explanation: <pick one of the 5 candidates above and justify briefly>.\n"
                f"  4. (optional) Without more data the actual source cannot be confirmed.\n"
            )
    elif ml_class:
        band_block = (
            f"FCC band-plan: this frequency is outside the band-plan's covered range\n"
            f"(permissive default — no allowed_modes constraint applied). ML model said {ml_class}.\n"
        )

    prompt = f"""You are helping a user understand RF detections from their SDR scanner. Be substantive and concrete — the user is technical (running a multi-RSPduo scanner) and wants real signal-identification reasoning, not generic prose.

Geographic / user context: {_build_geographic_context()}

Detection:
- Frequency: {bundle.get('freq_mhz', 0):.4f} MHz
- Modulation class (from local CNN classifier): {ml_class}
- Classifier confidence: {bundle.get('confidence', 0):.2f}
- Bandwidth: {bundle.get('bandwidth_khz', 0):.1f} kHz
- SNR: {bundle.get('snr_db', 0):.1f} dB
- Tuner band: {bundle.get('tuner', '?')}
- Band-plan tag: {bundle.get('protocol_tag', 'none')}
{licensee_block}{band_block}
Write a 2-4 sentence interpretation. Cover, in this order:
1. **Identification**: what this signal most likely is (be specific — name the service, allocation, or system if the freq + modulation + licensee combination supports it). If the FCC match looks plausible, lead with the licensee name; otherwise reason from the frequency band plan.
2. **Context**: typical use, common activity patterns, any time-of-day / day-of-week notes if relevant. For amateur callsigns, mention the band and likely modes (e.g. 70cm typically NFM repeater, FM voice, occasional D-STAR/DMR digital).
3. **Confidence / caveats**: where you're sure vs guessing. If the licensee name doesn't match the modulation/bandwidth (e.g. ULS hit on a commercial freq but classifier says NOISE), flag it. If the freq is in an unlicensed band (ISM 902-928, broadcast 88-108) say so.{band_anomaly_clause}

Style: plain English, no bullet lists, no markdown. Short paragraphs separated by a blank line. Do not restate the freq or class in your answer (the UI shows that already). Prefer "likely" / "almost certainly" / "unclear" over hedge words. If you genuinely don't know, say "unclear from this data" and stop.

Interpretation:"""
    body = {
        "model": model,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            return data["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="ignore")[:200]
        return f"LLM HTTP {e.code}: {body_text}"
    except Exception as e:
        return f"LLM error: {e}"


def interpret_loop(cfg, conn):
    interp_cfg = cfg.get("interpret", {})
    poll_s = float(interp_cfg.get("poll_interval_s", 10))
    rate_per_min = int(interp_cfg.get("rate_per_min", 10))
    daily_budget_usd = float(interp_cfg.get("daily_budget_usd", 1.0))
    cost_per_call_cents = float(interp_cfg.get("cost_per_call_cents", 0.05))
    cache_ttl_s = float(interp_cfg.get("cache_ttl_s", 3600))
    model = interp_cfg.get("model", DEFAULT_MODEL)
    min_confidence = float(interp_cfg.get("min_confidence", 0.6))
    bin_khz = float(interp_cfg.get("dedupe_bin_khz", 25.0))
    max_per_cycle = int(interp_cfg.get("max_per_cycle", 5))

    api_key = load_api_key(KEY_FILE)
    backend = "claude" if api_key else "stub"

    # Phase 4: load FCC band plan once at startup. Failure is non-fatal —
    # bundle augmentation falls through and the prompt's band-plan section
    # is omitted. Same fallback path classifier.py uses.
    plan = None
    if _BAND_PLAN_AVAILABLE:
        try:
            plan = band_plan.load_band_plan(BAND_PLAN_PATH)
            LOG.info("band-plan loaded: %d bands from %s", len(plan), BAND_PLAN_PATH)
        except Exception as _bpe:
            LOG.warning("band-plan load failed (%s); interpretations will skip band-plan augmentation", _bpe)
    else:
        LOG.warning("band-plan import unavailable: %s", _BAND_PLAN_IMPORT_ERROR)

    LOG.info("interpret backend=%s model=%s daily_budget=$%.2f rate=%d/min min_conf=%.2f cache_ttl=%.0fs band_plan=%s",
             backend, model, daily_budget_usd, rate_per_min, min_confidence, cache_ttl_s,
             "available" if plan else "DISABLED")

    call_times = []
    daily_cents_used = 0.0
    daily_reset = time.time() + 86400
    last_key_check = time.time()
    interp_count = 0
    cache_hit_count = 0

    while not _STOP:
        if time.time() - last_key_check > 30:
            new_key = load_api_key(KEY_FILE)
            if new_key and not api_key:
                api_key = new_key
                LOG.info("API key now present — switching to live Claude backend")
            last_key_check = time.time()

        if time.time() > daily_reset:
            daily_cents_used = 0.0
            daily_reset = time.time() + 86400

        if api_key and daily_cents_used >= daily_budget_usd * 100:
            LOG.warning("daily budget exhausted ($%.2f); waiting 60s", daily_budget_usd)
            time.sleep(60)
            continue

        rows = conn.execute(
            "SELECT id, tuner_id, freq_hz, modulation_class, modulation_confidence, "
            "bandwidth_hz, snr_db, ts, protocol_tag, "
            "uls_callsign, uls_entity_name, uls_emission_designator, "
            "uls_station_class, uls_distance_km, uls_source "
            "FROM detections "
            "WHERE modulation_class IS NOT NULL "
            "  AND modulation_class != 'unclassified' "
            "  AND modulation_confidence >= ? "
            "  AND interpretation IS NULL "
            "ORDER BY snr_db DESC "
            "LIMIT ?",
            (min_confidence, max_per_cycle * 4)
        ).fetchall()

        if not rows:
            time.sleep(poll_s)
            continue

        cycle_done = 0
        for row in rows:
            if _STOP:
                break
            if cycle_done >= max_per_cycle:
                break

            now = time.time()
            call_times = [t for t in call_times if now - t < 60]
            if len(call_times) >= rate_per_min:
                time.sleep(2)
                continue

            (det_id, tuner, freq, mod, conf, bw, snr, ts, ptag,
             uls_call, uls_name, uls_emit, uls_stcl, uls_dist, uls_src) = row
            # Phase 4 band-plan augmentation: re-derive band info at interpret-time
            # (least invasive — no DB schema change). modulation_class is the raw ML
            # output; band_plan tells us if it's allowed in the freq's allocation.
            band_name = None
            band_allowed_modes = []
            band_rejected = False
            if plan is not None:
                band_entry = band_plan.band_for(freq, plan)
                if band_entry is not None:
                    band_name = band_entry.name
                    band_allowed_modes = sorted(band_entry.allowed_modes)
                    band_rejected = bool(mod and mod not in band_entry.allowed_modes)
            bundle = {
                "freq_mhz": freq / 1e6,
                "modulation_class": mod,
                "ml_class": mod,
                "confidence": conf,
                "bandwidth_khz": bw / 1e3,
                "snr_db": snr,
                "tuner": tuner,
                "protocol_tag": ptag,
                "band_name": band_name,
                "band_allowed_modes": band_allowed_modes,
                "band_rejected": band_rejected,
                "uls_callsign": uls_call,
                "uls_entity_name": uls_name,
                "uls_emission_designator": uls_emit,
                "uls_station_class": uls_stcl,
                "uls_distance_km": uls_dist,
                "uls_source": uls_src,
            }
            bin_idx = int((freq / 1e3) / bin_khz)
            # C5: include band_rejected in the cache key so the C5 prompt fix
            # doesn't serve stale (hallucinated) prose from before the fix.
            # Adding the field also bumps the cache hash for ALL rows — a
            # one-time invalidation of pre-C5 cache entries that's intentional.
            # The "prompt_v" version marker explicitly signals a prompt-shape
            # change so future prompt revisions can invalidate cleanly without
            # a schema-meaningful field having to carry the load.
            # C7 (Travel Mode): add location_bucket so cached interpretations
            # don't bleed across regions when Will travels. Bucket is the
            # ZIP's first 3 digits (SCF) — coarse enough that intra-metro
            # moves stay cached, fine enough that Nashville and Philly
            # interpretations don't share entries. prompt_v bump from c5 to
            # c7 invalidates ALL prior entries (pre-Travel-Mode interpretations
            # were keyed on the implicit Nashville context).
            location_bucket = (
                get_location_bucket() if _LOCATION_AVAILABLE and get_location_bucket
                else "372"
            )
            # C8 (HPDB): bump from c7 → c8 to invalidate cache entries built
            # before HPDB lookups started landing curated labels. Pre-c8
            # interpretations were keyed only on (ULS / CDBS / band-plan)
            # context and could miss the HPDB-curated label rendered into
            # the new licensee_block header.
            cache_key_obj = {
                "bin_idx": bin_idx,
                "modulation_class": mod,
                "band_rejected": band_rejected,
                "location_bucket": location_bucket,
                "prompt_v": "c8",
            }
            bundle_hash = hash_bundle(cache_key_obj)
            cached = conn.execute(
                "SELECT text, ts FROM interpretation_cache WHERE bundle_hash = ?",
                (bundle_hash,)
            ).fetchone()
            text = None
            if cached and (now - cached[1]) < cache_ttl_s:
                text = cached[0]
                cache_hit_count += 1
            else:
                text = call_claude(api_key, bundle, model)
                if api_key and not text.startswith("LLM error") and not text.startswith("LLM HTTP") and text != "no key configured":
                    daily_cents_used += cost_per_call_cents
                    call_times.append(now)

            # Skip writing stub/error placeholders so the row gets re-interpreted later
            is_stub_or_error = (text == "no key configured" or text.startswith("LLM error") or text.startswith("LLM HTTP"))
            if is_stub_or_error:
                if not api_key:
                    time.sleep(poll_s)
                    break
                continue

            if not cached or (now - cached[1]) >= cache_ttl_s:
                conn.execute(
                    "INSERT OR REPLACE INTO interpretation_cache (bundle_hash, text, ts) VALUES (?,?,?)",
                    (bundle_hash, text, now)
                )
            conn.execute(
                "UPDATE detections SET interpretation = ?, interpreted_ts = ? WHERE id = ?",
                (text, now, det_id)
            )
            conn.commit()
            interp_count += 1
            cycle_done += 1
            short = text[:90].replace("\n", " ")
            LOG.info("[%s] %.4fMHz %s: %s", tuner, freq/1e6, mod, short)

        time.sleep(1)


def main():
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    cfg = load_config(CONFIG_PATH)
    conn = sqlite3.connect(cfg["db"]["path"], timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    migrate_schema(conn)
    LOG.info("DB: %s  KEY_FILE: %s", cfg["db"]["path"], KEY_FILE)
    try:
        interpret_loop(cfg, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
