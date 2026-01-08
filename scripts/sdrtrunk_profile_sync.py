#!/usr/bin/env python3
import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILES_DIR = REPO_ROOT / "profiles" / "sdrtrunk"
DEFAULT_HOME = Path(os.environ.get("SDRTRUNK_HOME", "/var/lib/sdrtrunk"))
ACTIVE_PROFILE_FILE = "active_profile"


def _copy_item(src: Path, dest: Path) -> None:
    if dest.exists():
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    if src.is_dir():
        shutil.copytree(src, dest, symlinks=True)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def sync_profile(profile_id: str, profiles_dir: Path, home: Path, dry_run: bool) -> None:
    profile_dir = profiles_dir / profile_id
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"Profile directory not found: {profile_dir}")

    sdrtrunk_dir = profile_dir / "sdrtrunk"
    runtime_root = home / "SDRTrunk"

    if not dry_run:
        runtime_root.mkdir(parents=True, exist_ok=True)

    if sdrtrunk_dir.is_dir():
        for child in sorted(sdrtrunk_dir.iterdir()):
            dest = runtime_root / child.name
            if dry_run:
                continue
            _copy_item(child, dest)
    else:
        print(f"WARN: no sdrtrunk directory found in profile {profile_id}", file=sys.stderr)

    if not dry_run:
        (home / ACTIVE_PROFILE_FILE).write_text(profile_id + "\n", encoding="utf-8")

    playlist_path = sdrtrunk_dir / "playlist" / "default.xml"
    if not playlist_path.exists():
        print(
            f"WARN: missing playlist/default.xml for profile {profile_id} ({playlist_path})",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync SDRTrunk profile into the runtime directory")
    parser.add_argument("--profile", required=True, help="Profile id (folder name)")
    parser.add_argument("--profiles-dir", default=str(DEFAULT_PROFILES_DIR), help="Base profiles directory")
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="Runtime home directory (user.home)")
    parser.add_argument("--dry-run", action="store_true", help="Validate paths without copying")
    args = parser.parse_args()

    profiles_dir = Path(args.profiles_dir).expanduser().resolve()
    home = Path(args.home).expanduser().resolve()

    try:
        sync_profile(args.profile, profiles_dir, home, args.dry_run)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
