"""Helpers for reading combined rtl_airband config state."""
import os
import re
from typing import Dict, List, Optional

try:
    from .config import (
        COMBINED_CONFIG_PATH,
        AIRBAND_MIN_MHZ,
        AIRBAND_MAX_MHZ,
        GROUND_CONFIG_PATH,
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
    )
    from .profile_config import read_active_config_path
except ImportError:
    from ui.config import (
        COMBINED_CONFIG_PATH,
        AIRBAND_MIN_MHZ,
        AIRBAND_MAX_MHZ,
        GROUND_CONFIG_PATH,
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
    )
    from ui.profile_config import read_active_config_path

RE_SERIAL = re.compile(r'serial\s*=\s*"([^"]+)"', re.I)
RE_INDEX = re.compile(r'index\s*=\s*(\d+)\s*;', re.I)
RE_GAIN = re.compile(r'gain\s*=\s*([0-9.]+)\s*;', re.I)
RE_SQUELCH = re.compile(r'squelch_threshold\s*=\s*(-?\d+)\s*;', re.I)
RE_FREQS_BLOCK = re.compile(r'freqs\s*=\s*\((.*?)\)\s*;', re.S | re.I)
RE_DEVICE_TYPE = re.compile(r'type\s*=\s*"([^"]+)"', re.I)
RE_DEVICE_STRING = re.compile(r'device_string\s*=\s*"([^"]+)"', re.I)
# Inside a SoapySDR device_string ("driver=sdrplay,serial=180903EF32,..."),
# extract the comma-separated key=value pairs.
RE_KV_PAIR = re.compile(r'([A-Za-z_][\w]*)\s*=\s*([^,]+)')
EXPECTED_DEVICE_INDICES = {
    "airband": 0,
    "ground": 1,
}


def _iter_struct_chars(text: str, start: int = 0):
    """Yield (i, ch) pairs for ``text[start:]``, skipping characters
    inside ``"..."`` string literals.

    rtl-airband config strings legitimately contain ``(``, ``)``, ``{``,
    and ``}`` (e.g. ``"ZNY Sector 58 Coyle (Ship Bottom RCAG)"``).  Any
    parser that walks the file for structural brackets must ignore
    string contents or it will lose track of nesting and return an
    empty section.  Honors ``\\"`` escapes inside strings.
    """
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        yield i, ch


def _extract_devices_section(text: str) -> str:
    idx = text.find("devices:")
    if idx == -1:
        return ""
    # Find the opening ``(`` for the devices list, skipping any ``(``
    # that appears inside a string literal between ``devices:`` and the
    # real opener.  Then walk to the matching close-paren with the same
    # string-aware iteration.
    start = -1
    for i, ch in _iter_struct_chars(text, idx):
        if ch == "(":
            start = i
            break
    if start == -1:
        return ""
    depth = 0
    for i, ch in _iter_struct_chars(text, start):
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
    for i, ch in _iter_struct_chars(section):
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


def _parse_device_string_kv(value: str) -> Dict[str, str]:
    """Parse a SoapySDR ``device_string`` (``key=value,key=value...``)
    into a flat dict.  Empty / malformed input yields ``{}``.

    The serial of a SoapySDR sdrplay device lives here rather than in
    a top-level ``serial = "..."`` field — this is how we recover it
    so callers can answer "which physical RSPduo does this device
    block claim".
    """
    out: Dict[str, str] = {}
    if not value:
        return out
    for match in RE_KV_PAIR.finditer(value):
        key = match.group(1).strip().lower()
        val = match.group(2).strip()
        if val:
            out[key] = val
    return out


def _parse_device_block(block: str) -> Dict:
    """Parse a single ``{ ... }`` device block into the canonical dict."""
    serial = None
    index = None
    gain = None
    squelch_dbfs = None
    device_type = None
    soapy_driver = None
    soapy_kwargs: Dict[str, str] = {}
    m = RE_DEVICE_TYPE.search(block)
    if m:
        device_type = m.group(1).strip().lower() or None
    m = RE_DEVICE_STRING.search(block)
    if m:
        soapy_kwargs = _parse_device_string_kv(m.group(1))
        soapy_driver = soapy_kwargs.get("driver")
        # For SoapySDR devices the canonical serial lives in
        # device_string; surface it at the top level so existing
        # callers that look at dev["serial"] don't need to know
        # about the soapy nesting.
        if "serial" in soapy_kwargs:
            serial = soapy_kwargs["serial"]
    m = RE_SERIAL.search(block)
    if m and not serial:
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
    return {
        "serial": serial,
        "index": index,
        "gain": gain,
        "squelch_dbfs": squelch_dbfs,
        "freqs": freqs,
        "is_airband": is_airband,
        # SoapySDR-specific structured info — None for legacy rtlsdr
        # blocks.  Lets callers reason about the device backend
        # without re-parsing the raw config.
        "device_type": device_type,
        "soapy_driver": soapy_driver,
        "soapy_kwargs": soapy_kwargs or None,
    }


def parse_combined_devices_text(text: str) -> List[Dict]:
    """Parse devices out of raw combined-config text — no file I/O.

    The file-path version (``read_combined_devices``) delegates here.
    Exposed separately so callers like the config validator can
    pre-flight a prospective combined config (e.g. the one a profile
    switch is about to apply) without writing to disk first, or
    validate an in-memory candidate during dry-run.
    """
    if not text:
        return []
    section = _extract_devices_section(text)
    if not section:
        return []
    return [_parse_device_block(block) for block in _split_device_blocks(section)]


def read_combined_devices(conf_path: Optional[str] = None) -> List[Dict]:
    # Resolve the default at call time, not import time, so callers
    # (and tests) can override ``COMBINED_CONFIG_PATH`` at the module
    # level after this module has been imported.
    if conf_path is None:
        conf_path = COMBINED_CONFIG_PATH
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except FileNotFoundError:
        return []
    return parse_combined_devices_text(text)


def serials_claimed_by_combined_config(
    conf_path: Optional[str] = None,
) -> set:
    """Return the set of physical-device serials currently claimed by
    the active combined rtl-airband config.

    Used by the OP25 dongle allocator to avoid handing out a tuner
    that rtl-airband already owns.  The combined config is the single
    source of truth for rtl-airband's device ownership — derive from
    it rather than maintaining a parallel env var that has to be kept
    in sync by hand.

    Works for both legacy ``type = "rtlsdr"; serial = "..."`` blocks
    and ``type = "soapysdr"; device_string = "...serial=X,..."``
    blocks.  Returns ``set()`` when the config is missing or empty.
    """
    if conf_path is None:
        conf_path = COMBINED_CONFIG_PATH
    out: set = set()
    for dev in read_combined_devices(conf_path):
        serial = str(dev.get("serial") or "").strip()
        if serial:
            out.add(serial)
    return out


def combined_device_summary(conf_path: Optional[str] = None) -> Dict[str, Optional[Dict]]:
    if conf_path is None:
        conf_path = COMBINED_CONFIG_PATH
    devices = read_combined_devices(conf_path)
    airband = None
    ground = None
    if AIRBAND_RTL_SERIAL:
        airband = next((d for d in devices if d.get("serial") == AIRBAND_RTL_SERIAL), None)
    if not airband:
        airband = next((d for d in devices if d["is_airband"]), None)
    if GROUND_RTL_SERIAL:
        ground = next((d for d in devices if d.get("serial") == GROUND_RTL_SERIAL), None)
    if not ground:
        ground = next((d for d in devices if not d["is_airband"] and d["freqs"]), None)
    expected_serials = {
        "airband": AIRBAND_RTL_SERIAL or (airband.get("serial") if airband else None),
        "ground": GROUND_RTL_SERIAL or (ground.get("serial") if ground else None),
    }
    expected_indices = dict(EXPECTED_DEVICE_INDICES)
    index_mismatch_detail = []
    for name, device in (("airband", airband), ("ground", ground)):
        if not isinstance(device, dict):
            continue
        expected_index = expected_indices.get(name)
        actual_index = device.get("index")
        if expected_index is None:
            continue
        if actual_index is None:
            index_mismatch_detail.append({
                "device": name,
                "expected": expected_index,
                "actual": None,
                "reason": f"{name} index missing",
            })
        elif int(actual_index) != int(expected_index):
            index_mismatch_detail.append({
                "device": name,
                "expected": expected_index,
                "actual": int(actual_index),
                "reason": f"{name} index mismatch",
            })
    return {
        "devices": devices,
        "airband": airband,
        "ground": ground,
        "expected_serials": expected_serials,
        "expected_indices": expected_indices,
        "index_mismatch_detail": index_mismatch_detail,
    }


def combined_config_stale(conf_path: Optional[str] = None) -> bool:
    if conf_path is None:
        conf_path = COMBINED_CONFIG_PATH
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
