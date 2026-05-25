"""Regression tests for the combined rtl-airband config parser.

Bug context (2026-05-25)
------------------------
A profile switch left the operator's SB3 with the airband RTL dongle
silently stuck.  ``/api/status`` reported ``combined_devices: 0`` and
``serial_mismatch: true`` for both airband and ground, even though
``runtime/rtl_airband_combined.conf`` had two perfectly well-formed
device blocks (lines ~30 and ~890, serials ``83241970`` + ``70613472``).

Root cause: ``_extract_devices_section`` and ``_split_device_blocks``
walked the config character-by-character counting ``(``/``)`` and
``{``/``}`` for nesting depth — but did not skip over ``"..."`` string
literals.  Channel labels added during NJ travel (``"ZNY Sector 58
Coyle (Ship Bottom RCAG)"``, ``"PHL Approach North High (Philadelphia
TRACON)"``, etc.) legitimately contain parens and bracket characters
inside the quoted text.  The parser counted those as structural
brackets, the depth counter never balanced, and the function returned
``""`` — which the rest of the pipeline treated as "no devices found,
serial mismatch on both".

This test exercises the exact pattern that broke production.
"""
from __future__ import annotations

import textwrap
import unittest

from ui.combined_status import (
    _extract_devices_section,
    _split_device_blocks,
    _iter_struct_chars,
    read_combined_devices,
)


# Real fragment from the production combined.conf the bug was first
# observed against, trimmed to the minimum that still trips the parser.
# Labels deliberately contain ``(`` and ``)`` inside quoted strings.
_TWO_DEVICE_FRAGMENT = textwrap.dedent('''\
    log_scan_activity = true;

    mixers: {
      combined: {
        outputs:
        (
          {
            type = "icecast";
            mountpoint = "ANALOG.mp3";
          }
        );
      };
    };

    devices:
    (
      {
        type = "rtlsdr";
        serial = "83241970";
        index = 0;
        gain = 32.800;
        channels:
        (
          {
            freqs = (121.5000, 124.6000, 127.7000);
            modulation = "am";
            squelch_threshold = -27;
            labels = (
              "Guard / 121.5 International Aircraft Emergency",
              "Approach/Departure",
              "ZNY Sector 51 CASINO Low (Sea Isle RCAG)"
            );
          }
        );
      },
      {
        type = "rtlsdr";
        serial = "70613472";
        index = 1;
        gain = 29.700;
        channels:
        (
          {
            freqs = (151.0775, 154.1300);
            modulation = "nfm";
            squelch_threshold = -27;
            labels = (
              "Police Dispatch",
              "Fire Dispatch/Operations (CMC Ch 1)"
            );
          }
        );
      }
    )
''')


class CombinedConfigParserStringAwarenessTests(unittest.TestCase):
    def test_iter_struct_chars_skips_paren_inside_string(self) -> None:
        text = 'a ( b "c (d) e" f )'
        # Only the outer ``(`` and ``)`` should appear; the ones inside
        # the quoted "c (d) e" must be skipped.
        emitted = [ch for _, ch in _iter_struct_chars(text) if ch in "()"]
        self.assertEqual(["(", ")"], emitted)

    def test_iter_struct_chars_honors_escape(self) -> None:
        # ``\"`` inside a string does not end the string; subsequent ``(``
        # remains inside the string and must be skipped.
        text = 'before ( "esc \\" still in string (skip)" outside )'
        emitted = [ch for _, ch in _iter_struct_chars(text) if ch in "()"]
        self.assertEqual(["(", ")"], emitted)

    def test_extract_devices_section_with_parens_in_string_labels(self) -> None:
        section = _extract_devices_section(_TWO_DEVICE_FRAGMENT)
        self.assertNotEqual("", section, "string-literal parens must not abort the depth counter")
        # The extracted section should contain both device blocks.
        self.assertIn('serial = "83241970"', section)
        self.assertIn('serial = "70613472"', section)

    def test_split_device_blocks_finds_both_top_level_blocks(self) -> None:
        section = _extract_devices_section(_TWO_DEVICE_FRAGMENT)
        blocks = _split_device_blocks(section)
        self.assertEqual(2, len(blocks), f"expected 2 device blocks, got {len(blocks)}")
        self.assertIn('serial = "83241970"', blocks[0])
        self.assertIn('serial = "70613472"', blocks[1])

    def test_read_combined_devices_returns_two_devices(self) -> None:
        # End-to-end via a temp file.
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".conf", delete=False
        ) as fh:
            fh.write(_TWO_DEVICE_FRAGMENT)
            path = fh.name
        try:
            devices = read_combined_devices(path)
        finally:
            os.unlink(path)

        self.assertEqual(2, len(devices))
        self.assertEqual("83241970", devices[0]["serial"])
        self.assertEqual(0, devices[0]["index"])
        self.assertTrue(devices[0]["is_airband"], "first device should classify as airband")
        self.assertEqual("70613472", devices[1]["serial"])
        self.assertEqual(1, devices[1]["index"])
        self.assertFalse(devices[1]["is_airband"], "second device should classify as ground")

    def test_split_device_blocks_with_brace_inside_string(self) -> None:
        # Defense-in-depth: if a label ever contains ``{`` / ``}``,
        # _split_device_blocks must not treat them as structural braces.
        section = textwrap.dedent('''\
              {
                serial = "AAA";
                channels: (
                  {
                    labels = (
                      "Has a { brace in label",
                      "And a } close"
                    );
                  }
                );
              },
              {
                serial = "BBB";
              }
        ''')
        blocks = _split_device_blocks(section)
        self.assertEqual(2, len(blocks))
        self.assertIn('"AAA"', blocks[0])
        self.assertIn('"BBB"', blocks[1])


if __name__ == "__main__":
    unittest.main()
