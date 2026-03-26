"""OP25 digital backend adapter.

OP25 is a Python/gnuradio P25 decoder that natively supports multiple
simultaneous trunked systems — one receiver per RTL-SDR dongle.  This
adapter generates OP25 config files from the same ``systems.json`` and
``control_channels.txt`` profile data that SDRTrunk uses, manages the
OP25 systemd service, parses OP25 log output for call events, and polls
the OP25 HTTP status endpoint for health.
"""
from __future__ import annotations

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
        OP25_LOG_PATH,
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
        OP25_LOG_PATH,
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
#   tsbk_handler(): cc 851012500 tg 12345 freq 855462500
#   voice update:   tg 54321 freq 856737500
#   control channel: 851012500  status: locked
_RE_TSBK = re.compile(
    r"tsbk.*?tg\s+(\d+)\s+freq\s+(\d+)",
    re.IGNORECASE,
)
_RE_VOICE = re.compile(
    r"voice\s+(?:update|grant).*?tg\s+(\d+)\s+freq\s+(\d+)",
    re.IGNORECASE,
)
_RE_CC_STATUS = re.compile(
    r"control\s+channel.*?(\d{9,10}).*?status:\s*(\w+)",
    re.IGNORECASE,
)

# Timestamp at the start of OP25 log lines:  2026-03-26 14:05:32.123
_RE_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")


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
    """
    labels: dict[str, str] = {}
    for name in ("talkgroups.csv", "talkgroups.tsv"):
        path = os.path.join(profile_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = re.split(r"[,\t]", line, maxsplit=2)
                    if len(parts) >= 2:
                        tgid = parts[0].strip()
                        label = parts[1].strip()
                        if tgid.isdigit() and label:
                            labels[tgid] = label
        except Exception:
            pass
    return labels


def generate_trunk_tsv(
    systems: list[dict],
    dongle_assignments: dict | None = None,
    op25_overrides: dict | None = None,
) -> str:
    """Generate OP25 trunk.tsv content from system definitions.

    Each system gets one row.  Columns:
    ``Sysname<TAB>Control Channel<TAB>Offset<TAB>NAC<TAB>Modulation<TAB>TGID Tags File<TAB>Whitelist<TAB>Blacklist<TAB>Center Frequency<TAB>Device``

    OP25 accepts the control channel in Hz.
    """
    overrides = op25_overrides or {}
    assignments = dongle_assignments or {}
    assignment_list = assignments.get("assignments") or []
    serial_map: dict[str, str] = {}
    for entry in assignment_list:
        sname = str(entry.get("system_name") or "").strip()
        serial = str(entry.get("preferred_tuner_serial") or "").strip()
        if sname and serial:
            serial_map[sname] = serial

    lines: list[str] = []
    for sys in systems:
        name = sys["name"]
        channels = sys["control_channels_hz"]
        if not channels:
            continue
        cc_hz = channels[0]  # OP25 uses the first control channel
        sys_overrides = overrides.get(name) or {}
        nac = str(sys_overrides.get("nac", "0")).strip()
        modulation = str(
            sys_overrides.get("modulation", OP25_DEFAULT_MODULATION)
        ).strip().lower()
        offset = str(sys_overrides.get("offset", "0")).strip()
        tgid_file = str(sys_overrides.get("tgid_tags_file", "")).strip()
        whitelist = str(sys_overrides.get("whitelist", "")).strip()
        blacklist = str(sys_overrides.get("blacklist", "")).strip()
        center_freq = str(sys_overrides.get("center_frequency", "")).strip()
        device = serial_map.get(name, "")
        if device:
            device = f"rtl:{device}"
        row = "\t".join([
            name,
            str(cc_hz),
            offset,
            nac,
            modulation,
            tgid_file,
            whitelist,
            blacklist,
            center_freq,
            device,
        ])
        lines.append(row)
    return "\n".join(lines) + "\n" if lines else ""


def generate_tgid_tags_tsv(labels: dict[str, str]) -> str:
    """Generate OP25 tgid_tags.tsv from talkgroup labels.

    Format: ``TGID<TAB>Tag``
    """
    lines: list[str] = []
    for tgid, label in sorted(labels.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        lines.append(f"{tgid}\t{label}")
    return "\n".join(lines) + "\n" if lines else ""


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

        trunk_content = generate_trunk_tsv(
            systems,
            dongle_assignments=dongle_assignments,
            op25_overrides=op25_overrides,
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

        if tags_content:
            try:
                tags_path = os.path.join(runtime, "tgid_tags.tsv")
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

        label = self._resolve_tg_label(tgid)
        return {
            "type": "digital",
            "tgid": tgid,
            "label": label or f"TG {tgid}",
            "mode": "P25",
            "frequency_hz": freq_hz,
            "frequency_mhz": _hz_to_mhz(freq_hz),
            "timeMs": timestamp_ms or int(time.time() * 1000),
        }

    def _extract_timestamp_ms(self, line: str) -> int:
        m = _RE_TIMESTAMP.search(line)
        if not m:
            return 0
        try:
            from datetime import datetime
            dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    def _resolve_tg_label(self, tgid: str) -> str:
        """Lookup talkgroup label from profile data."""
        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            return ""
        profile_id = os.path.basename(profile_dir)
        if profile_id != self._tg_labels_profile:
            self._tg_labels = _read_talkgroup_labels(profile_dir)
            self._tg_labels_profile = profile_id
        return self._tg_labels.get(tgid, "")

    def getLastEvent(self):
        self._refresh_log_cache()
        return super().getLastEvent()

    def getRecentEvents(self, limit: int = 20):
        self._refresh_log_cache()
        return super().getRecentEvents(limit)

    # ------------------------------------------------------------------
    # Health / preflight
    # ------------------------------------------------------------------

    def _poll_op25_status(self) -> dict:
        """Poll OP25's HTTP status endpoint. Returns parsed JSON or {}."""
        now = time.monotonic()
        if (now - self._last_status_time) < self._status_cache_ttl and self._last_status:
            return dict(self._last_status)
        url = f"http://{self._status_host}:{self._status_port}/status"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                self._last_status = data
                self._last_status_time = now
                return data
        except Exception:
            return self._last_status or {}

    def preflight(self) -> dict:
        """Return health payload compatible with the scheduler's expectations."""
        status = self._poll_op25_status()
        # OP25 status format varies; adapt to common fields.
        locked = bool(status.get("locked") or status.get("control_channel_locked"))
        ber = float(status.get("ber", 0) or 0)
        decode_rate = float(status.get("decode_rate", 0) or 0)

        return {
            "control_channel_locked": locked,
            "control_decode_available": locked,
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
