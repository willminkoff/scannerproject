#!/usr/bin/env python3
"""chirp/scripts/migrate_state.py — one-time pre-populate chirp daemon state.

Phase 4c.  Reads the existing rtl-airband-side state of the dashboard
(favorites + per-band preset overrides) and writes the equivalent
``chirp/state.ChirpState`` JSON files to
``/var/lib/chirp/airband.state.json`` and
``/var/lib/chirp/ground.state.json``.

Used during Phase 4d cutover so the chirp daemons come up
pre-populated with the operator's active favorite + current preset
thresholds instead of starting empty.

Inputs:
  - ``data/hp_state.json`` — favorites + per-band activation flags.
  - ``profiles/managed_analog_controls.json`` — per-band preset +
    threshold metadata.
  - ``profiles/rtl_airband_*_<band>.conf`` (resolved via
    ``ui.profile_config``) — the per-channel ``squelch_threshold``
    list that the squelch_tracker has been maintaining.

Outputs (two files):
  - ``/var/lib/chirp/airband.state.json``
  - ``/var/lib/chirp/ground.state.json``

Both written via the same atomic-write contract as the daemon
(``chirp.state.StateStore.save``).

Idempotent: running with ``--apply`` against a state file that
already matches the planned content is a no-op (no write).

``--dry-run`` prints the planned content + a per-band diff but
makes no writes.  Default mode is ``--dry-run`` — operator must
pass ``--apply`` to mutate disk.

Exit codes:
  0 — success (no-op or write completed)
  1 — input error (missing favorites, unparseable profile)
  2 — output error (state dir unwritable)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# --- repo + import setup -----------------------------------------------------

# Resolve repo root from this script's location so the script can be
# executed via systemd / cron / `python3 chirp/scripts/migrate_state.py`.
REPO = Path(__file__).resolve().parents[2]  # .../scannerproject
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# --- per-band band-membership filter ----------------------------------------


def _is_air_freq(mhz: float) -> bool:
    """rtl-airband convention: airband < 137 MHz."""
    return 0.0 < mhz < 137.0


def _is_ground_freq(mhz: float) -> bool:
    """rtl-airband convention: ground >= 137 MHz (the 138 MHz NFM cluster)."""
    return mhz >= 137.0


def _band_membership(band: str, mhz: float) -> bool:
    if band == "airband":
        return _is_air_freq(mhz)
    return _is_ground_freq(mhz)


# --- HP state reader --------------------------------------------------------


def load_hp_state(path: Path) -> dict:
    """Load the dashboard favorites JSON.  Returns the parsed dict.

    Raises FileNotFoundError if missing — the cutover should not run
    without favorites already on disk.
    """
    with open(path, "r") as f:
        return json.load(f)


def active_favorite(hp_state: dict, band: str) -> dict | None:
    """Return the favorite dict with ``enabled_<air|ground>``=true.

    Mirrors the activation logic in ``/api/hp/state/activate``.
    Returns None if no favorite is activated for the given band.
    """
    key = f"enabled_{'air' if band == 'airband' else 'ground'}"
    for fav in hp_state.get("favorites") or []:
        if isinstance(fav, dict) and bool(fav.get(key)):
            return fav
    return None


def custom_favorite_freqs(hp_state: dict, band: str) -> list[dict]:
    """Extract the per-channel definitions for the given band.

    Returns list of ``{"id": str, "freq_mhz": float, "label": str|None,
    "mode": "am"|"nfm"}``.  Filters by band-membership (< 137 MHz for
    airband, >= 137 MHz for ground).
    """
    out: list[dict] = []
    for entry in hp_state.get("custom_favorites") or []:
        if not isinstance(entry, dict):
            continue
        try:
            freq = float(entry.get("frequency") or 0.0)
        except (TypeError, ValueError):
            continue
        if freq <= 0.0:
            continue
        if not _band_membership(band, freq):
            continue
        cid_raw = str(entry.get("id") or "").strip()
        cid = cid_raw[:64] if cid_raw else f"freq:{freq:.4f}"
        tag = str(entry.get("alpha_tag") or entry.get("department_name") or "").strip()
        out.append({
            "id": cid,
            "freq_mhz": float(freq),
            "label": tag[:64] if tag else None,
            "mode": "am" if band == "airband" else "nfm",
        })
    return out


# --- preset / threshold reader ----------------------------------------------


def load_preset_override(controls_path: Path, band: str) -> dict:
    """Read the per-band override block from managed_analog_controls.json.

    Returns ``{}`` if the file is missing or the band has no override.
    """
    if not controls_path.exists():
        return {}
    try:
        with open(controls_path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    targets = data.get("targets") or {}
    block = targets.get(band) or {}
    return dict(block.get("override") or {})


# --- builder ----------------------------------------------------------------


def build_state(band: str, hp_state: dict, override: dict,
                default_gain_db: float = 0.0) -> dict:
    """Produce the planned ``ChirpState``-shaped dict for one band.

    Output schema mirrors ``chirp.state.ChirpState``:
      {
        "schema_version": 1,
        "band": <band>,
        "master_gain_db": 0.0,
        "presets": { "active_preset": ..., "margin_db": ..., ... },
        "channels": [
           {"id": ..., "freq_mhz": ..., "mode": ...,
            "squelch_dbfs": ..., "gain_db": ..., "label": ...},
           ...
        ],
      }
    """
    chans_in = custom_favorite_freqs(hp_state, band)

    # squelch_dbfs source of truth (in order):
    #   1) the preset override's squelch_dbfs (median threshold)
    #   2) -60 dBFS safe default (matches rtl-airband's initial state)
    default_squelch = float(override.get("squelch_dbfs", -60.0))

    # Per-channel gain: the ``override.gain`` value from
    # managed_analog_controls.json is the operator's SDR FRONT-END gain
    # in dB — it goes into the SDR's RF chain (see chirp/config/*.json
    # ``sdr.gain_db``), NOT into the per-channel audio trim.  Phase 4d
    # (2026-06-04) corrects this: the migration now writes
    # ``gain_db: 0.0`` per channel.  The RF gain stays in the per-band
    # config's sdr block where it belongs.
    #
    # ``default_gain_db`` is still respected so callers/tests can pass a
    # non-zero per-channel trim if they have a real reason to — that's
    # what ``Channel.set_gain`` is for (clamped to ±20 dB).  The
    # ``override.gain`` is intentionally ignored.
    _ = default_gain_db  # keep the API surface; not consulted in default flow
    channel_audio_trim_db = 0.0

    channels = []
    for c in chans_in:
        channels.append({
            "id": c["id"],
            "freq_mhz": float(c["freq_mhz"]),
            "mode": c["mode"],
            "squelch_dbfs": float(default_squelch),
            "gain_db": float(channel_audio_trim_db),
            "label": c["label"],
        })

    # Stash preset metadata under ``presets`` so the daemon can hand it
    # back via get_status without the airband-ui needing a separate
    # round-trip to managed_analog_controls.json.
    presets: dict[str, Any] = {}
    for k in (
        "squelch_preset", "squelch_preset_margin_db",
        "squelch_preset_noise_floor_dbfs",
        "squelch_preset_computed_at_ms",
        "squelch_auto",
        "squelch_tracker_applied_at_ms",
    ):
        if k in override:
            presets[k] = override[k]

    return {
        "schema_version": 1,
        "band": band,
        "master_gain_db": 0.0,
        "channels": channels,
        "presets": presets,
    }


# --- comparison / diff ------------------------------------------------------


def read_existing_state(path: Path) -> dict | None:
    """Read an existing chirp state file.  None if missing/corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def state_matches(planned: dict, existing: dict | None) -> bool:
    """Idempotency check.  True if existing state is byte-equivalent to
    the planned state for our purposes (channel set, presets, master gain).

    We deliberately ignore the daemon's own ephemeral metadata
    (e.g. last_squelch_dbfs updates while the daemon was running) and
    only consider the persistent shape.
    """
    if existing is None:
        return False
    # Compare normalized canonical representations.
    return _canonical(planned) == _canonical(existing)


def _canonical(state: dict) -> dict:
    """Strip extra keys + sort channels so two states compare equal iff
    they represent the same daemon-bootable shape."""
    chans = sorted(
        ({k: state.get(k) for k in
          ("id", "freq_mhz", "mode", "squelch_dbfs", "gain_db", "label")}
         for state in state.get("channels") or []),
        key=lambda c: (c.get("id") or "", c.get("freq_mhz") or 0.0),
    )
    return {
        "schema_version": int(state.get("schema_version", 1)),
        "band": state.get("band"),
        "master_gain_db": float(state.get("master_gain_db") or 0.0),
        "channels": chans,
        "presets": state.get("presets") or {},
    }


def diff_summary(planned: dict, existing: dict | None) -> str:
    """Human-readable summary for --dry-run."""
    if existing is None:
        return (
            f"  NEW FILE — {len(planned['channels'])} channels, "
            f"preset={planned['presets'].get('squelch_preset', '(none)')}"
        )
    pc = {c["id"]: c for c in planned["channels"]}
    ec = {c.get("id"): c for c in existing.get("channels") or []}
    add = sorted(set(pc) - set(ec))
    rem = sorted(set(ec) - set(pc))
    changed = []
    for cid in sorted(set(pc) & set(ec)):
        a = pc[cid]; b = ec[cid]
        if any(a.get(k) != b.get(k) for k in
               ("freq_mhz", "mode", "squelch_dbfs", "gain_db", "label")):
            changed.append(cid)
    if not (add or rem or changed):
        return f"  unchanged — {len(pc)} channels, preset={planned['presets'].get('squelch_preset', '(none)')}"
    parts = []
    if add:
        parts.append(f"+{len(add)} added")
    if rem:
        parts.append(f"-{len(rem)} removed")
    if changed:
        parts.append(f"~{len(changed)} changed")
    return "  " + ", ".join(parts)


# --- atomic write -----------------------------------------------------------


def atomic_write(path: Path, content: str) -> None:
    """tmp file in same dir -> fsync -> rename.  Crash-safe.

    Matches chirp.state.StateStore.save's contract.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


# --- CLI --------------------------------------------------------------------


def default_state_path(band: str) -> Path:
    """Match chirp.state.default_state_path's convention."""
    env = os.environ.get("CHIRP_STATE_DIR")
    base = Path(env) if env else Path("/var/lib/chirp")
    return base / f"{band}.state.json"


def default_hp_state_path() -> Path:
    return REPO / "data" / "hp_state.json"


def default_controls_path() -> Path:
    return REPO / "profiles" / "managed_analog_controls.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate dashboard state into chirp daemon state files."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the state files (default is dry-run).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print plan + diff but make no writes (default).",
    )
    parser.add_argument(
        "--hp-state",
        default=str(default_hp_state_path()),
        help="Path to data/hp_state.json (default: %(default)s)",
    )
    parser.add_argument(
        "--controls",
        default=str(default_controls_path()),
        help="Path to profiles/managed_analog_controls.json (default: %(default)s)",
    )
    parser.add_argument(
        "--bands",
        default="airband,ground",
        help="Comma-separated band list (default: %(default)s)",
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("CHIRP_STATE_DIR", "/var/lib/chirp"),
        help="Directory for chirp state files (default: %(default)s)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print planned JSON (one block per band).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if not args.verbose else logging.DEBUG,
        format="migrate_state: %(message)s",
    )

    # --dry-run is the default; --apply opts in to writes.
    do_write = bool(args.apply) and not bool(args.dry_run)

    hp_path = Path(args.hp_state)
    controls_path = Path(args.controls)
    state_dir = Path(args.state_dir)
    bands = [b.strip() for b in args.bands.split(",") if b.strip()]

    if not hp_path.exists():
        print(f"ERROR: hp_state file not found: {hp_path}", file=sys.stderr)
        return 1

    hp_state = load_hp_state(hp_path)

    any_write_needed = False
    print(f"chirp/migrate_state — mode={'APPLY' if do_write else 'DRY-RUN'}")
    print(f"  hp_state     : {hp_path}")
    print(f"  controls     : {controls_path}")
    print(f"  state dir    : {state_dir}")
    print(f"  bands        : {', '.join(bands)}")

    for band in bands:
        if band not in ("airband", "ground"):
            print(f"WARN: unknown band {band!r}, skipping")
            continue
        print(f"\n[{band}]")
        override = load_preset_override(controls_path, band)
        active_fav = active_favorite(hp_state, band)
        print(
            f"  active favorite : "
            + (
                f"{active_fav.get('label') or active_fav.get('id')}"
                if active_fav else "(none)"
            )
        )
        planned = build_state(band, hp_state, override)
        print(
            f"  channels (planned): {len(planned['channels'])}, "
            f"preset={planned['presets'].get('squelch_preset', '(none)')}"
        )
        out_path = state_dir / f"{band}.state.json"
        existing = read_existing_state(out_path)
        already = state_matches(planned, existing)
        print(f"  target path     : {out_path}")
        print(f"  existing match  : {already}")
        print(diff_summary(planned, existing))

        if args.verbose:
            print("  planned JSON   :")
            for line in json.dumps(planned, indent=2).splitlines():
                print("    " + line)

        if already:
            continue
        any_write_needed = True
        if not do_write:
            continue
        # Apply
        content = json.dumps(planned, separators=(",", ":"), sort_keys=False)
        try:
            atomic_write(out_path, content)
        except OSError as e:
            print(f"ERROR: write failed for {out_path}: {e}", file=sys.stderr)
            return 2
        print(f"  WROTE {out_path} ({len(content)} bytes)")

    if not any_write_needed:
        print("\nNo writes needed — state already up to date.")
    elif not do_write:
        print("\n(Dry run — no changes written.  Re-run with --apply to mutate state files.)")
    else:
        print("\nApply complete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
