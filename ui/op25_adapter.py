"""OP25 digital backend adapter.

OP25 is a Python/gnuradio P25 decoder that natively supports multiple
simultaneous trunked systems — one receiver per RTL-SDR dongle.  This
adapter generates OP25 config files from the same ``systems.json`` and
``control_channels.txt`` profile data that SDRTrunk uses, manages the
OP25 systemd service, parses OP25 log output for call events, and polls
the OP25 HTTP status endpoint for health.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any

try:
    from .config import (
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_PROFILES_DIR,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_TERTIARY,
        DIGITAL_STREAM_MOUNT,
        ICECAST_HOST,
        ICECAST_PORT,
        OP25_DEFAULT_MODULATION,
        OP25_DEFAULT_OFFSET,
        OP25_DEFAULT_SAMPLE_RATE,
        OP25_LOG_PATH,
        OP25_MULTI_RX_PATH,
        OP25_RUNTIME_DIR,
        OP25_RX_PATH,
        OP25_SERVICE_NAME,
        OP25_STATUS_HOST,
        OP25_STATUS_PORT,
    )
    from .dongle_allocator import load_assignments
    from .systemd import restart_digital, unit_active
except ImportError:
    from ui.config import (  # type: ignore[no-redef]
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_PROFILES_DIR,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_TERTIARY,
        DIGITAL_STREAM_MOUNT,
        ICECAST_HOST,
        ICECAST_PORT,
        OP25_DEFAULT_MODULATION,
        OP25_DEFAULT_OFFSET,
        OP25_DEFAULT_SAMPLE_RATE,
        OP25_LOG_PATH,
        OP25_MULTI_RX_PATH,
        OP25_RUNTIME_DIR,
        OP25_RX_PATH,
        OP25_SERVICE_NAME,
        OP25_STATUS_HOST,
        OP25_STATUS_PORT,
    )
    from ui.dongle_allocator import load_assignments  # type: ignore[no-redef]
    from ui.systemd import restart_digital, unit_active  # type: ignore[no-redef]

# Late import to avoid circular dependency — digital.py defines the base classes.
try:
    from .digital import (
        _BaseDigitalAdapter,
        _normalize_name,
        _safe_realpath,
        validate_digital_profile_id,
        validate_digital_service_name,
    )
except ImportError:
    from ui.digital import (  # type: ignore[no-redef]
        _BaseDigitalAdapter,
        _normalize_name,
        _safe_realpath,
        validate_digital_profile_id,
        validate_digital_service_name,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OP25 log event patterns
# ---------------------------------------------------------------------------
# Example OP25 trunk log lines:
#   2026-03-26 14:05:32 tsbk_handler(): cc 851012500 tg 12345 freq 855462500
#   03/26/26 17:09:06.559189 voice update:  tg(3207), freq(857762500), slot(-), prio(3)
#   04/08/26 12:40:00.123 [0] voice update:  tg(3207), rid(0), freq(854.587500), slot(-), prio(3)
#   control channel: 851012500  status: locked
_RE_TSBK = re.compile(
    r"tsbk.*?tg\s*\(?\s*(\d+)\s*\)?\s*,?\s*freq\s*\(?\s*(\d[\d.]*)\s*\)?",
    re.IGNORECASE,
)
_RE_VOICE = re.compile(
    r"voice\s+(?:update|preempt|grant).*?tg\s*\(?\s*(\d+)\s*\)?\s*.*?freq\s*\(?\s*(\d[\d.]*)\s*\)?",
    re.IGNORECASE,
)
_RE_CC_STATUS = re.compile(
    r"control\s+channel.*?(\d{9,10}).*?status:\s*(\w+)",
    re.IGNORECASE,
)
_RE_ROOT_TSBKS = re.compile(r"\btsbks\s+(\d+)\b", re.IGNORECASE)

# RSPduo Master/Slave dual-tuner mode constrains the SoapySDRPlay3 driver
# to a small set of sample rates (max 2 MSps).  When the gr-osmosdr device
# args put the RSPduo in MA or SL mode, override the per-device rate to a
# valid value so the driver doesn't silently snap and produce garbage.
RSPDUO_DT_SAMPLE_RATE = 2_000_000


def _device_sample_rate_for_args(args: str, default_rate: int) -> int:
    """Pick a per-device sample rate compatible with the device's mode.

    RSPduo Single Tuner mode (mode=ST) and all non-RSPduo devices use the
    caller-supplied default.  RSPduo Master (mode=MA) or Slave (mode=SL)
    forces the rate to ``RSPDUO_DT_SAMPLE_RATE``.

    Match is on whole-token substrings so we don't false-positive on
    other args containing ``MA`` or ``SL``.
    """
    if "mode=MA" in args or "mode=SL" in args:
        return RSPDUO_DT_SAMPLE_RATE
    return default_rate


# RTL-SDR's gr-osmosdr backend exposes a single ``LNA`` gain element
# (0-49 dB).  SoapySDRPlay3 (used for RSPduo) does NOT expose ``LNA`` —
# its gain elements are ``IFGR`` (IF gain reduction, 20-59 dB; lower=more
# IF gain) and ``RFGR`` (RF gain reduction step, 0-9 on UHF; lower=more
# LNA gain).  Setting ``gains: "LNA:36"`` on a SDRplay device causes
# ``osmo_src.set_gain(36, "LNA")`` to silently no-op because no element
# by that name exists, leaving the device pinned at its driver-default
# AGC setpoint — usually too low to recover a distant P25 control
# channel.
_DEFAULT_GAINS_RTL = "LNA:36"
_DEFAULT_GAINS_SDRPLAY = "IFGR:40,RFGR:0"


def _is_sdrplay_args(args: str) -> bool:
    """Return True if *args* describes a SoapySDRPlay3 (RSPduo) source."""
    return "driver=sdrplay" in str(args or "").lower()


def _default_gains_for_args(args: str) -> str:
    """Pick a sensible default ``gains`` string for the given device args.

    See module-level note on ``_DEFAULT_GAINS_*``.  Returns the SDRplay
    default for ``soapy=,driver=sdrplay,...`` args; otherwise the
    legacy RTL-SDR default.  Per-system override via
    ``op25_system_config.json`` -> ``gains`` still wins when present.
    """
    if _is_sdrplay_args(args):
        return _DEFAULT_GAINS_SDRPLAY
    return _DEFAULT_GAINS_RTL


def _default_gain_mode_for_args(args: str) -> bool:
    """Pick a sensible ``gain_mode`` (AGC enable) per device backend.

    On SoapySDRPlay3 the driver-side IF AGC interacts unpredictably with
    manually-set IFGR — turning AGC on top of a manual IFGR setpoint
    causes the driver to override the user value at runtime.  For
    deterministic gain we disable ``gain_mode`` whenever the device is
    SDRplay so the IFGR/RFGR elements take effect verbatim.  RTL-SDR's
    osmosdr AGC implementation cooperates with manual LNA gain, so we
    leave it on for backwards compatibility.
    """
    return not _is_sdrplay_args(args)


# Gain element names recognised per backend.  Anything outside these sets
# is silently no-op'd by the underlying source plugin, leaving the device
# pinned at its driver default (the bug ``_resolve_gains_for_args``
# protects against).
_VALID_GAIN_ELEMENTS_RTL = frozenset({"LNA", "TUNER", "IF"})
_VALID_GAIN_ELEMENTS_SDRPLAY = frozenset({"IFGR", "RFGR"})


def _valid_gain_elements_for_args(args: str) -> frozenset[str]:
    """Return the set of gain-element names valid for the given device args."""
    if _is_sdrplay_args(args):
        return _VALID_GAIN_ELEMENTS_SDRPLAY
    return _VALID_GAIN_ELEMENTS_RTL


def _resolve_gains_for_args(args: str, override: str | None) -> str:
    """Resolve the multi_rx ``gains`` string for *args*, honouring *override*.

    If *override* is empty/None, return the backend-appropriate default.
    Otherwise, parse the override into ``Name:Value`` parts and keep only
    those whose ``Name`` is recognised by the device's backend (e.g.
    ``LNA`` is kept for RTL but dropped for SDRplay).  If *all* parts are
    dropped (legacy RTL-only profile pointing at an SDRplay device, e.g.
    ``"LNA:42"`` post-RSPduo migration), fall back to the default and log
    a warning so the operator sees that the override didn't apply.

    This protects against the silent-no-op trap where a profile carries
    ``"gains": "LNA:42"`` after the dongle behind a system migrated from
    RTL-SDR to RSPduo: SoapySDRPlay3 has no element named ``LNA``, so
    ``set_gain(42, "LNA")`` does nothing, and the device falls to its
    driver default — typically too low to recover a distant control
    channel.
    """
    default = _default_gains_for_args(args)
    text = str(override or "").strip()
    if not text:
        return default
    valid = _valid_gain_elements_for_args(args)
    kept: list[str] = []
    dropped: list[str] = []
    for raw in text.split(","):
        part = raw.strip()
        if not part or ":" not in part:
            continue
        name_raw, _, value = part.partition(":")
        name = name_raw.strip().upper()
        value = value.strip()
        if name in valid:
            # Normalise to canonical uppercase: gr-osmosdr / Soapy gain
            # element lookup is case-sensitive, so ``ifgr:25`` would
            # silently no-op the same way ``LNA:36`` does on SDRplay.
            kept.append(f"{name}:{value}")
        else:
            dropped.append(part)
    if not kept:
        logger.warning(
            "op25 gains override %r drops every element on this backend "
            "(args=%r, valid=%s); using default %r instead",
            text, args, sorted(valid), default,
        )
        return default
    if dropped:
        logger.warning(
            "op25 gains override %r contains elements unknown to backend "
            "(args=%r); dropping %s, keeping %s",
            text, args, dropped, kept,
        )
    return ",".join(kept)


def _resolve_gain_mode_for_args(args: str, override: bool | None) -> bool:
    """Resolve ``gain_mode`` for *args*, honouring an explicit override.

    Per-system overrides win when present.  Otherwise pick the
    backend-appropriate default (``False`` for SDRplay so manual IFGR
    sticks; ``True`` for RTL-SDR).
    """
    if override is not None:
        return bool(override)
    return _default_gain_mode_for_args(args)


def _master_first_device_order_key(device: dict) -> int:
    """Sort key that pulls RSPduo Master entries to the front of the device list.

    SoapySDRPlay3 requires that any RSPduo opened with ``mode=MA`` is
    initialised before its companion ``mode=SL``.  multi_rx.py opens
    devices in JSON list order, so the device list must order Master
    before Slave.  Other devices (RTL-SDR, RSPduo ST, etc.) keep their
    relative position via Python's stable sort.

    Returns 0 for Master, 2 for Slave, 1 for everything else.
    """
    args = str(device.get("args") or "")
    if "mode=MA" in args:
        return 0
    if "mode=SL" in args:
        return 2
    return 1


_OP25_ROOT_ACTIVITY_MAX_AGE_SEC = 15.0
_OP25_SITE_SELECTOR_STATE = "site_selector_state.json"
_OP25_SITE_SELECTOR_ACTION_COOLDOWN_MS = 30_000
_OP25_SITE_SELECTOR_SURVEY_DWELL_SEC = 15
_OP25_SITE_SELECTOR_SURVEY_MAX_SEC = 60
_OP25_SITE_SELECTOR_STALE_WINDOW_WINDOW_MS = 10 * 60 * 1000
_OP25_SITE_SELECTOR_POST_RESTART_GRACE_MS = 90_000  # 90s grace after restart before counting stale windows

_DEFAULT_SITE_POLICY = {
    "mode": "auto",
    "pinned_site_id": "",
    "preferred_site_ids": [],
    "avoid_site_ids": [],
    "min_dwell_sec": 120,
    "unproductive_window_sec": 300,
    "revisit_cooldown_sec": 180,
    "switch_margin": 20,
}

# Timestamps at the start of OP25 log lines can be ISO-like or US short-date.
_RE_TIMESTAMP = re.compile(
    r"((?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{2})\s+\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)"
)
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%y %H:%M:%S.%f",
    "%m/%d/%y %H:%M:%S",
)


def _hz_to_mhz(hz: int) -> float:
    return round(hz / 1_000_000, 4)


def _mhz_to_hz(mhz: float) -> int:
    return int(round(mhz * 1_000_000))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_utc(ms: int = 0) -> str:
    value = int(ms or _now_ms())
    return dt.datetime.fromtimestamp(value / 1000.0, tz=dt.timezone.utc).isoformat()


def _ms_from_iso(text: str) -> int:
    raw = str(text or "").strip()
    if not raw:
        return 0
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(raw).timestamp() * 1000)
    except Exception:
        return 0


def _parse_enabled(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# Config generation helpers
# ---------------------------------------------------------------------------

def _read_system_definitions(profile_dir: str) -> list[dict]:
    """Read ``systems.json`` from *profile_dir*.

    Returns a list of dicts with keys ``name`` and ``control_channels_hz``.
    Merges ``inject_sites`` from ``op25_system_config.json`` sidecar so
    extra sites survive scan-pool regeneration of systems.json on reboot.
    """
    systems_path = os.path.join(profile_dir, "systems.json")
    if not os.path.isfile(systems_path):
        return []
    try:
        with open(systems_path, "r", encoding="utf-8", errors="ignore") as f:
            payload = json.load(f)
    except Exception:
        return []

    raw_list = payload.get("systems") if isinstance(payload, dict) else payload
    if not isinstance(raw_list, list):
        return []

    overrides = _read_op25_system_config(profile_dir)

    systems: list[dict] = []
    seen: set[str] = set()
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("name") or item.get("id") or item.get("system") or ""
        ).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        channels: list[int] = []
        sites_raw = item.get("sites")
        if isinstance(sites_raw, list):
            seen_channels: set[int] = set()
            # PR1 compatibility path: flatten enabled candidate sites to one
            # control-channel union. PR2 will switch runtime generation to one
            # explicit active site per system instead of this union.
            for site in sites_raw:
                if not isinstance(site, dict):
                    continue
                if not _parse_enabled(site.get("enabled", True)):
                    continue
                site_channels = _parse_control_channels(
                    site.get("control_channels_hz")
                    or site.get("control_channels_mhz")
                    or site.get("control_channels")
                    or site.get("controls")
                )
                for hz in site_channels:
                    if hz not in seen_channels:
                        seen_channels.add(hz)
                        channels.append(hz)
            # Merge inject_sites from sidecar config
            injected_raw = (overrides.get(name) or {}).get("inject_sites")
            if isinstance(injected_raw, list):
                for raw_site in injected_raw:
                    if not isinstance(raw_site, dict):
                        continue
                    if not _parse_enabled(raw_site.get("enabled", True)):
                        continue
                    inj_channels = _parse_control_channels(
                        raw_site.get("control_channels_hz")
                        or raw_site.get("control_channels_mhz")
                        or raw_site.get("control_channels")
                        or raw_site.get("controls")
                    )
                    for hz in inj_channels:
                        if hz not in seen_channels:
                            seen_channels.add(hz)
                            channels.append(hz)
            channels.sort()
        else:
            channels_raw = (
                item.get("control_channels_hz")
                or item.get("control_channels_mhz")
                or item.get("control_channels")
                or item.get("controls")
            )
            channels = _parse_control_channels(channels_raw)
        if not channels:
            continue
        systems.append({"name": name, "control_channels_hz": channels})
    return systems


def _normalize_site_policy(raw_policy: Any, site_ids: set[str]) -> tuple[dict[str, Any], list[str]]:
    policy = dict(_DEFAULT_SITE_POLICY)
    warnings: list[str] = []
    raw_policy = raw_policy if isinstance(raw_policy, dict) else {}

    mode = str(raw_policy.get("mode") or policy["mode"]).strip().lower()
    policy["mode"] = mode if mode in {"auto", "manual"} else "auto"

    pinned_site_id = str(raw_policy.get("pinned_site_id") or "").strip()
    if pinned_site_id and pinned_site_id not in site_ids:
        warnings.append(f"unknown pinned_site_id {pinned_site_id}")
    policy["pinned_site_id"] = pinned_site_id

    def _norm_site_list(name: str) -> list[str]:
        seen: set[str] = set()
        items: list[str] = []
        raw_values = raw_policy.get(name)
        if isinstance(raw_values, str):
            raw_values = [token.strip() for token in raw_values.replace(",", " ").split() if token.strip()]
        if not isinstance(raw_values, list):
            raw_values = []
        for raw in raw_values:
            site_id = str(raw or "").strip()
            if not site_id or site_id in seen:
                continue
            seen.add(site_id)
            if site_id not in site_ids:
                warnings.append(f"unknown {name} entry {site_id}")
            items.append(site_id)
        return items

    policy["preferred_site_ids"] = _norm_site_list("preferred_site_ids")
    policy["avoid_site_ids"] = _norm_site_list("avoid_site_ids")

    for key in ("min_dwell_sec", "unproductive_window_sec", "revisit_cooldown_sec", "switch_margin"):
        default_value = int(_DEFAULT_SITE_POLICY[key])
        try:
            value = int(raw_policy.get(key, default_value))
        except Exception:
            value = default_value
        if value <= 0:
            value = default_value
        policy[key] = value

    return policy, warnings


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    if parsed != parsed:
        return None
    return parsed


def _site_radius_sort_value(value: Any) -> float:
    parsed = _optional_float(value)
    if parsed is None or parsed <= 0:
        return 0.0
    return float(parsed)


def _site_distance_sort_value(value: Any) -> float:
    parsed = _optional_float(value)
    if parsed is None or parsed < 0:
        return float("inf")
    return float(parsed)


def _selector_state_path(runtime_dir: str) -> str:
    override_path = str(os.getenv("OP25_SITE_SELECTOR_STATE_PATH") or "").strip()
    if override_path:
        return override_path
    override_dir = str(os.getenv("OP25_SITE_SELECTOR_STATE_DIR") or "").strip()
    if override_dir:
        return os.path.join(override_dir, _OP25_SITE_SELECTOR_STATE)
    runtime_real = os.path.realpath(str(runtime_dir or "").strip() or ".")
    if runtime_real.startswith("/run/"):
        home = os.path.expanduser("~")
        if home and home != "~":
            return os.path.join(home, ".local", "state", "scannerproject", "op25", _OP25_SITE_SELECTOR_STATE)
    return os.path.join(runtime_dir, _OP25_SITE_SELECTOR_STATE)


def _load_selector_state(runtime_dir: str) -> dict[str, Any]:
    path = _selector_state_path(runtime_dir)
    if not os.path.isfile(path):
        return {"systems": {}}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            payload = json.load(f) or {}
    except Exception:
        return {"systems": {}}
    if not isinstance(payload, dict):
        return {"systems": {}}
    systems = payload.get("systems")
    if not isinstance(systems, dict):
        payload["systems"] = {}
    return payload


def _save_selector_state(runtime_dir: str, payload: dict[str, Any]) -> None:
    path = _selector_state_path(runtime_dir)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".site-selector-state-",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _same_site_restart_enabled() -> bool:
    value = str(os.getenv("OP25_SITE_SELECTOR_SAME_SITE_RESTART", "1") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _state_system_key(profile_id: str, system_name: str) -> str:
    return f"{profile_id}::{system_name}"


def _normalize_runtime_system_definitions(
    profile_dir: str,
    *,
    op25_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    systems_path = os.path.join(profile_dir, "systems.json")
    if not os.path.isfile(systems_path):
        return []
    try:
        with open(systems_path, "r", encoding="utf-8", errors="ignore") as f:
            payload = json.load(f)
    except Exception:
        return []

    raw_list = payload.get("systems") if isinstance(payload, dict) else payload
    if not isinstance(raw_list, list):
        return []

    overrides = op25_overrides or {}
    systems: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or item.get("system") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        system_id = str(item.get("system_id") or "").strip()

        sites_raw = item.get("sites")
        normalized_sites: list[dict[str, Any]] = []
        if isinstance(sites_raw, list):
            for raw_site in sites_raw:
                if not isinstance(raw_site, dict):
                    continue
                site_channels = _parse_control_channels(
                    raw_site.get("control_channels_hz")
                    or raw_site.get("control_channels_mhz")
                    or raw_site.get("control_channels")
                    or raw_site.get("controls")
                )
                if not site_channels:
                    continue
                site_payload: dict[str, Any] = {
                    "site_id": str(raw_site.get("site_id") or "legacy:auto").strip() or "legacy:auto",
                    "site_name": str(raw_site.get("site_name") or "Legacy Control Channel Set").strip()
                    or "Legacy Control Channel Set",
                    "control_channels_hz": sorted(site_channels),
                    "enabled": _parse_enabled(raw_site.get("enabled", True)),
                }
                for field in ("latitude", "longitude", "radius", "distance_miles"):
                    parsed = _optional_float(raw_site.get(field))
                    if parsed is not None:
                        site_payload[field] = parsed
                normalized_sites.append(site_payload)
        else:
            legacy_channels = _parse_control_channels(
                item.get("control_channels_hz")
                or item.get("control_channels_mhz")
                or item.get("control_channels")
                or item.get("controls")
            )
            if legacy_channels:
                normalized_sites.append(
                    {
                        "site_id": "legacy:auto",
                        "site_name": "Legacy Control Channel Set",
                        "control_channels_hz": sorted(legacy_channels),
                        "enabled": True,
                    }
                )
        if not normalized_sites:
            continue

        # Merge inject_sites from op25_system_config.json sidecar so extra
        # sites survive scan-pool regeneration of systems.json on reboot.
        injected_raw = (overrides.get(name) or {}).get("inject_sites")
        if isinstance(injected_raw, list):
            existing_ids = {str(s["site_id"]) for s in normalized_sites}
            for raw_site in injected_raw:
                if not isinstance(raw_site, dict):
                    continue
                inj_id = str(raw_site.get("site_id") or "").strip()
                if not inj_id or inj_id in existing_ids:
                    continue
                inj_channels = _parse_control_channels(
                    raw_site.get("control_channels_hz")
                    or raw_site.get("control_channels_mhz")
                    or raw_site.get("control_channels")
                    or raw_site.get("controls")
                )
                if not inj_channels:
                    continue
                existing_ids.add(inj_id)
                site_payload = {
                    "site_id": inj_id,
                    "site_name": str(raw_site.get("site_name") or f"Injected {inj_id}").strip(),
                    "control_channels_hz": sorted(inj_channels),
                    "enabled": _parse_enabled(raw_site.get("enabled", True)),
                }
                for field in ("latitude", "longitude", "radius", "distance_miles"):
                    parsed = _optional_float(raw_site.get(field))
                    if parsed is not None:
                        site_payload[field] = parsed
                normalized_sites.append(site_payload)

        site_ids = {str(site["site_id"]) for site in normalized_sites}
        policy, policy_warnings = _normalize_site_policy(
            (overrides.get(name) or {}).get("site_policy"),
            site_ids,
        )
        systems.append(
            {
                "name": name,
                "system_id": system_id,
                "sites": normalized_sites,
                "site_policy": policy,
                "site_policy_warnings": policy_warnings,
                "active_site_id": "",
                "active_control_channels_hz": [],
            }
        )
    return systems


def _candidate_state_defaults(site: dict[str, Any]) -> dict[str, Any]:
    return {
        "site_id": str(site.get("site_id") or ""),
        "site_name": str(site.get("site_name") or ""),
        "latitude": site.get("latitude"),
        "longitude": site.get("longitude"),
        "radius": site.get("radius"),
        "distance_miles": site.get("distance_miles"),
        "enabled": _parse_enabled(site.get("enabled", True)),
        "state": "candidate",
        "score": 0,
        "control_locked": False,
        "control_decode_available": False,
        "last_tsbk_age_sec": None,
        "recent_any_grants": 0,
        "recent_monitored_tg_hits": 0,
        "exclusion_reason": "",
        "demotion_reason": "",
    }


def _canonical_site_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [row for row in rows if isinstance(row, dict)],
        key=lambda row: (
            -_site_radius_sort_value(row.get("radius")),
            _site_distance_sort_value(row.get("distance_miles")),
            str(row.get("site_name") or "").strip().lower(),
            str(row.get("site_id") or "").strip().lower(),
        ),
    )


def _ordered_control_channels_for_state(
    raw_channels: list[Any],
    selected_control_hz: int,
) -> list[int]:
    channels: list[int] = []
    seen: set[int] = set()
    for value in raw_channels or []:
        try:
            hz = int(value)
        except Exception:
            continue
        if hz <= 0 or hz in seen:
            continue
        seen.add(hz)
        channels.append(hz)
    if selected_control_hz > 0 and selected_control_hz in channels:
        return [selected_control_hz] + [hz for hz in channels if hz != selected_control_hz]
    return channels


def _initial_selector_system_state(system: dict[str, Any]) -> dict[str, Any]:
    return {
        "selected_site_id": "",
        "selected_site_name": "",
        "selected_control_frequency_hz": 0,
        "selection_mode": "legacy",
        "reason_code": "",
        "reason_text": "",
        "last_switch_time": "",
        "switch_count": 0,
        "same_site_restart_count": 0,
        "site_switch_restart_count": 0,
        "generic_restart_count": 0,
        "stale_window_count": 0,
        "current_site_since": "",
        "unproductive_since": "",
        "revisit_block_until": {},
        "candidates": [_candidate_state_defaults(site) for site in system.get("sites") or []],
        "_last_restart_time_ms": 0,
        "_last_stale_window_time_ms": 0,
        "_stale_window_times_ms": [],
        "_survey_started_at_ms": 0,
        "_survey_candidate_index": 0,
        "_survey_completed": False,
    }


def _hydrate_runtime_systems_for_config(
    profile_dir: str,
    runtime_dir: str,
    *,
    op25_overrides: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    profile_id = os.path.basename(profile_dir.rstrip(os.sep))
    systems = _normalize_runtime_system_definitions(profile_dir, op25_overrides=op25_overrides)
    state = _load_selector_state(runtime_dir)
    systems_state = state.setdefault("systems", {})
    changed = False

    for system in systems:
        key = _state_system_key(profile_id, system["name"])
        sys_state = systems_state.get(key)
        if not isinstance(sys_state, dict):
            sys_state = _initial_selector_system_state(system)
            systems_state[key] = sys_state
            changed = True
        enabled_sites = [site for site in system["sites"] if _parse_enabled(site.get("enabled", True))]
        is_legacy_single_site = (
            len(system.get("sites") or []) == 1
            and str((system.get("sites") or [{}])[0].get("site_id") or "") == "legacy:auto"
        )
        selected_site = None
        policy = system["site_policy"]
        pinned_site_id = str(policy.get("pinned_site_id") or "").strip()
        if pinned_site_id:
            selected_site = next(
                (site for site in enabled_sites if str(site.get("site_id") or "") == pinned_site_id),
                None,
            )
            if selected_site is not None:
                sys_state["selection_mode"] = "pinned"
                sys_state["reason_code"] = "pinned_site_selected"
                sys_state["reason_text"] = f"Pinned site {selected_site['site_name']} selected"
        if selected_site is None:
            current_site_id = str(sys_state.get("selected_site_id") or "").strip()
            if current_site_id:
                selected_site = next(
                    (site for site in enabled_sites if str(site.get("site_id") or "") == current_site_id),
                    None,
                )
        if selected_site is None:
            preferred_ids = [str(item or "").strip() for item in policy.get("preferred_site_ids") or [] if str(item or "").strip()]
            for preferred_id in preferred_ids:
                selected_site = next(
                    (site for site in enabled_sites if str(site.get("site_id") or "") == preferred_id),
                    None,
                )
                if selected_site is not None:
                    sys_state["selection_mode"] = "preferred"
                    sys_state["reason_code"] = "preferred_site_selected"
                    sys_state["reason_text"] = f"Preferred site {selected_site['site_name']} selected"
                    break
        if selected_site is None and enabled_sites:
            ordered_enabled_sites = _canonical_site_order(enabled_sites)
            selected_site = ordered_enabled_sites[0]
            if is_legacy_single_site:
                sys_state["selection_mode"] = "legacy"
                sys_state["reason_code"] = "legacy_single_site"
                sys_state["reason_text"] = "Legacy control-channel set selected"
            elif len(enabled_sites) > 1 and str(policy.get("mode") or "auto") == "auto" and not pinned_site_id:
                sys_state["selection_mode"] = "survey"
                sys_state["reason_code"] = "survey_initial"
                sys_state["reason_text"] = f"Initial survey candidate {selected_site['site_name']}"
                if not int(sys_state.get("_survey_started_at_ms") or 0):
                    sys_state["_survey_started_at_ms"] = _now_ms()
            else:
                sys_state["selection_mode"] = "fallback"
                sys_state["reason_code"] = "fallback_first_enabled"
                sys_state["reason_text"] = f"First enabled site {selected_site['site_name']} selected"
        if selected_site is None:
            system["active_site_id"] = ""
            system["active_control_channels_hz"] = []
            sys_state["selected_site_id"] = ""
            sys_state["selected_site_name"] = ""
            sys_state["reason_code"] = "no_valid_site"
            sys_state["reason_text"] = "No enabled sites available"
            changed = True
            continue

        selected_site_id = str(selected_site.get("site_id") or "")
        if str(sys_state.get("selected_site_id") or "") != selected_site_id:
            sys_state["selected_site_id"] = selected_site_id
            sys_state["selected_site_name"] = str(selected_site.get("site_name") or "")
            if not str(sys_state.get("last_switch_time") or ""):
                now_iso = _iso_utc()
                sys_state["last_switch_time"] = now_iso
                sys_state["current_site_since"] = now_iso
            changed = True
        elif not str(sys_state.get("selected_site_name") or "").strip():
            sys_state["selected_site_name"] = str(selected_site.get("site_name") or "")
            changed = True
        if not str(sys_state.get("current_site_since") or "").strip():
            sys_state["current_site_since"] = str(sys_state.get("last_switch_time") or _iso_utc())
            changed = True
        ordered_channels = _ordered_control_channels_for_state(
            list(selected_site.get("control_channels_hz") or []),
            int(sys_state.get("selected_control_frequency_hz") or 0),
        )
        selected_control_hz = ordered_channels[0] if ordered_channels else 0
        if int(sys_state.get("selected_control_frequency_hz") or 0) != selected_control_hz:
            sys_state["selected_control_frequency_hz"] = selected_control_hz
            changed = True
        system["active_site_id"] = selected_site_id
        system["active_control_channels_hz"] = ordered_channels
        if not system["active_control_channels_hz"]:
            sys_state["reason_code"] = "no_valid_site"
            sys_state["reason_text"] = "Selected site has no control channels"

    if changed:
        _save_selector_state(runtime_dir, state)
    return systems, state


def _flatten_active_runtime_systems(runtime_systems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    systems: list[dict[str, Any]] = []
    for system in runtime_systems:
        channels = _ordered_control_channels_for_state(
            list(system.get("active_control_channels_hz") or []),
            0,
        )
        if not channels:
            continue
        systems.append(
            {
                "name": str(system.get("name") or ""),
                "control_channels_hz": channels,
            }
        )
    return systems


def _candidate_is_unhealthy(candidate: dict[str, Any]) -> bool:
    if not _parse_enabled(candidate.get("enabled", True)):
        return True
    if not bool(candidate.get("control_locked")):
        return True
    if not bool(candidate.get("control_decode_available")):
        return True
    age = candidate.get("last_tsbk_age_sec")
    if age is None:
        return True
    try:
        return float(age) > 60.0
    except Exception:
        return True


def _candidate_cooldown_active(candidate: dict[str, Any], *, now_ms: int) -> bool:
    until_ms = int(candidate.get("_revisit_block_until_ms") or 0)
    return until_ms > now_ms


def _compute_candidate_score(candidate: dict[str, Any], system: dict[str, Any], *, now_ms: int) -> int:
    site_id = str(candidate.get("site_id") or "")
    policy = system.get("site_policy") or {}
    score = 0
    enabled = _parse_enabled(candidate.get("enabled", True))
    if not enabled:
        return -100000
    if site_id and site_id == str(policy.get("pinned_site_id") or "").strip():
        score += 1000
    if site_id in set(policy.get("preferred_site_ids") or []):
        score += 100
    if site_id in set(policy.get("avoid_site_ids") or []):
        score -= 80

    if bool(candidate.get("control_locked")):
        score += 40
    if bool(candidate.get("control_decode_available")):
        score += 20
    age = candidate.get("last_tsbk_age_sec")
    if age is None:
        score -= 20
    else:
        try:
            age_value = float(age)
        except Exception:
            age_value = None
        if age_value is None:
            score -= 20
        else:
            if age_value > 60:
                score -= 100
            elif age_value > 30:
                score -= 60
            elif age_value > 15:
                score -= 30

    recent_any = int(candidate.get("recent_any_grants") or 0)
    recent_monitored = int(candidate.get("recent_monitored_tg_hits") or 0)
    score += min(20, recent_any)
    if recent_monitored > 0:
        score += 60
    if recent_monitored >= 3:
        score += 10
    if _candidate_cooldown_active(candidate, now_ms=now_ms):
        score -= 50
    return int(score)


def _parse_control_channels(raw) -> list[int]:
    """Parse a list of control channel values to Hz."""
    if raw is None:
        return []
    if isinstance(raw, (int, float)):
        raw = [raw]
    if isinstance(raw, str):
        raw = [tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()]
    if not isinstance(raw, list):
        return []
    channels: list[int] = []
    seen: set[int] = set()
    for val in raw:
        text = str(val).strip()
        if not text:
            continue
        try:
            fval = float(text)
        except ValueError:
            continue
        # Heuristic: values < 10000 are MHz, otherwise Hz.
        hz = int(round(fval * 1_000_000)) if fval < 10_000 else int(round(fval))
        if hz > 0 and hz not in seen:
            seen.add(hz)
            channels.append(hz)
    return channels


def _read_op25_system_config(profile_dir: str) -> dict:
    """Read optional ``op25_system_config.json`` sidecar from *profile_dir*.

    Returns a dict keyed by system name with per-system OP25 overrides
    (nac, modulation, etc.).  Returns ``{}`` if the file is absent.
    """
    path = os.path.join(profile_dir, "op25_system_config.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _read_talkgroup_labels(profile_dir: str) -> dict[str, str]:
    """Read talkgroup CSV from *profile_dir*.

    Returns ``{decimal_tgid_str: label}``.

    Supports two CSV layouts:
      - ``DEC,HEX,Mode,Alpha Tag,...``  (profile_editor / sidecar)
      - ``DEC,Mode,Alpha Tag,...``      (_render_talkgroups_text)
    Detects the header and picks the "Alpha Tag" column.
    Falls back to column 1 for simple ``TGID<TAB>Label`` TSV files.
    """
    labels: dict[str, str] = {}
    for name in ("talkgroups.csv", "talkgroups.tsv"):
        path = os.path.join(profile_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                alpha_col = 1  # default: second column
                for lineno, line in enumerate(f):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = re.split(r"[,\t]", line)
                    # Detect header row and find Alpha Tag column.
                    if lineno == 0 and parts[0].strip().upper() in ("DEC", "TGID"):
                        lower_parts = [p.strip().lower() for p in parts]
                        if "alpha tag" in lower_parts:
                            alpha_col = lower_parts.index("alpha tag")
                        elif "description" in lower_parts:
                            alpha_col = lower_parts.index("description")
                        continue  # skip header
                    tgid = parts[0].strip()
                    if not tgid.isdigit():
                        continue
                    label = parts[alpha_col].strip() if alpha_col < len(parts) else ""
                    if not label:
                        # Fallback: try other columns for a non-hex label.
                        for p in parts[1:]:
                            candidate = p.strip()
                            if candidate and not candidate.isalnum():
                                label = candidate
                                break
                            if candidate and not all(c in "0123456789abcdefABCDEF" for c in candidate):
                                label = candidate
                                break
                    if tgid and label:
                        labels[tgid] = label
        except Exception:
            pass
    return labels


def _read_talkgroup_display_metadata(profile_dir: str) -> dict[str, dict[str, str]]:
    """Read grouped talkgroup display metadata from *profile_dir*.

    Returns ``{decimal_tgid_str: {label, agency, department, label_full}}``.
    Falls back to plain labels when grouped metadata is unavailable.
    """
    metadata: dict[str, dict[str, str]] = {}
    grouped_path = os.path.join(profile_dir, "talkgroups_with_group.csv")
    if os.path.isfile(grouped_path):
        try:
            with open(grouped_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not isinstance(row, dict):
                        continue
                    tgid = str(
                        row.get("DEC")
                        or row.get("Tgid")
                        or row.get("TGID")
                        or row.get("Dec")
                        or ""
                    ).strip()
                    if not tgid.isdigit():
                        continue
                    label = str(
                        row.get("Alpha Tag")
                        or row.get("alpha tag")
                        or row.get("Description")
                        or row.get("description")
                        or ""
                    ).strip()
                    agency = str(row.get("Group") or row.get("group") or "").strip()
                    department = label
                    entry = {
                        "label": label,
                        "agency": agency,
                        "department": department,
                        "label_full": "",
                    }
                    if agency and department:
                        entry["label_full"] = f"{agency} - {department}"
                    else:
                        entry["label_full"] = department or agency
                    metadata[tgid] = entry
        except Exception:
            metadata = {}
    if metadata:
        return metadata

    for tgid, label in _read_talkgroup_labels(profile_dir).items():
        clean = str(label or "").strip()
        metadata[tgid] = {
            "label": clean,
            "agency": "",
            "department": clean,
            "label_full": clean,
        }
    return metadata


def generate_trunk_tsv(
    systems: list[dict],
    dongle_assignments: dict | None = None,
    op25_overrides: dict | None = None,
    tgid_tags_path: str = "",
) -> str:
    """Generate OP25 trunk.tsv content from system definitions.

    OP25 expects a TSV with a header row and quoted fields.  Columns:
    ``Sysname  Control Channel List  Offset  NAC  Modulation  TGID Tags File  Whitelist  Blacklist  Center Frequency``

    Control channels are in MHz, comma-separated.
    """
    overrides = op25_overrides or {}
    assignments = dongle_assignments or {}
    
    def _q(val: str) -> str:
        return f'"{val}"'

    header = "\t".join([
        _q("Sysname"), _q("Control Channel List"), _q("Offset"),
        _q("NAC"), _q("Modulation"), _q("TGID Tags File"),
        _q("Whitelist"), _q("Blacklist"), _q("Center Frequency"),
    ])

    lines: list[str] = [header]
    for sys in systems:
        name = sys["name"]
        channels = sys["control_channels_hz"]
        if not channels:
            continue
        # OP25 expects MHz, comma-separated for all control channels.
        cc_mhz = ",".join(f"{hz / 1e6:.5f}" for hz in channels)
        sys_overrides = overrides.get(name) or {}
        nac = str(sys_overrides.get("nac", "0")).strip()
        modulation = str(
            sys_overrides.get("modulation", OP25_DEFAULT_MODULATION)
        ).strip().lower()
        offset = str(sys_overrides.get("offset", "0")).strip()
        tgid_file = str(sys_overrides.get("tgid_tags_file", tgid_tags_path)).strip()
        whitelist = str(sys_overrides.get("whitelist", "")).strip()
        blacklist = str(sys_overrides.get("blacklist", "")).strip()
        center_freq = str(sys_overrides.get("center_frequency", "")).strip()
        row = "\t".join([
            _q(name), _q(cc_mhz), _q(offset), _q(nac), _q(modulation),
            _q(tgid_file), _q(whitelist), _q(blacklist), _q(center_freq),
        ])
        lines.append(row)
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""


def generate_tgid_tags_tsv(labels: dict[str, str]) -> str:
    """Generate OP25 tgid_tags.tsv from talkgroup labels.

    Format: ``TGID<TAB>Tag``
    """
    lines: list[str] = []
    for tgid, label in sorted(labels.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        lines.append(f"{tgid}\t{label}")
    return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# multi_rx.py JSON config generation
# ---------------------------------------------------------------------------

# Default UDP audio base port.  Channel N gets BASE + N*2.
_UDP_AUDIO_BASE_PORT = 23456


def generate_multi_rx_config(
    systems: list[dict],
    dongle_map: dict[str, str],
    *,
    dongle_args_map: dict[str, str] | None = None,
    traffic_dongle_serial: str = "",
    traffic_system_name: str = "",
    traffic_dongle_serial_2: str = "",
    traffic_system_name_2: str = "",
    op25_overrides: dict | None = None,
    tgid_tags_path: str = "",
    http_port: int = 8080,
    udp_audio_base_port: int = _UDP_AUDIO_BASE_PORT,
    sample_rate: int = OP25_DEFAULT_SAMPLE_RATE,
    offset: int = OP25_DEFAULT_OFFSET,
) -> dict:
    """Generate a multi_rx.py JSON config for all systems + optional traffic follower(s).

    traffic_dongle_serial / traffic_system_name: first traffic follower (sdr_traffic),
    follows systems[0] by default or whichever system has traffic_priority set.
    traffic_dongle_serial_2 / traffic_system_name_2: optional second traffic follower
    (sdr_traffic2), follows systems[1] by default. Used when a spare dongle (e.g. VDL2
    dongle when VDL2 is inactive) is available to give each trunked system its own
    dedicated voice follower. System names are resolved dynamically — never hardcoded.
    """
    overrides = op25_overrides or {}
    arg_map = dongle_args_map or {}

    devices: list[dict] = []
    channels: list[dict] = []
    trunking_chans: list[dict] = []
    udp_port_idx = 0

    for idx, sys_def in enumerate(systems):
        name = sys_def["name"]
        cc_hz = sys_def["control_channels_hz"]
        if not cc_hz:
            continue
        serial = dongle_map.get(name, "")
        if not serial:
            continue

        sys_over = overrides.get(name) or {}
        modulation = str(sys_over.get("modulation", OP25_DEFAULT_MODULATION)).strip().lower()
        nac = str(sys_over.get("nac", "0")).strip()
        center_hz = int(cc_hz[0])

        dev_name = f"sdr{idx}"
        dev_args = str(arg_map.get(serial) or f"rtl={serial}")
        dev_gains = _resolve_gains_for_args(dev_args, sys_over.get("gains"))
        dev_gain_mode = _resolve_gain_mode_for_args(dev_args, sys_over.get("gain_mode"))
        devices.append({
            "name": dev_name,
            "args": dev_args,
            "rate": _device_sample_rate_for_args(dev_args, sample_rate),
            "frequency": center_hz,
            "offset": offset,
            "ppm": 0.0,
            "gains": dev_gains,
            "gain_mode": dev_gain_mode,
            "tunable": True,
        })

        udp_port = udp_audio_base_port + udp_port_idx * 2
        channels.append({
            "name": f"ch_{name}",
            "device": dev_name,
            "trunking_sysname": name,
            "demod_type": modulation,
            "filter_type": "rc",
            "if_rate": 24000,
            "symbol_rate": 4800,
            "destination": f"udp://127.0.0.1:{udp_port}",
            "enable_analog": "off",
        })
        udp_port_idx += 1

        trunking_chan: dict = {
            "sysname": name,
            "control_channel_list": ",".join(f"{hz / 1e6:.5f}" for hz in cc_hz),
            "nac": nac,
        }
        if tgid_tags_path:
            trunking_chan["tgid_tags_file"] = tgid_tags_path
        if sys_over.get("whitelist"):
            trunking_chan["whitelist"] = str(sys_over["whitelist"])
        if sys_over.get("blacklist"):
            trunking_chan["blacklist"] = str(sys_over["blacklist"])
        trunking_chans.append(trunking_chan)

    if traffic_dongle_serial:
        target_sys = traffic_system_name or (systems[0]["name"] if systems else "")
        if target_sys:
            target_cc_hz = 0
            target_mod = OP25_DEFAULT_MODULATION
            for sys_def in systems:
                if sys_def["name"] == target_sys:
                    if sys_def["control_channels_hz"]:
                        target_cc_hz = int(sys_def["control_channels_hz"][0])
                    sys_over = overrides.get(target_sys) or {}
                    target_mod = str(sys_over.get("modulation", OP25_DEFAULT_MODULATION)).strip().lower()
                    break
            if target_cc_hz:
                traffic_sys_over = overrides.get(target_sys) or {}
                traffic_args = str(arg_map.get(traffic_dongle_serial) or f"rtl={traffic_dongle_serial}")
                traffic_gains = _resolve_gains_for_args(
                    traffic_args, traffic_sys_over.get("gains")
                )
                traffic_gain_mode = _resolve_gain_mode_for_args(
                    traffic_args, traffic_sys_over.get("gain_mode")
                )
                devices.append({
                    "name": "sdr_traffic",
                    "args": traffic_args,
                    "rate": _device_sample_rate_for_args(traffic_args, sample_rate),
                    "frequency": target_cc_hz,
                    "offset": offset,
                    "ppm": 0.0,
                    "gains": traffic_gains,
                    "gain_mode": traffic_gain_mode,
                    "tunable": True,
                })
                udp_port = udp_audio_base_port + udp_port_idx * 2
                channels.append({
                    "name": f"traffic_{target_sys}",
                    "device": "sdr_traffic",
                    "trunking_sysname": target_sys,
                    "demod_type": target_mod,
                    "filter_type": "rc",
                    "if_rate": 24000,
                    "symbol_rate": 4800,
                    "destination": f"udp://127.0.0.1:{udp_port}",
                    "enable_analog": "off",
                })
                udp_port_idx += 1
                logger.debug(
                    "generate_multi_rx_config: sdr_traffic serial=%s -> system=%s port=%d",
                    traffic_dongle_serial, target_sys, udp_port,
                )

    if traffic_dongle_serial_2:
        # Default: second traffic follower targets systems[1] (system-agnostic, never hardcoded).
        target_sys_2 = traffic_system_name_2 or (
            systems[1]["name"] if len(systems) > 1 else (systems[0]["name"] if systems else "")
        )
        if target_sys_2:
            target_cc_hz_2 = 0
            target_mod_2 = OP25_DEFAULT_MODULATION
            for sys_def in systems:
                if sys_def["name"] == target_sys_2:
                    if sys_def["control_channels_hz"]:
                        target_cc_hz_2 = int(sys_def["control_channels_hz"][0])
                    sys_over_2 = overrides.get(target_sys_2) or {}
                    target_mod_2 = str(sys_over_2.get("modulation", OP25_DEFAULT_MODULATION)).strip().lower()
                    break
            if target_cc_hz_2:
                traffic_sys_over_2 = overrides.get(target_sys_2) or {}
                traffic2_args = str(arg_map.get(traffic_dongle_serial_2) or f"rtl={traffic_dongle_serial_2}")
                traffic_gains_2 = _resolve_gains_for_args(
                    traffic2_args, traffic_sys_over_2.get("gains")
                )
                traffic_gain_mode_2 = _resolve_gain_mode_for_args(
                    traffic2_args, traffic_sys_over_2.get("gain_mode")
                )
                devices.append({
                    "name": "sdr_traffic2",
                    "args": traffic2_args,
                    "rate": _device_sample_rate_for_args(traffic2_args, sample_rate),
                    "frequency": target_cc_hz_2,
                    "offset": offset,
                    "ppm": 0.0,
                    "gains": traffic_gains_2,
                    "gain_mode": traffic_gain_mode_2,
                    "tunable": True,
                })
                udp_port = udp_audio_base_port + udp_port_idx * 2
                channels.append({
                    "name": f"traffic2_{target_sys_2}",
                    "device": "sdr_traffic2",
                    "trunking_sysname": target_sys_2,
                    "demod_type": target_mod_2,
                    "filter_type": "rc",
                    "if_rate": 24000,
                    "symbol_rate": 4800,
                    "destination": f"udp://127.0.0.1:{udp_port}",
                    "enable_analog": "off",
                })
                udp_port_idx += 1
                logger.debug(
                    "generate_multi_rx_config: sdr_traffic2 serial=%s -> system=%s port=%d",
                    traffic_dongle_serial_2, target_sys_2, udp_port,
                )
            else:
                logger.warning(
                    "generate_multi_rx_config: sdr_traffic2 serial=%s skipped — "
                    "no control channel found for system=%s",
                    traffic_dongle_serial_2, target_sys_2,
                )

    # Reorder devices so any RSPduo Master precedes its Slave (and any other
    # device).  Channels reference devices by ``name`` so this is safe.
    devices.sort(key=_master_first_device_order_key)

    return {
        "devices": devices,
        "channels": channels,
        "trunking": {
            "module": "tk_p25.py",
            "chans": trunking_chans,
        },
        "terminal": {
            "module": "terminal.py",
            "terminal_type": f"http:0.0.0.0:{http_port}",
        },
    }


def _multi_rx_udp_ports(config: dict) -> list[int]:
    """Extract the UDP audio ports from a multi_rx config dict."""
    ports: list[int] = []
    for ch in config.get("channels") or []:
        dest = ch.get("destination", "")
        if dest.startswith("udp://"):
            try:
                ports.append(int(dest.rsplit(":", 1)[1]))
            except (ValueError, IndexError):
                pass
    return sorted(ports)


# ---------------------------------------------------------------------------
# Op25Adapter
# ---------------------------------------------------------------------------

class Op25Adapter(_BaseDigitalAdapter):
    """OP25 digital backend adapter with native multi-system support."""

    name = "op25"

    def __init__(self):
        super().__init__()
        self._service_name = _normalize_name(OP25_SERVICE_NAME)
        self._profiles_dir = DIGITAL_PROFILES_DIR
        self._active_link = DIGITAL_ACTIVE_PROFILE_LINK
        self._log_path = OP25_LOG_PATH
        self._runtime_dir = OP25_RUNTIME_DIR
        self._status_host = OP25_STATUS_HOST
        self._status_port = OP25_STATUS_PORT
        # Log parsing state — start near end of log to avoid re-parsing
        # the entire file on first run (large logs with old data).
        self._log_offset = 0
        self._log_inode = 0
        self._init_log_offset()
        self._refresh_lock = threading.Lock()
        self._last_refresh_monotonic = 0.0
        self._refresh_min_interval_sec = 0.5
        # Talkgroup labels
        self._tg_labels: dict[str, str] = {}
        self._tg_display: dict[str, dict[str, str]] = {}
        self._tg_labels_profile = ""
        self._tg_labels_mtime = None
        # Health cache
        self._last_status: dict = {}
        self._last_status_time = 0.0
        self._status_cache_ttl = 1.5
        self._status_get_failed_ports: set[int] = set()
        # Active systems
        self._active_systems: list[dict] = []
        self._runtime_metrics_data: dict = {
            "profile_apply_last_duration_ms": 0,
            "profile_apply_last_error": "",
            "profile_apply_last_changed": False,
        }
        if not validate_digital_service_name(self._service_name):
            self._set_last_error("invalid OP25 service name")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def supports_multi_system(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Systemd service control
    # ------------------------------------------------------------------

    def _systemctl(self, args: list[str]) -> tuple[bool, str]:
        if not validate_digital_service_name(self._service_name):
            return False, "invalid OP25 service name"
        cmd = ["systemctl"] + list(args) + [self._service_name]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout or "").strip()
        if not err:
            err = f"systemctl failed (code {result.returncode})"
        if "interactive authentication required" in err.lower() or "access denied" in err.lower():
            try:
                result = subprocess.run(
                    ["sudo"] + cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            except Exception as e:
                return False, str(e)
            if result.returncode == 0:
                return True, ""
            err = (result.stderr or result.stdout or "").strip() or err
        return False, err

    def start(self):
        ok, err = self._systemctl(["start"])
        if not ok:
            self._set_last_error(err or "start failed")
            return False, self._last_error
        self._clear_error()
        return True, ""

    def stop(self):
        ok, err = self._systemctl(["stop"])
        if not ok:
            self._set_last_error(err or "stop failed")
            return False, self._last_error
        self._clear_error()
        return True, ""

    def restart(self):
        if not validate_digital_service_name(self._service_name):
            self._set_last_error("invalid OP25 service name")
            return False, self._last_error
        ok, err = restart_digital(self._service_name)
        if not ok:
            self._set_last_error(err or "restart failed")
            return False, self._last_error
        self._clear_error()
        return True, ""

    def isActive(self):
        if not validate_digital_service_name(self._service_name):
            return False
        return unit_active(self._service_name)

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def _list_profile_dirs(self) -> list[str]:
        base = self._profiles_dir
        if not base:
            return []
        try:
            entries = os.listdir(base)
        except Exception:
            return []
        profiles = []
        for name in entries:
            if not validate_digital_profile_id(name):
                continue
            if os.path.isdir(os.path.join(base, name)):
                profiles.append(name)
        return sorted(profiles)

    def listProfiles(self):
        return self._list_profile_dirs()

    def _read_active_profile_id(self) -> str:
        link = self._active_link
        if not link or not os.path.islink(link):
            return ""
        try:
            target = _safe_realpath(link)
        except Exception:
            return ""
        base = _safe_realpath(self._profiles_dir)
        if base and target.startswith(base + os.sep):
            return os.path.basename(target)
        return ""

    def _read_active_profile_dir(self) -> str:
        link = self._active_link
        if not link or not os.path.islink(link):
            return ""
        try:
            target = _safe_realpath(link)
        except Exception:
            return ""
        if target and os.path.isdir(target):
            return target
        return ""

    def getProfile(self):
        return self._read_active_profile_id()

    def setProfile(self, profileId: str, *, restart_service: bool = True):
        pid = _normalize_name(profileId)
        if not validate_digital_profile_id(pid):
            return False, "invalid profileId"
        base = _safe_realpath(self._profiles_dir)
        target = _safe_realpath(os.path.join(self._profiles_dir, pid))
        if not base or not target.startswith(base + os.sep):
            return False, "invalid profile path"
        if not os.path.isdir(target):
            return False, "unknown profileId"

        link = self._active_link
        try:
            if os.path.islink(link):
                os.remove(link)
            os.symlink(target, link)
        except Exception as e:
            return False, f"symlink failed: {e}"

        # Regenerate OP25 config and optionally restart.
        systems = _read_system_definitions(target)
        if systems:
            ok, err = self._write_runtime_config(target, systems)
            if not ok:
                return False, err

        if restart_service and self.isActive():
            return self.restart()
        return True, ""

    # ------------------------------------------------------------------
    # Config generation & runtime
    # ------------------------------------------------------------------

    def _write_runtime_config(
        self,
        profile_dir: str,
        systems: list[dict],
    ) -> tuple[bool, str]:
        """Generate trunk.tsv + tgid_tags.tsv in the runtime directory."""
        runtime = self._runtime_dir
        try:
            os.makedirs(runtime, exist_ok=True)
        except Exception as e:
            return False, f"cannot create runtime dir: {e}"

        dongle_assignments = None
        try:
            dongle_assignments = load_assignments()
        except Exception:
            pass

        op25_overrides = _read_op25_system_config(profile_dir)
        runtime_systems, _selector_state = _hydrate_runtime_systems_for_config(
            profile_dir,
            runtime,
            op25_overrides=op25_overrides,
        )
        active_systems = _flatten_active_runtime_systems(runtime_systems)
        tg_labels = _read_talkgroup_labels(profile_dir)
        tags_path = os.path.join(runtime, "tgid_tags.tsv")

        trunk_content = generate_trunk_tsv(
            active_systems or systems,
            dongle_assignments=dongle_assignments,
            op25_overrides=op25_overrides,
            tgid_tags_path=tags_path,
        )
        tags_content = generate_tgid_tags_tsv(tg_labels)

        try:
            trunk_path = os.path.join(runtime, "trunk.tsv")
            tmp = trunk_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(trunk_content)
            os.replace(tmp, trunk_path)
        except Exception as e:
            return False, f"failed to write trunk.tsv: {e}"

        try:
            tmp = tags_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(tags_content)
            os.replace(tmp, tags_path)
        except Exception as e:
            logger.debug("Failed to write tgid_tags.tsv: %s", e)

        self._active_systems = list(active_systems or systems)
        return True, ""

    # ------------------------------------------------------------------
    # apply_system / activate_systems
    # ------------------------------------------------------------------

    def apply_system(
        self,
        system_name: str,
        control_channels_hz: list,
        *,
        preferred_tuner: str = "",
        force: bool = False,
    ) -> tuple[bool, str, bool]:
        """No-op for OP25 in multi-system mode.

        OP25 monitors all configured systems simultaneously once started.
        Individual system changes are handled via :meth:`activate_systems`.
        """
        return True, "", False

    def activate_systems(self, systems: list) -> tuple[bool, str, bool]:
        """Regenerate OP25 config for all *systems* and restart the service."""
        started = time.monotonic()
        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            return False, "no active profile", False

        ok, err = self._write_runtime_config(profile_dir, systems)
        if not ok:
            self._runtime_metrics_data["profile_apply_last_error"] = err
            return False, err, False

        changed = True
        if self.isActive():
            rok, rerr = self.restart()
            if not rok:
                self._runtime_metrics_data["profile_apply_last_error"] = rerr
                return False, rerr, changed

        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._runtime_metrics_data["profile_apply_last_duration_ms"] = elapsed_ms
        self._runtime_metrics_data["profile_apply_last_error"] = ""
        self._runtime_metrics_data["profile_apply_last_changed"] = changed
        return True, "", changed

    # ------------------------------------------------------------------
    # Retune — OP25 manages its own control channels
    # ------------------------------------------------------------------

    def retune_control_frequency(self, freq_mhz: float) -> tuple[bool, str]:
        return False, "OP25 manages control channels natively; retune not supported"

    def runtime_retune_available(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # Event log parsing
    # ------------------------------------------------------------------

    def _refresh_log_cache(self) -> None:
        """Tail OP25 log file for new call events."""
        now_mono = time.monotonic()
        if (
            self._last_refresh_monotonic
            and (now_mono - self._last_refresh_monotonic) < self._refresh_min_interval_sec
        ):
            return
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            self._last_refresh_monotonic = time.monotonic()
            self._tail_op25_log()
        finally:
            self._refresh_lock.release()

    def _init_log_offset(self) -> None:
        """Seek to near end of the log file so we only parse recent lines."""
        path = self._log_path
        if not path or not os.path.isfile(path):
            return
        try:
            stat = os.stat(path)
            # Start 32KB from end — enough to pick up recent events
            self._log_offset = max(0, stat.st_size - 32_768)
            self._log_inode = stat.st_ino
        except Exception:
            pass

    def _tail_op25_log(self) -> None:
        path = self._log_path
        if not path or not os.path.isfile(path):
            return
        try:
            stat = os.stat(path)
        except Exception:
            return
        # Detect log rotation.
        if stat.st_ino != self._log_inode:
            self._log_offset = 0
            self._log_inode = stat.st_ino
        if stat.st_size <= self._log_offset:
            if stat.st_size < self._log_offset:
                self._log_offset = 0  # truncated
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self._log_offset)
                new_data = f.read(256_000)  # cap read size
                self._log_offset = f.tell()
        except Exception:
            return
        for line in new_data.splitlines():
            event = self._parse_log_line(line)
            if event:
                self._set_last_event(
                    event.get("label", ""),
                    mode=event.get("mode"),
                    raw=event,
                )
                self._record_event(event)

    def _parse_log_line(self, line: str) -> dict | None:
        """Parse a single OP25 log line into an event dict, or None."""
        m = _RE_TSBK.search(line) or _RE_VOICE.search(line)
        if not m:
            return None
        tgid = m.group(1)
        freq_raw = m.group(2)
        # Boatbod OP25 logs freq in MHz (e.g. "854.587500"), others in Hz.
        try:
            if "." in freq_raw:
                freq_hz = int(round(float(freq_raw) * 1_000_000))
            else:
                freq_hz = int(freq_raw)
        except (ValueError, OverflowError):
            freq_hz = 0
        timestamp_ms = self._extract_timestamp_ms(line)

        metadata = self._resolve_tg_display(tgid)
        label = str(metadata.get("label") or "").strip() or self._resolve_tg_label(tgid)
        event = {
            "type": "digital",
            "tgid": tgid,
            "label": label or f"TG {tgid}",
            "mode": "P25",
            "frequency_hz": freq_hz,
            "frequency_mhz": _hz_to_mhz(freq_hz),
            "timeMs": timestamp_ms or int(time.time() * 1000),
        }
        agency = str(metadata.get("agency") or "").strip()
        department = str(metadata.get("department") or "").strip()
        label_full = str(metadata.get("label_full") or "").strip()
        if agency:
            event["agency"] = agency
        if department:
            event["department"] = department
        if label_full:
            event["label_full"] = label_full
        return event

    def _extract_timestamp_ms(self, line: str) -> int:
        m = _RE_TIMESTAMP.search(line)
        if not m:
            return 0
        try:
            from datetime import datetime
            token = str(m.group(1) or "").strip()
            for fmt in _TIMESTAMP_FORMATS:
                try:
                    dt = datetime.strptime(token, fmt)
                    return int(dt.timestamp() * 1000)
                except ValueError:
                    continue
        except Exception:
            return 0
        return 0

    def _resolve_tg_label(self, tgid: str) -> str:
        """Lookup talkgroup label from profile data."""
        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            return ""
        profile_id = os.path.basename(profile_dir)
        if profile_id != self._tg_labels_profile:
            self._tg_display = _read_talkgroup_display_metadata(profile_dir)
            self._tg_labels = {
                key: str((value or {}).get("label") or "").strip()
                for key, value in self._tg_display.items()
            }
            self._tg_labels_profile = profile_id
        return self._tg_labels.get(tgid, "")

    def _resolve_tg_display(self, tgid: str) -> dict[str, str]:
        """Lookup talkgroup display metadata from profile data."""
        _ = self._resolve_tg_label(tgid)
        return dict(self._tg_display.get(str(tgid or "").strip(), {}))

    def getLastEvent(self):
        self._refresh_log_cache()
        self._refresh_http_event_cache()
        return super().getLastEvent()

    def getRecentEvents(self, limit: int = 20):
        self._refresh_log_cache()
        self._refresh_http_event_cache()
        return super().getRecentEvents(limit)

    # ------------------------------------------------------------------
    # Health / preflight
    # ------------------------------------------------------------------

    def _load_instance_manifest(self) -> list[dict[str, Any]]:
        path = os.path.join(self._runtime_dir, "instances.json")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                payload = json.load(handle)
        except Exception:
            return []
        if isinstance(payload, list):
            return [entry for entry in payload if isinstance(entry, dict)]
        if isinstance(payload, dict):
            channels = payload.get("channels")
            if isinstance(channels, list):
                return [entry for entry in channels if isinstance(entry, dict)]
        return []

    def _status_ports_from_manifest(self) -> list[int]:
        ports: list[int] = []
        seen: set[int] = set()
        for entry in self._load_instance_manifest():
            try:
                port = int(entry.get("http_status_port") or 0)
            except Exception:
                port = 0
            if port <= 0 or port in seen:
                continue
            seen.add(port)
            ports.append(port)
        return ports

    def _request_json(self, path: str, *, method: str = "GET", payload=None, port: int | None = None):
        route = str(path or "/").strip()
        if not route.startswith("/"):
            route = f"/{route}"
        target_port = int(port or self._status_port)
        url = f"http://{self._status_host}:{target_port}{route}"
        try:
            body = None
            headers = {}
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                return data if isinstance(data, (dict, list)) else {}
        except Exception:
            return {}

    @staticmethod
    def _status_needs_root_fallback(status: dict) -> bool:
        if not isinstance(status, dict) or not status:
            return True
        signal_keys = {
            "locked",
            "control_channel_locked",
            "control_decode_available",
            "decode_rate",
            "ber",
            "trunk_update",
            "channel_update",
            "call_log",
        }
        return not any(key in status for key in signal_keys)

    @staticmethod
    def _iter_trunk_system_rows(status: dict):
        if not isinstance(status, dict):
            return []
        trunk_update = status.get("trunk_update")
        if not isinstance(trunk_update, dict):
            return []
        systems = trunk_update.get("systems")
        if isinstance(systems, dict):
            rows = [row for row in systems.values() if isinstance(row, dict)]
            if rows:
                return rows
        rows = []
        for key, row in trunk_update.items():
            if key in {"json_type", "nac", "systems"}:
                continue
            if isinstance(row, dict):
                    rows.append(row)
        return rows

    @staticmethod
    def _iter_channel_rows(status: dict):
        if not isinstance(status, dict):
            return []
        channel_update = status.get("channel_update")
        if not isinstance(channel_update, dict):
            return []
        return [row for row in channel_update.values() if isinstance(row, dict)]

    @staticmethod
    def _op25_system_key(token) -> str:
        return str(token or "").strip().lower()

    @classmethod
    def _root_trunk_decode_available(cls, status: dict) -> bool:
        if not isinstance(status, dict):
            return False
        now_sec = float(time.time())
        saw_last_tsbk = False
        fallback_tsbk_count = False
        for row in cls._iter_trunk_system_rows(status):
            try:
                last_tsbk = float(row.get("last_tsbk") or 0.0)
            except Exception:
                last_tsbk = 0.0
            if last_tsbk > 0:
                saw_last_tsbk = True
                if last_tsbk <= (now_sec + 120.0) and (now_sec - last_tsbk) <= _OP25_ROOT_ACTIVITY_MAX_AGE_SEC:
                    return True
            if last_tsbk <= 0:
                top_line = str(row.get("top_line") or "").strip()
                match = _RE_ROOT_TSBKS.search(top_line)
                if not match:
                    continue
                try:
                    fallback_tsbk_count = int(match.group(1) or 0) > 0
                except Exception:
                    fallback_tsbk_count = False
                if fallback_tsbk_count:
                    break
        return fallback_tsbk_count and not saw_last_tsbk

    @staticmethod
    def _root_control_channel_locked(status: dict) -> bool:
        if not isinstance(status, dict):
            return False
        channel_update = status.get("channel_update")
        if isinstance(channel_update, dict):
            for row in channel_update.values():
                if not isinstance(row, dict):
                    continue
                tag = str(row.get("tag") or "").strip().lower()
                try:
                    freq = int(row.get("freq") or 0)
                except Exception:
                    freq = 0
                if "control channel" in tag and freq > 0:
                    return True
        now_sec = float(time.time())
        for row in Op25Adapter._iter_trunk_system_rows(status):
            try:
                rxchan = int(row.get("rxchan") or 0)
            except Exception:
                rxchan = 0
            try:
                last_tsbk = float(row.get("last_tsbk") or 0.0)
            except Exception:
                last_tsbk = 0.0
            if rxchan <= 0 or last_tsbk <= 0:
                continue
            if last_tsbk <= (now_sec + 120.0) and (now_sec - last_tsbk) <= _OP25_ROOT_ACTIVITY_MAX_AGE_SEC:
                return True
        return False

    @staticmethod
    def _normalize_update_payload(payload) -> dict:
        if isinstance(payload, dict):
            return payload
        if not isinstance(payload, list):
            return {}
        status: dict = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            json_type = str(item.get("json_type") or "").strip()
            if not json_type:
                continue
            if json_type == "trunk_update":
                normalized = dict(item)
                systems = {}
                for key, row in item.items():
                    if key in {"json_type", "nac", "systems"}:
                        continue
                    if not isinstance(row, dict):
                        continue
                    system_key = str(row.get("system") or key).strip() or str(key)
                    systems[system_key] = row
                if systems:
                    normalized["systems"] = systems
                status["trunk_update"] = normalized
                continue
            if json_type == "call_log":
                status["call_log"] = list(item.get("log") or [])
                continue
            status[json_type] = dict(item)
        return status

    @classmethod
    def _merge_instance_statuses(cls, statuses: list[tuple[int, dict]]) -> dict:
        valid = [
            (int(port), dict(status))
            for port, status in statuses
            if isinstance(status, dict) and status
        ]
        if not valid:
            return {}
        if len(valid) == 1:
            merged = dict(valid[0][1])
            merged["op25_instance_ports"] = [valid[0][0]]
            return merged

        trunk_systems: dict[str, dict[str, Any]] = {}
        channel_update: dict[str, dict[str, Any]] = {}
        call_log: list[dict[str, Any]] = []
        locked = False
        control_decode_available = False
        decode_rate = 0.0
        ber_values: list[float] = []

        for port, status in valid:
            if bool(status.get("locked") or status.get("control_channel_locked")) or cls._root_control_channel_locked(status):
                locked = True
            if bool(status.get("control_decode_available")) or cls._root_trunk_decode_available(status):
                control_decode_available = True
            try:
                decode_rate += float(status.get("decode_rate", 0) or 0)
            except Exception:
                pass
            try:
                ber_values.append(float(status.get("ber", 0) or 0))
            except Exception:
                pass

            for idx, row in enumerate(cls._iter_trunk_system_rows(status)):
                if not isinstance(row, dict):
                    continue
                system_name = str(row.get("system") or row.get("sysname") or "").strip()
                key = system_name or f"{port}:{idx}"
                trunk_systems[key] = dict(row)

            for idx, row in enumerate(cls._iter_channel_rows(status)):
                if not isinstance(row, dict):
                    continue
                channel_update[f"{port}:{idx}"] = dict(row)

            for row in status.get("call_log") or []:
                if isinstance(row, dict):
                    call_log.append(dict(row))

        merged: dict[str, Any] = {
            "locked": locked,
            "control_channel_locked": locked,
            "control_decode_available": control_decode_available,
            "decode_rate": decode_rate,
            "ber": max(ber_values) if ber_values else 0.0,
            "op25_instance_ports": [port for port, _status in valid],
        }
        if trunk_systems:
            merged["trunk_update"] = {
                "json_type": "trunk_update",
                "systems": trunk_systems,
            }
        if channel_update:
            merged["channel_update"] = channel_update
        if call_log:
            try:
                call_log.sort(key=lambda row: float(row.get("time") or 0.0))
            except Exception:
                pass
            merged["call_log"] = call_log
        return merged

    def _fetch_json(self, path: str, *, port: int | None = None) -> dict:
        data = self._request_json(path, method="GET", port=port)
        return data if isinstance(data, dict) else {}

    def _fetch_update_json(self, *, port: int | None = None) -> dict:
        payload = [{"command": "update", "arg1": 0, "arg2": 0}]
        data = self._request_json("/", method="POST", payload=payload, port=port)
        return self._normalize_update_payload(data)

    @staticmethod
    def _system_from_call_log_row(row: dict) -> str:
        if not isinstance(row, dict):
            return ""
        system = str(row.get("system") or "").strip()
        if system:
            return system
        receiver = str(row.get("rcvrtag") or "").strip()
        if receiver.startswith("ch_"):
            return receiver[3:].strip()
        return ""

    @staticmethod
    def _event_identity_key(event: dict) -> tuple[str, str, int | None, str]:
        if not isinstance(event, dict):
            return ("", "", None, "")
        system = str(event.get("system") or "").strip().lower()
        tgid = str(event.get("tgid") or "").strip()
        try:
            freq_hz = int(event.get("frequency_hz") or 0)
        except Exception:
            freq_hz = 0
        label = str(event.get("label") or "").strip().lower()
        if tgid:
            return (system, tgid, freq_hz or None, "")
        return (system, "", freq_hz or None, label)

    def _events_from_status(self, status: dict) -> list[dict]:
        if not isinstance(status, dict):
            return []
        events = []
        seen: set[tuple[str, str, int | None, str]] = set()
        for row in status.get("call_log") or []:
            if not isinstance(row, dict):
                continue
            tgid = str(row.get("tgid") or "").strip()
            metadata = self._resolve_tg_display(tgid)
            label = str(row.get("tgtag") or "").strip() or str(metadata.get("label") or "").strip() or self._resolve_tg_label(tgid)
            if not tgid and not label:
                continue
            try:
                freq_hz = int(row.get("freq") or 0)
            except Exception:
                freq_hz = 0
            try:
                time_ms = int(float(row.get("time") or 0.0) * 1000)
            except Exception:
                time_ms = 0
            event = {
                "type": "digital",
                "tgid": tgid,
                "label": label or (f"TG {tgid}" if tgid else ""),
                "mode": "P25",
                "frequency_hz": freq_hz,
                "timeMs": time_ms or int(time.time() * 1000),
                "raw": row,
            }
            agency = str(metadata.get("agency") or "").strip()
            department = str(metadata.get("department") or "").strip()
            label_full = str(metadata.get("label_full") or "").strip()
            if agency:
                event["agency"] = agency
            if department:
                event["department"] = department
            if label_full:
                event["label_full"] = label_full
            if freq_hz > 0:
                event["frequency_mhz"] = _hz_to_mhz(freq_hz)
            system = self._system_from_call_log_row(row)
            if system:
                event["system"] = system
            seen.add(self._event_identity_key(event))
            events.append(event)

        now_ms = int(time.time() * 1000)
        channel_update = status.get("channel_update")
        if isinstance(channel_update, dict):
            for row in channel_update.values():
                if not isinstance(row, dict):
                    continue
                tgid = str(row.get("tgid") or "").strip()
                if not tgid:
                    continue
                metadata = self._resolve_tg_display(tgid)
                label = str(row.get("tag") or "").strip() or str(metadata.get("label") or "").strip() or self._resolve_tg_label(tgid)
                try:
                    freq_hz = int(row.get("freq") or 0)
                except Exception:
                    freq_hz = 0
                event = {
                    "type": "digital",
                    "tgid": tgid,
                    "label": label or f"TG {tgid}",
                    "mode": str(row.get("mode") or "P25").strip() or "P25",
                    "frequency_hz": freq_hz,
                    "timeMs": now_ms,
                    "raw": row,
                }
                agency = str(metadata.get("agency") or "").strip()
                department = str(metadata.get("department") or "").strip()
                label_full = str(metadata.get("label_full") or "").strip()
                if agency:
                    event["agency"] = agency
                if department:
                    event["department"] = department
                if label_full:
                    event["label_full"] = label_full
                if freq_hz > 0:
                    event["frequency_mhz"] = _hz_to_mhz(freq_hz)
                system = str(row.get("system") or "").strip()
                if system:
                    event["system"] = system
                identity = self._event_identity_key(event)
                if identity in seen:
                    continue
                seen.add(identity)
                events.append(event)
        events.sort(key=lambda item: int(item.get("timeMs") or 0))
        return events

    def _refresh_http_event_cache(self) -> None:
        status = self._poll_op25_status()
        if not status:
            return
        for event in self._events_from_status(status):
            self._set_last_event(
                event.get("label", ""),
                mode=event.get("mode"),
                raw=event,
            )
            self._record_event(event)

    def _poll_op25_status(self) -> dict:
        """Poll OP25's HTTP status endpoint. Returns parsed JSON or {}.

        Always tries POST / (update command) first — this is the method
        used by boatbod OP25 (and all modern builds).  Only falls back
        to GET /status once, on the very first call, if POST returns
        nothing, to support legacy OP25 installs.
        """
        now = time.monotonic()
        if (now - self._last_status_time) < self._status_cache_ttl and self._last_status:
            return dict(self._last_status)
        ports = self._status_ports_from_manifest() or [self._status_port]
        instance_statuses: list[tuple[int, dict]] = []
        for port in ports:
            data: dict = {}
            update_data = self._fetch_update_json(port=port)
            if update_data and not self._status_needs_root_fallback(update_data):
                data = update_data
            elif port not in self._status_get_failed_ports:
                legacy = self._fetch_json("/status", port=port)
                if legacy and not self._status_needs_root_fallback(legacy):
                    data = legacy
                else:
                    self._status_get_failed_ports.add(port)
            if data:
                instance_statuses.append((port, data))
        data = self._merge_instance_statuses(instance_statuses)
        if data:
            self._last_status = data
            self._last_status_time = now
            return data
        return self._last_status or {}

    def _read_runtime_systems(self, profile_dir: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        overrides = _read_op25_system_config(profile_dir)
        return _hydrate_runtime_systems_for_config(
            profile_dir,
            self._runtime_dir,
            op25_overrides=overrides,
        )

    def _monitored_tgids(self, profile_dir: str) -> set[str]:
        return {
            str(tgid).strip()
            for tgid in _read_talkgroup_labels(profile_dir).keys()
            if str(tgid).strip().isdigit()
        }

    def _status_metrics_for_system(
        self,
        status: dict,
        system_name: str,
        monitored_tgids: set[str],
        *,
        runtime_system_count: int,
    ) -> dict[str, Any]:
        system_key = self._op25_system_key(system_name)
        now_sec = time.time()
        trunk_rows = self._iter_trunk_system_rows(status)
        channel_rows = self._iter_channel_rows(status)
        matched_trunk = [row for row in trunk_rows if self._op25_system_key(row.get("system")) == system_key]
        matched_channels = [row for row in channel_rows if self._op25_system_key(row.get("system")) == system_key]
        if runtime_system_count == 1:
            if not matched_trunk and trunk_rows:
                matched_trunk = trunk_rows
            if not matched_channels and channel_rows:
                matched_channels = channel_rows

        last_tsbk_values: list[float] = []
        for row in matched_trunk:
            try:
                last_tsbk = float(row.get("last_tsbk") or 0.0)
            except Exception:
                last_tsbk = 0.0
            if last_tsbk > 0:
                last_tsbk_values.append(last_tsbk)
        last_tsbk_age_sec = None
        if last_tsbk_values:
            last_tsbk_age_sec = max(0.0, now_sec - max(last_tsbk_values))

        control_locked = False
        on_voice_channel = False
        for row in matched_channels:
            tag = str(row.get("tag") or "").strip().lower()
            if "control channel" in tag:
                control_locked = True
            if "voice channel" in tag:
                on_voice_channel = True
        # When time-slicing, the SDR legitimately leaves the control channel to
        # follow voice grants — TSBKs stop arriving during the call.  If the
        # channel_update shows the SDR is currently on a voice channel, suspend
        # the staleness check so we don't declare the decode stale mid-call.
        # Set env var OP25_VOICE_EXEMPT_STALE=0 to disable and revert to the
        # original unconditional 15-second threshold.
        _voice_exempt = os.environ.get("OP25_VOICE_EXEMPT_STALE", "1") != "0"
        if _voice_exempt and on_voice_channel:
            control_decode_available = True
        else:
            control_decode_available = bool(
                last_tsbk_age_sec is not None and last_tsbk_age_sec <= _OP25_ROOT_ACTIVITY_MAX_AGE_SEC
            )

        call_log = status.get("call_log") or []
        has_explicit_system = any(
            isinstance(row, dict) and str(row.get("system") or "").strip()
            for row in call_log
        )
        recent_any_grants = 0
        recent_monitored_tg_hits = 0
        for row in call_log:
            if not isinstance(row, dict):
                continue
            row_system = self._op25_system_key(row.get("system"))
            if has_explicit_system and row_system and row_system != system_key:
                continue
            tgid = str(row.get("tgid") or "").strip()
            if not tgid:
                continue
            recent_any_grants += 1
            if monitored_tgids and tgid in monitored_tgids:
                recent_monitored_tg_hits += 1

        return {
            "control_locked": bool(control_locked),
            "control_decode_available": bool(control_decode_available),
            "last_tsbk_age_sec": round(float(last_tsbk_age_sec), 3) if last_tsbk_age_sec is not None else None,
            "recent_any_grants": int(recent_any_grants),
            "recent_monitored_tg_hits": int(recent_monitored_tg_hits),
        }

    def _candidate_state_map(self, system: dict[str, Any], sys_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        existing: dict[str, dict[str, Any]] = {}
        for row in sys_state.get("candidates") or []:
            if isinstance(row, dict):
                site_id = str(row.get("site_id") or "").strip()
                if site_id:
                    existing[site_id] = dict(row)
        candidates: dict[str, dict[str, Any]] = {}
        for site in system.get("sites") or []:
            site_id = str(site.get("site_id") or "").strip()
            base = _candidate_state_defaults(site)
            stored = existing.get(site_id) or {}
            for key in (
                "control_locked",
                "control_decode_available",
                "last_tsbk_age_sec",
                "recent_any_grants",
                "recent_monitored_tg_hits",
                "_revisit_block_until_ms",
                "_last_sample_time_ms",
            ):
                if key in stored:
                    base[key] = stored[key]
            candidates[site_id] = base
        return candidates

    def _select_best_alternate(
        self,
        candidates: list[dict[str, Any]],
        *,
        current_site_id: str,
    ) -> dict[str, Any] | None:
        enabled = [
            row
            for row in candidates
            if _parse_enabled(row.get("enabled")) and str(row.get("site_id") or "") != current_site_id
        ]
        if not enabled:
            return None
        non_avoided = [row for row in enabled if not bool(row.get("_avoided"))]
        pool = non_avoided if non_avoided else enabled
        pool = sorted(
            pool,
            key=lambda row: (
                int(row.get("score") or 0),
                str(row.get("site_name") or "").lower(),
                str(row.get("site_id") or "").lower(),
            ),
            reverse=True,
        )
        return pool[0] if pool else None

    def _selector_decision_for_system(
        self,
        system: dict[str, Any],
        sys_state: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        now_ms: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        policy = system.get("site_policy") or {}
        selected_site_id = str(sys_state.get("selected_site_id") or "").strip()
        selected = next((row for row in candidates if str(row.get("site_id") or "") == selected_site_id), None)
        enabled_candidates = [row for row in candidates if bool(row.get("enabled"))]
        if not enabled_candidates:
            return ({
                "action": "generic_restart",
                "site_id": "",
                "selection_mode": str(sys_state.get("selection_mode") or "auto"),
                "reason_code": "generic_restart_no_valid_site",
                "reason_text": "No enabled sites available",
            }, sys_state)

        pinned_id = str(policy.get("pinned_site_id") or "").strip()
        if pinned_id:
            pinned_candidate = next((row for row in enabled_candidates if str(row.get("site_id") or "") == pinned_id), None)
            if pinned_candidate:
                if selected_site_id != pinned_id:
                    return ({
                        "action": "switch",
                        "site_id": pinned_id,
                        "selection_mode": "pinned",
                        "reason_code": "site_switch_policy_pinned",
                        "reason_text": f"Pinned site {pinned_candidate['site_name']} selected",
                    }, sys_state)
                return ({
                    "action": "stay",
                    "site_id": pinned_id,
                    "selection_mode": "pinned",
                    "reason_code": "pinned_site_selected",
                    "reason_text": f"Pinned site {pinned_candidate['site_name']} retained",
                }, sys_state)

        if selected is None:
            non_avoided = [row for row in enabled_candidates if not bool(row.get("_avoided"))]
            selected = sorted(
                non_avoided or enabled_candidates,
                key=lambda row: (int(row.get("score") or 0), str(row.get("site_name") or ""), str(row.get("site_id") or "")),
                reverse=True,
            )[0]
            return ({
                "action": "switch",
                "site_id": str(selected.get("site_id") or ""),
                "selection_mode": "fallback",
                "reason_code": "fallback_first_enabled",
                "reason_text": f"Selected best available enabled site {selected.get('site_name')}",
            }, sys_state)

        current_score = int(selected.get("score") or 0)
        current_unhealthy = _candidate_is_unhealthy(selected)
        best_alternate = self._select_best_alternate(candidates, current_site_id=selected_site_id)
        min_dwell_ms = int(policy.get("min_dwell_sec") or 120) * 1000
        unproductive_window_ms = int(policy.get("unproductive_window_sec") or 300) * 1000
        switch_margin = int(policy.get("switch_margin") or 20)
        current_since_ms = _ms_from_iso(str(sys_state.get("current_site_since") or ""))
        current_dwell_ms = max(0, now_ms - current_since_ms) if current_since_ms > 0 else 0

        survey_mode = (
            str(sys_state.get("selection_mode") or "") == "survey"
            and not bool(sys_state.get("_survey_completed"))
            and len(enabled_candidates) > 1
            and str(policy.get("mode") or "auto") == "auto"
        )
        if survey_mode:
            ordered_enabled_candidates = _canonical_site_order(enabled_candidates)
            survey_started_at_ms = int(sys_state.get("_survey_started_at_ms") or now_ms)
            survey_index = int(sys_state.get("_survey_candidate_index") or 0)
            actual_index = next(
                (
                    idx
                    for idx, row in enumerate(ordered_enabled_candidates)
                    if str(row.get("site_id") or "") == selected_site_id
                ),
                -1,
            )
            if actual_index >= 0:
                survey_index = actual_index
                sys_state["_survey_candidate_index"] = actual_index
            total_elapsed_ms = max(0, now_ms - survey_started_at_ms)
            if int(selected.get("recent_monitored_tg_hits") or 0) > 0 and not current_unhealthy:
                sys_state["_survey_completed"] = True
                return ({
                    "action": "stay",
                    "site_id": selected_site_id,
                    "selection_mode": "survey",
                    "reason_code": "survey_candidate_productive",
                    "reason_text": f"Survey retained productive site {selected.get('site_name')}",
                }, sys_state)
            if total_elapsed_ms < (_OP25_SITE_SELECTOR_SURVEY_MAX_SEC * 1000) and current_dwell_ms >= (_OP25_SITE_SELECTOR_SURVEY_DWELL_SEC * 1000):
                if survey_index + 1 < len(ordered_enabled_candidates):
                    next_candidate = ordered_enabled_candidates[survey_index + 1]
                    sys_state["_survey_candidate_index"] = survey_index + 1
                    return ({
                        "action": "switch",
                        "site_id": str(next_candidate.get("site_id") or ""),
                        "selection_mode": "survey",
                        "reason_code": "site_survey_switch",
                        "reason_text": f"Survey switching to {next_candidate.get('site_name')}",
                    }, sys_state)
            best = sorted(
                ordered_enabled_candidates,
                key=lambda row: (
                    int(row.get("score") or 0),
                    str(row.get("site_name") or "").lower(),
                    str(row.get("site_id") or "").lower(),
                ),
                reverse=True,
            )[0]
            sys_state["_survey_completed"] = True
            if str(best.get("site_id") or "") != selected_site_id:
                return ({
                    "action": "switch",
                    "site_id": str(best.get("site_id") or ""),
                    "selection_mode": "survey",
                    "reason_code": "site_survey_switch",
                    "reason_text": f"Survey completed; switching to best scored site {best.get('site_name')}",
                }, sys_state)
            return ({
                "action": "stay",
                "site_id": selected_site_id,
                "selection_mode": "survey",
                "reason_code": "survey_complete_best_score",
                "reason_text": f"Survey completed; retained best scored site {selected.get('site_name')}",
            }, sys_state)

        if current_unhealthy:
            if best_alternate and bool(best_alternate.get("enabled")):
                return ({
                    "action": "switch",
                    "site_id": str(best_alternate.get("site_id") or ""),
                    "selection_mode": str(sys_state.get("selection_mode") or "auto"),
                    "reason_code": "site_switch_unhealthy",
                    "reason_text": f"Current site unhealthy; switching to {best_alternate.get('site_name')}",
                }, sys_state)
            # Grace period: skip stale counting for 90s after a restart so
            # OP25 has time to scan and lock onto control channels.
            last_restart_ms = int(sys_state.get("_last_restart_time_ms") or 0)
            if last_restart_ms > 0 and (now_ms - last_restart_ms) < _OP25_SITE_SELECTOR_POST_RESTART_GRACE_MS:
                return ({
                    "action": "stay",
                    "site_id": selected_site_id,
                    "selection_mode": str(sys_state.get("selection_mode") or "auto"),
                    "reason_code": "stay_post_restart_grace",
                    "reason_text": f"Post-restart grace period ({(now_ms - last_restart_ms) // 1000}s / {_OP25_SITE_SELECTOR_POST_RESTART_GRACE_MS // 1000}s)",
                }, sys_state)
            stale_times = [
                int(ts)
                for ts in (sys_state.get("_stale_window_times_ms") or [])
                if int(ts) > (now_ms - _OP25_SITE_SELECTOR_STALE_WINDOW_WINDOW_MS)
            ]
            last_stale_time_ms = int(sys_state.get("_last_stale_window_time_ms") or 0)
            if last_stale_time_ms <= 0 or (now_ms - last_stale_time_ms) >= 30_000:
                stale_times.append(now_ms)
                sys_state["_last_stale_window_time_ms"] = now_ms
            sys_state["_stale_window_times_ms"] = stale_times
            sys_state["stale_window_count"] = len(stale_times)
            if len(stale_times) >= 6:
                if not _same_site_restart_enabled():
                    return ({
                        "action": "stay",
                        "site_id": selected_site_id,
                        "selection_mode": str(sys_state.get("selection_mode") or "auto"),
                        "reason_code": "stay_same_site_restart_disabled",
                        "reason_text": "Current site unhealthy, no alternate is available, and same-site restart is disabled",
                    }, sys_state)
                return ({
                    "action": "same_site_restart",
                    "site_id": selected_site_id,
                    "selection_mode": str(sys_state.get("selection_mode") or "auto"),
                    "reason_code": "same_site_restart_stale",
                    "reason_text": f"Repeated stale windows on {selected.get('site_name')}",
                }, sys_state)
            return ({
                "action": "stay",
                "site_id": selected_site_id,
                "selection_mode": str(sys_state.get("selection_mode") or "auto"),
                "reason_code": "stay_current_unhealthy_no_alternate",
                "reason_text": "Current site unhealthy and no better alternate is available",
            }, sys_state)

        evidence_other_hits = any(
            int(row.get("recent_monitored_tg_hits") or 0) > 0
            for row in candidates
            if str(row.get("site_id") or "") != selected_site_id
        )
        current_monitored = int(selected.get("recent_monitored_tg_hits") or 0)
        current_any = int(selected.get("recent_any_grants") or 0)
        unproductive_since_ms = _ms_from_iso(str(sys_state.get("unproductive_since") or ""))
        if current_monitored > 0:
            sys_state["unproductive_since"] = ""
            unproductive_since_ms = 0
        elif current_any > 0 or evidence_other_hits:
            if unproductive_since_ms <= 0:
                sys_state["unproductive_since"] = _iso_utc(now_ms)
                unproductive_since_ms = now_ms
        else:
            sys_state["unproductive_since"] = ""
            unproductive_since_ms = 0

        current_unproductive = bool(
            unproductive_since_ms > 0
            and (now_ms - unproductive_since_ms) >= unproductive_window_ms
            and (current_any > 0 or evidence_other_hits)
        )
        if current_unproductive and best_alternate:
            best_alternate_score = int(best_alternate.get("score") or 0)
            if best_alternate_score >= current_score + switch_margin:
                return ({
                    "action": "switch",
                    "site_id": str(best_alternate.get("site_id") or ""),
                    "selection_mode": str(sys_state.get("selection_mode") or "auto"),
                    "reason_code": "site_switch_unproductive",
                    "reason_text": f"Current site healthy but unproductive; switching to {best_alternate.get('site_name')}",
                }, sys_state)

        if current_dwell_ms < min_dwell_ms:
            return ({
                "action": "stay",
                "site_id": selected_site_id,
                "selection_mode": str(sys_state.get("selection_mode") or "auto"),
                "reason_code": "stay_current_min_dwell",
                "reason_text": "Retaining current site during minimum dwell window",
            }, sys_state)

        return ({
            "action": "stay",
            "site_id": selected_site_id,
            "selection_mode": str(sys_state.get("selection_mode") or "auto"),
            "reason_code": "stay_current_healthy",
            "reason_text": "Current site remains healthy and preferred by policy/score",
        }, sys_state)

    def _evaluate_site_selector(self, profile_dir: str, status: dict) -> dict[str, Any]:
        runtime_systems, selector_state = self._read_runtime_systems(profile_dir)
        profile_id = os.path.basename(profile_dir.rstrip(os.sep))
        systems_state = selector_state.setdefault("systems", {})
        monitored_tgids = self._monitored_tgids(profile_dir)
        now_ms = _now_ms()
        changed = False
        restart_requests: list[dict[str, Any]] = []
        selection_payload: dict[str, Any] = {}

        for system in runtime_systems:
            key = _state_system_key(profile_id, system["name"])
            sys_state = systems_state.get(key)
            if not isinstance(sys_state, dict):
                sys_state = _initial_selector_system_state(system)
                systems_state[key] = sys_state
                changed = True
            state_before = json.dumps(sys_state, sort_keys=True)

            candidates_by_id = self._candidate_state_map(system, sys_state)
            current_site_id = str(sys_state.get("selected_site_id") or system.get("active_site_id") or "").strip()
            metrics = self._status_metrics_for_system(
                status,
                system["name"],
                monitored_tgids,
                runtime_system_count=len(runtime_systems),
            )
            if current_site_id in candidates_by_id:
                candidates_by_id[current_site_id].update(metrics)
                candidates_by_id[current_site_id]["_last_sample_time_ms"] = now_ms

            policy = system.get("site_policy") or {}
            revisit_raw = sys_state.get("revisit_block_until") or {}
            if not isinstance(revisit_raw, dict):
                revisit_raw = {}

            candidate_rows: list[dict[str, Any]] = []
            unproductive_since_ms = _ms_from_iso(str(sys_state.get("unproductive_since") or ""))
            unproductive_window_ms = int(policy.get("unproductive_window_sec") or 300) * 1000
            for site in system.get("sites") or []:
                site_id = str(site.get("site_id") or "")
                candidate = candidates_by_id.get(site_id) or _candidate_state_defaults(site)
                candidate["enabled"] = _parse_enabled(site.get("enabled", True))
                candidate["_avoided"] = site_id in set(policy.get("avoid_site_ids") or [])
                candidate["_revisit_block_until_ms"] = _ms_from_iso(str(revisit_raw.get(site_id) or ""))
                candidate["score"] = _compute_candidate_score(candidate, system, now_ms=now_ms)
                state = "candidate"
                exclusion_reason = ""
                demotion_reason = ""
                if not candidate["enabled"]:
                    state = "disabled"
                    exclusion_reason = "site disabled by profile"
                elif _candidate_cooldown_active(candidate, now_ms=now_ms):
                    state = "cooldown"
                    demotion_reason = "under revisit cooldown"
                elif site_id == current_site_id:
                    state = "selected"
                elif candidate["_avoided"]:
                    state = "avoided"
                    demotion_reason = "avoid policy"
                elif site_id in set(policy.get("preferred_site_ids") or []):
                    state = "preferred"
                if state not in {"disabled", "cooldown", "avoided"} and _candidate_is_unhealthy(candidate):
                    state = "unhealthy"
                    demotion_reason = "stale decode or unlocked control"
                if state == "selected" and not _candidate_is_unhealthy(candidate):
                    current_unproductive = bool(
                        int(candidate.get("recent_monitored_tg_hits") or 0) == 0
                        and unproductive_since_ms > 0
                        and (now_ms - unproductive_since_ms) >= unproductive_window_ms
                    )
                    if current_unproductive:
                        state = "unproductive"
                        demotion_reason = "healthy but unproductive"
                candidate["state"] = state
                candidate["exclusion_reason"] = exclusion_reason
                candidate["demotion_reason"] = demotion_reason
                candidate_rows.append(candidate)

            decision, sys_state = self._selector_decision_for_system(
                system,
                sys_state,
                candidate_rows,
                now_ms=now_ms,
            )
            reason_code = str(decision.get("reason_code") or "")
            reason_text = str(decision.get("reason_text") or "")
            target_site_id = str(decision.get("site_id") or current_site_id or "")
            selection_mode = str(decision.get("selection_mode") or sys_state.get("selection_mode") or "auto")
            if target_site_id != current_site_id and decision.get("action") == "switch":
                previous_site_id = current_site_id
                previous_name = str(sys_state.get("selected_site_name") or "")
                sys_state["selected_site_id"] = target_site_id
                selected_row = next((row for row in candidate_rows if str(row.get("site_id") or "") == target_site_id), None)
                sys_state["selected_site_name"] = str(selected_row.get("site_name") or "") if selected_row else ""
                sys_state["selection_mode"] = selection_mode
                sys_state["reason_code"] = reason_code
                sys_state["reason_text"] = reason_text
                restart_requests.append({
                    "type": "switch",
                    "system_name": system["name"],
                    "previous_site_id": previous_site_id,
                    "selected_site_id": target_site_id,
                    "reason_code": reason_code,
                    "reason_text": reason_text,
                    "selection_mode": selection_mode,
                    "previous_site_name": previous_name,
                })
                changed = True
            else:
                sys_state["selection_mode"] = selection_mode
                sys_state["reason_code"] = reason_code
                sys_state["reason_text"] = reason_text
                if decision.get("action") == "same_site_restart":
                    restart_requests.append({
                        "type": "same_site_restart",
                        "system_name": system["name"],
                        "previous_site_id": target_site_id,
                        "selected_site_id": target_site_id,
                        "reason_code": reason_code,
                        "reason_text": reason_text,
                        "selection_mode": selection_mode,
                        "previous_site_name": str(sys_state.get("selected_site_name") or ""),
                    })
                    changed = True
                elif decision.get("action") == "generic_restart":
                    restart_requests.append({
                        "type": "generic_restart",
                        "system_name": system["name"],
                        "previous_site_id": target_site_id,
                        "selected_site_id": target_site_id,
                        "reason_code": reason_code,
                        "reason_text": reason_text,
                        "selection_mode": selection_mode,
                        "previous_site_name": str(sys_state.get("selected_site_name") or ""),
                    })
                    changed = True

            sys_state["candidates"] = [{key: value for key, value in row.items() if not key.startswith("_")} for row in candidate_rows]
            systems_state[key] = sys_state
            selection_payload[system["name"]] = {
                "name": system["name"],
                "selected_site_id": str(sys_state.get("selected_site_id") or ""),
                "selected_site_name": str(sys_state.get("selected_site_name") or ""),
                "selection_mode": str(sys_state.get("selection_mode") or ""),
                "reason_code": str(sys_state.get("reason_code") or ""),
                "reason_text": str(sys_state.get("reason_text") or ""),
                "last_switch_time": str(sys_state.get("last_switch_time") or ""),
                "switch_count": int(sys_state.get("switch_count") or 0),
                "same_site_restart_count": int(sys_state.get("same_site_restart_count") or 0),
                "site_switch_restart_count": int(sys_state.get("site_switch_restart_count") or 0),
                "generic_restart_count": int(sys_state.get("generic_restart_count") or 0),
                "stale_window_count": int(sys_state.get("stale_window_count") or 0),
                "candidate_sites": list(sys_state.get("candidates") or []),
                "policy_warnings": list(system.get("site_policy_warnings") or []),
            }
            if json.dumps(sys_state, sort_keys=True) != state_before:
                changed = True

        if changed:
            _save_selector_state(self._runtime_dir, selector_state)
        self._runtime_metrics_data["site_selector"] = selection_payload
        self._runtime_metrics_data["site_selector_state_path"] = _selector_state_path(self._runtime_dir)
        return {"systems": selection_payload, "restart_requests": restart_requests}

    def _regenerate_runtime_via_script(self) -> tuple[bool, str]:
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "ensure-op25-runtime.py",
        )
        try:
            result = subprocess.run(
                ["python3", script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except Exception as exc:
            return False, str(exc)
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout or "").strip() or "ensure-op25-runtime failed"

    def _handle_selector_restart_requests(self, profile_dir: str, restart_requests: list[dict[str, Any]]) -> None:
        if not restart_requests:
            return
        now_ms = _now_ms()
        state = _load_selector_state(self._runtime_dir)
        systems_state = state.setdefault("systems", {})
        profile_id = os.path.basename(profile_dir.rstrip(os.sep))
        runtime_system_defs = _normalize_runtime_system_definitions(
            profile_dir,
            op25_overrides=_read_op25_system_config(profile_dir),
        )
        runtime_policies = {
            str(system.get("name") or "").strip(): (system.get("site_policy") or {})
            for system in runtime_system_defs
        }
        runtime_system_map = {
            str(system.get("name") or "").strip(): system
            for system in runtime_system_defs
            if str(system.get("name") or "").strip()
        }
        eligible_requests: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        for request in restart_requests:
            system_name = str(request.get("system_name") or "").strip()
            if not system_name:
                continue
            key = _state_system_key(profile_id, system_name)
            sys_state = systems_state.get(key)
            if not isinstance(sys_state, dict):
                continue
            last_restart_time_ms = int(sys_state.get("_last_restart_time_ms") or 0)
            if last_restart_time_ms > 0 and (now_ms - last_restart_time_ms) < _OP25_SITE_SELECTOR_ACTION_COOLDOWN_MS:
                continue
            reason_code = str(request.get("reason_code") or "")
            score_summary = ";".join(
                f"{row.get('site_id')}={row.get('score')}"
                for row in (sys_state.get("candidates") or [])
                if isinstance(row, dict)
            )
            logger.info(
                "op25_site_selector action=%s system=%s selected_site_id=%s previous_site_id=%s reason_code=%s scores=%s",
                request.get("type"),
                system_name,
                request.get("selected_site_id"),
                request.get("previous_site_id"),
                reason_code,
                score_summary,
            )
            eligible_requests.append((request, key, sys_state))

        if not eligible_requests:
            return

        for request, _key, sys_state in eligible_requests:
            if str(request.get("type") or "") != "same_site_restart":
                continue
            system_name = str(request.get("system_name") or "").strip()
            system_def = runtime_system_map.get(system_name) or {}
            selected_site_id = str(
                request.get("selected_site_id")
                or sys_state.get("selected_site_id")
                or ""
            ).strip()
            if not selected_site_id:
                continue
            selected_site = next(
                (
                    site
                    for site in (system_def.get("sites") or [])
                    if str(site.get("site_id") or "").strip() == selected_site_id
                ),
                None,
            )
            if not isinstance(selected_site, dict):
                continue
            channels = _ordered_control_channels_for_state(
                list(selected_site.get("control_channels_hz") or []),
                0,
            )
            if len(channels) <= 1:
                continue
            current_hz = int(sys_state.get("selected_control_frequency_hz") or 0)
            try:
                current_idx = channels.index(current_hz)
            except ValueError:
                current_idx = -1
            next_hz = channels[(current_idx + 1) % len(channels)]
            if next_hz <= 0 or next_hz == current_hz:
                continue
            sys_state["selected_control_frequency_hz"] = next_hz
            request["previous_control_frequency_hz"] = current_hz
            request["selected_control_frequency_hz"] = next_hz
            request["reason_text"] = (
                f"{str(request.get('reason_text') or '').strip()} "
                f"(rotating control to {next_hz / 1_000_000:.5f} MHz)"
            ).strip()

        _save_selector_state(self._runtime_dir, state)
        ok, err = self._regenerate_runtime_via_script()
        if not ok:
            for request, _key, _sys_state in eligible_requests:
                logger.warning(
                    "op25_site_selector action=restart_failed system=%s selected_site_id=%s previous_site_id=%s reason_code=%s error=%s",
                    request.get("system_name"),
                    request.get("selected_site_id"),
                    request.get("previous_site_id"),
                    request.get("reason_code"),
                    err,
                )
            return

        restart_result = self.restart()
        if isinstance(restart_result, tuple):
            restart_ok = bool(restart_result[0])
            restart_err = str(restart_result[1] or "")
        else:
            restart_ok = bool(restart_result)
            restart_err = ""
        if not restart_ok:
            for request, _key, _sys_state in eligible_requests:
                logger.warning(
                    "op25_site_selector action=restart_failed system=%s selected_site_id=%s previous_site_id=%s reason_code=%s error=%s",
                    request.get("system_name"),
                    request.get("selected_site_id"),
                    request.get("previous_site_id"),
                    request.get("reason_code"),
                    restart_err or "restart returned false",
                )
            return

        success_iso = _iso_utc(now_ms)
        for request, key, sys_state in eligible_requests:
            sys_state["_last_restart_time_ms"] = now_ms
            action_type = str(request.get("type") or "")
            if action_type == "switch":
                sys_state["switch_count"] = int(sys_state.get("switch_count") or 0) + 1
                sys_state["site_switch_restart_count"] = int(sys_state.get("site_switch_restart_count") or 0) + 1
                sys_state["last_switch_time"] = success_iso
                sys_state["current_site_since"] = success_iso
                previous_site_id = str(request.get("previous_site_id") or "")
                if previous_site_id:
                    revisit = dict(sys_state.get("revisit_block_until") or {})
                    system_name = str(request.get("system_name") or "").strip()
                    policy = runtime_policies.get(system_name) or {}
                    revisit[previous_site_id] = _iso_utc(now_ms + int(policy.get("revisit_cooldown_sec") or 180) * 1000)
                    sys_state["revisit_block_until"] = revisit
            elif action_type == "same_site_restart":
                sys_state["same_site_restart_count"] = int(sys_state.get("same_site_restart_count") or 0) + 1
            elif action_type == "generic_restart":
                sys_state["generic_restart_count"] = int(sys_state.get("generic_restart_count") or 0) + 1
            # Clear stale window history after any restart so accumulated
            # stale events don't immediately re-trigger another restart.
            sys_state["_stale_window_times_ms"] = []
            sys_state["_last_stale_window_time_ms"] = 0
            sys_state["stale_window_count"] = 0
            systems_state[key] = sys_state
        _save_selector_state(self._runtime_dir, state)

    def preflight(self) -> dict:
        """Return health payload compatible with the scheduler's expectations."""
        status = self._poll_op25_status()
        profile_dir = self._read_active_profile_dir()
        site_selection = {"systems": {}}
        if profile_dir:
            try:
                site_selection = self._evaluate_site_selector(profile_dir, status)
                self._handle_selector_restart_requests(profile_dir, site_selection.get("restart_requests") or [])
            except Exception:
                logger.exception("op25_site_selector action=preflight_error")
        # OP25 status format varies; adapt to common fields.
        locked = bool(
            status.get("locked")
            or status.get("control_channel_locked")
            or self._root_control_channel_locked(status)
        )
        ber = float(status.get("ber", 0) or 0)
        decode_rate = float(status.get("decode_rate", 0) or 0)
        control_decode_available = bool(
            status.get("control_decode_available")
            or locked
            or decode_rate > 0
            or self._root_trunk_decode_available(status)
        )

        return {
            "control_channel_locked": locked,
            "control_decode_available": control_decode_available,
            "tuner_busy": False,
            "tuner_busy_count": 0,
            "tuner_busy_lines": [],
            "op25_ber": ber,
            "op25_decode_rate": decode_rate,
            "op25_status_raw": status,
            "op25_site_selection": site_selection.get("systems") or {},
        }

    def runtime_metrics(self) -> dict:
        return dict(self._runtime_metrics_data)

    # ------------------------------------------------------------------
    # Ensure runtime seed
    # ------------------------------------------------------------------

    def ensure_runtime_seed(self, profile_id: str = "") -> tuple[bool, str, bool]:
        """Generate OP25 runtime config if not already present."""
        trunk_path = os.path.join(self._runtime_dir, "trunk.tsv")
        if os.path.isfile(trunk_path):
            return True, "", False

        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            return False, "no active profile", False

        systems = _read_system_definitions(profile_dir)
        if not systems:
            return False, "no systems defined in profile", False

        ok, err = self._write_runtime_config(profile_dir, systems)
        if not ok:
            return False, err, False
        return True, "", True
