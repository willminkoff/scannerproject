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
    # NOTE: no poison-ceiling guard on the chirp path. The guard was a
    # rtl-airband-era safeguard against the pre-buffer init constant
    # poisoning the squelch estimator across a restart. Chirp's
    # ``set_squelch`` is a hot UDP call on the active demod block — no
    # initialization quirk, no post-restart estimator settle window. The
    # tracker re-runs every cycle, so even if a single instantaneous
    # signal_level_dbfs is high (because a channel is mid-transmission),
    # the next cycle will refine. Applying the operator's preset chip
    # unconditionally is the whole point of a hot-config-reload backend.

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

    # Compute thresholds: noise + margin. No poison clamp (see comment
    # at the top of this function for the architectural reason). The
    # ``sanitized_channels`` field in the response is retained as an
    # empty list to preserve the response shape consumed by handlers.py
    # and the audit-log JSON; it just never has entries on the chirp
    # path.
    noise_median = float(statistics.median(noise_used))
    thresholds = [int(round(n + margin_db)) for n in noise_used]
    sanitized: list[dict] = []

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
        # Read THIS favorite's own channel list — not the top-level
        # state.custom_favorites, which is a stale "last loaded" global set
        # that activate() never recomputes.  Reading the global meant
        # activating any favorite (e.g. single-channel "Casino") re-pushed
        # whatever was last loaded (e.g. SIC's ~20 channels).  The legacy
        # rtl-airband path keys off the enabled favorite's own channels
        # (scan_mode_controller); this aligns the chirp path with it.
        custom = list(f.get("custom_favorites") or [])
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


def _enabled_fav_id_for_band(band: str) -> Optional[str]:
    """Return the ``fav_id`` of the favorite currently enabled for the
    given band (``enabled_air`` for airband, ``enabled_ground`` for
    ground), or ``None`` if no favorite is enabled, the HP state can't
    be loaded, or the state contains no favorites list.

    Used by ``reset_radios_via_chirp`` to decide which favorite to
    re-push after the reset.  Defensive: never raises — any failure
    becomes "no enabled fav" and the caller treats the band as a
    no-op for repopulate.
    """
    try:
        try:
            from .hp_state import HPState
        except ImportError:
            from ui.hp_state import HPState  # type: ignore
        state = HPState.load()
    except Exception:
        logger.debug(
            "chirp_adapter: HPState.load failed in _enabled_fav_id_for_band",
            exc_info=True,
        )
        return None
    target = _normalize_band(band)
    flag_key = f"enabled_{'air' if target == 'airband' else 'ground'}"
    for f in (getattr(state, "favorites", []) or []):
        if not isinstance(f, dict):
            continue
        if bool(f.get(flag_key)):
            fav_id = str(f.get("id") or "").strip()
            if fav_id:
                return fav_id
    return None


def _populate_after_reset(band: str, fav_id: str) -> dict:
    """Push the favorite's channel list + re-apply preset to a daemon
    whose pool was JUST emptied (by ``reset_radios_via_chirp`` or
    ``activate_favorite_via_chirp``).

    Assumes the caller has already issued ``client.reset()`` — this
    helper does NOT reset, so it can be reused by both the "single-band
    favorite swap" and "both-bands reset+repopulate" flows without
    double-resetting the daemon.  That keeps ``chirp_client.jsonl``
    showing each band's reset+add as a clean back-to-back pair (no
    spurious second reset) so forensic passes can correlate the events
    cleanly.

    Returns the same dict shape as ``activate_favorite_via_chirp`` so
    the two callers can plumb the result into their respective response
    bodies.
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

    # Step A: batch add_channel.  The daemon validates the batch
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

    # Step B: re-apply the current preset's thresholds.  We do this so
    # the operator's chip selection is honored after the wipe;
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

    n_loaded = int(result.get("count") or len(channels))
    # Telemetry: surface profile/favorite activation so "Casino -> 1 channel"
    # is visible in the airband-ui journal.
    logger.info(
        "favorite_activated band=%s fav_id=%s n_channels_loaded=%d",
        target, fav_id, n_loaded,
    )
    return {
        "ok": True,
        "target": target,
        "fav_id": fav_id,
        "added_count": n_loaded,
        "channels": channels,
        "via": "chirp",
    }


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
    # Probe the favorite up-front so a "no channels for this band"
    # favorite swap doesn't even bother resetting the daemon — that
    # matches the legacy behavior of the rtl-airband path and keeps
    # chirp_client.jsonl from logging a "reset that did nothing".
    channels_preview = _read_favorite_freqs_for_band(target, fav_id)
    if not channels_preview:
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

    # Steps 2 + 3: add channels + re-apply preset.  Shared with
    # reset_radios_via_chirp via _populate_after_reset so the two flows
    # behave identically post-reset.
    return _populate_after_reset(target, fav_id)


# ---------------------------------------------------------------------------
# 4) /api/sitrep/action reset_radios (chirp-on)
# ---------------------------------------------------------------------------


def reset_radios_via_chirp(*, unconditional_repopulate: bool = True) -> tuple[bool, str, str]:
    """Send ``reset`` to BOTH chirp daemons, then immediately re-push
    each band's currently-enabled favorite so the channel pool stays
    populated.

    Why the repopulate?  In the rtl-airband world, ``reset_radios``
    meant "restart the rtl-airband services"; the next start re-read
    rtl_airband.conf and the channel list came back automatically.  In
    the chirp world the daemons hold their inventory in RAM (persisted
    to ``state.json`` per change), and an explicit ``reset`` wipes that
    inventory — without a follow-up ``add_channel`` the operator's pool
    stays empty and the dashboard shows "0 hits" until a favorite is
    re-activated manually.  We mirror the legacy behavior by reading
    the per-band ``enabled_air`` / ``enabled_ground`` flag from
    ``HPState`` and re-pushing whichever favorite is currently active.

    Sub-second op (typical: ~25 ms reset + ~30 ms add per band).
    Returns the same ``(ok, msg, err)`` tuple shape as
    ``_run_sitrep_action``.

    Args:
        unconditional_repopulate: when True (the default), repopulate
            each band after reset by re-pushing its enabled favorite.
            Pass False for CLI / tests / any caller that genuinely
            wants the pool empty (e.g. ``activate_favorite_via_chirp``
            handles its own re-add and a double-add would be a bug).

    Failure semantics:
      - If a daemon's *reset* fails (timeout, daemon down), we report
        failure for that band and skip its repopulate, but continue
        with the other band.  Matches the legacy "per-band detail"
        rollup.
      - If a band's *repopulate* fails (HPState corrupt, add_channels
        rejected mid-batch, preset apply raises), we log and surface
        the per-band repopulate error in the message but DO NOT undo
        the reset.  The pool stays empty for that band; the operator
        can re-activate from the dashboard.  Overall ``ok`` is still
        True iff every band's reset succeeded — the repopulate state
        is informational, not a contract guarantee.

    Logging contract: every send goes through ``chirp_client``, which
    appends one JSON line per call to ``chirp_client.jsonl``.  When the
    repopulate path fires, the log will show the reset+add pair for
    each band back-to-back, making forensic correlation easy.
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
            results[name] = {
                "ok": True,
                "pool_free": data.get("pool_free"),
                "repopulate": None,
            }
        except ChirpClientError as exc:
            results[name] = {"ok": False, "error": str(exc), "repopulate": None}
        except Exception as exc:  # noqa: BLE001
            results[name] = {
                "ok": False,
                "error": f"unexpected: {exc}",
                "repopulate": None,
            }

    # ----- Repopulate phase (only when requested) ---------------------------
    # Each band is independent — one band's repopulate failure must not
    # block the other.  We use the enabled_air/enabled_ground flag on
    # HPState as the source of truth (mirrors what
    # /api/hp/state/activate writes).
    if unconditional_repopulate:
        for name in ("airband", "ground"):
            band_result = results[name]
            if not band_result.get("ok"):
                # Reset itself failed — skip repopulate; no daemon to
                # talk to anyway.
                band_result["repopulate"] = {"skipped": "reset_failed"}
                continue
            fav_id = _enabled_fav_id_for_band(name)
            if not fav_id:
                band_result["repopulate"] = {"skipped": "no_enabled_favorite"}
                continue
            try:
                rp = _populate_after_reset(name, fav_id)
            except Exception as exc:  # noqa: BLE001
                # Defensive — _populate_after_reset is designed to
                # swallow internal failures, but if a bug or upstream
                # change ever lets one through, log and continue.  The
                # operator can re-activate from the dashboard.
                logger.exception(
                    "chirp_adapter: repopulate band=%s fav_id=%s raised",
                    name, fav_id,
                )
                band_result["repopulate"] = {
                    "ok": False,
                    "fav_id": fav_id,
                    "error": f"unexpected: {exc}",
                }
                continue
            band_result["repopulate"] = {
                "ok": bool(rp.get("ok")),
                "fav_id": fav_id,
                "added_count": rp.get("added_count"),
                "error": rp.get("error"),
            }

    elapsed = time.monotonic() - t0
    all_ok = all(r.get("ok") for r in results.values())

    # ----- Escalation: comms failure → safe_restart_rtl_airband ----------
    # If any band's UDP reset failed with a comms-style error (timeout,
    # refused, no response), the daemon is likely wedged at a syscall
    # (canonical case: stuck in sdrplay_api_Open with the SoapySDR
    # daemon hung; UDP socket isn't being serviced).  Fall back to
    # safe_restart_rtl_airband, which sequences the systemd restart
    # with an sdrplay-daemon bounce on probe failure.  Without this
    # escalation, "Reset Radios" returns red and the operator has to
    # SSH in and run the recovery by hand (which is what happened on
    # 2026-06-08; commit history has the manual sequence).
    restart_summary = None
    if not all_ok:
        comms_failed = [
            name for name, r in results.items()
            if not r.get("ok") and _looks_like_comms_failure(r.get("error"))
        ]
        if comms_failed:
            try:
                try:
                    from .airband_restart import safe_restart_rtl_airband
                except ImportError:
                    from ui.airband_restart import safe_restart_rtl_airband  # type: ignore
                logger.info(
                    "chirp_adapter: reset_radios escalating to safe_restart "
                    "for bands=%s (comms-failed)", comms_failed,
                )
                sr = safe_restart_rtl_airband(
                    bands=tuple(comms_failed),
                    reason="chirp_reset_unresponsive",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "chirp_adapter: safe_restart_rtl_airband raised during "
                    "reset_radios escalation"
                )
                sr = {"status": "raised", "error": f"raised: {exc}", "results": {}}
            restart_summary = {
                "status": sr.get("status"),
                "restarted_sdrplay": bool(sr.get("restarted_sdrplay")),
                "bands": comms_failed,
            }
            per_band = sr.get("results") or {}
            for band in comms_failed:
                band_sr = per_band.get(band, {})
                if band_sr.get("ok"):
                    # Restart succeeded — band is back.  Mark ok, note
                    # escalation, attempt repopulate (the per-band repop
                    # was skipped earlier because reset_failed).
                    results[band]["ok"] = True
                    results[band]["escalated"] = "safe_restart"
                    if unconditional_repopulate:
                        fav_id = _enabled_fav_id_for_band(band)
                        if not fav_id:
                            results[band]["repopulate"] = {
                                "skipped": "no_enabled_favorite",
                            }
                        else:
                            try:
                                rp = _populate_after_reset(band, fav_id)
                                results[band]["repopulate"] = {
                                    "ok": bool(rp.get("ok")),
                                    "fav_id": fav_id,
                                    "added_count": rp.get("added_count"),
                                    "error": rp.get("error"),
                                }
                            except Exception as exc:  # noqa: BLE001
                                logger.exception(
                                    "chirp_adapter: post-escalation repop "
                                    "raised band=%s fav_id=%s", band, fav_id,
                                )
                                results[band]["repopulate"] = {
                                    "ok": False,
                                    "fav_id": fav_id,
                                    "error": f"unexpected: {exc}",
                                }
                else:
                    # Restart also failed — propagate combined error.
                    sr_err = band_sr.get("error") or sr.get("error") or "restart_failed"
                    results[band]["error"] = (
                        f"reset+restart both failed: "
                        f"{results[band].get('error')} → {sr_err}"
                    )
            # Recompute after escalation.
            all_ok = all(r.get("ok") for r in results.values())

    elapsed = time.monotonic() - t0  # recompute after possible escalation

    def _repop_summary(b: str) -> str:
        rp = results[b].get("repopulate")
        if rp is None:
            return "no-repop"
        if "skipped" in rp:
            return f"no-repop({rp['skipped']})"
        if rp.get("ok"):
            return f"repop={rp.get('added_count')}"
        return f"repop-fail({rp.get('error') or 'unknown'})"

    def _escal_summary() -> str:
        if not restart_summary:
            return ""
        sd = " (sdrplay bounced)" if restart_summary["restarted_sdrplay"] else ""
        return (
            f" [escalated:safe_restart status={restart_summary['status']}"
            f" bands={','.join(restart_summary['bands'])}{sd}]"
        )

    if all_ok:
        msg = (
            f"Reset Radios (chirp) triggered in {elapsed:.2f}s "
            f"(airband pool_free={results['airband'].get('pool_free')} "
            f"{_repop_summary('airband')}, "
            f"ground pool_free={results['ground'].get('pool_free')} "
            f"{_repop_summary('ground')})"
            f"{_escal_summary()}"
        )
        return True, msg, ""
    detail = "; ".join(
        f"{b}: {('ok' if r.get('ok') else (r.get('error') or 'fail'))}"
        for b, r in results.items()
    )
    return False, "", (
        f"Reset Radios (chirp) partial fail after {elapsed:.2f}s — {detail}"
        f"{_escal_summary()}"
    )


def _looks_like_comms_failure(err: Optional[str]) -> bool:
    """Heuristic: does an error string look like a UDP comms failure
    rather than a logic / parse error?  UDP-comms failures are the
    signature of a wedged daemon and warrant escalation to
    safe_restart_rtl_airband.  Logic errors (HPState corrupt,
    add_channel rejected, etc.) would not be helped by a systemd
    restart and we let them bubble up as-is.
    """
    if not err:
        return False
    s = err.lower()
    return any(
        token in s
        for token in (
            "timed out", "timeout",
            "refused", "no response", "no reply",
            "broken pipe", "connection reset", "connection",
            "unreachable", "errno 111", "errno 113",
        )
    )




def calibrate_squelch_via_chirp(
    band: str,
    sample_sec: float = 15.0,
    margin_db: float = 3.0,
    poll_hz: float = 10.0,
    skip_open: bool = True,
) -> dict:
    """Per-channel noise-floor calibration via signal_level_dbfs sampling.

    The existing preset apply (`apply_squelch_preset_via_chirp`) reads
    ``signal_level_dbfs`` ONCE and adds margin_db.  If the snapshot
    happens during a brief signal peak, the resulting threshold is too
    high and gates real voice (we hit this on marine VHF 2026-06-09).

    This sampler polls at ``poll_hz`` for ``sample_sec`` seconds, builds
    a distribution per channel, and sets ``squelch = p90_noise + margin_db``.
    p90 (90th-percentile) of the measured noise is robust against noise
    fluctuations while still discriminating voice (which would push the
    percentile much higher when it occurs).

    Channels that were open at ANY sample tick are skipped — voice during
    sampling would contaminate the noise estimate.  Operator can re-run
    later when the channel is quiet.

    Returns a plan dict with per-channel measurements + applied thresholds.
    """
    import time as _time_mod
    import statistics as _stats_mod

    sp = _squelch_preset_module()
    target = _normalize_band(band)

    # Validate/clamp inputs.
    sample_sec = float(max(2.0, min(60.0, sample_sec)))
    margin_db = float(max(-10.0, min(30.0, margin_db)))
    poll_hz = float(max(1.0, min(20.0, poll_hz)))
    poll_interval = 1.0 / poll_hz

    try:
        client = _chirp_client_for(target)
    except Exception as exc:
        return {
            "target": target,
            "sample_sec": sample_sec,
            "margin_db": margin_db,
            "changed": False,
            "error": f"chirp_client_init_failed: {exc}",
            "via": "chirp_calibrate",
        }

    # Sample loop.
    samples_per_channel: dict[str, list[float]] = {}
    labels: dict[str, str] = {}
    open_seen: set[str] = set()
    last_status: dict = {}
    n_polls = 0
    t0 = _time_mod.monotonic()
    end_time = t0 + sample_sec

    while _time_mod.monotonic() < end_time:
        try:
            status = client.get_status()
        except Exception as exc:
            return {
                "target": target,
                "sample_sec": sample_sec,
                "margin_db": margin_db,
                "polls": n_polls,
                "changed": False,
                "error": f"chirp_daemon_unreachable: {exc}",
                "via": "chirp_calibrate",
            }
        last_status = status
        n_polls += 1
        for ch in status.get("channels", []) or []:
            cid = ch.get("id")
            if not cid:
                continue
            lvl = ch.get("signal_level_dbfs")
            try:
                lvl_f = float(lvl)
            except (TypeError, ValueError):
                continue
            labels[cid] = ch.get("label") or cid
            if ch.get("squelch_open"):
                open_seen.add(cid)
            samples_per_channel.setdefault(cid, []).append(lvl_f)
        # Sleep until next poll tick (compensates for get_status latency).
        next_t = t0 + (n_polls * poll_interval)
        sleep_for = next_t - _time_mod.monotonic()
        if sleep_for > 0:
            _time_mod.sleep(sleep_for)

    elapsed = _time_mod.monotonic() - t0

    # Build per-channel report and apply thresholds.
    per_channel: list[dict] = []
    applied_count = 0
    skipped_count = 0
    for cid in sorted(samples_per_channel.keys()):
        samples = samples_per_channel[cid]
        label = labels.get(cid, cid)
        if not samples:
            continue
        if skip_open and cid in open_seen:
            per_channel.append({
                "id": cid,
                "label": label,
                "samples": len(samples),
                "skipped": True,
                "reason": "was_open_during_sampling",
                "applied_squelch_dbfs": None,
            })
            skipped_count += 1
            continue
        sorted_s = sorted(samples)
        n = len(sorted_s)
        # 90th percentile (linear interpolation between nearest indices)
        idx_f = 0.9 * (n - 1)
        i = int(idx_f)
        frac = idx_f - i
        p90 = sorted_s[i] if i + 1 >= n else (sorted_s[i] + frac * (sorted_s[i + 1] - sorted_s[i]))
        median = _stats_mod.median(sorted_s)
        threshold = int(round(p90 + margin_db))
        # Clamp to a sane operating range.
        threshold = max(-100, min(-1, threshold))
        try:
            client.set_squelch(id=cid, dbfs=float(threshold))
            applied_count += 1
            per_channel.append({
                "id": cid,
                "label": label,
                "samples": n,
                "median_noise_dbfs": round(float(median), 1),
                "p90_noise_dbfs": round(float(p90), 1),
                "noise_min_dbfs": round(float(sorted_s[0]), 1),
                "noise_max_dbfs": round(float(sorted_s[-1]), 1),
                "threshold_dbfs": threshold,
                "applied_squelch_dbfs": threshold,
            })
        except Exception as exc:
            per_channel.append({
                "id": cid,
                "label": label,
                "samples": n,
                "median_noise_dbfs": round(float(median), 1),
                "p90_noise_dbfs": round(float(p90), 1),
                "threshold_dbfs": threshold,
                "applied_squelch_dbfs": None,
                "error": f"set_squelch_failed: {exc}",
            })

    return {
        "target": target,
        "sample_sec": round(sample_sec, 1),
        "elapsed_sec": round(elapsed, 1),
        "margin_db": margin_db,
        "poll_hz": poll_hz,
        "polls": n_polls,
        "channels_total": len(samples_per_channel),
        "channels_applied": applied_count,
        "channels_skipped": skipped_count,
        "channels": per_channel,
        "changed": applied_count > 0,
        "via": "chirp_calibrate",
    }


def wide_open_squelch_via_chirp(band: str, dbfs: float = -120.0) -> dict:
    """Test mode: set every channel in the band to a pass-everything dbfs.

    Used to verify the demod + audio path is intact (e.g., after a config
    change you want to confirm audio reaches the speaker before tightening
    the squelch back up).  After testing, the operator re-applies a
    preset (or runs calibrate) to restore noise-floor-based thresholds.

    Returns plan dict with the count of channels touched.
    """
    target = _normalize_band(band)
    try:
        client = _chirp_client_for(target)
    except Exception as exc:
        return {
            "target": target,
            "applied_dbfs": dbfs,
            "changed": False,
            "error": f"chirp_client_init_failed: {exc}",
            "via": "chirp_wide_open",
        }
    try:
        status = client.get_status()
    except Exception as exc:
        return {
            "target": target,
            "applied_dbfs": dbfs,
            "changed": False,
            "error": f"chirp_daemon_unreachable: {exc}",
            "via": "chirp_wide_open",
        }
    dbfs = float(max(-120.0, min(-1.0, dbfs)))
    applied = 0
    failed: list[str] = []
    for ch in status.get("channels", []) or []:
        cid = ch.get("id")
        if not cid:
            continue
        try:
            client.set_squelch(id=cid, dbfs=dbfs)
            applied += 1
        except Exception as exc:
            failed.append(f"{cid}: {exc}")
    return {
        "target": target,
        "applied_dbfs": dbfs,
        "channels_total": len(status.get("channels", []) or []),
        "channels_applied": applied,
        "changed": applied > 0,
        "errors": failed,
        "via": "chirp_wide_open",
    }




def set_audio_vad_sensitivity_via_chirp(band: str, sensitivity: float) -> dict:
    """Push the VAD threshold to every channel in the band.

    The UI exposes a 0-10 Sensitivity slider; we map that linearly to a
    0-100 VAD score threshold (10 = 0 threshold = let everything pass,
    0 = 100 threshold = strict voice only).  Default operator position 5
    maps to threshold 50.

    Returns a per-channel report dict similar to other chirp_adapter
    helpers so handlers.py can surface counts/errors uniformly.
    """
    target = _normalize_band(band)
    sensitivity = float(max(0.0, min(10.0, sensitivity)))
    # 10 = threshold 0 (very permissive), 0 = threshold 100 (very strict).
    threshold = float(round(100.0 - sensitivity * 10.0))
    try:
        client = _chirp_client_for(target)
    except Exception as exc:
        return {
            "target": target, "sensitivity": sensitivity, "threshold": threshold,
            "changed": False, "error": f"chirp_client_init_failed: {exc}",
            "via": "chirp_vad_sensitivity",
        }
    try:
        status = client.get_status()
    except Exception as exc:
        return {
            "target": target, "sensitivity": sensitivity, "threshold": threshold,
            "changed": False, "error": f"chirp_daemon_unreachable: {exc}",
            "via": "chirp_vad_sensitivity",
        }
    applied = 0
    failed: list[str] = []
    for ch in status.get("channels", []) or []:
        cid = ch.get("id")
        if not cid:
            continue
        try:
            client.set_vad_threshold(id=cid, threshold=threshold)
            applied += 1
        except Exception as exc:
            failed.append(f"{cid}: {exc}")
    return {
        "target": target, "sensitivity": sensitivity, "threshold": threshold,
        "channels_total": len(status.get("channels", []) or []),
        "channels_applied": applied,
        "changed": applied > 0,
        "errors": failed,
        "via": "chirp_vad_sensitivity",
    }


def set_audio_vad_bypass_via_chirp(band: str, bypass: bool) -> dict:
    """Set every channel's VAD gate to bypass=True (passthrough) or False.

    Used by the Test/wide-open UI to verify the audio path.
    """
    target = _normalize_band(band)
    try:
        client = _chirp_client_for(target)
    except Exception as exc:
        return {
            "target": target, "bypass": bool(bypass), "changed": False,
            "error": f"chirp_client_init_failed: {exc}",
            "via": "chirp_vad_bypass",
        }
    try:
        status = client.get_status()
    except Exception as exc:
        return {
            "target": target, "bypass": bool(bypass), "changed": False,
            "error": f"chirp_daemon_unreachable: {exc}",
            "via": "chirp_vad_bypass",
        }
    applied = 0
    failed: list[str] = []
    for ch in status.get("channels", []) or []:
        cid = ch.get("id")
        if not cid:
            continue
        try:
            client.set_vad_bypass(id=cid, bypass=bool(bypass))
            applied += 1
        except Exception as exc:
            failed.append(f"{cid}: {exc}")
    return {
        "target": target, "bypass": bool(bypass),
        "channels_total": len(status.get("channels", []) or []),
        "channels_applied": applied,
        "changed": applied > 0,
        "errors": failed,
        "via": "chirp_vad_bypass",
    }


__all__ = [
    "apply_squelch_preset_via_chirp",
    "calibrate_squelch_via_chirp",
    "wide_open_squelch_via_chirp",
    "set_audio_vad_sensitivity_via_chirp",
    "set_audio_vad_bypass_via_chirp",
    "set_squelch_auto_via_chirp",
    "activate_favorite_via_chirp",
    "reset_radios_via_chirp",
]
