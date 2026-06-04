"""ui.chirp_adapter — chirp-flag-on implementations of the four operator endpoints.

Phase 4c. When ``SB5_USE_GR_DEMOD=true``, ``ui/handlers.py`` branches
ONCE at the top of each of these endpoints and delegates to the
corresponding helper below:

  /api/airband/squelch_preset    -> apply_squelch_preset_via_chirp(...)
  /api/airband/squelch_auto       -> set_squelch_auto_via_chirp(...)
  /api/hp/state/activate          -> activate_favorite_via_chirp(...)
  /api/sitrep/action reset_radios -> reset_radios_via_chirp(...)

Each helper returns the same shape as the legacy implementation so the
HTTP response surface is byte-identical from the dashboard's POV.

Production safety: this module is dormant when the flag is off.  No
state, no threads — just sync helpers that wrap the ChirpClient
singletons.  Importing it does NOT contact the chirp daemons.

Failure modes (the legacy rtl-airband path's analogues are noted):

  - Daemon down (ChirpDaemonDown)  -> equivalent of "rtl_airband stats
    file missing".  Helpers return ``{"ok": False, "error":
    "chirp_daemon_down", ...}`` so the dashboard surfaces a recovery
    hint instead of crashing.
  - Daemon rejected (ChirpRejected) -> equivalent of "noise_floor_not_warm"
    or "unknown channel".  The 409 contract from the legacy handler is
    preserved: noise-floor-not-warm bubbles up with its retry hint;
    other rejections become 500.
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Import-side note: we deliberately import inside functions so the cost
# is zero when the flag is off.  The module-level body is stdlib-only.


# ---------------------------------------------------------------------------
# Helper — band normalisation matches the rtl-airband side
# ---------------------------------------------------------------------------


def _normalize_band(band: str) -> str:
    """Map 'air'/'airband' -> 'airband', 'ground'/'gnd' -> 'ground'.

    Raises ValueError on anything else.
    """
    b = str(band or "").strip().lower()
    if b in ("airband", "air"):
        return "airband"
    if b in ("ground", "gnd"):
        return "ground"
    raise ValueError(f"unknown band: {band!r}")


def _chirp_client_for(band: str):
    """Lazy import + lookup of the per-band ChirpClient singleton."""
    try:
        from .chirp_client import client_for_band
    except ImportError:
        from ui.chirp_client import client_for_band  # type: ignore
    return client_for_band(band)


def _squelch_preset_module():
    """Lazy import of ui.squelch_preset (shared margin definitions)."""
    try:
        from . import squelch_preset as sp
    except ImportError:
        from ui import squelch_preset as sp  # type: ignore
    return sp


def _managed_controls_module():
    """Lazy import of ui.managed_analog_controls."""
    try:
        from . import managed_analog_controls as mac
    except ImportError:
        from ui import managed_analog_controls as mac  # type: ignore
    return mac


# ---------------------------------------------------------------------------
# 1) /api/airband/squelch_preset (chirp-on)
# ---------------------------------------------------------------------------


def apply_squelch_preset_via_chirp(band: str, preset: str) -> dict:
    """Compute per-channel thresholds from chirp's get_status and push
    set_squelch per channel.

    Return shape matches ``ui.squelch_preset.apply_preset(...)`` so
    handlers.py can branch ONCE at the top and use the same response
    plumbing.

    Design decision (per task spec): read noise floor from chirp,
    compute thresholds *locally* using the SAME shared margin map
    that the rtl-airband path uses (ui.squelch_preset.PRESET_MARGINS_DB).
    Then push set_squelch per channel.  This keeps the preset logic in
    ONE place — the chirp daemon does not need to know about preset
    names; it just executes set_squelch.
    """
    sp = _squelch_preset_module()
    target = _normalize_band(band)
    norm_preset = sp.normalize_preset(preset)
    margin_db = sp.margin_for(norm_preset)
    ceiling = sp.poison_ceiling_for_band(target)

    client = _chirp_client_for(target)

    # Pull a snapshot of the daemon's per-channel state.
    try:
        status = client.get_status()
    except Exception as exc:
        # Treat daemon-down as a soft failure with no apply.  Response
        # shape mirrors the legacy plan dict's error case so the caller
        # can keep its existing 500 branch.
        return {
            "target": target,
            "preset": norm_preset,
            "margin_db": margin_db,
            "freqs": [],
            "thresholds": [],
            "noise_floor_median": None,
            "stats_available": False,
            "changed": False,
            "error": f"chirp_daemon_down: {exc}",
            "via": "chirp",
        }

    channels = status.get("channels") or []
    # Channel.snapshot() exposes signal_level_dbfs which is the
    # instantaneous estimator output — the chirp equivalent of
    # rtl-airband's noise_floor field.  In Phase 4c we treat that as
    # the noise floor sample (good enough for the preset apply; the
    # tracker will refine over time with its own hysteresis).
    noise_used: list[float] = []
    freqs: list[float] = []
    ids: list[str] = []
    for ch in channels:
        try:
            nf = float(ch.get("signal_level_dbfs"))
            freq = float(ch.get("freq_mhz"))
            cid = str(ch.get("id"))
        except (TypeError, ValueError):
            continue
        if not cid or freq <= 0.0:
            continue
        noise_used.append(nf)
        freqs.append(freq)
        ids.append(cid)

    if not freqs:
        return {
            "target": target,
            "preset": norm_preset,
            "margin_db": margin_db,
            "freqs": [],
            "thresholds": [],
            "noise_floor_median": None,
            "stats_available": True,
            "changed": False,
            "error": "no_freqs_in_profile",
            "via": "chirp",
        }

    # Poison-noise-floor rejection — mirror ui.squelch_preset.apply_preset
    # so the 409 contract is preserved.
    noise_median = float(statistics.median(noise_used))
    if noise_median > ceiling:
        return {
            "target": target,
            "preset": norm_preset,
            "margin_db": margin_db,
            "freqs": freqs,
            "thresholds": [],
            "noise_floor_median": noise_median,
            "stats_available": True,
            "changed": False,
            "error": "noise_floor_not_warm",
            "status": "rejected",
            "reason": "noise_floor_median above poison ceiling",
            "poison_ceiling_dbfs": float(ceiling),
            "retry_after_sec": 30,
            "via": "chirp",
        }

    # Compute thresholds: noise + margin, clamped to the same ceiling as
    # the rtl-airband path.
    thresholds = [int(round(n + margin_db)) for n in noise_used]
    # Per-channel poison sanity (single-channel race) — fall back to a
    # safe floor (-100 dBFS) for the offending channel.
    sanitized: list[dict] = []
    for i, n in enumerate(noise_used):
        if n > ceiling:
            sanitized.append({
                "i": i,
                "noise_used_dbfs": n,
                "poisoned_threshold": thresholds[i],
                "fallback": -100,
                "fallback_source": "floor",
            })
            thresholds[i] = -100

    # Push per-channel set_squelch.  Best-effort: a single rejection
    # does NOT abort the rest — chirp's set_squelch is idempotent and
    # the operator surface continues to show useful "applied N of M"
    # information.
    applied_count = 0
    rejections: list[dict] = []
    t0 = time.monotonic()
    for cid, dbfs in zip(ids, thresholds):
        try:
            client.set_squelch(cid, float(dbfs))
            applied_count += 1
        except Exception as exc:
            rejections.append({"id": cid, "dbfs": dbfs, "error": str(exc)})

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    threshold_median = int(round(statistics.median(thresholds))) if thresholds else None

    # Persist the override metadata so the SSE readout shows fresh data —
    # mirrors what apply_preset() does on the rtl-airband path.
    try:
        mac = _managed_controls_module()
        try:
            from .profile_config import resolve_controls_path, parse_controls
        except ImportError:
            from ui.profile_config import resolve_controls_path, parse_controls  # type: ignore
        conf_path = resolve_controls_path(target)
        try:
            cur_gain, cur_snr, _cur_dbfs, _cur_mode = parse_controls(conf_path)
        except Exception:
            cur_gain, cur_snr = 32.8, 10.0
        mac.persist_managed_controls_override(
            target,
            conf_path,
            gain=float(cur_gain),
            squelch_mode="dbfs",
            squelch_snr=float(cur_snr),
            squelch_dbfs=float(threshold_median if threshold_median is not None else -60.0),
            squelch_preset=norm_preset,
            squelch_preset_margin_db=float(margin_db),
            squelch_preset_noise_floor_dbfs=noise_median,
            squelch_preset_computed_at_ms=int(time.time() * 1000),
        )
    except Exception:
        logger.debug("chirp_adapter: persist override skipped", exc_info=True)

    return {
        "target": target,
        "preset": norm_preset,
        "margin_db": margin_db,
        "freqs": freqs,
        "thresholds": thresholds,
        "threshold_median": threshold_median,
        "noise_floor_median": noise_median,
        "stats_available": True,
        "sanitized_channels": sanitized,
        "sanitized_count": len(sanitized),
        "applied_count": applied_count,
        "rejections": rejections,
        "rejected_count": len(rejections),
        "changed": bool(applied_count),
        "elapsed_ms": round(elapsed_ms, 2),
        "error": "",
        "via": "chirp",
    }


# ---------------------------------------------------------------------------
# 2) /api/airband/squelch_auto (chirp-on)
# ---------------------------------------------------------------------------


def set_squelch_auto_via_chirp(band: str, enabled: bool) -> bool:
    """Toggle the per-band AUTO/MANUAL flag.

    The tracker (see ui.squelch_tracker) reads this flag on every cycle
    and decides whether to apply.  The chirp-on tracker path (Task 5)
    pushes set_squelch via the chirp client when AUTO is on; pushes
    nothing when AUTO is off.  So this endpoint's behavior is identical
    flag-on or flag-off — the difference is in the tracker's sink.
    """
    target = _normalize_band(band)
    mac = _managed_controls_module()
    return bool(mac.set_band_squelch_auto(target, bool(enabled)))


# ---------------------------------------------------------------------------
# 3) /api/hp/state/activate (chirp-on)
# ---------------------------------------------------------------------------


def _read_favorite_freqs_for_band(band: str, fav_id: str) -> list[dict]:
    """Read the favorite's per-band freq list from hp_state.json.

    Returns a list of ``{"id": str, "freq_mhz": float, "label": str}``
    suitable for batch add_channel.  Returns [] if the favorite is not
    found, has no freqs for the band, or hp_state.json is missing.
    """
    try:
        try:
            from .hp_state import HPState
        except ImportError:
            from ui.hp_state import HPState  # type: ignore
        state = HPState.load()
    except Exception:
        logger.debug("chirp_adapter: HPState.load failed", exc_info=True)
        return []
    target = _normalize_band(band)
    # Find the activated favorite (caller has already set enabled_<band> = True).
    favorites = list(getattr(state, "favorites", []) or [])
    for f in favorites:
        if not isinstance(f, dict):
            continue
        if str(f.get("id") or "").strip() != fav_id:
            continue
        # Per-band selector — fav already activated by handler, but we
        # still cross-check the flag so a stale request doesn't push
        # the wrong list.
        flag_key = f"enabled_{'air' if target == 'airband' else 'ground'}"
        if not bool(f.get(flag_key)):
            return []
        # Favorite's "frequency" + custom_favorites list contains the
        # channel definitions.  The legacy rtl-airband path uses the
        # profiles/rtl_airband_hp3_favorites_*.conf file to enumerate
        # channels; for the chirp path we don't need that file because
        # the daemon receives the freq list directly.
        custom = list(state.custom_favorites or [])
        out: list[dict] = []
        for entry in custom:
            if not isinstance(entry, dict):
                continue
            try:
                freq = float(entry.get("frequency") or 0.0)
            except (TypeError, ValueError):
                continue
            if freq <= 0.0:
                continue
            tag = str(entry.get("alpha_tag") or entry.get("department_name") or "").strip()
            cid = str(entry.get("id") or "").strip() or f"freq:{freq:.3f}"
            # band-membership heuristic mirrors the existing rtl-airband
            # profile builder: < 137 MHz = airband, >= 137 = ground.
            if target == "airband" and freq >= 137.0:
                continue
            if target == "ground" and freq < 137.0:
                continue
            out.append({
                "id": cid[:64],
                "freq_mhz": float(freq),
                "mode": "am" if target == "airband" else "nfm",
                "squelch_dbfs": -60.0,
                "gain_db": 0.0,
                "label": tag[:64] if tag else None,
            })
        return out
    return []


def activate_favorite_via_chirp(band: str, fav_id: str) -> dict:
    """Push the favorite's channel list to the relevant chirp daemon.

    Strategy: reset the daemon (parks all channels in sub-second), then
    batch add_channel for the new list.  Atomically replaces the
    daemon's channel inventory.

    Returns ``{ok, target, fav_id, added_count, error}`` so the handler
    can synthesize the same JSON shape as the legacy path (which just
    saves the HPState and returns the saved payload).
    """
    target = _normalize_band(band)
    channels = _read_favorite_freqs_for_band(target, fav_id)
    if not channels:
        return {
            "ok": True,  # No-op is not an error — favorite has no channels for this band.
            "target": target,
            "fav_id": fav_id,
            "added_count": 0,
            "via": "chirp",
            "note": "no channels for this band",
        }

    client = _chirp_client_for(target)

    # Step 1: reset (parks every slot, zeros master gain).  Sub-second.
    try:
        client.reset()
    except Exception as exc:
        return {
            "ok": False,
            "target": target,
            "fav_id": fav_id,
            "error": f"reset_failed: {exc}",
            "via": "chirp",
        }

    # Step 2: batch add_channel.  The daemon validates the batch
    # transactionally — partial failure is impossible (pool overflow
    # rejects the whole batch).
    try:
        result = client.add_channels(channels)
    except Exception as exc:
        return {
            "ok": False,
            "target": target,
            "fav_id": fav_id,
            "added_count": 0,
            "error": f"add_channels_failed: {exc}",
            "via": "chirp",
        }

    # Step 3: re-apply the current preset's thresholds.  We do this so
    # the operator's chip selection is honored after a favorite swap;
    # without this, the newly added channels keep their default -60 dBFS
    # threshold from the add_channel call.
    try:
        mac = _managed_controls_module()
        try:
            from .profile_config import resolve_controls_path
        except ImportError:
            from ui.profile_config import resolve_controls_path  # type: ignore
        conf_path = resolve_controls_path(target)
        rec = mac.recommended_managed_controls(target, conf_path) or {}
        preset = rec.get("squelch_preset")
        if preset:
            apply_squelch_preset_via_chirp(target, str(preset))
    except Exception:
        logger.debug("chirp_adapter: post-add preset restore skipped",
                     exc_info=True)

    return {
        "ok": True,
        "target": target,
        "fav_id": fav_id,
        "added_count": int(result.get("count") or len(channels)),
        "channels": channels,
        "via": "chirp",
    }


# ---------------------------------------------------------------------------
# 4) /api/sitrep/action reset_radios (chirp-on)
# ---------------------------------------------------------------------------


def reset_radios_via_chirp() -> tuple[bool, str, str]:
    """Send ``reset`` to BOTH chirp daemons.

    Sub-second op; no SDR restart cascade.  Returns the same
    ``(ok, msg, err)`` tuple shape as ``_run_sitrep_action``.

    Failure semantics: if either daemon is down, we report failure but
    continue with the other so the operator gets useful partial-success
    info.  Matches the rtl-airband path's "per-band detail" rollup.
    """
    try:
        from .chirp_client import (
            get_airband_client, get_ground_client, ChirpClientError,
        )
    except ImportError:
        from ui.chirp_client import (  # type: ignore
            get_airband_client, get_ground_client, ChirpClientError,
        )

    t0 = time.monotonic()
    results: dict[str, dict] = {}
    for name, getter in (("airband", get_airband_client), ("ground", get_ground_client)):
        client = getter()
        try:
            data = client.reset()
            results[name] = {"ok": True, "pool_free": data.get("pool_free")}
        except ChirpClientError as exc:
            results[name] = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            results[name] = {"ok": False, "error": f"unexpected: {exc}"}
    elapsed = time.monotonic() - t0
    all_ok = all(r.get("ok") for r in results.values())
    if all_ok:
        msg = (
            f"Reset Radios (chirp) triggered in {elapsed:.2f}s "
            f"(airband pool_free={results['airband'].get('pool_free')}, "
            f"ground pool_free={results['ground'].get('pool_free')})"
        )
        return True, msg, ""
    detail = "; ".join(
        f"{b}: {('ok' if r.get('ok') else (r.get('error') or 'fail'))}"
        for b, r in results.items()
    )
    return False, "", f"Reset Radios (chirp) partial fail after {elapsed:.2f}s — {detail}"


__all__ = [
    "apply_squelch_preset_via_chirp",
    "set_squelch_auto_via_chirp",
    "activate_favorite_via_chirp",
    "reset_radios_via_chirp",
]
