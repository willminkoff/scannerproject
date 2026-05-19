"""Tests for ui.profile_config.replace_freqs_labels — specifically the
non-ASCII label regression that broke disco's Listen flow.

Disco's _format_label() builds labels like "DISCO 164.2516 GOV_VHF_HIGH —
FM_NARROW" with an em-dash. Pre-fix, json.dumps(s) emitted "\\u2014" which
then tripped re.sub()'s replacement-string parser (Python rejects "\\u" as
a bad backreference escape) and raised, aborting the merge silently.

Fix: json.dumps(s, ensure_ascii=False) keeps the em-dash as literal UTF-8.
"""
from __future__ import annotations

import unittest

from ui.profile_config import replace_freqs_labels


_PROFILE_TEMPLATE = """\
devices:
(
  {
    type = "rtlsdr";
    serial = "fake0001";
    centerfreq = 162000000;
    correction = 0;
    gain = 30;
    channels:
    (
      {
        freqs = (
          162.5500,
          162.5750,
          164.2516
        );
        labels = (
          "WX",
          "Some Label",
          "Another"
        );
        modulation = "nfm";
        outputs:
        (
          {
            type = "icecast";
            mountpoint = "ANALOG.mp3";
          }
        );
      }
    );
  }
);
"""


class ReplaceFreqsLabelsAsciiTests(unittest.TestCase):
    def test_pure_ascii_labels_roundtrip(self):
        out = replace_freqs_labels(
            _PROFILE_TEMPLATE,
            [162.5500, 162.5750, 164.2516],
            ["WX-A", "WX-B", "WX-C"],
        )
        self.assertIn('"WX-A"', out)
        self.assertIn('"WX-B"', out)
        self.assertIn('"WX-C"', out)


class ReplaceFreqsLabelsUnicodeTests(unittest.TestCase):
    """Regression: non-ASCII chars in labels must not trip re.sub."""

    def test_em_dash_label_does_not_raise(self):
        """Pre-fix this raised 'bad escape \\u at position N'."""
        try:
            replace_freqs_labels(
                _PROFILE_TEMPLATE,
                [162.5500, 162.5750, 164.2516],
                [
                    "WX-A",
                    "WX-B",
                    "DISCO 164.2516 GOV_VHF_HIGH — FM_NARROW",
                ],
            )
        except Exception as e:
            self.fail(f"replace_freqs_labels raised {type(e).__name__}: {e}")

    def test_em_dash_label_appears_literally_in_output(self):
        out = replace_freqs_labels(
            _PROFILE_TEMPLATE,
            [162.5500, 162.5750, 164.2516],
            [
                "WX-A",
                "WX-B",
                "DISCO 164.2516 GOV_VHF_HIGH — FM_NARROW",
            ],
        )
        # Literal em-dash (U+2014) present in output.
        self.assertIn(
            "DISCO 164.2516 GOV_VHF_HIGH — FM_NARROW",
            out,
        )
        # And NOT the json-escape form — — that's the form that breaks
        # re.sub's replacement parser.
        self.assertNotIn("\\u2014", out)

    def test_assorted_non_ascii_chars_dont_raise(self):
        """Other non-ASCII characters that previously would have produced
        bad-escape \\u-style errors should now pass through cleanly."""
        problematic = [
            "WX-A",
            "WX-B",
            "Kéz — déjà vu",  # accents + em-dash
        ]
        try:
            out = replace_freqs_labels(
                _PROFILE_TEMPLATE,
                [162.5500, 162.5750, 164.2516],
                problematic,
            )
        except Exception as e:
            self.fail(f"replace_freqs_labels raised {type(e).__name__}: {e}")
        self.assertIn("Kéz — déjà vu", out)


if __name__ == "__main__":
    unittest.main()
