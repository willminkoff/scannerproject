#!/usr/bin/env python3
"""One-shot RF/runtime diagnostic for the OP25 digital pipeline on Micro.

Run on the host where ``scanner-digital-op25`` lives.  Designed to be safe
to invoke at any time (read-only) and to print everything needed to
diagnose a "decoder running but no control-channel lock" failure mode:

* service activity for the OP25 main + audio-bridge units
* tail of ``/var/log/op25/op25.log`` so any per-channel "control channel
  locked" / "rx error" / "device open failed" messages are visible
* the live ``multi_rx.json`` (or ``instance-*/multi_rx.json`` for the
  split-process RSPduo runtime) so the actual ``args`` / ``gains`` /
  ``gain_mode`` values applied to each tuner are surfaced
* ``SoapySDRUtil --probe`` for any attached RSPduo, which prints the
  driver's actual gain-element names — the canonical answer to
  "is ``LNA:36`` a valid gain string for this device?"
* the dongle allocator's current assignments

Print-only — does not restart services, write files, or change state.

Usage on Micro::

    ssh root@micro "python3 /home/ubuntu/scannerproject/scripts/op25-rf-diag.py"
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap


OP25_LOG_PATH = os.environ.get("OP25_LOG_PATH", "/var/log/op25/op25.log")
OP25_RUNTIME_DIR = os.environ.get("OP25_RUNTIME_DIR", "/run/scannerproject/op25")
OP25_SERVICE_NAME = os.environ.get("OP25_SERVICE_NAME", "scanner-digital-op25")
DIGITAL_PROFILES_DIR = os.environ.get(
    "DIGITAL_PROFILES_DIR", "/etc/scannerproject/digital/profiles"
)
DIGITAL_ACTIVE_PROFILE_LINK = os.environ.get(
    "DIGITAL_ACTIVE_PROFILE_LINK",
    "/run/scannerproject/digital/active-profile",
)


def _section(title: str) -> None:
    bar = "=" * len(title)
    print(f"\n{title}\n{bar}")


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    """Run *cmd* and return combined stdout/stderr.  Never raises."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (result.stdout or "") + (result.stderr or "")
    except FileNotFoundError:
        return f"<{cmd[0]} not installed>"
    except subprocess.TimeoutExpired:
        return f"<{' '.join(cmd)} timed out after {timeout:g}s>"
    except Exception as exc:
        return f"<exec error: {exc!r}>"


def _print_service_status() -> None:
    _section("Service status")
    for unit in (
        OP25_SERVICE_NAME,
        f"{OP25_SERVICE_NAME}-audio",
        "airband-ui",
    ):
        active = _run(["systemctl", "is-active", unit]).strip()
        sub = _run(["systemctl", "show", unit, "-p", "SubState", "--value"]).strip()
        since = _run(
            ["systemctl", "show", unit, "-p", "ActiveEnterTimestamp", "--value"]
        ).strip()
        print(f"  {unit:32s}  active={active:10s}  sub={sub:10s}  since={since}")


def _print_log_tail(lines: int = 200) -> None:
    _section(f"OP25 log tail ({lines} lines)")
    if not os.path.isfile(OP25_LOG_PATH):
        print(f"  (log file {OP25_LOG_PATH} missing)")
        return
    output = _run(["tail", "-n", str(lines), OP25_LOG_PATH])
    if not output.strip():
        print("  (log empty)")
        return
    for line in output.splitlines():
        print(f"  {line}")


def _print_multi_rx_configs() -> None:
    _section("Live multi_rx.json")
    paths: list[str] = []
    main_path = os.path.join(OP25_RUNTIME_DIR, "multi_rx.json")
    if os.path.isfile(main_path):
        paths.append(main_path)
    if os.path.isdir(OP25_RUNTIME_DIR):
        for entry in sorted(os.listdir(OP25_RUNTIME_DIR)):
            if entry.startswith("instance-"):
                inst_path = os.path.join(OP25_RUNTIME_DIR, entry, "multi_rx.json")
                if os.path.isfile(inst_path):
                    paths.append(inst_path)
    if not paths:
        print(f"  (no multi_rx.json under {OP25_RUNTIME_DIR})")
        return
    for path in paths:
        print(f"\n  -- {path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as exc:
            print(f"     <parse error: {exc!r}>")
            continue
        # Top-level summary (split-process manifest is shaped differently).
        if "process_count" in cfg:
            print(f"     split runtime summary: process_count={cfg.get('process_count')}")
            continue
        for dev in cfg.get("devices", []) or []:
            print(
                f"     device {dev.get('name'):<14} "
                f"args={dev.get('args')}  "
                f"freq={(dev.get('frequency') or 0)/1e6:.5f} MHz  "
                f"rate={(dev.get('rate') or 0)/1e6:.2f} MSps  "
                f"offset={dev.get('offset')}  "
                f"ppm={dev.get('ppm')}  "
                f"gains={dev.get('gains')!r}  "
                f"gain_mode={dev.get('gain_mode')}"
            )
        for ch in cfg.get("channels", []) or []:
            print(
                f"     channel {ch.get('name'):<22} "
                f"device={ch.get('device'):<12} "
                f"sysname={ch.get('trunking_sysname'):<12} "
                f"demod={ch.get('demod_type')}"
            )
        for tch in (cfg.get("trunking") or {}).get("chans", []) or []:
            print(
                f"     trunking sysname={tch.get('sysname'):<12} "
                f"control_channel_list={tch.get('control_channel_list')!r}  "
                f"nac={tch.get('nac')}"
            )


def _print_rspduo_probe() -> None:
    _section("SoapySDRUtil RSPduo probe")
    if shutil.which("SoapySDRUtil") is None:
        print("  (SoapySDRUtil not installed — skip)")
        return
    listing = _run(["SoapySDRUtil", "--find=driver=sdrplay"], timeout=15.0)
    print("  -- find:")
    for line in listing.splitlines():
        print(f"     {line}")
    if "No devices found" in listing or not listing.strip():
        print("  (no SDRplay device enumerated)")
        return
    probe = _run(["SoapySDRUtil", "--probe=driver=sdrplay"], timeout=20.0)
    keep = ("Gain", "Antenn", "Hardware", "Found", "Available", "Range")
    printed = 0
    print("  -- probe (gain/antenna/hardware lines):")
    for line in probe.splitlines():
        if any(token in line for token in keep):
            print(f"     {line}")
            printed += 1
            if printed > 80:
                print("     <truncated>")
                break
    if not printed:
        print("     (no gain/antenna lines found in probe output)")


def _print_dongle_assignments() -> None:
    _section("Dongle allocator assignments")
    candidates = [
        os.path.join(OP25_RUNTIME_DIR, "dongle_assignments.json"),
        "/run/scannerproject/dongle_assignments.json",
        "/var/lib/scannerproject/dongle_assignments.json",
    ]
    for path in candidates:
        if os.path.isfile(path):
            print(f"  -- {path}")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception as exc:
                print(f"     <parse error: {exc!r}>")
                continue
            for entry in payload.get("assignments") or []:
                print(
                    f"     system={entry.get('system_name'):<14} "
                    f"serial={entry.get('preferred_tuner_serial')!r}"
                )
            for trail in payload.get("traffic_followers") or []:
                print(f"     traffic_follower={trail!r}")
            return
    print("  (no dongle_assignments.json found)")


def _print_active_profile() -> None:
    _section("Active digital profile")
    print(f"  link: {DIGITAL_ACTIVE_PROFILE_LINK}")
    if os.path.islink(DIGITAL_ACTIVE_PROFILE_LINK):
        target = os.path.realpath(DIGITAL_ACTIVE_PROFILE_LINK)
        print(f"  -> {target}")
        for fname in ("systems.json", "op25_system_config.json"):
            path = os.path.join(target, fname)
            if os.path.isfile(path):
                print(f"\n  -- {path}")
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        print(textwrap.indent(f.read().rstrip(), "     "))
                except Exception as exc:
                    print(f"     <read error: {exc!r}>")
    else:
        print("  (link missing or not a symlink)")


def main() -> int:
    print("OP25 RF diagnostic — read-only")
    print(f"  host:    {os.uname().nodename}")
    print(f"  service: {OP25_SERVICE_NAME}")
    print(f"  runtime: {OP25_RUNTIME_DIR}")
    print(f"  log:     {OP25_LOG_PATH}")
    _print_service_status()
    _print_active_profile()
    _print_dongle_assignments()
    _print_multi_rx_configs()
    _print_rspduo_probe()
    _print_log_tail()
    return 0


if __name__ == "__main__":
    sys.exit(main())
