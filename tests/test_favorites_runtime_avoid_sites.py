"""Tests for the hard avoid_site_ids filter in ui/favorites_runtime.

Covers the 2026-06-12 review finding P1-10: the hard filter must accept the
same sidecar shapes as the soft-penalty path in ui/op25_adapter — both the
JSON-list form (["758"]) and the comma/space-separated string form
("758, 759"). The string form used to be silently ignored, so the operator
believed a site was excluded while it still reached systems.json.
"""

import unittest

from ui import favorites_runtime as fr
from ui.op25_adapter import coerce_site_id_list


def _system(name, site_ids):
    return {
        "name": name,
        "sites": [{"site_id": sid, "site_name": f"site-{sid}"} for sid in site_ids],
    }


class CoerceSiteIdListTests(unittest.TestCase):
    def test_list_form(self):
        self.assertEqual(coerce_site_id_list(["758", " 759 ", 760]), ["758", "759", "760"])

    def test_string_form_commas_and_spaces(self):
        self.assertEqual(coerce_site_id_list("758, 759 760"), ["758", "759", "760"])

    def test_dedup_preserves_order(self):
        self.assertEqual(coerce_site_id_list(["a", "b", "a"]), ["a", "b"])

    def test_garbage_shapes_empty(self):
        self.assertEqual(coerce_site_id_list(None), [])
        self.assertEqual(coerce_site_id_list({"not": "a list"}), [])
        self.assertEqual(coerce_site_id_list(42), [])


class ApplyAvoidSiteIdsTests(unittest.TestCase):
    def test_list_form_drops_site(self):
        systems = [_system("VA STARS", ["100", "200"])]
        overrides = {"VA STARS": {"site_policy": {"avoid_site_ids": ["200"]}}}
        out = fr._apply_avoid_site_ids(systems, overrides)
        self.assertEqual([s["site_id"] for s in out[0]["sites"]], ["100"])

    def test_string_form_drops_site(self):
        # Regression: the string form was silently ignored pre-2026-06-12.
        systems = [_system("VA STARS", ["100", "200", "300"])]
        overrides = {"VA STARS": {"site_policy": {"avoid_site_ids": "200, 300"}}}
        out = fr._apply_avoid_site_ids(systems, overrides)
        self.assertEqual([s["site_id"] for s in out[0]["sites"]], ["100"])

    def test_system_dropped_when_all_sites_avoided(self):
        systems = [_system("VA STARS", ["100"]), _system("KEEP", ["900"])]
        overrides = {"VA STARS": {"site_policy": {"avoid_site_ids": ["100"]}}}
        out = fr._apply_avoid_site_ids(systems, overrides)
        self.assertEqual([s["name"] for s in out], ["KEEP"])

    def test_unrelated_system_untouched(self):
        systems = [_system("OTHER", ["100"])]
        overrides = {"VA STARS": {"site_policy": {"avoid_site_ids": ["100"]}}}
        out = fr._apply_avoid_site_ids(systems, overrides)
        self.assertEqual([s["site_id"] for s in out[0]["sites"]], ["100"])

    def test_no_overrides_is_identity(self):
        systems = [_system("VA STARS", ["100"])]
        self.assertIs(fr._apply_avoid_site_ids(systems, None), systems)
        self.assertIs(fr._apply_avoid_site_ids(systems, {}), systems)


if __name__ == "__main__":
    unittest.main()
