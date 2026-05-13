"""Tests for managed-profile serial re-sync in ui.favorites_runtime.

`_ensure_managed_profile` is called on every favorites-sync pass.  Previously
it only seeded the serial when the managed profile was first created; if the
operator later swapped the dongle and updated AIRBAND_RTL_SERIAL /
GROUND_RTL_SERIAL in /etc/airband-ui.conf, the managed profile on disk would
keep the stale serial.  The runtime config builder masks this cosmetically
(combined_config.enforce_device_serial rewrites the line at build time), but
the managed profile itself drifts out of sync with env.

These tests cover the re-sync helper and its integration into
_ensure_managed_profile.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from ui import favorites_runtime


_STALE_AIRBAND_CONF = """airband = true;

devices:
({
  type = "rtlsdr";
  index = 0;
  serial = "80000003";
  mode = "scan";
  gain = 32.800;

  channels:
  (
    {
      freqs = (118.6000);
      modulation = "am";
      bandwidth = 12000;
      squelch_threshold = -52;
      squelch_delay = 0.8;

      outputs:
      (
        {
          type = "icecast";
          server = "127.0.0.1";
          port = 8000;
          mountpoint = "scannerbox.mp3";
          username = "source";
          password = "062352";
          bitrate = 32;
        }
      );
    }
  );
});
"""


_GROUND_CONF = """airband = false;

devices:
({
  type = "rtlsdr";
  index = 1;
  serial = "11111111";
  mode = "scan";
  gain = 32.800;

  channels:
  (
    {
      freqs = (462.6500);
      modulation = "nfm";
      bandwidth = 12000;
      squelch_threshold = -70;
      squelch_delay = 0.8;
    }
  );
});
"""


_NO_SERIAL_CONF = """airband = true;

devices:
({
  type = "rtlsdr";
  index = 0;
  mode = "scan";
  gain = 32.800;
});
"""


def _write(path: str, contents: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(contents)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _serial_line(text: str) -> str:
    for line in text.splitlines():
        if "serial" in line and "=" in line:
            return line.strip()
    return ""


class EnforceManagedProfileSerialTests(unittest.TestCase):
    """Direct tests of the _enforce_managed_profile_serial helper."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "profile.conf")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_rewrites_stale_serial_for_airband(self):
        _write(self.path, _STALE_AIRBAND_CONF)
        changed = favorites_runtime._enforce_managed_profile_serial(
            self.path, "61108285"
        )
        self.assertTrue(changed)
        self.assertEqual('serial = "61108285";', _serial_line(_read(self.path)))

    def test_rewrites_stale_serial_for_ground(self):
        _write(self.path, _GROUND_CONF)
        changed = favorites_runtime._enforce_managed_profile_serial(
            self.path, "70613472"
        )
        self.assertTrue(changed)
        self.assertEqual('serial = "70613472";', _serial_line(_read(self.path)))

    def test_noop_when_serial_already_matches(self):
        _write(self.path, _STALE_AIRBAND_CONF)
        changed = favorites_runtime._enforce_managed_profile_serial(
            self.path, "80000003"
        )
        self.assertFalse(changed)
        self.assertEqual(_STALE_AIRBAND_CONF, _read(self.path))

    def test_noop_when_desired_is_empty(self):
        _write(self.path, _STALE_AIRBAND_CONF)
        changed = favorites_runtime._enforce_managed_profile_serial(self.path, "")
        self.assertFalse(changed)
        self.assertEqual(_STALE_AIRBAND_CONF, _read(self.path))

    def test_noop_when_desired_is_whitespace(self):
        _write(self.path, _STALE_AIRBAND_CONF)
        changed = favorites_runtime._enforce_managed_profile_serial(
            self.path, "   "
        )
        self.assertFalse(changed)
        self.assertEqual(_STALE_AIRBAND_CONF, _read(self.path))

    def test_noop_when_file_has_no_serial_line(self):
        # Helper does not seed a serial; that's the runtime builder's job.
        _write(self.path, _NO_SERIAL_CONF)
        changed = favorites_runtime._enforce_managed_profile_serial(
            self.path, "61108285"
        )
        self.assertFalse(changed)
        self.assertEqual(_NO_SERIAL_CONF, _read(self.path))

    def test_noop_when_file_missing(self):
        missing = os.path.join(self.tmpdir.name, "does-not-exist.conf")
        changed = favorites_runtime._enforce_managed_profile_serial(
            missing, "61108285"
        )
        self.assertFalse(changed)
        self.assertFalse(os.path.exists(missing))

    def test_idempotent(self):
        _write(self.path, _STALE_AIRBAND_CONF)
        first = favorites_runtime._enforce_managed_profile_serial(
            self.path, "61108285"
        )
        second = favorites_runtime._enforce_managed_profile_serial(
            self.path, "61108285"
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual('serial = "61108285";', _serial_line(_read(self.path)))

    def test_preserves_indentation(self):
        indented = "devices:\n({\n    serial = \"OLD\";\n});\n"
        _write(self.path, indented)
        favorites_runtime._enforce_managed_profile_serial(self.path, "NEW")
        self.assertIn('    serial = "NEW";', _read(self.path))


class EnsureManagedProfileSerialResyncTests(unittest.TestCase):
    """Verify _ensure_managed_profile re-syncs serial from env on every pass."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

        # Pre-seed the managed airband + ground profiles so _ensure_managed_profile
        # takes the "already seeded" path and only the re-sync logic runs.
        self.air_path = os.path.join(
            self.tmpdir.name, "rtl_airband_hp3_favorites_airband.conf"
        )
        self.ground_path = os.path.join(
            self.tmpdir.name, "rtl_airband_hp3_favorites_ground.conf"
        )
        _write(self.air_path, _STALE_AIRBAND_CONF)
        _write(self.ground_path, _GROUND_CONF)

        self._patches = [
            mock.patch.object(
                favorites_runtime,
                "_profile_path_for",
                side_effect=lambda pid: os.path.join(
                    self.tmpdir.name, f"rtl_airband_{pid}.conf"
                ),
            ),
            mock.patch.object(
                favorites_runtime, "safe_profile_path", side_effect=lambda p: p
            ),
            # Suppress side-effects we don't care about for serial-resync.
            mock.patch.object(favorites_runtime, "write_airband_flag"),
            mock.patch.object(favorites_runtime, "enforce_profile_index"),
            mock.patch.object(favorites_runtime, "upsert_analog_profile"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.tmpdir.cleanup()

    def _call(self, airband: bool):
        profiles: list = []
        return favorites_runtime._ensure_managed_profile(
            profiles,
            profile_id=(
                favorites_runtime._MANAGED_AIR_ID
                if airband
                else favorites_runtime._MANAGED_GROUND_ID
            ),
            label="test",
            airband=airband,
        )

    def test_airband_serial_resynced_from_env(self):
        with mock.patch.dict(os.environ, {"AIRBAND_RTL_SERIAL": "61108285"}, clear=False):
            _, changed = self._call(airband=True)
        self.assertTrue(changed)
        self.assertEqual('serial = "61108285";', _serial_line(_read(self.air_path)))

    def test_ground_serial_resynced_from_env(self):
        with mock.patch.dict(os.environ, {"GROUND_RTL_SERIAL": "70613472"}, clear=False):
            _, changed = self._call(airband=False)
        self.assertTrue(changed)
        self.assertEqual(
            'serial = "70613472";', _serial_line(_read(self.ground_path))
        )

    def test_unset_env_leaves_stale_serial_alone(self):
        env = dict(os.environ)
        env.pop("AIRBAND_RTL_SERIAL", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self._call(airband=True)
        self.assertEqual(
            'serial = "80000003";', _serial_line(_read(self.air_path))
        )

    def test_empty_env_leaves_stale_serial_alone(self):
        with mock.patch.dict(os.environ, {"AIRBAND_RTL_SERIAL": ""}, clear=False):
            self._call(airband=True)
        self.assertEqual(
            'serial = "80000003";', _serial_line(_read(self.air_path))
        )

    def test_idempotent_second_pass_no_change(self):
        with mock.patch.dict(os.environ, {"AIRBAND_RTL_SERIAL": "61108285"}, clear=False):
            _, first_changed = self._call(airband=True)
            _, second_changed = self._call(airband=True)
        self.assertTrue(first_changed)
        # Second pass: serial already matches env, so this helper contributes
        # no change.  (Other registry-level changes may still flip `changed`,
        # but the file contents stay stable.)
        self.assertEqual('serial = "61108285";', _serial_line(_read(self.air_path)))
        # File mtime/content stable on second pass.
        self.assertEqual(_read(self.air_path), _read(self.air_path))
        del second_changed  # registry bookkeeping may still flip this; not asserted

    def test_only_target_profile_touched(self):
        # Airband env set; ground env unset -> only airband profile changes.
        env = {k: v for k, v in os.environ.items() if k != "GROUND_RTL_SERIAL"}
        env["AIRBAND_RTL_SERIAL"] = "61108285"
        ground_before = _read(self.ground_path)
        with mock.patch.dict(os.environ, env, clear=True):
            self._call(airband=True)
        self.assertEqual('serial = "61108285";', _serial_line(_read(self.air_path)))
        self.assertEqual(ground_before, _read(self.ground_path))


if __name__ == "__main__":
    unittest.main()
