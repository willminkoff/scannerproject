"""Digital backend integration (live-only, in-memory metadata)."""
from __future__ import annotations

import json
import csv
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from xml.etree import ElementTree as ET

_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("xsi", _XSI_NS)

try:
    from .config import (
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_BACKEND,
        DIGITAL_EVENT_LOG_DIR,
        DIGITAL_EVENT_LOG_MODE,
        DIGITAL_EVENT_LOG_TAIL_LINES,
        DIGITAL_FORCE_PREFERRED_TUNER,
        DIGITAL_STREAM_MOUNT,
        ICECAST_HOST,
        ICECAST_PORT,
        DIGITAL_LOG_PATH,
        DIGITAL_PLAYLIST_PATH,
        DIGITAL_PROFILES_DIR,
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_DEVICE,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_HINT,
        DIGITAL_SCHEDULER_STATE_PATH,
        DIGITAL_SDRTRUNK_STREAM_NAME,
        DIGITAL_ATTACH_BROADCAST_CHANNEL,
        DIGITAL_IGNORE_DATA_CALLS,
        DIGITAL_PAUSE_ON_HIT,
        DIGITAL_SCAN_MODE,
        DIGITAL_SOURCE_ROTATION_DELAY_MS,
        DIGITAL_SERVICE_NAME,
        DIGITAL_SYSTEM_DWELL_MS,
        DIGITAL_SYSTEM_HANG_MS,
        DIGITAL_SYSTEM_ORDER,
        DIGITAL_USE_MULTI_FREQ_SOURCE,
    )
    from .systemd import unit_active
except ImportError:
    from ui.config import (
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_BACKEND,
        DIGITAL_EVENT_LOG_DIR,
        DIGITAL_EVENT_LOG_MODE,
        DIGITAL_EVENT_LOG_TAIL_LINES,
        DIGITAL_FORCE_PREFERRED_TUNER,
        DIGITAL_STREAM_MOUNT,
        ICECAST_HOST,
        ICECAST_PORT,
        DIGITAL_LOG_PATH,
        DIGITAL_PLAYLIST_PATH,
        DIGITAL_PROFILES_DIR,
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_DEVICE,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_HINT,
        DIGITAL_SCHEDULER_STATE_PATH,
        DIGITAL_SDRTRUNK_STREAM_NAME,
        DIGITAL_ATTACH_BROADCAST_CHANNEL,
        DIGITAL_IGNORE_DATA_CALLS,
        DIGITAL_PAUSE_ON_HIT,
        DIGITAL_SCAN_MODE,
        DIGITAL_SOURCE_ROTATION_DELAY_MS,
        DIGITAL_SERVICE_NAME,
        DIGITAL_SYSTEM_DWELL_MS,
        DIGITAL_SYSTEM_HANG_MS,
        DIGITAL_SYSTEM_ORDER,
        DIGITAL_USE_MULTI_FREQ_SOURCE,
    )
    from ui.systemd import unit_active


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_MODE_RE = re.compile(r"\b(P25|P25P1|P25P2|DMR|NXDN|D-STAR|TETRA|YSF|EDACS|LTR)\b", re.I)
_PHASE1_RE = re.compile(r"\bP25\s*Phase\s*1\b", re.I)
_PHASE2_RE = re.compile(r"\bP25\s*Phase\s*2\b", re.I)
_P25_HINT_RE = re.compile(r"\b(P25|PROJECT\s*25|APCO\s*25)\b", re.I)
_DMR_HINT_RE = re.compile(r"\b(DMR|MOTOTRBO|CAP\+|CAPACITY\s*PLUS|CONNECT\s*PLUS|TIER\s*III)\b", re.I)
_NXDN_HINT_RE = re.compile(r"\b(NXDN|NEXEDGE|IDAS)\b", re.I)
_AUTO_ALPHA_RE = re.compile(r"^auto\s+\d+$", re.I)
_LABEL_RE = re.compile(
    r"\b(label|alias|alpha\s*tag|talkgroup|tgid|channel|channel\s*name|alias\s*name|group)[=:]\s*([^|,]+)",
    re.I,
)
_TGID_RE = re.compile(r"\b(?:tgid|talkgroup|tg)\b\s*[:=#-]?\s*\(?\s*(\d+)\s*\)?", re.I)
_EVENT_HINT_RE = re.compile(r"(call|voice|traffic|talkgroup|tgid|alias|alpha\s*tag|channel\s*event|from:|to:)", re.I)
_TS_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})")
_TS_COMPACT_RE = re.compile(r"(?P<date>\d{8})\s+(?P<time>\d{6})(?:\.\d+)?")
_LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+")
_LOG_PREFIX_COMPACT_RE = re.compile(r"^\d{8}\s+\d{6}(?:\.\d+)?\s+")
_LOG_LEVEL_RE = re.compile(r"^(INFO|WARN|ERROR|DEBUG|TRACE)\s+", re.I)
_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_NON_FATAL_ERROR_RE = re.compile(
    r"(no audio playback devices available|couldn't obtain master gain|usb.*in-use|device is busy|"
    r"unable to set usb configuration|mMainGui\" is null|error while broadcasting.*tunerevent)",
    re.I,
)
_SUPPRESS_STATUS_WARNING_RE = re.compile(
    r"(mMainGui\" is null|error while broadcasting.*tunerevent)",
    re.I,
)
_TUNER_BUSY_RE = re.compile(
    r"(in[- ]use by another application|device is busy|usb_claim_interface error|"
    r"unable to set usb configuration|failed to open rtlsdr device)",
    re.I,
)
_IGNORE_EVENT_RE = re.compile(
    r"(auto-start failed|no tuner available|mountpoint in use|unable to connect|audiooutput|playbackpreference|"
    r"audio streaming broadcaster|status: connected|starting main application|loading playlist|discovering tuners|"
    r"ensure-digital-runtime)",
    re.I,
)
_JAVA_STACK_FRAME_RE = re.compile(r"^\s*at\s+[A-Za-z0-9_.$<>]+\([^)]*\)\s*$")
_JAVA_STACK_OMIT_RE = re.compile(r"^\s*\.\.\.\s+\d+\s+more\s*$", re.I)
_MUTE_STATE_PATH = "/run/airband_ui_digital_mute.json"
_DIGITAL_MUTED = False
_DEFAULT_PROFILE_NOTE = (
    "This is a placeholder SDRTrunk profile directory.\n"
    "Export or copy your SDRTrunk configuration into this folder.\n"
    "Then set this profile active from the UI or by updating the active symlink.\n"
)
_LISTEN_FILENAME = "talkgroups_listen.json"
_DEFAULT_LISTEN_ENABLED = os.getenv(
    "DIGITAL_LISTEN_DEFAULT",
    "0",
).strip().lower() in ("1", "true", "yes", "on")
_REJECT_SCAN_MAX_LINES = max(200, int(os.getenv("DIGITAL_REJECT_SCAN_MAX_LINES", "5000")))
_REJECT_SCAN_MAX_BYTES = max(16384, int(os.getenv("DIGITAL_REJECT_SCAN_MAX_BYTES", "1048576")))
_REJECT_SCAN_MAX_FILES = max(1, int(os.getenv("DIGITAL_REJECT_SCAN_MAX_FILES", "3")))
_REJECT_GRANT_RE = re.compile(r"channel start rejected", re.I)
_EVENT_HEADER_KEYS = (
    "timestamp",
    "time",
    "date",
    "talkgroup",
    "tgid",
    "alias",
    "alpha",
    "system",
    "site",
    "frequency",
    "freq",
)
_EVENT_LABEL_KEYS = (
    "alias",
    "alpha tag",
    "alpha",
    "talkgroup name",
    "group",
    "description",
    "name",
    "channel name",
)
_EVENT_TGID_KEYS = (
    "tgid",
    "talkgroup",
    "talkgroup id",
    "dec",
    "decimal",
    "tg",
)
_EVENT_MODE_KEYS = (
    "mode",
    "protocol",
    "type",
    "system type",
    "decoder",
)
_EVENT_KIND_KEYS = (
    "event",
    "event type",
    "event_name",
)
_EVENT_TIME_KEYS = (
    "timestamp",
    "time",
    "start time",
    "event time",
    "date time",
    "start",
    "received",
)
_EVENT_DATE_KEYS = (
    "date",
    "event date",
)
_EVENT_TIME_ONLY_KEYS = (
    "time",
    "start time",
    "event time",
)
_EVENT_ID_KEYS = (
    "event id",
    "event_id",
    "id",
)
_EVENT_DURATION_KEYS = (
    "duration_ms",
    "duration",
    "duration ms",
)
_EVENT_DETAILS_KEYS = (
    "details",
    "detail",
    "message",
    "status",
)
_EVENT_FREQ_KEYS = (
    "frequency",
    "freq",
    "control channel",
    "control frequency",
)
_EVENT_SITE_KEYS = (
    "site",
    "system",
    "site name",
    "system name",
)
_DIGITAL_HIT_MIN_DURATION_MS = int(os.getenv("DIGITAL_HIT_MIN_DURATION_MS", "250"))
_DIGITAL_STATUS_CLEAR_MS = max(0, int(os.getenv("DIGITAL_STATUS_CLEAR_MS", "180000")))
_DIGITAL_TGID_MAX = max(1, int(os.getenv("DIGITAL_TGID_MAX", "16777215")))
_DIGITAL_EVENT_MIN_DATA_BYTES = max(128, int(os.getenv("DIGITAL_EVENT_MIN_DATA_BYTES", "128")))
_DIGITAL_EVENT_SCAN_MAX_FILES = max(50, int(os.getenv("DIGITAL_EVENT_SCAN_MAX_FILES", "2000")))
_DIGITAL_TUNER_BUSY_WINDOW_MS = max(30000, int(os.getenv("DIGITAL_TUNER_BUSY_WINDOW_MS", "180000")))
_DIGITAL_CONTROL_WINDOW_MS = max(30000, int(os.getenv("DIGITAL_CONTROL_WINDOW_MS", "120000")))
_DIGITAL_CONTROL_TAIL_LINES = max(80, int(os.getenv("DIGITAL_CONTROL_TAIL_LINES", "300")))
_DIGITAL_CONTROL_TAIL_BYTES = max(16384, int(os.getenv("DIGITAL_CONTROL_TAIL_BYTES", "131072")))
_DIGITAL_SCHEDULER_APPLY_MIN_INTERVAL_MS = max(
    250,
    int(os.getenv("DIGITAL_SCHEDULER_APPLY_MIN_INTERVAL_MS", "1000")),
)
_DIGITAL_SCHEDULER_LOCK_LOSS_MS = max(
    2000,
    int(os.getenv("DIGITAL_SCHEDULER_LOCK_LOSS_MS", "2500")),
)
_DIGITAL_SCHEDULER_TICK_SEC = max(
    0.25,
    float(os.getenv("DIGITAL_SCHEDULER_TICK_SEC", "1.0")),
)
_CONTROL_MESSAGE_RE = re.compile(
    r"\b(TSBK|PDU|RFSS_STATUS_BCST|SEC_CCH_BROADCST|IDEN_UPDATE|TDMA_SYNC_BCST|"
    r"SNDCP_DCH_|GRP_VCH_GRANT|UU_VCH_GRANT|GROUP VOICE CHANNEL UPDATE)\b",
    re.I,
)
_SYNC_LOSS_RE = re.compile(r"\bSYNC LOSS\b", re.I)
_CONTROL_LOCK_FAIL_RE = re.compile(
    r"(can't get a lock|cannot get a lock|could not get a lock|failed to lock|"
    r"unable to lock|control channel.*(not lock|unlock|no lock)|searching for control channel)",
    re.I,
)
_DIGITAL_EVENT_DROP_RE = re.compile(
    r"(rejected|tuner unavailable|encrypted|encryption|data channel grant|nsapi)",
    re.I,
)
_DIGITAL_NON_AUDIO_LABEL_RE = re.compile(r"^\(P:\d+\s*\[\d+\]\)$", re.I)
_DIGITAL_DEBUG_INCLUDE_GRANTS = os.getenv(
    "DIGITAL_DEBUG_INCLUDE_GRANTS",
    "0",
).strip().lower() in ("1", "true", "yes", "on")
_DIGITAL_SOURCE_ROTATION_DELAY_MS = max(100, int(DIGITAL_SOURCE_ROTATION_DELAY_MS or 500))
_DIGITAL_STREAM_SOURCE_USER = os.getenv("ICECAST_SOURCE_USER", "source").strip() or "source"
_DIGITAL_STREAM_SOURCE_PASSWORD = os.getenv("ICECAST_SOURCE_PASSWORD", "062352").strip() or "062352"
_DIGITAL_STREAM_BITRATE = max(8, int(os.getenv("DIGITAL_STREAM_BITRATE", "32")))
_DIGITAL_STREAM_SAMPLE_RATE = max(8000, int(os.getenv("DIGITAL_STREAM_SAMPLE_RATE", "16000")))
_DIGITAL_STREAM_CHANNELS = 1 if int(os.getenv("DIGITAL_STREAM_CHANNELS", "1")) <= 1 else 2
_DIGITAL_STREAM_MAX_RECORDING_AGE_MS = max(
    60000,
    int(os.getenv("DIGITAL_STREAM_MAX_RECORDING_AGE_MS", "600000")),
)
_DIGITAL_STREAM_DELAY_MS = max(0, int(os.getenv("DIGITAL_STREAM_DELAY_MS", "0")))
_DIGITAL_STREAM_BITRATE_OVERRIDE = os.getenv("DIGITAL_STREAM_BITRATE", "").strip() != ""
_DIGITAL_STREAM_SAMPLE_RATE_OVERRIDE = os.getenv("DIGITAL_STREAM_SAMPLE_RATE", "").strip() != ""
_DIGITAL_STREAM_CHANNELS_OVERRIDE = os.getenv("DIGITAL_STREAM_CHANNELS", "").strip() != ""
_DIGITAL_STREAM_MAX_RECORDING_AGE_OVERRIDE = os.getenv("DIGITAL_STREAM_MAX_RECORDING_AGE_MS", "").strip() != ""
_DIGITAL_STREAM_DELAY_OVERRIDE = os.getenv("DIGITAL_STREAM_DELAY_MS", "").strip() != ""
_DURATION_HMS_RE = re.compile(
    r"^(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2}(?:\.\d+)?)$"
)


def validate_digital_profile_id(profile_id: str) -> bool:
    """Strict validation for digital profile IDs."""
    if not profile_id:
        return False
    return bool(_NAME_RE.match(profile_id))


def validate_digital_service_name(service_name: str) -> bool:
    """Strict validation for digital service name."""
    if not service_name:
        return False
    return bool(_NAME_RE.match(service_name))


def _normalize_name(value: str) -> str:
    if not value:
        return ""
    return value.strip()


def _norm_key(value: str) -> str:
    return _KEY_NORMALIZE_RE.sub(" ", str(value or "").lower()).strip()


def _safe_realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def _profile_decoder_mode(profile_dir: str) -> tuple[str, str]:
    """Resolve decoder mode for a profile: P25 (default), DMR, or NXDN."""
    system_path = os.path.join(profile_dir, "system.json")
    if not os.path.isfile(system_path):
        return "P25", "default"
    try:
        with open(system_path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
    except Exception:
        return "P25", "default"
    if not isinstance(data, dict):
        return "P25", "default"

    for key in ("decoder", "protocol", "mode"):
        value = str(data.get(key) or "").strip()
        if not value:
            continue
        if _NXDN_HINT_RE.search(value):
            return "NXDN", f"system.json:{key}"
        if _DMR_HINT_RE.search(value):
            return "DMR", f"system.json:{key}"
        if _P25_HINT_RE.search(value):
            return "P25", f"system.json:{key}"

    hint = " ".join(
        str(data.get(key) or "")
        for key in ("system_type", "system_name", "note")
    )
    if _NXDN_HINT_RE.search(hint):
        return "NXDN", "system.json:system_type/system_name/note"
    if _DMR_HINT_RE.search(hint):
        return "DMR", "system.json:system_type/system_name/note"
    return "P25", "default"


def _apply_decode_configuration(channel: ET.Element, decoder_mode: str) -> None:
    decode_conf = channel.find("decode_configuration")
    target_type = "decodeConfigP25Phase1"
    if decoder_mode == "DMR":
        target_type = "decodeConfigDMR"
    ignore_data_calls_val = "true" if DIGITAL_IGNORE_DATA_CALLS else "false"

    if decode_conf is None or str(decode_conf.get("type", "")).strip() != target_type:
        if decode_conf is not None:
            channel.remove(decode_conf)
        decode_conf = ET.SubElement(channel, "decode_configuration")

    decode_conf.attrib.clear()
    decode_conf.set("type", target_type)

    if decoder_mode == "DMR":
        decode_conf.set("traffic_channel_pool_size", "20")
        decode_conf.set("ignore_data_calls", ignore_data_calls_val)
        decode_conf.set("ignore_crc", "false")
        decode_conf.set("use_compressed_talkgroups", "false")
        return

    decode_conf.set("modulation", "C4FM")
    decode_conf.set("traffic_channel_pool_size", "20")
    decode_conf.set("ignore_data_calls", ignore_data_calls_val)


def _sync_stream_configuration(root: ET.Element) -> bool:
    stream_name = str(DIGITAL_SDRTRUNK_STREAM_NAME or "").strip()
    if not stream_name:
        return False

    mount = str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/") or "DIGITAL.mp3"
    mount_point = f"/{mount}"
    stream = None
    duplicates: list[ET.Element] = []
    for candidate in list(root.findall("stream")):
        name = str(candidate.get("name", "")).strip()
        candidate_mount = str(candidate.get("mount_point", "")).strip()
        if name == stream_name or candidate_mount == mount_point:
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
            pass

    attrs = {
        "type": "icecastHTTPConfiguration",
        f"{{{_XSI_NS}}}type": "ICECAST_HTTP",
        "public": "false",
        "user_name": _DIGITAL_STREAM_SOURCE_USER,
        "mount_point": mount_point,
        "inline": "true",
        "host": str(ICECAST_HOST or "127.0.0.1"),
        "name": stream_name,
        "enabled": "true",
        "port": str(ICECAST_PORT or 8000),
        "password": _DIGITAL_STREAM_SOURCE_PASSWORD,
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
        or _DIGITAL_STREAM_SAMPLE_RATE_OVERRIDE
        or existing_sample_rate < _DIGITAL_STREAM_SAMPLE_RATE
    ):
        attrs["sample_rate"] = str(_DIGITAL_STREAM_SAMPLE_RATE)
    if created_stream or _DIGITAL_STREAM_CHANNELS_OVERRIDE or not str(stream.get("channels", "")).strip():
        attrs["channels"] = str(_DIGITAL_STREAM_CHANNELS)
    if created_stream or _DIGITAL_STREAM_BITRATE_OVERRIDE or existing_bitrate < _DIGITAL_STREAM_BITRATE:
        attrs["bitrate"] = str(_DIGITAL_STREAM_BITRATE)
    if created_stream or _DIGITAL_STREAM_DELAY_OVERRIDE or not str(stream.get("delay", "")).strip():
        attrs["delay"] = str(_DIGITAL_STREAM_DELAY_MS)
    if (
        created_stream
        or _DIGITAL_STREAM_MAX_RECORDING_AGE_OVERRIDE
        or not str(stream.get("maximum_recording_age", "")).strip()
    ):
        attrs["maximum_recording_age"] = str(_DIGITAL_STREAM_MAX_RECORDING_AGE_MS)
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


def _digital_tuner_targets() -> list[str]:
    targets: list[str] = []
    for candidate in (
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_DEVICE,
    ):
        value = str(candidate or "").strip()
        if value and value not in targets:
            targets.append(value)
    return targets


def _preferred_tuner_target() -> str:
    # In dual-dongle digital mode, avoid pinning to one tuner so SDRTrunk can
    # split control and traffic across both enabled digital tuners.
    if DIGITAL_RTL_SERIAL_SECONDARY and not DIGITAL_FORCE_PREFERRED_TUNER:
        return ""
    if DIGITAL_PREFERRED_TUNER:
        return DIGITAL_PREFERRED_TUNER
    if DIGITAL_RTL_SERIAL:
        return DIGITAL_RTL_SERIAL
    if DIGITAL_RTL_DEVICE and not str(DIGITAL_RTL_DEVICE).isdigit():
        return str(DIGITAL_RTL_DEVICE).strip()
    return ""


def _sync_source_configuration(source_conf: ET.Element, control_channels: list[int]) -> dict:
    use_multi = DIGITAL_USE_MULTI_FREQ_SOURCE and len(control_channels) > 1
    if use_multi:
        source_conf.set("type", "sourceConfigTunerMultipleFrequency")
        source_conf.set("source_type", "TUNER_MULTIPLE_FREQUENCIES")
        source_conf.set("frequency_rotation_delay", str(_DIGITAL_SOURCE_ROTATION_DELAY_MS))
        if "frequency" in source_conf.attrib:
            del source_conf.attrib["frequency"]
        for child in list(source_conf):
            if child.tag == "frequency":
                source_conf.remove(child)
        for hz in control_channels:
            child = ET.SubElement(source_conf, "frequency")
            child.text = str(hz)
    else:
        source_conf.set("type", "sourceConfigTuner")
        source_conf.set("source_type", "TUNER")
        source_conf.set("frequency", str(control_channels[0]))
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
        "source_type": source_conf.get("source_type", ""),
        "source_config_type": source_conf.get("type", ""),
        "control_count": len(control_channels),
        "control_hz": int(control_channels[0]),
        "preferred_tuner": preferred_tuner,
        "tuner_targets": _digital_tuner_targets(),
    }


def _ensure_alias_broadcast_channel(root: ET.Element, alias_list_name: str) -> int:
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


def _read_profile_alias_seed_rows(profile_dir: str) -> list[tuple[str, str, str]]:
    if not profile_dir:
        return []

    candidates = ("talkgroups.csv", "talkgroups_with_group.csv")
    path = ""
    for name in candidates:
        candidate = os.path.join(profile_dir, name)
        if os.path.isfile(candidate):
            path = candidate
            break
    if not path:
        return []

    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
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


def _seed_alias_list_from_profile(root: ET.Element, alias_list_name: str, profile_dir: str) -> int:
    if not alias_list_name or not profile_dir:
        return 0

    seed_rows = _read_profile_alias_seed_rows(profile_dir)
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


def _read_tail_lines(path: str, max_bytes: int = 8192, max_lines: int = 120):
    try:
        size = os.path.getsize(path)
    except Exception:
        return []
    start = max(size - max_bytes, 0)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(max_bytes)
    except Exception:
        return []
    text = data.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


def _parse_time_ms(line: str, fallback_ms: int) -> int:
    text = str(line or "").strip()
    # Prefer leading timestamps and avoid matching date strings that appear
    # later inside decoded-message payload text.
    m2 = _TS_COMPACT_RE.match(text)
    if m2:
        try:
            dt = datetime.strptime(f"{m2.group('date')} {m2.group('time')}", "%Y%m%d %H%M%S")
            return int(time.mktime(dt.timetuple()) * 1000)
        except Exception:
            return fallback_ms
    m = _TS_RE.match(text)
    if m:
        try:
            dt = datetime.strptime(f"{m.group('date')} {m.group('time')}", "%Y-%m-%d %H:%M:%S")
            return int(time.mktime(dt.timetuple()) * 1000)
        except Exception:
            return fallback_ms
    return fallback_ms


def _parse_time_value(value: str, fallback_ms: int) -> int:
    raw = str(value or "").strip()
    if not raw:
        return fallback_ms
    if raw.isdigit():
        if len(raw) >= 13:
            try:
                return int(raw[:13])
            except Exception:
                return fallback_ms
        if len(raw) == 10:
            try:
                return int(raw) * 1000
            except Exception:
                return fallback_ms
    return _parse_time_ms(raw, fallback_ms)


def _parse_duration_ms(value: str) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    m_hms = _DURATION_HMS_RE.fullmatch(raw)
    if m_hms:
        try:
            hours = int(m_hms.group("h") or 0)
            minutes = int(m_hms.group("m") or 0)
            seconds = float(m_hms.group("s") or 0)
            return int(round(((hours * 3600) + (minutes * 60) + seconds) * 1000))
        except Exception:
            return None

    token = raw.lower()
    m_num = re.search(r"\d+(?:\.\d+)?", token)
    if not m_num:
        return None
    try:
        num = float(m_num.group(0))
    except Exception:
        return None
    if num < 0:
        return None

    if re.search(r"\b(ms|msec|millisecond|milliseconds)\b", token):
        return int(round(num))
    if re.search(r"\b(s|sec|secs|second|seconds)\b", token):
        return int(round(num * 1000))

    # Bare numeric durations in SDRTrunk logs are typically milliseconds.
    # Treat very small bare values as seconds to avoid dropping real calls.
    if num < 50:
        return int(round(num * 1000))
    return int(round(num))


def _strip_log_prefix(line: str) -> str:
    line = (line or "").strip()
    if not line:
        return ""
    line = _LOG_PREFIX_RE.sub("", line)
    line = _LOG_PREFIX_COMPACT_RE.sub("", line)
    line = re.sub(r"^\[[^\]]+\]\s+", "", line)
    line = _LOG_LEVEL_RE.sub("", line)
    if " - " in line:
        left, right = line.split(" - ", 1)
        if "." in left or left.isupper():
            line = right
    return line.strip()


def _extract_label_mode(line: str):
    line = _strip_log_prefix(line or "")
    if not line:
        return "", "", False
    label = ""
    mode = ""
    label_from_field = False
    m_label = _LABEL_RE.search(line)
    if m_label:
        label = m_label.group(2).strip().strip('"')
        label_from_field = True
    m_tg = _TGID_RE.search(line)
    if not label and m_tg:
        label = f"TG {m_tg.group(1)}"
        label_from_field = True
    if _PHASE2_RE.search(line):
        mode = "P25P2"
    elif _PHASE1_RE.search(line):
        mode = "P25P1"
    else:
        m_mode = _MODE_RE.search(line)
        if m_mode:
            mode = m_mode.group(1).upper()
    if not label:
        label = line
    return label, mode, label_from_field


def _coerce_mode(value: str) -> str:
    if not value:
        return ""
    _, mode, _ = _extract_label_mode(str(value))
    return mode or ""


def _row_value(row: dict, keys: tuple) -> str:
    for key in keys:
        val = row.get(key)
        if val:
            return str(val).strip()
    return ""


def _parse_listen_payload(payload: object) -> tuple[dict[str, bool], bool, dict[str, dict]]:
    """Parse listen payloads across legacy and current schemas.

    Supported formats:
    - {"items": {"47152": true}, "default_listen": false}
    - {"talkgroups": {"47152": {"listen": true, ...}}, "default_listen": false}
    - {"talkgroups": {"47152": true}, ...}
    """
    mapping: dict[str, bool] = {}
    metadata: dict[str, dict] = {}
    default_listen = bool(_DEFAULT_LISTEN_ENABLED)
    if not isinstance(payload, dict):
        return mapping, default_listen, metadata

    default_raw = payload.get(
        "default_listen",
        payload.get("default", bool(_DEFAULT_LISTEN_ENABLED)),
    )
    default_listen = bool(default_raw)

    items = payload.get("items")
    if isinstance(items, dict):
        for key, value in items.items():
            dec = _normalize_tgid(str(key))
            if not dec:
                continue
            mapping[dec] = bool(value)
    elif isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            dec = _normalize_tgid(
                str(
                    item.get("dec")
                    or item.get("tgid")
                    or item.get("id")
                    or "",
                )
            )
            if not dec:
                continue
            mapping[dec] = bool(item.get("listen"))

    talkgroups = payload.get("talkgroups")
    if isinstance(talkgroups, dict):
        for key, value in talkgroups.items():
            dec = _normalize_tgid(str(key))
            if not dec:
                continue
            if isinstance(value, dict):
                meta = {}
                for mk, mv in value.items():
                    mks = str(mk or "").strip()
                    if not mks:
                        continue
                    if mks.lower() == "listen":
                        mapping[dec] = bool(mv)
                    else:
                        meta[mks] = mv
                if meta:
                    metadata[dec] = meta
            else:
                mapping[dec] = bool(value)

    return mapping, default_listen, metadata


def _read_listen_config(path: str) -> tuple[dict[str, bool], bool, dict[str, dict]]:
    mapping: dict[str, bool] = {}
    metadata: dict[str, dict] = {}
    default_listen = bool(_DEFAULT_LISTEN_ENABLED)
    if not path or not os.path.isfile(path):
        return mapping, default_listen, metadata
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            payload = json.load(f) or {}
        mapping, default_listen, metadata = _parse_listen_payload(payload)
    except Exception:
        mapping = {}
        metadata = {}
        default_listen = bool(_DEFAULT_LISTEN_ENABLED)
    return mapping, default_listen, metadata


def _normalize_tgid(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.isdigit():
        return ""
    try:
        dec = int(raw)
    except Exception:
        return ""
    if dec <= 0 or dec > _DIGITAL_TGID_MAX:
        return ""
    return str(dec)


def _row_to_event(row: dict, raw_line: str, fallback_ms: int) -> dict | None:
    label = _row_value(row, _EVENT_LABEL_KEYS)
    tgid = _normalize_tgid(_row_value(row, _EVENT_TGID_KEYS))
    event_id = _row_value(row, _EVENT_ID_KEYS)
    event_kind = _row_value(row, _EVENT_KIND_KEYS).lower()
    duration_raw = _row_value(row, _EVENT_DURATION_KEYS)
    details = _row_value(row, _EVENT_DETAILS_KEYS)
    mode_val = _row_value(row, _EVENT_MODE_KEYS)
    time_val = _row_value(row, _EVENT_TIME_KEYS)
    date_val = _row_value(row, _EVENT_DATE_KEYS)
    time_only = _row_value(row, _EVENT_TIME_ONLY_KEYS)
    freq = _row_value(row, _EVENT_FREQ_KEYS)
    site = _row_value(row, _EVENT_SITE_KEYS)
    to_val = _row_value(row, ("to", "from"))
    if to_val:
        if not label:
            label = to_val
        if not tgid:
            m = re.search(r"\((\d+)\)", to_val)
            if not m:
                m = re.search(r"\b(\d{3,})\b", to_val)
            if m:
                tgid = _normalize_tgid(m.group(1))

    details_l = details.lower() if details else ""
    is_channel_grant = "channel grant" in details_l
    include_grant_debug = _DIGITAL_DEBUG_INCLUDE_GRANTS and is_channel_grant

    # Call/event logs include high-volume non-audio control events (register/response/etc).
    # Keep only call-type events when an explicit event field is present.
    if event_kind and "call" not in event_kind and not include_grant_debug:
        return None
    if event_kind and "encrypted" in event_kind and not include_grant_debug:
        return None
    if event_kind and "data call" in event_kind and not include_grant_debug:
        return None

    # Drop known non-audible call log rows (rejected/encrypted control updates).
    if details and _DIGITAL_EVENT_DROP_RE.search(details) and not include_grant_debug:
        return None

    duration_ms = _parse_duration_ms(duration_raw)

    # For structured call event rows, wait until the call has lasted long enough
    # to be considered an audible "hit" before surfacing it.
    if event_id and not include_grant_debug:
        # SDRTrunk call-event files often emit a short control "CHANNEL GRANT"
        # row first, then repeat the same event_id with a populated duration.
        # Keep only rows that look like actionable call activity.
        if is_channel_grant and duration_ms is None:
            return None
        # Some SDRTrunk schemas emit call rows without a duration field.
        # Allow those rows when they are not pure channel grants.
        if duration_ms is not None and duration_ms < _DIGITAL_HIT_MIN_DURATION_MS:
            return None

    time_ms = _parse_time_value(time_val, fallback_ms)
    if not time_val and (date_val or time_only):
        time_ms = _parse_time_ms(f"{date_val} {time_only}".strip(), fallback_ms)

    if freq:
        try:
            if float(str(freq).strip()) <= 0.0 and not include_grant_debug:
                return None
        except Exception:
            pass

    if not label and tgid:
        label = f"TG {tgid}"

    # Ignore control-only parenthetical pseudo-labels that are not real talkgroups.
    if not tgid and label and _DIGITAL_NON_AUDIO_LABEL_RE.fullmatch(label.strip()):
        return None

    if not label and not tgid:
        return None

    mode = _coerce_mode(mode_val)
    if not mode:
        mode = _coerce_mode(raw_line)

    event = {
        "type": "digital",
        "label": label,
        "timeMs": int(time_ms or fallback_ms),
        "raw": raw_line,
    }
    if mode:
        event["mode"] = mode
    if tgid:
        event["tgid"] = tgid
    if event_id:
        event["event_id"] = event_id
    if duration_ms is not None:
        event["durationMs"] = int(max(0, duration_ms))
    if freq:
        event["frequency"] = freq
    if site:
        event["site"] = site
    if include_grant_debug:
        event["debug_grant"] = True
    return event


def _extract_event_from_line(line: str, fallback_ms: int) -> dict | None:
    raw = (line or "").strip()
    if not raw:
        return None
    stripped = _strip_log_prefix(raw)
    if not stripped:
        return None
    if _IGNORE_EVENT_RE.search(stripped):
        return None
    label, mode, label_from_field = _extract_label_mode(stripped)
    if not label:
        return None
    if not (label_from_field or _EVENT_HINT_RE.search(stripped)):
        return None
    time_ms = _parse_time_ms(raw, fallback_ms)
    event = {"type": "digital", "label": label, "timeMs": time_ms, "raw": stripped}
    if mode:
        event["mode"] = mode
    tgid = _extract_tgid(stripped)
    if not tgid and _DIGITAL_NON_AUDIO_LABEL_RE.fullmatch(label.strip()):
        return None
    if tgid:
        event["tgid"] = tgid
    return event


def _extract_tgid(text: str) -> str:
    if not text:
        return ""
    raw = str(text).strip()
    m = _TGID_RE.search(raw)
    if m:
        return _normalize_tgid(m.group(1))

    # Common standalone forms from event logs, e.g. "(10101)", "TG 10101", "10101".
    compact = raw
    if compact.startswith("(") and compact.endswith(")"):
        compact = compact[1:-1].strip()
    compact = re.sub(r"^\s*(?:tgid|talkgroup|tg)\s*[:=#-]?\s*", "", compact, flags=re.I)
    compact = compact.strip()
    if compact.isdigit():
        return _normalize_tgid(compact)
    return ""


def _is_auto_placeholder_label(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    low = text.lower()
    if "auto-learned clear voice" in low:
        return True
    if _AUTO_ALPHA_RE.fullmatch(text):
        return True
    return False


def _is_non_fatal_error(line: str) -> bool:
    return bool(_NON_FATAL_ERROR_RE.search(line or ""))


def _suppress_status_warning(line: str) -> bool:
    return bool(_SUPPRESS_STATUS_WARNING_RE.search(line or ""))


def _is_java_stack_line(line: str) -> bool:
    text = (line or "").strip()
    if not text:
        return False
    return bool(_JAVA_STACK_FRAME_RE.match(text) or _JAVA_STACK_OMIT_RE.match(text))


def _write_mute_state(muted: bool) -> None:
    payload = {"muted": bool(muted), "ts": int(time.time())}
    tmp = _MUTE_STATE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_MUTE_STATE_PATH) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, _MUTE_STATE_PATH)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def get_digital_muted() -> bool:
    return bool(_DIGITAL_MUTED)


def set_digital_muted(muted: bool) -> bool:
    global _DIGITAL_MUTED
    _DIGITAL_MUTED = bool(muted)
    _write_mute_state(_DIGITAL_MUTED)
    return _DIGITAL_MUTED


def _digital_profile_paths(profile_id: str):
    base = _safe_realpath(DIGITAL_PROFILES_DIR)
    target = _safe_realpath(os.path.join(DIGITAL_PROFILES_DIR, profile_id))
    return base, target


def create_digital_profile_dir(profile_id: str):
    pid = _normalize_name(profile_id)
    if not validate_digital_profile_id(pid):
        return False, "invalid profileId"
    base, target = _digital_profile_paths(pid)
    if not base:
        return False, "profiles dir not configured"
    if not target.startswith(base + os.sep):
        return False, "invalid profile path"
    if os.path.exists(target):
        return False, "profile already exists"
    try:
        os.makedirs(target, exist_ok=False)
        note_path = os.path.join(target, "README.txt")
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_PROFILE_NOTE)
        listen_path = os.path.join(target, _LISTEN_FILENAME)
        with open(listen_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "updated": int(time.time()),
                    "default_listen": bool(_DEFAULT_LISTEN_ENABLED),
                    "items": {},
                },
                f,
                indent=2,
                sort_keys=True,
            )
            f.write("\n")
    except Exception as e:
        return False, str(e)
    return True, ""


def delete_digital_profile_dir(profile_id: str):
    pid = _normalize_name(profile_id)
    if not validate_digital_profile_id(pid):
        return False, "invalid profileId"
    base, target = _digital_profile_paths(pid)
    if not base:
        return False, "profiles dir not configured"
    if not target.startswith(base + os.sep):
        return False, "invalid profile path"
    if not os.path.isdir(target):
        return False, "profile not found"
    if os.path.islink(target):
        return False, "profile path is a symlink"
    active_link = DIGITAL_ACTIVE_PROFILE_LINK
    if active_link and os.path.islink(active_link):
        try:
            active_target = _safe_realpath(active_link)
        except Exception:
            active_target = ""
        if active_target and active_target == target:
            return False, "profile is active"
    try:
        import shutil
        shutil.rmtree(target)
    except Exception as e:
        return False, str(e)
    return True, ""


def inspect_digital_profile(profile_id: str, max_files: int = 200, max_depth: int = 3, max_preview_bytes: int = 20000):
    pid = _normalize_name(profile_id)
    if not validate_digital_profile_id(pid):
        return False, "invalid profileId"
    base, target = _digital_profile_paths(pid)
    if not base:
        return False, "profiles dir not configured"
    if not target.startswith(base + os.sep):
        return False, "invalid profile path"
    if not os.path.isdir(target):
        return False, "profile not found"

    files = []
    has_more = False
    for dirpath, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, target)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if len(files) >= max_files:
                has_more = True
                break
            fpath = os.path.join(dirpath, name)
            if os.path.islink(fpath):
                continue
            rel = os.path.relpath(fpath, target)
            files.append(rel)
        if has_more:
            break

    files = sorted(files)

    preview_name = ""
    preview = ""
    preview_candidates = ("README.txt", "README.md", "notes.txt")
    for candidate in preview_candidates:
        candidate_path = os.path.join(target, candidate)
        if os.path.isfile(candidate_path) and not os.path.islink(candidate_path):
            preview_name = candidate
            try:
                with open(candidate_path, "rb") as f:
                    data = f.read(max_preview_bytes)
                preview = data.decode("utf-8", errors="ignore")
            except Exception:
                preview = ""
            break

    payload = {
        "ok": True,
        "profileId": pid,
        "files": files,
        "has_more": bool(has_more),
        "previewName": preview_name,
        "preview": preview,
    }
    return True, payload


def _get_profile_dir(profile_id: str):
    pid = _normalize_name(profile_id)
    if not validate_digital_profile_id(pid):
        return "", "invalid profileId"
    base, target = _digital_profile_paths(pid)
    if not base:
        return "", "profiles dir not configured"
    if not target.startswith(base + os.sep):
        return "", "invalid profile path"
    if not os.path.isdir(target):
        return "", "profile not found"
    return target, ""


def _list_profile_call_event_logs(profile_id: str) -> list[str]:
    base = str(DIGITAL_EVENT_LOG_DIR or "").strip()
    pid = str(profile_id or "").strip().lower()
    if not base or not pid or not os.path.isdir(base):
        return []
    matches: list[tuple[float, str]] = []
    try:
        entries = os.listdir(base)
    except Exception:
        return []
    suffix = f"_{pid}_call_events.log"
    for name in entries:
        lower = str(name or "").strip().lower()
        if not lower.endswith("_call_events.log"):
            continue
        if suffix not in lower:
            continue
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = float(os.path.getmtime(path))
        except Exception:
            mtime = 0.0
        matches.append((mtime, path))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in matches[:_REJECT_SCAN_MAX_FILES]]


def _extract_reject_tgid_from_row(row: dict) -> str:
    tgid = _normalize_tgid(_row_value(row, _EVENT_TGID_KEYS))
    if tgid:
        return tgid

    to_val = _row_value(row, ("to", "from"))
    if to_val:
        m = re.search(r"\((\d+)\)", to_val)
        if not m:
            m = re.search(r"\b(\d{3,7})\b", to_val)
        if m:
            tgid = _normalize_tgid(m.group(1))
            if tgid:
                return tgid

    label = _row_value(row, _EVENT_LABEL_KEYS)
    if label:
        tgid = _extract_tgid(label)
        if tgid:
            return tgid

    return ""


def _read_profile_rejected_grants(profile_id: str) -> tuple[dict, dict]:
    reject_map: dict[str, dict] = {}
    files = _list_profile_call_event_logs(profile_id)
    summary = {
        "events": 0,
        "tgids": 0,
        "files": [os.path.basename(path) for path in files],
        "maxFiles": int(_REJECT_SCAN_MAX_FILES),
        "maxLinesPerFile": int(_REJECT_SCAN_MAX_LINES),
    }
    if not files:
        return reject_map, summary

    for path in files:
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = time.time()
        fallback_ms = int(float(mtime or time.time()) * 1000)
        lines = _read_tail_lines(path, max_bytes=_REJECT_SCAN_MAX_BYTES, max_lines=_REJECT_SCAN_MAX_LINES)
        if not lines:
            continue

        header = None
        for raw in lines:
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                row = next(csv.reader([text]))
            except Exception:
                continue
            if not row:
                continue
            if header is None:
                norm = [_norm_key(x) for x in row]
                joined = " ".join(norm)
                if any(_norm_key(k) in joined for k in _EVENT_HEADER_KEYS):
                    header = norm
                    continue
                # Tail reads usually omit CSV headers; use the known CALL_EVENT schema.
                if len(row) >= 11:
                    header = [
                        "timestamp",
                        "duration_ms",
                        "protocol",
                        "event",
                        "from",
                        "to",
                        "channel_number",
                        "frequency",
                        "timeslot",
                        "details",
                        "event_id",
                    ]
                else:
                    continue
            if not header:
                continue

            row_norm = {}
            for idx, key in enumerate(header):
                if idx >= len(row):
                    break
                row_norm[key] = row[idx]

            details = _row_value(row_norm, _EVENT_DETAILS_KEYS)
            if not details or not _REJECT_GRANT_RE.search(details):
                continue

            tgid = _extract_reject_tgid_from_row(row_norm)
            if not tgid:
                continue

            time_val = _row_value(row_norm, _EVENT_TIME_KEYS)
            date_val = _row_value(row_norm, _EVENT_DATE_KEYS)
            time_only = _row_value(row_norm, _EVENT_TIME_ONLY_KEYS)
            time_ms = _parse_time_value(time_val, fallback_ms)
            if not time_val and (date_val or time_only):
                time_ms = _parse_time_ms(f"{date_val} {time_only}".strip(), fallback_ms)

            summary["events"] = int(summary["events"]) + 1
            entry = reject_map.get(tgid)
            if not entry:
                entry = {
                    "count": 0,
                    "lastTimeMs": 0,
                    "lastReason": "",
                }
                reject_map[tgid] = entry
            entry["count"] = int(entry.get("count") or 0) + 1
            if int(time_ms or 0) >= int(entry.get("lastTimeMs") or 0):
                entry["lastTimeMs"] = int(time_ms or 0)
                entry["lastReason"] = details

    summary["tgids"] = len(reject_map)
    return reject_map, summary


def read_digital_talkgroups(profile_id: str, max_rows: int = 5000):
    profile_dir, err = _get_profile_dir(profile_id)
    if err:
        return False, err
    candidates = ("talkgroups.csv", "talkgroups_with_group.csv")
    path = ""
    for name in candidates:
        candidate = os.path.join(profile_dir, name)
        if os.path.isfile(candidate):
            path = candidate
            break
    if not path:
        return False, "talkgroups file not found"

    listen_path = os.path.join(profile_dir, _LISTEN_FILENAME)
    listen_map, default_listen, _ = _read_listen_config(listen_path)

    reject_map, reject_summary = _read_profile_rejected_grants(profile_id)

    items = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue
                row_norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
                dec = row_norm.get("dec") or row_norm.get("decimal") or ""
                if not dec.isdigit():
                    continue
                item = {
                    "dec": dec,
                    "hex": row_norm.get("hex") or "",
                    "mode": row_norm.get("mode") or "",
                    "alpha": row_norm.get("alpha tag") or row_norm.get("alpha_tag") or "",
                    "description": row_norm.get("description") or "",
                    "tag": row_norm.get("tag") or "",
                }
                item["listen"] = bool(listen_map.get(dec, default_listen))
                reject_entry = reject_map.get(dec) or {}
                reject_count = int(reject_entry.get("count") or 0)
                item["rejectedGrantCount"] = reject_count
                item["rejectedGrantRecent"] = bool(reject_count > 0)
                item["rejectedGrantLastTimeMs"] = int(reject_entry.get("lastTimeMs") or 0)
                if reject_count > 0:
                    item["rejectedGrantReason"] = str(reject_entry.get("lastReason") or "")
                items.append(item)
                if len(items) >= max_rows:
                    break
    except Exception as e:
        return False, str(e)

    return True, {
        "ok": True,
        "profileId": _normalize_name(profile_id),
        "items": items,
        "source": os.path.basename(path),
        "rejectedGrantSummary": reject_summary,
    }


def write_digital_listen(profile_id: str, items: list):
    profile_dir, err = _get_profile_dir(profile_id)
    if err:
        return False, err
    listen_path = os.path.join(profile_dir, _LISTEN_FILENAME)
    incoming: dict[str, bool] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        dec = _normalize_tgid(str(item.get("dec") or item.get("tgid") or ""))
        if not dec:
            continue
        incoming[dec] = bool(item.get("listen"))
    existing_map, default_listen, existing_meta = _read_listen_config(listen_path)
    mapping = dict(existing_map)
    mapping.update(incoming)

    talkgroups_payload: dict[str, dict] = {}
    keys = set(mapping.keys()) | set(existing_meta.keys())
    for dec in sorted(keys, key=lambda x: int(x)):
        node = {}
        meta = existing_meta.get(dec) or {}
        if isinstance(meta, dict):
            node.update(meta)
        node["listen"] = bool(mapping.get(dec, default_listen))
        talkgroups_payload[dec] = node

    payload = {
        "updated": int(time.time()),
        "items": mapping,
        "default_listen": bool(default_listen),
    }
    if talkgroups_payload:
        payload["talkgroups"] = talkgroups_payload
    try:
        tmp = listen_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, listen_path)
    except Exception as e:
        return False, str(e)
    return True, ""


class DigitalAdapter:
    """Interface for digital backends."""
    name = "base"

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def restart(self):
        raise NotImplementedError

    def isActive(self):
        raise NotImplementedError

    def listProfiles(self):
        raise NotImplementedError

    def getProfile(self):
        raise NotImplementedError

    def setProfile(self, profileId: str, *, restart_service: bool = True):
        raise NotImplementedError

    def getLastEvent(self):
        raise NotImplementedError

    def getLastError(self):
        raise NotImplementedError

    def getLastWarning(self):
        raise NotImplementedError

    def getRecentEvents(self, limit: int = 20):
        raise NotImplementedError


class _BaseDigitalAdapter(DigitalAdapter):
    """Shared in-memory state for adapters."""

    def __init__(self):
        self._profile = ""
        self._last_event = None
        self._last_error = ""
        self._last_error_time_ms = 0
        self._last_warning = ""
        self._last_warning_time_ms = 0
        self._last_event_time_ms = 0
        self._recent_events = []
        self._recent_event_keys = set()
        self._recent_limit = 50

    def _set_last_error(self, msg: str, time_ms: int = 0):
        self._last_error = (msg or "").strip()
        self._last_error_time_ms = int(time_ms) if int(time_ms or 0) > 0 else int(time.time() * 1000)

    def _set_last_warning(self, msg: str, time_ms: int = 0):
        self._last_warning = (msg or "").strip()
        self._last_warning_time_ms = int(time_ms) if int(time_ms or 0) > 0 else int(time.time() * 1000)

    def _clear_error(self):
        self._last_error = ""
        self._last_error_time_ms = 0

    def _clear_warning(self):
        self._last_warning = ""
        self._last_warning_time_ms = 0

    def _set_last_event(self, label: str, mode: str | None = None, raw=None):
        event = {
            "label": label or "",
            "timeMs": int(time.time() * 1000),
        }
        if mode:
            event["mode"] = mode
        if raw is not None:
            event["raw"] = raw
        self._last_event = event
        self._last_event_time_ms = int(event.get("timeMs") or 0)

    def _record_event(self, event: dict) -> None:
        if not event:
            return
        if "type" not in event:
            event = dict(event)
            event["type"] = "digital"
        event_time_ms = int(event.get("timeMs") or 0)
        event_id = str(event.get("event_id") or "").strip()
        if event_id:
            # Event logs often repeat the same EVENT_ID for a call as duration
            # updates stream in. Keep one row per second per ID so hit history
            # advances without flooding duplicates.
            sec_bucket = int(event_time_ms / 1000) if event_time_ms > 0 else 0
            key = f"id:{event_id}:{sec_bucket}"
        else:
            key = f"{event_time_ms}|{event.get('label')}|{event.get('mode','')}"
        if key in self._recent_event_keys:
            return
        event = dict(event)
        event["_key"] = key
        self._recent_event_keys.add(key)
        self._recent_events.append(event)
        if len(self._recent_events) > self._recent_limit:
            old = self._recent_events.pop(0)
            old_key = old.get("_key")
            if old_key:
                self._recent_event_keys.discard(old_key)

    def getRecentEvents(self, limit: int = 20):
        items = list(self._recent_events)[-max(1, limit):]
        cleaned = []
        for item in items:
            item = dict(item)
            item.pop("_key", None)
            cleaned.append(item)
        return cleaned

    def getLastEvent(self):
        if self._last_event:
            return dict(self._last_event)
        return {"label": "", "timeMs": 0}

    def getLastError(self):
        return self._last_error or None

    def getLastWarning(self):
        return self._last_warning or None

class NullDigitalAdapter(_BaseDigitalAdapter):
    """No-op adapter when digital is disabled or misconfigured."""
    name = "none"

    def __init__(self, reason: str = "digital backend disabled"):
        super().__init__()
        self._reason = reason
        if reason:
            self._set_last_error(reason)

    def start(self):
        return False, self._reason

    def stop(self):
        return False, self._reason

    def restart(self):
        return False, self._reason

    def isActive(self):
        return False

    def listProfiles(self):
        return []

    def getProfile(self):
        return ""

    def setProfile(self, profileId: str, *, restart_service: bool = True):
        return False, self._reason

    def getRecentEvents(self, limit: int = 20):
        return []


class SdrtrunkAdapter(_BaseDigitalAdapter):
    """Systemd-backed adapter for sdrtrunk."""
    name = "sdrtrunk"

    def __init__(self):
        super().__init__()
        self._service_name = _normalize_name(DIGITAL_SERVICE_NAME)
        self._profiles_dir = DIGITAL_PROFILES_DIR
        self._active_link = DIGITAL_ACTIVE_PROFILE_LINK
        self._log_path = DIGITAL_LOG_PATH
        self._last_log_mtime = None
        self._last_log_size = None
        self._event_log_dir = DIGITAL_EVENT_LOG_DIR
        self._event_log_mode = (DIGITAL_EVENT_LOG_MODE or "auto").strip().lower()
        self._event_log_tail_lines = int(DIGITAL_EVENT_LOG_TAIL_LINES or 500)
        self._event_log_offsets = {}
        self._event_log_headers = {}
        self._event_log_files_cache: list[str] = []
        self._event_log_files_cache_at = 0.0
        self._event_log_files_cache_ready = False
        self._decoded_log_files_cache: list[str] = []
        self._decoded_log_files_cache_at = 0.0
        self._decoded_log_files_cache_ready = False
        self._event_log_scan_interval_sec = max(
            1.0,
            float(os.getenv("DIGITAL_EVENT_LOG_SCAN_INTERVAL_SEC", "20")),
        )
        self._tg_map = {}
        self._tg_map_profile = ""
        self._tg_map_mtime = None
        self._listen_map = {}
        self._listen_map_profile = ""
        self._listen_map_mtime = None
        self._listen_default = bool(_DEFAULT_LISTEN_ENABLED)
        self._refresh_lock = threading.Lock()
        self._last_refresh_monotonic = 0.0
        self._refresh_min_interval_sec = max(
            0.0,
            float(os.getenv("DIGITAL_LOG_REFRESH_MIN_INTERVAL_SEC", "0.40")),
        )
        if not validate_digital_service_name(self._service_name):
            self._set_last_error("invalid digital service name")

    def _systemctl(self, args):
        if not validate_digital_service_name(self._service_name):
            return False, "invalid digital service name"
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
        # Retry with sudo if policykit blocks non-root control.
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

    def _refresh_log_cache(self):
        now_mono = time.monotonic()
        if (
            self._last_refresh_monotonic
            and (now_mono - self._last_refresh_monotonic) < self._refresh_min_interval_sec
        ):
            return

        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            now_mono = time.monotonic()
            if (
                self._last_refresh_monotonic
                and (now_mono - self._last_refresh_monotonic) < self._refresh_min_interval_sec
            ):
                return
            self._last_refresh_monotonic = now_mono

            mode = self._event_log_mode or "auto"
            mode = mode if mode in ("auto", "event_logs", "app_log") else "auto"
            lines = []
            fallback_ms = int(time.time() * 1000)
            app_events = []

            try:
                stat = os.stat(self._log_path)
                mtime = stat.st_mtime
                size = stat.st_size
            except Exception:
                mtime = None
                size = None
            if mtime and size is not None:
                if self._last_log_mtime != mtime or self._last_log_size != size:
                    self._last_log_mtime = mtime
                    self._last_log_size = size
                    lines = _read_tail_lines(self._log_path)
                if lines:
                    fallback_ms = int(mtime * 1000) if mtime else fallback_ms
                    if mode in ("auto", "app_log"):
                        for line in lines:
                            event = _extract_event_from_line(line, fallback_ms)
                            if event:
                                mapped = self._map_event_label(event)
                                if mapped and mapped.get("muted"):
                                    continue
                                app_events.append(mapped)

                    # Last error/warning (best-effort) from app log regardless of mode.
                    last_err = None
                    last_err_time_ms = 0
                    last_warn = None
                    last_warn_time_ms = 0
                    for line in reversed(lines):
                        raw = (line or "").strip()
                        if not raw:
                            continue
                        clean = _strip_log_prefix(raw)
                        if _is_java_stack_line(raw) or _is_java_stack_line(clean):
                            continue
                        if not re.search(r"(error|exception)", clean, re.I):
                            continue
                        line_time_ms = _parse_time_ms(raw, fallback_ms)
                        if _is_non_fatal_error(clean):
                            if not last_warn:
                                last_warn = clean
                                last_warn_time_ms = line_time_ms
                            continue
                        last_err = clean
                        last_err_time_ms = line_time_ms
                        break
                    if last_err:
                        self._set_last_error(last_err, last_err_time_ms)
                    else:
                        self._clear_error()
                    if last_warn:
                        self._set_last_warning(last_warn, last_warn_time_ms)
                    else:
                        self._clear_warning()

            event_log_events = []
            if mode in ("auto", "event_logs"):
                event_log_events = self._read_event_logs()
            has_event_log_files = bool(self._event_log_files_cache)

            events = []
            if mode == "app_log":
                events = app_events
            elif mode == "event_logs":
                events = event_log_events
            else:
                # In auto mode, trust structured call-event logs whenever they
                # exist to avoid surfacing control/metadata lines from app logs
                # as synthetic "hits".
                if has_event_log_files:
                    events = event_log_events
                else:
                    events = app_events

            if events:
                for event in events:
                    self._record_event(event)
                latest = max(events, key=lambda item: item.get("timeMs", 0))
                self._last_event = latest
                self._last_event_time_ms = int(latest.get("timeMs") or fallback_ms)
                return

            if mode == "app_log" and lines:
                # Last event fallback: use last non-empty line only for app_log mode.
                last_line = ""
                for line in reversed(lines):
                    if line.strip():
                        last_line = line.strip()
                        break
                if not last_line:
                    return
                if not (_EVENT_HINT_RE.search(last_line) or _TGID_RE.search(last_line)):
                    return
                time_ms = _parse_time_ms(last_line, fallback_ms)
                label, mode_label, _ = _extract_label_mode(last_line)
                event = {"type": "digital", "label": label, "timeMs": time_ms, "raw": last_line}
                if mode_label:
                    event["mode"] = mode_label
                event = self._map_event_label(event)
                self._last_event = event
                self._last_event_time_ms = time_ms
                return
        finally:
            self._refresh_lock.release()

    def _list_event_log_files(self):
        now_mono = time.monotonic()
        if self._event_log_files_cache_ready:
            cache_age = now_mono - self._event_log_files_cache_at
            if cache_age < self._event_log_scan_interval_sec:
                return [
                    path for path in self._event_log_files_cache
                    if os.path.isfile(path)
                ]

        base = self._event_log_dir
        if not base or not os.path.isdir(base):
            self._event_log_files_cache = []
            self._event_log_files_cache_at = now_mono
            self._event_log_files_cache_ready = True
            return []
        all_candidates: list[tuple[str, str]] = []
        call_candidates: list[tuple[str, str]] = []
        try:
            it = os.scandir(base)
        except Exception:
            self._event_log_files_cache = []
            self._event_log_files_cache_at = now_mono
            self._event_log_files_cache_ready = True
            return []
        with it:
            for entry in it:
                name = str(entry.name or "")
                if name.startswith("."):
                    continue
                if not re.search(r"\.(csv|log|txt|json)$", name, re.I):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except Exception:
                    continue
                item = (name, entry.path)
                all_candidates.append(item)
                if "call_events" in name.lower():
                    call_candidates.append(item)

        # Prefer call_event files when present.
        candidates = call_candidates if call_candidates else all_candidates
        # Most SDRTrunk files use timestamp-prefixed names; newest tend to sort last.
        # Scan newest-first and stop early once we have enough candidates.
        candidates.sort(key=lambda item: item[0], reverse=True)

        def _prefer_aggregate(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
            aggregate = [item for item in items if "_0_hz_" in item[0].lower()]
            if not aggregate:
                return items
            aggregate_paths = {path for _, path in aggregate}
            return aggregate + [item for item in items if item[1] not in aggregate_paths]

        # Prioritize logs for the active profile first so stale files from other
        # profiles do not starve current events in very large event_log dirs.
        active_profile = str(self._read_active_profile_id() or "").strip().lower()
        if active_profile:
            token = f"{active_profile}_call_events.log"
            active_candidates = [item for item in candidates if token in item[0].lower()]
            other_candidates = [item for item in candidates if token not in item[0].lower()]
            candidates = _prefer_aggregate(active_candidates) + _prefer_aggregate(other_candidates)
        else:
            candidates = _prefer_aggregate(candidates)

        data_paths: list[str] = []
        header_only_paths: list[str] = []
        scanned = 0
        for _, path in candidates:
            scanned += 1
            try:
                size = int(os.path.getsize(path))
            except Exception:
                size = 0
            if size > _DIGITAL_EVENT_MIN_DATA_BYTES:
                data_paths.append(path)
            else:
                # Keep a small fallback list if every file appears header-only.
                if len(header_only_paths) < 5:
                    header_only_paths.append(path)

            if len(data_paths) >= 5:
                break
            # Keep scan bounded even if the directory has many thousands of files.
            if scanned >= _DIGITAL_EVENT_SCAN_MAX_FILES:
                break

        selected_paths = list(data_paths[:5])
        if len(selected_paths) < 5:
            selected_paths.extend(header_only_paths[: 5 - len(selected_paths)])
        self._event_log_files_cache = selected_paths
        self._event_log_files_cache_at = now_mono
        self._event_log_files_cache_ready = True
        return list(selected_paths)

    def _ensure_event_log_header(self, path: str) -> None:
        if path in self._event_log_headers:
            return
        header = None
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                first = f.readline()
            if first:
                row = next(csv.reader([first]))
                if row:
                    norm = [_norm_key(x) for x in row]
                    joined = " ".join(norm)
                    if any(_norm_key(k) in joined for k in _EVENT_HEADER_KEYS):
                        header = norm
        except Exception:
            header = None
        self._event_log_headers[path] = header

    def _read_event_log_lines(self, path: str):
        try:
            size = os.path.getsize(path)
        except Exception:
            return []
        last_offset = self._event_log_offsets.get(path)
        if last_offset is None:
            self._ensure_event_log_header(path)
            lines = _read_tail_lines(path, max_bytes=65536, max_lines=self._event_log_tail_lines)
            self._event_log_offsets[path] = size
            return lines
        if size < last_offset:
            last_offset = 0
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(last_offset)
                data = f.read()
                self._event_log_offsets[path] = f.tell()
        except Exception:
            return []
        return data.splitlines()

    def _parse_event_log_line(self, raw: str, path: str, fallback_ms: int):
        text = (raw or "").strip()
        if not text:
            return None
        if _IGNORE_EVENT_RE.search(text):
            return None
        if text.startswith("{") and text.endswith("}"):
            try:
                payload = json.loads(text)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                row = {_norm_key(k): str(v) for k, v in payload.items()}
                return _row_to_event(row, text, fallback_ms)
        header = self._event_log_headers.get(path)
        try:
            row = next(csv.reader([text]))
        except Exception:
            row = []
        if row and header is None:
            norm = [_norm_key(x) for x in row]
            joined = " ".join(norm)
            if any(_norm_key(k) in joined for k in _EVENT_HEADER_KEYS):
                self._event_log_headers[path] = norm
                return None
            header = None
        if row and header:
            if row and all(_norm_key(x) == (header[i] if i < len(header) else "") for i, x in enumerate(row[: len(header)])):
                return None
            row_norm = {}
            for idx, key in enumerate(header):
                if idx >= len(row):
                    break
                row_norm[key] = row[idx]
            event = _row_to_event(row_norm, text, fallback_ms)
            return event
        kv = {}
        if ":" in text or "=" in text:
            parts = re.split(r"[|,]", text)
            for part in parts:
                if ":" in part:
                    key, val = part.split(":", 1)
                elif "=" in part:
                    key, val = part.split("=", 1)
                else:
                    continue
                key = _norm_key(key)
                if key:
                    kv[key] = val.strip()
        if kv:
            event = _row_to_event(kv, text, fallback_ms)
            return event
        return _extract_event_from_line(text, fallback_ms)

    def _event_log_line_encryption_hint(self, raw: str, path: str) -> tuple[str, bool]:
        text = (raw or "").strip()
        if not text:
            return "", False
        header = self._event_log_headers.get(path)
        try:
            row = next(csv.reader([text]))
        except Exception:
            return "", False
        if not row:
            return "", False
        if header is None:
            norm = [_norm_key(x) for x in row]
            joined = " ".join(norm)
            if any(_norm_key(k) in joined for k in _EVENT_HEADER_KEYS):
                self._event_log_headers[path] = norm
            return "", False
        if row and all(_norm_key(x) == (header[i] if i < len(header) else "") for i, x in enumerate(row[: len(header)])):
            return "", False

        row_norm = {}
        for idx, key in enumerate(header):
            if idx >= len(row):
                break
            row_norm[key] = row[idx]
        event_id = _row_value(row_norm, _EVENT_ID_KEYS)
        if not event_id:
            return "", False
        event_kind = _row_value(row_norm, _EVENT_KIND_KEYS).lower()
        details = _row_value(row_norm, _EVENT_DETAILS_KEYS)
        encrypted = False
        if event_kind and "encrypted" in event_kind:
            encrypted = True
        if details and re.search(r"\b(encrypt|encrypted|encryption)\b", details, re.I):
            encrypted = True
        return event_id, encrypted

    def _read_event_logs(self):
        events = []
        paths = self._list_event_log_files()
        now_ms = int(time.time() * 1000)
        for path in paths:
            self._ensure_event_log_header(path)
            lines = self._read_event_log_lines(path)
            if not lines:
                continue
            blocked_event_ids: set[str] = set()
            path_events: list[dict] = []
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = None
            fallback_ms = int(mtime * 1000) if mtime else now_ms
            for line in lines:
                hinted_event_id, hinted_encrypted = self._event_log_line_encryption_hint(line, path)
                if hinted_encrypted and hinted_event_id:
                    blocked_event_ids.add(str(hinted_event_id).strip())
                event = self._parse_event_log_line(line, path, fallback_ms)
                if event:
                    mapped = self._map_event_label(event)
                    if mapped and mapped.get("muted"):
                        continue
                    path_events.append(mapped)
            if blocked_event_ids:
                for item in path_events:
                    event_id = str(item.get("event_id") or "").strip()
                    if event_id and event_id in blocked_event_ids:
                        continue
                    events.append(item)
            else:
                events.extend(path_events)
        return events

    def _read_log_tail(self, max_lines: int | None = None):
        max_lines = max_lines or self._event_log_tail_lines or 500
        return _read_tail_lines(self._log_path, max_lines=max_lines)

    def _detect_tuner_busy(self, lines: list) -> list:
        hits = []
        now_ms = int(time.time() * 1000)
        for line in lines or []:
            if _TUNER_BUSY_RE.search(line or ""):
                raw = (line or "").strip()
                ts = _parse_time_ms(raw, now_ms)
                hits.append({"line": raw, "timeMs": ts})
        return hits

    def _decoded_message_log_files(self) -> list[str]:
        now_mono = time.monotonic()
        if self._decoded_log_files_cache_ready:
            cache_age = now_mono - self._decoded_log_files_cache_at
            if cache_age < self._event_log_scan_interval_sec:
                return [
                    path for path in self._decoded_log_files_cache
                    if os.path.isfile(path)
                ]

        base = self._event_log_dir
        if not base or not os.path.isdir(base):
            self._decoded_log_files_cache = []
            self._decoded_log_files_cache_at = now_mono
            self._decoded_log_files_cache_ready = True
            return []

        active_profile = str(self._read_active_profile_id() or "").strip().lower()
        active_token = f"_{active_profile}_decoded_messages.log" if active_profile else ""
        active_candidates: list[tuple[str, int, float, bool]] = []
        other_candidates: list[tuple[str, int, float, bool]] = []
        try:
            it = os.scandir(base)
        except Exception:
            self._decoded_log_files_cache = []
            self._decoded_log_files_cache_at = now_mono
            self._decoded_log_files_cache_ready = True
            return []

        with it:
            for entry in it:
                name = str(entry.name or "")
                if name.startswith("."):
                    continue
                lower = name.lower()
                if "_decoded_messages.log" not in lower:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except Exception:
                    continue
                item = (
                    entry.path,
                    int(getattr(st, "st_size", 0) or 0),
                    float(getattr(st, "st_mtime", 0.0) or 0.0),
                    "_0_hz_" in lower,
                )
                if active_token and active_token in lower:
                    active_candidates.append(item)
                else:
                    other_candidates.append(item)

        selected = active_candidates if active_candidates else other_candidates
        selected.sort(key=lambda item: item[2], reverse=True)  # newest first

        # Prefer aggregate control-channel logs when present, then newest others.
        aggregate = [item for item in selected if item[3]]
        non_aggregate = [item for item in selected if not item[3]]
        ordered = aggregate + non_aggregate if aggregate else selected

        data_paths: list[str] = []
        header_only_paths: list[str] = []
        for path, size, _, _ in ordered:
            if size > _DIGITAL_EVENT_MIN_DATA_BYTES:
                data_paths.append(path)
            elif len(header_only_paths) < 5:
                header_only_paths.append(path)
            if len(data_paths) >= 5:
                break

        selected_paths = list(data_paths[:5])
        if len(selected_paths) < 5:
            selected_paths.extend(header_only_paths[: 5 - len(selected_paths)])

        self._decoded_log_files_cache = selected_paths
        self._decoded_log_files_cache_at = now_mono
        self._decoded_log_files_cache_ready = True
        return list(selected_paths)

    def _control_channel_summary(self) -> dict:
        now_ms = int(time.time() * 1000)
        window_ms = int(_DIGITAL_CONTROL_WINDOW_MS)
        lock_fail_count = 0
        last_lock_fail_ms = 0

        for line in self._read_log_tail(max_lines=_DIGITAL_CONTROL_TAIL_LINES):
            raw = (line or "").strip()
            if not raw or not _CONTROL_LOCK_FAIL_RE.search(raw):
                continue
            ts = _parse_time_ms(raw, now_ms)
            if ts > (now_ms + 120000):
                continue
            if (now_ms - ts) > window_ms:
                continue
            lock_fail_count += 1
            if ts > last_lock_fail_ms:
                last_lock_fail_ms = ts

        decoded_logs = self._decoded_message_log_files()
        if not decoded_logs:
            return {
                "control_decode_available": False,
                "control_channel_locked": False,
                "control_activity_count": 0,
                "control_sync_loss_count": 0,
                "control_last_time_ms": 0,
                "control_lock_fail_count": int(lock_fail_count),
                "control_lock_fail_last_time_ms": int(last_lock_fail_ms),
                "control_window_ms": window_ms,
                "control_decode_files": 0,
            }

        control_count = 0
        sync_loss_count = 0
        last_control_ms = 0
        for path in decoded_logs:
            lines = _read_tail_lines(
                path,
                max_bytes=_DIGITAL_CONTROL_TAIL_BYTES,
                max_lines=_DIGITAL_CONTROL_TAIL_LINES,
            )
            for line in lines:
                raw = (line or "").strip()
                if not raw:
                    continue
                ts = _parse_time_ms(raw, 0)
                if ts <= 0:
                    continue
                if ts > (now_ms + 120000):
                    continue
                if (now_ms - ts) > window_ms:
                    continue
                if _SYNC_LOSS_RE.search(raw):
                    sync_loss_count += 1
                    continue
                if _CONTROL_LOCK_FAIL_RE.search(raw):
                    lock_fail_count += 1
                    if ts > last_lock_fail_ms:
                        last_lock_fail_ms = ts
                    continue
                # Count decoded control-plane messages as direct evidence that
                # the control channel is being demodulated.
                if ",PASSED," in raw.upper() and _CONTROL_MESSAGE_RE.search(raw):
                    control_count += 1
                    if ts > last_control_ms:
                        last_control_ms = ts

        return {
            "control_decode_available": True,
            "control_channel_locked": bool(control_count > 0 and last_control_ms > 0),
            "control_activity_count": int(control_count),
            "control_sync_loss_count": int(sync_loss_count),
            "control_last_time_ms": int(last_control_ms),
            "control_lock_fail_count": int(lock_fail_count),
            "control_lock_fail_last_time_ms": int(last_lock_fail_ms),
            "control_window_ms": window_ms,
            "control_decode_files": len(decoded_logs),
        }

    def _playlist_source_summary(self) -> dict:
        playlist_path = _safe_realpath(DIGITAL_PLAYLIST_PATH)
        if not playlist_path:
            return {
                "playlist_source_ok": False,
                "playlist_source_error": "digital playlist path not configured",
            }
        if not os.path.isfile(playlist_path):
            return {
                "playlist_source_ok": False,
                "playlist_source_error": f"playlist not found: {playlist_path}",
                "playlist_path": playlist_path,
            }
        try:
            tree = ET.parse(playlist_path)
            root = tree.getroot()
        except Exception as e:
            return {
                "playlist_source_ok": False,
                "playlist_source_error": f"failed to parse playlist: {e}",
                "playlist_path": playlist_path,
            }
        channel = root.find("channel")
        if channel is None:
            return {
                "playlist_source_ok": False,
                "playlist_source_error": "playlist has no channel node",
                "playlist_path": playlist_path,
            }
        source_conf = channel.find("source_configuration")
        if source_conf is None:
            return {
                "playlist_source_ok": False,
                "playlist_source_error": "playlist channel has no source_configuration",
                "playlist_path": playlist_path,
            }
        frequencies: list[int] = []
        seen: set[int] = set()
        freq_attr = str(source_conf.get("frequency", "")).strip()
        if freq_attr.isdigit():
            hz = int(freq_attr)
            if hz > 0 and hz not in seen:
                frequencies.append(hz)
                seen.add(hz)
        for node in source_conf.findall("frequency"):
            value = str(node.text or "").strip()
            if not value.isdigit():
                continue
            hz = int(value)
            if hz <= 0 or hz in seen:
                continue
            frequencies.append(hz)
            seen.add(hz)
        return {
            "playlist_source_ok": True,
            "playlist_path": playlist_path,
            "playlist_source_type": str(source_conf.get("source_type", "")).strip(),
            "playlist_source_config_type": str(source_conf.get("type", "")).strip(),
            "playlist_preferred_tuner": str(source_conf.get("preferred_tuner", "")).strip(),
            "playlist_frequency_count": len(frequencies),
            "playlist_frequency_hz": frequencies[:64],
        }

    def _listen_filter_summary(self) -> dict:
        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            return {
                "listen_filter_ok": False,
                "listen_filter_error": "no active profile",
                "listen_talkgroup_count": 0,
                "listen_enabled_count": 0,
                "listen_default": True,
                "listen_map_entries": 0,
                "listen_filter_blocking": False,
            }

        tg_map = self._load_talkgroup_map() or {}
        listen_map = self._load_listen_map() or {}
        default_listen = bool(self._listen_default)

        talkgroup_count = len(tg_map)
        enabled_count = 0
        if talkgroup_count > 0:
            if default_listen and not listen_map:
                enabled_count = talkgroup_count
            else:
                for tgid in tg_map.keys():
                    if listen_map.get(str(tgid), default_listen):
                        enabled_count += 1
        blocking = talkgroup_count > 0 and enabled_count <= 0
        payload = {
            "listen_filter_ok": True,
            "listen_talkgroup_count": int(talkgroup_count),
            "listen_enabled_count": int(enabled_count),
            "listen_default": bool(default_listen),
            "listen_map_entries": int(len(listen_map)),
            "listen_filter_blocking": bool(blocking),
        }
        if blocking:
            payload["listen_filter_error"] = (
                "all talkgroups are muted by listen settings "
                "(talkgroups>0, enabled=0)"
            )
        return payload

    def preflight(self):
        lines = self._read_log_tail()
        busy_hits = self._detect_tuner_busy(lines)
        now_ms = int(time.time() * 1000)
        busy_hits = [
            hit for hit in busy_hits
            if int(hit.get("timeMs") or 0) <= 0
            or (now_ms - int(hit.get("timeMs") or 0)) <= _DIGITAL_TUNER_BUSY_WINDOW_MS
        ]
        busy_lines = [h.get("line") for h in busy_hits if h.get("line")]
        busy_lines = busy_lines[-10:]
        last_time = 0
        for hit in reversed(busy_hits):
            if hit.get("timeMs"):
                last_time = int(hit.get("timeMs"))
                break
        payload = {
            "tuner_busy": bool(busy_lines),
            "tuner_busy_lines": busy_lines,
            "tuner_busy_count": len(busy_hits),
            "tuner_busy_last_time_ms": last_time,
        }
        payload.update(self._playlist_source_summary())
        payload.update(self._listen_filter_summary())
        payload.update(self._control_channel_summary())
        return payload

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

    def _list_profile_dirs(self):
        profiles = []
        base = self._profiles_dir
        if not base:
            return profiles
        try:
            entries = os.listdir(base)
        except Exception:
            return profiles
        for name in entries:
            if not validate_digital_profile_id(name):
                continue
            path = os.path.join(base, name)
            if os.path.isdir(path):
                profiles.append(name)
        return sorted(profiles)

    def listProfiles(self):
        return self._list_profile_dirs()

    def _read_active_profile_id(self):
        link = self._active_link
        if not link:
            return ""
        if not os.path.islink(link):
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

    @staticmethod
    def _read_control_channels(profile_dir: str) -> list[int]:
        path = os.path.join(profile_dir, "control_channels.txt")
        if not os.path.isfile(path):
            return []
        channels = []
        seen = set()
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    raw = line.split("#", 1)[0].strip()
                    if not raw:
                        continue
                    m = re.search(r"\d+\.\d+", raw)
                    if not m:
                        continue
                    try:
                        hz = int(round(float(m.group(0)) * 1_000_000))
                    except Exception:
                        continue
                    if hz <= 0 or hz in seen:
                        continue
                    seen.add(hz)
                    channels.append(hz)
        except Exception:
            return []
        return channels

    def _apply_profile_runtime(self, profile_dir: str, profile_id: str):
        control_channels = self._read_control_channels(profile_dir)
        if not control_channels:
            return False, "profile has no control channels"

        decoder_mode, _decoder_source = _profile_decoder_mode(profile_dir)
        if decoder_mode == "NXDN":
            return False, (
                "profile requires NXDN decode, but current SDRTrunk runtime "
                "supports P25/DMR only"
            )

        playlist_path = _safe_realpath(DIGITAL_PLAYLIST_PATH)
        if not playlist_path:
            return False, "digital playlist path not configured"
        if not os.path.isfile(playlist_path):
            return False, f"playlist not found: {playlist_path}"

        try:
            tree = ET.parse(playlist_path)
            root = tree.getroot()
        except Exception as e:
            return False, f"failed to parse playlist: {e}"

        channel = root.find("channel")
        if channel is None:
            channel = ET.SubElement(
                root,
                "channel",
                {
                    "system": "DMR" if decoder_mode == "DMR" else "P25",
                    "name": profile_id,
                    "enabled": "true",
                    "order": "1",
                },
            )

        channel.set("enabled", "true")
        channel.set("system", "DMR" if decoder_mode == "DMR" else "P25")
        channel.set("name", profile_id)

        event_conf = channel.find("event_log_configuration")
        if event_conf is None:
            event_conf = ET.SubElement(channel, "event_log_configuration")
        existing_loggers = {
            str(logger.text or "").strip()
            for logger in event_conf.findall("logger")
        }
        for logger_name in ("CALL_EVENT", "TRAFFIC_CALL_EVENT", "DECODED_MESSAGE"):
            if logger_name not in existing_loggers:
                logger = ET.SubElement(event_conf, "logger")
                logger.text = logger_name

        source_conf = channel.find("source_configuration")
        if source_conf is None:
            source_conf = ET.SubElement(channel, "source_configuration")
        _sync_source_configuration(source_conf, control_channels)

        # Allow profile-local alias list override so sub-profiles can reuse an
        # existing SDRTrunk alias list without requiring duplicate exports.
        alias_list_name = profile_id.upper()
        alias_name_path = os.path.join(profile_dir, "alias_list_name.txt")
        if os.path.isfile(alias_name_path):
            try:
                with open(alias_name_path, "r", encoding="utf-8", errors="ignore") as f:
                    for raw in f:
                        value = str(raw or "").strip()
                        if value:
                            alias_list_name = value
                            break
            except Exception:
                alias_list_name = profile_id.upper()

        alias_list = channel.find("alias_list_name")
        if alias_list is None:
            alias_list = ET.SubElement(channel, "alias_list_name")
        alias_list.text = alias_list_name
        _seed_alias_list_from_profile(root, alias_list_name, profile_dir)
        _ensure_alias_broadcast_channel(root, alias_list_name)

        _apply_decode_configuration(channel, decoder_mode)
        if channel.find("record_configuration") is None:
            ET.SubElement(channel, "record_configuration")
        _sync_stream_configuration(root)

        try:
            tree.write(playlist_path, encoding="utf-8", xml_declaration=False)
        except Exception as e:
            return False, f"failed to write playlist: {e}"
        return True, ""

    def _load_talkgroup_map(self) -> dict:
        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            self._tg_map = {}
            self._tg_map_profile = ""
            self._tg_map_mtime = None
            return self._tg_map
        if profile_dir == self._tg_map_profile and self._tg_map_mtime:
            try:
                if os.path.getmtime(self._tg_map_mtime[0]) == self._tg_map_mtime[1]:
                    return self._tg_map
            except Exception:
                pass
        candidates = ["talkgroups.csv", "talkgroups_with_group.csv"]
        path = ""
        for name in candidates:
            candidate = os.path.join(profile_dir, name)
            if os.path.isfile(candidate):
                path = candidate
                break
        if not path:
            self._tg_map = {}
            self._tg_map_profile = profile_dir
            self._tg_map_mtime = None
            return self._tg_map
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = None
        if self._tg_map_profile == profile_dir and self._tg_map_mtime and self._tg_map_mtime[0] == path:
            if mtime == self._tg_map_mtime[1]:
                return self._tg_map
        tg_map = {}
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not row:
                        continue
                    row_norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
                    dec = row_norm.get("dec") or row_norm.get("decimal") or ""
                    if not dec.isdigit():
                        continue
                    alpha = row_norm.get("alpha tag") or row_norm.get("alpha_tag") or ""
                    desc = row_norm.get("description") or row_norm.get("desc") or ""
                    # Prefer RR Description unless it is an auto-placeholder.
                    if _is_auto_placeholder_label(desc):
                        label = alpha if not _is_auto_placeholder_label(alpha) else f"TG {dec}"
                    else:
                        label = desc or alpha
                    if _is_auto_placeholder_label(label):
                        label = f"TG {dec}"
                    if label:
                        tg_map[dec] = label
        except Exception:
            tg_map = {}
        self._tg_map = tg_map
        self._tg_map_profile = profile_dir
        self._tg_map_mtime = (path, mtime)
        return self._tg_map

    def _load_listen_map(self) -> dict:
        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            self._listen_map = {}
            self._listen_map_profile = ""
            self._listen_map_mtime = None
            self._listen_default = bool(_DEFAULT_LISTEN_ENABLED)
            return self._listen_map
        listen_path = os.path.join(profile_dir, _LISTEN_FILENAME)
        if not os.path.isfile(listen_path):
            self._listen_map = {}
            self._listen_map_profile = profile_dir
            self._listen_map_mtime = None
            self._listen_default = bool(_DEFAULT_LISTEN_ENABLED)
            return self._listen_map
        try:
            mtime = os.path.getmtime(listen_path)
        except Exception:
            mtime = None
        if self._listen_map_profile == profile_dir and self._listen_map_mtime and self._listen_map_mtime[0] == listen_path:
            if mtime == self._listen_map_mtime[1]:
                return self._listen_map
        mapping = {}
        default_listen = bool(_DEFAULT_LISTEN_ENABLED)
        mapping, default_listen, _ = _read_listen_config(listen_path)
        self._listen_map = mapping
        self._listen_map_profile = profile_dir
        self._listen_map_mtime = (listen_path, mtime)
        self._listen_default = bool(default_listen)
        return self._listen_map

    def _map_event_label(self, event: dict) -> dict:
        if not event:
            return event
        label = str(event.get("label") or "").strip()
        raw = str(event.get("raw") or "")
        tgid = str(event.get("tgid") or "").strip()
        tg_map = self._load_talkgroup_map()
        if not tg_map:
            return event
        if not tgid:
            if label:
                tgid = _extract_tgid(label)
            if not tgid and raw:
                tgid = _extract_tgid(raw)
        if not tgid:
            if not self._listen_default:
                event = dict(event)
                event["muted"] = True
            return event
        if tgid:
            listen_map = self._load_listen_map()
            listen = listen_map.get(tgid, self._listen_default) if listen_map else self._listen_default
            if not listen:
                event = dict(event)
                event["muted"] = True
                event["tgid"] = tgid
                return event
            event = dict(event)
            event["tgid"] = tgid
            mapped_label = tg_map.get(tgid)
            if mapped_label:
                event["label"] = mapped_label
            else:
                current = str(event.get("label") or "").strip()
                if re.fullmatch(r"\(?\d+\)?", current):
                    event["label"] = f"TG {tgid}"
                elif not current:
                    event["label"] = f"TG {tgid}"
        return event

    def getProfile(self):
        current = self._read_active_profile_id()
        if current:
            self._profile = current
        return self._profile

    def setProfile(self, profileId: str, *, restart_service: bool = True):
        pid = _normalize_name(profileId)
        if not validate_digital_profile_id(pid):
            self._set_last_error("invalid profileId")
            return False, "invalid profileId"
        base = _safe_realpath(self._profiles_dir)
        target_dir = _safe_realpath(os.path.join(self._profiles_dir, pid))
        if not base or not target_dir.startswith(base + os.sep):
            self._set_last_error("invalid profile path")
            return False, "invalid profile path"
        if not os.path.isdir(target_dir):
            self._set_last_error("unknown profileId")
            return False, "unknown profileId"
        link = self._active_link
        if not link:
            self._set_last_error("active profile link not configured")
            return False, "active profile link not configured"
        previous_target = ""
        if os.path.islink(link):
            previous_target = _safe_realpath(link)
        link_dir = os.path.dirname(link) or "."
        try:
            os.makedirs(link_dir, exist_ok=True)
        except Exception:
            pass
        if os.path.exists(link) and not os.path.islink(link):
            self._set_last_error("active profile link is not a symlink")
            return False, "active profile link is not a symlink"
        tmp_link = f"{link}.tmp"
        try:
            if os.path.exists(tmp_link):
                os.remove(tmp_link)
            os.symlink(target_dir, tmp_link)
            os.replace(tmp_link, link)
        except Exception as e:
            self._set_last_error(str(e))
            return False, str(e)

        ok, err = self._apply_profile_runtime(target_dir, pid)
        if not ok:
            if previous_target and os.path.isdir(previous_target):
                restore_tmp = f"{link}.restore.tmp"
                try:
                    if os.path.exists(restore_tmp):
                        os.remove(restore_tmp)
                    os.symlink(previous_target, restore_tmp)
                    os.replace(restore_tmp, link)
                except Exception:
                    pass
            self._set_last_error(err or "runtime profile apply failed")
            return False, err or "runtime profile apply failed"

        # Force reload of talkgroup/listen maps after profile change.
        self._tg_map_profile = ""
        self._tg_map_mtime = None
        self._listen_map_profile = ""
        self._listen_map_mtime = None
        self._event_log_files_cache = []
        self._event_log_files_cache_at = 0.0
        self._event_log_files_cache_ready = False
        self._decoded_log_files_cache = []
        self._decoded_log_files_cache_at = 0.0
        self._decoded_log_files_cache_ready = False
        self._event_log_offsets = {}
        self._event_log_headers = {}

        self._profile = pid
        if restart_service:
            ok, err = self.restart()
            if not ok:
                return False, err or "restart failed"
        self._clear_error()
        return True, ""

    def getLastEvent(self):
        self._refresh_log_cache()
        event = super().getLastEvent()
        if not event:
            return {"label": "", "timeMs": 0}
        mapped = self._map_event_label(dict(event))
        if mapped and not mapped.get("muted"):
            return mapped

        # Fall back to the newest unmuted cached event when listen settings changed.
        for candidate in reversed(super().getRecentEvents(self._recent_limit)):
            mapped_candidate = self._map_event_label(dict(candidate))
            if mapped_candidate and not mapped_candidate.get("muted"):
                return mapped_candidate
        return {"label": "", "timeMs": 0}

    def getLastError(self):
        self._refresh_log_cache()
        return super().getLastError()

    def getLastWarning(self):
        self._refresh_log_cache()
        return super().getLastWarning()

    def getRecentEvents(self, limit: int = 20):
        self._refresh_log_cache()
        items = super().getRecentEvents(max(1, int(limit or 20)))
        filtered = []
        for item in items:
            mapped = self._map_event_label(dict(item))
            if not mapped or mapped.get("muted"):
                continue
            filtered.append(mapped)
        if len(filtered) > int(limit or 20):
            filtered = filtered[-int(limit or 20):]
        return filtered


class DigitalManager:
    """Selects and owns exactly one digital adapter."""

    def __init__(self, backend: str | None = None):
        selected = (backend or DIGITAL_BACKEND or "sdrtrunk").strip().lower()
        if not selected:
            selected = "sdrtrunk"
        self._backend = selected
        self._adapter = self._build_adapter(selected)
        self._scheduler_mode = (
            DIGITAL_SCAN_MODE
            if DIGITAL_SCAN_MODE in ("single_system", "timeslice_multi_system")
            else "single_system"
        )
        self._scheduler_dwell_ms = max(1000, int(DIGITAL_SYSTEM_DWELL_MS or 15000))
        self._scheduler_hang_ms = max(0, int(DIGITAL_SYSTEM_HANG_MS or 4000))
        self._scheduler_pause_on_hit = bool(DIGITAL_PAUSE_ON_HIT)
        self._scheduler_order = [str(x).strip() for x in (DIGITAL_SYSTEM_ORDER or []) if str(x).strip()]
        self._scheduler_state_path = str(DIGITAL_SCHEDULER_STATE_PATH or "").strip()
        self._scheduler_profile = ""
        self._scheduler_systems: list[str] = []
        self._scheduler_active_system = ""
        self._scheduler_last_switch_time_ms = 0
        self._scheduler_switch_reason = "manual"
        self._scheduler_in_call_hold = False
        self._scheduler_last_applied_system = ""
        self._scheduler_last_apply_time_ms = 0
        self._scheduler_last_apply_attempt_ms = 0
        self._scheduler_last_apply_error = ""
        self._scheduler_last_apply_error_system = ""
        self._scheduler_lock_loss_ms = int(_DIGITAL_SCHEDULER_LOCK_LOSS_MS)
        self._scheduler_system_health: dict[str, dict] = {}
        self._scheduler_lock = threading.Lock()
        self._scheduler_stop = threading.Event()
        self._load_scheduler_state()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="digital-scheduler-loop",
            daemon=True,
        )
        self._scheduler_thread.start()
        self._scheduler_tick()

    @staticmethod
    def _build_adapter(backend: str):
        if backend in ("sdrtrunk",):
            return SdrtrunkAdapter()
        if backend in ("none", "disabled", "off"):
            return NullDigitalAdapter("digital backend disabled")
        return NullDigitalAdapter(f"unknown digital backend: {backend}")

    def backend(self):
        return self._backend

    def start(self):
        return self._adapter.start()

    def stop(self):
        return self._adapter.stop()

    def restart(self):
        return self._adapter.restart()

    def isActive(self):
        return self._adapter.isActive()

    def listProfiles(self):
        return self._adapter.listProfiles()

    def getProfile(self):
        return self._adapter.getProfile()

    def setProfile(self, profileId: str, *, restart_service: bool = True):
        ok, err = self._adapter.setProfile(profileId, restart_service=restart_service)
        if ok:
            with self._scheduler_lock:
                local_systems = self._discover_profile_local_systems(str(profileId or "").strip())
                profile_key = str(profileId or "").strip().lower()
                ordered_keys = {
                    str(item or "").strip().lower()
                    for item in (self._scheduler_order or [])
                    if str(item or "").strip()
                }
                # HomePatrol-style default: if this profile clearly defines two or
                # more local systems, immediately run in timeslice mode across
                # those systems.
                if len(local_systems) >= 2:
                    self._scheduler_mode = "timeslice_multi_system"
                    self._scheduler_order = list(local_systems)
                # Otherwise prevent stale cross-profile scheduler state from
                # pinning a newly selected profile in "searching".
                elif (
                    self._scheduler_mode == "timeslice_multi_system"
                    and profile_key
                    and profile_key not in ordered_keys
                ):
                    self._scheduler_mode = "single_system"
                    self._scheduler_order = [str(profileId).strip()]
                self._scheduler_profile = ""
                self._scheduler_switch_reason = "manual"
                self._scheduler_last_switch_time_ms = int(time.time() * 1000)
                self._scheduler_last_applied_system = ""
                self._scheduler_last_apply_error = ""
                self._scheduler_last_apply_error_system = ""
                self._scheduler_system_health = {}
                self._write_scheduler_state()
            self._scheduler_tick()
        return ok, err

    def getLastEvent(self):
        return self._adapter.getLastEvent()

    def getLastError(self):
        return self._adapter.getLastError()

    def getLastWarning(self):
        return self._adapter.getLastWarning()
    def getRecentEvents(self, limit: int = 20):
        return self._adapter.getRecentEvents(limit)
    def preflight(self):
        if hasattr(self._adapter, "preflight"):
            try:
                return self._adapter.preflight()
            except Exception:
                return {"tuner_busy": False, "tuner_busy_lines": []}
        return {"tuner_busy": False, "tuner_busy_lines": []}

    def _scheduler_tick(self):
        try:
            preflight = self.preflight() or {}
            event = self.getLastEvent() or {}
            with self._scheduler_lock:
                self._scheduler_payload(event, preflight)
        except Exception:
            return

    def _scheduler_loop(self):
        while not self._scheduler_stop.wait(_DIGITAL_SCHEDULER_TICK_SEC):
            self._scheduler_tick()

    def _scheduler_state_payload(self) -> dict:
        return {
            "mode": str(self._scheduler_mode or "single_system"),
            "system_dwell_ms": int(self._scheduler_dwell_ms),
            "system_hang_ms": int(self._scheduler_hang_ms),
            "pause_on_hit": bool(self._scheduler_pause_on_hit),
            "system_order": list(self._scheduler_order),
            "updated_ts": int(time.time()),
        }

    def _write_scheduler_state(self) -> None:
        path = str(self._scheduler_state_path or "").strip()
        if not path:
            return
        tmp = f"{path}.tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._scheduler_state_payload(), f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _load_scheduler_state(self) -> None:
        path = str(self._scheduler_state_path or "").strip()
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                payload = json.load(f)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        mode_raw = payload.get("mode", payload.get("digital_scan_mode"))
        if mode_raw is not None:
            mode = str(mode_raw or "").strip().lower()
            if mode in ("single_system", "timeslice_multi_system"):
                self._scheduler_mode = mode

        dwell_raw = payload.get("system_dwell_ms", payload.get("digital_system_dwell_ms"))
        if dwell_raw is not None:
            try:
                self._scheduler_dwell_ms = self._parse_scheduler_int(
                    dwell_raw,
                    field="system_dwell_ms",
                    minimum=1000,
                    maximum=3600000,
                )
            except ValueError:
                pass

        hang_raw = payload.get("system_hang_ms", payload.get("digital_system_hang_ms"))
        if hang_raw is not None:
            try:
                self._scheduler_hang_ms = self._parse_scheduler_int(
                    hang_raw,
                    field="system_hang_ms",
                    minimum=0,
                    maximum=3600000,
                )
            except ValueError:
                pass

        pause_raw = payload.get("pause_on_hit", payload.get("digital_pause_on_hit"))
        if pause_raw is not None:
            try:
                self._scheduler_pause_on_hit = self._parse_scheduler_bool(pause_raw)
            except ValueError:
                pass

        order_raw = payload.get("system_order", payload.get("digital_system_order"))
        if order_raw is not None:
            self._scheduler_order = self._parse_scheduler_order(order_raw)

    @staticmethod
    def _parse_scheduler_bool(raw) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        text = str(raw or "").strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError("invalid boolean")

    @staticmethod
    def _parse_scheduler_int(raw, *, field: str, minimum: int, maximum: int) -> int:
        try:
            value = int(str(raw).strip())
        except Exception:
            raise ValueError(f"invalid {field}") from None
        if value < minimum:
            raise ValueError(f"{field} must be >= {minimum}")
        if value > maximum:
            raise ValueError(f"{field} must be <= {maximum}")
        return value

    @staticmethod
    def _parse_scheduler_order(raw) -> list[str]:
        tokens: list[str] = []
        if raw is None:
            return tokens
        if isinstance(raw, list):
            incoming = raw
        else:
            text = str(raw or "")
            incoming = text.replace(";", ",").replace("\n", ",").split(",")
        seen: set[str] = set()
        for item in incoming:
            value = str(item or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(value)
            if len(tokens) >= 64:
                break
        return tokens

    @staticmethod
    def _read_control_channel_groups_for_dir(profile_dir: str) -> list[tuple[str, list[int]]]:
        path = str(profile_dir or "").strip()
        if not path:
            return []
        control_path = os.path.join(path, "control_channels.txt")
        if not os.path.isfile(control_path):
            return []

        groups: list[tuple[str, list[int]]] = []
        current_name = ""
        current_channels: list[int] = []
        current_seen: set[int] = set()
        default_name = os.path.basename(path) or "default"

        def _flush_group() -> None:
            nonlocal current_name, current_channels, current_seen
            if current_name and current_channels:
                groups.append((current_name, list(current_channels)))
            current_name = ""
            current_channels = []
            current_seen = set()

        try:
            with open(control_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    raw = str(line or "").strip()
                    if not raw:
                        continue
                    if raw.startswith("#"):
                        label = raw.lstrip("#").strip()
                        if label:
                            _flush_group()
                            current_name = label
                        continue
                    m = re.search(r"\d+\.\d+", raw)
                    if not m:
                        continue
                    try:
                        hz = int(round(float(m.group(0)) * 1_000_000))
                    except Exception:
                        continue
                    if hz <= 0:
                        continue
                    if not current_name:
                        current_name = default_name
                    if hz in current_seen:
                        continue
                    current_seen.add(hz)
                    current_channels.append(hz)
        except Exception:
            return []

        _flush_group()
        return groups

    @staticmethod
    def _control_channel_value_to_hz(value) -> int:
        raw = str(value or "").strip()
        if not raw:
            return 0
        try:
            if re.fullmatch(r"\d+", raw):
                num = int(raw)
                if num >= 1_000_000:
                    return num
                if num > 100:
                    return int(round(float(num) * 1_000_000))
                return 0
            numf = float(raw)
            if numf >= 1_000_000:
                return int(round(numf))
            if numf > 100:
                return int(round(numf * 1_000_000))
        except Exception:
            return 0
        return 0

    @classmethod
    def _parse_system_control_channels(cls, raw_values) -> list[int]:
        if raw_values is None:
            return []
        if isinstance(raw_values, list):
            incoming = raw_values
        else:
            text = str(raw_values or "").replace("\n", ",").replace(";", ",")
            incoming = text.split(",")
        channels: list[int] = []
        seen: set[int] = set()
        for item in incoming:
            hz = cls._control_channel_value_to_hz(item)
            if hz <= 0 or hz in seen:
                continue
            seen.add(hz)
            channels.append(hz)
        return channels

    @classmethod
    def _read_system_definitions_for_dir(cls, profile_dir: str) -> list[tuple[str, list[int]]]:
        path = str(profile_dir or "").strip()
        if not path:
            return []
        systems_path = os.path.join(path, "systems.json")
        if not os.path.isfile(systems_path):
            return []
        try:
            with open(systems_path, "r", encoding="utf-8", errors="ignore") as f:
                payload = json.load(f)
        except Exception:
            return []

        if isinstance(payload, dict):
            systems_raw = payload.get("systems")
        else:
            systems_raw = payload
        if not isinstance(systems_raw, list):
            return []

        systems: list[tuple[str, list[int]]] = []
        seen: set[str] = set()
        for item in systems_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or item.get("system") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            channels_raw = (
                item.get("control_channels_hz")
                if item.get("control_channels_hz") is not None
                else (
                    item.get("control_channels_mhz")
                    if item.get("control_channels_mhz") is not None
                    else item.get("control_channels")
                )
            )
            if channels_raw is None:
                channels_raw = item.get("controls")
            channels = cls._parse_system_control_channels(channels_raw)
            if not channels:
                continue
            systems.append((name, channels))
        return systems

    def _read_control_channels_for_dir(self, profile_dir: str) -> list[int]:
        path = str(profile_dir or "").strip()
        if not path:
            return []
        explicit_systems = self._read_system_definitions_for_dir(path)
        if explicit_systems:
            channels: list[int] = []
            seen: set[int] = set()
            for _name, values in explicit_systems:
                for hz in values:
                    if hz <= 0 or hz in seen:
                        continue
                    seen.add(hz)
                    channels.append(hz)
            if channels:
                return channels
        read_channels = getattr(self._adapter, "_read_control_channels", None)
        if callable(read_channels):
            try:
                values = read_channels(path)
                if isinstance(values, list):
                    return [int(v) for v in values if int(v) > 0]
            except Exception:
                pass
        groups = self._read_control_channel_groups_for_dir(path)
        if not groups:
            return []
        channels: list[int] = []
        seen: set[int] = set()
        for _name, values in groups:
            for hz in values:
                if hz <= 0 or hz in seen:
                    continue
                seen.add(hz)
                channels.append(hz)
        return channels

    def _discover_profile_local_systems(self, profile_id: str) -> list[str]:
        systems: list[str] = []
        seen: set[str] = set()
        profile_key = str(profile_id or "").strip()

        def _add_system(raw_name: str) -> None:
            name = str(raw_name or "").strip()
            if not name:
                return
            key = name.lower()
            if key in seen:
                return
            seen.add(key)
            systems.append(name)

        profile_dir = ""
        read_active_profile_dir = getattr(self._adapter, "_read_active_profile_dir", None)
        if callable(read_active_profile_dir):
            try:
                profile_dir = str(read_active_profile_dir() or "").strip()
            except Exception:
                profile_dir = ""

        if profile_dir and os.path.isdir(profile_dir):
            explicit = self._read_system_definitions_for_dir(profile_dir)
            if explicit:
                for name, values in explicit:
                    if values:
                        _add_system(name)
            else:
                groups = self._read_control_channel_groups_for_dir(profile_dir)
                if len(groups) >= 2:
                    for name, values in groups:
                        if values:
                            _add_system(name)
                elif groups:
                    _add_system(profile_key or os.path.basename(profile_dir))

                subdirs = []
                try:
                    with os.scandir(profile_dir) as it:
                        for entry in it:
                            try:
                                if not entry.is_dir(follow_symlinks=False):
                                    continue
                            except Exception:
                                continue
                            subdirs.append(entry.name)
                except Exception:
                    subdirs = []
                for name in sorted(subdirs):
                    control_path = os.path.join(profile_dir, name, "control_channels.txt")
                    if os.path.isfile(control_path):
                        _add_system(name)

        if not systems and profile_key:
            _add_system(profile_key)
        return systems

    @staticmethod
    def _source_configuration_channels(source_conf: ET.Element) -> list[int]:
        frequencies: list[int] = []
        seen: set[int] = set()
        attr = str(source_conf.get("frequency", "")).strip()
        if attr.isdigit():
            hz = int(attr)
            if hz > 0 and hz not in seen:
                seen.add(hz)
                frequencies.append(hz)
        for node in source_conf.findall("frequency"):
            text = str(node.text or "").strip()
            if not text.isdigit():
                continue
            hz = int(text)
            if hz <= 0 or hz in seen:
                continue
            seen.add(hz)
            frequencies.append(hz)
        return frequencies

    def _resolve_scheduler_system_control_channels(self, profile_id: str, system_name: str) -> list[int]:
        system = str(system_name or "").strip()
        if not system:
            return []
        profile_dir = ""
        read_active_profile_dir = getattr(self._adapter, "_read_active_profile_dir", None)
        if callable(read_active_profile_dir):
            try:
                profile_dir = str(read_active_profile_dir() or "").strip()
            except Exception:
                profile_dir = ""
        if not profile_dir or not os.path.isdir(profile_dir):
            return []

        for name, values in self._read_system_definitions_for_dir(profile_dir):
            if str(name).strip().lower() == system.lower() and values:
                return list(values)

        for name, values in self._read_control_channel_groups_for_dir(profile_dir):
            if str(name).strip().lower() == system.lower() and values:
                return list(values)

        profile_key = str(profile_id or "").strip()
        if profile_key and system == profile_key:
            channels = self._read_control_channels_for_dir(profile_dir)
            if channels:
                return channels

        subdir = os.path.join(profile_dir, system)
        if os.path.isdir(subdir):
            channels = self._read_control_channels_for_dir(subdir)
            if channels:
                return channels

        # Also allow scheduler order entries to target sibling profile IDs so
        # we can time-slice across two standalone digital profiles.
        profiles_base = _safe_realpath(DIGITAL_PROFILES_DIR)
        sibling_profile = _safe_realpath(os.path.join(DIGITAL_PROFILES_DIR, system))
        if (
            profiles_base
            and sibling_profile
            and sibling_profile.startswith(profiles_base + os.sep)
            and os.path.isdir(sibling_profile)
        ):
            channels = self._read_control_channels_for_dir(sibling_profile)
            if channels:
                return channels

        return self._read_control_channels_for_dir(profile_dir)

    def _apply_scheduler_system(
        self,
        profile_id: str,
        system_name: str,
        *,
        force: bool = False,
    ) -> tuple[bool, str, bool]:
        now_ms = int(time.time() * 1000)
        if not force:
            delta = now_ms - int(self._scheduler_last_apply_attempt_ms or 0)
            if delta >= 0 and delta < _DIGITAL_SCHEDULER_APPLY_MIN_INTERVAL_MS:
                return True, "", False
        self._scheduler_last_apply_attempt_ms = now_ms

        channels = self._resolve_scheduler_system_control_channels(profile_id, system_name)
        if not channels:
            self._scheduler_last_apply_error = f"system has no control channels: {system_name}"
            self._scheduler_last_apply_error_system = system_name
            return False, self._scheduler_last_apply_error, False

        playlist_path = _safe_realpath(DIGITAL_PLAYLIST_PATH)
        if not playlist_path:
            self._scheduler_last_apply_error = "digital playlist path not configured"
            self._scheduler_last_apply_error_system = system_name
            return False, self._scheduler_last_apply_error, False
        if not os.path.isfile(playlist_path):
            self._scheduler_last_apply_error = f"playlist not found: {playlist_path}"
            self._scheduler_last_apply_error_system = system_name
            return False, self._scheduler_last_apply_error, False

        try:
            tree = ET.parse(playlist_path)
            root = tree.getroot()
        except Exception as e:
            self._scheduler_last_apply_error = f"failed to parse playlist: {e}"
            self._scheduler_last_apply_error_system = system_name
            return False, self._scheduler_last_apply_error, False

        channel = root.find("channel")
        if channel is None:
            self._scheduler_last_apply_error = "playlist has no channel node"
            self._scheduler_last_apply_error_system = system_name
            return False, self._scheduler_last_apply_error, False
        source_conf = channel.find("source_configuration")
        if source_conf is None:
            source_conf = ET.SubElement(channel, "source_configuration")

        before_channels = self._source_configuration_channels(source_conf)
        before_source_type = str(source_conf.get("source_type", "")).strip().upper()
        before_preferred = str(source_conf.get("preferred_tuner", "")).strip()
        preferred = _preferred_tuner_target()
        expected_source_type = (
            "TUNER_MULTIPLE_FREQUENCIES"
            if DIGITAL_USE_MULTI_FREQ_SOURCE and len(channels) > 1
            else "TUNER"
        )

        source_unchanged = (
            before_channels == channels
            and before_source_type == expected_source_type
            and before_preferred == preferred
        )
        stream_changed = _sync_stream_configuration(root)

        if source_unchanged and not stream_changed:
            self._scheduler_last_applied_system = system_name
            self._scheduler_last_apply_time_ms = now_ms
            self._scheduler_last_apply_error = ""
            self._scheduler_last_apply_error_system = ""
            return True, "", False

        if not source_unchanged:
            _sync_source_configuration(source_conf, channels)
        try:
            tree.write(playlist_path, encoding="utf-8", xml_declaration=False)
        except Exception as e:
            self._scheduler_last_apply_error = f"failed to write playlist: {e}"
            self._scheduler_last_apply_error_system = system_name
            return False, self._scheduler_last_apply_error, False

        self._scheduler_last_applied_system = system_name
        self._scheduler_last_apply_time_ms = now_ms
        self._scheduler_last_apply_error = ""
        self._scheduler_last_apply_error_system = ""
        return True, "", True

    def getScheduler(self) -> dict:
        preflight = self.preflight() or {}
        event = self.getLastEvent() or {}
        with self._scheduler_lock:
            payload = self._scheduler_payload(event, preflight)
            payload["digital_scheduler_systems"] = list(self._scheduler_systems)
            payload["digital_scheduler_profile"] = str(self.getProfile() or "")
            payload["digital_scheduler_applied_system"] = self._scheduler_last_applied_system
            payload["digital_scheduler_last_apply_time"] = int(self._scheduler_last_apply_time_ms or 0)
            if self._scheduler_last_apply_error:
                payload["digital_scheduler_last_apply_error"] = self._scheduler_last_apply_error
        return payload

    def _scheduler_health_entry(self, system_name: str) -> dict:
        key = str(system_name or "").strip().lower()
        if not key:
            return {}
        entry = self._scheduler_system_health.get(key)
        if not isinstance(entry, dict):
            entry = {"name": str(system_name or "").strip()}
            self._scheduler_system_health[key] = entry
        if not entry.get("name"):
            entry["name"] = str(system_name or "").strip()
        return entry

    def _scheduler_system_health_payload(
        self,
        systems: list[str],
        active_system: str,
        mode: str,
        preflight: dict,
        now_ms: int,
        lock_timeout_ms: int,
    ) -> list[dict]:
        allowed = {str(name or "").strip().lower() for name in systems if str(name or "").strip()}
        for key in list(self._scheduler_system_health.keys()):
            if key not in allowed:
                self._scheduler_system_health.pop(key, None)

        metric_ready = bool(preflight.get("control_decode_available"))
        control_locked = bool(preflight.get("control_channel_locked"))
        lock_fail_count = int(preflight.get("control_lock_fail_count") or 0)
        window_sec = max(1, int(int(preflight.get("control_window_ms") or _DIGITAL_CONTROL_WINDOW_MS) / 1000))
        tuner_busy = bool(preflight.get("tuner_busy"))
        rows: list[dict] = []

        for name in systems:
            entry = self._scheduler_health_entry(name)
            if not entry:
                continue
            is_active = name == active_system
            state = "standby"
            reason = "timeslice standby" if mode == "timeslice_multi_system" else "inactive"

            if is_active:
                elapsed_ms = now_ms - int(self._scheduler_last_switch_time_ms or 0)
                if not self.isActive():
                    state = "failed"
                    reason = "decoder stopped"
                elif (
                    self._scheduler_last_apply_error
                    and str(self._scheduler_last_apply_error_system or "").strip().lower() == name.lower()
                ):
                    state = "failed"
                    reason = str(self._scheduler_last_apply_error)
                elif metric_ready:
                    if control_locked:
                        state = "locked"
                        reason = "control decode active"
                        entry["last_lock_time_ms"] = now_ms
                        entry["lock_failures"] = 0
                    else:
                        if lock_fail_count > 0:
                            state = "degraded"
                            reason = f"decoder lock failures ({lock_fail_count}/{window_sec}s)"
                        elif elapsed_ms >= lock_timeout_ms:
                            state = "degraded"
                            reason = f"no control lock after {int(elapsed_ms / 1000)}s"
                        else:
                            state = "searching"
                            reason = "acquiring control lock"
                else:
                    state = "inferred"
                    reason = "control metric unavailable"
                if tuner_busy and state in ("locked", "searching", "inferred"):
                    state = "degraded"
                    reason = "tuner contention"

            rows.append(
                {
                    "name": name,
                    "active": bool(is_active),
                    "state": state,
                    "reason": reason,
                    "lock_failures": int(entry.get("lock_failures") or 0),
                    "last_lock_time": int(entry.get("last_lock_time_ms") or 0),
                    "last_lock_loss_time": int(entry.get("last_lock_loss_time_ms") or 0),
                }
            )
        return rows

    def setScheduler(self, payload: dict) -> tuple[bool, str, dict]:
        if not isinstance(payload, dict):
            return False, "invalid scheduler payload", {}

        mode_raw = payload.get("mode", payload.get("digital_scan_mode"))
        dwell_raw = payload.get("system_dwell_ms", payload.get("digital_system_dwell_ms"))
        hang_raw = payload.get("system_hang_ms", payload.get("digital_system_hang_ms"))
        pause_raw = payload.get("pause_on_hit", payload.get("digital_pause_on_hit"))
        order_raw = payload.get("system_order", payload.get("digital_system_order"))

        with self._scheduler_lock:
            if mode_raw is not None:
                mode = str(mode_raw or "").strip().lower()
                if mode not in ("single_system", "timeslice_multi_system"):
                    return False, "invalid mode", {}
                self._scheduler_mode = mode

            if dwell_raw is not None:
                try:
                    self._scheduler_dwell_ms = self._parse_scheduler_int(
                        dwell_raw,
                        field="system_dwell_ms",
                        minimum=1000,
                        maximum=3600000,
                    )
                except ValueError as e:
                    return False, str(e), {}

            if hang_raw is not None:
                try:
                    self._scheduler_hang_ms = self._parse_scheduler_int(
                        hang_raw,
                        field="system_hang_ms",
                        minimum=0,
                        maximum=3600000,
                    )
                except ValueError as e:
                    return False, str(e), {}

            if pause_raw is not None:
                try:
                    self._scheduler_pause_on_hit = self._parse_scheduler_bool(pause_raw)
                except ValueError:
                    return False, "invalid pause_on_hit", {}

            if order_raw is not None:
                self._scheduler_order = self._parse_scheduler_order(order_raw)

            self._scheduler_profile = ""
            self._scheduler_switch_reason = "manual"
            self._scheduler_last_switch_time_ms = int(time.time() * 1000)
            self._scheduler_in_call_hold = False
            self._scheduler_last_applied_system = ""
            self._scheduler_last_apply_error = ""
            self._scheduler_last_apply_error_system = ""
            self._scheduler_system_health = {}
            self._write_scheduler_state()

        snapshot = self.getScheduler()
        return True, "", snapshot

    def _discover_scheduler_systems(self, profile_id: str) -> list[str]:
        systems: list[str] = list(self._discover_profile_local_systems(profile_id))
        seen: set[str] = {str(name).strip().lower() for name in systems if str(name).strip()}

        # If scheduler order references standalone profile IDs, include them
        # as scan targets when they have usable control channel definitions.
        profiles_base = _safe_realpath(DIGITAL_PROFILES_DIR)
        if profiles_base and os.path.isdir(profiles_base):
            for token in self._scheduler_order:
                name = str(token or "").strip()
                if not name:
                    continue
                candidate = _safe_realpath(os.path.join(profiles_base, name))
                if not candidate or not candidate.startswith(profiles_base + os.sep):
                    continue
                if not os.path.isdir(candidate):
                    continue
                if name.lower() in seen:
                    continue
                if self._read_control_channels_for_dir(candidate):
                    seen.add(name.lower())
                    systems.append(name)

        if not self._scheduler_order:
            return systems

        rank = {
            name.lower(): idx
            for idx, name in enumerate(self._scheduler_order)
        }
        ordered = sorted(
            systems,
            key=lambda name: (rank.get(name.lower(), len(rank)), name.lower()),
        )
        return ordered

    @staticmethod
    def _next_system(systems: list[str], current: str) -> str:
        if not systems:
            return ""
        if current not in systems:
            return systems[0]
        idx = systems.index(current)
        return systems[(idx + 1) % len(systems)]

    def _scheduler_payload(self, event: dict, preflight: dict) -> dict:
        now_ms = int(time.time() * 1000)
        profile_id = str(self.getProfile() or "").strip()
        systems = self._discover_scheduler_systems(profile_id)
        pending_apply = False
        pending_reason = ""
        recovery_system = ""
        lock_timeout_ms = max(2000, min(int(self._scheduler_dwell_ms), int(self._scheduler_lock_loss_ms)))

        configured_mode = self._scheduler_mode
        mode = configured_mode
        auto_enabled_multi = False
        if configured_mode == "single_system" and len(systems) >= 2:
            mode = "timeslice_multi_system"
            auto_enabled_multi = True
            self._scheduler_mode = "timeslice_multi_system"
            if not self._scheduler_order:
                self._scheduler_order = list(systems)
            configured_mode = self._scheduler_mode
            self._write_scheduler_state()
        if configured_mode == "timeslice_multi_system" and len(systems) < 2:
            mode = "single_system"

        systems_changed = systems != self._scheduler_systems
        profile_changed = profile_id != self._scheduler_profile
        active_missing = self._scheduler_active_system not in systems if systems else False
        if systems_changed or profile_changed or active_missing:
            self._scheduler_systems = list(systems)
            self._scheduler_profile = profile_id
            self._scheduler_active_system = systems[0] if systems else ""
            self._scheduler_last_switch_time_ms = now_ms if self._scheduler_active_system else 0
            self._scheduler_switch_reason = "auto_profile_multi" if auto_enabled_multi else "manual"
            self._scheduler_in_call_hold = False
            pending_apply = bool(self._scheduler_active_system)
            pending_reason = "auto_profile_multi" if auto_enabled_multi else "manual"

        event_time_ms = int(event.get("timeMs") or 0)
        recent_event = event_time_ms > 0 and (now_ms - event_time_ms) <= self._scheduler_hang_ms
        metric_ready = bool(preflight.get("control_decode_available"))
        control_locked = bool(preflight.get("control_channel_locked"))
        if self._scheduler_pause_on_hit and recent_event:
            self._scheduler_in_call_hold = True

        if mode == "timeslice_multi_system" and len(systems) > 1 and self._scheduler_active_system:
            in_hold_window = (
                self._scheduler_pause_on_hit
                and event_time_ms > 0
                and (now_ms - event_time_ms) <= self._scheduler_hang_ms
            )
            if in_hold_window:
                self._scheduler_in_call_hold = True
            else:
                should_switch = False
                switch_reason = "idle_timeout"
                elapsed_ms = now_ms - int(self._scheduler_last_switch_time_ms or 0)
                if self._scheduler_in_call_hold:
                    should_switch = True
                    switch_reason = "call_end"
                    self._scheduler_in_call_hold = False
                elif metric_ready and not control_locked and elapsed_ms >= lock_timeout_ms:
                    should_switch = True
                    switch_reason = "lock_timeout"
                elif elapsed_ms >= self._scheduler_dwell_ms:
                    should_switch = True
                    switch_reason = "idle_timeout"
                if should_switch:
                    previous = str(self._scheduler_active_system or "")
                    candidate = self._next_system(
                        systems,
                        self._scheduler_active_system,
                    )
                    if candidate:
                        self._scheduler_active_system = candidate
                        self._scheduler_last_switch_time_ms = now_ms
                        self._scheduler_switch_reason = switch_reason
                        pending_apply = True
                        pending_reason = switch_reason
                        if switch_reason == "lock_timeout" and previous:
                            health = self._scheduler_health_entry(previous)
                            if health:
                                health["lock_failures"] = int(health.get("lock_failures") or 0) + 1
                                health["last_lock_loss_time_ms"] = now_ms
                        if previous and previous != candidate:
                            recovery_system = previous

        active_system = self._scheduler_active_system or (systems[0] if systems else "")
        if active_system and self._scheduler_last_applied_system != active_system:
            pending_apply = True
            if not pending_reason:
                pending_reason = "manual"

        if pending_apply and active_system:
            ok, _err, _changed = self._apply_scheduler_system(
                profile_id,
                active_system,
                force=True,
            )
            if not ok:
                self._scheduler_switch_reason = "error_recovery"
                if (
                    recovery_system
                    and recovery_system in systems
                    and recovery_system != active_system
                ):
                    self._scheduler_active_system = recovery_system
                    recovery_ok, _recovery_err, _recovery_changed = self._apply_scheduler_system(
                        profile_id,
                        recovery_system,
                        force=True,
                    )
                    if recovery_ok:
                        active_system = recovery_system
            elif pending_reason:
                self._scheduler_switch_reason = pending_reason

        next_system = self._next_system(systems, active_system) if len(systems) > 1 else active_system
        voice_tuner_available = bool(DIGITAL_RTL_SERIAL_SECONDARY and not preflight.get("tuner_busy"))

        payload = {
            "digital_scan_mode": configured_mode,
            "digital_system_dwell_ms": int(self._scheduler_dwell_ms),
            "digital_system_hang_ms": int(self._scheduler_hang_ms),
            "digital_system_order": list(self._scheduler_order),
            "digital_pause_on_hit": bool(self._scheduler_pause_on_hit),
            "digital_scheduler_mode": mode,
            "digital_scheduler_active_system": active_system,
            "digital_scheduler_next_system": next_system,
            "digital_scheduler_last_switch_time": int(self._scheduler_last_switch_time_ms or 0),
            "digital_scheduler_switch_reason": self._scheduler_switch_reason,
            "digital_scheduler_applied_system": self._scheduler_last_applied_system,
            "digital_scheduler_last_apply_time": int(self._scheduler_last_apply_time_ms or 0),
            "digital_scheduler_lock_timeout_ms": int(lock_timeout_ms),
            "digital_voice_tuner_available": voice_tuner_available,
        }
        payload["digital_scheduler_system_health"] = self._scheduler_system_health_payload(
            systems,
            active_system,
            mode,
            preflight,
            now_ms,
            lock_timeout_ms,
        )
        if self._scheduler_last_apply_error:
            payload["digital_scheduler_last_apply_error"] = self._scheduler_last_apply_error
        return payload

    def status_payload(self):
        event = self.getLastEvent() or {}
        label = str(event.get("label") or "")
        mode = event.get("mode")
        time_ms = int(event.get("timeMs") or 0)
        payload = {
            "digital_active": bool(self.isActive()),
            "digital_backend": self.backend(),
            "digital_profile": str(self.getProfile() or ""),
            "digital_muted": bool(get_digital_muted()),
            "digital_last_label": label,
            "digital_last_time": time_ms if time_ms > 0 else 0,
        }
        if mode:
            payload["digital_last_mode"] = str(mode)
        err = self.getLastError()
        if err:
            payload["digital_last_error"] = err
        warn = self.getLastWarning()
        if warn and not _suppress_status_warning(str(warn)):
            payload["digital_last_warning"] = warn
        preflight = self.preflight() or {}
        payload["digital_preflight"] = preflight
        payload["digital_tuner_busy_count"] = int(preflight.get("tuner_busy_count") or 0)
        payload["digital_tuner_busy_time"] = int(preflight.get("tuner_busy_last_time_ms") or 0)
        with self._scheduler_lock:
            payload.update(self._scheduler_payload(event, preflight))
        payload["digital_playlist_source_ok"] = bool(preflight.get("playlist_source_ok"))
        if "playlist_source_type" in preflight:
            payload["digital_playlist_source_type"] = preflight.get("playlist_source_type")
        if "playlist_source_config_type" in preflight:
            payload["digital_playlist_source_config_type"] = preflight.get("playlist_source_config_type")
        if "playlist_frequency_count" in preflight:
            payload["digital_playlist_frequency_count"] = int(preflight.get("playlist_frequency_count") or 0)
        if preflight.get("playlist_preferred_tuner"):
            payload["digital_playlist_preferred_tuner"] = str(preflight.get("playlist_preferred_tuner"))
        if preflight.get("playlist_source_error"):
            payload["digital_playlist_source_error"] = str(preflight.get("playlist_source_error"))
        payload["digital_control_channel_metric_ready"] = bool(preflight.get("control_decode_available"))
        payload["digital_control_channel_locked"] = bool(preflight.get("control_channel_locked"))
        payload["digital_control_channel_count"] = int(preflight.get("control_activity_count") or 0)
        payload["digital_control_channel_last_time"] = int(preflight.get("control_last_time_ms") or 0)
        payload["digital_control_sync_loss_count"] = int(preflight.get("control_sync_loss_count") or 0)
        payload["digital_control_lock_fail_count"] = int(preflight.get("control_lock_fail_count") or 0)
        payload["digital_control_lock_fail_last_time"] = int(preflight.get("control_lock_fail_last_time_ms") or 0)
        payload["digital_control_window_ms"] = int(preflight.get("control_window_ms") or 0)
        payload["digital_control_decode_files"] = int(preflight.get("control_decode_files") or 0)
        if "listen_talkgroup_count" in preflight:
            payload["digital_listen_talkgroup_count"] = int(preflight.get("listen_talkgroup_count") or 0)
        if "listen_enabled_count" in preflight:
            payload["digital_listen_enabled_count"] = int(preflight.get("listen_enabled_count") or 0)
        if "listen_filter_blocking" in preflight:
            payload["digital_listen_filter_blocking"] = bool(preflight.get("listen_filter_blocking"))
        if preflight.get("listen_filter_error"):
            payload["digital_listen_filter_error"] = str(preflight.get("listen_filter_error"))
        if preflight.get("tuner_busy"):
            air_serial = os.getenv("AIRBAND_RTL_SERIAL", "").strip()
            ground_serial = os.getenv("GROUND_RTL_SERIAL", "").strip()
            digital_serial = DIGITAL_RTL_SERIAL or ""
            digital_secondary = DIGITAL_RTL_SERIAL_SECONDARY or ""
            tuner_targets = _digital_tuner_targets()
            tuner_target_note = (
                ", ".join(tuner_targets)
                if tuner_targets else "auto"
            )
            serials_note = (
                f"expected serials: airband={air_serial or 'unknown'}, "
                f"ground={ground_serial or 'unknown'}, digital={digital_serial or 'unknown'}, "
                f"digital_secondary={digital_secondary or 'unset'}"
            )
            serial_note = f" (serial {DIGITAL_RTL_SERIAL})" if DIGITAL_RTL_SERIAL else ""
            msg = (
                f"SDRTrunk tuner busy{serial_note}: likely dongle conflict with rtl-airband; "
                f"{serials_note}. Preferred tuner targets: {tuner_target_note}. "
                f"In SDRTrunk, disable unrelated RTL tuners and bind control to {digital_serial or 'your digital dongle'}."
            )
            payload["digital_last_warning"] = msg
        elif (
            payload.get("digital_active")
            and not preflight.get("control_channel_locked")
            and int(preflight.get("control_lock_fail_count") or 0) > 0
        ):
            lock_fail_count = int(preflight.get("control_lock_fail_count") or 0)
            window_sec = max(1, int(int(preflight.get("control_window_ms") or _DIGITAL_CONTROL_WINDOW_MS) / 1000))
            freq_count = int(preflight.get("playlist_frequency_count") or 0)
            payload["digital_last_warning"] = (
                f"SDRTrunk reports control-channel lock failures ({lock_fail_count} in {window_sec}s). "
                f"Verify RF signal and control channels (configured={freq_count}), and confirm tuner/PPM calibration."
            )
        elif preflight.get("listen_filter_blocking"):
            payload["digital_last_warning"] = (
                "Digital listen filter currently blocks all talkgroups "
                f"(enabled={int(preflight.get('listen_enabled_count') or 0)} / "
                f"{int(preflight.get('listen_talkgroup_count') or 0)})."
            )
        elif not DIGITAL_RTL_SERIAL and not _preferred_tuner_target() and DIGITAL_RTL_SERIAL_HINT:
            payload.setdefault("digital_last_warning", DIGITAL_RTL_SERIAL_HINT)

        # Auto-clear stale error/warning once digital has recovered and is producing activity.
        if payload.get("digital_active") and not preflight.get("tuner_busy"):
            now_ms = int(time.time() * 1000)
            last_event_ms = int(payload.get("digital_last_time") or 0)
            err_time_ms = int(getattr(self._adapter, "_last_error_time_ms", 0) or 0)
            warn_time_ms = int(getattr(self._adapter, "_last_warning_time_ms", 0) or 0)
            last_warn_text = str(payload.get("digital_last_warning") or "")
            stale_event = (
                last_event_ms > 0
                and _DIGITAL_STATUS_CLEAR_MS > 0
                and (now_ms - last_event_ms) >= _DIGITAL_STATUS_CLEAR_MS
            )
            if stale_event:
                payload["digital_last_label"] = ""
                payload["digital_last_time"] = 0
                payload.pop("digital_last_mode", None)
            recovered_after_error = last_event_ms > 0 and err_time_ms > 0 and last_event_ms >= err_time_ms
            stale_error = err_time_ms > 0 and _DIGITAL_STATUS_CLEAR_MS > 0 and (now_ms - err_time_ms) >= _DIGITAL_STATUS_CLEAR_MS
            if recovered_after_error or stale_error:
                payload.pop("digital_last_error", None)
            recovered_after_warn = last_event_ms > 0 and warn_time_ms > 0 and last_event_ms >= warn_time_ms
            stale_warn = warn_time_ms > 0 and _DIGITAL_STATUS_CLEAR_MS > 0 and (now_ms - warn_time_ms) >= _DIGITAL_STATUS_CLEAR_MS
            if (
                (recovered_after_warn or stale_warn)
                and last_warn_text != DIGITAL_RTL_SERIAL_HINT
            ):
                payload.pop("digital_last_warning", None)
        return payload

    def __del__(self):
        try:
            self._scheduler_stop.set()
        except Exception:
            pass

    def isMuted(self):
        return get_digital_muted()

    def setMuted(self, muted: bool):
        return set_digital_muted(muted)


_MANAGER = None


def get_digital_manager():
    """Return the singleton DigitalManager."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = DigitalManager()
    return _MANAGER
