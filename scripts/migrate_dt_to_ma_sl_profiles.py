#!/usr/bin/env python3
"""Convert rtl-airband profile device_strings from DT mode to MA/SL mode.

Companion to ``migrate_legacy_rtl_profiles.py`` (which earlier today
moved rtl-airband off RTL-SDR onto RSPduo DT mode).  The MA/SL split-
process architecture (docs/rspduo_ma_sl_split.md) requires each tuner
to be opened by its own process, with the Master (Tuner 1) and Slave
(Tuner 2) declared distinctly in their respective device_string.

Mapping
-------
    mode=DT,tuner=1   →   mode=MA,tuner=1     (airband-band profiles)
    mode=DT,tuner=2   →   mode=SL,tuner=2     (ground-band profiles)

Profiles whose device_string doesn't reference DT mode are left
untouched.  Each rewritten file gets a sibling
``.pre-ma-sl-20260526`` backup for the rollback path.

The script is intentionally narrow — it just does the mode-token
swap.  Channel content, gains, squelches, all left exactly as they
were.  No tuner-renumbering, no serial changes.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# These exact substrings are what migrate_legacy_rtl_profiles.py
# produced, so we can do a clean string substitution.  If a future
# config format introduces variations (whitespace, quoting), tighten
# this to a regex with explicit captures.
DT_TO_MA_SL = {
    "mode=DT,tuner=1": "mode=MA,tuner=1",
    "mode=DT,tuner=2": "mode=SL,tuner=2",
}


def migrate_text(text: str) -> tuple[str, list[str]]:
    """Return (new_text, list_of_tokens_swapped)."""
    swaps: list[str] = []
    new_text = text
    for old, new in DT_TO_MA_SL.items():
        if old in new_text:
            new_text = new_text.replace(old, new)
            swaps.append(f"{old} -> {new}")
    return new_text, swaps


def migrate_file(path: Path, *, backup_suffix: str, dry_run: bool) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    new_text, swaps = migrate_text(text)
    summary = {"path": str(path), "changed": new_text != text, "swaps": swaps}
    if not summary["changed"]:
        return summary
    if not dry_run:
        backup = path.with_suffix(path.suffix + backup_suffix)
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(new_text, encoding="utf-8")
        summary["backup"] = str(backup)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles-dir", default="profiles",
        help="Directory containing rtl_airband_*.conf files",
    )
    parser.add_argument(
        "--backup-suffix", default=".pre-ma-sl-20260526",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    profiles_dir = Path(args.profiles_dir)
    if not profiles_dir.is_dir():
        print(f"error: {profiles_dir} is not a directory", file=sys.stderr)
        return 2

    files = sorted(profiles_dir.glob("rtl_airband_*.conf"))
    if not files:
        print(f"no profile files in {profiles_dir}", file=sys.stderr)
        return 1

    changed = 0
    for fpath in files:
        summary = migrate_file(
            fpath,
            backup_suffix=args.backup_suffix,
            dry_run=args.dry_run,
        )
        if summary["changed"]:
            changed += 1
            tag = "[dry-run]" if args.dry_run else "MIGRATED"
            print(f"{tag} {fpath.name}:")
            for swap in summary["swaps"]:
                print(f"  {swap}")
        else:
            print(f"  ok    {fpath.name} (no DT-mode device_string)")
    print(
        f"\n{'Would migrate' if args.dry_run else 'Migrated'} "
        f"{changed} of {len(files)} files."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
