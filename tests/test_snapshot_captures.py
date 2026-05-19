"""Tests for disco/bin/snapshot-captures.py — verifies --for-date hardlinks
files whose mtime falls inside the local-TZ calendar day, skips out-of-window
files, and is idempotent on repeated invocations.

Uses tmpfs (TemporaryDirectory) so we can control mtime precisely without
touching real captures. Imports the script by file path since `disco/bin/`
isn't on the import path by default.
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import tempfile
import unittest


def _load_snapshot_module():
    """Load disco/bin/snapshot-captures.py as a module (the hyphen in the
    filename prevents a normal `import`)."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    path = os.path.join(repo_root, "disco", "bin", "snapshot-captures.py")
    spec = importlib.util.spec_from_file_location("snapshot_captures", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


snapshot_captures = _load_snapshot_module()


def _make_slice(dir_path, name, mtime_ts):
    """Create a placeholder .iq.f32 + .meta sidecar at the given mtime."""
    os.makedirs(dir_path, exist_ok=True)
    iq = os.path.join(dir_path, name)
    meta = iq + ".meta"
    with open(iq, "wb") as fh:
        fh.write(b"\x00" * 64)
    with open(meta, "w") as fh:
        fh.write("snr_db=20.0\n")
    os.utime(iq, (mtime_ts, mtime_ts))
    os.utime(meta, (mtime_ts, mtime_ts))
    return iq, meta


def _local_noon_ts(date_str):
    """Local-TZ noon on the given date — guaranteed to fall inside the
    [00:00, 23:59:59] day window regardless of DST quirks."""
    start, end = snapshot_captures.local_day_window(date_str)
    return (start + end) / 2


class SnapshotForDateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.captures = os.path.join(self.tmp.name, "captures")
        self.training = os.path.join(self.tmp.name, "training_captures")
        os.makedirs(self.captures)

    def test_for_date_links_in_window_skips_out_of_window(self):
        date_str = "2026-05-14"
        in_window_ts = _local_noon_ts(date_str)
        # Day before — should NOT be linked
        before_ts = in_window_ts - 86400
        # Day after — should NOT be linked
        after_ts = in_window_ts + 86400

        # 3 P25 slices: one in-window, one before, one after
        p25 = os.path.join(self.captures, "P25")
        _make_slice(p25, "A-T1_770500000_8500_50000_in.iq.f32", in_window_ts)
        _make_slice(p25, "A-T1_770500000_8500_50000_before.iq.f32", before_ts)
        _make_slice(p25, "A-T1_770500000_8500_50000_after.iq.f32", after_ts)
        # 1 NXDN slice in-window
        nxdn = os.path.join(self.captures, "NXDN")
        _make_slice(nxdn, "A-T2_453000000_5500_50000_in.iq.f32", in_window_ts)
        # _suspicious quarantine dir — must always be skipped
        susp = os.path.join(self.captures, "_suspicious")
        _make_slice(susp, "A-T2_445000000_5000_50000_quarantined.iq.f32", in_window_ts)
        # .gitkeep — dot-prefixed, must be skipped
        os.makedirs(os.path.join(self.captures, ".gitkeep_dir"), exist_ok=True)

        drive_dir, linked, existing, by_label = snapshot_captures.run(
            date_str, self.captures, self.training, db_path="/nonexistent.db"
        )

        # 1 in-window P25 (.iq.f32 + .meta = 2 files) + 1 NXDN (2 files) = 4 links
        self.assertEqual(linked, 4)
        self.assertEqual(existing, 0)
        self.assertEqual(by_label, {"P25": (2, 0), "NXDN": (2, 0)})
        # Verify on-disk: in-window files exist
        self.assertTrue(os.path.exists(os.path.join(drive_dir, "P25", "A-T1_770500000_8500_50000_in.iq.f32")))
        self.assertTrue(os.path.exists(os.path.join(drive_dir, "NXDN", "A-T2_453000000_5500_50000_in.iq.f32")))
        # Out-of-window files NOT linked
        self.assertFalse(os.path.exists(os.path.join(drive_dir, "P25", "A-T1_770500000_8500_50000_before.iq.f32")))
        self.assertFalse(os.path.exists(os.path.join(drive_dir, "P25", "A-T1_770500000_8500_50000_after.iq.f32")))
        # Quarantine dir not linked
        self.assertFalse(os.path.isdir(os.path.join(drive_dir, "_suspicious")))
        # Manifest written
        self.assertTrue(os.path.exists(os.path.join(drive_dir, "manifest.tsv")))

    def test_re_run_is_idempotent_no_double_link(self):
        date_str = "2026-05-14"
        ts = _local_noon_ts(date_str)
        p25 = os.path.join(self.captures, "P25")
        _make_slice(p25, "A-T1_770500000_8500_50000_x.iq.f32", ts)

        # First run: 2 new links (.iq.f32 + .meta)
        _, linked1, existing1, _ = snapshot_captures.run(
            date_str, self.captures, self.training, db_path="/nonexistent.db"
        )
        self.assertEqual((linked1, existing1), (2, 0))
        # Second run: nothing new, both already present
        _, linked2, existing2, _ = snapshot_captures.run(
            date_str, self.captures, self.training, db_path="/nonexistent.db"
        )
        self.assertEqual((linked2, existing2), (0, 2))

    def test_links_share_inode_with_source(self):
        date_str = "2026-05-14"
        ts = _local_noon_ts(date_str)
        p25 = os.path.join(self.captures, "P25")
        src, _ = _make_slice(p25, "A-T1_770500000_8500_50000_inode.iq.f32", ts)

        drive_dir, _, _, _ = snapshot_captures.run(
            date_str, self.captures, self.training, db_path="/nonexistent.db"
        )
        dst = os.path.join(drive_dir, "P25", "A-T1_770500000_8500_50000_inode.iq.f32")
        self.assertEqual(os.stat(src).st_ino, os.stat(dst).st_ino,
                         "hardlink must share inode with source (not be a copy)")

    def test_dry_run_makes_no_fs_changes(self):
        date_str = "2026-05-14"
        ts = _local_noon_ts(date_str)
        p25 = os.path.join(self.captures, "P25")
        _make_slice(p25, "A-T1_770500000_8500_50000_dryrun.iq.f32", ts)

        drive_dir, linked, existing, _ = snapshot_captures.run(
            date_str, self.captures, self.training, db_path="/nonexistent.db", dry_run=True
        )
        # Counts reflect what WOULD be linked
        self.assertEqual(linked, 2)
        self.assertEqual(existing, 0)
        # But no destination dir got created
        self.assertFalse(os.path.exists(os.path.join(drive_dir, "P25")))
        # And no manifest
        self.assertFalse(os.path.exists(os.path.join(drive_dir, "manifest.tsv")))

    def test_empty_day_writes_no_manifest(self):
        # No files in captures — script exits cleanly without writing
        # an empty manifest.
        drive_dir, linked, existing, by_label = snapshot_captures.run(
            "2026-05-14", self.captures, self.training, db_path="/nonexistent.db"
        )
        self.assertEqual((linked, existing), (0, 0))
        self.assertEqual(by_label, {})
        self.assertIsNone(drive_dir)


if __name__ == "__main__":
    unittest.main()
