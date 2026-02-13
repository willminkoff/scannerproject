#!/usr/bin/env python3
"""Ensure digital runtime prerequisites exist before starting SDRTrunk.

This script is intended for scanner-digital.service ExecStartPre.
It repairs/creates the active profile symlink and syncs playlist runtime
frequency from control_channels.txt.
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

FREQ_RE = re.compile(r"\d+\.\d+")

PROFILES_DIR = Path(os.getenv("DIGITAL_PROFILES_DIR", "/etc/scannerproject/digital/profiles")).expanduser()
ACTIVE_LINK = Path(os.getenv("DIGITAL_ACTIVE_PROFILE_LINK", "/etc/scannerproject/digital/active")).expanduser()
PLAYLIST_PATH = Path(
    os.getenv("DIGITAL_PLAYLIST_PATH", str(Path.home() / "SDRTrunk" / "playlist" / "default.xml"))
).expanduser()
DEFAULT_PROFILE = os.getenv("DIGITAL_BOOT_DEFAULT_PROFILE", "default").strip()


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} ensure-digital-runtime: {msg}")


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


def _read_first_control_channel_hz(profile_dir: Path) -> int:
    path = profile_dir / "control_channels.txt"
    if not path.is_file():
        raise RuntimeError(f"missing control_channels.txt in {profile_dir}")
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        raise RuntimeError(f"failed to read {path}: {e}") from e
    for line in lines:
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        match = FREQ_RE.search(raw)
        if not match:
            continue
        try:
            return int(round(float(match.group(0)) * 1_000_000))
        except Exception:
            continue
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


def _sync_playlist(profile_dir: Path, control_hz: int) -> None:
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

    event_conf = _ensure_child(channel, "event_log_configuration")
    existing = {str(e.text or "").strip() for e in event_conf.findall("logger")}
    for logger_name in ("CALL_EVENT", "TRAFFIC_CALL_EVENT", "DECODED_MESSAGE"):
        if logger_name not in existing:
            logger = ET.SubElement(event_conf, "logger")
            logger.text = logger_name

    source_conf = _ensure_child(channel, "source_configuration")
    source_conf.set("type", "sourceConfigTuner")
    source_conf.set("source_type", "TUNER")
    source_conf.set("frequency", str(control_hz))

    if channel.find("decode_configuration") is None:
        ET.SubElement(
            channel,
            "decode_configuration",
            {
                "type": "decodeConfigP25Phase1",
                "modulation": "C4FM",
                "traffic_channel_pool_size": "20",
                "ignore_data_calls": "false",
            },
        )

    _ensure_child(channel, "record_configuration")

    tree.write(PLAYLIST_PATH, encoding="utf-8", xml_declaration=False)


def main() -> int:
    try:
        target = _choose_profile()
        _point_active_link(target)
        control_hz = _read_first_control_channel_hz(target)
        _sync_playlist(target, control_hz)
        _log(f"active profile={target.name} control_hz={control_hz}")
        return 0
    except Exception as e:
        _log(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
