#!/usr/bin/env python3
"""Migrate legacy rtl-airband profile files from RTL-SDR to RSPduo.

Background
----------
Before the 2026-05-25 RSPduo migration, rtl-airband used two RTL-SDR
dongles whose serials were baked into the profile templates:

  - Airband dongle, serial ``00000002``, declared ``index = 0``.
  - Ground dongle,  serial ``70613472``, declared ``index = 1``.

After the migration neither dongle is plugged in.  Selecting any of
the legacy profiles (``bandscan_marine``, ``rtl_airband_gmrs*``,
``rtl_airband_campbell_*``, ``rtl_airband_wx``, etc.) produces a
combined config whose device block names a serial the system can't
open — rtl_airband crash-loops on ``device not found``.

The Step C validator now blocks those selections at apply-time, but
the profiles themselves are still dead weight.  This script walks
``profiles/*.conf``, finds every legacy rtlsdr device block, and
rewrites it as the equivalent RSPduo Dual-Tuner block:

  - ``serial = "00000002"`` and/or ``index = 0`` → Tuner 1 (airband)
  - ``serial = "70613472"`` and/or ``index = 1`` → Tuner 2 (ground)
  - Bare ``type = "rtlsdr"`` with no serial / no index → resolved
    from the ``airband = true/false`` header at file top
    (airband=true → Tuner 1, otherwise → Tuner 2)

All channels / freqs / gains / squelches / outputs are preserved
exactly.  Each rewritten file gets a sibling ``.pre-rspduo-20260526``
backup so the migration is reversible.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

# Post-migration constants — both RSPduos in service.  rtl-airband
# owns 1809063632 in DT mode; 180903EF32 is reserved for OP25.
RSPDUO_SERIAL = "1809063632"
SAMPLE_RATE_HZ = 1_000_000

# Legacy dead serials that map deterministically to a tuner.
DEAD_SERIAL_TO_TUNER: dict[str, int] = {
    "00000002": 1,   # airband RTL-SDR -> Tuner 1
    "70613472": 2,   # ground  RTL-SDR -> Tuner 2
}

RE_AIRBAND_FLAG = re.compile(r"^\s*airband\s*=\s*(true|false)\s*;", re.I | re.M)
RE_TYPE_RTLSDR = re.compile(r'^(?P<indent>\s*)type\s*=\s*"rtlsdr"\s*;', re.M)
# Match `serial = "X"` or `index = N` lines (with their newline)
# directly so we can strip them once we've moved their semantic into
# the new device_string.
RE_SERIAL_LINE = re.compile(r'^\s*serial\s*=\s*"[^"]*"\s*;\s*\n', re.M)
RE_INDEX_LINE = re.compile(r'^\s*index\s*=\s*\d+\s*;\s*\n', re.M)


def _tuner_for_block(serial: Optional[str], index: Optional[int], airband_flag: bool) -> int:
    """Resolve which RSPduo tuner this device block should target.

    Priority order:
      1. Explicit dead serial that maps to a known tuner
      2. Explicit index (0→1, 1→2)
      3. Top-of-file ``airband = true/false`` flag
    """
    if serial and serial in DEAD_SERIAL_TO_TUNER:
        return DEAD_SERIAL_TO_TUNER[serial]
    if index is not None:
        return 1 if int(index) == 0 else 2
    return 1 if airband_flag else 2


def _extract_serial_and_index_in_block(block_text: str) -> tuple[Optional[str], Optional[int]]:
    """Pull serial + index out of a single device-block fragment."""
    serial = None
    index = None
    m = re.search(r'serial\s*=\s*"([^"]+)"\s*;', block_text)
    if m:
        serial = m.group(1).strip()
    m = re.search(r'index\s*=\s*(\d+)\s*;', block_text)
    if m:
        try:
            index = int(m.group(1))
        except ValueError:
            index = None
    return serial, index


def _make_replacement(indent: str, tuner: int) -> str:
    """Build the SoapySDR replacement for the ``type = "rtlsdr";`` line.

    Replaces the single ``type = "rtlsdr";`` line with three lines:
      type = "soapysdr";
      device_string = "driver=sdrplay,serial=...,mode=DT,tuner=N";
      sample_rate = 1000000;

    The serial/index sibling lines further down get stripped
    separately so the final block has no contradictory hints.
    Includes a trailing newline so the next surviving line (typically
    ``mode = "scan";``) keeps its own indentation on its own line.
    """
    return (
        f'{indent}type = "soapysdr";\n'
        f'{indent}device_string = "driver=sdrplay,serial={RSPDUO_SERIAL},'
        f'mode=DT,tuner={tuner}";\n'
        f'{indent}sample_rate = {SAMPLE_RATE_HZ};\n'
    )


def migrate_text(text: str) -> tuple[str, list[dict]]:
    """Return (migrated_text, list_of_changes).

    Idempotent: a file already in SoapySDR shape returns unchanged with
    an empty change list.
    """
    if "type = \"rtlsdr\"" not in text and 'type="rtlsdr"' not in text:
        return text, []

    airband_flag_match = RE_AIRBAND_FLAG.search(text)
    airband_flag = bool(
        airband_flag_match
        and airband_flag_match.group(1).strip().lower() == "true"
    )

    changes: list[dict] = []

    # Each rtlsdr block needs its own targeted rewrite.  We walk
    # ``type = "rtlsdr";`` matches in order, slicing the surrounding
    # block to read its serial / index for the tuner decision, then
    # editing the source text.  Multiple device blocks in one file
    # are handled by scanning from the LAST match backward, which
    # keeps earlier offsets stable as we edit.
    matches = list(RE_TYPE_RTLSDR.finditer(text))
    if not matches:
        return text, []

    out = text
    for match in reversed(matches):
        # Carve out roughly one block's worth of text after the type=
        # line — enough to pick up the sibling serial / index lines
        # but not the channels list (which can be huge).  In practice
        # serial + index always appear within ~6 lines of the type=
        # declaration; widen if a future config violates that.
        block_end = out.find("channels", match.end())
        if block_end == -1:
            block_end = match.end() + 400
        block_text = out[match.end():block_end]
        serial, index = _extract_serial_and_index_in_block(block_text)
        tuner = _tuner_for_block(serial, index, airband_flag)

        # Replace the type = "rtlsdr"; line with the three-line
        # SoapySDR equivalent.
        replacement = _make_replacement(match.group("indent"), tuner)
        out = out[:match.start()] + replacement + out[match.end():]

        # Re-locate the (now-edited) block extent and strip the now-
        # contradictory serial / index lines from it.  Operate on a
        # bounded slice rather than the whole file to avoid touching
        # other device blocks higher up.
        replacement_end = match.start() + len(replacement)
        # Block ends at the first ``channels`` keyword OR the next
        # device-block opener (``},`` after a closing brace).
        scan_end = out.find("channels", replacement_end)
        if scan_end == -1:
            scan_end = min(replacement_end + 400, len(out))
        scoped = out[replacement_end:scan_end]
        scoped_clean = RE_SERIAL_LINE.sub("", scoped)
        scoped_clean = RE_INDEX_LINE.sub("", scoped_clean)
        out = out[:replacement_end] + scoped_clean + out[scan_end:]

        changes.append({
            "block_offset": match.start(),
            "former_serial": serial,
            "former_index": index,
            "new_tuner": tuner,
            "resolved_via": (
                "serial" if serial in DEAD_SERIAL_TO_TUNER
                else "index" if index is not None
                else "airband_flag"
            ),
        })

    return out, list(reversed(changes))


def migrate_file(path: Path, *, backup_suffix: str, dry_run: bool) -> dict:
    """Migrate one profile file in place.  Returns a summary dict."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    new_text, changes = migrate_text(text)
    summary = {
        "path": str(path),
        "changed": new_text != text,
        "changes": changes,
        "backup": "",
        "dry_run": dry_run,
    }
    if not summary["changed"]:
        return summary
    backup_path = path.with_suffix(path.suffix + backup_suffix)
    if not dry_run:
        if not backup_path.exists():
            shutil.copy2(path, backup_path)
        path.write_text(new_text, encoding="utf-8")
        summary["backup"] = str(backup_path)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles-dir",
        default="profiles",
        help="Directory containing rtl_airband_*.conf files",
    )
    parser.add_argument(
        "--backup-suffix",
        default=".pre-rspduo-20260526",
        help="Suffix appended to original filename for the backup copy",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without modifying files",
    )
    args = parser.parse_args(argv)

    profiles_dir = Path(args.profiles_dir)
    if not profiles_dir.is_dir():
        print(f"error: {profiles_dir} is not a directory", file=sys.stderr)
        return 2

    files = sorted(profiles_dir.glob("rtl_airband_*.conf"))
    if not files:
        print(f"no profile files found in {profiles_dir}", file=sys.stderr)
        return 1

    total_changed = 0
    for fpath in files:
        summary = migrate_file(
            fpath,
            backup_suffix=args.backup_suffix,
            dry_run=args.dry_run,
        )
        if summary["changed"]:
            total_changed += 1
            mode = "[dry-run]" if args.dry_run else "MIGRATED"
            print(f"{mode} {fpath.name}: {len(summary['changes'])} block(s)")
            for chg in summary["changes"]:
                print(
                    f"  serial={chg['former_serial']!r} index={chg['former_index']!r} "
                    f"resolved-via={chg['resolved_via']} -> tuner={chg['new_tuner']}"
                )
        else:
            print(f"  ok    {fpath.name}: no rtlsdr blocks to migrate")
    print(
        f"\n{'Would migrate' if args.dry_run else 'Migrated'} "
        f"{total_changed} of {len(files)} profile files."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
