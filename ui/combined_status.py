"""Helpers for reading combined rtl_airband config state."""
import os
import re
from typing import Dict, List, Optional

try:
    from .config import COMBINED_CONFIG_PATH, AIRONLY_CONFIG_PATH, RTLAIRBAND_ACTIVE_CONFIG_PATH, AIRBAND_MIN_MHZ, AIRBAND_MAX_MHZ, GROUND_CONFIG_PATH
    from .profile_config import read_active_config_path
except ImportError:
    from ui.config import COMBINED_CONFIG_PATH, AIRONLY_CONFIG_PATH, RTLAIRBAND_ACTIVE_CONFIG_PATH, AIRBAND_MIN_MHZ, AIRBAND_MAX_MHZ, GROUND_CONFIG_PATH
    from ui.profile_config import read_active_config_path

RE_SERIAL = re.compile(r'serial\s*=\s*"([^"]+)"', re.I)
RE_INDEX = re.compile(r'index\s*=\s*(\d+)\s*;', re.I)
RE_GAIN = re.compile(r'gain\s*=\s*([0-9.]+)\s*;', re.I)
RE_SQUELCH = re.compile(r'squelch_threshold\s*=\s*(-?\d+)\s*;', re.I)
RE_FREQS_BLOCK = re.compile(r'freqs\s*=\s*\((.*?)\)\s*;', re.S | re.I)


def _extract_devices_section(text: str) -> str:
    idx = text.find("devices:")
    if idx == -1:
        return ""
    start = text.find("(", idx)
    if start == -1:
        return ""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return ""


def _split_device_blocks(section: str) -> List[str]:
    blocks = []
    depth = 0
    start = None
    for i, ch in enumerate(section):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(section[start:i + 1])
                start = None
    return blocks


def _parse_freqs(block: str) -> List[float]:
    freqs = []
    for match in RE_FREQS_BLOCK.finditer(block):
        for num in re.findall(r'(\d+(?:\.\d+)?)', match.group(1)):
            try:
                freqs.append(float(num))
            except ValueError:
                continue
    return freqs


def _freq_in_airband(freq: float) -> bool:
    return AIRBAND_MIN_MHZ <= freq <= AIRBAND_MAX_MHZ


def read_combined_devices(conf_path: str = RTLAIRBAND_ACTIVE_CONFIG_PATH) -> List[Dict]:
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except FileNotFoundError:
        return []
    section = _extract_devices_section(text)
    if not section:
        return []
    devices = []
    for block in _split_device_blocks(section):
        serial = None
        index = None
        gain = None
        squelch_dbfs = None
        m = RE_SERIAL.search(block)
        if m:
            serial = m.group(1).strip()
        m = RE_INDEX.search(block)
        if m:
            try:
                index = int(m.group(1))
            except ValueError:
                index = None
        m = RE_GAIN.search(block)
        if m:
            try:
                gain = float(m.group(1))
            except ValueError:
                gain = None
        m = RE_SQUELCH.search(block)
        if m:
            try:
                squelch_dbfs = float(m.group(1))
            except ValueError:
                squelch_dbfs = None
        freqs = _parse_freqs(block)
        is_airband = any(_freq_in_airband(f) for f in freqs)
        devices.append({
            "serial": serial,
            "index": index,
            "gain": gain,
            "squelch_dbfs": squelch_dbfs,
            "freqs": freqs,
            "is_airband": is_airband,
        })
    return devices


def combined_device_summary(conf_path: str = RTLAIRBAND_ACTIVE_CONFIG_PATH) -> Dict[str, Optional[Dict]]:
    devices = read_combined_devices(conf_path)
    airband = next((d for d in devices if d["is_airband"]), None)
    ground = next((d for d in devices if not d["is_airband"] and d["freqs"]), None)
    return {
        "devices": devices,
        "airband": airband,
        "ground": ground,
    }


def combined_config_stale(conf_path: str = RTLAIRBAND_ACTIVE_CONFIG_PATH) -> bool:
    try:
        combined_mtime = os.path.getmtime(conf_path)
    except FileNotFoundError:
        return True
    sources = [read_active_config_path(), os.path.realpath(GROUND_CONFIG_PATH)]
    for src in sources:
        try:
            if os.path.getmtime(src) > combined_mtime:
                return True
        except FileNotFoundError:
            continue
    return False
