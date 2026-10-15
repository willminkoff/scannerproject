"""Guards on the two code paths that write channels to a chirp daemon.

Both _hp_push_chirp and bandscan_apply remove the band's existing channels
before adding new ones, and chirp rejects an add_channel batch ALL-OR-NOTHING
(pool exhausted, cluster budget blown, or a mode the daemon can't demodulate).
Every combination of those means the band ends up with nothing -- which on
2026-10-14/15 took ground down and left chirp-mount-watchdog kickstarting a
daemon every 2 minutes against a mount it could never satisfy.

These tests drive both paths against a fake chirp on a real UDP socket and
assert the thing that actually matters: when a push cannot succeed, the
existing channels are still there.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sb3.ui import routes  # noqa: E402


class FakeChirp:
    """Minimal chirp cmd-server: records every command it is sent."""

    def __init__(self, pool_mode="nfm", max_channels=16, max_clusters=6,
                 iq_bw_hz=2.0e6, channels=("existing-1", "existing-2")):
        self.pool_mode = pool_mode
        self.max_channels = max_channels
        self.max_clusters = max_clusters
        self.iq_bw_hz = iq_bw_hz
        self.channels = list(channels)
        self.commands = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        self.sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                env = json.loads(data.decode("utf-8", "ignore"))
            except Exception:
                continue
            cmd = env.get("cmd")
            self.commands.append((cmd, env.get("args")))
            if cmd == "get_status":
                body = {
                    "pool_mode": self.pool_mode,
                    "max_channels": self.max_channels,
                    "pool_free": max(0, self.max_channels - len(self.channels)),
                    "channels": [{"id": c, "freq_mhz": 100.0} for c in self.channels],
                    "lo_scheduler": {"max_clusters": self.max_clusters,
                                     "iq_bw_hz": self.iq_bw_hz},
                }
                resp = {"v": 1, "id": env.get("id"), "status": "ok", "data": body}
            elif cmd == "remove_channel":
                cid = (env.get("args") or {}).get("id")
                if cid in self.channels:
                    self.channels.remove(cid)
                resp = {"v": 1, "id": env.get("id"), "status": "ok", "data": {}}
            elif cmd == "add_channel":
                chans = (env.get("args") or {}).get("channels") or []
                self.channels.extend(c["id"] for c in chans)
                resp = {"v": 1, "id": env.get("id"), "status": "ok",
                        "data": {"count": len(chans)}}
            else:
                resp = {"v": 1, "id": env.get("id"), "status": "ok", "data": {}}
            try:
                self.sock.sendto(json.dumps(resp).encode(), addr)
            except OSError:
                return

    @property
    def removes(self):
        return [a for c, a in self.commands if c == "remove_channel"]

    @property
    def adds(self):
        return [a for c, a in self.commands if c == "add_channel"]

    def close(self):
        self._stop.set()
        self._t.join(timeout=2)
        self.sock.close()


class _ChirpTestCase(unittest.TestCase):
    """Point the module's chirp host/port maps at a fake daemon."""

    def _fake(self, band="ground", **kw):
        fake = FakeChirp(**kw)
        self.addCleanup(fake.close)
        orig_hosts = dict(routes._CHIRP_HOSTS_BY_PORT)
        orig_ports = dict(routes._CHIRP_PORT_BY_BAND)
        routes._CHIRP_HOSTS_BY_PORT[fake.port] = "127.0.0.1"
        routes._CHIRP_PORT_BY_BAND[band] = fake.port

        def restore():
            routes._CHIRP_HOSTS_BY_PORT.clear()
            routes._CHIRP_HOSTS_BY_PORT.update(orig_hosts)
            routes._CHIRP_PORT_BY_BAND.clear()
            routes._CHIRP_PORT_BY_BAND.update(orig_ports)
        self.addCleanup(restore)
        return fake


class EmptyPushTests(_ChirpTestCase):
    """A profile with no channels for a band must leave that band alone."""

    def test_empty_freqs_removes_nothing_and_never_opens_a_socket(self):
        fake = self._fake()
        out = routes._hp_push_chirp(fake.port, [], "nfm")
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], 0)
        self.assertEqual(out["added"], 0)
        self.assertIn("skipped", out)
        self.assertEqual(fake.commands, [], "must not contact chirp at all")
        self.assertEqual(fake.channels, ["existing-1", "existing-2"])

    def test_non_empty_push_still_replaces(self):
        fake = self._fake()
        out = routes._hp_push_chirp(
            fake.port, [{"freq_mhz": 162.4, "label": "wx"}], "nfm")
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], 2)
        self.assertEqual(out["added"], 1)


class BandscanModeTests(_ChirpTestCase):
    """CB posts am at a ground daemon that only demodulates nfm."""

    def _apply(self, fake, mode, freqs="27.005,27.015"):
        return routes.bandscan_apply(
            {"target": "ground", "modulation": mode, "preset_id": "cb",
             "freqs_text": freqs}, None)

    def test_mode_mismatch_refuses_without_removing(self):
        fake = self._fake(pool_mode="nfm")
        out = self._apply(fake, "am")
        self.assertFalse(out["ok"])
        self.assertEqual(out["pool_mode"], "nfm")
        self.assertEqual(out["requested_mode"], "am")
        self.assertEqual(out["removed"], 0)
        self.assertEqual(fake.removes, [], "band must be left intact")
        self.assertEqual(fake.adds, [])
        self.assertEqual(fake.channels, ["existing-1", "existing-2"])

    def test_matching_mode_proceeds(self):
        fake = self._fake(pool_mode="nfm")
        out = self._apply(fake, "nfm", freqs="162.400,162.425")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["added"], 2)


class BandscanClusterTests(_ChirpTestCase):
    """A preset spanning more than the LO budget is trimmed, not rejected."""

    def test_wide_preset_is_trimmed_to_the_cluster_budget(self):
        fake = self._fake(pool_mode="nfm", max_clusters=2, max_channels=16)
        out = routes.bandscan_apply(
            {"target": "ground", "modulation": "nfm", "preset_id": "wide",
             "freqs_text": "150.000,153.000,156.000,159.000,162.000"}, None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["added"], 2)
        self.assertEqual(out["dropped_over_clusters"], 3)
        self.assertEqual(out["max_clusters"], 2)
        self.assertEqual(len(fake.adds), 1, "exactly one add_channel batch")

    def test_narrow_preset_untouched(self):
        fake = self._fake(pool_mode="nfm", max_clusters=6)
        out = routes.bandscan_apply(
            {"target": "ground", "modulation": "nfm", "preset_id": "wx",
             "freqs_text": "162.400,162.425,162.450,162.475,162.500,"
                           "162.525,162.550"}, None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["added"], 7)
        self.assertNotIn("dropped_over_clusters", out)


if __name__ == "__main__":
    unittest.main()
