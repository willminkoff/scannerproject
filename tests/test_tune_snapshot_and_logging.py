"""A tune silently destroyed a 10-channel pool, and nothing recorded who did it.

Three times on 2026-11-08/09 airband's N4 pool was replaced by a single `vfo`
at 119.35 MHz. The daemon log shows the mechanism exactly -- three gain+squelch
applies with the value oscillating 39 -> 29 -> 39, then ten `channel_removed`
and one `channel_added {"ch": "vfo", "freq_mhz": 119.35}` -- but not the
caller. The frequency was 119.35 every time, including once when the channel
actually receiving was 124.75, so it is a fixed target rather than "tune to
what is active".

It was not the SB3 page: `/api/tune` with `target: activeTarget` is reachable
only from directTune() -> onTuneButtonClick -> the #btn-tune listener, and it
requires a human to type a frequency into promptTuneFreq(). Something else on
the network is posting to it, and `sb3/ui/server.py` had
`log_message: pass` -- no HTTP request was recorded at all, which blocked the
diagnosis three separate times.

Two fixes, tested here:
  * mutating POSTs log the client address, user-agent and form shape;
  * a tune snapshots the pool first, and /api/tune-restore puts it back --
    an endpoint sb3.html has always called and which always returned 501.
"""
from __future__ import annotations

import ast
import io
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ROUTES_SRC = io.open(_ROOT / "sb3" / "ui" / "routes.py", encoding="utf-8").read()
_SERVER_SRC = io.open(_ROOT / "sb3" / "ui" / "server.py", encoding="utf-8").read()


def _load(names, extra=None):
    tree = ast.parse(_ROUTES_SRC)
    want = set(names)
    body = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef,)) and n.name in want]
    missing = want - {n.name for n in body}
    if missing:
        raise AssertionError("routes.py missing %s" % sorted(missing))
    ns = dict(extra or {})
    exec(compile(ast.Module(body=body, type_ignores=[]), "<sub>", "exec"), ns)
    return ns


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ns = _load(
            ["_tune_snapshot_dir", "_tune_snapshot_path", "_save_tune_snapshot"],
            {"_TUNE_SNAPSHOT_DIR": self.dir},
        )

    def _save(self, band, channels):
        return self.ns["_save_tune_snapshot"](band, channels)

    def test_records_the_pool_before_it_is_destroyed(self):
        chans = [{"id": "n4-%d-x" % i, "freq_mhz": 118.0 + i, "mode": "am"}
                 for i in range(10)]
        n = self._save("airband", chans)
        self.assertEqual(n, 10)
        saved = json.loads((self.dir / "airband.json").read_text())
        self.assertEqual(len(saved["channels"]), 10)
        self.assertEqual(saved["band"], "airband")

    def test_excludes_the_vfo_and_keepalive(self):
        """Restoring a vfo would reinstate the thing being undone, and the
        keepalive is never removed by a tune in the first place."""
        chans = [{"id": "n4-0-tower", "freq_mhz": 118.6},
                 {"id": "vfo", "freq_mhz": 119.35},
                 {"id": "keepalive-air", "freq_mhz": 108.0}]
        self.assertEqual(self._save("airband", chans), 1)
        ids = [c["id"] for c in json.loads((self.dir / "airband.json").read_text())["channels"]]
        self.assertEqual(ids, ["n4-0-tower"])

    def test_an_already_tuned_band_does_not_clobber_the_good_snapshot(self):
        """Tuning twice must not overwrite the pre-first-tune pool with
        nothing -- that one is the only copy worth having."""
        self._save("airband", [{"id": "n4-0-tower", "freq_mhz": 118.6}])
        before = (self.dir / "airband.json").read_text()
        self.assertEqual(self._save("airband", [{"id": "vfo", "freq_mhz": 119.35}]), 0)
        self.assertEqual((self.dir / "airband.json").read_text(), before)

    def test_bad_rows_are_skipped_not_fatal(self):
        chans = [{"id": "ok", "freq_mhz": 118.6}, {"id": "bad", "freq_mhz": None},
                 "not-a-dict", {"no_id": 1}]
        self.assertEqual(self._save("airband", chans), 1)

    def test_never_raises(self):
        """A tune must not fail because the snapshot could not be written."""
        self.ns["_TUNE_SNAPSHOT_DIR"] = Path("/proc/nonexistent/nope")
        self.ns["_tune_snapshot_dir"] = lambda: Path("/proc/nonexistent/nope")
        try:
            self.assertEqual(self._save("airband", [{"id": "a", "freq_mhz": 1.0}]), 0)
        except Exception as exc:
            self.fail("snapshot raised: %r" % (exc,))

    def test_bands_are_kept_separate(self):
        self._save("airband", [{"id": "a", "freq_mhz": 118.6}])
        self._save("ground", [{"id": "g", "freq_mhz": 462.5}])
        self.assertTrue((self.dir / "airband.json").is_file())
        self.assertTrue((self.dir / "ground.json").is_file())


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sent = []

        def fake_send(port, cmd, args, *a, **k):
            self.sent.append((port, cmd, args))
            if cmd == "get_status":
                return {"status": "ok", "data": {"channels": [
                    {"id": "vfo"}, {"id": "keepalive-air"}]}}
            if cmd == "add_channel":
                return {"status": "ok",
                        "data": {"added": args.get("channels") or []}}
            return {"status": "ok"}

        self.ns = _load(
            ["_tune_snapshot_dir", "_tune_snapshot_path", "tune_restore"],
            {"_TUNE_SNAPSHOT_DIR": self.dir,
             "_CHIRP_PORT_BY_BAND": {"airband": 7400, "ground": 7401},
             "_chirp_send": fake_send},
        )

    def _write(self, band, channels):
        (self.dir / ("%s.json" % band)).write_text(
            json.dumps({"band": band, "saved_at": 1.0, "channels": channels}))

    def test_restores_the_snapshot(self):
        self._write("airband", [{"id": "n4-0-tower", "freq_mhz": 118.6, "mode": "am"}])
        res = self.ns["tune_restore"]({"target": "airband"}, None)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["restored"], 1)
        adds = [a for _, c, a in self.sent if c == "add_channel"]
        self.assertEqual(adds[0]["channels"][0]["id"], "n4-0-tower")

    def test_removes_the_vfo_but_keeps_the_keepalive(self):
        self._write("airband", [{"id": "n4-0-tower", "freq_mhz": 118.6}])
        self.ns["tune_restore"]({"target": "airband"}, None)
        removed = [a["id"] for _, c, a in self.sent if c == "remove_channel"]
        self.assertIn("vfo", removed)
        self.assertNotIn("keepalive-air", removed,
                         "the keepalive holds the icecast mount")

    def test_air_is_an_alias(self):
        self._write("airband", [{"id": "x", "freq_mhz": 118.6}])
        self.assertTrue(self.ns["tune_restore"]({"target": "air"}, None)["ok"])

    def test_missing_snapshot_is_a_clear_error_not_a_crash(self):
        res = self.ns["tune_restore"]({"target": "airband"}, None)
        self.assertFalse(res["ok"])
        self.assertIn("no snapshot", res["error"])

    def test_empty_snapshot_rejected(self):
        self._write("airband", [])
        res = self.ns["tune_restore"]({"target": "airband"}, None)
        self.assertFalse(res["ok"])

    def test_unknown_target_rejected(self):
        res = self.ns["tune_restore"]({"target": "banana"}, None)
        self.assertFalse(res["ok"])
        self.assertIn("unknown target", res["error"])


class TunePathTests(unittest.TestCase):
    def test_tune_snapshots_before_removing(self):
        src = _ROUTES_SRC.split("def _tune_chirp_band", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_save_tune_snapshot(band", src)
        self.assertLess(src.index("_save_tune_snapshot"), src.index("remove_channel"),
                        "the snapshot must be taken BEFORE the removal loop")

    def test_tune_reports_whether_it_is_undoable(self):
        src = _ROUTES_SRC.split("def _tune_chirp_band", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"restorable"', src)
        self.assertIn('"snapshot_channels"', src)


class MutationLoggingTests(unittest.TestCase):
    def test_log_message_still_suppresses_the_status_poll(self):
        """GETs are a ~1 Hz poll; logging them would bury the useful line."""
        self.assertRegex(_SERVER_SRC, r"def log_message\(self, fmt, \*args\):\s*\n\s*pass")

    def test_tune_is_logged(self):
        self.assertIn("/api/tune", _SERVER_SRC)
        m = re.search(r"MUTATING_PATHS = \((.*?)\)", _SERVER_SRC, re.S)
        self.assertIsNotNone(m, "MUTATING_PATHS missing")
        for path in ("/api/tune", "/api/profile", "/api/bandscan/apply",
                     "/api/apply"):
            with self.subTest(path=path):
                self.assertIn('"%s"' % path, m.group(1))

    def test_the_log_line_names_the_caller(self):
        m = re.search(r"def _log_mutation\(self, path, form\):(.*?)\n    def ",
                      _SERVER_SRC, re.S)
        self.assertIsNotNone(m, "_log_mutation missing")
        body = m.group(1)
        for want in ("client_address", "User-Agent", "Referer"):
            with self.subTest(field=want):
                self.assertIn(want, body)

    def test_credentials_are_redacted(self):
        m = re.search(r"_SECRET_KEYS = \((.*?)\)", _SERVER_SRC, re.S)
        self.assertIsNotNone(m)
        for t in ("pass", "secret", "token"):
            self.assertIn('"%s"' % t, m.group(1))
        self.assertIn("<redacted>", _SERVER_SRC)

    def test_logging_cannot_break_a_request(self):
        m = re.search(r"def _log_mutation\(self, path, form\):(.*?)\n    def ",
                      _SERVER_SRC, re.S)
        self.assertRegex(m.group(1), r"except Exception:\s*\n\s*pass")

    def test_it_is_called_before_dispatch(self):
        self.assertIn("self._log_mutation(p, form", _SERVER_SRC)
        self.assertLess(_SERVER_SRC.index("self._log_mutation(p, form"),
                        _SERVER_SRC.index('if p == "/api/dongles/power/schedule"'),
                        "log the call even if the handler then raises")


if __name__ == "__main__":
    unittest.main()


class ImportOrderTests(unittest.TestCase):
    """The snapshot dir must not be evaluated at import time.

    It was, as a module-level `_WxPath(...)` constant ~300 lines ABOVE
    `from pathlib import Path as _WxPath`. That is a NameError at module load,
    and it took sb3-ui down on startup with exit code 1 -- a dead UI shipped to
    fix a UI bug. Module-level work in a 4000-line file is ordering-sensitive
    in a way that no unit test of the function itself can catch.
    """

    def test_snapshot_dir_is_resolved_lazily(self):
        self.assertIn("def _tune_snapshot_dir():", _ROUTES_SRC)
        self.assertRegex(_ROUTES_SRC, r"_TUNE_SNAPSHOT_DIR = None")

    def test_no_module_level_path_call_before_its_import(self):
        decl = _ROUTES_SRC.index("_TUNE_SNAPSHOT_DIR = None")
        self.assertNotIn("_WxPath", _ROUTES_SRC[decl:decl + 200],
                         "resolve the path inside a function, not at import")

    def test_routes_compiles_standalone(self):
        """Cheap proxy for 'the module can be loaded at all'."""
        compile(_ROUTES_SRC, "routes.py", "exec")
