"""chirp.cmd.server command-attribution audit log.

WHY THIS EXISTS
---------------
``CHIRP_CMD_HOST`` is ``0.0.0.0`` on scannerbox-1, so any host that can route
to port 7400 can retune the radio, unauthenticated. For months nothing
recorded who did, and that made one failure undiagnosable: airband was wiped
from a 10-channel scan pool down to a single VFO channel at 119.35 MHz, three
times in one day, always the same signature -- a gain change, ten
``remove_channel``, one ``add_channel``. sb3-ui's own request log held no
matching request, which read as evidence and was not: a client speaking
straight to this port never touches sb3-ui, so its absence from that log is
equally consistent with guilt and innocence.

These tests import the real module and drive a real socket. That is
deliberate. The last two defects in this repo both passed a full green suite
because the tests exec'd functions out of the AST: one tested a function that
a later IIFE shadowed, and one never imported the module it covered, so a
NameError at import took the UI down with 20 tests passing.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

# Import the real module. If this line fails the suite fails, which is the point.
from chirp.cmd import server as S
from chirp.cmd.schema import Response, parse_args


def _recv_json(sock: socket.socket, timeout: float = 3.0) -> dict:
    sock.settimeout(timeout)
    data, _ = sock.recvfrom(65536)
    return json.loads(data.decode("utf-8"))


class _Harness:
    """A real CommandServer with a stub dispatch and a private audit file."""

    def __init__(self, tmpdir: str, name: str = "cmd-audit.log") -> None:
        self.path = Path(tmpdir) / name
        self.seen: list = []
        self._prev_env = os.environ.get("CHIRP_CMD_AUDIT_LOG")
        os.environ["CHIRP_CMD_AUDIT_LOG"] = str(self.path)
        # Reset module-level install state and detach any handler a previous
        # test left behind, so each harness writes only its own file.
        S._audit_handler_installed = False
        S._audit_path = None
        for h in list(S.audit.handlers):
            S.audit.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        self.server = S.CommandServer(
            S.ServerConfig(host="127.0.0.1", port=0), self._dispatch)
        self.server.start()
        self.port = self.server._transport.get_extra_info("sockname")[1]

    def _dispatch(self, env, args):
        self.seen.append((env.cmd, env.id))
        return Response.make_ok(env.id, {"ok": True})

    def send(self, body: dict, sock: socket.socket | None = None) -> dict:
        own = sock is None
        s = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(json.dumps(body).encode("utf-8"), ("127.0.0.1", self.port))
            return _recv_json(s)
        finally:
            if own:
                s.close()

    def send_raw(self, raw: bytes) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(raw, ("127.0.0.1", self.port))
            try:
                _recv_json(s, timeout=1.0)
            except Exception:
                pass
        finally:
            s.close()

    def audit_text(self) -> str:
        # The handler flushes per record; a short settle covers the asyncio
        # thread not having run the callback yet.
        for _ in range(40):
            if self.path.exists() and self.path.stat().st_size:
                break
            time.sleep(0.02)
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def close(self) -> None:
        self.server.stop()
        for h in list(S.audit.handlers):
            S.audit.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        S._audit_handler_installed = False
        S._audit_path = None
        if self._prev_env is None:
            os.environ.pop("CHIRP_CMD_AUDIT_LOG", None)
        else:
            os.environ["CHIRP_CMD_AUDIT_LOG"] = self._prev_env


class AuditWireTests(unittest.TestCase):
    """End-to-end: a datagram in, an attributed line in the audit file."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.h = _Harness(self.tmp.name)

    def tearDown(self) -> None:
        self.h.close()
        self.tmp.cleanup()

    def test_mutating_command_records_cmd_id_and_sender(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        src_port = sock.getsockname()[1]
        try:
            resp = self.h.send({"v": 1, "id": "abc123", "cmd": "set_freq",
                                "args": {"id": "vfo", "mhz": 119.35}}, sock)
        finally:
            sock.close()
        self.assertEqual(resp["status"], "ok")
        text = self.h.audit_text()
        self.assertIn("cmd=set_freq", text)
        self.assertIn("id=abc123", text)
        self.assertIn("from=127.0.0.1:%d" % src_port, text)
        self.assertIn("mhz=119.35", text)

    def test_the_observed_wipe_signature_is_fully_attributed(self):
        """The real failure: gain change, then remove x N, then one add."""
        self.h.send({"v": 1, "id": "g1", "cmd": "set_master_gain",
                     "args": {"db": 29.0}})
        for i in range(3):
            self.h.send({"v": 1, "id": "r%d" % i, "cmd": "remove_channel",
                         "args": {"id": "n4-%d-tower" % i}})
        self.h.send({"v": 1, "id": "a1", "cmd": "add_channel", "args": {
            "channels": [{"id": "vfo", "freq_mhz": 119.35, "mode": "am",
                          "squelch_dbfs": -70.0}]}})
        text = self.h.audit_text()
        self.assertIn("cmd=set_master_gain", text)
        self.assertIn("db=29.0", text)
        self.assertEqual(text.count("cmd=remove_channel"), 3)
        self.assertIn("id=n4-2-tower", text)
        self.assertIn("cmd=add_channel", text)
        self.assertIn("vfo@119.35", text)
        # Every mutating line names a sender.
        for line in text.splitlines():
            if "cmd=" in line:
                self.assertIn("from=127.0.0.1:", line)

    def test_get_status_is_not_logged(self):
        """The UI polls it several times a second; logging it buries the rest."""
        for i in range(5):
            self.h.send({"v": 1, "id": "s%d" % i, "cmd": "get_status", "args": {}})
        self.assertEqual(len(self.h.seen), 5, "dispatch still ran")
        self.assertNotIn("cmd=get_status", self.h.audit_text())

    def test_subscribe_is_not_logged_as_a_mutation(self):
        self.h.send({"v": 1, "id": "sub", "cmd": "subscribe",
                     "args": {"events": ["hit_start"]}})
        self.assertNotIn("cmd=subscribe", self.h.audit_text())

    def test_new_client_is_announced_once_per_host(self):
        for i in range(3):
            self.h.send({"v": 1, "id": "s%d" % i, "cmd": "get_status", "args": {}})
        text = self.h.audit_text()
        self.assertEqual(text.count("new client"), 1, text)
        self.assertIn("new client 127.0.0.1:", text)

    def test_malformed_datagram_still_announces_the_host(self):
        """A client speaking the wrong protocol is still a client."""
        self.h.send_raw(b"this is not json")
        self.assertIn("new client 127.0.0.1:", self.h.audit_text())

    def test_oversized_datagram_still_announces_the_host(self):
        self.h.send_raw(b"x" * (self.h.server.cfg.max_packet + 10))
        self.assertIn("new client 127.0.0.1:", self.h.audit_text())

    def test_rejected_command_is_not_attributed_but_host_is(self):
        """An unknown cmd never reaches dispatch, so there is no cmd= line --
        but the sender is still on record via the new-client notice."""
        resp = self.h.send({"v": 1, "id": "z", "cmd": "no_such_cmd", "args": {}})
        self.assertEqual(resp["status"], "rejected")
        text = self.h.audit_text()
        self.assertNotIn("cmd=no_such_cmd", text)
        self.assertIn("new client", text)

    def test_audit_failure_cannot_break_a_command(self):
        """Close the handler's stream underneath it; the command must still run."""
        for h in S.audit.handlers:
            try:
                h.stream.close()
            except Exception:
                pass
        resp = self.h.send({"v": 1, "id": "after", "cmd": "set_master_gain",
                            "args": {"db": 31.0}})
        self.assertEqual(resp["status"], "ok")
        self.assertIn(("set_master_gain", "after"), self.h.seen)


class PeerTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = S.CommandServer(S.ServerConfig(host="127.0.0.1", port=0),
                                   lambda env, args: Response.make_ok(env.id, {}))

    def test_keyed_by_ip_not_by_port(self):
        """A client gets a fresh source port per socket; port-keying would
        reannounce the same host forever."""
        self.assertTrue(self.srv.note_peer(("10.0.0.5", 1111)))
        self.assertFalse(self.srv.note_peer(("10.0.0.5", 2222)))
        self.assertTrue(self.srv.note_peer(("10.0.0.6", 1111)))

    def test_table_is_capped_and_warns_once(self):
        for i in range(S._AUDIT_MAX_PEERS):
            self.assertTrue(self.srv.note_peer(("10.1.%d.%d" % (i // 256, i % 256), 1)))
        with self.assertLogs("chirp.cmd.server", level="WARNING") as cm:
            self.assertFalse(self.srv.note_peer(("10.9.9.9", 1)))
        self.assertTrue(any("peer table full" in m for m in cm.output))
        # Second overflow is silent, not a log flood.
        self.srv._peer_table_full = True
        self.assertFalse(self.srv.note_peer(("10.9.9.10", 1)))

    def test_already_capped_host_still_rejected_cleanly(self):
        self.srv._seen_peers = {"x%d" % i for i in range(S._AUDIT_MAX_PEERS)}
        self.assertFalse(self.srv.note_peer(("1.2.3.4", 5)))

    def test_malformed_addr_is_not_a_crash(self):
        self.assertFalse(self.srv.note_peer(()))


class ArgSummaryTests(unittest.TestCase):
    def test_add_channel_previews_and_counts(self):
        args = parse_args("add_channel", {"channels": [
            {"id": "c%d" % i, "freq_mhz": 118.0 + i, "mode": "am",
             "squelch_dbfs": -70.0} for i in range(6)]})
        out = S._audit_args("add_channel", args)
        self.assertTrue(out.startswith("n=6 "), out)
        self.assertIn("c0@118.0", out)
        self.assertIn("c3@121.0", out)
        self.assertNotIn("c4@", out)
        self.assertIn("+2", out)

    def test_empty_add_channel(self):
        self.assertEqual(S._audit_args("add_channel",
                                       SimpleNamespace(channels=[])), "n=0")

    def test_whitelisted_fields_only(self):
        args = SimpleNamespace(id="vfo", mhz=119.35, secret="hunter2")
        out = S._audit_args("set_freq", args)
        self.assertIn("id=vfo", out)
        self.assertIn("mhz=119.35", out)
        self.assertNotIn("hunter2", out)

    def test_false_and_zero_are_not_dropped(self):
        out = S._audit_args("set_vad_bypass", SimpleNamespace(id="c", bypass=False))
        self.assertIn("bypass=False", out)
        self.assertIn("db=0.0", S._audit_args("set_master_gain",
                                              SimpleNamespace(db=0.0)))

    def test_never_raises(self):
        class Boom:
            @property
            def id(self):
                raise RuntimeError("nope")
        self.assertEqual(S._audit_args("set_freq", Boom()), "<args unsummarizable>")

    def test_newline_in_an_id_cannot_forge_a_line(self):
        """ids are free-form and the port is unauthenticated."""
        args = SimpleNamespace(id="ok\nMUTATE cmd=set_freq from=1.2.3.4:1", mhz=1.0)
        out = S._audit_args("set_freq", args)
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)

    def test_long_field_is_truncated(self):
        out = S._audit_scrub("x" * 5000)
        self.assertLessEqual(len(out), S._AUDIT_MAX_FIELD + 3)
        self.assertTrue(out.endswith("..."))

    def test_scrub_handles_non_text(self):
        self.assertEqual(S._audit_scrub(None), "None")

    def test_fmt_addr_shapes(self):
        self.assertEqual(S._fmt_addr(("1.2.3.4", 9)), "1.2.3.4:9")
        # A bare string must not be indexed into "s:t".
        self.assertEqual(S._fmt_addr("sock"), "sock")
        self.assertEqual(S._fmt_addr(()), "()")


class HandlerInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._env = {k: os.environ.get(k)
                     for k in ("CHIRP_CMD_AUDIT_LOG", "CHIRP_HIT_LOG")}
        S._audit_handler_installed = False
        S._audit_path = None
        self._handlers = list(S.audit.handlers)
        for h in self._handlers:
            S.audit.removeHandler(h)

    def tearDown(self) -> None:
        for h in list(S.audit.handlers):
            S.audit.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in self._handlers:
            S.audit.addHandler(h)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        S._audit_handler_installed = False
        S._audit_path = None
        self.tmp.cleanup()

    def test_explicit_env_wins(self):
        os.environ["CHIRP_CMD_AUDIT_LOG"] = str(Path(self.tmp.name) / "a.log")
        os.environ["CHIRP_HIT_LOG"] = str(Path(self.tmp.name) / "h" / "hits.jsonl")
        self.assertEqual(S._audit_log_path(), Path(self.tmp.name) / "a.log")

    def test_defaults_beside_the_hit_log(self):
        """Not the daemon's stdout log: that takes a cluster_hop every ~0.8 s."""
        os.environ.pop("CHIRP_CMD_AUDIT_LOG", None)
        os.environ["CHIRP_HIT_LOG"] = "/var/x/chirp/logs/hits.jsonl"
        self.assertEqual(S._audit_log_path(),
                         Path("/var/x/chirp/logs/cmd-audit.log"))

    def test_blank_env_is_ignored(self):
        os.environ["CHIRP_CMD_AUDIT_LOG"] = "   "
        os.environ["CHIRP_HIT_LOG"] = "/var/x/chirp/logs/hits.jsonl"
        self.assertEqual(S._audit_log_path(),
                         Path("/var/x/chirp/logs/cmd-audit.log"))

    def test_no_configured_location_means_no_file(self):
        """There is no last-resort default, on purpose.

        chirp's own test suite starts real CommandServers. With a default
        path, running the suite on scannerbox-1 wrote 49 lines of fixture
        traffic into the live attribution log -- the one file whose entire
        worth is that every line in it is a real command from a real client.
        """
        os.environ.pop("CHIRP_CMD_AUDIT_LOG", None)
        os.environ.pop("CHIRP_HIT_LOG", None)
        self.assertIsNone(S._audit_log_path())

    def test_unconfigured_install_adds_no_handler(self):
        os.environ.pop("CHIRP_CMD_AUDIT_LOG", None)
        os.environ.pop("CHIRP_HIT_LOG", None)
        self.assertIsNone(S.install_audit_handler())
        self.assertEqual(S.audit.handlers, [])
        # Still idempotent, and still silent on a second call.
        self.assertIsNone(S.install_audit_handler())
        self.assertEqual(S.audit.handlers, [])

    def test_a_bare_command_server_writes_nothing(self):
        """The regression itself: start a server the way chirp's other tests
        do -- no audit env at all -- and no file may be created anywhere."""
        os.environ.pop("CHIRP_CMD_AUDIT_LOG", None)
        os.environ.pop("CHIRP_HIT_LOG", None)
        srv = S.CommandServer(S.ServerConfig(host="127.0.0.1", port=0),
                              lambda env, args: Response.make_ok(env.id, {}))
        srv.start()
        try:
            port = srv._transport.get_extra_info("sockname")[1]
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(json.dumps({"v": 1, "id": "x", "cmd": "set_master_gain",
                                        "args": {"db": 3.0}}).encode("utf-8"),
                            ("127.0.0.1", port))
                _recv_json(sock)
            finally:
                sock.close()
        finally:
            srv.stop()
        self.assertEqual(S.audit.handlers, [])

    def test_install_is_idempotent(self):
        os.environ["CHIRP_CMD_AUDIT_LOG"] = str(Path(self.tmp.name) / "b.log")
        first = S.install_audit_handler()
        self.assertEqual(len(S.audit.handlers), 1)
        self.assertEqual(S.install_audit_handler(), first)
        self.assertEqual(len(S.audit.handlers), 1)

    def test_unwritable_path_degrades_instead_of_raising(self):
        """Attribution should fall back to the root handlers, not blow up the
        daemon at startup."""
        os.environ["CHIRP_CMD_AUDIT_LOG"] = "/proc/definitely/not/writable.log"
        with self.assertLogs("chirp.cmd.server", level="WARNING"):
            self.assertIsNone(S.install_audit_handler())
        self.assertEqual(len(S.audit.handlers), 0)

    def test_handler_rotates(self):
        os.environ["CHIRP_CMD_AUDIT_LOG"] = str(Path(self.tmp.name) / "r.log")
        S.install_audit_handler()
        h = S.audit.handlers[0]
        self.assertIsInstance(h, logging.handlers.RotatingFileHandler)
        self.assertGreater(h.maxBytes, 0)
        self.assertGreater(h.backupCount, 0)

    def test_level_survives_a_quieted_root_logger(self):
        """Propagation consults ancestor HANDLERS, never ancestor logger
        levels -- so pinning this logger keeps attribution alive even if the
        daemon's log level is turned down later."""
        os.environ["CHIRP_CMD_AUDIT_LOG"] = str(Path(self.tmp.name) / "q.log")
        path = S.install_audit_handler()
        root = logging.getLogger()
        prev = root.level
        root.setLevel(logging.CRITICAL)
        try:
            S.audit.info("cmd=set_freq id=x from=1.2.3.4:5")
        finally:
            root.setLevel(prev)
        self.assertIn("cmd=set_freq", Path(path).read_text(encoding="utf-8"))


class ModuleContractTests(unittest.TestCase):
    """Guards against the two ways this repo has shipped green-but-broken."""

    def test_module_imports_from_scratch(self):
        """A module-level name used above its definition is an import-time
        NameError, and an AST-exec test would never see it."""
        for name in [m for m in list(sys.modules) if m.startswith("chirp.cmd")]:
            sys.modules.pop(name, None)
        mod = importlib.import_module("chirp.cmd.server")
        self.assertTrue(hasattr(mod, "install_audit_handler"))
        self.assertTrue(hasattr(mod, "READ_ONLY_COMMANDS"))

    def test_read_only_set_matches_the_schema(self):
        """Every read-only name must be a real command, or the exclusion is a
        typo that silently logs nothing."""
        from chirp.cmd.schema import COMMAND_ARGS
        for cmd in S.READ_ONLY_COMMANDS:
            self.assertIn(cmd, COMMAND_ARGS, cmd)

    def test_every_other_command_is_audited(self):
        """If a mutating command is added to the schema it is audited by
        default -- the whitelist is of exclusions, not of inclusions."""
        from chirp.cmd.schema import COMMAND_ARGS
        mutating = set(COMMAND_ARGS) - set(S.READ_ONLY_COMMANDS)
        self.assertIn("add_channel", mutating)
        self.assertIn("remove_channel", mutating)
        self.assertIn("set_master_gain", mutating)
        self.assertIn("reset", mutating)
        self.assertNotIn("get_status", mutating)

    def test_audit_call_sits_before_dispatch(self):
        """Logged before the flowgraph touches it, so a command that wedges
        the daemon still leaves its sender named."""
        src = Path(S.__file__).read_text(encoding="utf-8")
        body = src.split("def datagram_received", 1)[1]
        self.assertLess(body.index("audit.info(\"cmd=%s"),
                        body.index("resp = self.dispatch(env, args)"))


if __name__ == "__main__":
    unittest.main()
