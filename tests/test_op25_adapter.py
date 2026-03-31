import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui.op25_adapter import Op25Adapter


class Op25AdapterEventExtractionTests(unittest.TestCase):
    def test_events_from_status_merges_call_log_and_channel_update(self):
        adapter = Op25Adapter()
        now_sec = time.time()
        status = {
            "call_log": [
                {
                    "tgid": "47008",
                    "tgtag": "District 3: Dispatch 1",
                    "freq": 769831250,
                    "time": now_sec,
                    "system": "6355:1",
                },
            ],
            "channel_update": {
                "0": {
                    "tgid": "47008",
                    "tag": "District 3: Dispatch 1",
                    "freq": 769831250,
                    "system": "6355:1",
                    "mode": "P25",
                },
                "1": {
                    "tgid": "3207",
                    "tag": "",
                    "freq": 855912500,
                    "system": "7078:2",
                    "mode": "P25",
                },
                "channels": ["0", "1"],
            },
        }

        with mock.patch.object(adapter, "_resolve_tg_label", side_effect=lambda tgid: {
            "3207": "Police Dispatch",
            "47008": "District 3: Dispatch 1",
        }.get(str(tgid), "")):
            events = adapter._events_from_status(status)

        self.assertEqual(2, len(events))
        by_tgid = {
            str(event.get("tgid")): event
            for event in events
        }
        self.assertIn("47008", by_tgid)
        self.assertIn("3207", by_tgid)
        self.assertEqual("6355:1", by_tgid["47008"]["system"])
        self.assertEqual("7078:2", by_tgid["3207"]["system"])
        self.assertEqual("Police Dispatch", by_tgid["3207"]["label"])
        self.assertEqual(855912500, by_tgid["3207"]["frequency_hz"])

    def test_events_from_status_include_verbose_grouped_metadata(self):
        adapter = Op25Adapter()
        now_sec = time.time()
        status = {
            "call_log": [
                {
                    "tgid": "47008",
                    "tgtag": "District 3: Dispatch 1",
                    "freq": 769831250,
                    "time": now_sec,
                    "system": "6355:1",
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)
            (profile_dir / "talkgroups_with_group.csv").write_text(
                "Group,DEC,HEX,Mode,Alpha Tag,Description,Tag\n"
                "\"TN - State Highway Patrol, District 3: Nashville\",47008,b7a0,D,District 3: Dispatch 1,District 3: Dispatch 1,\n",
                encoding="utf-8",
            )
            with mock.patch.object(adapter, "_read_active_profile_dir", return_value=str(profile_dir)):
                events = adapter._events_from_status(status)

        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("District 3: Dispatch 1", event["label"])
        self.assertEqual("TN - State Highway Patrol, District 3: Nashville", event["agency"])
        self.assertEqual("District 3: Dispatch 1", event["department"])
        self.assertEqual(
            "TN - State Highway Patrol, District 3: Nashville - District 3: Dispatch 1",
            event["label_full"],
        )


if __name__ == "__main__":
    unittest.main()
