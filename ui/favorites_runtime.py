"""Apply HP/SB3 scan-pool channels to analog and digital runtime profiles."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

try:
    from .config import (
        AIRBAND_MAX_MHZ,
        AIRBAND_MIN_MHZ,
        CONFIG_SYMLINK,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_TERTIARY,
        GROUND_CONFIG_PATH,
        PROFILES_DIR,
    )
    from .dongle_allocator import allocate as allocate_dongles
    from .profile_config import (
        enforce_profile_index,
        find_profile,
        guess_current_profile,
        load_profiles_registry,
        read_active_config_path,
        safe_profile_path,
        save_profiles_registry,
        set_profile,
        split_profiles,
        write_airband_flag,
        write_combined_config,
        write_freqs_labels,
    )
    from .managed_analog_controls import apply_managed_profile_controls
    from .scan_mode_controller import get_scan_mode_controller
    from .scanner import mark_analog_hit_cutoff
    from .systemd import restart_rtl
    from .v3_runtime import set_active_analog_profile, upsert_analog_profile
except ImportError:
    from ui.config import (
        AIRBAND_MAX_MHZ,
        AIRBAND_MIN_MHZ,
        CONFIG_SYMLINK,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_TERTIARY,
        GROUND_CONFIG_PATH,
        PROFILES_DIR,
    )
    from ui.dongle_allocator import allocate as allocate_dongles
    from ui.profile_config import (
        enforce_profile_index,
        find_profile,
        guess_current_profile,
        load_profiles_registry,
        read_active_config_path,
        safe_profile_path,
        save_profiles_registry,
        set_profile,
        split_profiles,
        write_airband_flag,
        write_combined_config,
        write_freqs_labels,
    )
    from ui.managed_analog_controls import apply_managed_profile_controls
    from ui.scan_mode_controller import get_scan_mode_controller
    from ui.scanner import mark_analog_hit_cutoff
    from ui.systemd import restart_rtl
    from ui.v3_runtime import set_active_analog_profile, upsert_analog_profile

logger = logging.getLogger(__name__)


_RSPDUO_USB_ENUM_RETRY_SLEEP_SEC = 2.0


def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _enumerated_rtl_serials() -> set[str] | None:
    """Best-effort: serials currently visible to librtlsdr.

    Used by ``_digital_serials()`` to drop configured-but-absent dongles
    from the digital pool so the allocator never hands the OP25 / SDRTrunk
    runtime an EEPROM serial that ``osmosdr.source('rtl=<serial>')`` would
    fail to open.

    Return values:

    * ``set`` of serial strings — ``rtl_test`` ran successfully; the set
      reflects what is currently present (may be empty if no RTL-SDR is
      attached, in which case the caller should drop ALL configured
      RTL serials from the pool).
    * ``None`` — ``rtl_test`` itself failed (binary missing, timeout,
      crash).  The caller should NOT filter, preserving the configured
      pool as-is so digital decoding doesn't silently lose dongles on
      hosts without the SDR tooling.
    """
    try:
        result = subprocess.run(
            ["rtl_test", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        logger.debug("rtl_test enumeration for digital pool filter failed", exc_info=True)
        return None

    out: set[str] = set()
    # Format: "  0:  Realtek, RTL2832U, SN: 80000003"
    for raw in (result.stdout or "").splitlines():
        m = re.search(r"^\s*\d+:\s+.*SN:\s+(\S+)\s*$", raw)
        if m:
            out.add(m.group(1).strip())
    return out


def _digital_serials() -> list[str]:
    """Collect configured digital dongle serials (primary + secondary + tertiary).

    Filters out serials that librtlsdr cannot currently see, so the
    allocator never hands the runtime a dead EEPROM serial.  When
    ``rtl_test`` itself fails (returns ``None``), the configured list is
    returned unfiltered — better to let the allocator try a stale serial
    than silently lose digital decoding on hosts without the SDR tooling.
    """
    configured: list[str] = []
    seen: set[str] = set()
    for s in (DIGITAL_RTL_SERIAL, DIGITAL_RTL_SERIAL_SECONDARY, DIGITAL_RTL_SERIAL_TERTIARY):
        val = str(s or "").strip()
        if val and val not in seen:
            seen.add(val)
            configured.append(val)
    if not configured:
        return configured

    enumerated = _enumerated_rtl_serials()
    if enumerated is None:
        # Tooling failure: don't filter.
        return configured

    # rtl_test ran (possibly returning 0 devices) — filter to what's present.
    filtered = [s for s in configured if s in enumerated]
    dropped = [s for s in configured if s not in enumerated]
    if dropped:
        logger.info(
            "Digital pool filter dropped configured-but-absent dongles: %s "
            "(enumerated: %s)",
            ", ".join(sorted(dropped)),
            ", ".join(sorted(enumerated)) or "(none)",
        )
    return filtered


def _sysfs_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.readline().strip()
    except Exception:
        return ""


def _rspduo_usb_sample() -> tuple[int, list[str]]:
    """Single sysfs pass: return (matching-device-count, collected-serials).

    Counts every ``/sys/bus/usb/devices`` entry whose ``idVendor``/``idProduct``
    match the RSPduo VID:PID (``1df7:3020``).  The count is populated even when
    the device's firmware load is still mid-flight and ``serial`` is empty —
    that asymmetry is the signal used by ``_rspduo_usb_serials()`` to detect
    the post-boot USB-enum race.
    """
    base = "/sys/bus/usb/devices"
    if not os.path.isdir(base):
        return 0, []
    try:
        entries = os.listdir(base)
    except Exception:
        return 0, []
    count = 0
    serials: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        dev_dir = os.path.join(base, entry)
        vendor = _sysfs_text(os.path.join(dev_dir, "idVendor")).lower()
        product = _sysfs_text(os.path.join(dev_dir, "idProduct")).lower()
        if vendor != "1df7" or product != "3020":
            continue
        count += 1
        serial = _sysfs_text(os.path.join(dev_dir, "serial")).strip().upper()
        if not serial or serial in seen:
            continue
        seen.add(serial)
        serials.append(serial)
    return count, sorted(serials)


def _rtl_airband_dedicated_rspduo_serials() -> set[str]:
    """Return device serials currently claimed by rtl-airband.

    Authoritative source: the active combined rtl-airband config.
    The config IS the declaration of which devices rtl-airband owns
    — derive from it rather than maintaining a parallel env var that
    has to be kept in sync by hand.

    Both legacy ``type = "rtlsdr"; serial = "..."`` and SoapySDR
    ``type = "soapysdr"; device_string = "...serial=X,..."`` blocks
    are recognized (the latter via
    ``ui.combined_status.serials_claimed_by_combined_config``).

    The ``RTL_AIRBAND_RSPDUO_SERIAL`` env var remains as an opt-in
    override / belt-and-suspenders for setups where the combined
    config can't be read (e.g. rtl-airband not yet installed, or
    operator wants to reserve a device that isn't in the config
    yet).  Env-var values are unioned with the config-derived set.
    """
    out: set[str] = set()
    try:
        try:
            from .combined_status import serials_claimed_by_combined_config
        except ImportError:  # pragma: no cover
            from ui.combined_status import serials_claimed_by_combined_config  # type: ignore[no-redef]
        out |= serials_claimed_by_combined_config()
    except Exception:
        logger.debug(
            "favorites_runtime: could not read combined config for "
            "rtl-airband-claimed serials (continuing with env var only)",
            exc_info=True,
        )
    raw = str(os.getenv("RTL_AIRBAND_RSPDUO_SERIAL", "") or "").strip()
    if raw:
        for token in raw.replace(";", ",").split(","):
            token = token.strip()
            if token:
                out.add(token)
    return out


def _rspduo_usb_serials() -> list[str]:
    """Resolve attached RSPduo serial numbers via Linux sysfs.

    Two-pass with one-shot retry to survive the sdrplay-api firmware-load
    USB-enum race on boot: when ``idVendor``/``idProduct`` are populated but
    ``serial`` is still empty for one or more devices, the first sample
    yields fewer serials than VID:PID matches.  We sleep briefly and
    re-sample; if the mismatch persists we return ``[]`` and let the caller
    fall back to SoapySDR enumeration (which has its own retries).

    Retry sleep is overridable via ``RSPDUO_USB_ENUM_RETRY_SLEEP_SEC``.

    Serials listed in ``RTL_AIRBAND_RSPDUO_SERIAL`` are filtered out
    of the result so the digital allocator can't claim a tuner that
    rtl-airband already owns in DT mode.
    """
    excluded = _rtl_airband_dedicated_rspduo_serials()

    def _filter(serials: list[str]) -> list[str]:
        if not excluded:
            return serials
        return [s for s in serials if s not in excluded]

    count, serials = _rspduo_usb_sample()
    if count == 0:
        return []
    if len(serials) == count:
        filtered = _filter(serials)
        if excluded and len(filtered) != len(serials):
            logger.info(
                "RSPduo USB enum: excluding %d serial(s) reserved for rtl-airband: %s",
                len(serials) - len(filtered),
                sorted(excluded),
            )
        return filtered

    try:
        sleep_s = float(
            os.getenv("RSPDUO_USB_ENUM_RETRY_SLEEP_SEC", "")
            or _RSPDUO_USB_ENUM_RETRY_SLEEP_SEC
        )
    except (TypeError, ValueError):
        sleep_s = _RSPDUO_USB_ENUM_RETRY_SLEEP_SEC

    logger.info(
        "RSPduo USB enum incomplete (%d device(s), %d serial(s)) — retry in %.1fs",
        count, len(serials), sleep_s,
    )
    time.sleep(max(0.0, sleep_s))

    count2, serials2 = _rspduo_usb_sample()
    if count2 > 0 and len(serials2) == count2:
        logger.info(
            "RSPduo USB enum retry recovered %d serial(s)", len(serials2)
        )
        filtered2 = _filter(serials2)
        if excluded and len(filtered2) != len(serials2):
            logger.info(
                "RSPduo USB enum (retry): excluding %d serial(s) reserved for rtl-airband: %s",
                len(serials2) - len(filtered2),
                sorted(excluded),
            )
        return filtered2

    logger.warning(
        "RSPduo USB enum still incomplete after retry "
        "(pass1: %d/%d, pass2: %d/%d) — returning [] for SoapySDR fallback",
        len(serials), count, len(serials2), count2,
    )
    return []


def _rspduo_tuner_ids(*, max_tuners: int | None = None) -> list[str]:
    """Discover RSPduo tuner identifiers available to the digital backend.

    Prefer a Linux sysfs probe of the attached SDRplay USB device so we can
    derive the canonical tuner IDs without touching the SoapySDR Python
    bindings.  On the Micro this avoids a hanging
    ``SoapySDR.Device.enumerate(driver=sdrplay)`` call while still yielding
    the exact IDs consumed by OP25.  When sysfs is unavailable, fall back to
    SoapySDR enumeration.

    Returns ``"RSPduo Tuner 1 SER#<serial>"`` for each attached device
    first. When the caller requests more control slots than there are
    physical RSPduos, and the OP25 split-process path is enabled, the
    corresponding ``"RSPduo Tuner 2 SER#<serial>"`` identifiers are
    appended after all Tuner 1 entries.

    The 12-bit ADC and lower noise figure make the RSPduo a higher-quality
    control-channel receiver than the 8-bit RTL-SDRs it joins in the pool,
    so its tuner identifiers are passed as priority entries to the allocator.

    Tuner 2 is withheld unless ``OP25_RSPDUO_SPLIT_PROCESSES`` is enabled
    because the current single-process multi_rx.py path cannot safely open
    Master and Slave back-to-back in one process. With multiple physical
    RSPduos we prefer independent Tuner 1 receivers across boxes before
    reaching for any Slave tuner.

    Returns an empty list when SoapySDR isn't installed, when no RSPduo
    is attached, or on any enumeration failure — the allocator then runs
    as a pure-RTL pool without RSPduo participation.
    """
    tuner_limit: int | None = None
    if max_tuners is not None:
        try:
            tuner_limit = max(0, int(max_tuners))
        except Exception:
            tuner_limit = 1

    out: list[str] = []
    allow_dual = tuner_limit is not None and tuner_limit >= 2 and _env_flag("OP25_RSPDUO_SPLIT_PROCESSES", "1")

    def _expand_rspduo_ids(serials: list[str]) -> list[str]:
        limit = len(serials) if tuner_limit is None else tuner_limit
        if limit <= 0:
            return []
        expanded: list[str] = []
        for serial in serials:
            expanded.append(f"RSPduo Tuner 1 SER#{serial}")
            if len(expanded) >= limit:
                return expanded
        if allow_dual:
            for serial in serials:
                expanded.append(f"RSPduo Tuner 2 SER#{serial}")
                if len(expanded) >= limit:
                    return expanded
        return expanded

    sysfs_serials = _rspduo_usb_serials()
    if sysfs_serials:
        return _expand_rspduo_ids(sysfs_serials)

    try:
        import SoapySDR  # type: ignore[import-untyped]
    except ImportError:
        return out
    try:
        results = SoapySDR.Device.enumerate(dict(driver="sdrplay"))
    except Exception:
        logger.debug("SoapySDR RSPduo enumeration failed", exc_info=True)
        return out

    seen_serials: set[str] = set()
    for kw in (results or []):
        kvs = _parse_soapy_kwargs(kw)
        serial = kvs.get("serial", "").strip().upper()
        hardware = kvs.get("label") or kvs.get("hardware") or ""
        if not serial or "RSPduo" not in hardware or serial in seen_serials:
            continue
        seen_serials.add(serial)
        out.append(serial)
    return _expand_rspduo_ids(out)


def _parse_soapy_kwargs(kw: Any) -> dict[str, str]:
    """Parse a SoapySDR Kwargs object into a plain dict.

    SoapySDRKwargs (the SWIG-wrapped std::map<string,string>) does NOT
    implement Python's mapping protocol fully on older bindings — no
    ``.get()`` method, no ``in`` operator.  But ``str(kw)`` always
    produces the canonical ``{key=value, key=value}`` format, so parse
    that.  When ``kw`` is already a plain dict (as in the test fakes),
    just shallow-copy it.
    """
    if isinstance(kw, dict):
        return {str(k): str(v) for k, v in kw.items()}
    text = str(kw or "").strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    out: dict[str, str] = {}
    for token in text.split(","):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        out[key.strip()] = value.strip()
    return out


_MANAGED_AIR_ID = "hp3_favorites_airband"
_MANAGED_GROUND_ID = "hp3_favorites_ground"
_MANAGED_DIGITAL_ID = "hp3_favorites_digital"
_MANAGED_AIR_LABEL = "HP3 Favorites Airband"
_MANAGED_GROUND_LABEL = "HP3 Favorites Ground"
_MAX_FREQS_PER_BAND = 256
_SYNC_LOCK = threading.Lock()
_LAST_SIGNATURE = ""
_LAST_DIGITAL_SIGNATURE = ""
_LAST_RESULT: dict[str, Any] = {"ok": True, "changed": False}
_LAST_DIGITAL_RESULT: dict[str, Any] = {"ok": True, "changed": False}
_LAST_ACTIVE_POOL_SIGNATURE = ""
_LAST_ACTIVE_POOL_APPLIED_AT_MS = 0
_LAST_ACTIVE_POOL_MODE = "expert"
_LAST_ACTIVE_POOL: dict[str, Any] = {"trunked_sites": [], "conventional": []}


def _profile_path_for(profile_id: str) -> str:
    filename = f"rtl_airband_{profile_id}.conf"
    return os.path.join(str(PROFILES_DIR), filename)


_RE_MANAGED_SERIAL = re.compile(r'^(\s*)serial(\s*=\s*)"([^"]*)"(\s*;.*)$')


def _enforce_managed_profile_serial(conf_path: str, desired_serial: str) -> bool:
    """Re-sync a managed profile's ``serial = "…";`` line to match env.

    Returns True if the file was rewritten. No-op if ``desired_serial`` is
    empty/unset, if the file is missing, or if no ``serial`` line exists
    (seeding a serial is left to the runtime config builder).
    """
    desired = str(desired_serial or "").strip()
    if not desired:
        return False
    conf_path = os.path.realpath(conf_path)
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return False

    out: list[str] = []
    changed = False
    for line in lines:
        match = _RE_MANAGED_SERIAL.match(line)
        if match and match.group(3) != desired:
            indent, equals, _, tail = match.groups()
            trailing_nl = "\n" if line.endswith("\n") else ""
            out.append(f'{indent}serial{equals}"{desired}"{tail}{trailing_nl}')
            changed = True
            continue
        out.append(line)

    if not changed:
        return False

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.writelines(out)
    os.replace(tmp, conf_path)
    return True


def _coerce_float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    if not (parsed > 0):
        return None
    return parsed


def _normalize_label(entry: dict[str, Any], fallback: str) -> str:
    alpha = str(entry.get("alpha_tag") or entry.get("channel_name") or "").strip()
    if alpha:
        return alpha
    system_name = str(entry.get("system_name") or "").strip()
    if system_name:
        return system_name
    return fallback


def _is_airband_frequency(freq_mhz: float) -> bool:
    return float(AIRBAND_MIN_MHZ) <= freq_mhz <= float(AIRBAND_MAX_MHZ)


def _minimal_profile_template(airband: bool) -> str:
    default_freq = "118.6000" if airband else "462.6500"
    default_mod = "am" if airband else "nfm"
    desired_index = 0 if airband else 1
    default_squelch = -52 if airband else -70
    return (
        f"airband = {'true' if airband else 'false'};\n\n"
        "devices:\n"
        "({\n"
        "  type = \"rtlsdr\";\n"
        f"  index = {desired_index};\n"
        "  mode = \"scan\";\n"
        "  gain = 32.800;   # UI_CONTROLLED\n\n"
        "  channels:\n"
        "  (\n"
        "    {\n"
        f"      freqs = ({default_freq});\n\n"
        f"      modulation = \"{default_mod}\";\n"
        "      bandwidth = 12000;\n"
        f"      squelch_threshold = {default_squelch};  # UI_CONTROLLED\n"
        "      squelch_delay = 0.8;\n\n"
        "      outputs:\n"
        "      (\n"
        "        {\n"
        "          type = \"icecast\";\n"
        "          send_scan_freq_tags = true;\n"
        "          server = \"127.0.0.1\";\n"
        "          port = 8000;\n"
        "          mountpoint = \"scannerbox.mp3\";\n"
        "          username = \"source\";\n"
        "          password = \"062352\";\n"
        "          name = \"SprontPi Radio\";\n"
        "          genre = \"AIRBAND\";\n"
        "          description = \"HP3 Favorites\";\n"
        "          bitrate = 32;\n"
        "        }\n"
        "      );\n"
        "    }\n"
        "  );\n"
        "});\n"
    )


def _profile_has_freqs_block(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            text = handle.read()
    except Exception:
        return False
    return "freqs" in text and "=" in text


def _template_profile_path(
    profiles: list[dict[str, Any]],
    airband: bool,
    *,
    exclude_paths: set[str] | None = None,
) -> str:
    exclude = {os.path.realpath(p) for p in (exclude_paths or set()) if str(p).strip()}
    preferred_id = "none_airband" if airband else "none_ground"
    preferred = find_profile(profiles, preferred_id)
    if preferred:
        candidate = str(preferred.get("path") or "").strip()
        candidate_real = os.path.realpath(candidate) if candidate else ""
        if (
            candidate
            and os.path.isfile(candidate)
            and candidate_real not in exclude
            and _profile_has_freqs_block(candidate)
        ):
            return candidate

    for row in profiles:
        if bool(row.get("airband")) != bool(airband):
            continue
        candidate = str(row.get("path") or "").strip()
        candidate_real = os.path.realpath(candidate) if candidate else ""
        if (
            candidate
            and os.path.isfile(candidate)
            and candidate_real not in exclude
            and _profile_has_freqs_block(candidate)
        ):
            return candidate
    return ""


def _ensure_managed_profile(
    profiles: list[dict[str, Any]],
    *,
    profile_id: str,
    label: str,
    airband: bool,
) -> tuple[dict[str, Any], bool]:
    changed = False
    profile = find_profile(profiles, profile_id)
    desired_path = _profile_path_for(profile_id)
    safe_path = safe_profile_path(desired_path)
    if not safe_path:
        raise RuntimeError(f"invalid managed profile path for {profile_id}")

    needs_seed = (not os.path.isfile(safe_path)) or (not _profile_has_freqs_block(safe_path))
    if needs_seed:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        template_path = _template_profile_path(
            profiles,
            airband=airband,
            exclude_paths={safe_path},
        )
        if template_path and os.path.isfile(template_path):
            shutil.copyfile(template_path, safe_path)
        else:
            with open(safe_path, "w", encoding="utf-8") as handle:
                handle.write(_minimal_profile_template(airband=airband))
        changed = True

    write_airband_flag(safe_path, bool(airband))
    enforce_profile_index(safe_path)

    serial_env_key = "AIRBAND_RTL_SERIAL" if airband else "GROUND_RTL_SERIAL"
    desired_serial = os.environ.get(serial_env_key, "").strip()
    if _enforce_managed_profile_serial(safe_path, desired_serial):
        changed = True

    if profile is None:
        profile = {
            "id": profile_id,
            "label": label,
            "path": safe_path,
            "airband": bool(airband),
        }
        profiles.append(profile)
        changed = True
    else:
        expected = {
            "id": profile_id,
            "label": label,
            "path": safe_path,
            "airband": bool(airband),
        }
        for key, value in expected.items():
            if profile.get(key) != value:
                profile[key] = value
                changed = True

    try:
        upsert_analog_profile(profile)
    except Exception:
        # Runtime compile persistence is best-effort here.
        pass
    return profile, changed


def _select_fallback_profile(profiles: list[dict[str, Any]], target: str) -> str:
    fallback_id = "none_ground" if target == "ground" else "none_airband"
    row = find_profile(profiles, fallback_id)
    if row and os.path.isfile(str(row.get("path") or "")):
        return fallback_id
    return _MANAGED_GROUND_ID if target == "ground" else _MANAGED_AIR_ID


def _current_profile_id_for_target(target: str) -> str:
    try:
        conf_path, profiles, _ = _profiles_for_target(target)
    except Exception:
        return ""
    if not profiles:
        return ""
    current_real = os.path.realpath(str(conf_path or ""))
    for pid, _, path in profiles:
        if os.path.realpath(str(path or "")) == current_real:
            return str(pid or "").strip()
    if current_real and os.path.exists(current_real):
        return str(guess_current_profile(conf_path, profiles) or "").strip()
    return ""


def _desired_analog_profile_for_empty_result(
    profiles: list[dict[str, Any]],
    target: str,
    *,
    managed_profile_id: str,
) -> str:
    fallback_id = "none_ground" if target == "ground" else "none_airband"
    current_id = _current_profile_id_for_target(target)
    if current_id and current_id != fallback_id:
        return current_id
    return _select_fallback_profile(profiles, target)


def _normalize_conventional_pool(pool: dict[str, Any]) -> tuple[list[float], list[str], list[float], list[str]]:
    rows = pool.get("conventional")
    if not isinstance(rows, list):
        rows = []

    air_labels_by_freq: dict[float, str] = {}
    ground_labels_by_freq: dict[float, str] = {}

    for item in rows:
        if not isinstance(item, dict):
            continue
        freq = _coerce_float(item.get("frequency"))
        if freq is None:
            continue
        mhz = round(freq, 6)
        label = _normalize_label(item, f"{mhz:.4f}")
        if _is_airband_frequency(mhz):
            air_labels_by_freq.setdefault(mhz, label)
        else:
            ground_labels_by_freq.setdefault(mhz, label)

    air_freqs = sorted(air_labels_by_freq.keys())[:_MAX_FREQS_PER_BAND]
    ground_freqs = sorted(ground_labels_by_freq.keys())[:_MAX_FREQS_PER_BAND]
    air_labels = [air_labels_by_freq[freq] for freq in air_freqs]
    ground_labels = [ground_labels_by_freq[freq] for freq in ground_freqs]
    return air_freqs, air_labels, ground_freqs, ground_labels


def _profiles_for_target(target: str) -> tuple[str, list[tuple[str, str, str]], str]:
    profile_payload, profiles_airband, profiles_ground = split_profiles()
    del profile_payload
    if target == "ground":
        tuples = [
            (
                str(row.get("id") or ""),
                str(row.get("label") or ""),
                str(row.get("path") or ""),
            )
            for row in profiles_ground
            if str(row.get("id") or "").strip() and bool(row.get("exists"))
        ]
        conf_path = os.path.realpath(str(GROUND_CONFIG_PATH))
        symlink_path = str(GROUND_CONFIG_PATH)
    else:
        tuples = [
            (
                str(row.get("id") or ""),
                str(row.get("label") or ""),
                str(row.get("path") or ""),
            )
            for row in profiles_airband
            if str(row.get("id") or "").strip() and bool(row.get("exists"))
        ]
        conf_path = os.path.realpath(read_active_config_path())
        symlink_path = str(CONFIG_SYMLINK)
    return conf_path, tuples, symlink_path


def _switch_profile_if_needed(target: str, desired_profile_id: str) -> tuple[bool, str]:
    conf_path, profiles, symlink_path = _profiles_for_target(target)
    if not profiles:
        return False, f"no {target} profiles available"
    current_real = os.path.realpath(str(conf_path or ""))
    current_id = ""
    for pid, _, path in profiles:
        if os.path.realpath(str(path or "")) == current_real:
            current_id = str(pid or "").strip()
            break
    if not current_id and os.path.exists(current_real):
        current_id = str(guess_current_profile(conf_path, profiles) or "").strip()
    if current_id == desired_profile_id:
        return False, ""
    ok, changed = set_profile(desired_profile_id, conf_path, profiles, symlink_path)
    if not ok:
        return False, f"unknown {target} profile: {desired_profile_id}"
    if not changed:
        return False, ""
    try:
        set_active_analog_profile(target, desired_profile_id)
    except Exception:
        logger.debug(
            "Failed recording active managed analog profile for %s -> %s",
            target,
            desired_profile_id,
            exc_info=True,
        )
    mark_analog_hit_cutoff(target, time.time())
    return True, ""


def _mode_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"hp", "hp3"}:
        return "expert"
    if token in {"expert", "sb3", "legacy", "profile"}:
        return "expert"
    return "expert"


def _sanitize_scan_pool(pool: Any) -> dict[str, Any]:
    if not isinstance(pool, dict):
        return {"trunked_sites": [], "conventional": []}
    try:
        normalized = json.loads(json.dumps(pool))
    except Exception:
        normalized = dict(pool)
    trunked = normalized.get("trunked_sites")
    conventional = normalized.get("conventional")
    normalized["trunked_sites"] = trunked if isinstance(trunked, list) else []
    normalized["conventional"] = conventional if isinstance(conventional, list) else []
    return normalized


def _active_pool_signature(mode: str, pool: dict[str, Any]) -> str:
    payload = {
        "mode": _mode_token(mode),
        "pool": _sanitize_scan_pool(pool),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8", errors="ignore")).hexdigest()


def _set_last_runtime_scan_pool_locked(mode: str, pool: dict[str, Any]) -> str:
    global _LAST_ACTIVE_POOL
    global _LAST_ACTIVE_POOL_MODE
    global _LAST_ACTIVE_POOL_SIGNATURE
    global _LAST_ACTIVE_POOL_APPLIED_AT_MS
    normalized = _sanitize_scan_pool(pool)
    signature = _active_pool_signature(mode, normalized)
    _LAST_ACTIVE_POOL = normalized
    _LAST_ACTIVE_POOL_MODE = _mode_token(mode)
    _LAST_ACTIVE_POOL_SIGNATURE = signature
    _LAST_ACTIVE_POOL_APPLIED_AT_MS = int(time.time() * 1000)
    return signature


def get_last_runtime_scan_pool() -> dict[str, Any]:
    with _SYNC_LOCK:
        pool = _sanitize_scan_pool(_LAST_ACTIVE_POOL)
        snapshot_ready = bool(str(_LAST_ACTIVE_POOL_SIGNATURE or "").strip())
        entry_count = int(len(pool.get("trunked_sites") or []) + len(pool.get("conventional") or []))
        return {
            "ok": True,
            "mode": str(_LAST_ACTIVE_POOL_MODE or "expert"),
            "pool": pool,
            "signature": str(_LAST_ACTIVE_POOL_SIGNATURE or ""),
            "applied_at_ms": int(_LAST_ACTIVE_POOL_APPLIED_AT_MS or 0),
            "snapshot_ready": bool(snapshot_ready),
            "entry_count": entry_count,
        }


def _normalize_system_token(row: dict[str, Any]) -> str:
    system_id = str(row.get("system_id") or "").strip()
    site_id = str(row.get("site_id") or "").strip()
    if system_id and site_id:
        return f"{system_id}:{site_id}"
    if system_id:
        return system_id
    if site_id:
        return f"site:{site_id}"
    fallback = str(row.get("system_name") or row.get("site_name") or row.get("department_name") or "").strip()
    if fallback:
        return fallback
    return "system"


def _normalize_control_channel_mhz(raw: Any) -> str:
    try:
        value = float(str(raw).strip())
    except Exception:
        return ""
    if not (value > 0):
        return ""
    return f"{value:.5f}".rstrip("0").rstrip(".")


def _normalize_control_channel_hz(raw: Any) -> int:
    token = _normalize_control_channel_mhz(raw)
    if not token:
        return 0
    try:
        hz = int(round(float(token) * 1_000_000.0))
    except Exception:
        return 0
    return hz if hz > 0 else 0


def _normalize_site_id(row: dict[str, Any], controls_hz: list[int]) -> str:
    raw_site_id = str(row.get("site_id") or "").strip()
    if raw_site_id:
        return raw_site_id
    system_id = str(row.get("system_id") or "").strip()
    site_name = str(row.get("site_name") or row.get("department_name") or row.get("system_name") or "").strip().lower()
    signature = json.dumps(
        {
            "system_id": system_id,
            "site_name": site_name,
            "controls_hz": [int(value) for value in controls_hz],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha1(signature.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"fav:{digest}"


def _coerce_site_float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    return parsed if parsed == parsed else None


def _site_radius_sort_value(value: Any) -> float:
    parsed = _coerce_site_float(value)
    if parsed is None or parsed <= 0:
        return 0.0
    return float(parsed)


def _site_distance_sort_value(value: Any) -> float:
    parsed = _coerce_site_float(value)
    if parsed is None or parsed < 0:
        return float("inf")
    return float(parsed)


def _primary_site_distance_sort_value(system: dict[str, Any]) -> float:
    sites = system.get("sites") or []
    if not isinstance(sites, list) or not sites:
        return float("inf")
    primary = sites[0] if isinstance(sites[0], dict) else {}
    return _site_distance_sort_value(primary.get("distance_miles"))


def _normalize_digital_pool(
    pool: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str], dict[str, int]]:
    trunked = pool.get("trunked_sites")
    if not isinstance(trunked, list):
        trunked = []

    systems_by_key: dict[str, dict[str, Any]] = {}
    controls_flat: list[str] = []
    controls_seen: set[str] = set()
    talkgroups: list[dict[str, str]] = []
    tg_seen: set[str] = set()

    for item in trunked:
        row = item if isinstance(item, dict) else {}
        token = _normalize_system_token(row)
        key = token.lower()

        controls_raw = row.get("control_channels")
        controls: list[str] = []
        controls_hz: list[int] = []
        seen_controls: set[str] = set()
        for control in controls_raw if isinstance(controls_raw, list) else []:
            value = _normalize_control_channel_mhz(control)
            if not value or value in seen_controls:
                continue
            seen_controls.add(value)
            controls.append(value)
            hz = _normalize_control_channel_hz(value)
            if hz > 0:
                controls_hz.append(hz)
            if value not in controls_seen:
                controls_seen.add(value)
                controls_flat.append(value)
        if not controls:
            continue

        system_id = str(row.get("system_id") or "").strip()
        system_name = str(row.get("system_name") or "").strip() or token
        system_key = f"id:{system_id}" if system_id else key
        system_entry = systems_by_key.get(system_key)
        if system_entry is None:
            system_entry = {
                "name": system_name,
                "sites": [],
            }
            if system_id:
                system_entry["system_id"] = system_id
            systems_by_key[system_key] = system_entry

        site_id = _normalize_site_id(row, controls_hz)
        site_name = str(row.get("site_name") or row.get("department_name") or system_name or token).strip() or token
        site_entry = {
            "site_id": site_id,
            "site_name": site_name,
            "control_channels_hz": controls_hz,
            "enabled": True,
        }
        latitude = _coerce_site_float(row.get("latitude"))
        longitude = _coerce_site_float(row.get("longitude"))
        radius = _coerce_site_float(row.get("radius"))
        distance_miles = _coerce_site_float(row.get("distance_miles"))
        if latitude is not None:
            site_entry["latitude"] = latitude
        if longitude is not None:
            site_entry["longitude"] = longitude
        if radius is not None:
            site_entry["radius"] = radius
        if distance_miles is not None:
            site_entry["distance_miles"] = distance_miles

        existing_site = None
        for candidate in system_entry["sites"]:
            if str(candidate.get("site_id") or "").strip() == site_id:
                existing_site = candidate
                break
        if existing_site is None:
            system_entry["sites"].append(site_entry)
        else:
            merged = sorted(
                {
                    int(value)
                    for value in list(existing_site.get("control_channels_hz") or []) + controls_hz
                    if int(value) > 0
                }
            )
            existing_site["control_channels_hz"] = merged
            existing_site["enabled"] = True
            if latitude is not None and existing_site.get("latitude") is None:
                existing_site["latitude"] = latitude
            if longitude is not None and existing_site.get("longitude") is None:
                existing_site["longitude"] = longitude
            if radius is not None and existing_site.get("radius") is None:
                existing_site["radius"] = radius
            if distance_miles is not None and existing_site.get("distance_miles") is None:
                existing_site["distance_miles"] = distance_miles

        labels = row.get("talkgroup_labels") if isinstance(row.get("talkgroup_labels"), dict) else {}
        groups = row.get("talkgroup_groups") if isinstance(row.get("talkgroup_groups"), dict) else {}
        department = str(row.get("department_name") or row.get("site_name") or row.get("system_name") or "").strip()
        service_tag = str(row.get("service_tag") or "").strip()
        for tgid_raw in row.get("talkgroups") if isinstance(row.get("talkgroups"), list) else []:
            token_tg = str(tgid_raw or "").strip()
            if not token_tg.isdigit():
                continue
            try:
                tgid = int(token_tg)
            except Exception:
                continue
            if tgid <= 0 or tgid > 65535:
                continue
            dec = str(tgid)
            if dec in tg_seen:
                continue
            tg_seen.add(dec)
            alpha = str(labels.get(dec) or "").strip() or f"TG {dec}"
            group = str(groups.get(dec) or department or token).strip()
            tag = str(service_tag or "").strip()
            talkgroups.append(
                {
                    "dec": dec,
                    "mode": "D",
                    "alpha": alpha,
                    "description": alpha,
                    "tag": tag,
                    "listen": "1",
                    "group": group,
                }
            )

    talkgroups.sort(key=lambda row: int(row["dec"]))
    controls_flat.sort(key=lambda token: float(token))
    # Sort sites within each system first so sites[0] is the primary
    # (largest-radius, then closest). The allocator may drop the tail when
    # over-subscribed; using the primary site's distance as the cross-system
    # key keeps geographically appropriate systems and avoids the Davidson
    # Services-vs-Simulcast trap (small-radius services don't outrank big
    # simulcasts on the same system).
    for system in systems_by_key.values():
        system["sites"] = sorted(
            list(system.get("sites") or []),
            key=lambda row: (
                -_site_radius_sort_value(row.get("radius")),
                _site_distance_sort_value(row.get("distance_miles")),
                str(row.get("site_name") or "").lower(),
                str(row.get("site_id") or ""),
            ),
        )
    systems = sorted(
        systems_by_key.values(),
        key=lambda row: (
            _primary_site_distance_sort_value(row),
            str(row.get("name") or "").lower(),
        ),
    )
    summary = {
        "systems": len(systems),
        "talkgroups": len(talkgroups),
        "control_channels": len(controls_flat),
    }
    return systems, talkgroups, controls_flat, summary


def _render_talkgroups_text(rows: list[dict[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["DEC", "Mode", "Alpha Tag", "Description", "Tag", "Listen"])
    for row in rows:
        writer.writerow(
            [
                str(row.get("dec") or "").strip(),
                str(row.get("mode") or "D").strip() or "D",
                str(row.get("alpha") or "").strip(),
                str(row.get("description") or "").strip(),
                str(row.get("tag") or "").strip(),
                str(row.get("listen") or "1").strip() or "1",
            ]
        )
    return out.getvalue()


def _render_talkgroups_with_group_text(rows: list[dict[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Group", "DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
    for row in rows:
        dec = str(row.get("dec") or "").strip()
        try:
            hex_value = format(int(dec), "x")
        except Exception:
            hex_value = ""
        writer.writerow(
            [
                str(row.get("group") or "").strip(),
                dec,
                hex_value,
                str(row.get("mode") or "D").strip() or "D",
                str(row.get("alpha") or "").strip(),
                str(row.get("description") or "").strip(),
                str(row.get("tag") or "").strip(),
            ]
        )
    return out.getvalue()


def _ensure_managed_digital_profile() -> tuple[bool, str]:
    try:
        from .digital import create_digital_profile_dir
    except ImportError:
        from ui.digital import create_digital_profile_dir
    ok, err = create_digital_profile_dir(_MANAGED_DIGITAL_ID)
    if ok:
        return True, ""
    text = str(err or "").strip().lower()
    if text in {"profile already exists", "exists"}:
        return True, ""
    return False, str(err or "failed creating managed digital profile")


def sync_scan_pool_to_digital_runtime(
    force: bool = False,
    *,
    mode: str | None = None,
    pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply active scan-pool trunked systems/talkgroups to managed digital profile."""
    global _LAST_DIGITAL_SIGNATURE
    global _LAST_DIGITAL_RESULT

    with _SYNC_LOCK:
        if pool is None or mode is None:
            controller = get_scan_mode_controller()
            resolved_mode = _mode_token(mode if mode is not None else controller.get_mode())
            resolved_pool = (
                controller.get_scan_pool()
                if resolved_mode in {"hp", "expert"}
                else {"trunked_sites": [], "conventional": []}
            )
        else:
            resolved_mode = _mode_token(mode)
            resolved_pool = pool
        mode = resolved_mode
        pool = _sanitize_scan_pool(resolved_pool)
        active_pool_signature = _set_last_runtime_scan_pool_locked(mode, pool)
        active_pool_applied_at_ms = int(_LAST_ACTIVE_POOL_APPLIED_AT_MS or 0)
        active_pool_entry_count = int(len(pool.get("trunked_sites") or []) + len(pool.get("conventional") or []))
        systems, talkgroups, controls_flat, counts = _normalize_digital_pool(pool)

        signature_payload = {
            "mode": mode,
            "systems": systems,
            "talkgroups": [row.get("dec") for row in talkgroups],
            "controls": controls_flat,
        }
        signature = json.dumps(signature_payload, sort_keys=True, separators=(",", ":"))
        if not force and signature == _LAST_DIGITAL_SIGNATURE:
            return dict(_LAST_DIGITAL_RESULT)

        result: dict[str, Any] = {
            "ok": True,
            "changed": False,
            "mode": mode,
            "profile_id": _MANAGED_DIGITAL_ID,
            "system_count": int(counts.get("systems") or 0),
            "talkgroup_count": int(counts.get("talkgroups") or 0),
            "control_channel_count": int(counts.get("control_channels") or 0),
            "applied_profile": "",
            "profile_saved": False,
            "profile_save_changed": False,
            "profile_switch_changed": False,
            "compile_ok": True,
            "compile_error": "",
            "errors": [],
            "scan_pool_signature": active_pool_signature,
            "scan_pool_applied_at_ms": active_pool_applied_at_ms,
            "scan_pool_entry_count": active_pool_entry_count,
        }

        if not systems or not talkgroups or not controls_flat:
            result["ok"] = True
            result["reason"] = "no digital targets in active scan pool"
            _LAST_DIGITAL_SIGNATURE = signature
            _LAST_DIGITAL_RESULT = dict(result)
            return result

        # --- Dongle allocation: assign digital tuners to system roles ---
        # RSPduo tuners are passed as priority_serials so they are picked for
        # control-channel duty ahead of RTL-SDRs (better RF performance: 12-bit
        # vs 8-bit ADC). We expose as many RSPduo tuners as there are active
        # systems, preferring Tuner 1 across physical boxes before any Tuner 2.
        try:
            rspduo_ids = _rspduo_tuner_ids(max_tuners=len(systems))
            allocation = allocate_dongles(
                _digital_serials(),
                systems,
                priority_serials=rspduo_ids,
                persist=True,
            )
            logger.info(
                "Dongle allocation: strategy=%s assignments=%d traffic=%d "
                "rspduo_priority=%d rtl_pool=%d",
                allocation.get("strategy"),
                len(allocation.get("assignments") or []),
                len(allocation.get("traffic_pool") or []),
                len(rspduo_ids),
                len(_digital_serials()),
            )
        except Exception:
            logger.error("Dongle allocation failed; proceeding without assignment", exc_info=True)

        ok_profile, err_profile = _ensure_managed_digital_profile()
        if not ok_profile:
            result["ok"] = False
            result["errors"].append(err_profile)
            _LAST_DIGITAL_SIGNATURE = signature
            _LAST_DIGITAL_RESULT = dict(result)
            return result

        systems_json_text = json.dumps({"systems": systems}, indent=2)
        if systems_json_text and not systems_json_text.endswith("\n"):
            systems_json_text += "\n"
        talkgroups_text = _render_talkgroups_text(talkgroups)
        controls_text = "\n".join(controls_flat).strip() + "\n"

        try:
            from .profile_editor import save_digital_editor_payload
        except ImportError:
            from ui.profile_editor import save_digital_editor_payload

        ok_save, err_save, save_payload = save_digital_editor_payload(
            _MANAGED_DIGITAL_ID,
            controls_text,
            talkgroups_text,
            systems_json_text=systems_json_text,
        )
        if not ok_save:
            result["ok"] = False
            result["errors"].append(str(err_save or "failed saving managed digital profile"))
            _LAST_DIGITAL_SIGNATURE = signature
            _LAST_DIGITAL_RESULT = dict(result)
            return result
        result["profile_saved"] = True
        result["profile_save_changed"] = bool((save_payload or {}).get("changed"))

        # Keep group metadata for sidecar readability when editing profile files directly.
        try:
            from .config import DIGITAL_PROFILES_DIR
        except ImportError:
            from ui.config import DIGITAL_PROFILES_DIR
        group_path = os.path.join(str(DIGITAL_PROFILES_DIR), _MANAGED_DIGITAL_ID, "talkgroups_with_group.csv")
        try:
            rendered_group = _render_talkgroups_with_group_text(talkgroups)
            os.makedirs(os.path.dirname(group_path), exist_ok=True)
            with open(group_path + ".tmp", "w", encoding="utf-8") as handle:
                handle.write(rendered_group)
            os.replace(group_path + ".tmp", group_path)
        except Exception:
            logger.debug(
                "Failed writing grouped talkgroup sidecar for managed digital profile %s",
                _MANAGED_DIGITAL_ID,
                exc_info=True,
            )

        try:
            from .digital import get_digital_manager
        except ImportError:
            from ui.digital import get_digital_manager
        manager = get_digital_manager()
        current_profile = str(manager.getProfile() or "").strip()
        should_switch = current_profile != _MANAGED_DIGITAL_ID
        if should_switch or bool(result["profile_save_changed"]):
            # Phase 2a: classify the impending change so op25 can soft-reload
            # (HTTP terminal 'reload') for talkgroup-only edits and skip the
            # 12-15s SDR reacquisition.  Two-stage flow:
            #   1. setProfile(restart_service=False) writes sidecars and
            #      regenerates /run/.../multi_rx.json so we can diff it.
            #   2. classify_op25_profile_change(old, new) -> bucket.
            #   3. setProfile(change_class=bucket) dispatches the action.
            try:
                from .op25_adapter import (
                    load_current_op25_multi_rx,
                    classify_op25_profile_change,
                )
            except ImportError:
                from ui.op25_adapter import (
                    load_current_op25_multi_rx,
                    classify_op25_profile_change,
                )

            t0 = time.monotonic()
            from_profile = current_profile or "?"
            to_profile = _MANAGED_DIGITAL_ID
            old_multi_rx = load_current_op25_multi_rx()

            # Stage 1: write sidecars + regenerate multi_rx.json. No restart.
            sidecar_ok, sidecar_err = manager.setProfile(
                _MANAGED_DIGITAL_ID, restart_service=False,
            )
            if not sidecar_ok:
                duration_ms = int((time.monotonic() - t0) * 1000)
                print(
                    f"favorites_switch[bucket=UNKNOWN path=error "
                    f"duration_ms={duration_ms} outcome=err reason=sidecar_write "
                    f"from_profile={from_profile} to_profile={to_profile}]",
                    flush=True,
                )
                result["ok"] = False
                result["errors"].append(str(sidecar_err or "failed applying managed digital profile"))
            else:
                new_multi_rx = load_current_op25_multi_rx()
                bucket = classify_op25_profile_change(old_multi_rx, new_multi_rx)
                # If the JSON is byte-identical but sidecars (whitelist /
                # tag map) were rewritten, treat as TALKGROUPS_ONLY so op25
                # is told to re-read those files.  The sidecar-write
                # decision is encoded in profile_save_changed.
                if bucket == "NONE" and bool(result.get("profile_save_changed")):
                    bucket = "TALKGROUPS_ONLY"

                # Stage 2: dispatch.  setProfile records the path taken in
                # manager._last_setprofile_path so we can emit accurate
                # telemetry without timing heuristics.
                switched_ok, switched_err = manager.setProfile(
                    _MANAGED_DIGITAL_ID, restart_service=True, change_class=bucket,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)

                tpath = getattr(manager, "_last_setprofile_path", "") or "restart"
                reason = getattr(manager, "_last_setprofile_reason", "-") or "-"
                outcome = "fallback_restart" if tpath == "fallback_restart" else ("ok" if switched_ok else "err")

                print(
                    f"favorites_switch[bucket={bucket} path={tpath} "
                    f"duration_ms={duration_ms} outcome={outcome} reason={reason} "
                    f"from_profile={from_profile} to_profile={to_profile}]",
                    flush=True,
                )

                if not switched_ok:
                    result["ok"] = False
                    result["errors"].append(str(switched_err or "failed applying managed digital profile"))
                else:
                    result["profile_switch_changed"] = bool(should_switch)
                    result["changed"] = bool(should_switch or result["profile_save_changed"])
        result["applied_profile"] = str(manager.getProfile() or "").strip()

        try:
            from .v3_runtime import set_active_digital_profile
        except ImportError:
            from ui.v3_runtime import set_active_digital_profile
        try:
            set_active_digital_profile(_MANAGED_DIGITAL_ID)
        except Exception as exc:
            result["compile_ok"] = False
            result["compile_error"] = str(exc)
            if not result["errors"]:
                result["errors"].append(f"digital canonical update warning: {exc}")

        _LAST_DIGITAL_SIGNATURE = signature
        _LAST_DIGITAL_RESULT = dict(result)
        return result


def sync_scan_pool_to_runtime(force: bool = False) -> dict[str, Any]:
    """Apply active scan pool to both analog and digital runtimes."""
    controller = get_scan_mode_controller()
    mode = _mode_token(controller.get_mode())
    pool = controller.get_scan_pool() if mode in {"hp", "expert"} else {"trunked_sites": [], "conventional": []}
    pool = _sanitize_scan_pool(pool)
    analog = sync_scan_pool_to_analog_runtime(force=force, mode=mode, pool=pool)
    digital = sync_scan_pool_to_digital_runtime(force=force, mode=mode, pool=pool)
    pool_snapshot = get_last_runtime_scan_pool()
    payload = {
        "ok": bool(analog.get("ok", True)) and bool(digital.get("ok", True)),
        "changed": bool(analog.get("changed", False)) or bool(digital.get("changed", False)),
        "analog": analog,
        "digital": digital,
        "scan_pool_signature": str(pool_snapshot.get("signature") or ""),
        "scan_pool_applied_at_ms": int(pool_snapshot.get("applied_at_ms") or 0),
        "scan_pool_entry_count": int(pool_snapshot.get("entry_count") or 0),
    }
    # Preserve existing top-level keys for compatibility with older UI call-sites.
    payload.update(
        {
            "mode": str(analog.get("mode") or ""),
            "airband_frequency_count": int(analog.get("airband_frequency_count") or 0),
            "ground_frequency_count": int(analog.get("ground_frequency_count") or 0),
            "selected_profiles": dict(analog.get("selected_profiles") or {}),
            "profile_write_changed": dict(analog.get("profile_write_changed") or {}),
            "profile_controls_changed": dict(analog.get("profile_controls_changed") or {}),
            "profile_controls_source": dict(analog.get("profile_controls_source") or {}),
            "profile_switched": dict(analog.get("profile_switched") or {}),
            "combined_changed": bool(analog.get("combined_changed", False)),
            "restart_ok": bool(analog.get("restart_ok", True)),
            "restart_error": str(analog.get("restart_error") or ""),
            "errors": list(analog.get("errors") or []) + list(digital.get("errors") or []),
        }
    )
    return payload


def sync_scan_pool_to_analog_runtime(
    force: bool = False,
    *,
    mode: str | None = None,
    pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply active scan-pool conventional channels to managed analog profiles."""
    global _LAST_SIGNATURE
    global _LAST_RESULT

    with _SYNC_LOCK:
        if pool is None or mode is None:
            controller = get_scan_mode_controller()
            resolved_mode = _mode_token(mode if mode is not None else controller.get_mode())
            resolved_pool = (
                controller.get_scan_pool()
                if resolved_mode in {"hp", "expert"}
                else {"trunked_sites": [], "conventional": []}
            )
        else:
            resolved_mode = _mode_token(mode)
            resolved_pool = pool
        mode = resolved_mode
        pool = _sanitize_scan_pool(resolved_pool)
        active_pool_signature = _set_last_runtime_scan_pool_locked(mode, pool)
        active_pool_applied_at_ms = int(_LAST_ACTIVE_POOL_APPLIED_AT_MS or 0)
        active_pool_entry_count = int(len(pool.get("trunked_sites") or []) + len(pool.get("conventional") or []))
        air_freqs, air_labels, ground_freqs, ground_labels = _normalize_conventional_pool(pool)
        signature_payload = {
            "mode": mode,
            "air": air_freqs,
            "ground": ground_freqs,
        }
        signature = json.dumps(signature_payload, sort_keys=True, separators=(",", ":"))
        if not force and signature == _LAST_SIGNATURE:
            return dict(_LAST_RESULT)

        changed = False
        errors: list[str] = []
        switched = {"airband": False, "ground": False}
        profile_write_changed = {"airband": False, "ground": False}
        profile_controls_changed = {"airband": False, "ground": False}
        profile_controls_source = {"airband": "", "ground": ""}
        selected_profiles = {"airband": "", "ground": ""}

        profiles = load_profiles_registry()
        _, reg_changed_air = _ensure_managed_profile(
            profiles,
            profile_id=_MANAGED_AIR_ID,
            label=_MANAGED_AIR_LABEL,
            airband=True,
        )
        _, reg_changed_ground = _ensure_managed_profile(
            profiles,
            profile_id=_MANAGED_GROUND_ID,
            label=_MANAGED_GROUND_LABEL,
            airband=False,
        )
        if reg_changed_air or reg_changed_ground:
            save_profiles_registry(profiles)
            changed = True

        air_profile = find_profile(profiles, _MANAGED_AIR_ID) or {}
        ground_profile = find_profile(profiles, _MANAGED_GROUND_ID) or {}
        air_path = str(air_profile.get("path") or "").strip()
        ground_path = str(ground_profile.get("path") or "").strip()

        if air_path and air_freqs:
            try:
                profile_write_changed["airband"] = bool(write_freqs_labels(air_path, air_freqs, air_labels))
                changed = changed or profile_write_changed["airband"]
                control_result = apply_managed_profile_controls("airband", air_path)
                profile_controls_changed["airband"] = bool(control_result.get("changed"))
                profile_controls_source["airband"] = str(control_result.get("source") or "")
                changed = changed or profile_controls_changed["airband"]
            except Exception as exc:
                errors.append(f"failed writing airband favorites profile: {exc}")

        if ground_path and ground_freqs:
            try:
                profile_write_changed["ground"] = bool(write_freqs_labels(ground_path, ground_freqs, ground_labels))
                changed = changed or profile_write_changed["ground"]
                control_result = apply_managed_profile_controls("ground", ground_path)
                profile_controls_changed["ground"] = bool(control_result.get("changed"))
                profile_controls_source["ground"] = str(control_result.get("source") or "")
                changed = changed or profile_controls_changed["ground"]
            except Exception as exc:
                errors.append(f"failed writing ground favorites profile: {exc}")

        desired_air_profile = (
            _MANAGED_AIR_ID
            if air_freqs
            else _desired_analog_profile_for_empty_result(
                profiles,
                "airband",
                managed_profile_id=_MANAGED_AIR_ID,
            )
        )
        desired_ground_profile = (
            _MANAGED_GROUND_ID
            if ground_freqs
            else _desired_analog_profile_for_empty_result(
                profiles,
                "ground",
                managed_profile_id=_MANAGED_GROUND_ID,
            )
        )
        selected_profiles["airband"] = desired_air_profile
        selected_profiles["ground"] = desired_ground_profile

        switched_air, err_air = _switch_profile_if_needed("airband", desired_air_profile)
        if err_air:
            errors.append(err_air)
        switched["airband"] = switched_air
        changed = changed or switched_air

        switched_ground, err_ground = _switch_profile_if_needed("ground", desired_ground_profile)
        if err_ground:
            errors.append(err_ground)
        switched["ground"] = switched_ground
        changed = changed or switched_ground

        restart_ok = True
        restart_error = ""
        combined_changed = False
        if changed:
            try:
                combined_changed = bool(write_combined_config())
            except Exception as exc:
                errors.append(f"failed updating combined config: {exc}")
            if combined_changed or switched_air or switched_ground:
                restart_ok, restart_error = restart_rtl()
                if not restart_ok and restart_error:
                    errors.append(f"rtl restart failed: {restart_error}")

        result = {
            "ok": len(errors) == 0,
            "changed": bool(changed),
            "mode": mode,
            "airband_frequency_count": len(air_freqs),
            "ground_frequency_count": len(ground_freqs),
            "selected_profiles": selected_profiles,
            "profile_write_changed": profile_write_changed,
            "profile_controls_changed": profile_controls_changed,
            "profile_controls_source": profile_controls_source,
            "profile_switched": switched,
            "combined_changed": bool(combined_changed),
            "restart_ok": bool(restart_ok),
            "restart_error": str(restart_error or ""),
            "errors": errors,
            "scan_pool_signature": active_pool_signature,
            "scan_pool_applied_at_ms": active_pool_applied_at_ms,
            "scan_pool_entry_count": active_pool_entry_count,
        }
        _LAST_SIGNATURE = signature
        _LAST_RESULT = dict(result)
        return result


def get_last_favorites_runtime_sync() -> dict[str, Any]:
    if not _SYNC_LOCK.acquire(blocking=False):
        analog = dict(_LAST_RESULT)
        digital = dict(_LAST_DIGITAL_RESULT)
        payload = {
            "ok": bool(analog.get("ok", True)) and bool(digital.get("ok", True)),
            "changed": bool(analog.get("changed", False)) or bool(digital.get("changed", False)),
            "analog": analog,
            "digital": digital,
            "sync_in_progress": True,
        }
        payload.update(analog)
        return payload

    try:
        analog = dict(_LAST_RESULT)
        digital = dict(_LAST_DIGITAL_RESULT)
        payload = {
            "ok": bool(analog.get("ok", True)) and bool(digital.get("ok", True)),
            "changed": bool(analog.get("changed", False)) or bool(digital.get("changed", False)),
            "analog": analog,
            "digital": digital,
            "sync_in_progress": False,
        }
        payload.update(analog)
        return payload
    finally:
        _SYNC_LOCK.release()
