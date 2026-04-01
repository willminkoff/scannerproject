import sqlite3
import tempfile
import unittest
from pathlib import Path

from ui import service_types


class ServiceTypesCatalogTests(unittest.TestCase):
    def _make_temp_hpdb(self) -> Path:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = Path(tmpdir.name) / "hpdb.sqlite"
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE talkgroups (service_tag INTEGER)")
            conn.execute("CREATE TABLE conventional_freqs (service_tag INTEGER)")
            conn.commit()
        finally:
            conn.close()
        return path

    def test_official_placeholder_relabels_preserve_flags_and_order(self):
        db_path = self._make_temp_hpdb()
        rows = service_types.get_all_service_types(db_path=str(db_path))
        by_tag = {int(row["service_tag"]): row for row in rows}

        self.assertEqual("Multi-Tac", by_tag[6]["name"])
        self.assertEqual("Federal", by_tag[16]["name"])
        self.assertEqual("Other", by_tag[21]["name"])
        self.assertEqual("Multi-Talk", by_tag[22]["name"])

        for tag in (6, 16, 21, 22):
            self.assertFalse(by_tag[tag]["enabled_by_default"])
            self.assertFalse(by_tag[tag]["is_custom"])

        ordered_tags = [int(row["service_tag"]) for row in rows]
        self.assertLess(ordered_tags.index(6), ordered_tags.index(7))
        self.assertLess(ordered_tags.index(16), ordered_tags.index(17))
        self.assertLess(ordered_tags.index(21), ordered_tags.index(22))
        self.assertEqual([2, 3, 4], service_types.get_default_enabled_service_types(db_path=str(db_path)))


if __name__ == "__main__":
    unittest.main()
