"""Tests for the per-profile teardown / startup contract reader.

Bug context (2026-05-25)
------------------------
Profiles add decoders but don't declare which decoders they displace.
Switching from the ``acars`` ground profile to ``hp3_favorites_airband``
should imply "stop acarsdec" — but nothing encoded that today.

ui/profile_metadata.py reads a declarative contract per profile from
``profiles/profile_metadata.json``.  This module exercises the reader
and the cache-invalidation behavior so the action path can rely on
fresh data without paying the parse cost on every hit.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from ui import profile_metadata


def _write_metadata(payload: dict) -> str:
    fh = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    )
    try:
        json.dump(payload, fh)
    finally:
        fh.close()
    return fh.name


class ProfileMetadataReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        profile_metadata.reset_cache_for_tests()
        self.addCleanup(profile_metadata.reset_cache_for_tests)

    def test_returns_empty_for_missing_file(self) -> None:
        # Point at a path that doesn't exist.
        result = profile_metadata.get_requires_stop("acars", path="/tmp/no-such-meta.json")
        self.assertEqual([], result)

    def test_returns_empty_for_unknown_profile(self) -> None:
        path = _write_metadata({
            "schema_version": 1,
            "profiles": {"acars": {"requires_stop": ["x.service"]}},
        })
        try:
            self.assertEqual([], profile_metadata.get_requires_stop("nope", path=path))
        finally:
            os.unlink(path)

    def test_get_requires_stop_returns_declared_list(self) -> None:
        path = _write_metadata({
            "schema_version": 1,
            "profiles": {
                "hp3_favorites_airband": {
                    "requires_stop": [
                        "acarsdec.service",
                        "dumpvdl2.service",
                        "radiosonde-auto-rx.service",
                    ],
                },
            },
        })
        try:
            units = profile_metadata.get_requires_stop("hp3_favorites_airband", path=path)
        finally:
            os.unlink(path)
        self.assertEqual(
            ["acarsdec.service", "dumpvdl2.service", "radiosonde-auto-rx.service"],
            units,
        )

    def test_get_starts_returns_declared_list(self) -> None:
        path = _write_metadata({
            "schema_version": 1,
            "profiles": {
                "acars": {"starts": ["acarsdec.service", "dumpvdl2.service"]},
            },
        })
        try:
            units = profile_metadata.get_starts("acars", path=path)
        finally:
            os.unlink(path)
        self.assertEqual(["acarsdec.service", "dumpvdl2.service"], units)

    def test_get_claims_serials_returns_declared_list(self) -> None:
        path = _write_metadata({
            "schema_version": 1,
            "profiles": {"acars": {"claims_serials": ["83241970"]}},
        })
        try:
            serials = profile_metadata.get_claims_serials("acars", path=path)
        finally:
            os.unlink(path)
        self.assertEqual(["83241970"], serials)

    def test_lists_dedupe_and_strip(self) -> None:
        path = _write_metadata({
            "schema_version": 1,
            "profiles": {
                "x": {
                    "requires_stop": [
                        "  acarsdec.service  ",
                        "acarsdec.service",
                        "",
                        "dumpvdl2.service",
                    ],
                },
            },
        })
        try:
            units = profile_metadata.get_requires_stop("x", path=path)
        finally:
            os.unlink(path)
        self.assertEqual(["acarsdec.service", "dumpvdl2.service"], units)

    def test_missing_keys_default_to_empty(self) -> None:
        path = _write_metadata({
            "schema_version": 1,
            "profiles": {"x": {}},
        })
        try:
            self.assertEqual([], profile_metadata.get_requires_stop("x", path=path))
            self.assertEqual([], profile_metadata.get_starts("x", path=path))
            self.assertEqual([], profile_metadata.get_claims_serials("x", path=path))
        finally:
            os.unlink(path)

    def test_malformed_json_returns_empty(self) -> None:
        fh = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        try:
            fh.write("{not valid json")
        finally:
            fh.close()
        try:
            self.assertEqual([], profile_metadata.get_requires_stop("acars", path=fh.name))
        finally:
            os.unlink(fh.name)

    def test_cache_reloads_on_mtime_change(self) -> None:
        path = _write_metadata({
            "schema_version": 1,
            "profiles": {"acars": {"requires_stop": ["v1.service"]}},
        })
        try:
            first = profile_metadata.get_requires_stop("acars", path=path)
            self.assertEqual(["v1.service"], first)

            # Bump the file content and the mtime explicitly so the
            # cache invalidation path fires (writing a fresh JSON is
            # fast enough that mtime resolution may collide).
            import time
            time.sleep(0.05)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({
                    "schema_version": 1,
                    "profiles": {"acars": {"requires_stop": ["v2.service"]}},
                }, fh)
            os.utime(path, None)  # touch
            new_mtime = os.path.getmtime(path) + 1
            os.utime(path, (new_mtime, new_mtime))

            second = profile_metadata.get_requires_stop("acars", path=path)
            self.assertEqual(["v2.service"], second)
        finally:
            os.unlink(path)


class BundledMetadataIntegrityTests(unittest.TestCase):
    """The metadata file shipped with the repo must parse and declare
    sane stop-lists for the managed favorites profiles, since
    action_set_profile relies on those during normal operation."""

    def test_bundled_metadata_parses(self) -> None:
        profile_metadata.reset_cache_for_tests()
        # Default path points at profiles/profile_metadata.json
        units = profile_metadata.get_requires_stop("hp3_favorites_airband")
        self.assertIn(
            "acarsdec.service",
            units,
            "hp3_favorites_airband must stop acarsdec by default (root-cause of 2026-05-25 incident)",
        )

    def test_bundled_acars_metadata_starts_decoders(self) -> None:
        profile_metadata.reset_cache_for_tests()
        units = profile_metadata.get_starts("acars")
        self.assertIn("acarsdec.service", units)
        self.assertIn("dumpvdl2.service", units)


if __name__ == "__main__":
    unittest.main()
