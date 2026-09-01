"""sb3.sdrtrunk_applier — apply a digital profile to SDRTrunk on Venus.

Reads a digital-role Profile, generates SDRTrunk playlist XML from its
`systems[]` block, SCPs the playlist + tuner_configuration to Venus, and
restarts sdrtrunk over SSH.

Digital profile schema (drop into /Users/willminkoff/scannerproject/profiles/):

    {
      "name": "digital.nashville",
      "role": "digital",
      "sub_role": "p25-phase1",
      "backend": "sdrtrunk",
      "target_host": "willminkoff@100.114.219.115",
      "stream": {
        "name": "scanner-digital",
        "mount_point": "/venus-digital.mp3",
        "bit_rate": 16, "sample_rate": 8000,
        "host": "127.0.0.1", "port": 8000,
        "user_name": "source", "password": "digitalpw123"
      },
      "systems": [
        {
          "name": "TACN Nashville (Tennessee Tower)",
          "preferred_tuner": "RSPduo SER:1809063632 Tuner 1",
          "modulation": "CQPSK",
          "traffic_channel_pool_size": 20,
          "ignore_data_calls": false,
          "control_channels_hz": [852987500, 853737500]
        }
      ],
      "disabled_tuners": [
        {"tunerClass": "RSP", "id": "RSPduo Tuner 1 SER#180903EF32"}
      ]
    }

Usage:
    python3 -m sb3.sdrtrunk_applier <profile_name_or_path> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape


DEFAULT_TARGET = os.environ.get(
    "SB3_SDRTRUNK_TARGET_HOST", "willminkoff@100.114.219.115"
)
REMOTE_PLAYLIST = os.environ.get(
    "SB3_SDRTRUNK_REMOTE_PLAYLIST",
    "/home/willminkoff/SDRTrunk/playlist/default.xml",
)
REMOTE_TUNER_CFG = os.environ.get(
    "SB3_SDRTRUNK_REMOTE_TUNER_CFG",
    "/home/willminkoff/SDRTrunk/configuration/tuner_configuration.json",
)
REMOTE_SDRPLAY_SERVICE = "sdrplay"
REMOTE_SDRTRUNK_SERVICE = "sdrtrunk"


def _xml_attrs(**kw: Any) -> str:
    """Space-joined XML attributes, values escaped."""
    parts: List[str] = []
    for k, v in kw.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        parts.append(f'{k}="{xml_escape(str(v), {chr(34): "&quot;"})}"')
    return " ".join(parts)


def build_playlist_xml(profile: Dict[str, Any]) -> str:
    """Generate SDRTrunk playlist XML from a digital profile."""
    stream = profile.get("stream", {})
    systems = profile.get("systems", [])
    if not systems:
        raise ValueError("digital profile has no systems[]")

    stream_attrs = _xml_attrs(
        type="icecastHTTPConfiguration",
        name=stream.get("name", "scanner-digital"),
        enabled="true",
        format="MP3",
        host=stream.get("host", "127.0.0.1"),
        port=stream.get("port", 8000),
        mount_point=stream.get("mount_point", "/venus-digital.mp3"),
        user_name=stream.get("user_name", "source"),
        password=stream.get("password", "digitalpw123"),
        bit_rate=stream.get("bit_rate", 16),
        sample_rate=stream.get("sample_rate", 8000),
        inline="false",
    )

    alias_list = stream.get("alias_list_name", "Nashville Digital")
    channel_blocks: List[str] = []
    for i, sys_ in enumerate(systems, start=1):
        ccs = sys_.get("control_channels_hz", [])
        if not ccs:
            raise ValueError(f"system {sys_.get('name')!r} has no control_channels_hz")
        freq_xml = "\n      ".join(f"<frequency>{int(f)}</frequency>" for f in ccs)
        src_attrs = _xml_attrs(
            type="sourceConfigTunerMultipleFrequency",
            frequency_rotation_delay=sys_.get("frequency_rotation_delay", 400),
            preferred_tuner=sys_.get("preferred_tuner", ""),
            source_type="TUNER_MULTIPLE_FREQUENCIES",
        )
        dec_attrs = _xml_attrs(
            type="decodeConfigP25Phase1",
            modulation=sys_.get("modulation", "CQPSK"),
            traffic_channel_pool_size=sys_.get("traffic_channel_pool_size", 20),
            ignore_data_calls=bool(sys_.get("ignore_data_calls", False)),
        )
        ch_attrs = _xml_attrs(
            name=sys_.get("name", f"P25 System {i}"),
            order=i,
            enabled="true",
            autoStart="true",
        )
        channel_blocks.append(f"""  <channel {ch_attrs}>
    <event_log_configuration>
      <logger>DECODED_MESSAGE</logger>
      <logger>CALL_EVENT</logger>
    </event_log_configuration>
    <aux_decode_configuration />
    <source_configuration {src_attrs}>
      {freq_xml}
    </source_configuration>
    <decode_configuration {dec_attrs} />
    <record_configuration />
    <alias_list_name>{xml_escape(alias_list)}</alias_list_name>
  </channel>""")

    channels_xml = "\n".join(channel_blocks)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<playlist version="4">
  <stream {stream_attrs} />
  <alias list="{xml_escape(alias_list)}" name="All P25 Voice">
    <id type="talkgroupRange" min="1" max="65535" protocol="APCO25" />
    <id type="broadcastChannel" channel="{xml_escape(stream.get("name", "scanner-digital"))}" />
  </alias>
{channels_xml}
</playlist>
"""


def build_tuner_configuration(profile: Dict[str, Any]) -> str:
    """Generate tuner_configuration.json from the profile's disabled_tuners."""
    disabled = profile.get("disabled_tuners", [])
    return json.dumps({"disabledTuners": disabled}, indent=2)


def apply_digital_profile(profile: Dict[str, Any], *, dry_run: bool = False) -> Dict[str, Any]:
    """Apply a digital-role profile to SDRTrunk on Venus."""
    result: Dict[str, Any] = {"ok": False, "actions": [], "errors": []}
    if profile.get("role") != "digital":
        result["errors"].append(
            f"profile role={profile.get('role')!r} — only 'digital' can drive sdrtrunk"
        )
        return result
    try:
        playlist_xml = build_playlist_xml(profile)
        tuner_cfg = build_tuner_configuration(profile)
    except ValueError as exc:
        result["errors"].append(f"profile invalid: {exc}")
        return result

    target = profile.get("target_host") or DEFAULT_TARGET
    result["actions"].append(f"target: {target}")
    result["playlist_bytes"] = len(playlist_xml)
    result["tuner_cfg_bytes"] = len(tuner_cfg)

    if dry_run:
        result["playlist_preview"] = playlist_xml[:600]
        result["ok"] = True
        return result

    # Write to temp files, then scp
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
        f.write(playlist_xml)
        playlist_path = f.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(tuner_cfg)
        tuner_path = f.name

    try:
        for local, remote in (
            (playlist_path, REMOTE_PLAYLIST),
            (tuner_path, REMOTE_TUNER_CFG),
        ):
            r = subprocess.run(
                ["scp", "-o", "BatchMode=yes", local, f"{target}:{remote}"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                result["errors"].append(
                    f"scp {os.path.basename(remote)} failed: {r.stderr.strip()}"
                )
                return result
            result["actions"].append(f"scp'd {os.path.basename(remote)}")

        # Restart the sdrtrunk cascade on Venus
        cmd = f"sudo systemctl restart {REMOTE_SDRTRUNK_SERVICE}"
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", target, cmd],
            capture_output=True, text=True, timeout=45,
        )
        if r.returncode != 0:
            result["errors"].append(
                f"remote restart failed: {r.stderr.strip()}"
            )
            return result
        result["actions"].append("restarted sdrtrunk on Venus (sdrplay pre-restart handled by unit)")

    finally:
        for f in (playlist_path, tuner_path):
            try:
                os.unlink(f)
            except OSError:
                pass

    result["ok"] = True
    return result


def _resolve_profile_arg(arg: str) -> Path:
    p = Path(arg)
    if p.is_file():
        return p
    root = Path(__file__).resolve().parent.parent
    for cand in (
        root / "profiles" / f"{arg}.json",
        root / "profiles" / arg,
        root / "profiles" / (arg.replace(".", "-") + ".json"),
    ):
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"profile not found: {arg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build XML/JSON but don't scp or restart Venus")
    args = ap.parse_args()

    path = _resolve_profile_arg(args.profile)
    profile = json.loads(path.read_text())
    result = apply_digital_profile(profile, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
