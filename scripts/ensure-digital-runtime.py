#!/usr/bin/env python3
"""Ensure digital runtime prerequisites exist before starting SDRTrunk.

This script is intended for scanner-digital.service ExecStartPre.
It repairs/creates the active profile symlink and syncs playlist runtime
frequency from control_channels.txt.
"""

from __future__ import annotations

import json
import os
import re
import csv
import time
import xml.etree.ElementTree as ET
from pathlib import Path

FREQ_RE = re.compile(r"\d+\.\d+")
_TRUTHY = ("1", "true", "yes", "on")

PROFILES_DIR = Path(os.getenv("DIGITAL_PROFILES_DIR", "/etc/scannerproject/digital/profiles")).expanduser()
ACTIVE_LINK = Path(os.getenv("DIGITAL_ACTIVE_PROFILE_LINK", "/etc/scannerproject/digital/active")).expanduser()
PLAYLIST_PATH = Path(
    os.getenv("DIGITAL_PLAYLIST_PATH", str(Path.home() / "SDRTrunk" / "playlist" / "default.xml"))
).expanduser()
DEFAULT_PROFILE = os.getenv("DIGITAL_BOOT_DEFAULT_PROFILE", "default").strip()
DIGITAL_RTL_DEVICE = os.getenv("DIGITAL_RTL_DEVICE", "").strip()
DIGITAL_RTL_SERIAL = os.getenv("DIGITAL_RTL_SERIAL", "").strip()
DIGITAL_RTL_SERIAL_SECONDARY = os.getenv(
    "DIGITAL_RTL_SERIAL_SECONDARY",
    os.getenv("DIGITAL_RTL_SERIAL_2", ""),
).strip()
DIGITAL_PREFERRED_TUNER = os.getenv("DIGITAL_PREFERRED_TUNER", "").strip()
DIGITAL_FORCE_PREFERRED_TUNER = os.getenv(
    "DIGITAL_FORCE_PREFERRED_TUNER",
    "0",
).strip().lower() in _TRUTHY
DIGITAL_USE_MULTI_FREQ_SOURCE = os.getenv("DIGITAL_USE_MULTI_FREQ_SOURCE", "1").strip().lower() in _TRUTHY
DIGITAL_SDRTRUNK_STREAM_NAME = os.getenv("DIGITAL_SDRTRUNK_STREAM_NAME", "DIGITAL").strip()
DIGITAL_ATTACH_BROADCAST_CHANNEL = os.getenv("DIGITAL_ATTACH_BROADCAST_CHANNEL", "1").strip().lower() in _TRUTHY
DIGITAL_IGNORE_DATA_CALLS = os.getenv("DIGITAL_IGNORE_DATA_CALLS", "1").strip().lower() in _TRUTHY
AIRBAND_RTL_SERIAL = os.getenv("AIRBAND_RTL_SERIAL", os.getenv("SCANNER1_RTL_DEVICE", "")).strip()
GROUND_RTL_SERIAL = os.getenv("GROUND_RTL_SERIAL", os.getenv("SCANNER2_RTL_DEVICE", "")).strip()
SDRTRUNK_TUNER_CONFIG_PATH = Path(
    os.getenv(
        "DIGITAL_TUNER_CONFIG_PATH",
        str(Path.home() / "SDRTrunk" / "configuration" / "tuner_configuration.json"),
    )
).expanduser()
USB_SYSFS_ROOT = Path(os.getenv("DIGITAL_USB_SYSFS_ROOT", "/sys/bus/usb/devices")).expanduser()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


DIGITAL_SOURCE_ROTATION_DELAY_MS = max(100, _env_int("DIGITAL_SOURCE_ROTATION_DELAY_MS", 500))


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} ensure-digital-runtime: {msg}")


def _sysfs_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _discover_rtl_unique_ids_by_serial() -> dict[str, str]:
    out: dict[str, str] = {}
    root = USB_SYSFS_ROOT
    if not root.is_dir():
        return out

    for dev in root.iterdir():
        serial_path = dev / "serial"
        if not serial_path.is_file():
            continue
        serial = _sysfs_text(serial_path)
        if not serial:
            continue
        if _sysfs_text(dev / "idVendor").lower() != "0bda":
            continue
        if _sysfs_text(dev / "idProduct").lower() != "2838":
            continue

        # Example device directory name: "3-1.1.2" => Bus 3, Port 1.1.2
        name = dev.name
        if "-" not in name:
            continue
        bus, port = name.split("-", 1)
        if not bus or not port:
            continue
        out[serial] = f"RTL-2832 USB Bus:{bus} Port:{port}"
    return out


def _default_tuner_config(unique_id: str, template: dict[str, object] | None = None) -> dict[str, object]:
    if template:
        cfg = dict(template)
        cfg["uniqueID"] = unique_id
        return cfg

    return {
        "type": "r820TTunerConfiguration",
        "masterGain": "GAIN_327",
        "mixerGain": "GAIN_105",
        "lnagain": "GAIN_222",
        "vgagain": "GAIN_210",
        "sampleRate": "RATE_2_400MHZ",
        "biasT": False,
        "frequency": 101100000,
        "frequencyCorrection": 0.0,
        "uniqueID": unique_id,
        "autoPPMCorrectionEnabled": True,
        "minimumFrequency": 0,
        "maximumFrequency": 0,
    }


def _sync_tuner_configuration() -> dict[str, object]:
    if not SDRTRUNK_TUNER_CONFIG_PATH.is_file():
        return {"updated": False, "reason": "missing_tuner_config"}

    try:
        raw = SDRTRUNK_TUNER_CONFIG_PATH.read_text(encoding="utf-8", errors="ignore")
        data = json.loads(raw)
    except Exception:
        return {"updated": False, "reason": "invalid_tuner_config"}

    if not isinstance(data, dict):
        return {"updated": False, "reason": "invalid_tuner_config_type"}

    serial_to_uid = _discover_rtl_unique_ids_by_serial()
    digital_serials = [s for s in (DIGITAL_RTL_SERIAL, DIGITAL_RTL_SERIAL_SECONDARY) if s]
    analog_serials = [s for s in (AIRBAND_RTL_SERIAL, GROUND_RTL_SERIAL) if s]

    digital_uids = {serial_to_uid[s] for s in digital_serials if s in serial_to_uid}
    analog_uids = {serial_to_uid[s] for s in analog_serials if s in serial_to_uid}

    changed = False
    disabled_in = data.get("disabledTuners")
    disabled = disabled_in if isinstance(disabled_in, list) else []

    kept: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for entry in disabled:
        if not isinstance(entry, dict):
            continue
        tuner_class = str(entry.get("tunerClass") or "").strip()
        tuner_id = str(entry.get("id") or "").strip()
        if not tuner_id:
            continue

        if tuner_class != "RTL2832":
            if tuner_id not in seen_ids:
                kept.append({"tunerClass": tuner_class or "RTL2832", "id": tuner_id})
                seen_ids.add(tuner_id)
            continue

        # Never keep a disabled entry for currently mapped digital tuner ports.
        if tuner_id in digital_uids:
            changed = True
            continue

        if tuner_id not in seen_ids:
            kept.append({"tunerClass": "RTL2832", "id": tuner_id})
            seen_ids.add(tuner_id)

    for uid in sorted(analog_uids):
        if uid not in seen_ids:
            kept.append({"tunerClass": "RTL2832", "id": uid})
            seen_ids.add(uid)
            changed = True

    if disabled != kept:
        data["disabledTuners"] = kept
        changed = True

    cfg_in = data.get("tunerConfigurations")
    cfgs = cfg_in if isinstance(cfg_in, list) else []
    template = next((c for c in cfgs if isinstance(c, dict) and c.get("uniqueID")), None)
    existing_ids = {
        str(c.get("uniqueID")).strip()
        for c in cfgs
        if isinstance(c, dict) and str(c.get("uniqueID") or "").strip()
    }
    for uid in sorted(digital_uids):
        if uid in existing_ids:
            continue
        cfgs.append(_default_tuner_config(uid, template=template if isinstance(template, dict) else None))
        existing_ids.add(uid)
        changed = True

    if cfg_in != cfgs:
        data["tunerConfigurations"] = cfgs
        changed = True

    if changed:
        SDRTRUNK_TUNER_CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return {
        "updated": changed,
        "reason": "ok",
        "digital_uids": sorted(digital_uids),
        "analog_uids": sorted(analog_uids),
    }


def _profile_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            out.append(entry)
    return out


def _active_target(link: Path) -> Path | None:
    if not link.is_symlink():
        return None
    try:
        target = link.resolve(strict=True)
    except Exception:
        return None
    if target.is_dir():
        return target
    return None


def _choose_profile() -> Path:
    active = _active_target(ACTIVE_LINK)
    if active is not None:
        return active

    if DEFAULT_PROFILE:
        candidate = PROFILES_DIR / DEFAULT_PROFILE
        if candidate.is_dir():
            return candidate

    profiles = _profile_dirs(PROFILES_DIR)
    if profiles:
        return profiles[0]

    raise RuntimeError(f"no digital profiles found in {PROFILES_DIR}")


def _point_active_link(target: Path) -> None:
    parent = ACTIVE_LINK.parent
    parent.mkdir(parents=True, exist_ok=True)
    if ACTIVE_LINK.exists() and not ACTIVE_LINK.is_symlink():
        raise RuntimeError(f"{ACTIVE_LINK} exists and is not a symlink")
    tmp_link = ACTIVE_LINK.with_name(f"{ACTIVE_LINK.name}.tmp")
    try:
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
    except Exception:
        pass
    tmp_link.symlink_to(target)
    os.replace(tmp_link, ACTIVE_LINK)


def _read_control_channels_hz(profile_dir: Path) -> list[int]:
    path = profile_dir / "control_channels.txt"
    if not path.is_file():
        raise RuntimeError(f"missing control_channels.txt in {profile_dir}")
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        raise RuntimeError(f"failed to read {path}: {e}") from e
    channels_hz: list[int] = []
    seen: set[int] = set()
    for line in lines:
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        match = FREQ_RE.search(raw)
        if not match:
            continue
        try:
            hz = int(round(float(match.group(0)) * 1_000_000))
        except Exception:
            continue
        if hz <= 0 or hz in seen:
            continue
        seen.add(hz)
        channels_hz.append(hz)
    if channels_hz:
        return channels_hz
    raise RuntimeError(f"no control channel frequencies in {path}")


def _load_playlist(path: Path) -> ET.ElementTree:
    if path.is_file():
        try:
            return ET.parse(path)
        except ET.ParseError:
            broken = path.with_suffix(path.suffix + ".broken")
            try:
                os.replace(path, broken)
                _log(f"playlist parse failed; moved broken file to {broken}")
            except Exception:
                _log("playlist parse failed; unable to move broken file, recreating in place")
    root = ET.Element("playlist", {"version": "4"})
    return ET.ElementTree(root)


def _ensure_child(parent: ET.Element, tag: str) -> ET.Element:
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    return child


def _preferred_tuner_target() -> str:
    # In dual-dongle digital mode, don't pin the control source to a single
    # tuner. Let SDRTrunk allocate one tuner for control and the other for
    # traffic channels.
    if DIGITAL_RTL_SERIAL_SECONDARY and not DIGITAL_FORCE_PREFERRED_TUNER:
        return ""
    if DIGITAL_PREFERRED_TUNER:
        return DIGITAL_PREFERRED_TUNER
    if DIGITAL_RTL_SERIAL:
        return DIGITAL_RTL_SERIAL
    if DIGITAL_RTL_DEVICE and not DIGITAL_RTL_DEVICE.isdigit():
        return DIGITAL_RTL_DEVICE
    return ""


def _sync_source_configuration(source_conf: ET.Element, control_channels_hz: list[int]) -> dict[str, object]:
    use_multi = DIGITAL_USE_MULTI_FREQ_SOURCE and len(control_channels_hz) > 1
    if use_multi:
        source_conf.set("type", "sourceConfigTunerMultipleFrequency")
        source_conf.set("source_type", "TUNER_MULTIPLE_FREQUENCIES")
        source_conf.set("frequency_rotation_delay", str(DIGITAL_SOURCE_ROTATION_DELAY_MS))
        if "frequency" in source_conf.attrib:
            del source_conf.attrib["frequency"]
        for child in list(source_conf):
            if child.tag == "frequency":
                source_conf.remove(child)
        for hz in control_channels_hz:
            child = ET.SubElement(source_conf, "frequency")
            child.text = str(hz)
    else:
        source_conf.set("type", "sourceConfigTuner")
        source_conf.set("source_type", "TUNER")
        source_conf.set("frequency", str(control_channels_hz[0]))
        if "frequency_rotation_delay" in source_conf.attrib:
            del source_conf.attrib["frequency_rotation_delay"]
        for child in list(source_conf):
            if child.tag == "frequency":
                source_conf.remove(child)

    preferred_tuner = _preferred_tuner_target()
    if preferred_tuner:
        source_conf.set("preferred_tuner", preferred_tuner)
    elif "preferred_tuner" in source_conf.attrib:
        del source_conf.attrib["preferred_tuner"]

    return {
        "source_mode": "multi" if use_multi else "single",
        "control_count": len(control_channels_hz),
        "control_hz": int(control_channels_hz[0]),
        "preferred_tuner": preferred_tuner,
    }


def _sync_alias_broadcast_channels(root: ET.Element, alias_list_name: str) -> int:
    stream_name = str(DIGITAL_SDRTRUNK_STREAM_NAME or "").strip()
    if not DIGITAL_ATTACH_BROADCAST_CHANNEL or not alias_list_name or not stream_name:
        return 0

    added = 0
    for alias in root.findall("alias"):
        if str(alias.get("list", "")).strip() != alias_list_name:
            continue

        has_talkgroup_id = False
        has_stream_binding = False
        for alias_id in alias.findall("id"):
            id_type = str(alias_id.get("type", "")).strip().lower()
            if id_type in {"talkgroup", "talkgrouprange", "p25fullyqualifiedtalkgroup", "talkgroupid"}:
                has_talkgroup_id = True
            if id_type == "broadcastchannel" and str(alias_id.get("channel", "")).strip() == stream_name:
                has_stream_binding = True

        if not has_talkgroup_id or has_stream_binding:
            continue

        ET.SubElement(
            alias,
            "id",
            {
                "type": "broadcastChannel",
                "channel": stream_name,
            },
        )
        added += 1

    return added


def _profile_alias_seed_rows(profile_dir: Path) -> list[tuple[str, str, str]]:
    candidates = (profile_dir / "talkgroups.csv", profile_dir / "talkgroups_with_group.csv")
    source = None
    for candidate in candidates:
        if candidate.is_file():
            source = candidate
            break
    if source is None:
        return []

    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    try:
        with source.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not row:
                    continue
                row_norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
                dec = row_norm.get("dec") or row_norm.get("decimal") or ""
                if not dec.isdigit() or dec in seen:
                    continue
                mode = str(row_norm.get("mode") or "").strip().upper()
                if mode and "E" in mode:
                    continue
                alpha = row_norm.get("alpha tag") or row_norm.get("alpha_tag") or row_norm.get("alpha") or ""
                desc = row_norm.get("description") or ""
                group = row_norm.get("group") or row_norm.get("tag") or "Imported"
                name = alpha or desc or f"TG {dec}"
                seen.add(dec)
                rows.append((dec, name, group))
    except Exception:
        return []
    return rows


def _alias_list_talkgroup_count(root: ET.Element, alias_list_name: str) -> int:
    count = 0
    for alias in root.findall("alias"):
        if str(alias.get("list", "")).strip() != alias_list_name:
            continue
        for alias_id in alias.findall("id"):
            if str(alias_id.get("type", "")).strip().lower() in {
                "talkgroup",
                "talkgrouprange",
                "p25fullyqualifiedtalkgroup",
                "talkgroupid",
            }:
                count += 1
                break
    return count


_ALIAS_TG_ID_TYPES = {
    "talkgroup",
    "talkgrouprange",
    "p25fullyqualifiedtalkgroup",
    "talkgroupid",
}


def _alias_talkgroup_value(alias_id: ET.Element) -> str:
    if str(alias_id.get("type", "")).strip().lower() not in _ALIAS_TG_ID_TYPES:
        return ""
    for key in ("value", "talkgroup", "tgid", "id"):
        value = str(alias_id.get(key, "")).strip()
        if value.isdigit():
            return value
    return ""


def _collect_alias_talkgroup_map(root: ET.Element, alias_list_name: str) -> dict[str, ET.Element]:
    mapping: dict[str, ET.Element] = {}
    for alias in root.findall("alias"):
        if str(alias.get("list", "")).strip() != alias_list_name:
            continue
        for alias_id in alias.findall("id"):
            dec = _alias_talkgroup_value(alias_id)
            if dec and dec not in mapping:
                mapping[dec] = alias
                break
    return mapping


def _seed_aliases_from_profile(root: ET.Element, alias_list_name: str, profile_dir: Path) -> int:
    if not alias_list_name:
        return 0

    seed_rows = _profile_alias_seed_rows(profile_dir)
    if not seed_rows:
        return 0

    existing = _collect_alias_talkgroup_map(root, alias_list_name)
    stream_name = str(DIGITAL_SDRTRUNK_STREAM_NAME or "").strip()
    added = 0
    for dec, name, group in seed_rows:
        alias = existing.get(dec)
        if alias is not None:
            if name and not str(alias.get("name", "")).strip():
                alias.set("name", name)
            if group and not str(alias.get("group", "")).strip():
                alias.set("group", group)
            continue

        alias = ET.SubElement(
            root,
            "alias",
            {
                "group": group or "Imported",
                "color": "0",
                "name": name,
                "list": alias_list_name,
            },
        )
        ET.SubElement(
            alias,
            "id",
            {
                "type": "talkgroup",
                "value": dec,
                "protocol": "APCO25",
            },
        )
        if DIGITAL_ATTACH_BROADCAST_CHANNEL and stream_name:
            ET.SubElement(
                alias,
                "id",
                {
                    "type": "broadcastChannel",
                    "channel": stream_name,
                },
            )
        added += 1
        existing[dec] = alias

    has_priority = False
    for alias in root.findall("alias"):
        if str(alias.get("list", "")).strip() != alias_list_name:
            continue
        if any(str(alias_id.get("type", "")).strip().lower() == "priority" for alias_id in alias.findall("id")):
            has_priority = True
            break
    if not has_priority:
        priority_alias = ET.SubElement(
            root,
            "alias",
            {
                "color": "0",
                "name": f"{alias_list_name}-ALL",
                "list": alias_list_name,
            },
        )
        ET.SubElement(
            priority_alias,
            "id",
            {
                "type": "priority",
                "priority": "1",
            },
        )

    return added


def _sync_decode_configuration(channel: ET.Element) -> None:
    decode_conf = channel.find("decode_configuration")
    if decode_conf is None:
        decode_conf = ET.SubElement(channel, "decode_configuration")

    dtype = str(decode_conf.get("type", "")).strip() or "decodeConfigP25Phase1"
    decode_conf.set("type", dtype)
    if dtype == "decodeConfigP25Phase1":
        decode_conf.set("modulation", str(decode_conf.get("modulation", "")).strip() or "C4FM")
    decode_conf.set("traffic_channel_pool_size", "20")
    decode_conf.set("ignore_data_calls", "true" if DIGITAL_IGNORE_DATA_CALLS else "false")


def _sync_playlist(profile_dir: Path, control_channels_hz: list[int]) -> dict[str, object]:
    PLAYLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tree = _load_playlist(PLAYLIST_PATH)
    root = tree.getroot()
    profile_id = profile_dir.name

    channel = root.find("channel")
    if channel is None:
        channel = ET.SubElement(
            root,
            "channel",
            {
                "system": "P25",
                "name": profile_id,
                "enabled": "true",
                "order": "1",
            },
        )

    channel.set("enabled", "true")
    channel.set("name", profile_id)

    # Optional profile-local alias list override allows sub-profiles to reuse
    # an existing SDRTrunk alias list name (without duplicating exports).
    alias_name = profile_id.upper()
    alias_name_path = profile_dir / "alias_list_name.txt"
    if alias_name_path.is_file():
        try:
            for raw in alias_name_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                value = raw.strip()
                if value:
                    alias_name = value
                    break
        except Exception:
            alias_name = profile_id.upper()

    alias_list = _ensure_child(channel, "alias_list_name")
    alias_list.text = alias_name
    seeded_aliases = _seed_aliases_from_profile(root, alias_name, profile_dir)
    stream_alias_updates = _sync_alias_broadcast_channels(root, alias_name)

    event_conf = _ensure_child(channel, "event_log_configuration")
    existing = {str(e.text or "").strip() for e in event_conf.findall("logger")}
    for logger_name in ("CALL_EVENT", "TRAFFIC_CALL_EVENT", "DECODED_MESSAGE"):
        if logger_name not in existing:
            logger = ET.SubElement(event_conf, "logger")
            logger.text = logger_name

    source_conf = _ensure_child(channel, "source_configuration")
    source_state = _sync_source_configuration(source_conf, control_channels_hz)

    _sync_decode_configuration(channel)

    _ensure_child(channel, "record_configuration")

    tree.write(PLAYLIST_PATH, encoding="utf-8", xml_declaration=False)
    source_state["seeded_aliases"] = seeded_aliases
    source_state["stream_alias_updates"] = stream_alias_updates
    source_state["stream_name"] = DIGITAL_SDRTRUNK_STREAM_NAME
    return source_state


def main() -> int:
    try:
        target = _choose_profile()
        _point_active_link(target)
        tuner_state = _sync_tuner_configuration()
        control_channels_hz = _read_control_channels_hz(target)
        source_state = _sync_playlist(target, control_channels_hz)
        _log(
            "active profile="
            f"{target.name} control_hz={source_state['control_hz']} "
            f"control_count={source_state['control_count']} "
            f"source_mode={source_state['source_mode']} "
            f"preferred_tuner={source_state['preferred_tuner'] or 'auto'} "
            f"stream={source_state.get('stream_name') or 'unset'} "
            f"seeded_aliases={source_state.get('seeded_aliases', 0)} "
            f"stream_alias_updates={source_state.get('stream_alias_updates', 0)} "
            f"digital_secondary={DIGITAL_RTL_SERIAL_SECONDARY or 'unset'} "
            f"tuner_config_updated={bool(tuner_state.get('updated'))} "
            f"digital_uids={','.join(tuner_state.get('digital_uids', [])) or 'none'} "
            f"analog_uids={','.join(tuner_state.get('analog_uids', [])) or 'none'}"
        )
        return 0
    except Exception as e:
        _log(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
