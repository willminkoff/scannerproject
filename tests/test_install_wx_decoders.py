"""Regression checks for scripts/install-wx-decoders.sh."""

from __future__ import annotations

import os
import unittest


class InstallWxDecodersScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "install-wx-decoders.sh"
        )
        with open(script_path, "r", encoding="utf-8") as f:
            cls.script = f.read()

    def test_libacars_step_precedes_acarsdec_step(self):
        self.assertLess(
            self.script.index('echo "2/9: Building libacars..."'),
            self.script.index('echo "3/9: Building acarsdec..."'),
        )

    def test_libacars_clone_precedes_acarsdec_clone(self):
        self.assertLess(
            self.script.index('git clone --depth 1 "$LIBACARS_REPO" "$BUILD_DIR/libacars"'),
            self.script.index('git clone --depth 1 "$ACARSDEC_REPO" "$BUILD_DIR/acarsdec"'),
        )


if __name__ == "__main__":
    unittest.main()
