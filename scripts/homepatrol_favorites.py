#!/usr/bin/env python3
"""Work with HomePatrol Favorites lists stored in an HPCOPY zip backup.

This script turns curated Favorites list content into scannerproject-ready files:
- control_channels.txt
- talkgroups.csv
- talkgroups_with_group.csv
- conventional.csv (reference for analog channels)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


def _field(parts: list[str], idx: int) -> str:
    if idx < 0 or idx >= len(parts):
        return ""
    return (parts[idx] or "").strip()


def _to_int(text: str) -> int | None:
    value = (text or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(text: str) -> float | None:
    value = (text or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _mhz_from_hz(freq_hz: int) -> str:
    return (f"{freq_hz / 1_000_000:.6f}").rstrip("0").rstrip(".")


def _slug(text: str) -> str:
    out: list[str] = []
    last_dash = False
    for ch in text.strip().lower():
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out.append(ch)
            last_dash = False
        elif ch in {" ", "-", "_", "."} and not last_dash:
            out.append("-")
            last_dash = True
    while out and out[0] == "-":
        out = out[1:]
    while out and out[-1] == "-":
        out.pop()
    return "".join(out)[:64] or "favorite"


@dataclass
class FavoriteEntry:
    name: str
    filename: str
    monitor: str


@dataclass
class ParsedFavorite:
    trunk_systems: set[str] = field(default_factory=set)
    site_names: set[str] = field(default_factory=set)
    control_hz: set[int] = field(default_factory=set)
    trunk_sites: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    talkgroups: list[dict[str, str]] = field(default_factory=list)
    conventional: list[dict[str, str]] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)


def _kv_map(parts: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in parts[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in out:
            out[key] = value
    return out


def _favorite_site_id(system_name: str, site_name: str, raw_site_id: str) -> str:
    site_id = (raw_site_id or "").strip()
    if site_id:
        return site_id
    return f"fav:{_slug(system_name)}:{_slug(site_name) or 'site'}"


def _ensure_trunk_site(
    parsed: ParsedFavorite,
    *,
    system_name: str,
    system_id: str,
    site_name: str,
    raw_site_id: str,
    latitude: float | None,
    longitude: float | None,
    radius: float | None,
) -> dict[str, object]:
    site_id = _favorite_site_id(system_name, site_name, raw_site_id)
    key = (system_name, site_id)
    site = parsed.trunk_sites.get(key)
    if site is None:
        site = {
            "system_name": system_name,
            "system_id": system_id,
            "site_id": site_id,
            "site_name": site_name or "Favorite Site",
            "latitude": latitude,
            "longitude": longitude,
            "radius": radius,
            "control_channels_hz": set(),
        }
        parsed.trunk_sites[key] = site
    else:
        if latitude is not None:
            site["latitude"] = latitude
        if longitude is not None:
            site["longitude"] = longitude
        if radius is not None:
            site["radius"] = radius
        if site_name:
            site["site_name"] = site_name
        if system_id and not site.get("system_id"):
            site["system_id"] = system_id
    return site


def _load_favorites_config(zf: zipfile.ZipFile) -> list[FavoriteEntry]:
    path = "HPCOPY/favorites_lists/favorites_lists.config"
    if path not in zf.namelist():
        return []
    entries: list[FavoriteEntry] = []
    with zf.open(path) as handle:
        for raw in handle:
            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
            if not line.startswith("F-List\t"):
                continue
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            entries.append(
                FavoriteEntry(
                    name=_field(parts, 1),
                    filename=_field(parts, 2),
                    monitor=_field(parts, 4),
                )
            )
    return entries


def _resolve_favorite_path(
    zf: zipfile.ZipFile,
    entries: list[FavoriteEntry],
    favorite: str,
) -> tuple[str, FavoriteEntry | None]:
    favorite = (favorite or "").strip()
    if not favorite:
        raise ValueError("favorite name/file is required")
    direct = f"HPCOPY/favorites_lists/{favorite}"
    if direct in zf.namelist():
        return direct, None
    for entry in entries:
        if entry.filename.lower() == favorite.lower() or entry.name.lower() == favorite.lower():
            path = f"HPCOPY/favorites_lists/{entry.filename}"
            if path in zf.namelist():
                return path, entry
    raise ValueError(f"favorite not found: {favorite}")


def _parse_favorite_hpd(zf: zipfile.ZipFile, path: str) -> ParsedFavorite:
    parsed = ParsedFavorite()

    current_trunk = ""
    current_trunk_id = ""
    current_site = ""
    current_site_ref: dict[str, object] | None = None
    current_tgroup = ""
    current_conventional = ""
    current_cgroup = ""

    with zf.open(path) as handle:
        for raw in handle:
            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
            if not line:
                continue
            parts = line.split("\t")
            rec = _field(parts, 0)
            kv = _kv_map(parts)
            parsed.counts[rec] += 1

            if rec == "Trunk":
                current_trunk = _field(parts, 3)
                current_trunk_id = kv.get("TrunkId", "")
                current_site = ""
                current_site_ref = None
                current_tgroup = ""
                if current_trunk:
                    parsed.trunk_systems.add(current_trunk)
                continue

            if rec == "Site":
                current_site = _field(parts, 3)
                if current_site:
                    parsed.site_names.add(current_site)
                current_site_ref = _ensure_trunk_site(
                    parsed,
                    system_name=current_trunk,
                    system_id=current_trunk_id,
                    site_name=current_site,
                    raw_site_id=kv.get("SiteId", ""),
                    latitude=_to_float(_field(parts, 5)),
                    longitude=_to_float(_field(parts, 6)),
                    radius=_to_float(_field(parts, 7)),
                )
                continue

            if rec == "T-Freq":
                freq_hz = _to_int(_field(parts, 5))
                if freq_hz and freq_hz > 0:
                    parsed.control_hz.add(freq_hz)
                    site_ref = current_site_ref
                    if current_trunk and site_ref is None:
                        site_ref = _ensure_trunk_site(
                            parsed,
                            system_name=current_trunk,
                            system_id=current_trunk_id,
                            site_name=current_site or "Favorite Site",
                            raw_site_id=kv.get("SiteId", ""),
                            latitude=None,
                            longitude=None,
                            radius=None,
                        )
                        current_site_ref = site_ref
                    if site_ref is not None:
                        site_ref["control_channels_hz"].add(freq_hz)
                continue

            if rec == "T-Group":
                current_tgroup = _field(parts, 3)
                continue

            if rec == "TGID":
                dec = _field(parts, 5)
                if not dec.isdigit():
                    continue
                dec_int = int(dec)
                service_tag = _field(parts, 7)
                parsed.talkgroups.append(
                    {
                        "group": current_tgroup,
                        "dec": dec,
                        "hex": f"{dec_int:X}",
                        "mode": _field(parts, 6),
                        "alpha": _field(parts, 3),
                        "description": current_tgroup,
                        "tag": f"Svc {service_tag}" if service_tag.isdigit() else "",
                    }
                )
                continue

            if rec == "Conventional":
                current_conventional = _field(parts, 3)
                current_cgroup = ""
                continue

            if rec == "C-Group":
                current_cgroup = _field(parts, 3)
                continue

            if rec == "C-Freq":
                freq_hz = _to_int(_field(parts, 5))
                if not freq_hz:
                    continue
                service_tag = _field(parts, 8)
                parsed.conventional.append(
                    {
                        "system": current_conventional,
                        "group": current_cgroup,
                        "alpha": _field(parts, 3),
                        "enabled": _field(parts, 4),
                        "freq_hz": str(freq_hz),
                        "freq_mhz": _mhz_from_hz(freq_hz),
                        "mode": _field(parts, 6),
                        "tone": _field(parts, 7),
                        "service_tag": service_tag,
                    }
                )
                continue

    return parsed


def cmd_list(args: argparse.Namespace) -> int:
    zpath = Path(args.zip).expanduser()
    if not zpath.is_file():
        print(f"error: zip not found: {zpath}")
        return 2
    with zipfile.ZipFile(zpath) as zf:
        entries = _load_favorites_config(zf)
        if not entries:
            print("no favorites found")
            return 0
        print("name\tfile\tmonitor\ttrunk\tsites\ttalkgroups\tconventional")
        for entry in entries:
            fpath = f"HPCOPY/favorites_lists/{entry.filename}"
            if fpath not in zf.namelist():
                continue
            parsed = _parse_favorite_hpd(zf, fpath)
            print(
                f"{entry.name}\t{entry.filename}\t{entry.monitor}\t"
                f"{parsed.counts['Trunk']}\t{parsed.counts['Site']}\t"
                f"{len(parsed.talkgroups)}\t{parsed.counts['C-Freq']}"
            )
    return 0


def _write_digital_files(profile_dir: Path, parsed: ParsedFavorite) -> None:
    control = sorted(parsed.control_hz)
    (profile_dir / "control_channels.txt").write_text(
        "\n".join(_mhz_from_hz(freq) for freq in control) + ("\n" if control else ""),
        encoding="utf-8",
    )

    talkgroups = sorted(parsed.talkgroups, key=lambda row: (int(row["dec"]), row["alpha"]))
    with (profile_dir / "talkgroups.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
        for row in talkgroups:
            writer.writerow([row["dec"], row["hex"], row["mode"], row["alpha"], row["description"], row["tag"]])

    with (profile_dir / "talkgroups_with_group.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Group", "DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
        for row in talkgroups:
            writer.writerow(
                [
                    row["group"],
                    row["dec"],
                    row["hex"],
                    row["mode"],
                    row["alpha"],
                    row["description"],
                    row["tag"],
                ]
            )

    site_groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for site in parsed.trunk_sites.values():
        control_channels_hz = sorted(int(freq) for freq in site["control_channels_hz"])
        if not control_channels_hz:
            continue
        system_name = str(site["system_name"] or "").strip()
        if not system_name:
            continue
        system_id = str(site.get("system_id") or "").strip()
        key = (system_name, system_id)
        site_groups.setdefault(key, []).append(
            {
                "site_id": str(site["site_id"]),
                "site_name": str(site["site_name"]),
                "control_channels_hz": control_channels_hz,
                "latitude": site.get("latitude"),
                "longitude": site.get("longitude"),
                "radius": site.get("radius"),
                "enabled": True,
            }
        )

    if site_groups:
        systems = []
        for (system_name, system_id), sites in sorted(site_groups.items(), key=lambda item: item[0][0].lower()):
            systems.append(
                {
                    "name": system_name,
                    "system_id": system_id,
                    "sites": sorted(sites, key=lambda row: (row["site_name"], row["site_id"])),
                }
            )
        with (profile_dir / "systems.json").open("w", encoding="utf-8") as handle:
            json.dump({"systems": systems}, handle, indent=2)
            handle.write("\n")


def _write_conventional_file(profile_dir: Path, parsed: ParsedFavorite) -> None:
    rows = sorted(
        parsed.conventional,
        key=lambda row: (row["system"], row["group"], float(row["freq_mhz"]), row["alpha"]),
    )
    with (profile_dir / "conventional.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["System", "Group", "Alpha", "Enabled", "Freq MHz", "Freq Hz", "Mode", "Tone", "Service Tag"]
        )
        for row in rows:
            writer.writerow(
                [
                    row["system"],
                    row["group"],
                    row["alpha"],
                    row["enabled"],
                    row["freq_mhz"],
                    row["freq_hz"],
                    row["mode"],
                    row["tone"],
                    row["service_tag"],
                ]
            )


def cmd_export(args: argparse.Namespace) -> int:
    zpath = Path(args.zip).expanduser()
    if not zpath.is_file():
        print(f"error: zip not found: {zpath}")
        return 2
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zpath) as zf:
        entries = _load_favorites_config(zf)
        try:
            fpath, matched = _resolve_favorite_path(zf, entries, args.favorite)
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
        parsed = _parse_favorite_hpd(zf, fpath)

    profile_name = args.profile_name.strip() if args.profile_name else (matched.name if matched else args.favorite)
    profile_slug = _slug(args.profile_slug) if args.profile_slug else _slug(profile_name)
    profile_dir = out_dir / profile_slug
    profile_dir.mkdir(parents=True, exist_ok=True)

    _write_digital_files(profile_dir, parsed)
    _write_conventional_file(profile_dir, parsed)

    created = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    systems = ", ".join(sorted(parsed.trunk_systems)) if parsed.trunk_systems else "(none)"
    sites = ", ".join(sorted(parsed.site_names)) if parsed.site_names else "(none)"
    monitor = matched.monitor if matched else ""
    readme = (
        f"# {profile_name}\n\n"
        f"- Source zip: `{zpath}`\n"
        f"- Source favorite file: `{Path(fpath).name}`\n"
        f"- Monitor state in scanner: `{monitor}`\n"
        f"- Generated: {created}\n"
        f"- Trunk systems: {systems}\n"
        f"- Sites: {sites}\n"
        f"- Control frequencies: `{len(parsed.control_hz)}` in `control_channels.txt`\n"
        f"- Candidate sites: `{len(parsed.trunk_sites)}` in `systems.json` when trunk data is present\n"
        f"- Talkgroups: `{len(parsed.talkgroups)}` in `talkgroups.csv`\n"
        f"- Conventional channels: `{len(parsed.conventional)}` in `conventional.csv`\n"
    )
    (profile_dir / "README.md").write_text(readme, encoding="utf-8")

    print(f"Profile exported: {profile_dir}")
    print(f"Trunk systems: {len(parsed.trunk_systems)}")
    print(f"Control channels: {len(parsed.control_hz)}")
    print(f"Talkgroups: {len(parsed.talkgroups)}")
    print(f"Conventional channels: {len(parsed.conventional)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert HomePatrol favorites from HPCOPY zip")
    parser.add_argument("--zip", required=True, help="Path to HPCOPY zip")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List favorites in zip with quick counts")
    p_list.set_defaults(func=cmd_list)

    p_export = sub.add_parser("export", help="Export one favorite to scannerproject files")
    p_export.add_argument("--favorite", required=True, help="Favorite display name or favorites_XXXXXX.hpd")
    p_export.add_argument("--out", required=True, help="Output directory")
    p_export.add_argument("--profile-name", default="", help="Friendly profile name")
    p_export.add_argument("--profile-slug", default="", help="Folder name override")
    p_export.set_defaults(func=cmd_export)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
