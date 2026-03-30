from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ui import digital


def _make_manager() -> digital.DigitalManager:
    mgr = digital.DigitalManager.__new__(digital.DigitalManager)
    mgr._scheduler_lock = threading.Lock()
    mgr._scheduler_pool_system_talkgroups = {}
    mgr._scheduler_active_system = ""
    return mgr


class DigitalActiveSystemFilterTests(unittest.TestCase):
    def test_multi_system_pool_allows_in_pool_events_from_non_active_systems(self):
        mgr = _make_manager()
        mgr._scheduler_active_system = "alpha"
        mgr._scheduler_pool_system_talkgroups = {
            "alpha": {"1001"},
            "bravo": {"2002"},
        }

        with (
            mock.patch.object(digital, "_DIGITAL_ENFORCE_ACTIVE_SYSTEM_EVENT_FILTER", True),
            mock.patch.object(digital, "get_current_scan_mode", return_value="expert"),
        ):
            self.assertTrue(mgr._event_allowed_for_active_system({"tgid": "2002"}))

    def test_single_system_pool_still_filters_out_of_pool_events(self):
        mgr = _make_manager()
        mgr._scheduler_active_system = "alpha"
        mgr._scheduler_pool_system_talkgroups = {
            "alpha": {"1001"},
        }

        with (
            mock.patch.object(digital, "_DIGITAL_ENFORCE_ACTIVE_SYSTEM_EVENT_FILTER", True),
            mock.patch.object(digital, "get_current_scan_mode", return_value="expert"),
        ):
            self.assertTrue(mgr._event_allowed_for_active_system({"tgid": "1001"}))
            self.assertFalse(mgr._event_allowed_for_active_system({"tgid": "2002"}))
