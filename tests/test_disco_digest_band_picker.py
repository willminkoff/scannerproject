"""The digest band picker must agree with DIGEST_DEFAULT_BAND.

2026-10-25: dropping the milair filter meant setting DIGEST_DEFAULT_BAND to
"all", and /api/digest did start defaulting to all bands -- but the page kept
asking for 225-400 anyway. The picker markup was a static two-option list with
``selected`` hardcoded on milair, so the browser overrode the API default on
every load and there was no way to make it stick.

The options are now rendered from DIGEST_BAND_SCOPES, which also makes true
the long-standing claim that adding a scope "shows up in the digest page's
band picker" -- it did not, while the list was hardcoded.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# dashboard pulls in fastapi + yaml at module scope. Neither is present on
# every dev box (that is the standing cause of this repo's local collection
# errors), so skip rather than stub a web framework to test string rendering.
# scannerbox-1 has both and runs this for real.
import pytest  # noqa: E402

pytest.importorskip("fastapi", reason="dashboard needs fastapi")
pytest.importorskip("yaml", reason="dashboard needs PyYAML")

from disco.src import dashboard  # noqa: E402


class BandPickerTests(unittest.TestCase):
    def test_default_band_is_the_selected_option(self):
        html = dashboard._digest_band_options_html()
        m = re.search(r'<option value="([^"]+)" selected>', html)
        self.assertIsNotNone(m, "no option carries selected")
        self.assertEqual(m.group(1), dashboard.DIGEST_DEFAULT_BAND)

    def test_exactly_one_option_is_selected(self):
        self.assertEqual(dashboard._digest_band_options_html().count(" selected>"), 1)

    def test_every_scope_appears(self):
        html = dashboard._digest_band_options_html()
        for key in dashboard.DIGEST_BAND_SCOPES:
            self.assertIn('value="%s"' % key, html)

    def test_a_new_scope_shows_up_in_the_picker(self):
        """The behaviour the hardcoded list silently failed to provide."""
        dashboard.DIGEST_BAND_SCOPES["vhf_test"] = (30e6, 300e6)
        dashboard.DIGEST_BAND_LABELS["vhf_test"] = "VHF test"
        try:
            html = dashboard._digest_band_options_html()
            self.assertIn('<option value="vhf_test">VHF test</option>', html)
        finally:
            dashboard.DIGEST_BAND_SCOPES.pop("vhf_test", None)
            dashboard.DIGEST_BAND_LABELS.pop("vhf_test", None)

    def test_label_falls_back_to_the_key(self):
        dashboard.DIGEST_BAND_SCOPES["unlabelled"] = (0.0, 1.0)
        try:
            self.assertIn('>unlabelled</option>', dashboard._digest_band_options_html())
        finally:
            dashboard.DIGEST_BAND_SCOPES.pop("unlabelled", None)


class DigestRouteTests(unittest.TestCase):
    """Check the routing table, not digest_page().

    The 2026-10-25 breakage lived entirely in routing: a helper got inserted
    between @app.get("/digest") and digest_page(), so the decorator bound the
    helper and /digest returned 93 bytes of <option> tags. Every
    function-level test still passed, because digest_page() was never wrong.

    Introspecting app.routes and calling whatever the decorator actually bound
    catches that, and needs no HTTP client (the box has no httpx).
    """

    def _digest_route(self):
        for r in dashboard.app.routes:
            if getattr(r, "path", None) == "/digest":
                return r
        self.fail("no /digest route registered")

    def test_route_is_bound_to_digest_page(self):
        self.assertEqual(self._digest_route().endpoint.__name__, "digest_page")

    def test_bound_endpoint_returns_the_whole_page(self):
        body = self._digest_route().endpoint()
        self.assertGreater(
            len(body), 5000,
            "route served %d bytes -- decorator bound to the wrong function?" % len(body))
        self.assertIn("<!doctype html>", body.lower())
        self.assertIn("Disco Digest", body)

    def test_bound_endpoint_selects_the_default_band(self):
        body = self._digest_route().endpoint()
        self.assertIn('<option value="%s" selected>' % dashboard.DIGEST_DEFAULT_BAND, body)
        self.assertNotIn("__BAND_OPTIONS__", body)


class DigestPageTests(unittest.TestCase):
    def test_placeholder_is_substituted(self):
        page = dashboard.digest_page()
        self.assertNotIn("__BAND_OPTIONS__", page)

    def test_page_selects_the_default_band(self):
        page = dashboard.digest_page()
        self.assertIn('<option value="%s" selected>' % dashboard.DIGEST_DEFAULT_BAND, page)

    def test_page_does_not_pin_milair(self):
        """The exact regression: markup forcing 225-400 regardless of default."""
        page = dashboard.digest_page()
        if dashboard.DIGEST_DEFAULT_BAND != "milair":
            self.assertNotIn('<option value="milair" selected>', page)


if __name__ == "__main__":
    unittest.main()
