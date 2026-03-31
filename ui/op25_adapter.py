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
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

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
    from .systemd import unit_active
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
    from ui.systemd import unit_active  # type: ignore[no-redef]

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
#   control channel: 851012500  status: locked
_RE_TSBK = re.compile(
    r"tsbk.*?tg\s*\(?\s*(\d+)\s*\)?\s*,?\s*freq\s*\(?\s*(\d+)\s*\)?",
    re.IGNORECASE,
)
_RE_VOICE = re.compile(
    r"voice\s+(?:update|grant).*?tg\s*\(?\s*(\d+)\s*\)?\s*,?\s*freq\s*\(?\s*(\d+)\s*\)?",
    re.IGNORECASE,
)
_RE_CC_STATUS = re.compile(
    r"control\s+channel.*?(\d{9,10}).*?status:\s*(\w+)",
    re.IGNORECASE,
)
_RE_ROOT_TSBKS = re.compile(r"\btsbks\s+(\d+)\b", re.IGNORECASE)

_OP25_ROOT_ACTIVITY_MAX_AGE_SEC = 15.0

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


# ---------------------------------------------------------------------------
# Config generation helpers
# ---------------------------------------------------------------------------

def _read_system_definitions(profile_dir: str) -> list[dict]:
    """Read ``systems.json`` from *profile_dir*.

    Returns a list of dicts with keys ``name`` and ``control_channels_hz``.
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
    op25_overrides: dict | None = None,
    tgid_tags_path: str = "",
    http_port: int = 8080,
    udp_audio_base_port: int = _UDP_AUDIO_BASE_PORT,
    sample_rate: int = OP25_DEFAULT_SAMPLE_RATE,
    offset: int = OP25_DEFAULT_OFFSET,
) -> dict:
    """Generate a multi_rx.py JSON config for all systems + optional traffic follower."""
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
        devices.append({
            "name": dev_name,
            "args": str(arg_map.get(serial) or f"rtl={serial}"),
            "rate": sample_rate,
            "frequency": center_hz,
            "offset": offset,
            "ppm": 0.0,
            "gains": "LNA:36",
            "gain_mode": True,
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
                devices.append({
                    "name": "sdr_traffic",
                    "args": str(arg_map.get(traffic_dongle_serial) or f"rtl={traffic_dongle_serial}"),
                    "rate": sample_rate,
                    "frequency": target_cc_hz,
                    "offset": offset,
                    "ppm": 0.0,
                    "gains": "LNA:36",
                    "gain_mode": True,
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
        # Log parsing state
        self._log_offset = 0
        self._log_inode = 0
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
        ok, err = self._systemctl(["restart"])
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
        tg_labels = _read_talkgroup_labels(profile_dir)
        tags_path = os.path.join(runtime, "tgid_tags.tsv")

        trunk_content = generate_trunk_tsv(
            systems,
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

        self._active_systems = list(systems)
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
        freq_hz = int(m.group(2))
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

    def _request_json(self, path: str, *, method: str = "GET", payload=None):
        route = str(path or "/").strip()
        if not route.startswith("/"):
            route = f"/{route}"
        url = f"http://{self._status_host}:{self._status_port}{route}"
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

    def _fetch_json(self, path: str) -> dict:
        data = self._request_json(path, method="GET")
        return data if isinstance(data, dict) else {}

    def _fetch_update_json(self) -> dict:
        payload = [{"command": "update", "arg1": 0, "arg2": 0}]
        data = self._request_json("/", method="POST", payload=payload)
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
        """Poll OP25's HTTP status endpoint. Returns parsed JSON or {}."""
        now = time.monotonic()
        if (now - self._last_status_time) < self._status_cache_ttl and self._last_status:
            return dict(self._last_status)
        data = self._fetch_json("/status")
        if self._status_needs_root_fallback(data):
            root_data = self._fetch_json("/")
            if root_data:
                data = root_data
        if self._status_needs_root_fallback(data):
            update_data = self._fetch_update_json()
            if update_data:
                data = update_data
        if data:
            self._last_status = data
            self._last_status_time = now
            return data
        return self._last_status or {}

    def preflight(self) -> dict:
        """Return health payload compatible with the scheduler's expectations."""
        status = self._poll_op25_status()
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
