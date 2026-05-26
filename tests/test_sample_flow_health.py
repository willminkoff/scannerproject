"""Tests for the sample-flow liveness signals.

Bug context (2026-05-25 / 2026-05-26)
-------------------------------------
``/api/status`` lied about rtl-airband and icecast health multiple times
in a single 24 h window.  Each time the failure mode looked identical
from the API surface:

- ``rtl_active: true``, ``ground_active: true`` — both based on
  ``systemctl is-active``, which stayed true while rtl-airband was
  silently wedged in a SoapySDR ``readStream TIMEOUT`` retry loop.
  Stats file mtime was 16-30 minutes stale.

- ``icecast_active: true`` — systemd's SysV wrapper reported active
  even after the icecast2 daemon died.  Mount entries persisted in
  the icecast status JSON (with ``server_name`` and other metadata
  intact) because icecast holds them for fallback chaining, but no
  source was actually publishing.

The fix is two narrow first-class signals consumed by the API layer:

1. rtl_airband_sample_flow_state() reads the stats file mtime.  Fresh
   ⇒ samples flowing.  Stale ⇒ zombie state, regardless of systemd.

2. mount_publishing() consults icecast's ``stream_start`` field, which
   is reset to null/empty the moment the source disconnects.  Other
   metadata fields are NOT reliable for this.

This module pins those signals against the actual icecast JSON shapes
and rtl-airband stats file patterns we observed in production.
"""
from __future__ import annotations

import json
import os
import tempfile
import textwrap
import time
import unittest

from ui.sample_flow import (
    _find_source_for_mount,
    mount_publishing,
    rtl_airband_sample_flow_state,
)


# Sample icecast status JSON modeled on what /status-json.xsl actually
# returns from production.  Three sources cover the three states we
# care about: a healthy publishing mount, a zombie mount whose source
# died (no stream_start), and a keepalive fallback mount.
ICE_JSON_HEALTHY_ANALOG = json.dumps({
    "icestats": {
        "source": [
            {
                "listenurl": "http://localhost:8000/ANALOG.mp3",
                "mount": "/ANALOG.mp3",
                "stream_start": "Tue, 26 May 2026 07:42:06 -0500",
                "stream_start_iso8601": "2026-05-26T07:42:06-0500",
                "server_name": "SprontPi Radio",
                "listeners": 2,
                "audio_bitrate": 32000,
            },
            {
                "listenurl": "http://localhost:8000/DIGITAL.mp3",
                "mount": "/DIGITAL.mp3",
                "stream_start": "Tue, 26 May 2026 07:42:08 -0500",
                "stream_start_iso8601": "2026-05-26T07:42:08-0500",
                "server_name": "SprontPi Radio Digital",
                "listeners": 1,
                "audio_bitrate": 96000,
            },
        ],
    },
})


ICE_JSON_ZOMBIE_ANALOG = json.dumps({
    "icestats": {
        "source": [
            {
                "listenurl": "http://localhost:8000/ANALOG.mp3",
                "mount": "/ANALOG.mp3",
                # The smoking gun: server_name still set from before the
                # source disconnected, but stream_start is null because
                # icecast cleared it on disconnect.
                "stream_start": None,
                "stream_start_iso8601": None,
                "server_name": "SprontPi Radio",
                "listeners": 0,
                "audio_bitrate": None,
            },
            {
                "listenurl": "http://localhost:8000/keepalive-analog.mp3",
                "mount": "/keepalive-analog.mp3",
                "stream_start": "Mon, 25 May 2026 12:00:44 -0500",
                "stream_start_iso8601": "2026-05-25T12:00:44-0500",
                "listeners": 2,
            },
            {
                "listenurl": "http://localhost:8000/DIGITAL.mp3",
                "mount": "/DIGITAL.mp3",
                "stream_start": "Tue, 26 May 2026 07:42:08 -0500",
                "stream_start_iso8601": "2026-05-26T07:42:08-0500",
                "listeners": 1,
                "audio_bitrate": 96000,
            },
        ],
    },
})


ICE_JSON_EMPTY = json.dumps({"icestats": {}})


class RtlAirbandSampleFlowStateTests(unittest.TestCase):
    def _make_stats_file(self, mtime_offset_sec: float = 0.0) -> str:
        """Create a temp file whose mtime is now + offset (offset < 0 for stale)."""
        fh = tempfile.NamedTemporaryFile(
            mode="w", suffix="_rtl_airband_stats.txt", delete=False
        )
        fh.write("# placeholder rtl-airband stats\n")
        fh.close()
        target = time.time() + mtime_offset_sec
        os.utime(fh.name, (target, target))
        return fh.name

    def test_fresh_stats_file_reports_healthy(self) -> None:
        path = self._make_stats_file(mtime_offset_sec=-1.0)
        try:
            state = rtl_airband_sample_flow_state(path, stale_threshold_sec=15.0)
        finally:
            os.unlink(path)
        self.assertTrue(state["sample_flow_ok"])
        self.assertIsNotNone(state["stats_mtime"])
        self.assertIsNotNone(state["stats_age_sec"])
        self.assertLess(state["stats_age_sec"], 5.0)
        self.assertEqual(state["reason"], "")

    def test_stale_stats_file_reports_unhealthy_with_reason(self) -> None:
        # 60 seconds stale, threshold 15 — clearly zombie.
        path = self._make_stats_file(mtime_offset_sec=-60.0)
        try:
            state = rtl_airband_sample_flow_state(path, stale_threshold_sec=15.0)
        finally:
            os.unlink(path)
        self.assertFalse(state["sample_flow_ok"])
        self.assertGreater(state["stats_age_sec"], 50.0)
        self.assertIn("stale", state["reason"])

    def test_missing_stats_file_reports_unhealthy(self) -> None:
        state = rtl_airband_sample_flow_state(
            "/tmp/this_path_definitely_does_not_exist_xyz.txt",
            stale_threshold_sec=15.0,
        )
        self.assertFalse(state["sample_flow_ok"])
        self.assertIsNone(state["stats_mtime"])
        self.assertIsNone(state["stats_age_sec"])
        self.assertIn("missing", state["reason"])

    def test_future_mtime_clamped_to_fresh(self) -> None:
        # Clock skew could make mtime > now; don't false-alarm on that.
        path = self._make_stats_file(mtime_offset_sec=+30.0)
        try:
            state = rtl_airband_sample_flow_state(path, stale_threshold_sec=15.0)
        finally:
            os.unlink(path)
        self.assertTrue(state["sample_flow_ok"])
        self.assertEqual(state["stats_age_sec"], 0.0)

    def test_threshold_boundary(self) -> None:
        # Exactly at threshold — sample_flow_ok requires STRICTLY less than.
        path = self._make_stats_file(mtime_offset_sec=-20.0)
        try:
            state = rtl_airband_sample_flow_state(path, stale_threshold_sec=5.0)
        finally:
            os.unlink(path)
        self.assertFalse(state["sample_flow_ok"])

    def test_now_override_for_determinism(self) -> None:
        # Pass a frozen ``now`` so tests don't race against wall-clock drift.
        path = self._make_stats_file(mtime_offset_sec=0.0)
        mtime = os.path.getmtime(path)
        try:
            state = rtl_airband_sample_flow_state(
                path, stale_threshold_sec=10.0, now=mtime + 3.0
            )
        finally:
            os.unlink(path)
        self.assertTrue(state["sample_flow_ok"])
        self.assertAlmostEqual(state["stats_age_sec"], 3.0, places=2)


class FindSourceForMountTests(unittest.TestCase):
    def test_matches_by_bare_mount_name(self) -> None:
        source = _find_source_for_mount(ICE_JSON_HEALTHY_ANALOG, "ANALOG.mp3")
        self.assertIsNotNone(source)
        self.assertEqual(source["mount"], "/ANALOG.mp3")

    def test_matches_when_caller_includes_leading_slash(self) -> None:
        source = _find_source_for_mount(ICE_JSON_HEALTHY_ANALOG, "/ANALOG.mp3")
        self.assertIsNotNone(source)
        self.assertEqual(source["mount"], "/ANALOG.mp3")

    def test_returns_none_for_unknown_mount(self) -> None:
        source = _find_source_for_mount(ICE_JSON_HEALTHY_ANALOG, "NOPE.mp3")
        self.assertIsNone(source)

    def test_returns_none_for_malformed_status_text(self) -> None:
        self.assertIsNone(_find_source_for_mount("not json at all", "ANALOG.mp3"))
        self.assertIsNone(_find_source_for_mount("", "ANALOG.mp3"))
        self.assertIsNone(_find_source_for_mount(ICE_JSON_EMPTY, "ANALOG.mp3"))


class MountPublishingTests(unittest.TestCase):
    def test_healthy_mount_with_stream_start_is_publishing(self) -> None:
        self.assertTrue(mount_publishing(ICE_JSON_HEALTHY_ANALOG, "ANALOG.mp3"))
        self.assertTrue(mount_publishing(ICE_JSON_HEALTHY_ANALOG, "DIGITAL.mp3"))

    def test_zombie_mount_with_null_stream_start_is_not_publishing(self) -> None:
        # This is the exact shape we saw in the field: server_name
        # still set, audio_bitrate=None, stream_start=None.  Without
        # checking stream_start specifically, we'd falsely claim it's
        # alive.
        self.assertFalse(mount_publishing(ICE_JSON_ZOMBIE_ANALOG, "ANALOG.mp3"))
        # Digital on the same icecast instance is still healthy — make
        # sure the zombie ANALOG doesn't drag DIGITAL down.
        self.assertTrue(mount_publishing(ICE_JSON_ZOMBIE_ANALOG, "DIGITAL.mp3"))

    def test_absent_mount_is_not_publishing(self) -> None:
        self.assertFalse(mount_publishing(ICE_JSON_HEALTHY_ANALOG, "NOPE.mp3"))

    def test_empty_or_garbage_status_is_not_publishing(self) -> None:
        self.assertFalse(mount_publishing("", "ANALOG.mp3"))
        self.assertFalse(mount_publishing("nope", "ANALOG.mp3"))
        self.assertFalse(mount_publishing(ICE_JSON_EMPTY, "ANALOG.mp3"))

    def test_iso8601_alone_also_counts_as_publishing(self) -> None:
        # Some icecast versions populate only stream_start_iso8601 (no
        # plain stream_start).  Verify we accept either field.
        payload = json.dumps({
            "icestats": {
                "source": {
                    "listenurl": "http://localhost:8000/X.mp3",
                    "mount": "/X.mp3",
                    "stream_start": None,
                    "stream_start_iso8601": "2026-05-26T08:00:00-0500",
                },
            },
        })
        self.assertTrue(mount_publishing(payload, "X.mp3"))

    def test_empty_string_stream_start_is_not_publishing(self) -> None:
        # icecast sometimes returns "" instead of null after a source
        # disconnect — treat either as "not publishing".
        payload = json.dumps({
            "icestats": {
                "source": {
                    "listenurl": "http://localhost:8000/X.mp3",
                    "mount": "/X.mp3",
                    "stream_start": "",
                    "stream_start_iso8601": "",
                },
            },
        })
        self.assertFalse(mount_publishing(payload, "X.mp3"))


if __name__ == "__main__":
    unittest.main()
