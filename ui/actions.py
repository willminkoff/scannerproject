"""Business logic for scanner actions."""
from __future__ import annotations

import os
import json
import shutil
import time
import re
import logging
import subprocess
import tempfile
from typing import Any
try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

try:
    from .config import (
        CONFIG_SYMLINK,
        GROUND_CONFIG_PATH,
        COMBINED_CONFIG_PATH,
        HOLD_STATE_PATH,
        TUNE_BACKUP_PATH,
        ANALOG_AUTO_SQUELCH_STATS_PATH,
        ANALOG_AUTO_SQUELCH_SAMPLE_SEC,
        ANALOG_AUTO_SQUELCH_MARGIN_DB,
        ANALOG_AUTO_SQUELCH_MIN_DBFS,
        ANALOG_AUTO_SQUELCH_MAX_DBFS,
        RTL_AIRBAND_AIRBAND_STATS_PATH,
        RTL_AIRBAND_GROUND_STATS_PATH,
        RE_UI_DISABLED,
    )
    from .systemd import (
        unit_active,
        stop_rtl,
        start_rtl,
        restart_rtl,
        restart_ground,
        restart_icecast,
        restart_keepalive,
        restart_ui,
        restart_digital,
        stop_ground,
        start_acars, stop_acars, restart_acars,
        start_vdl2, stop_vdl2, restart_vdl2,
        start_radiosonde, stop_radiosonde, restart_radiosonde,
    )
    from .profile_config import (
        split_profiles, guess_current_profile, set_profile, write_controls,
        write_combined_config, read_active_config_path, resolve_controls_path, avoid_current_hit,
        clear_avoids, write_filter, parse_freqs_labels, replace_freqs_labels, parse_controls,
        read_wx_decoder,
    )
    from .managed_analog_controls import persist_managed_controls_override
    from .scanner import mark_analog_hit_cutoff
    from .config_validator import validate_combined_config_text
except ImportError:
    from ui.config import (
        CONFIG_SYMLINK,
        GROUND_CONFIG_PATH,
        COMBINED_CONFIG_PATH,
        HOLD_STATE_PATH,
        TUNE_BACKUP_PATH,
        ANALOG_AUTO_SQUELCH_STATS_PATH,
        ANALOG_AUTO_SQUELCH_SAMPLE_SEC,
        ANALOG_AUTO_SQUELCH_MARGIN_DB,
        ANALOG_AUTO_SQUELCH_MIN_DBFS,
        ANALOG_AUTO_SQUELCH_MAX_DBFS,
        RTL_AIRBAND_AIRBAND_STATS_PATH,
        RTL_AIRBAND_GROUND_STATS_PATH,
        RE_UI_DISABLED,
    )
    from ui.systemd import (
        unit_active,
        stop_rtl,
        start_rtl,
        restart_rtl,
        restart_ground,
        restart_icecast,
        restart_keepalive,
        restart_ui,
        restart_digital,
        stop_ground,
        start_acars, stop_acars, restart_acars,
        start_vdl2, stop_vdl2, restart_vdl2,
        start_radiosonde, stop_radiosonde, restart_radiosonde,
    )
    from ui.profile_config import (
        split_profiles, guess_current_profile, set_profile, write_controls,
        write_combined_config, read_active_config_path, resolve_controls_path, avoid_current_hit,
        clear_avoids, write_filter, parse_freqs_labels, replace_freqs_labels, parse_controls,
        read_wx_decoder,
    )
    from ui.managed_analog_controls import persist_managed_controls_override
    from ui.scanner import mark_analog_hit_cutoff
    from ui.config_validator import validate_combined_config_text

logger = logging.getLogger(__name__)


_USBDEVFS_RESET = 0x5514
_DEFAULT_USB_HUB_RESET_PATHS = (
    "/dev/bus/usb/003/002",
    "/dev/bus/usb/004/002",
)
_USB_HUB_RESET_PATHS_ENV = "USB_HUB_RESET_PATHS"
_USB_RESET_HELPER = (
    "import os,fcntl,sys;"
    "fd=os.open(sys.argv[1], os.O_WRONLY | getattr(os, 'O_CLOEXEC', 0));"
    "fcntl.ioctl(fd, 0x5514, 0);"
    "os.close(fd)"
)
_RE_DBFS_NOISE_LEVEL = re.compile(
    r'^channel_dbfs_noise_level\{freq="([0-9.]+)"[^}]*\}\s+(-?[0-9.]+)\s*$'
)
_AUTO_SQUELCH_POLL_SEC = 1.0
_AUTO_SQUELCH_FREQ_EPSILON_MHZ = 0.002


def _usb_hub_reset_paths() -> list[str]:
    raw = str(os.getenv(_USB_HUB_RESET_PATHS_ENV, "") or "").strip()
    candidates = re.split(r"[,\s;]+", raw) if raw else list(_DEFAULT_USB_HUB_RESET_PATHS)
    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = str(candidate or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _reset_usb_hub_path(path: str) -> tuple[bool, str]:
    direct_error = ""
    if fcntl is not None:
        fd = None
        try:
            fd = os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
            fcntl.ioctl(fd, _USBDEVFS_RESET, 0)
            return True, ""
        except PermissionError as e:
            direct_error = str(e)
        except Exception as e:
            return False, str(e)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    logger.debug("actions: failed to close USB reset fd for %s", path, exc_info=True)

    try:
        result = subprocess.run(
            ["sudo", "-n", "python3", "-c", _USB_RESET_HELPER, path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception as e:
        return False, direct_error or str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"usb reset failed (code {result.returncode})"
    if direct_error:
        err = f"{direct_error}; sudo fallback: {err}"
    return False, err


def _freq_metric_key(freq_mhz: float) -> str:
    return f"{float(freq_mhz):.3f}"


def _median(values: list[float]) -> float | None:
    ordered = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if not ordered:
        return None
    n = len(ordered)
    half = n // 2
    if n % 2 == 1:
        return ordered[half]
    return (ordered[half - 1] + ordered[half]) / 2.0


def _read_dbfs_noise_metrics(stats_path: str) -> dict[str, float]:
    noise_by_freq: dict[str, float] = {}
    with open(stats_path, "r", encoding="utf-8", errors="ignore") as handle:
        for raw in handle:
            line = raw.strip()
            match = _RE_DBFS_NOISE_LEVEL.match(line)
            if not match:
                continue
            freq = str(match.group(1) or "").strip()
            if not freq:
                continue
            try:
                value = float(match.group(2))
            except Exception:
                continue
            noise_by_freq[freq] = value
    return noise_by_freq


def _collect_dbfs_noise_samples(
    stats_path: str,
    sample_sec: int,
    poll_sec: float = _AUTO_SQUELCH_POLL_SEC,
) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = {}
    duration = max(1.0, float(sample_sec))
    end_monotonic = time.monotonic() + duration
    while True:
        snapshot = _read_dbfs_noise_metrics(stats_path)
        for freq_key, value in snapshot.items():
            bucket = samples.setdefault(freq_key, [])
            bucket.append(float(value))
        now = time.monotonic()
        if now >= end_monotonic:
            break
        sleep_for = min(max(0.05, float(poll_sec)), max(0.0, end_monotonic - now))
        if sleep_for > 0:
            time.sleep(sleep_for)
    return samples


def _stats_path_for_target(target: str) -> str:
    """Return the per-target rtl-airband stats file path.

    Post-MA/SL-split (commit 2cd6afb) each rtl-airband instance writes to its
    own stats file.  Auto-squelch needs to read the correct one for the
    target it is calibrating, otherwise it sees /run/rtl_airband_stats.txt
    (the legacy single-process path) which no longer exists and the action
    fails with FileNotFoundError.
    """
    t = str(target or "").strip().lower()
    if t == "ground":
        return str(RTL_AIRBAND_GROUND_STATS_PATH or "").strip()
    return str(RTL_AIRBAND_AIRBAND_STATS_PATH or "").strip()


def _load_target_profile_freqs(target: str) -> tuple[str, list[float]]:
    conf_path = resolve_controls_path(target)
    with open(conf_path, "r", encoding="utf-8", errors="ignore") as handle:
        text = handle.read()
    freqs, _labels = parse_freqs_labels(text)
    parsed: list[float] = []
    for freq in freqs:
        try:
            parsed.append(float(freq))
        except Exception:
            continue
    return conf_path, parsed


def _resolve_noise_values_for_freqs(
    freqs_mhz: list[float],
    noise_samples: dict[str, list[float]],
) -> tuple[list[float], int]:
    if not freqs_mhz:
        return [], 0
    parsed_noise: dict[float, list[float]] = {}
    for key, values in noise_samples.items():
        try:
            parsed_noise[float(key)] = [float(v) for v in values]
        except Exception:
            continue
    resolved: list[float] = []
    matched_freqs = 0
    for freq in freqs_mhz:
        key = _freq_metric_key(freq)
        if key in noise_samples and noise_samples[key]:
            resolved.extend(float(v) for v in noise_samples[key])
            matched_freqs += 1
            continue
        best_match = None
        best_delta = None
        for candidate, values in parsed_noise.items():
            delta = abs(candidate - float(freq))
            if delta > _AUTO_SQUELCH_FREQ_EPSILON_MHZ:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_match = values
        if best_match:
            resolved.extend(best_match)
            matched_freqs += 1
    return resolved, matched_freqs


def _clamp_squelch_dbfs(value: float) -> float:
    clamped = max(float(ANALOG_AUTO_SQUELCH_MIN_DBFS), float(value))
    clamped = min(float(ANALOG_AUTO_SQUELCH_MAX_DBFS), clamped)
    return round(clamped, 1)

# Decoder service helpers keyed by decoder name
_WX_START = {"acars": start_acars, "vdl2": start_vdl2, "radiosonde": start_radiosonde}
_WX_STOP = {"acars": stop_acars, "vdl2": stop_vdl2, "radiosonde": stop_radiosonde}
_WX_BIN = {"acars": "/usr/local/bin/acarsdec", "vdl2": "/usr/local/bin/dumpvdl2", "radiosonde": "/opt/radiosonde_auto_rx/auto_rx.py"}


def _start_wx_reader(decoder: str) -> None:
    """Start the wx data reader thread for the given decoder."""
    try:
        from .server_workers import start_wx_reader
    except ImportError:
        from ui.server_workers import start_wx_reader
    start_wx_reader(decoder)


def _stop_wx_reader() -> None:
    """Stop any active wx data reader thread."""
    try:
        from .server_workers import stop_wx_reader
    except ImportError:
        from ui.server_workers import stop_wx_reader
    stop_wx_reader()


def _normalize_hold_state(state):
    if not isinstance(state, dict):
        return {}
    if state.get("active") and state.get("target") in ("airband", "ground"):
        return {state["target"]: state}
    return state


def _state_path_candidates(path: str):
    raw = str(path or "").strip()
    seen = set()
    if raw:
        seen.add(raw)
        yield raw
    base = os.path.basename(raw) if raw else "airband_ui_state.json"
    try:
        uid = int(os.getuid())
    except Exception:
        logger.debug("actions: failed to resolve uid for state path candidates", exc_info=True)
        uid = 0
    for candidate in (
        os.path.join("/tmp", f"{base}.{uid}"),
        os.path.join("/tmp", base),
    ):
        candidate = str(candidate).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate


def _load_json_with_fallback(path: str, default):
    for candidate in _state_path_candidates(path):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            continue
        except Exception:
            logger.debug("actions: failed to load JSON state candidate %s", candidate, exc_info=True)
            continue
    return default


def _save_json_with_fallback(path: str, data) -> str:
    last_error = None
    for candidate in _state_path_candidates(path):
        tmp = ""
        try:
            os.makedirs(os.path.dirname(candidate) or ".", exist_ok=True)
            tmp = f"{candidate}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, candidate)
            return candidate
        except Exception as exc:
            last_error = exc
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                logger.debug("actions: failed to remove temp state file %s", tmp, exc_info=True)
            continue
    raise RuntimeError(f"unable to persist state file {path}: {last_error}")


def _clear_json_with_fallback(path: str) -> None:
    for candidate in _state_path_candidates(path):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("actions: failed to clear JSON state candidate %s", candidate, exc_info=True)


def _load_hold_state():
    """Load persisted hold state."""
    data = _load_json_with_fallback(HOLD_STATE_PATH, {})
    return _normalize_hold_state(data)


def _save_hold_state(data: dict) -> None:
    """Persist hold state to disk."""
    _save_json_with_fallback(HOLD_STATE_PATH, data)


def _clear_hold_state() -> None:
    """Clear hold state file."""
    _clear_json_with_fallback(HOLD_STATE_PATH)


def _has_active_hold(state: dict) -> bool:
    for entry in (state or {}).values():
        if isinstance(entry, dict) and entry.get("active"):
            return True
    return False


def _save_or_clear_hold_state(state: dict) -> None:
    if _has_active_hold(state):
        _save_hold_state(state)
        return
    _clear_hold_state()


_FREQ_BLOCK_RE = re.compile(r'(^\s*freqs\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)
_LABEL_BLOCK_RE = re.compile(r'(^\s*labels\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)


def _rewrite_single_freq(text: str, freq_val: float, label: str) -> str:
    """Rewrite all freq/label blocks to a single frequency."""
    def replace_freq(m):
        return f"{m.group(1)}{freq_val:.4f}{m.group(3)}"

    def replace_label(m):
        return f'{m.group(1)}"{label}"{m.group(3)}'

    text = _FREQ_BLOCK_RE.sub(replace_freq, text)
    text = _LABEL_BLOCK_RE.sub(replace_label, text)
    return text


def _load_tune_backup():
    return _load_json_with_fallback(TUNE_BACKUP_PATH, {})


def _save_tune_backup(data: dict):
    _save_json_with_fallback(TUNE_BACKUP_PATH, data)


def _clear_tune_backup():
    _clear_json_with_fallback(TUNE_BACKUP_PATH)


def _swap_symlink(link_path: str, target_path: str) -> None:
    link_path = str(link_path or "").strip()
    target_path = str(target_path or "").strip()
    if not link_path or not target_path:
        raise ValueError("missing symlink path")
    parent = os.path.dirname(link_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_link = os.path.join(parent, f".{os.path.basename(link_path)}.tmp")
    try:
        if os.path.lexists(tmp_link):
            os.unlink(tmp_link)
    except Exception:
        logger.debug("actions: failed to remove temp symlink %s", tmp_link, exc_info=True)
    os.symlink(target_path, tmp_link)
    os.replace(tmp_link, link_path)


def _parse_profile_ids(raw: Any) -> list[str]:
    if isinstance(raw, list):
        incoming = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                incoming = parsed if isinstance(parsed, list) else [text]
            except Exception:
                logger.debug("actions: failed to parse profile id list %r as JSON", text, exc_info=True)
                incoming = text.replace(";", ",").split(",")
        else:
            incoming = text.replace(";", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in incoming:
        value = str(item or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= _PROFILE_LOOP_MAX_SELECTED:
            break
    return out


def action_set_profile(profile_id: str, target: str, *, restart_service: bool = True) -> dict:
    """Action: Set a profile."""
    _, profiles_airband, profiles_ground = split_profiles()
    if target == "ground":
        conf_path = os.path.realpath(GROUND_CONFIG_PATH)
        profiles = [(p["id"], p["label"], p["path"]) for p in profiles_ground]
        target_symlink = GROUND_CONFIG_PATH
    else:
        conf_path = read_active_config_path()
        profiles = [(p["id"], p["label"], p["path"]) for p in profiles_airband]
        target_symlink = CONFIG_SYMLINK
    unit_restart = lambda: _select_analog_restart(target, reason="set_profile")

    if not profiles:
        return {"status": 400, "payload": {"ok": False, "error": "no profiles available"}}

    current_profile = guess_current_profile(conf_path, profiles)
    
    # Only proceed if profile actually changed
    if profile_id and profile_id != current_profile:
        # Resolve new profile path
        next_path = None
        for pid, _, path in profiles:
            if pid == profile_id:
                next_path = path
                break

        # Safety: refuse to activate a profile with an empty freqs list,
        # UNLESS it has ui_disabled=true (wx decoder or none_* profiles).
        if next_path and os.path.exists(next_path):
            try:
                with open(next_path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                is_disabled = bool(RE_UI_DISABLED.search(text) and "true" in RE_UI_DISABLED.search(text).group(0).lower())
                if not is_disabled:
                    freqs, _labels = parse_freqs_labels(text)
                    if not freqs:
                        return {"status": 400, "payload": {"ok": False, "error": "profile has no frequencies; add freqs and save first"}}
            except Exception:
                # If parsing fails, continue; rtl_airband will surface config errors.
                logger.debug(
                    "actions: failed to pre-validate frequencies for profile %s (%s)",
                    profile_id,
                    next_path,
                    exc_info=True,
                )

        # Determine wx decoder state for old and new profiles (ground only)
        old_decoder = None
        new_decoder = None
        if target == "ground":
            old_decoder = read_wx_decoder(os.path.realpath(GROUND_CONFIG_PATH))
            if next_path:
                new_decoder = read_wx_decoder(next_path)

            # Validate decoder binary is installed
            if new_decoder and new_decoder in _WX_BIN:
                bin_path = _WX_BIN[new_decoder]
                if not (shutil.which(bin_path) or os.path.isfile(bin_path)):
                    return {"status": 500, "payload": {
                        "ok": False,
                        "error": f"{bin_path} not found; install it to use {new_decoder} mode"
                    }}

        # Snapshot the current combined config text BEFORE swapping
        # symlinks, so we can roll back cleanly if the prospective
        # config fails validation.
        prev_combined_text = ""
        try:
            with open(COMBINED_CONFIG_PATH, "r", encoding="utf-8", errors="ignore") as fh:
                prev_combined_text = fh.read()
        except Exception:
            prev_combined_text = ""

        ok, changed = set_profile(profile_id, conf_path, profiles, target_symlink)
        if not ok:
            return {"status": 400, "payload": {"ok": False, "error": "unknown profile"}}
        combined_changed = False
        restart_ok = True
        restart_error = ""
        restart_needed = bool(changed or combined_changed)

        if restart_service:
            try:
                combined_changed = write_combined_config()
            except Exception as e:
                return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
            restart_needed = bool(changed or combined_changed)

            # Pre-flight: validate the prospective combined config
            # against currently-plugged devices BEFORE we trigger a
            # restart.  The 2026-05-26 bandscan_marine outage was caused
            # by a profile whose serial referenced a long-unplugged
            # RTL-SDR; rtl_airband crash-looped for ~20 minutes before
            # someone noticed.  Catching it here turns that class of
            # bug into a clean 409 with no restart and no audio outage.
            try:
                with open(COMBINED_CONFIG_PATH, "r", encoding="utf-8", errors="ignore") as fh:
                    new_combined_text = fh.read()
                validation = validate_combined_config_text(new_combined_text)
            except Exception:
                logger.warning(
                    "action_set_profile: validator raised; allowing apply "
                    "to proceed (sample-flow watchdog will catch failure)",
                    exc_info=True,
                )
                validation = {"ok": True, "issues": [], "checked_devices": []}

            if not validation["ok"]:
                # Roll back symlinks + combined config to the previous
                # known-good state before returning the rejection.
                logger.warning(
                    "action_set_profile: rejecting profile=%s target=%s: "
                    "%d issue(s) in prospective combined config",
                    profile_id, target, len(validation["issues"]),
                )
                rollback_ok = False
                rollback_detail = ""
                try:
                    if current_profile:
                        set_profile(current_profile, conf_path, profiles, target_symlink)
                    write_combined_config()
                    rollback_ok = True
                except Exception as exc:
                    rollback_detail = f"rollback failed: {exc}"
                    logger.error(
                        "action_set_profile: rollback failed after "
                        "validation rejection: %s", exc, exc_info=True,
                    )
                    # Best-effort: drop the previous combined text back
                    # directly so we don't leave rtl-airband pointed at
                    # the rejected config.
                    if prev_combined_text:
                        try:
                            with open(COMBINED_CONFIG_PATH, "w", encoding="utf-8") as fh:
                                fh.write(prev_combined_text)
                            rollback_ok = True
                        except Exception:
                            logger.error(
                                "action_set_profile: direct combined-config "
                                "restore also failed",
                                exc_info=True,
                            )
                return {
                    "status": 409,
                    "payload": {
                        "ok": False,
                        "error": "combined config validation failed",
                        "issues": validation["issues"],
                        "checked_devices": validation["checked_devices"],
                        "rolled_back": rollback_ok,
                        "rollback_detail": rollback_detail,
                        "rejected_profile": profile_id,
                        "restored_profile": current_profile or "",
                    },
                }

        payload = {
            "ok": True,
            "changed": bool(changed or combined_changed),
            "profile_switched": bool(changed),
            "combined_changed": bool(combined_changed),
        }
        # Preflight, two layers:
        #
        # 1. IMPLICIT (ui/device_ownership.py): parse the about-to-be-
        #    loaded combined config to discover which RTL serials it
        #    will claim, then stop any active decoder service whose
        #    env-resolved serial overlaps.  Catches the LIBUSB_BUSY
        #    crash-loop case where systemd's Restart=on-failure hides
        #    the failure behind a perpetually-active unit state.
        #
        # 2. EXPLICIT (ui/profile_metadata.py): honor the declarative
        #    ``requires_stop`` list for the incoming profile, even for
        #    units whose serial does not appear in the combined config
        #    (e.g. a wx decoder running on a non-target dongle that the
        #    operator nonetheless wants stopped on profile switch).
        device_release_actions: list[dict] = []
        if restart_service and restart_needed:
            try:
                try:
                    from .device_ownership import (
                        release_rtl_serials,
                        serials_from_combined_config,
                        _stop_unit as _release_stop_unit,
                    )
                    from .profile_metadata import (
                        get_requires_stop,
                        get_claims_serials,
                    )
                    from .systemd import unit_active as _unit_active
                except ImportError:
                    from ui.device_ownership import (  # type: ignore[no-redef]
                        release_rtl_serials,
                        serials_from_combined_config,
                        _stop_unit as _release_stop_unit,
                    )
                    from ui.profile_metadata import (  # type: ignore[no-redef]
                        get_requires_stop,
                        get_claims_serials,
                    )
                    from ui.systemd import unit_active as _unit_active  # type: ignore
                from ui.config import COMBINED_CONFIG_PATH as _COMBINED_PATH  # type: ignore
                # Layer 1: implicit (serial overlap).  Merge declared
                # claims_serials so dynamically-tuned profiles (acarsdec
                # picks its serial via env, not via rtl-airband conf)
                # still trigger displacement of other holders.
                target_serials = list(serials_from_combined_config(_COMBINED_PATH))
                declared_claims = get_claims_serials(profile_id)
                for s in declared_claims:
                    if s and s not in target_serials:
                        target_serials.append(s)
                if target_serials:
                    device_release_actions = release_rtl_serials(target_serials)
                # Layer 2: explicit (per-profile requires_stop).  Stop
                # any unit named in the metadata that is currently
                # active and has not already been stopped by Layer 1.
                already_stopped = {a["unit"] for a in device_release_actions if a.get("stopped")}
                for unit_name in get_requires_stop(profile_id):
                    if unit_name in already_stopped:
                        continue
                    if not _unit_active(unit_name):
                        continue
                    try:
                        ok_stop, err_stop = _release_stop_unit(unit_name, use_sudo=True)
                    except Exception as exc:
                        ok_stop, err_stop = False, str(exc)
                    device_release_actions.append({
                        "unit": unit_name,
                        "serial": "",
                        "source": "requires_stop",
                        "stopped": bool(ok_stop),
                        "error": (err_stop or "") if not ok_stop else "",
                    })
                    if ok_stop:
                        logger.info(
                            "actions: stopped %s for profile=%s (requires_stop metadata)",
                            unit_name, profile_id,
                        )
                    else:
                        logger.warning(
                            "actions: failed to stop %s for profile=%s: %s",
                            unit_name, profile_id, err_stop,
                        )
            except Exception as exc:
                logger.warning(
                    "actions: device-ownership preflight failed (continuing): %s",
                    exc,
                    exc_info=True,
                )
        if device_release_actions:
            payload["device_release"] = device_release_actions

        if restart_service and target == "ground" and (old_decoder or new_decoder):
            # Stop old decoder(s) if leaving one
            if old_decoder:
                for name, stop_fn in _WX_STOP.items():
                    try:
                        stop_fn()
                    except Exception:
                        pass
                _stop_wx_reader()

            # Always restart rtl-airband when transitioning decoder state
            if restart_needed:
                restart_ok, restart_error = unit_restart()
                time.sleep(0.5)  # let USB device release

            # Start new decoder(s) if entering one
            if new_decoder and new_decoder in _WX_START:
                if new_decoder == "acars":
                    # ACARS mode starts both acarsdec and dumpvdl2
                    dec_ok1, dec_err1 = _WX_START["acars"]()
                    dec_ok2, dec_err2 = _WX_START.get("vdl2", lambda: (True, ""))()
                    dec_ok = dec_ok1 or dec_ok2
                    dec_err = "; ".join(e for e in [dec_err1, dec_err2] if e)
                else:
                    dec_ok, dec_err = _WX_START[new_decoder]()
                if not dec_ok:
                    payload["ok"] = False
                    payload["error"] = f"{new_decoder} service failed to start: {dec_err}"
                else:
                    _start_wx_reader(new_decoder)
                payload["wx_decoder"] = new_decoder
        elif restart_service and restart_needed:
                restart_ok, restart_error = unit_restart()
        if restart_service:
            payload["restart_skipped"] = not restart_needed
        if restart_service and (restart_needed or old_decoder or new_decoder):
            payload["restart_ok"] = restart_ok
            if not restart_ok and restart_error:
                payload["restart_error"] = restart_error
        if not restart_service:
            payload["restart_skipped"] = True
        if changed:
            mark_analog_hit_cutoff(target)
        return {"status": 200, "payload": payload}
    
    # No profile change requested
    return {"status": 200, "payload": {"ok": True, "changed": False}}


def action_apply_controls(target: str, gain: float, squelch_mode: str, squelch_snr: float, squelch_dbfs: float) -> dict:
    """Action: Apply gain/squelch controls."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        conf_path = resolve_controls_path(target)
        changed = write_controls(conf_path, gain, squelch_mode, squelch_snr, squelch_dbfs)
        persist_managed_controls_override(
            target,
            conf_path,
            gain=gain,
            squelch_mode=squelch_mode,
            squelch_snr=squelch_snr,
            squelch_dbfs=squelch_dbfs,
        )
        combined_changed = write_combined_config()
        changed = changed or combined_changed
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    restart_ok = True
    restart_error = ""
    device_release_actions: list[dict] = []
    if changed:
        # Preflight: free RTL serials the new combined config claims
        # from active wx decoders before restarting rtl-airband.  See
        # ui/device_ownership.py for the discovery contract.
        try:
            try:
                from .device_ownership import (
                    release_rtl_serials,
                    serials_from_combined_config,
                )
            except ImportError:
                from ui.device_ownership import (  # type: ignore[no-redef]
                    release_rtl_serials,
                    serials_from_combined_config,
                )
            from ui.config import COMBINED_CONFIG_PATH as _COMBINED_PATH  # type: ignore
            target_serials = serials_from_combined_config(_COMBINED_PATH)
            if target_serials:
                device_release_actions = release_rtl_serials(target_serials)
        except Exception as exc:
            logger.warning(
                "actions: device-ownership preflight failed (continuing): %s",
                exc,
                exc_info=True,
            )
        restart_ok, restart_error = _select_analog_restart(target, reason="apply_controls")
    payload = {"ok": True, "changed": changed}
    if device_release_actions:
        payload["device_release"] = device_release_actions
    if changed:
        payload["restart_ok"] = restart_ok
        if not restart_ok and restart_error:
            payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_apply_batch(target: str, gain: float, squelch_mode: str, squelch_snr: float, squelch_dbfs: float, cutoff_hz: float) -> dict:
    """Apply gain/squelch and filter in a single restart."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        conf_path = resolve_controls_path(target)
        changed_controls = write_controls(conf_path, gain, squelch_mode, squelch_snr, squelch_dbfs)
        persist_managed_controls_override(
            target,
            conf_path,
            gain=gain,
            squelch_mode=squelch_mode,
            squelch_snr=squelch_snr,
            squelch_dbfs=squelch_dbfs,
        )
        changed_filter = write_filter(target, cutoff_hz)
        combined_changed = write_combined_config() if changed_controls else False
        changed = changed_controls or changed_filter or combined_changed
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    restart_ok = True
    restart_error = ""
    if changed:
        restart_ok, restart_error = _select_analog_restart(target, reason="apply_batch")
    payload = {"ok": True, "changed": changed}
    if changed:
        payload["restart_ok"] = restart_ok
        if not restart_ok and restart_error:
            payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_auto_squelch(targets: list[str] | None = None) -> dict:
    """Quickly estimate noise floor and apply recommended dBFS squelch."""
    ordered_targets: list[str] = []
    for candidate in (targets or ["airband", "ground"]):
        target = str(candidate or "").strip().lower()
        if target not in ("airband", "ground"):
            continue
        if target in ordered_targets:
            continue
        ordered_targets.append(target)
    if not ordered_targets:
        return {"status": 400, "payload": {"ok": False, "error": "no valid targets"}}

    sample_sec = max(1, int(ANALOG_AUTO_SQUELCH_SAMPLE_SEC))
    margin_db = float(ANALOG_AUTO_SQUELCH_MARGIN_DB)
    # Post-MA/SL-split each target writes to its own stats file.  Read the
    # correct one per target so noise floors come from the band were calibrating.
    noise_samples_by_target: dict[str, dict[str, list[float]]] = {}
    for _tgt in ordered_targets:
        _path = _stats_path_for_target(_tgt)
        try:
            noise_samples_by_target[_tgt] = _collect_dbfs_noise_samples(_path, sample_sec)
        except FileNotFoundError:
            return {
                "status": 503,
                "payload": {
                    "ok": False,
                    "error": f"stats file not found: {_path}",
                    "target": _tgt,
                },
            }
        except Exception as e:
            return {"status": 500, "payload": {"ok": False, "error": str(e), "target": _tgt}}

    if not any(noise_samples_by_target.values()):
        return {
            "status": 503,
            "payload": {
                "ok": False,
                "error": "no noise metrics available",
                "stats_paths": {t: _stats_path_for_target(t) for t in ordered_targets},
            },
        }

    payload_targets: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    changed_controls = False
    try:
        for target in ordered_targets:
            conf_path, freqs = _load_target_profile_freqs(target)
            gain, squelch_snr, current_dbfs, _mode = parse_controls(conf_path)
            noise_samples = noise_samples_by_target.get(target) or {}
            all_noise_values: list[float] = []
            for _vs in noise_samples.values():
                all_noise_values.extend(float(v) for v in _vs)
            matched_noise_values, matched_freqs = _resolve_noise_values_for_freqs(freqs, noise_samples)
            used_fallback_noise = False
            if not matched_noise_values and all_noise_values:
                matched_noise_values = list(all_noise_values)
                used_fallback_noise = True
                warnings.append(f"{target}: no direct frequency noise match; used global noise sample")

            noise_median = _median(matched_noise_values)
            if noise_median is None:
                suggested_dbfs = _clamp_squelch_dbfs(float(current_dbfs))
            else:
                suggested_dbfs = _clamp_squelch_dbfs(float(noise_median) + margin_db)

            changed = write_controls(
                conf_path,
                float(gain),
                "dbfs",
                float(squelch_snr),
                float(suggested_dbfs),
            )
            persist_managed_controls_override(
                target,
                conf_path,
                gain=float(gain),
                squelch_mode="dbfs",
                squelch_snr=float(squelch_snr),
                squelch_dbfs=float(suggested_dbfs),
            )
            changed_controls = changed_controls or bool(changed)
            payload_targets[target] = {
                "config_path": conf_path,
                "frequency_count": len(freqs),
                "matched_frequency_count": int(matched_freqs),
                "sample_count": len(matched_noise_values),
                "noise_dbfs_median": round(float(noise_median), 2) if noise_median is not None else None,
                "margin_db": margin_db,
                "current_squelch_dbfs": float(current_dbfs),
                "applied_squelch_dbfs": float(suggested_dbfs),
                "used_global_noise_fallback": bool(used_fallback_noise),
            }
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}

    restart_ok = True
    restart_error = ""
    combined_changed = False
    changed = bool(changed_controls)
    if changed_controls:
        try:
            combined_changed = bool(write_combined_config())
            changed = bool(changed_controls or combined_changed)
        except Exception as e:
            return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
        if changed:
            # Per-target restart: never bundle multiple bands into a single
            # airband-side restart.  Each target hits its own MA/SL service
            # so SL doesnt race MA at sdrplay_api_Open.
            for _tgt in ordered_targets:
                _ok, _err = _select_analog_restart(_tgt, reason="auto_squelch")
                if not _ok:
                    restart_ok = False
                    if _err:
                        restart_error = (
                            f"{restart_error}; {_tgt}: {_err}"
                            if restart_error else f"{_tgt}: {_err}"
                        )

    payload: dict[str, Any] = {
        "ok": True,
        "changed": bool(changed),
        "stats_path": stats_path,
        "sample_sec": int(sample_sec),
        "targets": payload_targets,
    }
    if changed:
        payload["restart_ok"] = bool(restart_ok)
        if restart_error:
            payload["restart_error"] = str(restart_error)
    if warnings:
        payload["warnings"] = warnings
    return {"status": 200, "payload": payload}


def action_apply_filter(target: str, cutoff_hz: float) -> dict:
    """Action: Apply noise filter."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        changed = write_filter(target, cutoff_hz)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    restart_ok = True
    restart_error = ""
    if changed:
        restart_ok, restart_error = _select_analog_restart(target, reason="apply_filter")
    payload = {"ok": True, "changed": changed}
    if changed:
        payload["restart_ok"] = restart_ok
        if not restart_ok and restart_error:
            payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def _select_analog_restart(target: str, *, reason: str = "unspecified"):
    """Pick the right restart function for the analog band based on what
    units actually exist.

    Pre-MA/SL-cutover (legacy state): rtl-airband.service is the only
    real analog unit; the new ``rtl-airband-airband.service`` and
    ``rtl-airband-ground.service`` aren't installed yet.  Route to
    the legacy ``restart_rtl()`` / ``restart_ground()``.

    Post-cutover: the new units exist and the legacy unit is masked.
    Route to the new sequenced-recovery functions that know about
    Master/Slave ordering and OP25 coordination.

    This runtime-dispatch keeps the same "Restart Airband" button
    working through the cutover without an intermediate UI redeploy.
    """
    try:
        try:
            from .systemd import (
                unit_exists,
                restart_rtl_airband,
                restart_rtl_ground,
            )
        except ImportError:  # pragma: no cover — packaging fallback
            from ui.systemd import (  # type: ignore[no-redef]
                unit_exists,
                restart_rtl_airband,
                restart_rtl_ground,
            )
    except Exception:
        # If even the import path is broken, fall through to the
        # legacy functions that have been working pre-split.
        unit_exists = None
        restart_rtl_airband = None
        restart_rtl_ground = None

    if target == "airband":
        if unit_exists and restart_rtl_airband and unit_exists("rtl-airband-airband"):
            return restart_rtl_airband(reason=reason)
        return restart_rtl(reason=reason)
    if target == "ground":
        if unit_exists and restart_rtl_ground and unit_exists("rtl-airband-ground"):
            return restart_rtl_ground(reason=reason)
        return restart_ground(reason=reason)
    raise ValueError(f"unknown analog target: {target!r}")


def action_restart(target: str) -> dict:
    """Action: Restart a scanner."""
    if target == "ground":
        ok, err = _select_analog_restart("ground")
    elif target == "airband":
        ok, err = _select_analog_restart("airband")
    elif target == "icecast":
        ok, err = restart_icecast()
    elif target == "keepalive":
        ok, err = restart_keepalive()
    elif target == "ui":
        ok, err = restart_ui()
    elif target == "digital":
        ok, err = restart_digital()
    elif target == "all":
        results = {
            "airband": _select_analog_restart("airband"),
            "ground": _select_analog_restart("ground"),
            "icecast": restart_icecast(),
            "keepalive": restart_keepalive(),
            "ui": restart_ui(),
            "digital": restart_digital(),
        }
        ok = all(v[0] for v in results.values())
        err = "; ".join(f"{k}:{v[1]}" for k, v in results.items() if v[1])
    else:
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    payload = {"ok": bool(ok)}
    if err:
        payload["restart_error"] = err
    return {"status": 200 if ok else 500, "payload": payload}


def action_usb_hub_reset() -> dict:
    """Action: reset known USB hub paths."""
    paths = _usb_hub_reset_paths()
    if not paths:
        return {"status": 400, "payload": {"ok": False, "error": "no hub paths configured"}}
    reset_paths: list[str] = []
    missing_paths: list[str] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        if not os.path.exists(path):
            missing_paths.append(path)
            continue
        ok, err = _reset_usb_hub_path(path)
        if ok:
            reset_paths.append(path)
        else:
            errors.append({"path": path, "error": err})
    payload = {
        "ok": bool(reset_paths) and not errors,
        "requested_paths": paths,
        "reset_paths": reset_paths,
        "missing_paths": missing_paths,
    }
    if errors:
        payload["errors"] = errors
    if not reset_paths and not errors:
        payload["error"] = "no configured hub paths found"
        return {"status": 404, "payload": payload}
    if errors:
        payload["error"] = "one or more hub resets failed"
        return {"status": 500, "payload": payload}
    return {"status": 200, "payload": payload}


def action_avoid(target: str) -> dict:
    """Action: Avoid the current frequency."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    conf_path = os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path()
    stop_rtl()
    try:
        freq, err = avoid_current_hit(conf_path, target)
    except Exception as e:
        start_rtl()
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    if err:
        start_rtl()
        return {"status": 400, "payload": {"ok": False, "error": err}}
    try:
        write_combined_config()
    except Exception as e:
        start_rtl()
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
    restart_ok, restart_error = _select_analog_restart(target, reason="avoid")
    payload = {"ok": True, "freq": f"{freq:.4f}", "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_avoid_clear(target: str) -> dict:
    """Action: Clear avoids."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    conf_path = os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path()
    stop_rtl()
    try:
        _, err = clear_avoids(conf_path, target)
    except Exception as e:
        start_rtl()
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    if err:
        start_rtl()
        return {"status": 400, "payload": {"ok": False, "error": err}}
    try:
        write_combined_config()
    except Exception as e:
        start_rtl()
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
    restart_ok, restart_error = _select_analog_restart(target, reason="avoid_clear")
    payload = {"ok": True, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def _write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _temp_config_dir_candidates():
    seen = set()
    for candidate in (
        str(os.getenv("AIRBAND_UI_TEMP_DIR", "") or "").strip(),
        os.path.dirname(str(TUNE_BACKUP_PATH or "").strip()),
        os.path.dirname(str(HOLD_STATE_PATH or "").strip()),
        "/tmp",
    ):
        path = str(candidate or "").strip()
        if not path:
            continue
        resolved = os.path.realpath(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        yield resolved


def _write_temp_config(target: str, purpose: str, text: str) -> str:
    prefix = f"airband_ui_{purpose}_{target}_"
    last_error = None
    for tmp_dir in _temp_config_dir_candidates():
        try:
            os.makedirs(tmp_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                prefix=prefix,
                suffix=".conf",
                dir=tmp_dir,
                delete=False,
            ) as handle:
                handle.write(text)
                return str(handle.name)
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"unable to write temporary config for target={target} purpose={purpose}: {last_error}"
    )


def _get_symlink_target(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def _resolve_config_target(target: str):
    """Resolve config path + symlink info for a target."""
    if target == "airband":
        symlink_path = CONFIG_SYMLINK if os.path.islink(CONFIG_SYMLINK) else None
        original_target = _get_symlink_target(CONFIG_SYMLINK) if symlink_path else None
        conf_path = original_target if symlink_path else read_active_config_path()
        return conf_path, symlink_path, original_target
    if target == "ground":
        symlink_path = GROUND_CONFIG_PATH if os.path.islink(GROUND_CONFIG_PATH) else None
        original_target = _get_symlink_target(GROUND_CONFIG_PATH) if symlink_path else None
        conf_path = original_target if symlink_path else os.path.realpath(GROUND_CONFIG_PATH)
        return conf_path, symlink_path, original_target
    return None, None, None


def action_hold_start(target: str, freq) -> dict:
    """Action: Enter hold by replacing target config with single frequency."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        freq_val = float(freq)
    except (TypeError, ValueError):
        return {"status": 400, "payload": {"ok": False, "error": "bad freq"}}

    state = _load_hold_state() or {}
    target_state = state.get(target) or {}
    if target_state.get("active"):
        return {"status": 400, "payload": {"ok": False, "error": f"already holding {target_state.get('freq')}"}}

    conf_path, symlink_path, original_target = _resolve_config_target(target)
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            original = f.read()
    except FileNotFoundError:
        return {"status": 400, "payload": {"ok": False, "error": "config not found"}}

    label = f"{freq_val:.4f}"
    try:
        new_text = _rewrite_single_freq(original, freq_val, label)
    except Exception as e:
        return {"status": 400, "payload": {"ok": False, "error": f"rewrite failed: {e}"}}

    temp_path = None
    if symlink_path:
        try:
            temp_path = _write_temp_config(target, "hold", new_text)
            _swap_symlink(symlink_path, temp_path)
        except Exception as e:
            return {"status": 500, "payload": {"ok": False, "error": f"write failed: {e}"}}
    else:
        try:
            _write_text(conf_path, new_text)
        except Exception as e:
            return {"status": 500, "payload": {"ok": False, "error": f"write failed: {e}"}}

    hold_state = {
        "active": True,
        "target": target,
        "freq": f"{freq_val:.4f}",
        "conf_path": conf_path,
        "symlink_path": symlink_path,
        "original_target": original_target,
        "temp_path": temp_path,
        "original_text": original,
        "ts": time.time(),
    }
    state[target] = hold_state
    try:
        _save_hold_state(state)
    except Exception:
        logger.debug("actions: failed to persist hold state", exc_info=True)

    try:
        write_combined_config()
    except Exception as e:
        # rollback to original config if combine fails
        try:
            if symlink_path and state.get(target):
                original_target = state[target].get("original_target")
                if original_target:
                    _swap_symlink(symlink_path, original_target)
            else:
                _write_text(conf_path, original)
            state.pop(target, None)
            _save_or_clear_hold_state(state)
        except Exception:
            logger.debug("actions: failed to roll back hold state after combine error", exc_info=True)
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_ok, restart_error = _select_analog_restart(target, reason="hold_start")
    payload = {"ok": True, "freq": f"{freq_val:.4f}", "target": target, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_hold_stop(target: str) -> dict:
    """Action: Exit hold and restore original config."""
    state = _load_hold_state() or {}
    target_state = state.get(target) or {}
    if not target_state.get("active"):
        return {"status": 200, "payload": {"ok": True, "restored": False}}

    conf_path = target_state.get("conf_path") or (os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path())
    original = target_state.get("original_text")
    symlink_path = target_state.get("symlink_path")
    original_target = target_state.get("original_target")
    temp_path = target_state.get("temp_path")
    if not conf_path or original is None:
        state.pop(target, None)
        _save_or_clear_hold_state(state)
        return {"status": 400, "payload": {"ok": False, "error": "hold state incomplete"}}

    try:
        if symlink_path and original_target:
            _swap_symlink(symlink_path, original_target)
            if temp_path:
                try:
                    os.remove(temp_path)
                except Exception:
                    logger.debug("actions: failed to remove temporary hold config %s", temp_path, exc_info=True)
        else:
            _write_text(conf_path, original)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"restore failed: {e}"}}

    state.pop(target, None)
    _save_or_clear_hold_state(state)

    try:
        write_combined_config()
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_ok, restart_error = _select_analog_restart(target, reason="hold_stop")
    payload = {"ok": True, "restored": True, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_tune(target: str, freq) -> dict:
    """Action: Directly tune target config to a single frequency (no auto-restore)."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    state = _load_hold_state() or {}
    if (state.get(target) or {}).get("active"):
        return {"status": 400, "payload": {"ok": False, "error": "cannot tune while hold is active"}}
    try:
        freq_val = float(freq)
    except (TypeError, ValueError):
        return {"status": 400, "payload": {"ok": False, "error": "bad freq"}}

    conf_path, symlink_path, original_target = _resolve_config_target(target)
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            original = f.read()
    except FileNotFoundError:
        return {"status": 400, "payload": {"ok": False, "error": "config not found"}}

    # Save backup for restore
    backup = _load_tune_backup()
    backup[target] = {
        "conf_path": conf_path,
        "original_text": original,
        "symlink_path": symlink_path,
        "original_target": original_target,
        "temp_path": None,
        "ts": time.time(),
    }
    try:
        _save_tune_backup(backup)
    except Exception:
        logger.debug("actions: failed to persist tune backup", exc_info=True)

    label = f"{freq_val:.4f}"
    try:
        new_text = _rewrite_single_freq(original, freq_val, label)
    except Exception as e:
        return {"status": 400, "payload": {"ok": False, "error": f"rewrite failed: {e}"}}

    try:
        if symlink_path:
            temp_path = _write_temp_config(target, "tune", new_text)
            backup[target]["temp_path"] = temp_path
            _save_tune_backup(backup)
            _swap_symlink(symlink_path, temp_path)
        else:
            _write_text(conf_path, new_text)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"write failed: {e}"}}

    try:
        write_combined_config()
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_ok, restart_error = _select_analog_restart(target, reason="tune")
    payload = {"ok": True, "freq": f"{freq_val:.4f}", "target": target, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_tune_restore(target: str) -> dict:
    """Restore config from tune backup."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    backup = _load_tune_backup()
    entry = backup.get(target)
    if not entry:
        return {"status": 400, "payload": {"ok": False, "error": "no tune backup"}}
    conf_path = entry.get("conf_path")
    original = entry.get("original_text")
    symlink_path = entry.get("symlink_path")
    original_target = entry.get("original_target")
    temp_path = entry.get("temp_path")
    if not conf_path or original is None:
        return {"status": 400, "payload": {"ok": False, "error": "invalid backup"}}
    try:
        if symlink_path and original_target:
            _swap_symlink(symlink_path, original_target)
            if temp_path:
                try:
                    os.remove(temp_path)
                except Exception:
                    logger.debug("actions: failed to remove temporary tune config %s", temp_path, exc_info=True)
        else:
            _write_text(conf_path, original)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"restore failed: {e}"}}
    backup.pop(target, None)
    try:
        _save_tune_backup(backup)
    except Exception:
        logger.debug("actions: failed to persist cleared tune backup", exc_info=True)
    try:
        write_combined_config()
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
    restart_ok, restart_error = _select_analog_restart(target, reason="tune_restore")
    payload = {"ok": True, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def execute_action(action: dict) -> dict:
    """Execute an action based on its type."""
    action_type = action.get("type")
    if action_type == "apply":
        return action_apply_controls(
            action.get("target"),
            action.get("gain"),
            action.get("squelch_mode"),
            action.get("squelch_snr"),
            action.get("squelch_dbfs"),
        )
    if action_type == "apply_batch":
        return action_apply_batch(
            action.get("target"),
            action.get("gain"),
            action.get("squelch_mode"),
            action.get("squelch_snr"),
            action.get("squelch_dbfs"),
            action.get("cutoff_hz"),
        )
    if action_type == "auto_squelch":
        return action_auto_squelch(action.get("targets"))
    if action_type == "filter":
        return action_apply_filter(action.get("target"), action.get("cutoff_hz"))
    if action_type == "profile":
        restart_service = action.get("restart_service", True)
        if isinstance(restart_service, str):
            restart_service = restart_service.strip().lower() not in ("0", "false", "no", "off")
        else:
            restart_service = bool(restart_service)
        return action_set_profile(
            action.get("profile"),
            action.get("target"),
            restart_service=restart_service,
        )
    if action_type == "restart":
        return action_restart(action.get("target"))
    if action_type == "usb_hub_reset":
        return action_usb_hub_reset()
    if action_type == "avoid":
        return action_avoid(action.get("target"))
    if action_type == "avoid_clear":
        return action_avoid_clear(action.get("target"))
    if action_type == "hold":
        mode = action.get("mode") or "start"
        if mode == "stop":
            return action_hold_stop(action.get("target"))
        return action_hold_start(action.get("target"), action.get("freq"))
    if action_type == "tune":
        return action_tune(action.get("target"), action.get("freq"))
    if action_type == "tune_restore":
        return action_tune_restore(action.get("target"))
    return {"status": 400, "payload": {"ok": False, "error": "unknown action"}}
