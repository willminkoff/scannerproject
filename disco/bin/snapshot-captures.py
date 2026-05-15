#!/usr/bin/env python3
"""Daily auto-snapshot of /captures/<LABEL>/ → /training_captures/<DATE>/<LABEL>/.

Walks each label dir under CAPTURE_DIR, finds .iq.f32 files (plus their .meta
sidecars) with mtime in the target calendar day [00:00:00, 23:59:59] LOCAL,
hardlinks them into the dated bucket, and writes manifest.tsv joining DB rows.

Hardlinks (not copies) — same inode, no double-write, idempotent re-runs
because os.link() is a no-op when the destination already exists (we catch
FileExistsError and count as "already-present").

Daily timer fires at 23:55 LOCAL via disco-snapshot-captures.timer; you can
also run ad-hoc with `--for-date YYYY-MM-DD` to bundle a specific day.
"""

import argparse
import datetime
import os
import sqlite3
import sys

CAPTURE_DIR = os.environ.get(
    "DISCO_CAPTURE_DIR", "/home/ubuntu/scannerproject/disco/captures"
)
TRAINING_CAPTURES_DIR = os.environ.get(
    "DISCO_TRAINING_CAPTURES_DIR", "/home/ubuntu/scannerproject/disco/training_captures"
)
DB_PATH = os.environ.get(
    "DISCO_DB_PATH", "/home/ubuntu/scannerproject/disco/state/disco.sqlite"
)
TZ_LOCAL = os.environ.get("DISCO_LOCAL_TZ", "America/Chicago")

# Label dirs to skip when walking CAPTURE_DIR. Dot-files (.gitkeep etc.) and
# the operator-managed _suspicious quarantine never get bundled.
SKIP_LABEL_PREFIXES = (".",)
SKIP_LABEL_NAMES = {"_suspicious"}


def local_day_window(date_str):
    """Return (start_ts, end_ts) UTC seconds covering [date 00:00:00.000,
    date 23:59:59.999999] in the TZ_LOCAL timezone. Falls back to UTC when
    zoneinfo isn't available (older Python or missing tzdata)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ_LOCAL)
    except Exception:
        tz = datetime.timezone.utc
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime.datetime.combine(d, datetime.time.min, tz)
    end = datetime.datetime.combine(d, datetime.time.max, tz)
    return start.timestamp(), end.timestamp()


def hardlink_or_skip(src, dst):
    """Hardlink src → dst. Returns True if a new link was created, False if
    dst already existed. Raises OSError on cross-FS / permission errors."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
        return True
    except FileExistsError:
        return False


def write_manifest(drive_dir, db_path):
    """For every .iq.f32 in drive_dir/<LABEL>/, look up the DB row by
    slice_path basename and write manifest.tsv. DB unavailability is
    non-fatal — the TSV gets the file rows with empty detection columns."""
    out_path = os.path.join(drive_dir, "manifest.tsv")
    rows_out = []
    for lab in sorted(os.listdir(drive_dir)):
        sub = os.path.join(drive_dir, lab)
        if not os.path.isdir(sub) or lab.startswith("."):
            continue
        for fn in sorted(os.listdir(sub)):
            if not fn.endswith(".iq.f32"):
                continue
            rows_out.append((lab, fn))
    c = None
    try:
        c = sqlite3.connect(db_path, timeout=10)
        c.row_factory = sqlite3.Row
    except Exception as e:
        print(f"  WARN: DB unreachable ({e}) — manifest will have no detection columns", file=sys.stderr)
    with open(out_path, "w") as fh:
        fh.write("\t".join([
            "rule_label", "filename", "det_id", "ts_utc", "tuner", "freq_mhz",
            "bw_khz", "snr_db", "ml_class", "ml_conf", "protocol_tag",
            "uls_callsign", "uls_entity_name", "uls_distance_km",
        ]) + "\n")
        for lab, fn in rows_out:
            r = None
            if c is not None:
                try:
                    r = c.execute(
                        "SELECT id, ts, tuner_id, freq_hz, bandwidth_hz, snr_db, "
                        "modulation_class, modulation_confidence, protocol_tag, "
                        "uls_callsign, uls_entity_name, uls_distance_km "
                        "FROM detections WHERE slice_path LIKE ? LIMIT 1",
                        ("%/" + fn,),
                    ).fetchone()
                except Exception:
                    r = None
            if r:
                ts_iso = (
                    datetime.datetime.fromtimestamp(r["ts"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if r["ts"] else ""
                )
                fh.write("\t".join([
                    lab, fn, str(r["id"]), ts_iso, r["tuner_id"] or "",
                    f"{(r['freq_hz'] or 0)/1e6:.4f}",
                    f"{(r['bandwidth_hz'] or 0)/1e3:.1f}",
                    f"{r['snr_db']:.1f}" if r["snr_db"] is not None else "",
                    r["modulation_class"] or "",
                    f"{r['modulation_confidence']:.2f}" if r["modulation_confidence"] is not None else "",
                    r["protocol_tag"] or "",
                    r["uls_callsign"] or "",
                    (r["uls_entity_name"] or "").replace("\t", " ").replace("\n", " "),
                    f"{r['uls_distance_km']:.1f}" if r["uls_distance_km"] is not None else "",
                ]) + "\n")
            else:
                fh.write("\t".join([lab, fn] + [""] * 12) + "\n")
    if c is not None:
        c.close()
    return out_path, len(rows_out)


def run(date_str, capture_dir, training_captures_dir, db_path, dry_run=False):
    """Snapshot routine — separated from CLI so tests can exercise it directly.

    Returns (drive_dir, total_linked, total_existing, by_label). drive_dir is
    None when total_linked == total_existing == 0 (nothing to snapshot)."""
    start_ts, end_ts = local_day_window(date_str)
    print(
        f"[snapshot-captures] date={date_str} window=[{start_ts:.0f}, {end_ts:.0f}] "
        f"capture_dir={capture_dir} training_captures_dir={training_captures_dir}"
        f"{' DRY-RUN' if dry_run else ''}",
        flush=True,
    )

    drive_dir = os.path.join(training_captures_dir, date_str)
    by_label = {}
    total_linked = 0
    total_existing = 0
    try:
        labels = sorted(os.listdir(capture_dir))
    except FileNotFoundError:
        print(f"  WARN: capture_dir {capture_dir} not found", file=sys.stderr)
        return drive_dir, 0, 0, {}

    for label in labels:
        if any(label.startswith(p) for p in SKIP_LABEL_PREFIXES) or label in SKIP_LABEL_NAMES:
            continue
        lab_dir = os.path.join(capture_dir, label)
        if not os.path.isdir(lab_dir):
            continue
        linked = 0
        existing = 0
        for fn in os.listdir(lab_dir):
            if not (fn.endswith(".iq.f32") or fn.endswith(".iq.f32.meta")):
                continue
            src = os.path.join(lab_dir, fn)
            try:
                mtime = os.path.getmtime(src)
            except OSError:
                continue
            if not (start_ts <= mtime <= end_ts):
                continue
            dst = os.path.join(drive_dir, label, fn)
            if dry_run:
                if os.path.exists(dst):
                    existing += 1
                else:
                    linked += 1
                continue
            try:
                if hardlink_or_skip(src, dst):
                    linked += 1
                else:
                    existing += 1
            except OSError as e:
                print(f"  WARN: link {src} → {dst}: {e}", file=sys.stderr)
        if linked or existing:
            by_label[label] = (linked, existing)
            total_linked += linked
            total_existing += existing

    for label, (linked, existing) in sorted(by_label.items()):
        print(f"  {label:14s}  linked={linked:5d}  already-present={existing:5d}", flush=True)
    print(f"  TOTAL          linked={total_linked:5d}  already-present={total_existing:5d}", flush=True)

    if dry_run:
        print("[snapshot-captures] dry-run — no FS changes, no manifest write", flush=True)
        return drive_dir, total_linked, total_existing, by_label

    if total_linked == 0 and total_existing == 0:
        print(f"[snapshot-captures] no slices to snapshot for {date_str} — exiting cleanly", flush=True)
        return None, 0, 0, {}

    os.makedirs(drive_dir, exist_ok=True)
    out_path, n = write_manifest(drive_dir, db_path)
    print(f"[snapshot-captures] wrote {out_path}  ({n} rows)", flush=True)
    return drive_dir, total_linked, total_existing, by_label


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--for-date",
        help="YYYY-MM-DD in local TZ (defaults to today). Used for ad-hoc snapshot runs.",
    )
    ap.add_argument("--capture-dir", default=CAPTURE_DIR)
    ap.add_argument("--training-captures-dir", default=TRAINING_CAPTURES_DIR)
    ap.add_argument("--db-path", default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true", help="Print what would be linked; touch nothing.")
    args = ap.parse_args()
    date_str = args.for_date or datetime.datetime.now().strftime("%Y-%m-%d")
    run(date_str, args.capture_dir, args.training_captures_dir, args.db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
