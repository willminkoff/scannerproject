#!/usr/bin/env python3
"""Pre-flight bootstrap for the OP25 digital backend.

Generates runtime config for one or more OP25 ``multi_rx.py`` processes.
The common case stays single-process. When both RSPduo tuners are assigned
to active control duties and ``OP25_RSPDUO_SPLIT_PROCESSES`` is enabled,
the launcher splits OP25 into two child processes so Tuner 1 (Master) and
Tuner 2 (Slave) never open in the same Python process.

Output layout:

  /run/scannerproject/op25/tgid_tags.tsv     — shared talkgroup labels
  /run/scannerproject/op25/whitelist.tsv      — favorited TGID whitelist
  /run/scannerproject/op25/trunk.tsv          — OP25 trunking config
  /run/scannerproject/op25/multi_rx.json      — single config or split-runtime summary
  /run/scannerproject/op25/instance-*/multi_rx.json — per-process configs when split
  /run/scannerproject/op25/start.sh           — launch script for one or more multi_rx.py children
  /run/scannerproject/op25/instances.json     — manifest (audio bridge + adapter)

Systemd unit scanner-digital-op25.service runs ``start.sh`` as a supervisor
process; that script launches one or more ``multi_rx.py`` children.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys

# Add project root to path so we can import ui modules.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from ui.config import (
    DIGITAL_ACTIVE_PROFILE_LINK,
    OP25_DEFAULT_OFFSET,
    OP25_DEFAULT_SAMPLE_RATE,
    OP25_MULTI_RX_PATH,
    OP25_RUNTIME_DIR,
    OP25_STATUS_PORT,
)
from ui.dongle_allocator import load_assignments
from ui.op25_adapter import (
    _multi_rx_udp_ports,
    _flatten_active_runtime_systems,
    _hydrate_runtime_systems_for_config,
    _read_op25_system_config,
    _read_system_definitions,
    _read_talkgroup_labels,
    generate_multi_rx_config,
    generate_tgid_tags_tsv,
    generate_trunk_tsv,
)

# Base UDP port for audio output.
_UDP_AUDIO_BASE_PORT = 23456

_RTL_TEST_DEVICE_RE = re.compile(r"^\s*(\d+):\s+.*SN:\s+(\S+)\s*$")

# Canonical RSPduo tuner identifier: "RSPduo Tuner 1 SER#180903EF32".
# Produced by ui.favorites_runtime._rspduo_tuner_ids() (which discovers the
# device via SoapySDR enumeration) and consumed here to build the gr-osmosdr
# "soapy=" device-args string.  Captures the physical tuner number (1 or 2)
# and the SDRplay device serial.
_RSPDUO_ID_RE = re.compile(
    r"^RSPduo\s+Tuner\s+(\d+)\s+SER#([0-9A-Fa-f]+)\s*$"
)

# RSPduo modes recognised by SoapySDRPlay3 / sdrplay-api 3.x:
#   ST = Single Tuner (one physical tuner active, up to ~10 MHz sample rate)
#   MA = Master in Dual Tuner mode (must be opened before any Slave)
#   SL = Slave in Dual Tuner mode (depends on a previously-opened Master
#        on the same physical device; same sample rate as Master)
_RSPDUO_VALID_MODES = ("ST", "MA", "SL")

# In Dual Tuner Master/Slave mode the SoapySDRPlay3 driver constrains the
# sample rate to a small set: 0.0625, 0.125, 0.25, 0.5, 1, 2 MSps.  OP25's
# default RTL rate (2.4 MSps) is invalid in DT mode and the driver silently
# snaps to the previous rate, producing junk samples.  Override per-device.
RSPDUO_DT_SAMPLE_RATE = 2_000_000


def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _rspduo_split_processes_enabled() -> bool:
    return _env_flag("OP25_RSPDUO_SPLIT_PROCESSES", "1")


def _rspduo_slave_start_delay_sec() -> float:
    try:
        return max(0.0, float(os.getenv("OP25_RSPDUO_SLAVE_START_DELAY_SEC", "2.0")))
    except Exception:
        return 2.0


def _rspduo_uid_parts(unique_id: str) -> tuple[int, str] | None:
    match = _RSPDUO_ID_RE.match(str(unique_id or "").strip())
    if not match:
        return None
    return int(match.group(1)), match.group(2).upper()


def _rspduo_osmosdr_args(unique_id: str, mode: str = "ST") -> str | None:
    """Translate an RSPduo identifier to gr-osmosdr soapy device-args.

    Pure formatter — does NOT decide which mode to use; that's the caller's
    job (see ``_build_dongle_arg_map`` for ST-vs-MA/SL selection based on
    pool composition).  Returns ``None`` if ``unique_id`` does not match the
    RSPduo pattern, leaving the caller to fall back to its RTL-SDR handling.

    ``mode`` must be one of ``ST`` / ``MA`` / ``SL`` — invalid values raise
    ``ValueError`` to surface configuration mistakes early rather than at
    osmosdr-open time.
    """
    if mode not in _RSPDUO_VALID_MODES:
        raise ValueError(
            f"Invalid RSPduo mode {mode!r}; expected one of {_RSPDUO_VALID_MODES}"
        )
    m = _RSPDUO_ID_RE.match(str(unique_id or ""))
    if not m:
        return None
    tuner = int(m.group(1))
    serial = m.group(2).upper()
    return (
        f"soapy=,driver=sdrplay,serial={serial},mode={mode},tuner={tuner}"
    )


def _select_rspduo_modes(serials: list[str]) -> dict[str, str]:
    """Choose ST / MA / SL per RSPduo identifier based on pool composition.

    For each physical RSPduo (grouped by SDRplay serial):

    * Both Tuner 1 and Tuner 2 in the pool -> Tuner 1 = MA (Master),
      Tuner 2 = SL (Slave).  Both tuners run independently in dual-tuner
      mode.  multi_rx.py must open the Master device entry before the
      Slave; this happens naturally because the dongle_allocator assigns
      Tuner 1 to the first system (lexicographic priority) and the device
      list in multi_rx.json follows system-sort order.
    * Only Tuner 1 -> ST.  Single Tuner mode, full bandwidth available.
    * Only Tuner 2 -> ST.  Single Tuner mode using the second physical
      tuner; fine standalone, but conflicts with any other active source
      on the same RSPduo.

    Identifiers that don't match the RSPduo pattern are absent from the
    return value — the caller falls back to its RTL handling for those.
    """
    by_device: dict[str, list[tuple[int, str]]] = {}
    for serial in serials:
        m = _RSPDUO_ID_RE.match(str(serial or ""))
        if not m:
            continue
        device_serial = m.group(2).upper()
        tuner_n = int(m.group(1))
        by_device.setdefault(device_serial, []).append((tuner_n, serial))

    modes: dict[str, str] = {}
    for device_serial, tuners in by_device.items():
        tuner_nums = {t[0] for t in tuners}
        dual = (1 in tuner_nums and 2 in tuner_nums)
        for tuner_n, uid in tuners:
            if dual:
                modes[uid] = "MA" if tuner_n == 1 else "SL"
            else:
                modes[uid] = "ST"
    return modes


def _safe_realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def _resolve_active_profile() -> str:
    link = DIGITAL_ACTIVE_PROFILE_LINK
    if not link or not os.path.islink(link):
        return ""
    target = _safe_realpath(link)
    if target and os.path.isdir(target):
        return target
    return ""


def _build_dongle_map(dongle_assignments: dict | None) -> dict[str, str]:
    """Map system_name -> dongle serial from allocator assignments."""
    if not dongle_assignments:
        return {}
    serial_map: dict[str, str] = {}
    for entry in dongle_assignments.get("assignments") or []:
        sname = str(entry.get("system_name") or "").strip()
        serial = str(entry.get("preferred_tuner_serial") or "").strip()
        if sname and serial:
            serial_map[sname] = serial
    return serial_map


def _generate_multi_rx_start_script(process_specs: list[dict[str, object]]) -> str:
    """Generate a launcher for one or more ``multi_rx.py`` child processes."""
    apps_dir = os.path.dirname(OP25_MULTI_RX_PATH)
    tdma_dir = os.path.join(apps_dir, "tdma")
    specs = [dict(spec) for spec in process_specs if isinstance(spec, dict)]
    if not specs:
        return ""

    if len(specs) == 1:
        config_path = str(specs[0].get("config_path") or "").strip()
        lines = [
            "#!/bin/bash",
            "# Auto-generated by ensure-op25-runtime.py — multi_rx.py mode",
            f'export PYTHONPATH="{apps_dir}:{tdma_dir}:${{PYTHONPATH:-}}"',
            f'cd "{apps_dir}"',
            f'exec python3 "{OP25_MULTI_RX_PATH}" \\',
            f'  -c "{config_path}" \\',
            f'  -v 1 \\',
            f'  2>&1',
        ]
        return "\n".join(lines) + "\n"

    lines = [
        "#!/bin/bash",
        "# Auto-generated by ensure-op25-runtime.py — split multi_rx.py supervisor",
        "set -u",
        f'export PYTHONPATH="{apps_dir}:{tdma_dir}:${{PYTHONPATH:-}}"',
        f'cd "{apps_dir}"',
        "pids=()",
        'shutdown_children() {',
        '  local pid',
        '  for pid in "${pids[@]}"; do',
        '    kill -INT "$pid" 2>/dev/null || true',
        '  done',
        '  wait || true',
        '}',
        "trap shutdown_children INT TERM",
    ]
    for idx, spec in enumerate(specs):
        delay = float(spec.get("delay_before_start_sec") or 0.0)
        config_path = str(spec.get("config_path") or "").strip()
        label = str(spec.get("label") or f"proc{idx}").strip() or f"proc{idx}"
        if idx > 0 and delay > 0:
            lines.append(f"sleep {delay:.3f}")
        lines.extend(
            [
                f'echo "OP25 runtime: starting {label} with {config_path}"',
                f'python3 "{OP25_MULTI_RX_PATH}" -c "{config_path}" -v 1 2>&1 &',
                "pids+=($!)",
            ]
        )
    lines.extend(
        [
            "status=0",
            'wait -n "${pids[@]}" || status=$?',
            "trap - INT TERM",
            "shutdown_children",
            "exit $status",
        ]
    )
    return "\n".join(lines) + "\n"


def _summarize_processes(processes: list[dict[str, object]]) -> dict[str, object]:
    return {
        "split_processes": len(processes) > 1,
        "process_count": len(processes),
        "processes": [
            {
                "index": int(proc.get("index") or 0),
                "label": str(proc.get("label") or ""),
                "config_path": str(proc.get("config_path") or ""),
                "http_status_port": int(proc.get("http_status_port") or 0),
                "system_names": list(proc.get("system_names") or []),
                "device_args": list(proc.get("device_args") or []),
            }
            for proc in processes
        ],
    }


def _split_rspduo_anchor_systems(
    systems: list[dict[str, object]],
    dongle_map: dict[str, str],
) -> tuple[str, str] | None:
    """Return the system names anchored on Tuner 1 and Tuner 2, if both exist."""
    by_device: dict[str, dict[int, str]] = {}
    for system in systems:
        system_name = str(system.get("name") or "").strip()
        serial = str(dongle_map.get(system_name) or "").strip()
        parts = _rspduo_uid_parts(serial)
        if not parts:
            continue
        tuner_n, device_serial = parts
        by_device.setdefault(device_serial, {})[tuner_n] = system_name

    for device_serial in sorted(by_device):
        tuners = by_device[device_serial]
        if 1 in tuners and 2 in tuners:
            return tuners[1], tuners[2]
    return None


def _build_runtime_process_plans(
    systems: list[dict[str, object]],
    dongle_map: dict[str, str],
    *,
    traffic_followers: list[tuple[str, str]],
) -> list[dict[str, object]]:
    """Partition systems into one or more runtime process plans."""
    ordered_systems = [
        dict(system)
        for system in systems
        if str(system.get("name") or "").strip() and str(dongle_map.get(str(system.get("name") or "").strip()) or "").strip()
    ]
    if not ordered_systems:
        return []

    split_pair = _split_rspduo_anchor_systems(ordered_systems, dongle_map)
    if not (_rspduo_split_processes_enabled() and split_pair):
        return [{
            "label": "op25-main",
            "systems": ordered_systems,
            "traffic_followers": list(traffic_followers),
        }]

    system_a, system_b = split_pair
    plan_a: dict[str, object] = {"label": "op25-main", "systems": [], "traffic_followers": []}
    plan_b: dict[str, object] = {"label": "op25-aux", "systems": [], "traffic_followers": []}
    plan_for_system: dict[str, dict[str, object]] = {}

    for system in ordered_systems:
        name = str(system.get("name") or "").strip()
        if name == system_a:
            cast = list(plan_a["systems"])  # type: ignore[arg-type]
            cast.append(system)
            plan_a["systems"] = cast
            plan_for_system[name] = plan_a
        elif name == system_b:
            cast = list(plan_b["systems"])  # type: ignore[arg-type]
            cast.append(system)
            plan_b["systems"] = cast
            plan_for_system[name] = plan_b

    for system in ordered_systems:
        name = str(system.get("name") or "").strip()
        if name in plan_for_system:
            continue
        target = plan_a if len(list(plan_a["systems"])) <= len(list(plan_b["systems"])) else plan_b
        cast = list(target["systems"])  # type: ignore[arg-type]
        cast.append(system)
        target["systems"] = cast
        plan_for_system[name] = target

    used_followers: set[str] = set()
    for serial, target_system in traffic_followers:
        serial = str(serial or "").strip()
        target_system = str(target_system or "").strip()
        if not serial or serial in used_followers:
            continue
        if _rspduo_uid_parts(serial):
            print(
                f"OP25 runtime: skipping RSPduo follower {serial} in split mode "
                "(traffic follower must stay in the same process as its control channel)"
            )
            continue
        target_plan = plan_for_system.get(target_system) or plan_a
        followers = list(target_plan["traffic_followers"])  # type: ignore[arg-type]
        followers.append((serial, target_system))
        target_plan["traffic_followers"] = followers
        used_followers.add(serial)

    return [plan_a, plan_b]


def _detect_traffic_dongle(dongle_assignments: dict | None) -> str:
    """Find a traffic-pool dongle for use as traffic follower."""
    if not dongle_assignments:
        return ""
    pool = dongle_assignments.get("traffic_pool") or []
    if pool:
        return str(pool[0]).strip()
    return ""


def _parse_rtl_test_device_map(output: str) -> dict[str, int]:
    """Parse ``rtl_test`` output into ``{serial: index}``."""
    mapping: dict[str, int] = {}
    for line in str(output or "").splitlines():
        match = _RTL_TEST_DEVICE_RE.match(line.strip())
        if not match:
            continue
        index = int(match.group(1))
        serial = str(match.group(2) or "").strip()
        if serial:
            mapping[serial] = index
    return mapping


def _enumerate_rtlsdr_serial_index_map() -> dict[str, int]:
    """Enumerate current RTL-SDR serials to librtlsdr indices.

    Uses ``rtl_test -d 9999 -t`` so librtlsdr prints the device list but does
    not open any matching tuner.
    """
    cmd = ["rtl_test", "-d", "9999", "-t"]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return {}
    return _parse_rtl_test_device_map(result.stdout or "")


def _build_dongle_arg_map(serials: list[str]) -> dict[str, str]:
    """Resolve OP25 dongle args for the requested serials.

    Three identifier shapes are recognised, each producing a distinct
    gr-osmosdr device-args string consumable by ``osmosdr.source()``:

    * **RSPduo identifier** (``"RSPduo Tuner 1 SER#180903EF32"``) —
      produced by ``ui.favorites_runtime._rspduo_tuner_ids()``. Translated
      to ``soapy=,driver=sdrplay,serial=...,rspduo_mode=...,tuner=...``.
      Mode (ST / MA / SL) is chosen by ``_select_rspduo_modes()`` based on
      whether both Tuner 1 and Tuner 2 of the same physical RSPduo are
      present in the pool — both means dual-tuner Master/Slave; one means
      Single Tuner.
    * **RTL-SDR EEPROM serial mappable to a librtlsdr index** — preferred
      because ``rtl=<index>`` opens faster and avoids serial-collision
      pitfalls with factory-default serials. The mapping comes from
      ``rtl_test -d 9999 -t``.
    * **RTL-SDR EEPROM serial without an index mapping** — fall back to
      ``rtl=<serial>``; resilient on hosts without ``rtl_test`` but
      requires librtlsdr to do the serial lookup at open time.
    """
    mapping = _enumerate_rtlsdr_serial_index_map()
    rspduo_modes = _select_rspduo_modes(serials)
    args_map: dict[str, str] = {}
    seen: set[str] = set()
    for raw_serial in serials:
        serial = str(raw_serial or "").strip()
        if not serial or serial in seen:
            continue
        seen.add(serial)
        if serial in rspduo_modes:
            args_map[serial] = _rspduo_osmosdr_args(serial, mode=rspduo_modes[serial])
        elif serial in mapping:
            args_map[serial] = f"rtl={mapping[serial]}"
        else:
            args_map[serial] = f"rtl={serial}"
    return args_map


def _device_sample_rate(args: str, default_rate: int) -> int:
    """Choose a per-device sample rate.

    RSPduo Master/Slave dual-tuner mode caps at 2 MSps; the SoapySDRPlay3
    driver silently snaps invalid rates (yielding garbage samples) rather
    than erroring.  Override to ``RSPDUO_DT_SAMPLE_RATE`` for any device
    whose args put the RSPduo in MA or SL mode.  RSPduo Single Tuner mode
    (ST) and all other devices use ``default_rate`` unchanged.
    """
    if "mode=MA" in args or "mode=SL" in args:
        return RSPDUO_DT_SAMPLE_RATE
    return default_rate


def main() -> int:
    profile_dir = _resolve_active_profile()
    if not profile_dir:
        print("ERROR: no active digital profile", file=sys.stderr)
        return 1

    profile_id = os.path.basename(profile_dir)
    print(f"OP25 runtime: profile={profile_id} dir={profile_dir}")

    op25_overrides = _read_op25_system_config(profile_dir)
    runtime_systems, _selector_state = _hydrate_runtime_systems_for_config(
        profile_dir,
        OP25_RUNTIME_DIR,
        op25_overrides=op25_overrides,
    )
    systems = _flatten_active_runtime_systems(runtime_systems)
    if not systems:
        print("ERROR: no systems defined in profile", file=sys.stderr)
        return 1

    # Re-order systems so the priority system (traffic_priority) is first.
    # Works around multi_rx issues where channel 0 gets preferential decode.
    def _sys_sort_key(sys_def: dict) -> tuple:
        sname = sys_def.get("name") or ""
        sys_over = op25_overrides.get(sname) or {}
        priority = 0 if sys_over.get("traffic_priority") else 1
        return (priority, sname.lower())

    systems.sort(key=_sys_sort_key)
    print(f"OP25 runtime: {len(systems)} system(s) found")

    # Read dongle assignments.
    dongle_assignments = None
    try:
        dongle_assignments = load_assignments()
    except Exception as e:
        print(f"WARNING: could not load dongle assignments: {e}", file=sys.stderr)

    dongle_map = _build_dongle_map(dongle_assignments)
    tg_labels = _read_talkgroup_labels(profile_dir)

    runtime_dir = OP25_RUNTIME_DIR
    os.makedirs(runtime_dir, exist_ok=True)

    # Symlink OP25 www assets so the built-in HTTP server can serve
    # static files from ../www/www-static relative to the runtime dir.
    _op25_apps_dir = os.path.dirname(OP25_MULTI_RX_PATH)
    _op25_www_dir = os.path.join(os.path.dirname(_op25_apps_dir), "www")
    _runtime_www_dir = os.path.join(os.path.dirname(runtime_dir), "www")
    if os.path.isdir(_op25_www_dir) and not os.path.exists(_runtime_www_dir):
        try:
            os.symlink(_op25_www_dir, _runtime_www_dir)
            print(f"OP25 runtime: symlinked {_runtime_www_dir} -> {_op25_www_dir}")
        except OSError as exc:
            print(f"WARNING: could not create www symlink: {exc}", file=sys.stderr)

    # Shared talkgroup tags file.
    tags_content = generate_tgid_tags_tsv(tg_labels)
    tags_path = os.path.join(runtime_dir, "tgid_tags.tsv")
    _atomic_write(tags_path, tags_content)
    print(f"OP25 runtime: wrote {tags_path} ({len(tg_labels)} talkgroups)")

    # Generate whitelist from talkgroup labels so OP25 only follows
    # the user's favorited talkgroups — matches HP scanner behavior.
    whitelist_path = ""
    if tg_labels:
        whitelist_path = os.path.join(runtime_dir, "whitelist.tsv")
        wl_lines = [
            str(tgid)
            for tgid in sorted(tg_labels.keys(), key=lambda t: int(t) if t.isdigit() else 0)
        ]
        _atomic_write(whitelist_path, "\n".join(wl_lines) + "\n")
        print(f"OP25 runtime: wrote {whitelist_path} ({len(wl_lines)} talkgroups)")
        # Inject whitelist into overrides for all systems (unless already set).
        for sys_def in systems:
            sname = sys_def["name"]
            if sname not in op25_overrides:
                op25_overrides[sname] = {}
            if not op25_overrides[sname].get("whitelist"):
                op25_overrides[sname]["whitelist"] = whitelist_path

    # Detect traffic follower dongle from allocator's traffic pool.
    # The permanent traffic dongle is always included regardless of VDL2 state.
    traffic_serial = _detect_traffic_dongle(dongle_assignments)
    traffic_system = ""
    if traffic_serial:
        # Check op25_system_config.json for a system with traffic_priority flag.
        traffic_system = ""
        for sys_def in systems:
            sname = sys_def["name"]
            sys_over = op25_overrides.get(sname) or {}
            if sys_over.get("traffic_priority"):
                traffic_system = sname
                break
        if not traffic_system:
            traffic_system = systems[0]["name"] if systems else ""
        print(f"OP25 runtime: traffic follower dongle={traffic_serial} -> {traffic_system}")
    else:
        print("OP25 runtime: no traffic follower dongle available")

    # Detect optional second traffic follower from VDL2 dongle sharing.
    # When VDL2 is inactive the spare dongle joins OP25 as sdr_traffic2,
    # giving each trunked system its own dedicated voice follower.
    # The handler touches a sentinel file before starting VDL2 so this
    # script excludes the dongle while VDL2 is using it.
    # IMPORTANT: sentinel must be OUTSIDE OP25_RUNTIME_DIR.
    # RuntimeDirectory=scannerproject/op25 causes systemd to wipe that entire
    # directory before ExecStartPre runs — anything inside would be deleted
    # before this script can check it.
    # Default: /run/user/1000/vdl2_dongle_reserved
    #   - writable by ubuntu (the service user and UI process)
    #   - cleared on reboot (correct: VDL2 won't be running after reboot)
    #   - survives OP25 service restarts
    _VDL2_SENTINEL = os.environ.get(
        "OP25_VDL2_SENTINEL",
        "/run/user/1000/vdl2_dongle_reserved",
    )
    _vdl2_serial = os.environ.get("VDL2_RTL_SERIAL", "").strip()
    _vdl2_share = os.environ.get("OP25_VDL2_TRAFFIC_SHARE", "1").strip() != "0"
    # Presence check: VDL2 borrowing only makes sense if the dongle is
    # actually plugged in.  Without this check, multi_rx.py crashes with
    # "Wrong rtlsdr device index given." when osmosdr.source('rtl=<serial>')
    # can't find the dongle, taking the whole OP25 process down even though
    # the RSPduo control sources opened fine.
    _enumerated = _enumerate_rtlsdr_serial_index_map()
    _vdl2_present = bool(_vdl2_serial) and _vdl2_serial in _enumerated
    traffic_serial_2 = ""
    traffic_system_2 = ""
    if _vdl2_share and _vdl2_present and not os.path.exists(_VDL2_SENTINEL):
        traffic_serial_2 = _vdl2_serial
        # System-agnostic: default target is systems[1], fallback to systems[0].
        traffic_system_2 = (
            systems[1]["name"] if len(systems) > 1 else (systems[0]["name"] if systems else "")
        )
        print(
            f"OP25 runtime: VDL2 dongle {_vdl2_serial} shared as sdr_traffic2 -> {traffic_system_2}"
        )
    else:
        reasons: list[str] = []
        if not _vdl2_share:
            reasons.append("OP25_VDL2_TRAFFIC_SHARE=0")
        if not _vdl2_serial:
            reasons.append("VDL2_RTL_SERIAL not set")
        elif not _vdl2_present:
            reasons.append(f"VDL2 dongle {_vdl2_serial} not enumerated by librtlsdr")
        if _vdl2_serial and os.path.exists(_VDL2_SENTINEL):
            reasons.append(f"sentinel present ({_VDL2_SENTINEL})")
        if reasons:
            print(f"OP25 runtime: VDL2 traffic share inactive: {', '.join(reasons)}")

    traffic_followers: list[tuple[str, str]] = []
    requested_serials = list(dongle_map.values())
    if traffic_serial:
        traffic_followers.append((traffic_serial, traffic_system))
        requested_serials.append(traffic_serial)
    if traffic_serial_2 and traffic_serial_2 not in requested_serials:
        traffic_followers.append((traffic_serial_2, traffic_system_2))
        requested_serials.append(traffic_serial_2)
    dongle_args_map = _build_dongle_arg_map(requested_serials)
    if dongle_args_map:
        for serial in requested_serials:
            serial = str(serial or "").strip()
            if not serial:
                continue
            resolved = dongle_args_map.get(serial, f"rtl={serial}")
            print(f"OP25 runtime: resolved tuner {serial} -> {resolved}")

    # Write trunk.tsv from the same systems/overrides used for multi_rx.json.
    trunk_path = os.path.join(runtime_dir, "trunk.tsv")
    trunk_content = generate_trunk_tsv(
        systems,
        dongle_assignments=dongle_assignments,
        op25_overrides=op25_overrides,
        tgid_tags_path=tags_path,
    )
    _atomic_write(trunk_path, trunk_content)
    print(f"OP25 runtime: wrote {trunk_path}")

    process_plans = _build_runtime_process_plans(
        systems,
        dongle_map,
        traffic_followers=traffic_followers,
    )
    if not process_plans:
        print("ERROR: no OP25 process plans could be built", file=sys.stderr)
        return 1
    print(f"OP25 runtime: {len(process_plans)} process plan(s) generated")

    instances_manifest = []
    process_specs: list[dict[str, object]] = []
    next_udp_base_port = _UDP_AUDIO_BASE_PORT
    split_runtime = len(process_plans) > 1

    for idx, plan in enumerate(process_plans):
        plan_label = str(plan.get("label") or f"op25-{idx}").strip() or f"op25-{idx}"
        plan_systems = [
            dict(system)
            for system in (plan.get("systems") or [])
            if isinstance(system, dict)
        ]
        plan_dongle_map = {
            str(system.get("name") or "").strip(): str(
                dongle_map.get(str(system.get("name") or "").strip()) or ""
            ).strip()
            for system in plan_systems
            if str(system.get("name") or "").strip()
        }
        followers = [
            (str(serial or "").strip(), str(target or "").strip())
            for serial, target in (plan.get("traffic_followers") or [])
            if str(serial or "").strip()
        ]
        follower_1 = followers[0] if len(followers) > 0 else ("", "")
        follower_2 = followers[1] if len(followers) > 1 else ("", "")
        if len(followers) > 2:
            print(
                f"WARNING: process {plan_label} has {len(followers)} followers; "
                "only the first two are supported"
            )

        plan_runtime_dir = runtime_dir
        if split_runtime:
            plan_runtime_dir = os.path.join(runtime_dir, f"instance-{idx}")
            os.makedirs(plan_runtime_dir, exist_ok=True)
        plan_config_path = os.path.join(plan_runtime_dir, "multi_rx.json")
        plan_http_port = OP25_STATUS_PORT + idx

        config = generate_multi_rx_config(
            plan_systems,
            plan_dongle_map,
            dongle_args_map=dongle_args_map,
            traffic_dongle_serial=follower_1[0],
            traffic_system_name=follower_1[1],
            traffic_dongle_serial_2=follower_2[0],
            traffic_system_name_2=follower_2[1],
            op25_overrides=op25_overrides,
            tgid_tags_path=tags_path,
            http_port=plan_http_port,
            udp_audio_base_port=next_udp_base_port,
            sample_rate=OP25_DEFAULT_SAMPLE_RATE,
            offset=OP25_DEFAULT_OFFSET,
        )

        _atomic_write(plan_config_path, json.dumps(config, indent=2) + "\n")
        if split_runtime:
            print(f"OP25 runtime: wrote {plan_config_path} ({plan_label})")
        else:
            print(f"OP25 runtime: wrote {plan_config_path}")

        for dev in config.get("devices", []):
            print(
                f"  [{plan_label}] device {dev['name']}: args={dev['args']}  "
                f"center={dev['frequency']/1e6:.5f} MHz  rate={dev['rate']/1e6:.1f}M"
            )
        for ch in config.get("channels", []):
            print(
                f"  [{plan_label}] channel {ch['name']}: device={ch['device']}  "
                f"system={ch['trunking_sysname']}  dest={ch['destination']}"
            )

        process_specs.append({
            "index": idx,
            "label": plan_label,
            "config_path": plan_config_path,
            "http_status_port": plan_http_port,
            "system_names": [
                str(system.get("name") or "").strip()
                for system in plan_systems
                if str(system.get("name") or "").strip()
            ],
            "device_args": [str(dev.get("args") or "") for dev in config.get("devices", [])],
            "sort_key": 0 if any("mode=MA" in str(dev.get("args") or "") for dev in config.get("devices", []))
            else (2 if any("mode=SL" in str(dev.get("args") or "") for dev in config.get("devices", [])) else 1),
            "delay_before_start_sec": (
                _rspduo_slave_start_delay_sec()
                if any("mode=SL" in str(dev.get("args") or "") for dev in config.get("devices", []))
                else 0.0
            ),
        })

        _multi_rx_udp_ports(config)
        for ch_idx, ch in enumerate(config.get("channels", [])):
            dest = ch.get("destination", "")
            udp_port = 0
            if dest.startswith("udp://"):
                try:
                    udp_port = int(dest.rsplit(":", 1)[1])
                except (ValueError, IndexError):
                    pass
            instances_manifest.append({
                "index": len(instances_manifest),
                "process_index": idx,
                "process_label": plan_label,
                "channel_name": ch.get("name", ""),
                "system_name": ch.get("trunking_sysname", ""),
                "device_name": ch.get("device", ""),
                "udp_audio_port": udp_port,
                "http_status_port": plan_http_port,
            })
        next_udp_base_port += max(0, len(config.get("channels", []))) * 2

    if split_runtime:
        summary_path = os.path.join(runtime_dir, "multi_rx.json")
        summary_payload = _summarize_processes(process_specs)
        _atomic_write(summary_path, json.dumps(summary_payload, indent=2) + "\n")
        print(f"OP25 runtime: wrote {summary_path} (split runtime summary)")

    # Generate start.sh.
    ordered_specs = [
        {key: value for key, value in spec.items() if key != "sort_key"}
        for spec in sorted(
            process_specs,
            key=lambda spec: (int(spec.get("sort_key") or 0), int(spec.get("index") or 0)),
        )
    ]
    start_script = _generate_multi_rx_start_script(ordered_specs)
    start_path = os.path.join(runtime_dir, "start.sh")
    _atomic_write(start_path, start_script)
    os.chmod(
        start_path,
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
    )
    print(f"OP25 runtime: wrote {start_path}")

    # Write instances manifest for audio bridge and adapter compatibility.
    manifest_path = os.path.join(runtime_dir, "instances.json")
    _atomic_write(manifest_path, json.dumps(instances_manifest, indent=2) + "\n")
    print(f"OP25 runtime: wrote {manifest_path} ({len(instances_manifest)} channels)")

    device_count = sum(len(spec.get("device_args") or []) for spec in process_specs)
    print(
        f"OP25 runtime: ready ({len(process_specs)} process(es), "
        f"{device_count} devices, {len(instances_manifest)} channels)"
    )
    return 0


def _atomic_write(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
