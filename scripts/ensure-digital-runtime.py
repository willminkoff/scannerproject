#!/usr/bin/env python3
"""Ensure digital runtime prerequisites exist before starting SDRTrunk.

This script is intended for scanner-digital.service ExecStartPre.
It repairs/creates the active profile symlink and syncs playlist runtime
frequency from control_channels.txt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import csv
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("xsi", _XSI_NS)

logger = logging.getLogger(__name__)

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
DIGITAL_RTL_SERIAL_TERTIARY = os.getenv(
    "DIGITAL_RTL_SERIAL_TERTIARY",
    os.getenv("DIGITAL_RTL_SERIAL_3", ""),
).strip()
DIGITAL_PREFERRED_TUNER = os.getenv("DIGITAL_PREFERRED_TUNER", "").strip()
DIGITAL_FORCE_PREFERRED_TUNER = os.getenv(
    "DIGITAL_FORCE_PREFERRED_TUNER",
    "0",
).strip().lower() in _TRUTHY
DONGLE_ASSIGNMENTS_PATH = os.getenv(
    "DONGLE_ASSIGNMENTS_PATH",
    str(Path.home() / ".local" / "state" / "scannerproject" / "airband_ui_dongle_assignments.json"),
).strip()
DIGITAL_REQUIRE_TUNER = os.getenv("DIGITAL_REQUIRE_TUNER", "1").strip().lower() in _TRUTHY
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


def _env_set(name: str) -> bool:
    raw = os.getenv(name)
    return raw is not None and str(raw).strip() != ""


DIGITAL_SOURCE_ROTATION_DELAY_MS = max(100, _env_int("DIGITAL_SOURCE_ROTATION_DELAY_MS", 500))
ICECAST_HOST = os.getenv("ICECAST_HOST", "127.0.0.1").strip() or "127.0.0.1"
ICECAST_PORT = str(_env_int("ICECAST_PORT", 8000))
ICECAST_SOURCE_USER = os.getenv("ICECAST_SOURCE_USER", "source").strip() or "source"
ICECAST_SOURCE_PASSWORD = os.getenv("ICECAST_SOURCE_PASSWORD", "062352").strip() or "062352"
DIGITAL_STREAM_MOUNT = os.getenv(
    "DIGITAL_STREAM_MOUNT",
    os.getenv("DIGITAL_MIXER_DIGITAL_MOUNT", "DIGITAL.mp3"),
).strip().lstrip("/") or "DIGITAL.mp3"
DIGITAL_AUTO_ADOPT_EXTRA_TUNERS = str(os.getenv("DIGITAL_AUTO_ADOPT_EXTRA_TUNERS", "1")).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
DIGITAL_STREAM_BITRATE = max(8, _env_int("DIGITAL_STREAM_BITRATE", 24))
DIGITAL_STREAM_LEGACY_BITRATE = 32
DIGITAL_STREAM_SAMPLE_RATE = max(8000, _env_int("DIGITAL_STREAM_SAMPLE_RATE", 16000))
DIGITAL_STREAM_CHANNELS = 1 if _env_int("DIGITAL_STREAM_CHANNELS", 1) <= 1 else 2
DIGITAL_STREAM_MAX_RECORDING_AGE_MS = max(60000, _env_int("DIGITAL_STREAM_MAX_RECORDING_AGE_MS", 600000))
DIGITAL_STREAM_DELAY_MS = max(0, _env_int("DIGITAL_STREAM_DELAY_MS", 0))
DIGITAL_STREAM_BITRATE_OVERRIDE = _env_set("DIGITAL_STREAM_BITRATE")
DIGITAL_STREAM_SAMPLE_RATE_OVERRIDE = _env_set("DIGITAL_STREAM_SAMPLE_RATE")
DIGITAL_STREAM_CHANNELS_OVERRIDE = _env_set("DIGITAL_STREAM_CHANNELS")
DIGITAL_STREAM_MAX_RECORDING_AGE_OVERRIDE = _env_set("DIGITAL_STREAM_MAX_RECORDING_AGE_MS")
DIGITAL_STREAM_DELAY_OVERRIDE = _env_set("DIGITAL_STREAM_DELAY_MS")
DIGITAL_P25_MODULATION = os.getenv("DIGITAL_P25_MODULATION", "").strip()
DIGITAL_LOCAL_MONITOR = os.getenv("DIGITAL_LOCAL_MONITOR", "0").strip().lower() in _TRUTHY
DIGITAL_DISABLE_LOCAL_SPEAKER_AUDIO = os.getenv("DIGITAL_DISABLE_LOCAL_SPEAKER_AUDIO", "1").strip().lower() in _TRUTHY
DIGITAL_LOCAL_AUDIO_MIXER = os.getenv("DIGITAL_LOCAL_AUDIO_MIXER", "").strip()
SDRTRUNK_PLAYBACK_PREFS_PATH = Path(
    os.getenv(
        "DIGITAL_PLAYBACK_PREFS_PATH",
        str(Path.home() / ".java" / ".userPrefs" / "io" / "github" / "dsheirer" / "preference" / "playback" / "prefs.xml"),
    )
).expanduser()
ASOUND_CARDS_PATH = Path(os.getenv("DIGITAL_ASOUND_CARDS_PATH", "/proc/asound/cards")).expanduser()
ASOUND_PCM_PATH = Path(os.getenv("DIGITAL_ASOUND_PCM_PATH", "/proc/asound/pcm")).expanduser()
_PLAYBACK_PREF_KEY = "audio.playback.mixer.channel.configuration"
_JAVA_PREFS_XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
_JAVA_PREFS_XML_DOCTYPE = '<!DOCTYPE map SYSTEM "http://java.sun.com/dtd/preferences.dtd">\n'


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


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _parse_card_labels(cards_text: str) -> dict[int, str]:
    labels: dict[int, str] = {}
    for raw in str(cards_text or "").splitlines():
        line = raw.rstrip()
        m = re.match(r"^\s*(\d+)\s+\[([^\]]+)\]", line)
        if not m:
            continue
        card = int(m.group(1))
        label = str(m.group(2) or "").strip()
        if label:
            labels[card] = label
    return labels


def _detect_preferred_local_audio_mixer(cards_text: str, pcm_text: str) -> str:
    card_labels = _parse_card_labels(cards_text)
    best: tuple[int, int, int, str] | None = None
    for raw in str(pcm_text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^(\d+)-(\d+):\s*(.*)$", line)
        if not m:
            continue
        card_num = int(m.group(1))
        dev_num = int(m.group(2))
        body = str(m.group(3) or "").strip()
        low = body.lower()
        if "playback" not in low:
            continue
        score = 0
        if "hdmi" in low:
            score += 100
        if "digital" in low or "iec958" in low:
            score += 40
        if "analog" in low:
            score -= 40
        if dev_num != 0:
            score += 10
        card_label = card_labels.get(card_num, f"CARD{card_num}")
        mixer_name = f"{card_label} [plughw:{card_num},{dev_num}] - STEREO"
        candidate = (score, card_num, dev_num, mixer_name)
        if best is None or candidate > best:
            best = candidate
    return best[3] if best else ""


def _load_java_pref_entries(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    if root.tag != "map":
        return {}
    out: dict[str, str] = {}
    for entry in root.findall("entry"):
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        out[key] = str(entry.get("value") or "")
    return out


def _write_java_pref_entries(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        _JAVA_PREFS_XML_HEADER.rstrip("\n"),
        _JAVA_PREFS_XML_DOCTYPE.rstrip("\n"),
        '<map MAP_XML_VERSION="1.0">',
    ]
    for key in sorted(entries):
        value = str(entries[key])
        key_xml = _xml_escape(str(key), {'"': "&quot;"})
        value_xml = _xml_escape(value, {'"': "&quot;"})
        lines.append(f'  <entry key="{key_xml}" value="{value_xml}"/>')
    lines.append("</map>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sync_local_audio_playback_preference() -> dict[str, object]:
    if DIGITAL_LOCAL_MONITOR:
        return {
            "updated": False,
            "reason": "local_monitor_enabled",
            "mixer": "",
        }
    if not DIGITAL_DISABLE_LOCAL_SPEAKER_AUDIO:
        return {
            "updated": False,
            "reason": "disabled_by_env",
            "mixer": "",
        }

    mixer = DIGITAL_LOCAL_AUDIO_MIXER
    if not mixer:
        mixer = _detect_preferred_local_audio_mixer(_read_text(ASOUND_CARDS_PATH), _read_text(ASOUND_PCM_PATH))
    if not mixer:
        return {
            "updated": False,
            "reason": "no_candidate_mixer",
            "mixer": "",
        }

    entries = _load_java_pref_entries(SDRTRUNK_PLAYBACK_PREFS_PATH)
    prior = str(entries.get(_PLAYBACK_PREF_KEY) or "")
    if prior == mixer:
        return {
            "updated": False,
            "reason": "already_set",
            "mixer": mixer,
        }

    entries[_PLAYBACK_PREF_KEY] = mixer
    _write_java_pref_entries(SDRTRUNK_PLAYBACK_PREFS_PATH, entries)
    return {
        "updated": True,
        "reason": "set",
        "mixer": mixer,
    }


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


def _discover_tuner_uid_state() -> dict[str, object]:
    serial_to_uid = _discover_rtl_unique_ids_by_serial()
    digital_serials = [
        s
        for s in (
            DIGITAL_RTL_SERIAL,
            DIGITAL_RTL_SERIAL_SECONDARY,
            DIGITAL_RTL_SERIAL_TERTIARY,
        )
        if s
    ]
    analog_serials = [s for s in (AIRBAND_RTL_SERIAL, GROUND_RTL_SERIAL) if s]

    all_rtl_uids = set(serial_to_uid.values())
    digital_uids = {serial_to_uid[s] for s in digital_serials if s in serial_to_uid}
    analog_uids = {serial_to_uid[s] for s in analog_serials if s in serial_to_uid}
    auto_extra_serials: list[str] = []
    if DIGITAL_AUTO_ADOPT_EXTRA_TUNERS:
        for serial, uid in sorted(serial_to_uid.items()):
            if serial in digital_serials or serial in analog_serials:
                continue
            if uid in analog_uids or uid in digital_uids:
                continue
            digital_uids.add(uid)
            auto_extra_serials.append(serial)
    uid_source = "configured"
    if not digital_uids:
        fallback_uids = {uid for uid in all_rtl_uids if uid not in analog_uids}
        if fallback_uids:
            digital_uids = fallback_uids
            uid_source = "fallback_non_analog"
        else:
            uid_source = "none"
    elif auto_extra_serials:
        uid_source = "configured_plus_auto_extra"
    return {
        "digital_uids": sorted(digital_uids),
        "analog_uids": sorted(analog_uids),
        "available_rtl_uids": sorted(all_rtl_uids),
        "available_rtl_serials": sorted(serial_to_uid),
        "digital_auto_extra_serials": auto_extra_serials,
        "digital_uid_source": uid_source,
    }


def _sync_tuner_configuration() -> dict[str, object]:
    tuner_state = _discover_tuner_uid_state()
    if not SDRTRUNK_TUNER_CONFIG_PATH.is_file():
        payload = dict(tuner_state)
        payload.update({"updated": False, "reason": "missing_tuner_config"})
        return payload

    try:
        raw = SDRTRUNK_TUNER_CONFIG_PATH.read_text(encoding="utf-8", errors="ignore")
        data = json.loads(raw)
    except Exception:
        payload = dict(tuner_state)
        payload.update({"updated": False, "reason": "invalid_tuner_config"})
        return payload

    if not isinstance(data, dict):
        payload = dict(tuner_state)
        payload.update({"updated": False, "reason": "invalid_tuner_config_type"})
        return payload

    digital_uids = set(tuner_state.get("digital_uids") or [])
    analog_uids = set(tuner_state.get("analog_uids") or [])

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
    present_rtl_uids = {
        str(uid or "").strip()
        for uid in (tuner_state.get("available_rtl_uids") or [])
        if str(uid or "").strip()
    }
    cleaned_cfgs: list[dict[str, object]] = []
    seen_cfg_ids: set[str] = set()
    for cfg in cfgs:
        if not isinstance(cfg, dict):
            continue
        uid = str(cfg.get("uniqueID") or "").strip()
        if not uid:
            cleaned_cfgs.append(dict(cfg))
            continue
        if uid in seen_cfg_ids:
            changed = True
            continue
        if uid.startswith("RTL-2832 ") and present_rtl_uids and uid not in present_rtl_uids:
            changed = True
            continue
        cleaned_cfgs.append(dict(cfg))
        seen_cfg_ids.add(uid)
    cfgs = cleaned_cfgs
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

    payload = dict(tuner_state)
    payload.update({
        "updated": changed,
        "reason": "ok",
    })
    return payload


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
        logger.debug("Failed removing temporary active digital profile link %s", tmp_link, exc_info=True)
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


def _read_primary_system_name(profile_dir: Path) -> str:
    """Read the first system name from systems.json in the profile directory."""
    systems_path = profile_dir / "systems.json"
    if not systems_path.is_file():
        return ""
    try:
        with open(systems_path, "r", encoding="utf-8", errors="ignore") as fh:
            payload = json.load(fh)
        systems_raw = payload.get("systems") if isinstance(payload, dict) else payload
        if isinstance(systems_raw, list) and systems_raw:
            name = str(systems_raw[0].get("name", "") if isinstance(systems_raw[0], dict) else "").strip()
            return name
    except Exception:
        pass
    return ""


def _load_dongle_assignment_for_system(system_name: str) -> str:
    """Read persisted dongle assignment file for a specific system's preferred tuner."""
    if not system_name or not DONGLE_ASSIGNMENTS_PATH:
        return ""
    try:
        if not os.path.isfile(DONGLE_ASSIGNMENTS_PATH):
            return ""
        with open(DONGLE_ASSIGNMENTS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for entry in data.get("assignments") or []:
            if str(entry.get("system_name") or "").strip().lower() == system_name.strip().lower():
                return str(entry.get("preferred_tuner_serial") or "").strip()
    except Exception:
        logger.debug("Failed reading dongle assignment for system %s", system_name, exc_info=True)
    return ""


def _preferred_tuner_target(system_name: str = "") -> str:
    """Return the preferred tuner serial, checking allocator assignments first."""
    # --- Allocator-aware path: per-system assignment ---
    if system_name:
        assigned = _load_dongle_assignment_for_system(system_name)
        if assigned:
            return assigned

    # --- Legacy global fallback ---
    if DIGITAL_PREFERRED_TUNER:
        return DIGITAL_PREFERRED_TUNER
    if DIGITAL_RTL_SERIAL:
        return DIGITAL_RTL_SERIAL
    if (
        any(
            str(candidate or "").strip()
            for candidate in (DIGITAL_RTL_SERIAL_SECONDARY, DIGITAL_RTL_SERIAL_TERTIARY)
        )
        and not DIGITAL_FORCE_PREFERRED_TUNER
    ):
        return ""
    if DIGITAL_RTL_DEVICE and not DIGITAL_RTL_DEVICE.isdigit():
        return DIGITAL_RTL_DEVICE
    return ""


def _sync_source_configuration(
    source_conf: ET.Element,
    control_channels_hz: list[int],
    *,
    system_name: str = "",
) -> dict[str, object]:
    """Write source configuration — always single-frequency TUNER mode."""
    source_conf.set("type", "sourceConfigTuner")
    source_conf.set("source_type", "TUNER")
    source_conf.set("frequency", str(control_channels_hz[0]))
    if "frequency_rotation_delay" in source_conf.attrib:
        del source_conf.attrib["frequency_rotation_delay"]
    for child in list(source_conf):
        if child.tag == "frequency":
            source_conf.remove(child)

    preferred_tuner = _preferred_tuner_target(system_name=system_name)
    if preferred_tuner:
        source_conf.set("preferred_tuner", preferred_tuner)
    elif "preferred_tuner" in source_conf.attrib:
        del source_conf.attrib["preferred_tuner"]

    return {
        "source_mode": "single",
        "control_count": len(control_channels_hz),
        "control_hz": int(control_channels_hz[0]),
        "preferred_tuner": preferred_tuner,
    }


def _sync_alias_broadcast_channels(root: ET.Element, alias_list_name: str) -> int:
    stream_name = str(DIGITAL_SDRTRUNK_STREAM_NAME or "").strip()
    if not alias_list_name or not stream_name:
        return 0

    # SDRTrunk validates all alias IDs at load time, not just the active list.
    # Normalize malformed stream bindings globally before any targeted updates.
    _normalize_alias_list_stream_bindings(root, "", stream_name)
    if not DIGITAL_ATTACH_BROADCAST_CHANNEL:
        return 0

    _normalize_alias_list_stream_bindings(root, alias_list_name, stream_name)
    added = 0
    for alias in root.findall("alias"):
        if str(alias.get("list", "")).strip() != alias_list_name:
            continue

        has_talkgroup_id = False
        has_stream_binding = False
        for alias_id in alias.findall("id"):
            _normalize_alias_stream_binding(alias_id, stream_name)
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


def _normalize_alias_stream_binding(alias_id: ET.Element, stream_name: str) -> bool:
    stream = str(stream_name or "").strip()
    if not stream:
        return False
    raw_type = str(alias_id.get("type", "")).strip()
    if not raw_type:
        return False
    # Some playlist exports have emitted malformed stream bindings such as
    # type="tDIGITAL". Normalize those back to broadcastChannel.
    if raw_type.lower() == f"t{stream.lower()}":
        alias_id.set("type", "broadcastChannel")
        if str(alias_id.get("channel", "")).strip() != stream:
            alias_id.set("channel", stream)
        return True
    return False


def _normalize_alias_list_stream_bindings(root: ET.Element, alias_list_name: str, stream_name: str) -> int:
    if not stream_name:
        return 0
    alias_filter = str(alias_list_name or "").strip()
    updates = 0
    for alias in root.findall("alias"):
        if alias_filter and str(alias.get("list", "")).strip() != alias_filter:
            continue
        for alias_id in alias.findall("id"):
            if _normalize_alias_stream_binding(alias_id, stream_name):
                updates += 1
    return updates


def _profile_alias_seed_rows(profile_dir: Path) -> list[tuple[str, str, str]]:
    candidates = (profile_dir / "talkgroups_with_group.csv", profile_dir / "talkgroups.csv")
    sources = [candidate for candidate in candidates if candidate.is_file()]
    if not sources:
        return []

    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    try:
        # Merge grouped and plain CSV exports. Grouped files usually carry
        # better labeling, while plain exports may include newer TGIDs.
        for source in sources:
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
        logger.debug("Failed seeding alias rows from %s", profile_dir, exc_info=True)
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

    desired_tgids = {str(dec).strip() for dec, _name, _group in seed_rows if str(dec).strip().isdigit()}
    for alias in list(root.findall("alias")):
        if str(alias.get("list", "")).strip() != alias_list_name:
            continue
        alias_tgids = []
        for alias_id in alias.findall("id"):
            token = _alias_talkgroup_value(alias_id)
            if token:
                alias_tgids.append(token)
        if alias_tgids and not any(token in desired_tgids for token in alias_tgids):
            try:
                root.remove(alias)
            except Exception:
                logger.debug("Failed pruning stale alias from list %s", alias_list_name, exc_info=True)
                continue

    existing = _collect_alias_talkgroup_map(root, alias_list_name)
    stream_name = str(DIGITAL_SDRTRUNK_STREAM_NAME or "").strip()
    _normalize_alias_list_stream_bindings(root, alias_list_name, stream_name)
    added = 0
    for dec, name, group in seed_rows:
        alias = existing.get(dec)
        if alias is not None:
            if name:
                alias.set("name", name)
            if group:
                alias.set("group", group)
            if DIGITAL_ATTACH_BROADCAST_CHANNEL and stream_name:
                has_stream_binding = any(
                    str(alias_id.get("type", "")).strip().lower() == "broadcastchannel"
                    and str(alias_id.get("channel", "")).strip() == stream_name
                    for alias_id in alias.findall("id")
                )
                if not has_stream_binding:
                    ET.SubElement(
                        alias,
                        "id",
                        {
                            "type": "broadcastChannel",
                            "channel": stream_name,
                        },
                    )
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
        decode_conf.set(
            "modulation",
            DIGITAL_P25_MODULATION
            or str(decode_conf.get("modulation", "")).strip()
            or "C4FM",
        )
    decode_conf.set("traffic_channel_pool_size", "20")
    decode_conf.set("ignore_data_calls", "true" if DIGITAL_IGNORE_DATA_CALLS else "false")


def _sync_stream_configuration(root: ET.Element) -> bool:
    stream_name = str(DIGITAL_SDRTRUNK_STREAM_NAME or "").strip()
    if not stream_name:
        return False

    mount_point = f"/{DIGITAL_STREAM_MOUNT}"
    stream = None
    duplicates: list[ET.Element] = []
    for candidate in list(root.findall("stream")):
        name = str(candidate.get("name", "")).strip()
        mount = str(candidate.get("mount_point", "")).strip()
        if name == stream_name or mount == mount_point:
            if stream is None:
                stream = candidate
            else:
                duplicates.append(candidate)

    changed = False
    created_stream = False
    if stream is None:
        stream = ET.SubElement(root, "stream")
        changed = True
        created_stream = True

    for dup in duplicates:
        try:
            root.remove(dup)
            changed = True
        except Exception:
            logger.debug("Failed removing duplicate stream entry %s", stream_name, exc_info=True)

    attrs = {
        "type": "icecastHTTPConfiguration",
        f"{{{_XSI_NS}}}type": "ICECAST_HTTP",
        "public": "false",
        "user_name": ICECAST_SOURCE_USER,
        "mount_point": mount_point,
        "inline": "true",
        "host": ICECAST_HOST,
        "name": stream_name,
        "enabled": "true",
        "port": ICECAST_PORT,
        "password": ICECAST_SOURCE_PASSWORD,
    }
    try:
        existing_sample_rate = int(str(stream.get("sample_rate", "")).strip())
    except Exception:
        existing_sample_rate = 0
    try:
        existing_bitrate = int(str(stream.get("bitrate", "")).strip())
    except Exception:
        existing_bitrate = 0
    if (
        created_stream
        or DIGITAL_STREAM_SAMPLE_RATE_OVERRIDE
        or existing_sample_rate < DIGITAL_STREAM_SAMPLE_RATE
    ):
        attrs["sample_rate"] = str(DIGITAL_STREAM_SAMPLE_RATE)
    if created_stream or DIGITAL_STREAM_CHANNELS_OVERRIDE or not str(stream.get("channels", "")).strip():
        attrs["channels"] = str(DIGITAL_STREAM_CHANNELS)
    if (
        created_stream
        or DIGITAL_STREAM_BITRATE_OVERRIDE
        or existing_bitrate < DIGITAL_STREAM_BITRATE
        or existing_bitrate == DIGITAL_STREAM_LEGACY_BITRATE
    ):
        attrs["bitrate"] = str(DIGITAL_STREAM_BITRATE)
    if created_stream or DIGITAL_STREAM_DELAY_OVERRIDE or not str(stream.get("delay", "")).strip():
        attrs["delay"] = str(DIGITAL_STREAM_DELAY_MS)
    if (
        created_stream
        or DIGITAL_STREAM_MAX_RECORDING_AGE_OVERRIDE
        or not str(stream.get("maximum_recording_age", "")).strip()
    ):
        attrs["maximum_recording_age"] = str(DIGITAL_STREAM_MAX_RECORDING_AGE_MS)
    for key, value in attrs.items():
        if str(stream.get(key, "")) != str(value):
            stream.set(key, str(value))
            changed = True

    fmt = stream.find("format")
    if fmt is None:
        fmt = ET.SubElement(stream, "format")
        changed = True
    if str(fmt.text or "").strip().upper() != "MP3":
        fmt.text = "MP3"
        changed = True

    return changed


def _sync_playlist(profile_dir: Path, control_channels_hz: list[int]) -> dict[str, object]:
    PLAYLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tree = _load_playlist(PLAYLIST_PATH)
    root = tree.getroot()
    profile_id = profile_dir.name
    primary_system_name = _read_primary_system_name(profile_dir)

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
    source_state = _sync_source_configuration(source_conf, control_channels_hz, system_name=primary_system_name)

    _sync_decode_configuration(channel)

    _ensure_child(channel, "record_configuration")
    stream_updated = _sync_stream_configuration(root)

    tree.write(PLAYLIST_PATH, encoding="utf-8", xml_declaration=False)
    source_state["seeded_aliases"] = seeded_aliases
    source_state["stream_alias_updates"] = stream_alias_updates
    source_state["stream_name"] = DIGITAL_SDRTRUNK_STREAM_NAME
    source_state["stream_mount"] = DIGITAL_STREAM_MOUNT
    source_state["stream_config_updated"] = stream_updated
    return source_state


def main() -> int:
    try:
        target = _choose_profile()
        _point_active_link(target)
        tuner_state = _sync_tuner_configuration()
        local_audio_pref_state = _sync_local_audio_playback_preference()
        digital_uids = tuner_state.get("digital_uids") or []
        if DIGITAL_REQUIRE_TUNER and not digital_uids:
            available_serials = ",".join(tuner_state.get("available_rtl_serials") or []) or "none"
            available_uids = ",".join(tuner_state.get("available_rtl_uids") or []) or "none"
            raise RuntimeError(
                "no digital RTL tuner detected "
                f"(configured={','.join([s for s in (DIGITAL_RTL_SERIAL, DIGITAL_RTL_SERIAL_SECONDARY, DIGITAL_RTL_SERIAL_TERTIARY) if s]) or 'none'} "
                f"available_serials={available_serials} available_uids={available_uids})"
            )
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
            f"digital_tertiary={DIGITAL_RTL_SERIAL_TERTIARY or 'unset'} "
            f"digital_auto_extra={','.join(tuner_state.get('digital_auto_extra_serials', [])) or 'none'} "
            f"tuner_config_updated={bool(tuner_state.get('updated'))} "
            f"tuner_config_reason={tuner_state.get('reason') or 'unknown'} "
            f"local_audio_pref_updated={bool(local_audio_pref_state.get('updated'))} "
            f"local_audio_pref_reason={local_audio_pref_state.get('reason') or 'unknown'} "
            f"local_audio_pref_mixer={local_audio_pref_state.get('mixer') or 'none'} "
            f"digital_uid_source={tuner_state.get('digital_uid_source') or 'unknown'} "
            f"digital_uids={','.join(tuner_state.get('digital_uids', [])) or 'none'} "
            f"analog_uids={','.join(tuner_state.get('analog_uids', [])) or 'none'}"
        )
        return 0
    except Exception as e:
        _log(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
