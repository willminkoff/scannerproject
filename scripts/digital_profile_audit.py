#!/usr/bin/env python3
"""Audit/fix a scannerproject digital profile so it is runtime-ready.

Canonical "working profile" checks:
1) control_channels.txt exists and has at least one valid control frequency.
2) talkgroups.csv or talkgroups_with_group.csv exists and has valid DEC TGIDs.
3) talkgroups_listen.json exists and includes every TGID from talkgroups CSV.

Optional --fix mode:
- Generates talkgroups.csv from talkgroups_with_group.csv when missing.
- Creates/repairs talkgroups_listen.json and fills missing TGID entries.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


_FIELDNAMES = ["DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"]
_AUTO_LABEL_RE = re.compile(r"^(auto|tg)\s+\d+$", re.I)


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _parse_control_hz(raw: str) -> int | None:
    line = str(raw or "").split("#", 1)[0].strip()
    if not line:
        return None

    mhz_match = re.search(r"\d+\.\d+", line)
    if mhz_match:
        try:
            return int(round(float(mhz_match.group(0)) * 1_000_000))
        except Exception:
            return None

    int_match = re.search(r"\d+", line)
    if not int_match:
        return None
    try:
        value = int(int_match.group(0))
    except Exception:
        return None
    if value >= 1_000_000:
        return value
    if value >= 100:
        return value * 1_000_000
    return None


def _read_control_channels(path: Path) -> list[int]:
    if not path.is_file():
        return []
    seen: set[int] = set()
    out: list[int] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                hz = _parse_control_hz(line)
                if not hz or hz <= 0 or hz in seen:
                    continue
                seen.add(hz)
                out.append(hz)
    except Exception:
        return []
    return out


def _row_value(row_norm: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(row_norm.get(key, "")).strip()
        if value:
            return value
    return ""


def _placeholder_label(alpha: str, desc: str) -> bool:
    a = str(alpha or "").strip()
    d = str(desc or "").strip()
    al = a.lower()
    dl = d.lower()
    if _AUTO_LABEL_RE.fullmatch(a):
        return True
    if "auto-learned clear voice" in dl:
        return True
    if al == "unknown" or dl == "unknown":
        return True
    return False


def _pick_talkgroups_path(profile_dir: Path) -> Path | None:
    preferred = profile_dir / "talkgroups.csv"
    fallback = profile_dir / "talkgroups_with_group.csv"
    if preferred.is_file():
        return preferred
    if fallback.is_file():
        return fallback
    return None


def _read_talkgroups(path: Path) -> tuple[dict[str, dict[str, str]], int]:
    rows: dict[str, dict[str, str]] = {}
    duplicates = 0
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if not row:
                continue
            row_norm = {_norm_key(k): str(v or "").strip() for k, v in row.items()}
            dec = _row_value(row_norm, ("dec", "decimal", "tgid", "talkgroup"))
            if not dec.isdigit():
                continue
            if dec in rows:
                duplicates += 1
                continue
            hex_value = _row_value(row_norm, ("hex",)) or format(int(dec), "x")
            mode = _row_value(row_norm, ("mode",))
            alpha = _row_value(row_norm, ("alpha tag", "alpha_tag", "alpha", "name"))
            desc = _row_value(row_norm, ("description", "desc", "label"))
            tag = _row_value(row_norm, ("tag", "group", "service type"))
            rows[dec] = {
                "DEC": dec,
                "HEX": hex_value,
                "Mode": mode,
                "Alpha Tag": alpha,
                "Description": desc,
                "Tag": tag,
            }
    return rows, duplicates


def _read_listen(path: Path) -> tuple[str, str, bool, dict[str, bool], dict[str, dict[str, Any]]]:
    if not path.is_file():
        return "missing", "missing", True, {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}")
    except Exception:
        return "invalid", "invalid", True, {}, {}

    if not isinstance(payload, dict):
        return "invalid", "invalid", True, {}, {}

    default_listen = bool(payload.get("default_listen", payload.get("default", True)))
    items: dict[str, bool] = {}
    metadata: dict[str, dict[str, Any]] = {}
    has_items = False
    has_talkgroups = False

    items_raw = payload.get("items")
    if isinstance(items_raw, dict):
        has_items = True
        for key, value in items_raw.items():
            k = str(key).strip()
            if not k.isdigit():
                continue
            items[k] = bool(value)

    talkgroups_raw = payload.get("talkgroups")
    if isinstance(talkgroups_raw, dict):
        has_talkgroups = True
        for key, value in talkgroups_raw.items():
            k = str(key).strip()
            if not k.isdigit():
                continue
            if isinstance(value, dict):
                if "listen" in value:
                    items[k] = bool(value.get("listen"))
                node_meta = {
                    str(mk): mv
                    for mk, mv in value.items()
                    if str(mk).strip() and str(mk).strip().lower() != "listen"
                }
                if node_meta:
                    metadata[k] = node_meta
            else:
                items[k] = bool(value)

    if not has_items and not has_talkgroups:
        return "invalid", "invalid", default_listen, {}, {}

    if has_items and has_talkgroups:
        schema = "dual"
    elif has_items:
        schema = "items"
    else:
        schema = "talkgroups"
    return "ok", schema, default_listen, items, metadata


def _write_listen(
    path: Path,
    default_listen: bool,
    items: dict[str, bool],
    metadata: dict[str, dict[str, Any]] | None = None,
) -> None:
    items_sorted = dict(sorted(items.items(), key=lambda kv: int(kv[0])))
    talkgroups: dict[str, dict[str, Any]] = {}
    metadata = metadata or {}
    for tgid, listen in items_sorted.items():
        node: dict[str, Any] = {}
        node_meta = metadata.get(tgid)
        if isinstance(node_meta, dict):
            node.update(node_meta)
        node["listen"] = bool(listen)
        talkgroups[tgid] = node

    payload = {
        "updated": int(datetime.now().timestamp()),
        "default_listen": bool(default_listen),
        "items": items_sorted,
        "talkgroups": talkgroups,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def _write_talkgroups_csv(path: Path, rows: dict[str, dict[str, str]]) -> None:
    sorted_rows = sorted(rows.values(), key=lambda row: int(row["DEC"]))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(sorted_rows)
    tmp.replace(path)


def audit_profile(profile_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile_dir": profile_dir,
        "errors": [],
        "warnings": [],
        "fixes": [],
    }
    errors: list[str] = result["errors"]
    warnings: list[str] = result["warnings"]

    control_path = profile_dir / "control_channels.txt"
    control_hz = _read_control_channels(control_path)
    result["control_path"] = control_path
    result["control_hz"] = control_hz
    if not control_hz:
        errors.append("missing/invalid control channels: control_channels.txt has no parseable frequencies")

    tg_path = _pick_talkgroups_path(profile_dir)
    result["talkgroups_path"] = tg_path
    talkgroups: dict[str, dict[str, str]] = {}
    duplicates = 0
    if not tg_path:
        errors.append("missing talkgroup file: expected talkgroups.csv or talkgroups_with_group.csv")
    else:
        try:
            talkgroups, duplicates = _read_talkgroups(tg_path)
        except Exception as exc:
            errors.append(f"failed to parse talkgroup file {tg_path.name}: {exc}")
            talkgroups = {}
        if not talkgroups:
            errors.append(f"talkgroup file {tg_path.name} has no valid DEC TGIDs")
    result["talkgroups"] = talkgroups
    result["duplicate_tgids"] = duplicates
    if duplicates:
        warnings.append(f"duplicate TGID rows ignored: {duplicates}")

    encrypted = 0
    placeholders = 0
    for row in talkgroups.values():
        if "E" in str(row.get("Mode", "")).upper():
            encrypted += 1
        if _placeholder_label(row.get("Alpha Tag", ""), row.get("Description", "")):
            placeholders += 1
    result["encrypted_count"] = encrypted
    result["placeholder_count"] = placeholders
    if placeholders:
        warnings.append(f"placeholder labels present: {placeholders}")

    listen_path = profile_dir / "talkgroups_listen.json"
    listen_status, listen_schema, listen_default, listen_items, listen_metadata = _read_listen(listen_path)
    result["listen_path"] = listen_path
    result["listen_status"] = listen_status
    result["listen_schema"] = listen_schema
    result["listen_default"] = listen_default
    result["listen_items"] = listen_items
    result["listen_metadata"] = listen_metadata
    if listen_status == "missing":
        warnings.append("missing talkgroups_listen.json")
    elif listen_status == "invalid":
        warnings.append("invalid talkgroups_listen.json")

    missing_listen = [tg for tg in sorted(talkgroups.keys(), key=int) if tg not in listen_items]
    result["missing_listen_tgids"] = missing_listen
    if missing_listen:
        warnings.append(f"listen map missing TGIDs: {len(missing_listen)}")

    if talkgroups and listen_items:
        listened_true = sum(1 for value in listen_items.values() if value)
        if listened_true == 0:
            warnings.append("listen map has zero enabled TGIDs")
        result["listen_true_count"] = listened_true
    else:
        result["listen_true_count"] = 0

    return result


def apply_fixes(result: dict[str, Any], default_listen_for_new: bool) -> list[str]:
    fixes: list[str] = []
    profile_dir: Path = result["profile_dir"]
    talkgroups: dict[str, dict[str, str]] = result.get("talkgroups", {})

    tg_path: Path | None = result.get("talkgroups_path")
    if tg_path and tg_path.name == "talkgroups_with_group.csv":
        target = profile_dir / "talkgroups.csv"
        if not target.exists() and talkgroups:
            _write_talkgroups_csv(target, talkgroups)
            fixes.append("created talkgroups.csv from talkgroups_with_group.csv")

    listen_path: Path = result["listen_path"]
    listen_status: str = result["listen_status"]
    listen_schema: str = result.get("listen_schema", "invalid")
    listen_default: bool = result["listen_default"]
    listen_items: dict[str, bool] = dict(result.get("listen_items", {}))
    listen_metadata: dict[str, dict[str, Any]] = dict(result.get("listen_metadata", {}))
    missing_listen_tgids: list[str] = list(result.get("missing_listen_tgids", []))

    need_listen_fix = listen_status != "ok" or bool(missing_listen_tgids) or listen_schema != "dual"
    if need_listen_fix and talkgroups:
        if listen_path.exists():
            backup = listen_path.with_name(f"{listen_path.name}.bak.{_timestamp()}.canon")
            shutil.copy2(listen_path, backup)
            fixes.append(f"backed up listen map to {backup.name}")

        if listen_status != "ok":
            listen_items = {}
            listen_metadata = {}
            listen_default = default_listen_for_new
        for tgid in sorted(talkgroups.keys(), key=int):
            if tgid not in listen_items:
                listen_items[tgid] = default_listen_for_new
        _write_listen(listen_path, listen_default, listen_items, metadata=listen_metadata)
        fixes.append("wrote talkgroups_listen.json with complete TGID coverage")

    return fixes


def _resolve_profile_dir(args: argparse.Namespace) -> Path:
    if args.profile:
        return Path(args.profile).expanduser().resolve()
    if args.profiles_root and args.profile_id:
        return Path(args.profiles_root).expanduser().resolve() / args.profile_id.strip()
    raise ValueError("provide either --profile, or --profiles-root with --profile-id")


def _status(result: dict[str, Any]) -> str:
    if result["errors"]:
        return "FAIL"
    if result["warnings"]:
        return "WARN"
    return "PASS"


def print_report(result: dict[str, Any]) -> None:
    profile_dir: Path = result["profile_dir"]
    talkgroups_path: Path | None = result.get("talkgroups_path")
    control_hz: list[int] = result.get("control_hz", [])
    talkgroups: dict[str, dict[str, str]] = result.get("talkgroups", {})
    missing_listen_tgids: list[str] = result.get("missing_listen_tgids", [])

    print(f"Profile: {profile_dir}")
    print(f"Status: {_status(result)}")
    print(f"Control channels: {len(control_hz)}")
    print(f"Talkgroup file: {talkgroups_path.name if talkgroups_path else '(missing)'}")
    print(f"Talkgroups: {len(talkgroups)}")
    print(f"Encrypted TGIDs: {result.get('encrypted_count', 0)}")
    print(f"Placeholder labels: {result.get('placeholder_count', 0)}")
    print(f"Duplicate TGID rows ignored: {result.get('duplicate_tgids', 0)}")
    print(
        "Listen map: "
        f"{result.get('listen_status')} "
        f"(schema={result.get('listen_schema', 'unknown')}, "
        f"default_listen={str(result.get('listen_default', True)).lower()}, "
        f"enabled={result.get('listen_true_count', 0)}, "
        f"missing_entries={len(missing_listen_tgids)})"
    )

    if result.get("fixes"):
        print("")
        print("Fixes:")
        for item in result["fixes"]:
            print(f"- {item}")

    if result["errors"]:
        print("")
        print("Errors:")
        for item in result["errors"]:
            print(f"- {item}")

    if result["warnings"]:
        print("")
        print("Warnings:")
        for item in result["warnings"]:
            print(f"- {item}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit/fix digital profile readiness")
    parser.add_argument("--profile", default="", help="Path to profile directory")
    parser.add_argument("--profiles-root", default="", help="Profiles root path")
    parser.add_argument("--profile-id", default="", help="Profile directory name under --profiles-root")
    parser.add_argument("--fix", action="store_true", help="Apply safe canonical fixes")
    parser.add_argument(
        "--default-listen",
        default="true",
        choices=("true", "false"),
        help="Listen value for TGIDs added during --fix (default: true)",
    )
    parser.add_argument(
        "--strict-warnings",
        action="store_true",
        help="Return non-zero if warnings remain after audit/fix",
    )
    return parser


def main(argv: list[str]) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        profile_dir = _resolve_profile_dir(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not profile_dir.is_dir():
        print(f"error: profile directory not found: {profile_dir}", file=sys.stderr)
        return 2

    result = audit_profile(profile_dir)
    if args.fix:
        fixes = apply_fixes(result, default_listen_for_new=_parse_bool(args.default_listen))
        if fixes:
            result = audit_profile(profile_dir)
            result["fixes"] = fixes
        else:
            result["fixes"] = []

    print_report(result)

    if result["errors"]:
        return 1
    if args.strict_warnings and result["warnings"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
